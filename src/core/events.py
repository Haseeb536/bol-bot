from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from src.models.product import ProductState


class BotEventType(str, Enum):
    STATE_CHANGE = "state_change"
    STOCK_FOUND = "stock_found"
    ATC_SUCCESS = "atc_success"
    ATC_FAILED = "atc_failed"
    CHECKOUT_SUCCESS = "checkout_success"
    CHECKOUT_FAILED = "checkout_failed"
    PROXY_SWITCH = "proxy_switch"
    ERROR = "error"


@dataclass
class BotEvent:
    type: BotEventType
    task_id: str
    payload: Dict[str, Any]
    state: Optional[ProductState] = None


Handler = Callable[[BotEvent], Any]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[BotEventType, List[Handler]] = {}

    def on(self, event_type: BotEventType, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event: BotEvent) -> None:
        for handler in self._handlers.get(event.type, []):
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
