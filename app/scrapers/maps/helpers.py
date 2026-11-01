import re
from urllib.parse import parse_qs, unquote, urlparse

from urllib.parse import quote_plus

SOCIAL_DOMAINS = (
    "facebook.com",
    "fb.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "wa.me",
    "whatsapp.com",
    "linktr.ee",
)


def build_maps_search_url(niche: str, city: str, governorate: str) -> str:
    query = f"{niche} in {city}, {governorate}, Egypt"
    return f"https://www.google.com/maps/search/{quote_plus(query)}"


def _unwrap_google_redirect(url: str) -> str:
    lowered = url.lower()
    if "google." in lowered and "/url" in lowered:
        parsed = urlparse(url)
        q = parse_qs(parsed.query).get("q", [""])[0]
        if q:
            return unquote(q)
    return url


def classify_website(url: str | None) -> str:
    if not url or not url.strip():
        return "missing"

    url = _unwrap_google_redirect(url.strip())
    lowered = url.lower()
    if lowered in {"#", "about:blank"}:
        return "missing"

    for domain in SOCIAL_DOMAINS:
        if domain in lowered:
            return "social_only"

    if "google." in lowered and ("/maps" in lowered or "/search" in lowered):
        return "invalid"

    if lowered.startswith("http://") or lowered.startswith("https://"):
        parsed = urlparse(lowered)
        host = parsed.netloc.replace("www.", "")
        if host and "." in host and host not in ("goo.gl", "bit.ly", "t.co"):
            return "has_website"

    return "invalid"


def is_target_lead(website_status: str) -> bool:
    return website_status in {"missing", "social_only", "invalid"}


_CID_RE = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
_PLACE_NAME_RE = re.compile(r"/maps/place/([^/@]+)")


def extract_place_key(url: str | None) -> str | None:
    """Stable identifier for a Maps listing so we can skip revisiting it."""
    if not url:
        return None

    cid = _CID_RE.search(url)
    if cid:
        return cid.group(0)

    name = _PLACE_NAME_RE.search(url)
    if name:
        return name.group(1).lower()

    return None
