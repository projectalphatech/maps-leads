"""Smarter Facebook/Instagram discovery via Google (no Meta login)."""

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import structlog
from playwright.async_api import BrowserContext
from rapidfuzz import fuzz

from app.config import settings
from app.scrapers.browser_factory import CaptchaDetectedError, dismiss_google_consent, safe_goto
from app.utils.delays import human_delay

logger = structlog.get_logger(__name__)

_FB_RE = re.compile(r"https?://(?:www\.|m\.)?facebook\.com/[a-zA-Z0-9._\-/?=&%]+", re.I)
_IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[a-zA-Z0-9._]+/?", re.I)
_IG_HANDLE_RE = re.compile(r"instagram\.com/([a-zA-Z0-9._]+)", re.I)
_IG_SKIP = {"p", "reel", "reels", "stories", "explore", "accounts", "about", "developer", "popular"}
_NOISE_WORDS = re.compile(
    r"\b(restaurant|cafe|shop|store|salon|clinic|pharmacy|center|centre|"
    r"مطعم|كافيه|صيدلية|عيادة|مركز|محل)\b",
    re.I,
)


@dataclass
class SocialJob:
    lead_id: int
    business_name: str
    city: str
    governorate: str
    maps_website: str | None = None
    phone: str | None = None


def _clean_google_href(href: str) -> str:
    if "google.com/url" in href:
        parsed = urlparse(href)
        q = parse_qs(parsed.query).get("q", [""])[0]
        return unquote(q)
    return href


def _name_variants(name: str) -> list[str]:
    base = name.strip()
    variants = [base]
    cleaned = _NOISE_WORDS.sub("", base).strip(" -|,")
    if cleaned and cleaned != base:
        variants.append(cleaned)
    short = " ".join(base.split()[:3])
    if short and short not in variants:
        variants.append(short)
    return variants


def _normalize_fb(url: str) -> str | None:
    url = url.rstrip("/").split("?")[0]
    lowered = url.lower()
    if "facebook.com" not in lowered:
        return None
    if any(x in lowered for x in ("/sharer", "/login", "/help", "/policy", "/watch", "/photo")):
        return None
    if lowered.endswith("facebook.com") or lowered.endswith("facebook.com/"):
        return None
    return url


def _normalize_ig(url: str) -> tuple[str | None, str | None]:
    url = url.rstrip("/").split("?")[0]
    match = _IG_HANDLE_RE.search(url)
    if not match:
        return None, None
    handle = match.group(1)
    if handle.lower() in _IG_SKIP:
        return None, None
    return f"https://www.instagram.com/{handle}/", f"@{handle}"


def _score_match(business_name: str, city: str, url: str, snippet: str = "") -> int:
    """0-100 confidence that this social page belongs to the business."""
    haystack = f"{url} {snippet}".lower()
    best = 0
    for variant in _name_variants(business_name):
        ratio = fuzz.partial_ratio(variant.lower(), haystack)
        best = max(best, ratio)
    score = int(best * 0.7)
    if city.lower() in haystack:
        score += 15
    if "egypt" in haystack or "مصر" in haystack:
        score += 10
    return min(score, 100)


async def _google_search(context: BrowserContext, query: str, captcha_rotate=None) -> list[tuple[str, str]]:
    """Return list of (url, visible_text) from search results."""
    page = await context.new_page()
    results: list[tuple[str, str]] = []
    try:
        encoded = quote_plus(query)
        await safe_goto(page, f"https://www.google.com/search?q={encoded}&num=10")
        await dismiss_google_consent(page)
        await human_delay(800, 1500)
    except CaptchaDetectedError:
        await page.close()
        if captcha_rotate:
            await captcha_rotate()
        raise
    except Exception as exc:
        logger.warning("social.google.error", query=query, error=str(exc))
        await page.close()
        return results

    try:

        # Result blocks
        blocks = page.locator("div.g, div[data-sokoban-container]")
        count = min(await blocks.count(), 12)
        for i in range(count):
            block = blocks.nth(i)
            text = (await block.text_content()) or ""
            anchors = block.locator("a[href]")
            for j in range(min(await anchors.count(), 3)):
                href = await anchors.nth(j).get_attribute("href")
                if href:
                    results.append((_clean_google_href(href), text))
    except Exception as exc:
        logger.warning("social.google.error", query=query, error=str(exc))
    finally:
        await page.close()
    return results


