#!/usr/bin/env python3
"""
Seed Akamai (_abck) cookies for bol.com through RoundProxies + Playwright.

Usage:
    python scripts/bol_seed_akamai.py

Uses config/roundproxies.yaml when present (recommended for bol.nl).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from src.proxy.bol_proxy import get_roundproxies_config, get_roundproxies_pool
    from src.sites.akamai import has_valid_akamai_cookies
    from src.sites.bol_session import seed_session_via_proxy

    pool = get_roundproxies_pool()
    cfg = get_roundproxies_config()
    from src.sites.bol_urls import resolve_product_url

    product_url = resolve_product_url(
        "9300000256665012",
        "https://www.bol.com/nl/nl/p/-/9300000256665012/",
        {
            "product_slug": "pokemon-me02-5-ascended-heroes-elite-trainer-box",
        },
    )

    if pool:
        print(f"[akamai] RoundProxies country={cfg.country if cfg else '?'}")
        if cfg and cfg.country.lower().replace("-", "") != "netherlands":
            print("[warn] bol.nl prefers Netherlands proxies — Uganda may stay blocked")
        ok = await seed_session_via_proxy(product_url, pool[0])
        if ok:
            print("[ok] Product page loaded via proxy -> bol_token.json updated")
            print("Run: python main.py")
            return
        from src.sites.akamai import has_valid_akamai_cookies

        if has_valid_akamai_cookies():
            print("[ok] Akamai cookies saved (_abck present). Product PDP may still be 403 pre-drop.")
            print("Run: python main.py")
            return
        print("[warn] Homepage seeded via proxy; product PDP still 403 (normal before drop).")
        print("  www.bol.com + NL proxy are working — run: python main.py")
        print("  Optional: paste Chrome cookies (www.bol.com) into login.txt or bol_import_cookies.py")
        return

    if has_valid_akamai_cookies():
        print("[ok] bol_token.json already has _abck (no proxy configured)")
        return

    print("[error] Configure config/roundproxies.yaml first, then re-run this script.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
