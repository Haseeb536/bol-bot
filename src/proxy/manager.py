from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import aiohttp

from src.models.task import ProxyGroupConfig
from src.utils.logging import get_logger

log = get_logger("proxy")


class ProxyState(str, Enum):
    HEALTHY = "healthy"
    RATE_LIMITED = "rate_limited"
    BANNED = "banned"
    TIMEOUT = "timeout"
    CLOUDFLARE = "cloudflare"
    DEAD = "dead"


@dataclass
class ProxyHealth:
    url: str
    state: ProxyState = ProxyState.HEALTHY
    failures: int = 0
    last_used: float = 0.0
    last_check: float = 0.0
    latency_ms: Optional[float] = None


@dataclass
class _ProxyEntry:
    health: ProxyHealth
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProxyManager:
    """Rotating proxy pool with health tracking and ban classification."""

    def __init__(self, groups: Dict[str, ProxyGroupConfig]) -> None:
        self._groups = groups
        self._entries: Dict[str, List[_ProxyEntry]] = {}
        self._index: Dict[str, int] = {}
        for name, group in groups.items():
            self._entries[name] = [
                _ProxyEntry(health=ProxyHealth(url=p)) for p in group.proxies
            ]
            self._index[name] = 0

    def classify_error(self, status: Optional[int], exc: Optional[Exception] = None) -> ProxyState:
        if exc and isinstance(exc, asyncio.TimeoutError):
            return ProxyState.TIMEOUT
        if exc and isinstance(exc, aiohttp.ClientError):
            return ProxyState.DEAD
        if status == 403:
            return ProxyState.BANNED
        if status == 429:
            return ProxyState.RATE_LIMITED
        if status in (503, 520, 521, 522, 523, 524):
            return ProxyState.CLOUDFLARE
        return ProxyState.DEAD

    async def acquire(self, group_name: Optional[str]) -> Optional[str]:
        if not group_name or group_name not in self._entries:
            return None
        entries = self._entries[group_name]
        if not entries:
            return None
        n = len(entries)
        start = self._index[group_name]
        for offset in range(n):
            idx = (start + offset) % n
            entry = entries[idx]
            if entry.health.state in (ProxyState.HEALTHY, ProxyState.RATE_LIMITED):
                async with entry.lock:
                    entry.health.last_used = time.monotonic()
                    self._index[group_name] = (idx + 1) % n
                    return entry.health.url
        log.warning(f"No healthy proxies in group {group_name}")
        return None

    def report_success(self, proxy_url: str, group_name: str, latency_ms: float) -> None:
        entry = self._find(group_name, proxy_url)
        if entry:
            entry.health.state = ProxyState.HEALTHY
            entry.health.failures = 0
            entry.health.latency_ms = latency_ms

    def report_failure(
        self, proxy_url: str, group_name: str, state: ProxyState
    ) -> None:
        entry = self._find(group_name, proxy_url)
        if not entry:
            return
        entry.health.failures += 1
        entry.health.state = state
        max_fail = self._groups[group_name].max_failures
        if entry.health.failures >= max_fail:
            entry.health.state = ProxyState.DEAD
            log.error(f"Proxy marked dead: {proxy_url[:40]}... ({state.value})")

    def _find(self, group_name: str, proxy_url: str) -> Optional[_ProxyEntry]:
        for entry in self._entries.get(group_name, []):
            if entry.health.url == proxy_url:
                return entry
        return None

    async def health_check_all(self) -> None:
        async with aiohttp.ClientSession() as session:
            for name, group in self._groups.items():
                for entry in self._entries[name]:
                    await self._check_one(session, name, entry, group.health_check_url)

    async def _check_one(
        self,
        session: aiohttp.ClientSession,
        group_name: str,
        entry: _ProxyEntry,
        check_url: str,
    ) -> None:
        proxy = entry.health.url
        started = time.monotonic()
        try:
            async with session.get(
                check_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                latency = (time.monotonic() - started) * 1000
                if resp.status < 500:
                    self.report_success(proxy, group_name, latency)
                else:
                    self.report_failure(
                        proxy, group_name, self.classify_error(resp.status)
                    )
        except Exception as exc:
            self.report_failure(
                proxy, group_name, self.classify_error(None, exc)
            )
