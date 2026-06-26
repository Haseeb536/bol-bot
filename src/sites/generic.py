from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from src.models.product import ProductState, StockStatus
from src.models.session import CartResult
from src.models.task import ProfileConfig, TaskConfig
from src.monitors.detector import ProductDetector
from src.sites.base import SiteAdapter
from src.utils.logging import get_logger

log = get_logger("generic")


class GenericSiteAdapter(SiteAdapter):
    name = "generic"

    async def fetch_state(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
    ) -> ProductState:
        url = str(task.product_url)
        candidates = [url] + await self.discover_api_endpoints(url)
        last_state: Optional[ProductState] = None

        for candidate in candidates:
            try:
                async with session.get(candidate, proxy=proxy_url) as resp:
                    body = await resp.read()
                    state = ProductDetector.parse_response(
                        candidate,
                        resp.status,
                        body,
                        resp.headers.get("Content-Type", ""),
                    )
                    last_state = state
                    if state.is_live:
                        return state
            except Exception as exc:
                log.debug(f"Candidate failed {candidate}: {exc}")
        return last_state or ProductState(url=url, status=StockStatus.UNKNOWN)

    async def discover_api_endpoints(self, product_url: str) -> List[str]:
        parsed = urlparse(product_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        guesses = [
            f"{base}/api/product{path}",
            f"{base}/api/products{path}",
            f"{product_url}.json",
            f"{product_url}?format=json",
        ]
        m = re.search(r"/(\d{8,})", path)
        if m:
            pid = m.group(1)
            guesses.extend([
                f"{base}/api/offer/{pid}",
                f"{base}/api/stock/{pid}",
            ])
        return guesses

    async def add_to_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        profile: ProfileConfig,
        proxy_url: Optional[str],
    ) -> CartResult:
        url = str(task.product_url)
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        atc_urls = [
            f"{base}/api/cart/add",
            f"{base}/cart/add",
        ]
        payload = {
            "url": url,
            "quantity": task.quantity,
            **task.metadata.get("atc_payload", {}),
        }
        headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
        for atc in atc_urls:
            try:
                async with session.post(
                    atc, json=payload, headers=headers, proxy=proxy_url
                ) as resp:
                    text = await resp.text()
                    if resp.status in (200, 201, 204):
                        return CartResult(success=True, verified=False, message="ATC OK", raw={"status": resp.status})
                    if resp.status == 302:
                        return CartResult(success=True, message="ATC redirect", raw={"location": resp.headers.get("Location")})
            except Exception as exc:
                log.debug(f"ATC attempt {atc}: {exc}")
        return CartResult(success=False, message="No generic ATC endpoint succeeded")

    async def verify_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
    ) -> bool:
        parsed = urlparse(str(task.product_url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in ("/api/cart", "/cart", "/winkelwagen"):
            try:
                async with session.get(urljoin(base, path), proxy=proxy_url) as resp:
                    if resp.status == 200:
                        body = await resp.text()
                        return "item" in body.lower() or "product" in body.lower()
            except Exception:
                continue
        return False
