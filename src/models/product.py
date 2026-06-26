from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class StockStatus(str, Enum):
    UNKNOWN = "unknown"
    OFFLINE = "offline"
    ONLINE = "online"
    OUT_OF_STOCK = "out_of_stock"
    IN_STOCK = "in_stock"


@dataclass
class ProductState:
    url: str
    status: StockStatus = StockStatus.UNKNOWN
    can_add_to_cart: bool = False
    http_status: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None

    @property
    def is_live(self) -> bool:
        return self.status in (StockStatus.ONLINE, StockStatus.IN_STOCK, StockStatus.OUT_OF_STOCK)

    @property
    def is_available(self) -> bool:
        return self.can_add_to_cart and self.status == StockStatus.IN_STOCK

    def transitioned_to_available(self, previous: Optional["ProductState"]) -> bool:
        if previous is None:
            return self.is_available
        return not previous.is_available and self.is_available

    def transitioned_online(self, previous: Optional["ProductState"]) -> bool:
        if previous is None:
            return self.is_live
        return not previous.is_live and self.is_live
