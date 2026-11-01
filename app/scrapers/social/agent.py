"""Background social-media agent — separate browser + proxy, parallel workers."""

import asyncio
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.export.excel import export_campaign_to_excel
from app.models import Campaign, Lead, SkippedLead
from app.orchestrator.saver_state import is_saver_active
from app.scrapers.browser_factory import BrowserFactory, CaptchaDetectedError, safe_goto
from app.scrapers.social.google_lookup import SocialJob, lookup_social_links

logger = structlog.get_logger(__name__)


class SocialAgent:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[SocialJob | None] = asyncio.Queue()
        self._factory = BrowserFactory("social")
        self._workers: list[asyncio.Task] = []
        self._started = False
        self._shutting_down = False

    async def start(self) -> None:
        if self._started:
            return
        self._shutting_down = False
        await self._factory.get_context()
        for _ in range(settings.social_workers):
            self._workers.append(asyncio.create_task(self._worker()))
        self._started = True
        logger.info("social.agent.started", workers=settings.social_workers)

    async def close(self) -> None:
        self._shutting_down = True
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
        await self._factory.shutdown()
        self._started = False

    async def enqueue(self, job: SocialJob) -> None:
        if not self._started:
            await self.start()
        await self._queue.put(job)

    async def _worker(self) -> None:
        while not self._shutting_down:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            if job is None:
                self._queue.task_done()
                break
            try:
                if self._shutting_down:
                    self._queue.task_done()
                    break
                context = await self._factory.get_context()
                social = await lookup_social_links(
                    context,
                    business_name=job.business_name,
                    city=job.city,
                    governorate=job.governorate,
                    maps_website=job.maps_website,
                    phone=job.phone,
                    captcha_rotate=self._factory.rotate_on_captcha,
                )
                await self._update_lead(job.lead_id, social)
            except CaptchaDetectedError:
                if self._shutting_down:
                    self._queue.task_done()
                    break
                logger.warning("social.captcha.detected", lead_id=job.lead_id)
                try:
                    await asyncio.wait_for(
                        self._factory.rotate_on_captcha(), timeout=30,
                    )
                except asyncio.TimeoutError:
                    logger.warning("social.captcha.backoff_skipped")
            except asyncio.CancelledError:
                self._queue.task_done()
                return
            except Exception as exc:
                logger.exception("social.worker.error", lead_id=job.lead_id, error=str(exc))
            finally:
                self._queue.task_done()

    async def _update_lead(self, lead_id: int, social: dict[str, Any]) -> None:
        async with SessionLocal() as session:
            lead = await session.get(Lead, lead_id)
            if not lead:
                return
            if social.get("facebook_url"):
                lead.facebook_url = social["facebook_url"]
            if social.get("instagram_handle"):
                lead.instagram_handle = social["instagram_handle"]
            if social.get("social_confidence"):
                lead.social_confidence = social["social_confidence"]
            await session.commit()

            if is_saver_active(lead.campaign_id):
                return

            campaign = await session.get(Campaign, lead.campaign_id)
            if not campaign:
                return
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
                await session.commit()


social_agent = SocialAgent()
