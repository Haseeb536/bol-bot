#!/usr/bin/env python3
"""Probe bol.com checkout/basket pages for GraphQL operation names."""
from __future__ import annotations

import re
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
sys.path.insert(0, __file__.rsplit("\\", 1)[0] + "/..")

from bol_login import ensure_session
from bol_cart import _init_session_holder, _page_get, _prime_www

URLS = (
    "https://www.bol.com/nl/nl/basket/",
    "https://www.bol.com/nl/nl/checkout/",
)

KEYWORDS = (
    "ideal",
    "checkout",
    "payment",
    "placeorder",
    "submit",
    "adyen",
    "redirecturl",
    "paymenturl",
    "issuer",
    "basket",
)


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)
    _prime_www(s)
    for url in URLS:
        r = _page_get(s, url, referer="https://www.bol.com/nl/nl/")
        html = r.text
        print(f"=== {url} status={r.status_code} len={len(html)}")
        ops = sorted(
            set(
                re.findall(
                    r'operationName["\']?\s*[:=]\s*["\']([A-Za-z0-9_]+)',
                    html,
                    re.I,
                )
            )
        )
        hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", html)))
        print("operations:", ops[:40])
        print("hash count:", len(hashes))
        for h in hashes[:20]:
            print(" ", h)
        low = html.lower()
        for kw in KEYWORDS:
            if kw in low:
                print("  has keyword:", kw)
        # remix chunk refs
        chunks = sorted(set(re.findall(r"/_remix/[^\"']+\.js", html)))
        print("remix chunks:", chunks[:8])


if __name__ == "__main__":
    main()
