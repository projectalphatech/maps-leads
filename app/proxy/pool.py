"""Proxy pool with rotation and cooldown after captcha blocks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


@dataclass
class ProxyEntry:
    server: str
    username: str | None = None
    password: str | None = None
    failures: int = 0
    cooldown_until: float = 0.0

    def to_playwright(self) -> dict:
        out: dict = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    def key(self) -> str:
        return self.server


def parse_proxy_line(line: str) -> ProxyEntry | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # host:port:username:password
    if "://" not in line:
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return ProxyEntry(server=f"http://{host}:{port}", username=user, password=pwd)
        if len(parts) == 2:
            host, port = parts
            return ProxyEntry(server=f"http://{host}:{port}")

    parsed = urlparse(line)
    if not parsed.scheme or not parsed.hostname:
        return None

    port = parsed.port or (1080 if "socks" in parsed.scheme else 8080)
    server = f"{parsed.scheme}://{parsed.hostname}:{port}"
    username = parsed.username
    password = parsed.password
    return ProxyEntry(server=server, username=username, password=password)


def load_proxies() -> list[ProxyEntry]:
    entries: list[ProxyEntry] = []

    if settings.proxies.strip():
        for chunk in settings.proxies.replace("\n", ",").split(","):
            entry = parse_proxy_line(chunk)
            if entry:
                entries.append(entry)

    proxy_file = settings.proxy_file
    if not proxy_file.is_absolute():
        proxy_file = settings.project_root / proxy_file
    if proxy_file.exists():
        with proxy_file.open(encoding="utf-8") as handle:
            for line in handle:
                entry = parse_proxy_line(line)
                if entry:
                    entries.append(entry)

    # Deduplicate by server
    seen: set[str] = set()
    unique: list[ProxyEntry] = []
    for entry in entries:
        if entry.key() not in seen:
            seen.add(entry.key())
            unique.append(entry)

    return unique


class ProxyPool:
    def __init__(self) -> None:
        self._proxies: list[ProxyEntry] = load_proxies()
        self._index = 0
        self._lock = asyncio.Lock()
        self._request_count = 0
        self._global_backoff_until = 0.0

    @property
    def enabled(self) -> bool:
        return settings.proxy_enabled and bool(self._proxies)

    @property
    def count(self) -> int:
        return len(self._proxies)

    async def wait_if_backoff(self) -> None:
        remaining = self._global_backoff_until - time.time()
        if remaining > 0:
            logger.warning("proxy.global_backoff", seconds=int(remaining))
            await asyncio.sleep(remaining)

    async def get_proxy(self, *, force_rotate: bool = False) -> ProxyEntry | None:
        if not self.enabled:
            return None

        await self.wait_if_backoff()

        async with self._lock:
            if force_rotate or self._request_count >= settings.proxy_rotate_every:
                self._index = (self._index + 1) % len(self._proxies)
                self._request_count = 0

            now = time.time()
            attempts = 0
            while attempts < len(self._proxies):
                entry = self._proxies[self._index]
                if entry.cooldown_until <= now:
                    self._request_count += 1
                    logger.debug("proxy.selected", server=entry.server, index=self._index)
                    return entry
                self._index = (self._index + 1) % len(self._proxies)
                attempts += 1

            # All on cooldown — pick least bad
            entry = min(self._proxies, key=lambda p: p.cooldown_until)
            logger.warning("proxy.all_cooldown", server=entry.server)
            return entry

    async def mark_captcha(self, entry: ProxyEntry | None) -> None:
        if not entry:
            self._global_backoff_until = time.time() + settings.captcha_cooldown_seconds
            return

        async with self._lock:
            entry.failures += 1
            cooldown = settings.captcha_cooldown_seconds * min(entry.failures, 5)
            entry.cooldown_until = time.time() + cooldown
            self._index = (self._index + 1) % len(self._proxies)
            self._request_count = 0
            logger.warning(
                "proxy.captcha_cooldown",
                server=entry.server,
                cooldown_seconds=cooldown,
                failures=entry.failures,
            )

    def status(self) -> dict:
        now = time.time()
        return {
            "enabled": self.enabled,
            "total": len(self._proxies),
            "active": sum(1 for p in self._proxies if p.cooldown_until <= now),
            "on_cooldown": sum(1 for p in self._proxies if p.cooldown_until > now),
        }


proxy_pool = ProxyPool()
