from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp
import orjson

DEFAULT_HEADERS = {
    "Accept": "text/html,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def parse_json_safe(data: bytes | str) -> Optional[Dict[str, Any]]:
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return orjson.loads(data)
    except Exception:
        return None


def build_client_session(
    *,
    proxy_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = 20.0,
    cookie_jar: Optional[aiohttp.CookieJar] = None,
) -> aiohttp.ClientSession:
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    return aiohttp.ClientSession(
        headers=merged,
        timeout=timeout,
        cookie_jar=cookie_jar or aiohttp.CookieJar(unsafe=True),
        connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=300),
        trust_env=True,
        # proxy applied per-request for rotation
    )


async def request_with_proxy(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    proxy_url: Optional[str] = None,
    **kwargs: Any,
) -> aiohttp.ClientResponse:
    return await session.request(method, url, proxy=proxy_url, **kwargs)
