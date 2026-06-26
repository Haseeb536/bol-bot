from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import aiohttp

from src.models.product import ProductState
from src.models.session import CartResult, CheckoutResult, SessionBundle
from src.models.task import ProfileConfig, TaskConfig


class SiteAdapter(ABC):
    """Site-specific monitor, ATC, and checkout hooks."""

    name: str = "base"

    @abstractmethod
    async def fetch_state(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
    ) -> ProductState:
        ...

    @abstractmethod
    async def add_to_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        profile: ProfileConfig,
        proxy_url: Optional[str],
    ) -> CartResult:
        ...

    @abstractmethod
    async def verify_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
    ) -> bool:
        ...

    async def discover_api_endpoints(self, product_url: str) -> List[str]:
        """Optional: return candidate stock/API URLs for monitoring."""
        return []

    async def checkout(
        self,
        browser_context: Any,
        task: TaskConfig,
        profile: ProfileConfig,
    ) -> CheckoutResult:
        """Default: not implemented — override per site."""
        return CheckoutResult(
            success=False,
            message=f"Checkout not implemented for {self.name}",
        )

    def build_session_bundle(self, cookies: Dict[str, str]) -> SessionBundle:
        return SessionBundle(cookies=cookies)
