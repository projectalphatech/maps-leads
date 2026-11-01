"""Browser factory: proxies, stealth, captcha detection, context rotation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.config import settings
from app.proxy.pool import ProxyEntry, proxy_pool

if TYPE_CHECKING:
    from app.proxy.pool import ProxyPool

logger = structlog.get_logger(__name__)


def cleanup_profile_locks(profile_dir: Path) -> None:
    """Remove stale Chromium lock files that prevent browser launch."""
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile_dir / lock_name
        if lock.exists():
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    parent_lock = profile_dir / "lockfile"
    if parent_lock.exists():
        try:
            parent_lock.unlink(missing_ok=True)
        except OSError:
            pass


def kill_orphaned_chrome() -> None:
    """Best-effort kill of leftover Playwright Chrome/Chromium processes on Windows."""
    for image in ("chrome.exe", "chromium.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", image],
                capture_output=True,
                timeout=8,
            )
        except Exception:
            pass

CONSENT_SELECTORS = (
    'button[aria-label*="Accept all"]',
    'button[aria-label*="قبول الكل"]',
    'button:has-text("Accept all")',
    'button:has-text("Reject all")',
    'button:has-text("I agree")',
    'button:has-text("رفض الكل")',
    "#L2AGLb",
    'form[action*="consent"] button',
)

CAPTCHA_URL_FRAGMENTS = ("/sorry/", "google.com/sorry", "consent.google")
CAPTCHA_TEXT_MARKERS = (
    "unusual traffic",
    "not a robot",
    "recaptcha",
    "captcha",
    "verify you are",
    "automated queries",
    "حصلنا على طلبات",
    "غير عادية",
    "تحقق",
)


class CaptchaDetectedError(Exception):
    pass


STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ar-EG', 'ar', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


async def configure_context(context: BrowserContext, navigation_timeout_ms: int = 90000) -> None:
    context.set_default_navigation_timeout(navigation_timeout_ms)
    context.set_default_timeout(45000)
    await context.add_init_script(STEALTH_SCRIPT)


async def dismiss_google_consent(page: Page) -> None:
    for selector in CONSENT_SELECTORS:
        btn = page.locator(selector).first
        try:
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(1500)
                logger.info("browser.consent.dismissed")
                return
        except Exception:
            continue


async def detect_captcha(page: Page) -> bool:
    url = page.url.lower()
    if any(frag in url for frag in CAPTCHA_URL_FRAGMENTS):
        return True
    try:
        body = (await page.locator("body").text_content(timeout=3000)) or ""
        body_lower = body.lower()
        if any(marker in body_lower for marker in CAPTCHA_TEXT_MARKERS):
            return True
        if await page.locator('iframe[src*="recaptcha"], div#g-recaptcha, #captcha').count() > 0:
            return True
    except Exception:
        pass
    return False


async def safe_goto(page: Page, url: str, *, retries: int = 3) -> None:
    from playwright._impl._errors import TargetClosedError

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, wait_until="commit", timeout=90000)
            await dismiss_google_consent(page)
            await page.wait_for_timeout(1500)

            if await detect_captcha(page):
                raise CaptchaDetectedError(f"Captcha detected at {page.url}")

            return
        except (CaptchaDetectedError, TargetClosedError):
            raise
        except PlaywrightTimeout as exc:
            last_error = exc
            logger.warning("browser.goto.timeout", url=url, attempt=attempt)
            if attempt < retries:
                await asyncio.sleep(5 * attempt)
        except Exception as exc:
            last_error = exc
            logger.warning("browser.goto.error", url=url, attempt=attempt, error=str(exc))
            if attempt < retries:
                await asyncio.sleep(3 * attempt)

    if last_error:
        raise last_error


class BrowserFactory:
    """Manages a persistent Playwright context with optional proxy rotation."""

    def __init__(self, profile_name: str, pool: ProxyPool | None = None) -> None:
        self._profile_name = profile_name
        self._pool = pool or proxy_pool
        self._playwright = None
        self._context: BrowserContext | None = None
        self._current_proxy: ProxyEntry | None = None
        self._launch_lock = asyncio.Lock()

    @property
    def current_proxy(self) -> ProxyEntry | None:
        return self._current_proxy

    async def get_context(self, *, force_rotate: bool = False) -> BrowserContext:
        async with self._launch_lock:
            if self._context and not force_rotate:
                return self._context
            if self._context:
                await self.close()
            return await self._launch(force_rotate=force_rotate)

    async def _try_launch_context(self, launch_kwargs: dict) -> BrowserContext:
        """Try real Chrome first (most reliable on Windows), then bundled Chromium."""
        attempts: list[dict] = [
            {**launch_kwargs, "channel": "chrome"},
            dict(launch_kwargs),
            {**launch_kwargs, "channel": "msedge"},
        ]
        errors: list[str] = []
        for idx, kwargs in enumerate(attempts, start=1):
            label = kwargs.get("channel") or "playwright-chromium"
            try:
                logger.info("browser.launch.attempt", profile=self._profile_name, channel=label)
                context = await asyncio.wait_for(
                    self._playwright.chromium.launch_persistent_context(**kwargs),
                    timeout=60,
                )
                logger.info("browser.launch.ready", profile=self._profile_name, channel=label)
                return context
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                logger.warning(
                    "browser.launch.attempt_failed",
                    profile=self._profile_name,
                    attempt=idx,
                    channel=label,
                    error=str(exc),
                )
        raise RuntimeError(
            "Could not launch browser. Install Google Chrome, then restart the app.\n"
            + "\n".join(errors[:2])
        )

    async def _launch(self, *, force_rotate: bool = False) -> BrowserContext:
        profile_dir = settings.profiles_dir / self._profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        cleanup_profile_locks(profile_dir)

        if not self._playwright:
            self._playwright = await async_playwright().start()

        self._current_proxy = await self._pool.get_proxy(force_rotate=force_rotate)

        launch_kwargs: dict = {
            "user_data_dir": str(profile_dir),
            "headless": settings.headless,
            "locale": settings.maps_locale,
            "viewport": {"width": 1366, "height": 900},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        if self._current_proxy:
            launch_kwargs["proxy"] = self._current_proxy.to_playwright()
            logger.info("browser.launch.proxy", server=self._current_proxy.server, profile=self._profile_name)
        else:
            logger.info("browser.launch.direct", profile=self._profile_name)

        try:
            self._context = await self._try_launch_context(launch_kwargs)
        except (asyncio.TimeoutError, RuntimeError):
            logger.error("browser.launch.timeout", profile=self._profile_name)
            await self._stop_playwright()
            shutil.rmtree(profile_dir, ignore_errors=True)
            profile_dir.mkdir(parents=True, exist_ok=True)
            cleanup_profile_locks(profile_dir)
            self._playwright = await async_playwright().start()
            self._context = await self._try_launch_context(launch_kwargs)

        await configure_context(self._context)
        return self._context

    async def _stop_playwright(self) -> None:
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    async def rotate_on_captcha(self, page: Page | None = None) -> BrowserContext:
        """Recover from captcha automatically: backoff + rotate proxy/context."""
        from app.scrapers.captcha_guard import adaptive_backoff_seconds, record_captcha

        logger.warning("browser.rotate.captcha", profile=self._profile_name)
        await record_captcha()
        await self._pool.mark_captcha(self._current_proxy)
        wait_s = await adaptive_backoff_seconds()
        logger.info("browser.captcha.backoff", seconds=int(wait_s))
        await asyncio.sleep(wait_s)
        return await self.get_context(force_rotate=True)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        self._current_proxy = None

    async def shutdown(self) -> None:
        await self.close()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
