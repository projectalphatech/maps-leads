"""Startup checks — dependencies and Chrome availability."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def chrome_installed() -> bool:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return any(p.is_file() for p in candidates if str(p))


def playwright_browsers_installed() -> bool:
    cache = Path.home() / "AppData" / "Local" / "ms-playwright"
    return cache.exists()


def run_preflight() -> list[str]:
    warnings: list[str] = []

    if not chrome_installed() and not shutil.which("msedge"):
        warnings.append(
            "Google Chrome not found — install Chrome for reliable scraping "
            "(Playwright bundled Chromium is slower and hangs more often)."
        )

    if not playwright_browsers_installed():
        warnings.append("Playwright browsers missing — run: playwright install chromium")

    for warning in warnings:
        logger.warning("preflight.issue", message=warning)

    if not warnings:
        logger.info("preflight.ok")
    return warnings
