from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

from src.models.product import ProductState, StockStatus
from src.models.task import PollingIntervals
from src.utils.logging import get_logger

log = get_logger("monitor")

OnStateChange = Callable[[ProductState, Optional[ProductState]], Awaitable[None]]
PollFn = Callable[[], Awaitable[ProductState]]


class AdaptiveMonitor:
    """
    Dynamically adjusts poll interval:
    - offline / unknown (page down, Akamai block): 3-7s
    - page live but no cart yet (ONLINE / pre-drop OOS): 1-2s
    - buyable (IN_STOCK): 1-2s
    """

    def __init__(
        self,
        task_id: str,
        poll_fn: PollFn,
        polling: PollingIntervals,
        on_state_change: Optional[OnStateChange] = None,
        extra_sleep_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.task_id = task_id
        self._poll_fn = poll_fn
        self._polling = polling
        self._on_state_change = on_state_change
        self._extra_sleep_fn = extra_sleep_fn
        self._previous: Optional[ProductState] = None
        self._running = False
        self._log = get_logger(task_id)

    def _interval_for(self, state: ProductState) -> float:
        # Only slow-poll when the listing is not reachable yet (offline / blocked).
        # A live PDP waiting for the cart button must poll at drop speed (1-2s).
        if state.status in (StockStatus.OFFLINE, StockStatus.UNKNOWN):
            if self._polling.offline_min_sec == self._polling.offline_max_sec:
                return self._polling.offline_min_sec
            return random.uniform(
                self._polling.offline_min_sec, self._polling.offline_max_sec
            )
        if self._polling.online_min_sec == self._polling.online_max_sec:
            return self._polling.online_min_sec
        return random.uniform(self._polling.online_min_sec, self._polling.online_max_sec)

    @staticmethod
    def _status_summary(state: ProductState) -> str:
        """Human-readable page/stock label for console logs."""
        labels = {
            StockStatus.OFFLINE: "OFFLINE — not live yet or page down",
            StockStatus.UNKNOWN: "UNKNOWN — could not read page",
            StockStatus.ONLINE: "ONLINE — page live, waiting for cart button",
            StockStatus.OUT_OF_STOCK: "OUT OF STOCK — page up, no add-to-cart",
            StockStatus.IN_STOCK: "IN STOCK — buyable now",
        }
        parts = [labels.get(state.status, state.status.value.upper())]
        parts.append("ATC=yes" if state.can_add_to_cart else "ATC=no")
        if state.http_status is not None:
            parts.append(f"HTTP {state.http_status}")
        if state.error:
            parts.append(state.error)
        return " | ".join(parts)

    def _log_poll_status(self, current: ProductState, interval: float) -> None:
        summary = self._status_summary(current)
        changed = (
            self._previous is None
            or current.status != self._previous.status
            or current.can_add_to_cart != self._previous.can_add_to_cart
        )
        if changed:
            if current.is_available:
                self._log.success(f"Status changed → {summary}")
            elif current.status == StockStatus.OFFLINE:
                self._log.warning(f"Status changed → {summary}")
            else:
                self._log.info(f"Status changed → {summary}")
        else:
            if (
                self._previous
                and current.error
                and current.error == self._previous.error
                and current.status == self._previous.status
            ):
                self._log.info(
                    f"{current.status.value.upper()} | ATC={'yes' if current.can_add_to_cart else 'no'} "
                    f"| next ~{interval:.0f}s"
                )
            else:
                self._log.info(f"{summary} | next check ~{interval:.0f}s")

    async def run(self) -> None:
        self._running = True
        self._log.info("Monitor started")
        while self._running:
            try:
                current = await self._poll_fn()
                interval = self._interval_for(current)
                self._log_poll_status(current, interval)
                if self._on_state_change and (
                    self._previous is None
                    or current.status != self._previous.status
                    or current.can_add_to_cart != self._previous.can_add_to_cart
                ):
                    await self._on_state_change(current, self._previous)
                if self._previous and current.transitioned_to_available(self._previous):
                    self._log.success(
                        f"STOCK DETECTED — buyable | {self._status_summary(current)}"
                    )
                elif self._previous is None and current.is_available:
                    self._log.success(
                        f"STOCK DETECTED (first poll) — buyable | "
                        f"{self._status_summary(current)}"
                    )
                elif self._previous and current.transitioned_online(self._previous):
                    self._log.info(
                        f"Page went LIVE (was offline/unknown) → {self._status_summary(current)}"
                    )
                self._previous = current
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(f"Poll error: {exc}")
                await asyncio.sleep(5)
                continue
            extra = self._extra_sleep_fn() if self._extra_sleep_fn else 0.0
            await asyncio.sleep(interval + extra)
        self._log.info("Monitor stopped")

    def stop(self) -> None:
        self._running = False
