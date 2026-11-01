"""Scrape speed profiles — Overnight Safe vs Turbo."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from app.config import settings

# Active profile for the current batch/campaign task (None = use global turbo/settings)
_active_profile: ContextVar[str | None] = ContextVar("scrape_profile", default=None)


@dataclass(frozen=True)
class ScrapeProfile:
    id: str
    label: str
    maps_workers: int
    listing_pause_min_ms: int
    listing_pause_max_ms: int
    batch_rest_min_ms: int
    batch_rest_max_ms: int
    skip_social: bool
    delay_mult: float
    description: str


SAFE = ScrapeProfile(
    id="safe",
    label="Overnight Steady",
    maps_workers=3,
    listing_pause_min_ms=900,
    listing_pause_max_ms=2200,
    batch_rest_min_ms=6000,
    batch_rest_max_ms=15000,
    skip_social=True,
    delay_mult=0.75,
    description="3 workers, balanced pauses, social off — best mix of speed and reliability overnight",
)

TURBO = ScrapeProfile(
    id="turbo",
    label="Turbo",
    maps_workers=0,  # filled from settings at runtime
    listing_pause_min_ms=0,
    listing_pause_max_ms=0,
    batch_rest_min_ms=0,
    batch_rest_max_ms=0,
    skip_social=True,
    delay_mult=0.45,
    description="6 workers, short pauses — faster but more captcha risk",
)


def set_active_profile(profile_id: str | None) -> None:
    _active_profile.set(profile_id)


def get_active_profile_id() -> str | None:
    return _active_profile.get()


def resolve_profile(profile_id: str | None = None) -> ScrapeProfile:
    pid = profile_id if profile_id is not None else _active_profile.get()
    if pid == "safe":
        return SAFE
    if pid == "turbo" or (pid is None and settings.turbo_mode):
        return ScrapeProfile(
            id="turbo",
            label="Turbo",
            maps_workers=settings.turbo_maps_workers,
            listing_pause_min_ms=settings.turbo_listing_pause_min_ms,
            listing_pause_max_ms=settings.turbo_listing_pause_max_ms,
            batch_rest_min_ms=settings.turbo_batch_rest_min_ms,
            batch_rest_max_ms=settings.turbo_batch_rest_max_ms,
            skip_social=settings.turbo_skip_social,
            delay_mult=0.45,
            description=TURBO.description,
        )
    # Standard (non-turbo global)
    return ScrapeProfile(
        id="standard",
        label="Standard",
        maps_workers=settings.maps_workers,
        listing_pause_min_ms=settings.listing_pause_min_ms,
        listing_pause_max_ms=settings.listing_pause_max_ms,
        batch_rest_min_ms=settings.batch_rest_min_ms,
        batch_rest_max_ms=settings.batch_rest_max_ms,
        skip_social=settings.skip_social,
        delay_mult=1.4 if settings.proxy_enabled else 1.0,
        description="Default single-campaign pacing",
    )


def is_safe_mode() -> bool:
    return get_active_profile_id() == "safe"
