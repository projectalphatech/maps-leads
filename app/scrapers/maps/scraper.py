import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from playwright.async_api import BrowserContext, Page
from playwright._impl._errors import TargetClosedError

from app.config import settings
from app.scrapers.browser_factory import BrowserFactory, CaptchaDetectedError, safe_goto
from app.scrapers.maps.helpers import build_maps_search_url, extract_place_key, is_target_lead
from app.scrapers.maps.parser import (
    parse_listing_panel,
    stream_result_links,
    wait_for_results_feed,
)
from app.utils.delays import human_delay, is_turbo, listing_delay

logger = structlog.get_logger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]]
LeadCallback = Callable[[dict], Awaitable[None]]
ShouldStopCallback = Callable[[], Awaitable[bool]]


def _worker_count() -> int:
    from app.scrape_profile import resolve_profile
    from app.scrapers.captcha_guard import recommended_workers

    profile = resolve_profile()
    return recommended_workers(profile.maps_workers)


def _safe_log_text(value: str | None) -> str:
    """Avoid Windows console encoding crashes while preserving readable text."""
    if not value:
        return ""
    try:
        return value.encode("cp1252").decode("cp1252")
    except Exception:
        return value.encode("ascii", "backslashreplace").decode("ascii")


@dataclass
class ScrapeStats:
    scanned: int = 0
    with_phone: int = 0
    no_phone: int = 0
    no_website: int = 0
    duplicate_known: int = 0
    saved_leads: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


