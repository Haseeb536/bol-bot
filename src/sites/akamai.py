"""
bol.com Akamai Bot Manager — cookie/session helpers.

Akamai requires www-scoped cookies (_abck, ak_bmsc, bm_*, sbsd*) from a real browser.
2captcha login alone does not set _abck; import from Chrome or seed via Playwright.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

from src.config.settings import get_settings
from src.utils.logging import get_logger

log = get_logger("akamai")

from src.utils.app_root import get_app_root

ROOT_DIR = get_app_root()
SCRIPTS_DIR = ROOT_DIR / "scripts"

# Core Akamai / bol anti-bot cookies (www.bol.com)
AKAMAI_COOKIE_NAMES = (
    "_abck",
    "ak_bmsc",
    "bm_sv",
    "bm_sz",
    "bm_lso",
    "sbsd",
    "sbsd_o",
    "sbsd_c",
)

# Real bol product HTML is large; ~2–10 KB responses are Akamai challenge pages.
MIN_PRODUCT_PAGE_CHARS = 50_000
# Akamai challenge / placeholder responses are ~2–10 KB; real PDP is 50 KB+.
MAX_PLACEHOLDER_BLOCK_CHARS = 15_000


def is_product_placeholder_block(
    html: str,
    http_status: int,
    product_id: Optional[str] = None,
    url: Optional[str] = None,
) -> bool:
    """
    403/429 on bol /p/-/id/ short links — not the same as a live slug PDP blocked by Akamai.
    """
    if url:
        from src.sites.bol_urls import is_placeholder_product_url

        if not is_placeholder_product_url(url):
            return False
    if http_status not in (403, 429):
        return False
    if len(html) > MAX_PLACEHOLDER_BLOCK_CHARS:
        return False
    if product_id and product_id in html:
        return False
    return True


def is_akamai_challenge_page(html: str, http_status: int) -> bool:
    """200 OK but body is Akamai interactive challenge (not the real PDP)."""
    if http_status != 200 or len(html) > 20_000:
        return False
    low = html.lower()
    return (
        "sec-bc-tile" in low
        or "/.well-known/sbsd" in low
        or "sec-if-cpt-container" in low
    )


def is_readable_product_page(
    html: str,
    http_status: int,
    product_id: Optional[str] = None,
) -> bool:
    if http_status in (403, 429):
        return False
    if http_status in (404, 410):
        return True
    if len(html) < MIN_PRODUCT_PAGE_CHARS:
        return False
    if product_id and product_id not in html:
        return False
    return True


def login_txt_path() -> Path:
    custom = __import__("os").environ.get("BOL_LOGIN_TXT", "").strip()
    if custom:
        return Path(custom)
    return ROOT_DIR / "login.txt"


def parse_login_txt_cookie_header(path: Optional[Path] = None) -> Dict[str, str]:
    """
    Extract the best Cookie header from DevTools export (login.txt).
    Prefers blocks where :authority is www.bol.com.
    """
    path = path or login_txt_path()
    if not path.is_file():
        return {}

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates: list[tuple[int, int, str]] = []

    for i, line in enumerate(lines):
        if line.strip().lower() != "cookie" or i + 1 >= len(lines):
            continue
        raw = lines[i + 1].strip()
        if "_abck=" not in raw:
            continue
        context = "\n".join(lines[max(0, i - 25) : i]).lower()
        score = 0
        if "www.bol.com" in context:
            score += 10
        if ":authority" in context and "www.bol.com" in context:
            score += 5
        candidates.append((score, len(raw), raw))

    if not candidates:
        return {}

    candidates.sort(reverse=True)
    from src.bol.login import _parse_cookie_string

    parsed = _parse_cookie_string(candidates[0][2])
    log.info(f"Parsed {len(parsed)} cookies from {path.name} (Akamai www capture)")
    return parsed


def import_cookies_into_bol_token(
    cookies: Dict[str, str], *, source: str = "akamai_import"
) -> int:
    if not cookies:
        return 0
    import requests

    from src.bol.login import DEFAULT_HEADERS, dedupe_cookies, load_session, save_session

    loaded = load_session()
    if loaded:
        session, _meta = loaded
    else:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    dedupe_cookies(session)
    save_session(session, source=source)
    return len(cookies)


def has_valid_akamai_cookies() -> bool:
    settings = get_settings()
    if not settings.bol_token_path.is_file():
        return False
    try:
        import json

        data = json.loads(settings.bol_token_path.read_text(encoding="utf-8"))
        jar = data.get("cookies") if isinstance(data.get("cookies"), dict) else data
        if not isinstance(jar, dict):
            return False
        abck = jar.get("_abck") or ""
        return bool(abck) and len(str(abck)) > 50
    except Exception:
        return False


def ensure_akamai_cookies_sync(
    *,
    proxy_url: Optional[str] = None,
    force_playwright: bool = False,
) -> bool:
    """
    Ensure bol_token.json has Akamai clearance (_abck).
    Order: existing token → login.txt import → Playwright www visit.
    """
    if not force_playwright and has_valid_akamai_cookies():
        return True

    txt = login_txt_path()
    if txt.is_file() and not force_playwright:
        imported = parse_login_txt_cookie_header(txt)
        if imported:
            import_cookies_into_bol_token(imported, source="login_txt_akamai")
            if has_valid_akamai_cookies():
                log.info("Akamai cookies loaded from login.txt")
                return True
            log.warning("login.txt imported but _abck still missing or stale")

    if __import__("os").environ.get("BOL_NO_PLAYWRIGHT", "").lower() in ("1", "true", "yes"):
        log.error(
            "Akamai _abck missing. Run: python main.py --import-cookies "
            "or python main.py --seed-akamai"
        )
        return False

    from src.sites.bol_session import fetch_product_page_playwright_sync  # noqa: WPS433

    log.info("Seeding Akamai cookies via Playwright (www.bol.com)...")
    status, _html = fetch_product_page_playwright_sync(
        "https://www.bol.com/nl/nl/", proxy_url=proxy_url
    )
    ok = has_valid_akamai_cookies()
    if ok:
        log.info(f"Akamai seed OK (HTTP {status}, _abck present)")
    else:
        log.error(
            f"Akamai seed failed (HTTP {status}). Paste fresh cookies: "
            "python main.py --import-cookies"
        )
    return ok


async def ensure_akamai_cookies(
    *, proxy_url: Optional[str] = None, force_playwright: bool = False
) -> bool:
    import asyncio

    return await asyncio.to_thread(
        ensure_akamai_cookies_sync,
        proxy_url=proxy_url,
        force_playwright=force_playwright,
    )