def _pick_best_fb(business_name: str, city: str, results: list[tuple[str, str]]) -> tuple[str | None, int]:
    best_url: str | None = None
    best_score = 0
    for url, snippet in results:
        match = _FB_RE.search(url) or _FB_RE.search(snippet)
        if not match:
            continue
        fb = _normalize_fb(match.group(0))
        if not fb:
            continue
        score = _score_match(business_name, city, fb, snippet)
        if score > best_score:
            best_score = score
            best_url = fb
    if best_score >= 45:
        return best_url, best_score
    return None, 0


def _pick_best_ig(business_name: str, city: str, results: list[tuple[str, str]]) -> tuple[str | None, str | None, int]:
    best_url: str | None = None
    best_handle: str | None = None
    best_score = 0
    for url, snippet in results:
        match = _IG_RE.search(url) or _IG_RE.search(snippet)
        if not match:
            continue
        ig_url, handle = _normalize_ig(match.group(0))
        if not ig_url:
            continue
        score = _score_match(business_name, city, ig_url, snippet)
        if score > best_score:
            best_score = score
            best_url = ig_url
            best_handle = handle
    if best_score >= 45:
        return best_url, best_handle, best_score
    return None, None, 0


async def lookup_social_links(
    context: BrowserContext,
    *,
    business_name: str,
    city: str,
    governorate: str,
    maps_website: str | None = None,
    phone: str | None = None,
    captcha_rotate=None,
) -> dict:
    result: dict = {
        "facebook_url": None,
        "instagram_handle": None,
        "instagram_url": None,
        "social_source": None,
        "social_confidence": 0,
    }

    # 1. Social links already on Maps listing
    if maps_website:
        if "facebook.com" in maps_website.lower():
            fb = _normalize_fb(maps_website)
            if fb:
                result["facebook_url"] = fb
                result["social_source"] = "maps"
                result["social_confidence"] = 95
        if "instagram.com" in maps_website.lower():
            ig_url, handle = _normalize_ig(maps_website)
            if ig_url:
                result["instagram_url"] = ig_url
                result["instagram_handle"] = handle
                result["social_source"] = "maps"
                result["social_confidence"] = 95

    location = f"{city} {governorate}"
    phone_digits = re.sub(r"\D", "", phone or "")[-8:]

    queries: list[str] = []
    for name in _name_variants(business_name)[:1 if settings.turbo_mode else 2]:
        queries.append(f'"{name}" {location} Egypt (site:facebook.com OR site:instagram.com)')
        if not settings.turbo_mode:
            queries.append(f'"{name}" {location} site:facebook.com')
            queries.append(f'"{name}" {location} site:instagram.com')
    if phone_digits and not settings.turbo_mode:
        queries.append(f'"{business_name}" {phone_digits} site:facebook.com')
        queries.append(f'"{business_name}" {phone_digits} site:instagram.com')

    seen_queries: set[str] = set()
    all_results: list[tuple[str, str]] = []

    for query in queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        batch = await _google_search(context, query, captcha_rotate)
        all_results.extend(batch)
        if result["facebook_url"] and result["instagram_handle"]:
            break
        # Early exit if we found both from accumulated results
        if not result["facebook_url"]:
            fb, fb_score = _pick_best_fb(business_name, city, all_results)
            if fb:
                result["facebook_url"] = fb
                result["social_confidence"] = fb_score
                result["social_source"] = "google"
        if not result["instagram_handle"]:
            ig_url, handle, ig_score = _pick_best_ig(business_name, city, all_results)
            if ig_url:
                result["instagram_url"] = ig_url
                result["instagram_handle"] = handle
                result["social_confidence"] = max(result["social_confidence"], ig_score)
                if not result["social_source"]:
                    result["social_source"] = "google"
        if result["facebook_url"] and result["instagram_handle"]:
            break
        await human_delay(1000, 2000)

    logger.info(
        "social.lookup.done",
        business=business_name,
        fb=bool(result["facebook_url"]),
        ig=bool(result["instagram_handle"]),
        confidence=result["social_confidence"],
    )
    return result