class MapsScraper:
    def __init__(self) -> None:
        self._factory = BrowserFactory("maps")

    async def _ensure_context(self) -> BrowserContext:
        return await self._factory.get_context()

    async def _recover_context(self) -> BrowserContext:
        """Restart the browser context after a crash."""
        logger.warning("browser.context.crashed", profile="maps")
        try:
            await self._factory.close()
        except Exception:
            pass
        return await self._factory.get_context(force_rotate=True)

    async def _goto_with_captcha_recovery(self, page: Page, url: str) -> Page:
        from app.scrapers.captcha_guard import record_success, wait_if_paused

        for attempt in range(settings.captcha_max_retries):
            await wait_if_paused()
            try:
                await safe_goto(page, url)
                await record_success()
                return page
            except TargetClosedError:
                logger.warning("browser.page.dead", url=url, attempt=attempt + 1)
                new_context = await self._recover_context()
                page = await new_context.new_page()
                if attempt >= settings.captcha_max_retries - 1:
                    raise
            except CaptchaDetectedError:
                logger.warning("maps.captcha.detected", url=url, attempt=attempt + 1)
                if attempt >= settings.captcha_max_retries - 1:
                    raise
                try:
                    await page.close()
                except Exception:
                    pass
                new_context = await self._factory.rotate_on_captcha()
                page = await new_context.new_page()
        return page

    async def close(self) -> None:
        await self._factory.shutdown()

    async def scrape_campaign(
        self,
        *,
        niche: str,
        governorate: str,
        city: str,
        max_results: int | None = None,
        on_progress: ProgressCallback | None = None,
        on_lead: LeadCallback | None = None,
        should_stop: ShouldStopCallback | None = None,
        only_no_website: bool = True,
        known_phones: set[str] | None = None,
        known_place_keys: set[str] | None = None,
        unlimited: bool = False,
    ) -> ScrapeStats:
        stats = ScrapeStats()
        known_phones = known_phones if known_phones is not None else set()
        known_place_keys = known_place_keys if known_place_keys is not None else set()
        scrape_limit = None if unlimited or not max_results or max_results <= 0 else max_results
        workers_n = _worker_count()

        context = await self._ensure_context()
        search_url = build_maps_search_url(niche, city, governorate)
        logger.info(
            "maps.search.start",
            url=search_url,
            unlimited=unlimited,
            turbo=is_turbo(),
            workers=workers_n,
        )

        link_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=100)
        state_lock = asyncio.Lock()
        seen_phones: set[str] = set()
        total_discovered = 0
        progress_counter = 0

        async def producer() -> None:
            nonlocal total_discovered, context
            last_error: Exception | None = None
            try:
                for attempt in range(1, 3):
                    page: Page | None = None
                    try:
                        logger.info("maps.producer.start", attempt=attempt, url=search_url)
                        page = await context.new_page()

                        async def _run_search() -> None:
                            nonlocal total_discovered
                            nonlocal page
                            page = await self._goto_with_captcha_recovery(page, search_url)
                            await human_delay(300, 800)
                            await wait_for_results_feed(page)

                            async for link in stream_result_links(page, scrape_limit):
                                if should_stop and await should_stop():
                                    break
                                total_discovered += 1
                                await link_queue.put(link)

                            logger.info("maps.scroll.done", discovered=total_discovered)

                        await asyncio.wait_for(_run_search(), timeout=180)
                        return
                    except asyncio.TimeoutError:
                        last_error = TimeoutError("maps search timed out after 180s")
                        logger.warning("maps.producer.timeout", attempt=attempt, url=search_url)
                        context = await self._recover_context()
                    except Exception as exc:
                        last_error = exc
                        logger.exception("maps.search.failed", url=search_url, error=str(exc))
                        if attempt < 2:
                            context = await self._recover_context()
                    finally:
                        if page:
                            try:
                                await page.close()
                            except Exception:
                                pass

                async with state_lock:
                    stats.skipped.append(
                        {
                            "business_name": None,
                            "maps_url": search_url,
                            "reason": f"search_error:{type(last_error).__name__ if last_error else 'unknown'}",
                        }
                    )
            finally:
                for _ in range(workers_n):
                    await link_queue.put(None)

        async def process_listing(listing_page: Page, link: str) -> None:
            nonlocal progress_counter
            link_place_key = extract_place_key(link)
            if link_place_key and link_place_key in known_place_keys:
                async with state_lock:
                    stats.duplicate_known += 1
                    stats.skipped.append(
                        {"business_name": None, "maps_url": link, "reason": "already_known"}
                    )
                return

            listing_name = "Unknown"
            try:
                listing_page = await self._goto_with_captcha_recovery(listing_page, link)
                if not is_turbo():
                    try:
                        await listing_page.wait_for_selector("h1", timeout=12000)
                    except Exception:
                        pass
                else:
                    try:
                        await listing_page.wait_for_selector("h1", timeout=6000)
                    except Exception:
                        pass

                await listing_delay()
                listing = await parse_listing_panel(listing_page, fast=is_turbo())
                listing_name = listing.business_name

                async with state_lock:
                    stats.scanned += 1
                    progress_counter += 1

                if not listing.has_valid_phone or not listing.phone_normalized:
                    async with state_lock:
                        stats.no_phone += 1
                        stats.skipped.append(
                            {
                                "business_name": listing.business_name,
                                "maps_url": listing.maps_url or link,
                                "reason": "no_phone",
                            }
                        )
                    if stats.no_phone % 10 == 1:
                        logger.info(
                            "maps.skip.no_phone",
                            name=_safe_log_text(listing.business_name),
                            scanned=stats.scanned,
                        )
                elif listing.phone_normalized in known_phones:
                    async with state_lock:
                        stats.duplicate_known += 1
                        stats.skipped.append(
                            {
                                "business_name": listing.business_name,
                                "maps_url": listing.maps_url or link,
                                "reason": "already_known",
                            }
                        )
                elif listing.phone_normalized in seen_phones:
                    async with state_lock:
                        stats.skipped.append(
                            {
                                "business_name": listing.business_name,
                                "maps_url": listing.maps_url or link,
                                "reason": "duplicate_phone",
                            }
                        )
                elif only_no_website and not is_target_lead(listing.website_status):
                    async with state_lock:
                        stats.skipped.append(
                            {
                                "business_name": listing.business_name,
                                "maps_url": listing.maps_url or link,
                                "reason": "has_website",
                            }
                        )
                else:
                    place_key = extract_place_key(listing.maps_url or link) or link_place_key
                    lead_data = {
                        "phone": listing.phone_normalized,
                        "phone_display": listing.phone_display,
                        "business_name": listing.business_name,
                        "category": listing.category,
                        "address": listing.address,
                        "city": city,
                        "governorate": governorate,
                        "maps_url": listing.maps_url or link,
                        "website": listing.website,
                        "website_status": listing.website_status,
                        "place_key": place_key,
                        "maps_website": listing.website,
                    }

                    async with state_lock:
                        seen_phones.add(listing.phone_normalized)
                        known_phones.add(listing.phone_normalized)
                        if place_key:
                            known_place_keys.add(place_key)
                        stats.with_phone += 1
                        if is_target_lead(listing.website_status):
                            stats.no_website += 1
                        stats.saved_leads.append(lead_data)

                    logger.info(
                        "maps.lead.found",
                        name=_safe_log_text(listing.business_name),
                        phone=_safe_log_text(listing.phone_display),
                    )

                    if on_lead:
                        on_lead(lead_data)

            except Exception as exc:
                logger.exception("maps.listing.error", link=link, error=str(exc))
                async with state_lock:
                    stats.skipped.append(
                        {"business_name": None, "maps_url": link, "reason": f"error:{exc}"}
                    )
            finally:
                should_report = progress_counter % (
                    settings.turbo_progress_every_n if is_turbo() else settings.progress_every_n
                ) == 0
                if on_progress and should_report:
                    async with state_lock:
                        await on_progress(
                            {
                                "scanned": stats.scanned,
                                "with_phone": stats.with_phone,
                                "no_phone": stats.no_phone,
                                "no_website": stats.no_website,
                                "current_name": listing_name,
                                "total": total_discovered,
                            }
                        )

        async def worker() -> None:
            listing_page: Page | None = None
            try:
                listing_page = await context.new_page()
            except Exception:
                ctx = await self._recover_context()
                listing_page = await ctx.new_page()
            try:
                while True:
                    link = await link_queue.get()
                    try:
                        if link is None:
                            break
                        if should_stop and await should_stop():
                            break
                        if (
                            not unlimited
                            and max_results
                            and stats.scanned >= settings.maps_daily_cap
                        ):
                            break
                        try:
                            await process_listing(listing_page, link)
                        except TargetClosedError:
                            logger.warning("maps.worker.recovering")
                            ctx = await self._recover_context()
                            listing_page = await ctx.new_page()
                            await process_listing(listing_page, link)
                        if not is_turbo():
                            await human_delay(200, 500)
                    finally:
                        link_queue.task_done()
            finally:
                try:
                    if listing_page:
                        await listing_page.close()
                except Exception:
                    pass

        try:
            workers = [asyncio.create_task(worker()) for _ in range(workers_n)]
            await producer()
            await asyncio.gather(*workers)

            if on_progress:
                async with state_lock:
                    await on_progress(
                        {
                            "scanned": stats.scanned,
                            "with_phone": stats.with_phone,
                            "no_phone": stats.no_phone,
                            "no_website": stats.no_website,
                            "current_name": "",
                            "total": total_discovered,
                        }
                    )

            logger.info(
                "maps.campaign.done",
                scanned=stats.scanned,
                saved=stats.with_phone,
                discovered=total_discovered,
            )
        except Exception as exc:
            logger.exception("maps.campaign.error", error=str(exc))

        return stats
