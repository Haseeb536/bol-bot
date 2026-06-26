#!/usr/bin/env python3
"""
Quick test: does tls_client bypass Akamai on bol.com?

Usage:
    python scripts/test_tls_fetch.py
    python scripts/test_tls_fetch.py --proxy http://user:pass@host:port
    python scripts/test_tls_fetch.py --prime    # visit homepage first then PDP
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sites.tls_profiles import BOL_HEADERS_NL
from src.utils.logging import setup_logging, get_logger

setup_logging("DEBUG")
log = get_logger("test_tls")


def test_fetch(url: str, proxy_url: str | None, prime: bool) -> None:
    from src.sites.bol_tls_fetch import fetch_product_page_tls, prime_www_tls

    if prime:
        log.info("Step 1: Priming www.bol.com homepage ...")
        home_status, home_html = prime_www_tls(proxy_url)
        log.info(f"Homepage: HTTP {home_status} | {len(home_html)} chars")
        import time; time.sleep(2.5)

    log.info(f"Fetching product page: {url}")
    status, html = fetch_product_page_tls(url, proxy_url=proxy_url)

    print(f"\n{'='*60}")
    print(f"HTTP status : {status}")
    print(f"Body size   : {len(html)} chars")

    if status == 200 and len(html) > 50_000:
        print("✅ SUCCESS — real product page received!")
        # Show stock hint
        if '"buyable":true' in html or '"buyable": true' in html:
            print("🟢 Product is IN STOCK (buyable=true found)")
        elif '"buyable":false' in html or '"buyable": false' in html:
            print("🔴 Product is OUT OF STOCK")
        else:
            print("⚪ Availability not found in JSON — check HTML")
    elif status in (403, 429) and len(html) < 15_000:
        print("❌ BLOCKED by Akamai (small 403/429 response)")
        print("   Try: --prime  or use a Netherlands residential proxy")
    elif status == 403 and len(html) > 15_000:
        print("⚠️  403 but large response — may be pre-drop placeholder")
    else:
        print(f"⚠️  Unexpected: HTTP {status}, {len(html)} chars")

    # Show first 500 chars of body for debug
    print(f"\nBody preview:\n{html[:500]}")
    print("="*60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://www.bol.com/nl/nl/p/pokemon-me02-5-ascended-heroes-elite-trainer-box/9300000256665012/",
        help="Product URL to test",
    )
    parser.add_argument("--proxy", default=None, help="Proxy URL (http://user:pass@host:port)")
    parser.add_argument("--prime", action="store_true", help="Visit homepage first before product page")
    args = parser.parse_args()

    test_fetch(args.url, args.proxy, args.prime)


if __name__ == "__main__":
    main()
