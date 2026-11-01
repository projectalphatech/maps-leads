"""Smarter captcha handling: manual solve, adaptive backoff, temporary slowdown."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog
from playwright.async_api import Page

from app.config import settings

logger = structlog.get_logger(__name__)


@dataclass
class CaptchaState:
    hits: int = 0
    consecutive: int = 0
    last_hit_at: float = 0.0
    slow_until: float = 0.0
    pause_until: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _manual_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_state = CaptchaState()


def captcha_status() -> dict:
    now = time.time()
    return {
        "hits": _state.hits,
        "consecutive": _state.consecutive,
        "slow_mode": _state.slow_until > now,
        "slow_remaining_s": max(0, int(_state.slow_until - now)),
        "paused": _state.pause_until > now,
        "pause_remaining_s": max(0, int(_state.pause_until - now)),
        "manual_solve": settings.captcha_manual_solve and not settings.headless,
    }


def delay_multiplier() -> float:
    """Extra delay factor while recovering from captchas."""
    now = time.time()
    if _state.slow_until <= now:
        return 1.0
    remaining = _state.slow_until - now
    window = max(settings.captcha_slow_seconds, 1)
    ratio = remaining / window
    return 1.2 + (1.3 * ratio)


def recommended_workers(default: int) -> int:
    """Drop parallelism after repeated captchas so we don't dig a deeper hole."""
    if _state.consecutive >= 3:
        return max(1, default // 3)
    if _state.consecutive >= 2 or _state.slow_until > time.time():
        return max(2, default // 2)
    return default


async def wait_if_paused() -> None:
    remaining = _state.pause_until - time.time()
    if remaining > 0:
        logger.info("captcha.global_pause", seconds=int(remaining))
        await asyncio.sleep(remaining)


async def record_captcha() -> None:
    async with _state._lock:
        now = time.time()
        if now - _state.last_hit_at > settings.captcha_reset_window_seconds:
            _state.consecutive = 0

        _state.hits += 1
        _state.consecutive += 1
        _state.last_hit_at = now
        _state.slow_until = now + settings.captcha_slow_seconds

        base = settings.captcha_cooldown_seconds
        pause = min(
            base * (2 ** min(_state.consecutive - 1, 4)),
            settings.captcha_max_backoff_seconds,
        )
        _state.pause_until = now + pause
        logger.warning(
            "captcha.recorded",
            consecutive=_state.consecutive,
            pause_seconds=int(pause),
            slow_seconds=settings.captcha_slow_seconds,
        )


async def record_success() -> None:
    async with _state._lock:
        if _state.consecutive > 0 and time.time() - _state.last_hit_at > 60:
            _state.consecutive = max(0, _state.consecutive - 1)


async def wait_for_manual_solve(page: Page) -> bool:
    """
    Keep the captcha page open and wait for the user to solve it.
    Only one worker waits at a time; others respect the global pause.
    """
    if settings.headless or not settings.captcha_manual_solve:
        return False

    if _state._manual_lock.locked():
        await wait_if_paused()
        from app.scrapers.browser_factory import detect_captcha

        try:
            return not page.is_closed() and not await detect_captcha(page)
        except Exception:
            return False

    async with _state._manual_lock:
        timeout = settings.captcha_manual_timeout_seconds
        logger.warning(
            "captcha.manual_solve.wait",
            timeout_seconds=timeout,
            url=page.url,
            hint="Solve the captcha in the open Chrome window — scraping resumes automatically",
        )

        deadline = time.time() + timeout
        from app.scrapers.browser_factory import detect_captcha

        while time.time() < deadline:
            try:
                if page.is_closed():
                    return False
                if not await detect_captcha(page):
                    logger.info("captcha.manual_solve.cleared")
                    async with _state._lock:
                        _state.consecutive = 0
                        _state.pause_until = time.time() + 15
                        _state.slow_until = time.time() + settings.captcha_slow_seconds
                    return True
            except Exception:
                return False
            await asyncio.sleep(2)

        logger.warning("captcha.manual_solve.timeout")
        return False


async def adaptive_backoff_seconds() -> float:
    consecutive = max(1, _state.consecutive)
    base = settings.captcha_cooldown_seconds
    return min(base * (2 ** min(consecutive - 1, 4)), settings.captcha_max_backoff_seconds)
