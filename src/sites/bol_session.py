from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp
from yarl import URL

from src.config.settings import get_settings
from src.proxy.bol_proxy import proxy_label, requests_proxy_dict
from src.utils.logging import get_logger

log = get_logger("bol.session")

BOL_ORIGIN = "https://www.bol.com"
from src.utils.app_root import get_app_root

ROOT_DIR = get_app_root()
SCRIPTS_DIR = ROOT_DIR / "scripts"

BOL_PAGE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "nl-NL,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_last_relogin_at: float = 0.0
_last_playwright_fetch_at: float = 0.0
_last_proxy_seed_at: float = 0.0
_last_www_prime_at: float = 0.0
_startup_login_lock = threading.Lock()
_startup_login_cached_msg: Optional[str] = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _playwright_fetch_cooldown_sec() -> float:
    return _env_float("BOL_PLAYWRIGHT_COOLDOWN_SEC", 600.0)


def proxy_seed_cooldown_sec() -> float:
    return _env_float("BOL_RESEED_COOLDOWN_SEC", 900.0)


def www_prime_cooldown_sec() -> float:
    return _env_float("BOL_PRIME_COOLDOWN_SEC", 300.0)


def _poll_playwright_enabled() -> bool:
    """Playwright on every poll burns proxies and triggers 429 — off by default."""
    return os.environ.get("BOL_POLL_PLAYWRIGHT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def startup_playwright_seed_enabled() -> bool:
    return os.environ.get("BOL_STARTUP_SEED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def poll_reseed_enabled() -> bool:
    """Extra Playwright re-seed during monitoring — off by default (1 HTTP poll only)."""
    return os.environ.get("BOL_POLL_RESEED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def startup_login_enabled() -> bool:
    return os.environ.get("BOL_NO_STARTUP_LOGIN", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def direct_fallback_enabled() -> bool:
    """When proxy returns Akamai stub, retry the same URL without proxy (login.txt IP)."""
    return os.environ.get("BOL_NO_DIRECT_FALLBACK", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def may_run_proxy_seed() -> bool:
    return time.monotonic() - _last_proxy_seed_at >= proxy_seed_cooldown_sec()


def mark_proxy_seed_ran() -> None:
    global _last_proxy_seed_at
    _last_proxy_seed_at = time.monotonic()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_cookie_dict() -> Dict[str, str]:
    settings = get_settings()
    token = _read_json(settings.bol_token_path)
    raw = token.get("cookies") if isinstance(token.get("cookies"), dict) else token
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and v != ""}


def load_basket_id() -> Optional[str]:
    settings = get_settings()
    cred = _read_json(settings.credentials_path)
    bid = cred.get("basket_id") or cred.get("basketId")
    return str(bid).strip() if bid else None


def apply_cookies_to_session(session: aiohttp.ClientSession) -> int:
    cookies = load_cookie_dict()
    if not cookies:
        return 0
    session.cookie_jar.update_cookies(cookies, response_url=URL(BOL_ORIGIN))
    return len(cookies)


def _auto_relogin_disabled() -> bool:
    return os.environ.get("BOL_NO_AUTO_RELOGIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _relogin_cooldown_sec() -> float:
    try:
        return float(os.environ.get("BOL_RELOGIN_COOLDOWN_SEC", "300"))
    except ValueError:
        return 300.0


def has_akamai_cookie() -> bool:
    from src.sites.akamai import has_valid_akamai_cookies

    return has_valid_akamai_cookies()


def _save_cookies_from_playwright(pw_cookies: list) -> None:
    import requests

    from src.bol.login import DEFAULT_HEADERS, dedupe_cookies, save_session  # noqa: E402

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    for c in pw_cookies:
        domain = c.get("domain") or ""
        if "bol.com" not in domain:
            continue
        session.cookies.set(
            c["name"],
            c["value"],
            domain=domain if domain.startswith(".") else f".{domain.lstrip('.')}",
            path=c.get("path") or "/",
        )
    dedupe_cookies(session)
    save_session(session, source="playwright_monitor")
    if session.cookies.get("_abck"):
        log.info("Playwright saved cookies including _abck to bol_token.json")


async def persist_playwright_cookies_to_token(context: Any) -> None:
    """Keep bol_token.json in sync after checkout browser session."""
    try:
        pw_cookies = await context.cookies()
        if pw_cookies:
            _save_cookies_from_playwright(pw_cookies)
    except Exception as exc:
        log.warning(f"Could not persist Playwright cookies: {exc}")


def _playwright_proxy(proxy_url: Optional[str]) -> Optional[dict]:
    if not proxy_url:
        return None
    from urllib.parse import urlparse

    u = urlparse(proxy_url)
    if not u.hostname:
        return None
    server = f"{u.scheme or 'http'}://{u.hostname}:{u.port or 80}"
    out: dict = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


async def _playwright_fetch_impl(
    url: str, proxy_url: Optional[str] = None, *, save_token: bool = True
) -> Tuple[int, str]:
    """Real Chromium fetch — runs Akamai sensor JS and sets _abck cookies."""
    from playwright.async_api import async_playwright

    cookies = load_cookie_dict()
    profile_state = ROOT_DIR / "data" / "browser" / "bol_main" / "state.json"
    no_proxy = os.environ.get("BOL_NO_PROXY", "").strip().lower() in {"1", "true", "yes"}
    pw_proxy = _playwright_proxy(proxy_url)
    if not pw_proxy and not no_proxy:
        pw_proxy = _playwright_proxy((requests_proxy_dict() or {}).get("http"))
    log.info(
        f"Playwright fetch ({proxy_label(proxy_url or (pw_proxy or {}).get('server'))}): {url[:60]}..."
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs: dict = {
            "locale": "nl-NL",
            "timezone_id": "Europe/Amsterdam",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if profile_state.is_file():
            ctx_kwargs["storage_state"] = str(profile_state)
        if pw_proxy:
            ctx_kwargs["proxy"] = pw_proxy
        context = await browser.new_context(**ctx_kwargs)
        if cookies and not profile_state.is_file():
            await context.add_cookies(
                [
                    {"name": name, "value": value, "domain": ".bol.com", "path": "/"}
                    for name, value in cookies.items()
                ]
            )
        page = await context.new_page()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3000)
        html = await page.content()
        status = resp.status if resp else 0
        log.info(f"Playwright HTTP {status} ({len(html)} chars)")
        if save_token:
            _save_cookies_from_playwright(await context.cookies())
        else:
            log.debug("Playwright fetch: skipped bol_token.json save (ATC session preserved)")
        profile_state.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(profile_state))
        await browser.close()
        return status, html


async def fetch_product_page_playwright(
    url: str, proxy_url: Optional[str] = None, *, save_token: bool = True
) -> Tuple[int, str]:
    return await _playwright_fetch_impl(url, proxy_url, save_token=save_token)


def fetch_product_page_playwright_sync(
    url: str, proxy_url: Optional[str] = None, *, save_token: bool = True
) -> Tuple[int, str]:
    return asyncio.run(
        _playwright_fetch_impl(url, proxy_url, save_token=save_token)
    )


def fetch_product_page_sync(url: str, proxy_url: Optional[str] = None) -> Tuple[int, str]:
    """
    Fetch product HTML — tries tls_client (Chrome 120 exact fingerprint) first,
    then falls back to curl_cffi (bol_cart stack).

    tls_client matches the exact H2 settings, pseudo-header order, and TLS
    signature algorithms that Akamai expects from a real Chrome 120 browser.
    curl_cffi uses a generic Chrome profile that Akamai increasingly rejects.
    """
    # ── Primary: tls_client with Chrome 120 exact fingerprint ──────────────
    try:
        from src.sites.bol_tls_fetch import fetch_product_page_tls
        status, text = fetch_product_page_tls(url, proxy_url=proxy_url)
        if status != 0:  # 0 means tls_client not installed
            if status not in (403, 429) or len(text) > 15_000:
                # Got a real response (even 403 PDP is better than nothing)
                return status, text
            log.debug(f"tls_client got {status} — falling back to curl_cffi")
    except Exception as exc:
        log.debug(f"tls_client path error: {exc}")

    # ── Fallback: curl_cffi (original bol_cart stack) ────────────────────
    from src.bol.login import (  # noqa: E402
        dedupe_cookies,
        ensure_session,
        load_session,
        save_session,
    )
    from src.bol.cart import _page_get, _prime_www  # noqa: E402

    proxies = requests_proxy_dict(proxy_url)
    if proxies:
        log.debug(f"Monitor curl fetch via {proxy_label(proxy_url)}")

    loaded = load_session()
    if loaded:
        session, _meta = loaded
    else:
        session = ensure_session()
    dedupe_cookies(session)
    skip_prime = os.environ.get("BOL_MONITOR_PRIME", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }
    now = time.monotonic()
    global _last_www_prime_at
    if (
        not skip_prime
        and not session.cookies.get("_abck")
        and now - _last_www_prime_at >= www_prime_cooldown_sec()
    ):
        _prime_www(session)
        _last_www_prime_at = now
    resp = _page_get(
        session, url, referer="https://www.bol.com/nl/nl/", proxies=proxies
    )
    save_session(session, source="monitor_fetch")
    return int(resp.status_code), resp.text or ""


def refresh_bol_login_sync(*, force: bool = False) -> str:
    """
    Prime www.bol.com or run a fresh login (2captcha) and save bol_token.json.
    """
    global _last_relogin_at

    if _auto_relogin_disabled():
        return "Auto re-login disabled (set BOL_NO_AUTO_RELOGIN=0 to enable)"

    now = time.monotonic()
    cooldown = _relogin_cooldown_sec()
    if not force and now - _last_relogin_at < cooldown:
        wait = int(cooldown - (now - _last_relogin_at))
        return f"Re-login skipped (cooldown {wait}s remaining)"

    from src.bol.login import (  # noqa: E402
        _load_default_credentials,
        clear_saved_session,
        dedupe_cookies,
        ensure_session,
        has_auth_cookies,
        is_valid,
        load_session,
        save_session,
    )
    from src.bol.cart import _prime_www  # noqa: E402

    username, password = _load_default_credentials()
    if not username or not password:
        return (
            "Cannot auto-login: missing username/password in bol_credentials.json. "
            "Use: python main.py --import-cookies"
        )

    _last_relogin_at = now

    if not force:
        loaded = load_session()
        if loaded:
            session, _meta = loaded
            if is_valid(session) and has_auth_cookies(session):
                dedupe_cookies(session)
                if not session.cookies.get("_abck"):
                    return (
                        "Logged in (BUI) but missing Akamai _abck — 2captcha login cannot fix www 403. "
                        "Import Chrome cookies: python main.py --import-cookies "
                        "(or monitor will use Playwright fallback)."
                    )
                log.info("Session cookies present — priming www.bol.com (Akamai _abck)...")
                _prime_www(session)
                save_session(session, source="monitor_prime")
                if session.cookies.get("_abck"):
                    return "Primed session and saved cookies"
                return (
                    "Prime did not obtain _abck (www still blocked). "
                    "Run: python main.py --import-cookies"
                )

    from src.sites.akamai import (  # noqa: WPS433
        has_valid_akamai_cookies,
        import_cookies_into_bol_token,
        login_txt_path,
        parse_login_txt_cookie_header,
    )

    txt = login_txt_path()
    if txt.is_file():
        imported = parse_login_txt_cookie_header(txt)
        if imported:
            import_cookies_into_bol_token(imported, source="monitor_reimport")
            if has_valid_akamai_cookies():
                return (
                    "Re-imported Akamai cookies from login.txt "
                    "(skipped 2captcha — it does not fix www.bol.com 403)"
                )

    log.warning("Running fresh bol.com login (2captcha)...")
    clear_saved_session()
    session = ensure_session(username, password, force_refresh=True)
    dedupe_cookies(session)
    _prime_www(session)
    save_session(session, source="monitor_relogin")
    names = [c.name for c in session.cookies]
    return f"Fresh login saved to bol_token.json ({len(names)} cookies, BUI={'BUI' in names})"


def _playwright_fallback_enabled() -> bool:
    return os.environ.get("BOL_NO_PLAYWRIGHT", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def _product_id_from_url(url: str) -> Optional[str]:
    import re

    m = re.search(r"/(\d{10,})/?", url)
    return m.group(1) if m else None


async def _playwright_seed_via_proxy(
    product_url: str, proxy_url: str
) -> Tuple[int, str]:
    """
    Human-like browsing session through the proxy to seed Akamai cookies.

    Navigation pattern matters — Akamai's sensor JS fingerprints:
      - Direct URL jumps (bot signal)
      - Navigation history (referrer chain)
      - Mouse movement / scroll activity
      - Time-on-page before navigating

    Flow: homepage → search for product name → click result → product page
    This matches what a real shopper does and builds up Akamai trust score.
    """
    import random
    from playwright.async_api import async_playwright

    from src.sites.akamai import is_readable_product_page

    profile_state = ROOT_DIR / "data" / "browser" / "bol_main" / "state.json"
    pw_proxy = _playwright_proxy(proxy_url)
    home = "https://www.bol.com/nl/nl/"
    pid = _product_id_from_url(product_url)
    log.info(
        f"Playwright seed ({proxy_label(proxy_url)}): human-like browse to product ..."
    )

    async def _human_pause(min_ms: int = 800, max_ms: int = 2500) -> None:
        """Random pause mimicking human think/read time."""
        await page.wait_for_timeout(random.randint(min_ms, max_ms))

    async def _scroll_page() -> None:
        """Scroll down then back up — human reading behaviour."""
        await page.evaluate("window.scrollTo({top: 400, behavior: 'smooth'})")
        await page.wait_for_timeout(random.randint(600, 1200))
        await page.evaluate("window.scrollTo({top: 800, behavior: 'smooth'})")
        await page.wait_for_timeout(random.randint(400, 900))
        await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        await page.wait_for_timeout(random.randint(300, 700))

    async def _move_mouse_randomly() -> None:
        """Random mouse movements across the viewport."""
        for _ in range(random.randint(2, 4)):
            x = random.randint(100, 1180)
            y = random.randint(100, 700)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(random.randint(150, 400))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--no-sandbox",
            ],
        )
        ctx_kwargs: dict = {
            "locale": "nl-NL",
            "timezone_id": "Europe/Amsterdam",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "extra_http_headers": {
                "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }
        if profile_state.is_file():
            ctx_kwargs["storage_state"] = str(profile_state)
        if pw_proxy:
            ctx_kwargs["proxy"] = pw_proxy

        context = await browser.new_context(**ctx_kwargs)

        # Patch navigator.webdriver to hide automation
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # ── Step 1: Visit homepage ────────────────────────────────────────────
        resp = await page.goto(home, wait_until="networkidle", timeout=120_000)
        home_status = resp.status if resp else 0
        home_html = await page.content()
        log.info(f"Playwright seed home HTTP {home_status} ({len(home_html)} chars)")
        _save_cookies_from_playwright(await context.cookies())

        # Behave like a human reading the homepage
        await _move_mouse_randomly()
        await _scroll_page()
        await _human_pause(2000, 4000)

        # ── Step 2: Navigate to product via search (not direct URL jump) ──────
        # Extract product name from URL slug for search query
        slug_match = None
        try:
            slug_part = product_url.rstrip("/").split("/")
            # URL: /nl/nl/p/<slug>/<id>/ → slug is before the last numeric segment
            for part in reversed(slug_part):
                if part.isdigit() or not part:
                    continue
                slug_match = part.replace("-", " ")
                break
        except Exception:
            pass

        navigated_to_pdp = False
        if slug_match:
            # Build search URL using the product slug as search query
            import urllib.parse
            search_query = urllib.parse.quote_plus(slug_match[:50])
            search_url = f"https://www.bol.com/nl/nl/s/?searchtext={search_query}"
            log.info(f"Playwright seed: search → {search_url[:80]}")
            try:
                resp = await page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                    referer=home,
                )
                await _human_pause(1500, 3000)
                await _scroll_page()
                await _move_mouse_randomly()
                await _human_pause(1000, 2500)

                # Now navigate to the actual product page (from search as referrer)
                resp = await page.goto(
                    product_url,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                    referer=search_url,
                )
                navigated_to_pdp = True
            except Exception as exc:
                log.debug(f"Search-path navigation failed, falling back: {exc}")

        if not navigated_to_pdp:
            # Fallback: direct navigation with homepage as referer
            resp = await page.goto(
                product_url,
                wait_until="domcontentloaded",
                timeout=90_000,
                referer=home,
            )

        # ── Step 3: Read product page result ─────────────────────────────────
        await _human_pause(3000, 6000)
        await _scroll_page()
        await _move_mouse_randomly()
        await _human_pause(1500, 3000)

        html = await page.content()
        status = resp.status if resp else 0
        log.info(f"Playwright seed product HTTP {status} ({len(html)} chars)")

        if not is_readable_product_page(html, status, pid):
            # One reload attempt — sometimes Akamai serves a challenge then clears
            await _human_pause(3000, 5000)
            resp = await page.reload(wait_until="domcontentloaded", timeout=90_000)
            await _human_pause(2000, 4000)
            html = await page.content()
            status = resp.status if resp else 0
            log.info(
                f"Playwright seed product reload HTTP {status} ({len(html)} chars)"
            )

        _save_cookies_from_playwright(await context.cookies())
        profile_state.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(profile_state))
        await browser.close()
        return status, html


async def seed_session_via_proxy(
    product_url: str, proxy_url: Optional[str]
) -> Tuple[bool, int, str]:
    """
    Visit bol.com via Playwright on the same proxy as monitoring to seed Akamai cookies.

    Returns (seeded_ok, http_status, html) so the caller can use the already-fetched
    Playwright HTML directly — avoiding a second tls_client/curl request that will
    almost certainly get 403 because it can't maintain Akamai's sensor state without
    a real browser executing JavaScript.
    """
    from src.sites.akamai import (
        has_valid_akamai_cookies,
        is_product_placeholder_block,
        is_readable_product_page,
    )

    if not proxy_url or not _playwright_fallback_enabled():
        return False, 0, ""
    if not may_run_proxy_seed():
        wait = int(proxy_seed_cooldown_sec() - (time.monotonic() - _last_proxy_seed_at))
        log.debug(f"Proxy seed skipped (cooldown {wait}s)")
        return False, 0, ""
    mark_proxy_seed_ran()
    log.info("Seeding Akamai session through proxy (www.bol.com + product page)...")
    st, html = await _playwright_seed_via_proxy(product_url, proxy_url)
    pid = _product_id_from_url(product_url)
    if is_readable_product_page(html, st, pid):
        log.info(f"Playwright seed: live product page ({len(html)} chars) — using directly")
        return True, st, html  # ← caller uses this HTML, no second fetch needed
    if is_product_placeholder_block(html, st, pid, product_url) or has_valid_akamai_cookies():
        log.info(
            "Proxy/www seed OK (product PDP still pre-drop 403 — monitoring OFFLINE→ONLINE)"
        )
        return False, st, html
    return False, st, html


async def fetch_product_page(
    url: str, proxy_url: Optional[str] = None
) -> Tuple[int, str]:
    """
    Fetch order:
      1. tls_client (Chrome 120 exact fingerprint) — best Akamai bypass
      2. curl_cffi (generic Chrome) — included in tls fallback inside fetch_product_page_sync
      3. Playwright (real Chromium) — gated by cooldown + BOL_POLL_PLAYWRIGHT
    """
    global _last_playwright_fetch_at
    from src.sites.akamai import is_product_placeholder_block, is_readable_product_page

    pid = _product_id_from_url(url)

    # Steps 1 + 2: tls_client → curl_cffi (handled inside fetch_product_page_sync)
    status, html = await asyncio.to_thread(fetch_product_page_sync, url, proxy_url)
    if is_readable_product_page(html, status, pid):
        return status, html
    if is_product_placeholder_block(html, status, pid, url):
        return status, html

    # Step 3: Playwright fallback (gated)
    if not _playwright_fallback_enabled() or not _poll_playwright_enabled():
        return status, html

    now = time.monotonic()
    if now - _last_playwright_fetch_at < _playwright_fetch_cooldown_sec():
        return status, html

    _last_playwright_fetch_at = now
    try:
        pw_status, pw_html = await fetch_product_page_playwright(url, proxy_url)
        if is_readable_product_page(pw_html, pw_status, pid):
            return pw_status, pw_html
    except Exception as exc:
        log.warning(f"Playwright fetch failed: {exc}")

    return 403, html


async def refresh_bol_login(*, force: bool = False) -> str:
    return await asyncio.to_thread(refresh_bol_login_sync, force=force)


def _prime_www_on_monitor_proxy(session: Any) -> None:
    """Prime www.bol.com on the same NL proxy used for monitoring (IP consistency)."""
    from src.bol.cart import _page_get, _prime_www  # noqa: E402

    try:
        from src.proxy.bol_proxy import get_roundproxies_pool, requests_proxy_dict

        pool = get_roundproxies_pool()
        if pool:
            proxy_url = pool[0]
            os.environ.setdefault("BOL_PROXY_URL", proxy_url)
            px = requests_proxy_dict(proxy_url)
            log.info(
                f"Startup www prime via {proxy_label(proxy_url)} "
                "(same IP as monitor/ATC)"
            )
            resp = _page_get(
                session,
                "https://www.bol.com/nl/nl/",
                referer="https://www.bol.com/",
                proxies=px,
            )
            if session.cookies.get("_abck"):
                log.info("Startup www prime: Akamai _abck acquired on proxy")
            elif resp.status_code == 200:
                log.info("Startup www prime: homepage OK on proxy")
            return
    except Exception as exc:
        log.debug(f"Startup proxy www prime failed: {exc}")

    _prime_www(session)


def startup_bol_login_sync() -> str:
    """
    Run bol_login.py-style session refresh on bot startup:
    fresh login → save bol_token.json → prime www on monitor proxy → merge login.txt.
    Falls back to existing bol_token.json + login.txt if login fails.
    """
    global _startup_login_cached_msg
    with _startup_login_lock:
        if _startup_login_cached_msg is not None:
            return _startup_login_cached_msg

    if not startup_login_enabled():
        return _cache_startup_login_msg("Startup login disabled (BOL_NO_STARTUP_LOGIN=1)")

    from src.bol.login import (  # noqa: E402
        _load_default_credentials,
        dedupe_cookies,
        ensure_session,
        has_auth_cookies,
        is_valid,
        load_session,
        save_session,
    )
    from src.sites.akamai import (  # noqa: WPS433
        has_valid_akamai_cookies,
        import_cookies_into_bol_token,
        login_txt_path,
        parse_login_txt_cookie_header,
    )

    def _merge_login_txt() -> None:
        txt = login_txt_path()
        if not txt.is_file():
            return
        imported = parse_login_txt_cookie_header(txt)
        if imported:
            n = import_cookies_into_bol_token(imported, source="startup_login_txt")
            if has_valid_akamai_cookies():
                log.info(f"Merged {n} Akamai cookies from login.txt (_abck present)")

    def _fallback_session_msg(reason: str) -> str:
        _merge_login_txt()
        loaded = load_session()
        if loaded:
            session, _meta = loaded
            if is_valid(session) and has_auth_cookies(session):
                dedupe_cookies(session)
                if not session.cookies.get("_abck"):
                    _prime_www_on_monitor_proxy(session)
                save_session(session, source="startup_fallback")
                names = [c.name for c in session.cookies]
                return (
                    f"Startup login failed ({reason}) — using saved bol_token.json "
                    f"({len(names)} cookies, BUI={'BUI' in names})"
                )
        return (
            f"Startup login failed ({reason}) — fix username/password in "
            "bol_credentials.json or import Chrome cookies: "
            "python main.py --import-cookies"
        )

    username, password = _load_default_credentials()
    force_saved_only = os.environ.get("BOL_FORCE_STARTUP_LOGIN", "").strip().lower() in {
        "0",
        "false",
        "no",
    }

    _merge_login_txt()

    if not username or not password:
        loaded = load_session()
        if loaded:
            session, _meta = loaded
            if is_valid(session) and has_auth_cookies(session):
                dedupe_cookies(session)
                if not session.cookies.get("_abck"):
                    _prime_www_on_monitor_proxy(session)
                save_session(session, source="startup_cached")
                names = [c.name for c in session.cookies]
                return _cache_startup_login_msg(
                    f"Using saved bol_token.json "
                    f"({len(names)} cookies, BUI={'BUI' in names})"
                )
        return _cache_startup_login_msg(
            _fallback_session_msg("missing username/password")
        )

    if force_saved_only:
        loaded = load_session()
        if loaded:
            session, _meta = loaded
            if is_valid(session) and has_auth_cookies(session):
                dedupe_cookies(session)
                _merge_login_txt()
                if not session.cookies.get("_abck"):
                    _prime_www_on_monitor_proxy(session)
                save_session(session, source="startup_cached")
                names = [c.name for c in session.cookies]
                return _cache_startup_login_msg(
                    f"Using saved bol_token.json "
                    f"({len(names)} cookies, BUI={'BUI' in names})"
                )

    log.info("Startup: fresh bol.com login → bol_token.json...")
    try:
        session = ensure_session(username, password, force_refresh=True)
    except RuntimeError as exc:
        err = str(exc)
        if "credentials_invalid" in err.lower():
            reason = "bol.com rejected username/password (credentials_invalid)"
        else:
            reason = err[:120]
        log.warning(f"Startup login failed: {reason}")
        return _cache_startup_login_msg(_fallback_session_msg(reason))

    dedupe_cookies(session)
    _merge_login_txt()
    _prime_www_on_monitor_proxy(session)
    save_session(session, source="startup_login")
    try:
        from src.bol.cart import _clear_saved_basket_id

        _clear_saved_basket_id()
    except Exception:
        pass
    names = [c.name for c in session.cookies]

    return _cache_startup_login_msg(
        f"Startup login OK — bol_token.json saved "
        f"({len(names)} cookies, BUI={'BUI' in names}, "
        f"_abck={'yes' if session.cookies.get('_abck') else 'no'})"
    )


def _cache_startup_login_msg(msg: str) -> str:
    global _startup_login_cached_msg
    with _startup_login_lock:
        _startup_login_cached_msg = msg
    return msg


async def startup_bol_login() -> str:
    return await asyncio.to_thread(startup_bol_login_sync)
