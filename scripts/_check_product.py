#!/usr/bin/env python3
"""One-off product page check."""
import asyncio
import re
import sys

sys.path.insert(0, str(__file__).rsplit("scripts", 1)[0] if "scripts" in __file__ else ".")

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sites.bol import BolSiteAdapter
from src.sites.bol_session import fetch_product_page

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.bol.com/nl/nl/p/-/9300000256665012/"


async def main() -> None:
    import os

    if os.environ.get("BOL_FORCE_LOGIN", "").strip().lower() in {"1", "true", "yes"}:
        from src.sites.bol_session import refresh_bol_login

        print("Login:", await refresh_bol_login(force=True))

    st, html = await fetch_product_page(URL)
    state = BolSiteAdapter()._state_from_html(URL, st, html)
    print("=== bol.com product check ===")
    print("URL:", URL)
    print("HTTP:", st)
    print("Status:", state.status.value)
    print("Can add to cart:", state.can_add_to_cart)
    if state.error:
        print("Error:", state.error)
    low = html.lower()
    for phrase in [
        "in winkelwagen",
        "nog niet verkrijgbaar",
        "uitverkocht",
        "niet op voorraad",
        "houd mij op de hoogte",
    ]:
        if phrase in low:
            print("Found in page:", phrase)
    m = re.search(r'"buyable"\s*:\s*(true|false)', html, re.I)
    if m:
        print("Embedded buyable:", m.group(1))
    title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    if title:
        print("Title:", title.group(1).strip()[:120])
    print("Page length:", len(html), "chars")


if __name__ == "__main__":
    asyncio.run(main())
