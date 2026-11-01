import re
from dataclasses import dataclass, field

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.scrapers.maps.browser import dismiss_google_consent, safe_goto
from app.scrapers.maps.helpers import classify_website, _unwrap_google_redirect
from app.utils.phone import normalize_egypt_phone


@dataclass
class MapsListing:
    business_name: str
    phone_raw: str | None = None
    phone_normalized: str | None = None
    phone_display: str | None = None
    category: str | None = None
    address: str | None = None
    maps_url: str | None = None
    website: str | None = None
    website_status: str = "missing"
    has_valid_phone: bool = False


PHONE_PATTERNS = [
    re.compile(r"\+20[\s\d\-]{8,14}"),
    re.compile(r"01[0125]\d[\s\d\-]{7,10}"),
    re.compile(r"0[2-9]\d[\s\d\-]{7,10}"),
]


def _extract_phone_from_text(text: str) -> str | None:
    for pattern in PHONE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


async def _text_content(page: Page, selector: str) -> str | None:
    locator = page.locator(selector).first
    if await locator.count() == 0:
        return None
    text = await locator.text_content()
    return text.strip() if text else None


async def _attr(page: Page, selector: str, name: str) -> str | None:
    locator = page.locator(selector).first
    if await locator.count() == 0:
        return None
    value = await locator.get_attribute(name)
    return value.strip() if value else None


async def parse_listing_panel(page: Page, *, fast: bool = False) -> MapsListing:
    await page.wait_for_timeout(250 if fast else 800)

    name = (
        await _text_content(page, "h1.DUwDvf")
        or await _text_content(page, "h1")
        or "Unknown"
    )

    category = await _text_content(page, "button.DkEaL")
    address = await _text_content(page, 'button[data-item-id="address"]')
    maps_url = page.url if "/maps/place" in page.url else None

    phone_raw: str | None = None

    phone_button = page.locator('button[data-item-id^="phone:tel:"]').first
    if await phone_button.count() > 0:
        data_id = await phone_button.get_attribute("data-item-id")
        if data_id and data_id.startswith("phone:tel:"):
            phone_raw = data_id.replace("phone:tel:", "")
        aria = await phone_button.get_attribute("aria-label")
        if aria and not phone_raw:
            phone_raw = aria.replace("Phone:", "").replace("هاتف:", "").strip()

    if not phone_raw:
        call_link = page.locator('a[href^="tel:"]').first
        if await call_link.count() > 0:
            href = await call_link.get_attribute("href")
            if href:
                phone_raw = href.replace("tel:", "")

    if not phone_raw:
        panel_text = await page.locator('div[role="main"]').first.text_content()
        if panel_text:
            phone_raw = _extract_phone_from_text(panel_text)

    website: str | None = None
    website_link = page.locator('a[data-item-id="authority"]').first
    if await website_link.count() > 0:
        website = await website_link.get_attribute("href")
    if not website:
        website_link = page.locator('a[aria-label*="Website"]').first
        if await website_link.count() > 0:
            website = await website_link.get_attribute("href")
    if not website:
        for link in await page.locator('a[href^="http"]').all():
            href = await link.get_attribute("href")
            if href and any(d in href for d in ("facebook.com", "instagram.com")):
                website = href
                break

    if website:
        website = _unwrap_google_redirect(website)

    phone_result = normalize_egypt_phone(phone_raw)
    website_status = classify_website(website)

    return MapsListing(
        business_name=name,
        phone_raw=phone_raw,
        phone_normalized=phone_result.normalized,
        phone_display=phone_result.display,
        category=category,
        address=address,
        maps_url=maps_url,
        website=website,
        website_status=website_status,
        has_valid_phone=phone_result.is_valid,
    )


async def wait_for_results_feed(page: Page, timeout_ms: int = 90000) -> None:
    await dismiss_google_consent(page)
    selectors = (
        'div[role="feed"]',
        'a[href*="/maps/place/"]',
        'div[role="main"]',
    )
    per_selector = timeout_ms // len(selectors)
    last_error: Exception | None = None
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=per_selector)
            return
        except PlaywrightTimeout as exc:
            last_error = exc
    if last_error:
        raise last_error


async def stream_result_links(page: Page, limit: int | None = None):
    """Yield Maps place URLs as they appear while scrolling — no wait for full list."""
    feed = page.locator('div[role="feed"]')
    seen: set[str] = set()
    stale_rounds = 0
    max_stale = 8 if (limit is None or limit <= 0) else 5

    # Initial pass
    anchors = feed.locator('a[href*="/maps/place/"]')
    for i in range(await anchors.count()):
        href = await anchors.nth(i).get_attribute("href")
        if href and href not in seen:
            seen.add(href)
            yield href
            if limit and limit > 0 and len(seen) >= limit:
                return

    while stale_rounds < max_stale:
        if limit and limit > 0 and len(seen) >= limit:
            break

        end_marker = page.locator(
            'span:has-text("end of the list"), span:has-text("نهاية القائمة")'
        )
        if await end_marker.count() > 0:
            break

        prev_len = len(seen)
        await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        from app.utils.delays import is_turbo

        await page.wait_for_timeout(1200 if is_turbo() else 2500)

        anchors = feed.locator('a[href*="/maps/place/"]')
        for i in range(await anchors.count()):
            href = await anchors.nth(i).get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                yield href
                if limit and limit > 0 and len(seen) >= limit:
                    return

        if len(seen) == prev_len:
            stale_rounds += 1
        else:
            stale_rounds = 0


async def collect_result_links(page: Page, limit: int | None = None) -> list[str]:
    links: list[str] = []
    async for link in stream_result_links(page, limit):
        links.append(link)
    return links


async def open_listing_by_url(page: Page, url: str) -> None:
    await safe_goto(page, url)
    try:
        await page.wait_for_selector("h1", timeout=20000)
    except PlaywrightTimeout:
        pass
