"""In-memory activity feed for the UI — what the bot is doing right now."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_MAX = 300
_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=_MAX)
_seq = 0

# Map structlog event names → friendly UI messages
_FRIENDLY = {
    "maps.search.start": "Searching Google Maps",
    "maps.scroll.done": "Finished scrolling Maps results",
    "maps.lead.found": "Lead saved",
    "maps.skip.no_phone": "Skipped (no phone)",
    "maps.campaign.done": "Campaign scrape finished",
    "maps.captcha.detected": "Captcha detected — backing off",
    "batch.stop_all": "Stopped all overnight runs",
    "batch.profile": "Overnight batch started",
    "batch.job.start": "Starting job",
    "batch.stopped_by_user": "Batch stopped",
    "batch.orphan.recovered": "Recovered stuck batch after restart",
    "batch.orphan.detected": "Batch was interrupted — resuming",
    "batch.orphan.resumed": "Batch resumed automatically",
    "batch.auto_resume": "Resuming overnight run",
    "batch.startup.resumed": "Resumed interrupted runs on startup",
    "batch.preparing": "Preparing overnight batch",
    "browser.starting": "Starting browser for job",
    "browser.launch.attempt": "Launching browser",
    "browser.launch.ready": "Browser ready",
    "browser.launch.attempt_failed": "Browser launch attempt failed",
    "browser.prewarm.ready": "Browser ready",
    "browser.prewarm.failed": "Browser warm-up failed — will retry on first job",
    "maps.producer.start": "Opening Google Maps search",
    "maps.producer.timeout": "Maps search timed out — retrying",
    "batch.watchdog.stalled": "No progress detected — auto-restarting run",
    "batch.watchdog.restarted": "Run restarted automatically (resuming where it left off)",
    "batch.watchdog.error": "Watchdog check failed",
    "batch.resumed": "Resuming overnight run",
    "campaign.completed": "Campaign completed",
    "campaign.failed": "Campaign failed",
    "batch.job.failed": "Job failed",
    "export.batched": "Excel updated",
    "brain.registered": "Brain updated (dedup)",
    "browser.rotate.captcha": "Rotating after captcha",
    "browser.captcha.backoff": "Waiting after captcha",
    "browser.launch.proxy": "Browser started with proxy",
    "browser.launch.direct": "Browser started",
    "browser.context.crashed": "Browser crashed — restarting...",
    "browser.page.dead": "Page died — recovering...",
    "browser.launch.timeout": "Browser took too long — resetting profile",
    "maps.worker.recovering": "Worker recovering after browser crash",
    "social.agent.started": "Social lookup started",
    "lead_saver.error": "Error saving lead",
}


def _format_message(event: str, data: dict[str, Any]) -> str:
    base = _FRIENDLY.get(event, event.replace(".", " ").replace("_", " "))

    if event == "maps.skip.no_phone":
        return f"Checked {data.get('scanned', '?')} listings (no phone yet)"

    if event == "maps.lead.found":
        name = data.get("name") or "business"
        phone = data.get("phone") or ""
        return f"Lead saved: {name}" + (f" | {phone}" if phone else "")

    if event == "maps.search.start":
        # url often has niche/city encoded; keep short
        return "Searching Google Maps..."

    if event == "maps.scroll.done":
        n = data.get("discovered")
        return f"Finished scrolling - {n} listings found" if n is not None else base

    if event == "maps.campaign.done":
        return (
            f"Done - scanned {data.get('scanned', '?')}, "
            f"saved {data.get('saved', '?')} leads"
        )

    if event == "batch.stop_all":
        return f"Stopped all overnight runs ({data.get('cancelled', '?')} cancelled)"

    if event == "batch.job.start":
        return f"Working on: {data.get('label', '...')}"

    if event == "batch.profile":
        return f"Overnight started ({data.get('profile', 'safe')} | {data.get('workers', '?')} workers)"

    if event == "export.batched":
        return f"Excel updated ({data.get('leads', '?')} leads)"

    if event == "brain.registered":
        return f"Remembered {data.get('added', '?')} new numbers in brain"

    if event == "browser.captcha.backoff":
        return f"Captcha pause - waiting {data.get('seconds', '?')}s"

    if event in ("campaign.failed", "batch.job.failed", "lead_saver.error"):
        err = data.get("error") or data.get("label") or ""
        return f"{base}: {err}" if err else base

    if event == "batch.watchdog.stalled":
        mins = data.get("stalled_minutes", "?")
        return f"No progress for {mins} min — restarting the run automatically"

    if event == "batch.resumed":
        return f"Resuming overnight run ({data.get('already_done', '?')} jobs already done)"

    if event == "browser.prewarm.start":
        return "Warming up browser — first launch can take up to 1 minute"

    if event == "batch.preparing":
        return "Preparing overnight batch..."

    if event == "browser.starting":
        label = data.get("label", "")
        return f"Starting browser for: {label}" if label else "Starting browser..."

    if event == "browser.launch.attempt":
        channel = data.get("channel", "chrome")
        return f"Launching {channel}..."

    if event == "browser.launch.ready":
        return "Browser ready — opening Google Maps"

    if event == "maps.producer.start":
        attempt = data.get("attempt", 1)
        return "Opening Google Maps search" + (f" (retry {attempt})" if attempt > 1 else "")

    return base


def push(level: str, event: str, **kwargs: Any) -> None:
    global _seq
    # Drop noisy debug-ish keys from display payload
    clean = {
        k: v
        for k, v in kwargs.items()
        if k not in ("logger", "level", "timestamp", "event") and v is not None
    }
    with _lock:
        _seq += 1
        _events.append(
            {
                "id": _seq,
                "ts": time.time(),
                "level": level,
                "event": event,
                "message": _format_message(event, clean),
                "data": clean,
            }
        )


def recent(limit: int = 80, after_id: int = 0) -> list[dict[str, Any]]:
    with _lock:
        items = list(_events)
    if after_id:
        items = [e for e in items if e["id"] > after_id]
    return items[-limit:]


def clear() -> None:
    with _lock:
        _events.clear()


class ActivityProcessor:
    """structlog processor that mirrors events into the UI feed."""

    def __call__(self, logger, method_name: str, event_dict: dict) -> dict:
        event = str(event_dict.get("event", ""))
        if not event or event.startswith("_"):
            return event_dict
        level = method_name if method_name in ("info", "warning", "error", "debug") else "info"
        if method_name in ("exception", "critical"):
            level = "error"
        # Only surface useful levels in the UI
        if level == "debug":
            return event_dict
        payload = {k: v for k, v in event_dict.items() if k != "event"}
        try:
            push(level, event, **payload)
        except Exception:
            pass
        return event_dict
