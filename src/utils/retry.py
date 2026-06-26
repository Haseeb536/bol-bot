from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay_ms: int = 200
    max_delay_ms: int = 5000
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        exp = min(self.max_delay_ms, self.base_delay_ms * (2 ** attempt))
        jitter = exp * self.jitter * random.random()
        return (exp + jitter) / 1000.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    should_retry: Optional[Callable[[Exception], bool]] = None,
    on_retry: Optional[Callable[[int, Exception], Awaitable[None]]] = None,
) -> T:
    last_exc: Optional[Exception] = None
    for attempt in range(policy.max_attempts):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= policy.max_attempts - 1:
                break
            if should_retry and not should_retry(exc):
                break
            if on_retry:
                await on_retry(attempt + 1, exc)
            await asyncio.sleep(policy.delay_for(attempt))
    assert last_exc is not None
    raise last_exc
