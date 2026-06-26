"""
tls_client-based product page fetcher for bol.com.

Why this beats curl_cffi / aiohttp for bol.com:
  - tls_client exposes exact H2 SETTINGS frame values + ordering
  - Exact pseudo-header order (:method :authority :scheme :path)
  - Exact request header ordering (Akamai fingerprints this)
  - Exact TLS signature algorithm list from Chrome 120 ClientHello
  - curl_cffi uses generic Chrome profile; tls_client gives per-field control

tls_client is synchronous — we wrap calls in asyncio.to_thread() for
compatibility with the async monitor loop.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.utils.logging import get_logger
from src.sites.tls_profiles import BOL_HEADERS_NL, TLS_PROFILES

log = get_logger("bol.tls")

from src.utils.app_root import get_app_root

ROOT_DIR = get_app_root()

# Cooldown: don't build a new session more often than this (seconds)
_SESSION_TTL = 300.0
_last_session_time: float = 0.0
_cached_session = None


def _build_tls_session(proxy_url: Optional[str] = None):
    """
    Build a tls_client.Session with Chrome 120 exact fingerprint.
    Returns None if tls_client is not installed.
    """
    try:
        import tls_client
    except ImportError:
        log.warning(
            "tls_client not installed — run: pip install tls-client\n"
            "Falling back to curl_cffi."
        )
        return None

    profile = TLS_PROFILES["chrome_120"]

    session = tls_client.Session(
        # client_identifier sets the base JA3/TLS cipher suite list.
        # chrome_120 is the closest match; some deployments use chrome_117
        # intentionally to hit a specific JA3 hash that passes Akamai.
        client_identifier="chrome_120",
        random_tls_extension_order=True,  # mimic Chrome's randomised extension order
    )

    # HTTP/2 SETTINGS frame — exact Chrome 120 values
    session.h2_settings = profile["h2_settings"]
    session.h2_settings_order = profile["h2_settings_order"]

    # TLS ClientHello signature algorithms
    session.supported_signature_algorithms = profile["supported_signature_algorithms"]

    # HTTP/2 pseudo-header order (:method :authority :scheme :path)
    session.pseudo_header_order = profile["pseudo_header_order"]

    # Request header order — Akamai's _abck sensor fingerprints this
    session.header_order = profile["header_order"]

    # Base browser-like headers (Dutch locale for bol.nl)
    session.headers.update(BOL_HEADERS_NL)

    # Proxy
    if proxy_url:
        session.proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }
        log.debug(f"tls_client proxy: {proxy_url.split('@')[-1][:50]}")

    return session


def _load_cookies_into_session(session) -> int:
    """Load bol_token.json cookies into the tls_client session."""
    from src.sites.bol_session import load_cookie_dict
    cookies = load_cookie_dict()
    if not cookies:
        return 0
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    return len(cookies)


def _save_cookies_from_response(session) -> None:
    """Persist any new cookies from the response back to bol_token.json."""
    try:
        from src.sites.akamai import import_cookies_into_bol_token
        jar: Dict[str, str] = {}
        for cookie in session.cookies:
            if "bol.com" in (getattr(cookie, "domain", "") or ""):
                jar[cookie.name] = cookie.value
        if jar:
            import_cookies_into_bol_token(jar, source="tls_client_fetch")
            log.debug(f"Saved {len(jar)} cookies back to bol_token.json")
    except Exception as exc:
        log.debug(f"Cookie save failed: {exc}")


def fetch_product_page_tls(
    url: str,
    proxy_url: Optional[str] = None,
    referer: str = "https://www.bol.com/nl/nl/",
) -> Tuple[int, str]:
    """
    Fetch a bol.com product page using tls_client (Chrome 120 TLS fingerprint).

    Returns (http_status, html_text).
    Falls back to (0, "") if tls_client is not available.
    """
    session = _build_tls_session(proxy_url)
    if session is None:
        return 0, ""  # caller will use curl_cffi fallback

    # Load saved bol.com cookies
    n = _load_cookies_into_session(session)
    log.debug(f"tls_client fetch: {n} cookies loaded | {url[:80]}")

    # Fresh visitor-ID per request — avoids Akamai session-level replay detection
    extra_headers = {
        "referer": referer,
        "x-ccvisitorid": str(uuid.uuid4()),
        "x-ccvisitid": str(uuid.uuid4()),
    }

    try:
        resp = session.get(url, headers=extra_headers, timeout_seconds=25, allow_redirects=True)
        status = resp.status_code
        text = resp.text or ""

        # ── Detect bol.com explicit IP ban page ──────────────────────────────
        # bol.com serves HTTP 200 with "IP address is blocked" body when the IP
        # is hard-banned (not a soft Akamai challenge). Treating this as 200
        # would confuse the monitor — return 403 so it's handled as a block.
        low = text.lower()
        if status == 200 and len(text) < 10_000 and (
            "ip address is blocked" in low
            or "ip adres is geblokkeerd" in low
            or "temporarily blocked" in low
            or "tijdelijk geblokkeerd" in low
        ):
            via = f"via proxy {proxy_url.split('@')[-1][:30]}" if proxy_url else "direct (no proxy)"
            log.warning(
                f"tls_client: bol.com HARD IP BAN ({via}). "
                f"This IP is blocked by bol.com. "
                f"Use a Netherlands residential proxy and import Chrome cookies via login.txt."
            )
            return 403, text

        log.info(f"tls_client HTTP {status} ({len(text)} chars) | {url[:60]}")

        # Persist any Akamai cookies received (_abck, ak_bmsc, bm_sv …)
        _save_cookies_from_response(session)

        return status, text

    except Exception as exc:
        err = str(exc)
        if "timeout" in err.lower() or "canceled" in err.lower():
            # Akamai is TCP-dropping this IP — the _abck cookie is missing/stale.
            # The connection hangs then dies. This is WORSE than a 403 because
            # it means the IP has zero Akamai clearance on the PDP endpoint.
            # Fix: import fresh Chrome cookies (login.txt) then restart.
            log.warning(
                f"tls_client TCP timeout on PDP — Akamai is dropping the connection. "
                f"Use a NL residential proxy with fresh Chrome cookies from login.txt."
            )
            return 0, ""  # signal caller to use Playwright seed
        log.warning(f"tls_client fetch failed: {exc}")
        return 0, ""


async def fetch_product_page_tls_async(
    url: str,
    proxy_url: Optional[str] = None,
    referer: str = "https://www.bol.com/nl/nl/",
) -> Tuple[int, str]:
    """Async wrapper — runs tls_client fetch in a thread pool."""
    return await asyncio.to_thread(fetch_product_page_tls, url, proxy_url, referer)


def prime_www_tls(proxy_url: Optional[str] = None) -> Tuple[int, str]:
    """
    Visit www.bol.com homepage first (same pattern as the seed flow).
    This lets Akamai set the initial _abck + ak_bmsc cookies on this IP
    before we hit the product page.
    """
    home = "https://www.bol.com/nl/nl/"
    log.info(f"tls_client: priming www.bol.com homepage ...")
    status, html = fetch_product_page_tls(
        home,
        proxy_url=proxy_url,
        referer="",  # direct navigation — no referer on homepage
    )
    log.info(f"tls_client prime: HTTP {status} ({len(html)} chars)")
    return status, html


async def prime_then_fetch_tls(
    product_url: str,
    proxy_url: Optional[str] = None,
) -> Tuple[int, str]:
    """
    Two-step flow matching what Playwright seed does:
      1. Visit homepage to get Akamai clearance cookies
      2. Visit product page with those cookies

    This is the recommended flow for bypassing Akamai on bol.com.
    """
    await asyncio.to_thread(prime_www_tls, proxy_url)
    # Small pause — mimic human think time between homepage and PDP
    await asyncio.sleep(2.5)
    return await fetch_product_page_tls_async(product_url, proxy_url)
