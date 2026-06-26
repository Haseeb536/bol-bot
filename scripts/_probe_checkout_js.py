#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
sys.path.insert(0, __file__.rsplit("\\", 1)[0] + "/..")

from bol_login import ensure_session
from bol_cart import _init_session_holder, _page_get, _prime_www, _request

CHECKOUT_PAGE = "https://www.bol.com/nl/nl/checkout/"
OUT = Path(__file__).resolve().parent.parent / "_checkout_dump.html"


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)
    _prime_www(s)
    r = _page_get(s, CHECKOUT_PAGE, referer="https://www.bol.com/nl/nl/")
    html = r.text
    OUT.write_text(html, encoding="utf-8")
    print("saved", OUT, "status", r.status_code, "len", len(html))

    hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", html)))
    print("hashes in html", len(hashes))
    for h in hashes[:30]:
        print(" ", h)

    pairs = re.findall(
        r'\["([A-Za-z][A-Za-z0-9_]{2,60})","(sha256:[a-f0-9]{64})"\]',
        html,
    )
    print("pairs", len(pairs))
    for op, h in pairs[:50]:
        print(f"  {op} -> {h}")

    for pat in [
        r"paymentUrl[^\"]{0,80}",
        r"redirectUrl[^\"]{0,80}",
        r"ideal[^\"]{0,40}",
        r"checkoutSession[^\"]{0,60}",
        r"orderId[^\"]{0,60}",
    ]:
        ms = re.findall(pat, html, re.I)[:5]
        if ms:
            print("match", pat, ms)

    # static asset hosts in page
    hosts = sorted(set(re.findall(r"https://[a-z0-9.-]+\.bol\.com[^\"']*checkout[^\"']*\.js", html)))
    print("asset urls", hosts[:10])

    manifest_m = re.search(r"manifest-([a-f0-9]+)\.js", html)
    if manifest_m:
        for base in (
            "https://www.bol.com/_remix/",
            "https://static.bol.com/_remix/",
            "https://assets.s-bol.com/_remix/",
        ):
            mf = base + "manifest-" + manifest_m.group(1) + ".js"
            mr = _request(s, "GET", mf, timeout=60)
            print("try", mf, "->", mr.status_code, len(mr.text))
            if mr.status_code == 200 and "checkout" in mr.text.lower():
                paths = re.findall(r'"(/_remix/[^"]*checkout[^"]*\.js)"', mr.text, re.I)
                print(" checkout paths", paths[:5])


if __name__ == "__main__":
    main()
