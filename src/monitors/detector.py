from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.models.product import ProductState, StockStatus
from src.utils.http import parse_json_safe


@dataclass
class DetectionContext:
    url: str
    http_status: int
    body: str
    content_type: str
    json_payload: Optional[Dict[str, Any]] = None


class ProductDetector:
    """Heuristic + JSON-based product state detection."""

    IN_STOCK_PATTERNS = [
        r'"availability"\s*:\s*"InStock"',
        r'>\s*Op voorraad\s*<',
        r'>\s*In stock\s*<',
        r'(?<!niet\s)op\s+voorraad',
    ]
    OOS_PATTERNS = [
        r'"availability"\s*:\s*"OutOfStock"',
        r"out\s*of\s*stock",
        r"niet\s+op\s+voorraad",
        r"temporarily\s+unavailable",
        r"sold\s*out",
    ]
    ATC_PATTERNS = [
        r"data-test=[\"']add-to-basket[\"']",
        r"data-test=[\"']add-to-cart[\"']",
        r'"addToCart"\s*:\s*true',
        r"aria-label=[\"'][^\"']*in\s*winkelwagen[^\"']*[\"'][^>]*(?<!disabled)",
    ]
    OFFLINE_PATTERNS = [
        r"page\s*not\s*found",
        r"product\s*offline",
        r"kunnen\s+deze\s+pagina\s+niet\s+meer\s+vinden",
        r"oeps[!,.]?\s+sorry,\s+we\s+kunnen\s+deze\s+pagina",
    ]
    # bol.com pre-release / notify-me (not buyable yet)
    COMING_SOON_PATTERNS = [
        r"nog\s+niet\s+verkrijgbaar",
        r"houd\s+mij\s+op\s+de\s+hoogte",
        r"beschikbaar\s+vanaf",
        r"verwacht\s+op",
        r"binnenkort\s+verkrijgbaar",
    ]

    @classmethod
    def from_http(cls, ctx: DetectionContext) -> ProductState:
        status = StockStatus.UNKNOWN
        can_atc = False

        if ctx.http_status in (404, 410):
            status = StockStatus.OFFLINE
        elif ctx.http_status >= 500:
            status = StockStatus.OFFLINE
        elif ctx.json_payload:
            status, can_atc = cls._from_json(ctx.json_payload)
        else:
            status, can_atc = cls._from_html(ctx.body)

        if status == StockStatus.UNKNOWN and ctx.http_status == 200:
            status = StockStatus.ONLINE

        return ProductState(
            url=ctx.url,
            status=status,
            can_add_to_cart=can_atc,
            http_status=ctx.http_status,
            raw={"content_type": ctx.content_type},
        )

    @classmethod
    def _from_json(cls, data: Dict[str, Any]) -> tuple[StockStatus, bool]:
        # Common inventory shapes
        paths = [
            ("available", True),
            ("inStock", True),
            ("in_stock", True),
            ("stock", lambda v: isinstance(v, dict) and v.get("available", 0) > 0),
            ("offer", lambda v: isinstance(v, dict) and v.get("available", True)),
            ("buyable", True),
        ]
        for key, check in paths:
            if key not in data:
                continue
            val = data[key]
            if callable(check):
                if check(val):
                    return StockStatus.IN_STOCK, True
            elif val is True or (isinstance(val, (int, float)) and val > 0):
                return StockStatus.IN_STOCK, True

        if any(k in data for k in ("soldOut", "sold_out", "outOfStock")):
            sold = data.get("soldOut") or data.get("sold_out") or data.get("outOfStock")
            if sold:
                return StockStatus.OUT_OF_STOCK, False

        return StockStatus.ONLINE, False

    @classmethod
    def _from_html(cls, body: str) -> tuple[StockStatus, bool]:
        low = body.lower()
        if re.search(r'"availability"\s*:\s*"InStock"', body, re.I):
            return StockStatus.IN_STOCK, True
        if re.search(r'"availability"\s*:\s*"OutOfStock"', body, re.I):
            return StockStatus.OUT_OF_STOCK, False
        if re.search(r'"buyable"\s*:\s*true', body, re.I):
            return StockStatus.IN_STOCK, True
        if re.search(r'"buyable"\s*:\s*false', body, re.I):
            return StockStatus.ONLINE, False
        for pat in cls.OOS_PATTERNS:
            if re.search(pat, low, re.I):
                return StockStatus.OUT_OF_STOCK, False
        for pat in cls.IN_STOCK_PATTERNS:
            if re.search(pat, body, re.I):
                return StockStatus.IN_STOCK, True
        for pat in cls.ATC_PATTERNS:
            if re.search(pat, body, re.I):
                return StockStatus.IN_STOCK, True
        if re.search(r"uitverkocht", low, re.I):
            return StockStatus.OUT_OF_STOCK, False
        for pat in cls.COMING_SOON_PATTERNS:
            if re.search(pat, low, re.I):
                return StockStatus.ONLINE, False
        for pat in cls.OFFLINE_PATTERNS:
            if re.search(pat, low, re.I):
                return StockStatus.OFFLINE, False
        if "disabled" in low and "cart" in low:
            return StockStatus.OUT_OF_STOCK, False
        return StockStatus.ONLINE, False

    @staticmethod
    def parse_response(
        url: str, status: int, body: bytes, content_type: str
    ) -> ProductState:
        text = body.decode("utf-8", errors="replace")
        payload = None
        if "json" in content_type.lower():
            payload = parse_json_safe(body)
        ctx = DetectionContext(
            url=url,
            http_status=status,
            body=text,
            content_type=content_type,
            json_payload=payload,
        )
        return ProductDetector.from_http(ctx)
