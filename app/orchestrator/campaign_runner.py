import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.dedup import load_known_phones, load_known_place_keys
from app.export.excel import export_campaign_to_excel
from app.models import (
    Batch,
    BatchStatus,
    Campaign,
    CampaignStatus,
    Lead,
    SkippedLead,
)
from app.activity import push as activity_push
from app.orchestrator.lead_saver import LeadSaver
from app.orchestrator.saver_state import mark_saver_active, mark_saver_inactive
from app.scrape_profile import resolve_profile, set_active_profile
from app.scrapers.maps.scraper import MapsScraper, ScrapeStats
from app.scrapers.social.agent import social_agent
from app.scrapers.social.google_lookup import SocialJob
from app.utils.delays import batch_rest_delay

logger = structlog.get_logger(__name__)

# Watchdog tuning: how often to check for stalled batches, and how long a
# batch's most recent job can go without any DB progress update before we
# consider it stuck and auto-restart it. Kept generous to avoid false
# positives from legitimate captcha backoff (up to 10 min) + rest delay.
WATCHDOG_CHECK_INTERVAL_S = 300
WATCHDOG_STALL_THRESHOLD_S = 1200


class CampaignManager:
    def __init__(self) -> None:
        self._scraper = MapsScraper()
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._stop_flags: dict[int, asyncio.Event] = {}
        self._batch_tasks: dict[int, asyncio.Task] = {}
        self._batch_stop_flags: dict[int, asyncio.Event] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_restarting: set[int] = set()
        self._last_resume_attempt: dict[int, float] = {}

    async def try_resume_batch(self, batch_id: int, *, min_interval_s: float = 30.0) -> bool:
        """Resume a DB-running batch if its task is missing (debounced)."""
        if self.is_batch_running(batch_id):
            return False
        now = time.time()
        last = self._last_resume_attempt.get(batch_id, 0.0)
        if now - last < min_interval_s:
            return False
        self._last_resume_attempt[batch_id] = now
        await self.start_batch(batch_id)
        return True

    async def shutdown(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        for task in list(self._running_tasks.values()) + list(self._batch_tasks.values()):
            task.cancel()
        await self._scraper.close()
        await social_agent.close()

    def start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(WATCHDOG_CHECK_INTERVAL_S)
                await self._check_for_stalled_batches()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("batch.watchdog.error", error=str(exc))

    async def _check_for_stalled_batches(self) -> None:
        for batch_id in list(self._batch_tasks.keys()):
            if not self.is_batch_running(batch_id) or batch_id in self._watchdog_restarting:
                continue
            age_s: float | None = None
            latest = None
            async with SessionLocal() as session:
                batch = await session.get(Batch, batch_id)
                if not batch:
                    continue
                result = await session.execute(
                    select(Campaign)
                    .where(Campaign.batch_id == batch_id)
                    .order_by(Campaign.id.desc())
                    .limit(1)
                )
                latest = result.scalars().first()
                if latest and latest.updated_at:
                    last_update = latest.updated_at
                    if last_update.tzinfo is None:
                        last_update = last_update.replace(tzinfo=timezone.utc)
                    age_s = (datetime.now(timezone.utc) - last_update).total_seconds()
                elif batch.updated_at:
                    last_update = batch.updated_at
                    if last_update.tzinfo is None:
                        last_update = last_update.replace(tzinfo=timezone.utc)
                    age_s = (datetime.now(timezone.utc) - last_update).total_seconds()

            if age_s is not None:
                stall_limit = WATCHDOG_STALL_THRESHOLD_S
                if latest is None:
                    stall_limit = 300  # no campaigns yet — restart sooner if stuck at startup
                if age_s >= stall_limit:
                    logger.warning(
                        "batch.watchdog.stalled",
                        batch_id=batch_id,
                        stalled_minutes=round(age_s / 60, 1),
                    )
                    asyncio.create_task(self._restart_stalled_batch(batch_id))

    async def _restart_stalled_batch(self, batch_id: int) -> None:
        self._watchdog_restarting.add(batch_id)
        try:
            task = self._batch_tasks.get(batch_id)
            stop_event = self._batch_stop_flags.get(batch_id)
            if stop_event:
                stop_event.set()
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=20)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            self._batch_tasks.pop(batch_id, None)
            self._batch_stop_flags.pop(batch_id, None)

            # Force a clean browser teardown so the restart launches fresh.
            try:
                await self._scraper.close()
            except Exception:
                pass

            logger.info("batch.watchdog.restarted", batch_id=batch_id)
            await self.start_batch(batch_id)
        finally:
            self._watchdog_restarting.discard(batch_id)

    def is_running(self, campaign_id: int) -> bool:
        task = self._running_tasks.get(campaign_id)
        return task is not None and not task.done()

    def is_batch_running(self, batch_id: int) -> bool:
        task = self._batch_tasks.get(batch_id)
        return task is not None and not task.done()

    async def start_campaign(self, campaign_id: int) -> None:
        if self.is_running(campaign_id):
            return
        stop_event = asyncio.Event()
        self._stop_flags[campaign_id] = stop_event
        task = asyncio.create_task(self._run_campaign_task(campaign_id, stop_event, batch_mode=False))
        self._running_tasks[campaign_id] = task

    async def stop_campaign(self, campaign_id: int) -> None:
        event = self._stop_flags.get(campaign_id)
        if event:
            event.set()

    async def start_batch(self, batch_id: int) -> None:
        if self.is_batch_running(batch_id):
            return
        # Only one overnight at a time — stop leftovers first
        if self.any_batch_running():
            await self.stop_all_batches()
        stop_event = asyncio.Event()
        self._batch_stop_flags[batch_id] = stop_event
        task = asyncio.create_task(self._run_batch_task(batch_id, stop_event))
        self._batch_tasks[batch_id] = task

    async def stop_batch(self, batch_id: int) -> None:
        event = self._batch_stop_flags.get(batch_id)
        if event:
            event.set()
        # Always mark DB cancelled so UI doesn't stay stuck on "running"
        async with SessionLocal() as session:
            batch = await session.get(Batch, batch_id)
            if batch and batch.status in (
                BatchStatus.RUNNING.value,
                BatchStatus.QUEUED.value,
                BatchStatus.PAUSED.value,
            ):
                batch.status = BatchStatus.CANCELLED.value
                batch.current_job_label = None
                camps = await session.execute(
                    select(Campaign).where(
                        Campaign.batch_id == batch_id,
                        Campaign.status.in_(
                            [
                                CampaignStatus.RUNNING.value,
                                CampaignStatus.QUEUED.value,
                                CampaignStatus.PAUSED.value,
                            ]
                        ),
                    )
                )
                for c in camps.scalars().all():
                    c.status = CampaignStatus.CANCELLED.value
                await session.commit()

    async def stop_all_batches(self) -> int:
        """Stop every in-memory batch task and cancel stuck DB rows."""
        for batch_id in list(self._batch_stop_flags.keys()):
            await self.stop_batch(batch_id)
        for campaign_id in list(self._stop_flags.keys()):
            await self.stop_campaign(campaign_id)

        cancelled = 0
        async with SessionLocal() as session:
            result = await session.execute(
                select(Batch).where(
                    Batch.status.in_(
                        [
                            BatchStatus.RUNNING.value,
                            BatchStatus.QUEUED.value,
                            BatchStatus.PAUSED.value,
                        ]
                    )
                )
            )
            for batch in result.scalars().all():
                batch.status = BatchStatus.CANCELLED.value
                batch.current_job_label = None
                cancelled += 1
            camps = await session.execute(
                select(Campaign).where(
                    Campaign.status.in_(
                        [
                            CampaignStatus.RUNNING.value,
                            CampaignStatus.QUEUED.value,
                            CampaignStatus.PAUSED.value,
                        ]
                    )
                )
            )
            for c in camps.scalars().all():
                c.status = CampaignStatus.CANCELLED.value
            await session.commit()
        logger.info("batch.stop_all", cancelled=cancelled)
        return cancelled

    def any_batch_running(self) -> bool:
        return any(self.is_batch_running(bid) for bid in self._batch_tasks)

    async def resume_interrupted_batches(self) -> int:
        """Restart batches left as running/queued in DB after an app restart."""
        resumed = 0
        async with SessionLocal() as session:
            result = await session.execute(
                select(Batch).where(
                    Batch.status.in_(
                        [
                            BatchStatus.RUNNING.value,
                            BatchStatus.QUEUED.value,
                            BatchStatus.PAUSED.value,
                        ]
                    )
                )
            )
            batches = list(result.scalars().all())

        for batch in batches:
            if self.is_batch_running(batch.id):
                continue
            logger.info("batch.auto_resume", batch_id=batch.id, name=batch.name)
            await self.start_batch(batch.id)
            resumed += 1
        return resumed

    async def _enqueue_social_for_campaign(self, campaign_id: int) -> None:
        async with SessionLocal() as session:
            result = await session.execute(select(Lead).where(Lead.campaign_id == campaign_id))
            leads = list(result.scalars().all())
        for lead in leads:
            if lead.facebook_url and lead.instagram_handle:
                continue
            await social_agent.enqueue(
                SocialJob(
                    lead_id=lead.id,
                    business_name=lead.business_name,
                    city=lead.city or "",
                    governorate=lead.governorate or "",
                    maps_website=lead.website,
                    phone=lead.phone,
                )
            )

    async def _scrape_campaign(
        self,
        campaign_id: int,
        stop_event: asyncio.Event,
        *,
        batch_mode: bool,
        known_phones: set[str] | None = None,
        known_place_keys: set[str] | None = None,
    ) -> ScrapeStats:
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if not campaign:
                raise ValueError(f"Campaign {campaign_id} not found")
            campaign.status = CampaignStatus.RUNNING.value
            campaign.error_message = None
            await session.commit()
            niche = campaign.niche
            governorate = campaign.governorate
            city = campaign.city
            max_results = campaign.max_results
            unlimited = campaign.max_results <= 0 or campaign.batch_id is not None

        if known_phones is None:
            known_phones = await load_known_phones()
        if known_place_keys is None:
            known_place_keys = await load_known_place_keys()

        defer_social = resolve_profile().skip_social  # skip_social: saver never queues the Google lookup
        saver = LeadSaver(campaign_id, defer_social=defer_social)
        saver.start()
        mark_saver_active(campaign_id)

        async def on_progress(data: dict[str, Any]) -> None:
            async with SessionLocal() as progress_session:
                db_campaign = await progress_session.get(Campaign, campaign_id)
                if not db_campaign:
                    return
                db_campaign.progress_scanned = data["scanned"]
                db_campaign.progress_with_phone = data["with_phone"]
                db_campaign.progress_no_phone = data["no_phone"]
                db_campaign.progress_no_website = data["no_website"]
                await progress_session.commit()

        def on_lead(lead_data: dict[str, Any]) -> None:
            saver.push(lead_data)

        async def should_stop() -> bool:
            return stop_event.is_set()

        try:
            stats = await self._scraper.scrape_campaign(
                niche=niche,
                governorate=governorate,
                city=city,
                max_results=max_results,
                on_progress=on_progress,
                on_lead=on_lead,
                should_stop=should_stop,
                only_no_website=True,
                known_phones=known_phones,
                known_place_keys=known_place_keys,
                unlimited=unlimited,
            )
        finally:
            await saver.stop()
            mark_saver_inactive(campaign_id)

        await self._finalize_campaign(campaign_id, stats, cancelled=stop_event.is_set())

        return stats

    async def _finalize_campaign(
        self, campaign_id: int, stats: ScrapeStats, cancelled: bool
    ) -> None:
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if not campaign:
                return

            for skipped_data in stats.skipped:
                session.add(
                    SkippedLead(
                        campaign_id=campaign.id,
                        business_name=skipped_data.get("business_name"),
                        maps_url=skipped_data.get("maps_url"),
                        reason=skipped_data.get("reason", "unknown"),
                    )
                )

            campaign.progress_scanned = stats.scanned
            campaign.progress_with_phone = stats.with_phone
            campaign.progress_no_phone = stats.no_phone
            campaign.progress_no_website = stats.no_website

            await session.flush()

            leads_result = await session.execute(
                select(Lead).where(Lead.campaign_id == campaign.id)
            )
            skipped_result = await session.execute(
                select(SkippedLead).where(SkippedLead.campaign_id == campaign.id)
            )
            leads = list(leads_result.scalars().all())
            skipped = list(skipped_result.scalars().all())

            if leads:
                export_path = export_campaign_to_excel(campaign, leads, skipped)
                campaign.export_path = str(export_path)

            campaign.status = (
                CampaignStatus.CANCELLED.value if cancelled else CampaignStatus.COMPLETED.value
            )
            await session.commit()
            logger.info("campaign.completed", campaign_id=campaign_id, leads=len(leads))

    async def _run_campaign_task(
        self, campaign_id: int, stop_event: asyncio.Event, *, batch_mode: bool
    ) -> None:
        try:
            if not batch_mode:
                await social_agent.start()
            await self._scrape_campaign(campaign_id, stop_event, batch_mode=batch_mode)
        except Exception as exc:
            logger.exception("campaign.failed", campaign_id=campaign_id, error=str(exc))
            async with SessionLocal() as session:
                campaign = await session.get(Campaign, campaign_id)
                if campaign:
                    campaign.status = CampaignStatus.FAILED.value
                    campaign.error_message = str(exc)
                    await session.commit()
        finally:
            self._running_tasks.pop(campaign_id, None)
            self._stop_flags.pop(campaign_id, None)

    async def _run_batch_task(self, batch_id: int, stop_event: asyncio.Event) -> None:
        try:
            async with SessionLocal() as session:
                batch = await session.get(Batch, batch_id)
                if not batch:
                    return
                niches = list(batch.niches)
                targets = list(batch.targets)
                profile_id = getattr(batch, "scrape_profile", None) or "safe"
                batch.status = BatchStatus.RUNNING.value
                batch.error_message = None
                batch.current_job_label = "Preparing batch..."
                await session.commit()

                # Resume support: skip (niche, city, governorate) jobs that already
                # completed in a prior run of this batch (e.g. before a restart).
                done_result = await session.execute(
                    select(Campaign.niche, Campaign.city, Campaign.governorate).where(
                        Campaign.batch_id == batch_id,
                        Campaign.status == CampaignStatus.COMPLETED.value,
                    )
                )
                done_jobs = {(n, c, g) for n, c, g in done_result.all()}
                completed = len(done_jobs)
                total_leads = batch.total_leads or 0

            activity_push("info", "batch.preparing", batch_id=batch_id)
            set_active_profile(profile_id)
            profile = resolve_profile(profile_id)
            logger.info(
                "batch.profile",
                batch_id=batch_id,
                profile=profile.id,
                workers=profile.maps_workers,
                skip_social=profile.skip_social,
            )

            skip_social = profile.skip_social
            if not skip_social:
                await social_agent.start()

            known_phones = await load_known_phones()
            known_place_keys = await load_known_place_keys()
            batch_campaign_ids: list[int] = []

            all_jobs = [(niche, target) for target in targets for niche in niches]
            jobs = [
                (niche, target)
                for niche, target in all_jobs
                if (niche, target["city"], target["governorate"]) not in done_jobs
            ]

            async with SessionLocal() as session:
                batch = await session.get(Batch, batch_id)
                if batch:
                    batch.total_jobs = len(all_jobs)
                    batch.completed_jobs = completed
                    await session.commit()

            if completed:
                logger.info(
                    "batch.resumed", batch_id=batch_id, already_done=completed, remaining=len(jobs)
                )

            for niche, target in jobs:
                if stop_event.is_set():
                    logger.info("batch.stopped_by_user", batch_id=batch_id)
                    break

                label = f"{niche} — {target['city']}, {target['governorate']}"
                logger.info("batch.job.start", label=label, batch_id=batch_id)
                activity_push("info", "batch.job.start", label=label, batch_id=batch_id)
                async with SessionLocal() as session:
                    batch = await session.get(Batch, batch_id)
                    if not batch:
                        return
                    batch.current_job_label = label
                    await session.commit()

                activity_push("info", "browser.starting", label=label)

                async with SessionLocal() as session:
                    campaign = Campaign(
                        batch_id=batch_id,
                        niche=niche,
                        governorate=target["governorate"],
                        city=target["city"],
                        max_results=0,
                        status=CampaignStatus.QUEUED.value,
                    )
                    session.add(campaign)
                    await session.commit()
                    await session.refresh(campaign)
                    campaign_id = campaign.id
                    batch_campaign_ids.append(campaign_id)

                campaign_stop = asyncio.Event()

                async def relay_stop() -> None:
                    while not campaign_stop.is_set():
                        if stop_event.is_set():
                            campaign_stop.set()
                            return
                        await asyncio.sleep(1)

                relay_task = asyncio.create_task(relay_stop())
                try:
                    stats = await self._scrape_campaign(
                        campaign_id,
                        campaign_stop,
                        batch_mode=True,
                        known_phones=known_phones,
                        known_place_keys=known_place_keys,
                    )
                    total_leads += stats.with_phone
                except Exception as exc:
                    logger.exception("batch.job.failed", label=label, error=str(exc))
                    async with SessionLocal() as session:
                        c = await session.get(Campaign, campaign_id)
                        if c:
                            c.status = CampaignStatus.FAILED.value
                            c.error_message = str(exc)
                            await session.commit()
                finally:
                    campaign_stop.set()
                    relay_task.cancel()

                completed += 1
                async with SessionLocal() as session:
                    batch = await session.get(Batch, batch_id)
                    if batch:
                        batch.completed_jobs = completed
                        batch.total_leads = total_leads
                        await session.commit()

                if not stop_event.is_set():
                    await batch_rest_delay()

            async with SessionLocal() as session:
                batch = await session.get(Batch, batch_id)
                if batch:
                    batch.status = (
                        BatchStatus.CANCELLED.value
                        if stop_event.is_set()
                        else BatchStatus.COMPLETED.value
                    )
                    batch.current_job_label = None
                    await session.commit()

            if not skip_social and batch_campaign_ids:
                for cid in batch_campaign_ids:
                    if stop_event.is_set():
                        break
                    await self._enqueue_social_for_campaign(cid)

        except Exception as exc:
            logger.exception("batch.failed", batch_id=batch_id, error=str(exc))
            async with SessionLocal() as session:
                batch = await session.get(Batch, batch_id)
                if batch:
                    batch.status = BatchStatus.FAILED.value
                    batch.error_message = str(exc)
                    await session.commit()
        finally:
            set_active_profile(None)
            self._batch_tasks.pop(batch_id, None)
            self._batch_stop_flags.pop(batch_id, None)


campaign_manager = CampaignManager()
