"""Non-blocking lead persistence with batched Excel export (major speedup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.dedup import register_known_leads
from app.export.excel import export_campaign_to_excel
from app.models import Campaign, Lead, SkippedLead
from app.scrapers.social.google_lookup import SocialJob

logger = structlog.get_logger(__name__)


class LeadSaver:
    """Queues lead saves so Maps workers never block on Excel/DB."""

    def __init__(self, campaign_id: int, *, defer_social: bool = False) -> None:
        self.campaign_id = campaign_id
        self.defer_social = defer_social
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._leads_since_export = 0
        self._last_export_at = 0.0
        self._export_lock = asyncio.Lock()
        self._pending_brain: list[dict] = []

    def start(self) -> None:
        if not self._worker_task:
            self._worker_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self._queue.put(None)
        if self._worker_task:
            await self._worker_task
            self._worker_task = None
        await self._flush_brain()
        await self._export_if_needed(force=True)

    def push(self, lead_data: dict[str, Any]) -> None:
        """Fire-and-forget — returns immediately."""
        self._queue.put_nowait(lead_data)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    break
                await self._save_one(item)
            except Exception as exc:
                logger.exception("lead_saver.error", error=str(exc))
            finally:
                self._queue.task_done()

    async def _save_one(self, lead_data: dict[str, Any]) -> None:
        async with SessionLocal() as session:
            lead = Lead(
                campaign_id=self.campaign_id,
                phone=lead_data["phone"],
                phone_display=lead_data.get("phone_display"),
                business_name=lead_data["business_name"],
                category=lead_data.get("category"),
                address=lead_data.get("address"),
                city=lead_data.get("city"),
                governorate=lead_data.get("governorate"),
                maps_url=lead_data.get("maps_url"),
                website=lead_data.get("website"),
                website_status=lead_data.get("website_status", "missing"),
            )
            session.add(lead)
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign:
                campaign.progress_with_phone = (campaign.progress_with_phone or 0) + 1
                campaign.progress_no_website = (campaign.progress_no_website or 0) + 1
            await session.commit()
            await session.refresh(lead)
            lead_id = lead.id

        self._pending_brain.append(lead_data)
        if len(self._pending_brain) >= settings.brain_batch_size:
            await self._flush_brain()

        self._leads_since_export += 1
        await self._export_if_needed()

        if not self.defer_social:
            from app.scrapers.social.agent import social_agent

            await social_agent.enqueue(
                SocialJob(
                    lead_id=lead_id,
                    business_name=lead_data["business_name"],
                    city=lead_data.get("city", ""),
                    governorate=lead_data.get("governorate", ""),
                    maps_website=lead_data.get("maps_website"),
                    phone=lead_data.get("phone"),
                )
            )

    async def _flush_brain(self) -> None:
        if not self._pending_brain:
            return
        batch = self._pending_brain[:]
        self._pending_brain.clear()
        await register_known_leads(batch, self.campaign_id)

    async def _export_if_needed(self, *, force: bool = False) -> None:
        now = time.time()
        interval = settings.export_interval_seconds
        batch_size = settings.export_batch_size
        due = (
            force
            or self._leads_since_export >= batch_size
            or (now - self._last_export_at) >= interval
        )
        if not due:
            return

        async with self._export_lock:
            if not force and self._leads_since_export < batch_size:
                if (time.time() - self._last_export_at) < interval:
                    return

            async with SessionLocal() as session:
                campaign = await session.get(Campaign, self.campaign_id)
                if not campaign:
                    return
                leads_result = await session.execute(
                    select(Lead).where(Lead.campaign_id == self.campaign_id)
                )
                skipped_result = await session.execute(
                    select(SkippedLead).where(SkippedLead.campaign_id == self.campaign_id)
                )
                leads = list(leads_result.scalars().all())
                skipped = list(skipped_result.scalars().all())
                if leads:
                    export_path = export_campaign_to_excel(campaign, leads, skipped)
                    campaign.export_path = str(export_path)
                    await session.commit()
                    logger.info("export.batched", campaign_id=self.campaign_id, leads=len(leads))

            self._leads_since_export = 0
            self._last_export_at = time.time()


# Campaigns with active savers — social agent skips per-lead Excel refresh
from app.orchestrator.saver_state import (
    mark_saver_active,
    mark_saver_inactive,
    is_saver_active,
)

__all__ = ["LeadSaver", "mark_saver_active", "mark_saver_inactive", "is_saver_active"]
