"""Append successful iDEAL payment URLs (same format as standalone monitor bot)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAYMENT_URLS_FILE = Path(
    os.environ.get("BOL_PAYMENT_URLS_FILE", str(ROOT / "payment_urls.txt"))
)
_LOCK = threading.Lock()


def append_payment_url(
    *,
    pay_url: str,
    product_url: str,
    product_id: str,
    offer_uid: str | None = None,
    seller: str = "bol",
) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    uid = (offer_uid or os.environ.get("BOL_OFFER_UID", "").strip() or "unknown")
    seller_label = seller or "bol"
    line = (
        f"{timestamp}\tproductId={product_id}\tofferUid={uid}\t"
        f"seller={seller_label}\tproductUrl={product_url}\tpayUrl={pay_url}\n"
    )
    try:
        with _LOCK:
            with open(PAYMENT_URLS_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass
