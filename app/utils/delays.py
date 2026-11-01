import asyncio
import random

from app.config import settings
from app.scrape_profile import resolve_profile
from app.scrapers.captcha_guard import delay_multiplier, wait_if_paused


def is_turbo() -> bool:
    profile = resolve_profile()
    return profile.id == "turbo"


async def human_delay(min_ms: int | None = None, max_ms: int | None = None) -> None:
    await wait_if_paused()
    profile = resolve_profile()
    mult = profile.delay_mult * delay_multiplier()
    low = int((min_ms if min_ms is not None else settings.scroll_pause_min_ms) * mult)
    high = int((max_ms if max_ms is not None else settings.scroll_pause_max_ms) * mult)
    if high < low:
        high = low
    # Clamp only in pure turbo without captcha slowdown
    if profile.id == "turbo" and delay_multiplier() <= 1.05 and high > 1200:
        high = 1200
    await asyncio.sleep(random.uniform(low, high) / 1000.0)


async def listing_delay() -> None:
    await wait_if_paused()
    profile = resolve_profile()
    caution = delay_multiplier()
    if profile.id in ("turbo", "safe") and caution <= 1.05:
        await human_delay(profile.listing_pause_min_ms, profile.listing_pause_max_ms)
        return
    # After captcha: use standard (slower) listing pauses
    mult = profile.delay_mult * caution
    low = int(settings.listing_pause_min_ms * mult)
    high = int(settings.listing_pause_max_ms * mult)
    await human_delay(low, high)


async def batch_rest_delay() -> None:
    profile = resolve_profile()
    await human_delay(profile.batch_rest_min_ms, profile.batch_rest_max_ms)
