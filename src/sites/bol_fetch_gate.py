from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

_lock = asyncio.Lock()
_last_fetch_at: float = 0.0


def _min_gap_sec() -> float:
    try:
        return max(0.4, float(os.environ.get("BOL_FETCH_MIN_GAP_SEC", "0.75")))
    except ValueError:
        return 0.75


@asynccontextmanager
async def bol_fetch_gate() -> AsyncIterator[None]:
    """Serialize bol.com PDP fetches across all monitor tasks."""
    global _last_fetch_at
    async with _lock:
        gap = _min_gap_sec()
        wait = gap - (time.monotonic() - _last_fetch_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_fetch_at = time.monotonic()
        yield
