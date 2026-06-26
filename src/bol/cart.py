#!/usr/bin/env python3
"""
bol.com add-to-cart helper.

Usage:
    python bol_cart.py <productId> [offerUid] [quantity]
    python bol_cart.py   # reads product_url + quantity from bol_credentials.json

Example:
    python bol_cart.py 9300000271683065
    python bol_cart.py 9300000271683065 f7972b78-6501-4f28-8e52-f72feed33f04 2

Cart limits (bol.com, enforced unless overridden via env):
  - BOL_MAX_UNITS_PER_ITEM=2   (max units per line item)
  - BOL_MAX_ITEMS_PER_CHECKOUT=4   (max distinct products per basket)

Flow (from captured network requests):
  1. Load / obtain a valid session from bol_login.py
  2. Fetch basket ID via GraphQL Basket query
  3. Fetch offer UID via GraphQL Offer query (if not provided)
  4. Resolve quantity (task qty, PDP max, per-item cap)
  5. Call GraphQL AddItem mutation

All GraphQL calls use persisted queries with sha256 hashes captured from live traffic.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

try:
    from curl_cffi import requests as curl_requests

    _CURL_AVAILABLE = True
except ImportError:
    curl_requests = None  # type: ignore[misc, assignment]
    _CURL_AVAILABLE = False

_CURL_IMPERSONATE = os.environ.get("BOL_IMPERSONATE", "chrome131")

from src.bol.login import (
    DEFAULT_HEADERS,
    clear_saved_session,
    dedupe_cookies,
    ensure_session,
    get_cookie_value,
    has_auth_cookies,
    prime_www_cookies,
    save_session,
    _load_default_credentials,
    _log_http,
    ROOT_DIR,
)

# All statuses that trigger backoff retries
BLOCK_HTTP_STATUSES = (403, 409, 429)
# Re-login only when session/auth is suspect — not on 429 (IP rate limit wastes 2captcha)
RELOGIN_HTTP_STATUSES = (403, 409)

_SESSION_HOLDER: Optional[Dict[str, Any]] = None

CREDENTIAL_FILE = os.path.join(ROOT_DIR, "bol_credentials.json")

GRAPHQL_URL = "https://www.bol.com/api/graphql"

# Persisted query hashes (product-web-fe / working monitor bots, May 2026)
HASH_ADD_ITEM = (
    "sha256:fda23bccf49694870747c1a4a5003944bca994020fc3cb05ae9c6cdf029aaa7c"
)
HASH_BASKET_QUERY = (
    "sha256:f51108f5b96e8bfca69bae940195d917a149a8e9107c8c07f7eb17732cf877a1"
)
HASH_CREATE_BASKET = (
    "sha256:92b016f96aa83a630f5cc5ebcd48d6da90e155aed1119a492e71856d99e590e0"
)
HASH_OFFER = "sha256:1463b809d2f60b211d3aa1ba11127a18025caf9b62677a413aca3d0a008d6c2c"
HASH_PRODUCT = "19a9e78148968e88bb63ef930b33d63b788c66d287ae658c413fe670389bcce4"
HASH_BASKET = (
    "sha256:74b6c1d6652a28d65b997594b012a911a796bb77c2b86142c01e711b65255c94"
)

TOKEN_FILE = os.path.join(ROOT_DIR, "bol_token.json")

PAGE_HEADERS = {
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


def _gql_headers(
    referer: str = "https://www.bol.com/",
    *,
    client_app: Optional[str] = None,
) -> Dict[str, str]:
    """Match bol_login session fingerprint (Chrome 148) for GraphQL."""
    app = (
        client_app
        or os.environ.get("BOL_CLIENT_APP", "").strip()
        or "product-web-fe"
    )
    h = dict(DEFAULT_HEADERS)
    h.update(
        {
            "Accept": (
                "application/graphql-response+json, application/graphql+json, application/json"
            ),
            "Accept-Language": "nl-NL",
            "Content-Type": "application/json",
            "bol-app-country": "NL",
            "bol-client-app-name": app,
            "Origin": "https://www.bol.com",
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return h

OFFER_UID_RE = re.compile(
    r'"offerUid"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    re.I,
)


def _init_session_holder(session: requests.Session) -> None:
    global _SESSION_HOLDER
    _SESSION_HOLDER = {
        "session": session,
        "relogin": False,
        "curl": None,
        "force_direct_gql": False,
    }


def _use_curl_for_bol() -> bool:
    if not _CURL_AVAILABLE:
        return False
    return os.environ.get("BOL_NO_CURL", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def _sync_cookies_to_curl(
    src: requests.Session, dest: Any, *, domain: str = ".bol.com"
) -> None:
    for cookie in src.cookies:
        dest.cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain or domain,
            path=cookie.path or "/",
        )


def _merge_cookies_from_response(
    resp: Any, dest: requests.Session, *, domain: str = ".bol.com"
) -> None:
    try:
        items = resp.cookies.items()
    except Exception:
        return
    for name, value in items:
        dest.cookies.set(name, value, domain=domain, path="/")
    dedupe_cookies(dest)


def _get_curl_session(req_session: requests.Session) -> Any:
    if not _use_curl_for_bol():
        raise RuntimeError("curl not available")
    if _SESSION_HOLDER is None:
        cs = curl_requests.Session(impersonate=_CURL_IMPERSONATE)
        _sync_cookies_to_curl(req_session, cs)
        return cs
    curl_sess = _SESSION_HOLDER.get("curl")
    if curl_sess is None:
        curl_sess = curl_requests.Session(impersonate=_CURL_IMPERSONATE)
        _SESSION_HOLDER["curl"] = curl_sess
    _sync_cookies_to_curl(req_session, curl_sess)
    return curl_sess


def _current_session() -> requests.Session:
    if _SESSION_HOLDER is None:
        raise RuntimeError("Session holder not initialized")
    return _SESSION_HOLDER["session"]


def _is_blocked_status(status: int) -> bool:
    return status in BLOCK_HTTP_STATUSES


def is_graphql_stub_response(resp: Any) -> bool:
    """Akamai/proxy often returns tiny text/plain bodies instead of GraphQL JSON."""
    ct = (getattr(resp, "headers", {}).get("Content-Type") or "").lower()
    body = getattr(resp, "content", b"") or b""
    size = len(body)
    if size < 128:
        try:
            data = resp.json()
            if isinstance(data, dict) and ("data" in data or "errors" in data):
                return False
        except Exception:
            pass
        return True
    if "text/plain" in ct and "json" not in ct and size < 2048:
        return True
    text = body.decode("utf-8", errors="replace").strip().lower()
    if text.startswith("<!") or text.startswith("<html"):
        return True
    if "access denied" in text or "request blocked" in text:
        return True
    return False


def _auto_relogin_disabled() -> bool:
    return os.environ.get("BOL_NO_AUTO_RELOGIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _should_relogin(status: int) -> bool:
    if status in RELOGIN_HTTP_STATUSES:
        return True
    if status == 429 and os.environ.get("BOL_RELOGIN_ON_429", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    return False


def _recreate_session_after_block(status: int) -> bool:
    """
    On 403/409 (and optionally 429): delete bol_token.json and run a fresh login once.
    Returns True if a new session was created.
    """
    global _SESSION_HOLDER
    if _SESSION_HOLDER is None or _SESSION_HOLDER.get("relogin"):
        return False
    if not _should_relogin(status) or _auto_relogin_disabled():
        return False

    active = _SESSION_HOLDER["session"]
    if has_auth_cookies(active):
        print(
            f"[session] HTTP {status} but login cookies (BUI) are present — "
            "skipping re-login (Akamai/IP block, not expired session)."
        )
        return False

    print(
        f"[session] HTTP {status} — no auth cookies; deleting session and logging in..."
    )
    clear_saved_session()
    time.sleep(3)

    username, password = _load_default_credentials()
    if not username or not password:
        raise RuntimeError(
            "Cannot re-login after block: set credentials in bol_credentials.json"
        )

    new_session = ensure_session(username, password, force_refresh=True)
    new_session.headers.update(DEFAULT_HEADERS)
    _prime_www(new_session)
    save_session(new_session, source="cart_relogin")
    _SESSION_HOLDER["session"] = new_session
    _SESSION_HOLDER["relogin"] = True
    _SESSION_HOLDER["curl"] = None
    print("[session] New login session saved to bol_token.json")
    return True


def _proxy_dict(proxies: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    if proxies is not None:
        return proxies
    if _SESSION_HOLDER and _SESSION_HOLDER.get("force_direct_gql"):
        return None
    if os.environ.get("BOL_NO_PROXY", "").strip().lower() in {"1", "true", "yes"}:
        fallback = os.environ.get("BOL_PROXY_FALLBACK_URL", "").strip()
        if os.environ.get("BOL_USE_PROXY_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
        } and fallback:
            return {"http": fallback, "https": fallback}
        return None
    env_url = os.environ.get("BOL_PROXY_URL", "").strip()
    if env_url:
        return {"http": env_url, "https": env_url}
    try:
        from src.proxy.bol_proxy import get_roundproxies_pool, requests_proxy_dict
    except ImportError:
        return None
    pool = get_roundproxies_pool()
    if not pool:
        return None
    px = requests_proxy_dict(pool[0])
    if px:
        print(f"[proxy] Using RoundProxies (NL residential)")
    return px


def _request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    proxies: Optional[Dict[str, str]] = None,
    force_no_proxy: bool = False,
    **kwargs: Any,
) -> requests.Response:
    if force_no_proxy:
        px = None
    elif proxies is not None:
        px = proxies
    else:
        px = _proxy_dict()
    if _use_curl_for_bol() and "bol.com" in url:
        cs = _get_curl_session(session)
        try:
            resp = cs.request(method, url, proxies=px, **kwargs)
        except Exception as exc:
            if _SESSION_HOLDER is not None:
                _SESSION_HOLDER["curl"] = None
            err = str(exc).lower()
            if "ssl" in err or "curl" in err or "connection" in err:
                cs = _get_curl_session(session)
                resp = cs.request(method, url, proxies=px, **kwargs)
            else:
                raise
        _merge_cookies_from_response(resp, session)
        if _SESSION_HOLDER is not None:
            _SESSION_HOLDER["curl"] = cs
        return resp  # type: ignore[return-value]
    return session.request(method, url, proxies=px, **kwargs)


def _page_get(
    session: requests.Session,
    url: str,
    *,
    referer: Optional[str] = None,
    proxies: Optional[Dict[str, str]] = None,
) -> requests.Response:
    headers = dict(DEFAULT_HEADERS)
    headers.update(PAGE_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    return _request(
        session,
        "GET",
        url,
        headers=headers,
        timeout=25,
        allow_redirects=True,
        proxies=proxies,
    )


def _prime_www(session: requests.Session) -> None:
    """Load www.bol.com homepage (uses curl_cffi when installed)."""
    resp = _page_get(session, "https://www.bol.com/nl/nl/")
    _log_http(resp, "prime_www_home")
    body_len = len(getattr(resp, "text", "") or getattr(resp, "content", b"") or b"")
    if body_len < 10_000 and os.environ.get("BOL_NO_PROXY", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        fallback = os.environ.get("BOL_PROXY_FALLBACK_URL", "").strip()
        if fallback and not os.environ.get("BOL_PROXY_URL", "").strip():
            os.environ["BOL_USE_PROXY_FALLBACK"] = "1"
            print(
                f"[main] Weak www prime ({body_len} chars) — retrying via monitor proxy"
            )
            resp = _page_get(session, "https://www.bol.com/nl/nl/")
            _log_http(resp, "prime_www_proxy")
            os.environ.pop("BOL_USE_PROXY_FALLBACK", None)
            body_len = len(
                getattr(resp, "text", "") or getattr(resp, "content", b"") or b""
            )
    if session.cookies.get("_abck"):
        print("[main] Akamai _abck cookie acquired")
    elif resp.status_code == 200:
        print("[main] www.bol.com homepage OK")
    elif _is_blocked_status(resp.status_code):
        print(f"[main] www.bol.com returned {resp.status_code} (may still work via curl)")
    if body_len < 10_000:
        print(
            f"[warn] www prime still small ({body_len} chars) — "
            "refresh login.txt from Chrome on the same IP as cart."
        )


def _warm_session(session: requests.Session) -> None:
    """Visit homepage so Akamai sets _abck / bm_* cookies before API calls."""
    if os.environ.get("BOL_SKIP_WARM", "").strip().lower() in {"1", "true", "yes"}:
        return
    if session.cookies.get("_abck"):
        return
    print("[main] Warming session (homepage)...")
    resp = _page_get(_current_session(), "https://www.bol.com/nl/nl/")
    _log_http(resp, "warm_homepage")
    if _is_blocked_status(resp.status_code):
        print(
            f"[main] Homepage returned {resp.status_code} — "
            "skipping warm-up (IP/Akamai limit). Will still try GraphQL."
        )
        return
    if _current_session().cookies.get("_abck"):
        print("[main] Akamai cookies acquired.")
    elif not _CURL_AVAILABLE:
        print("[main] Warning: install curl_cffi for Akamai bypass: pip install curl_cffi")


def _wait_rate_limit(status: int, attempt: int) -> bool:
    if not _is_blocked_status(status):
        return False
    delay = min(60, 5 * (2 ** attempt))
    print(f"[warn] bol.com returned {status} — waiting {delay}s before retry...")
    time.sleep(delay)
    return True


def _extract_offer_uid_from_text(text: str) -> Optional[str]:
    m = OFFER_UID_RE.search(text)
    if m:
        return m.group(1)
    for pat in (
        r'"defaultOfferUid"\s*:\s*"([^"]+)"',
        r'"selectedOfferUid"\s*:\s*"([^"]+)"',
        r'offerUid\\":\\"([0-9a-f-]{36})',
    ):
        m2 = re.search(pat, text, re.I)
        if m2:
            return m2.group(1)
    return None


def _parse_next_data_offer(body: str, product_id: Optional[str] = None) -> Optional[str]:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        body,
        re.I,
    )
    if not m:
        return None
    try:
        blob = m.group(1)
        if product_id:
            for pat in (
                rf'"bestSellingOffer"\s*:\s*\{{[^{{}}]*"offerUid"\s*:\s*"([0-9a-f-]{{36}})"[^{{}}]*\}}[^{{}}]*"id"\s*:\s*"{product_id}"',
                rf'"id"\s*:\s*"{product_id}"',
            ):
                m2 = re.search(pat, blob, re.I | re.S)
                if m2 and m2.lastindex:
                    return m2.group(1)
                if m2 and not m2.lastindex:
                    idx = m2.start()
                    chunk = blob[max(0, idx - 3000) : idx]
                    m3 = re.search(
                        r'"offerUid"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
                        chunk,
                        re.I,
                    )
                    if m3:
                        return m3.group(1)
        data = json.loads(blob)
        return _extract_offer_uid_from_text(json.dumps(data))
    except Exception:
        return None


_MAX_QTY_PATTERNS = (
    r'"maxQuantity"\s*:\s*(\d+)',
    r'maxQuantity\\":(\d+)',
    r'"maximumOrderQuantity"\s*:\s*(\d+)',
    r'"maxOrderQuantity"\s*:\s*(\d+)',
    r'maximaal\s+(\d+)\s+(?:stuks?|keer|per\s+bestelling)',
    r'Je kunt maximaal\s+(\d+)',
    r'input[^>]{0,120}name=["\']quantity["\'][^>]{0,120}max=["\'](\d+)',
    r'data-test=["\']quantity["\'][^>]{0,80}max=["\'](\d+)',
)


def _max_qty_candidates(html: str) -> list[int]:
    found: list[int] = []
    for pat in _MAX_QTY_PATTERNS:
        for m in re.finditer(pat, html, re.I):
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 999:
                found.append(n)
    return found


def parse_max_order_quantity(
    html: str,
    *,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
) -> Optional[int]:
    """
    Parse bol.com max order quantity from PDP HTML / dehydrated JSON.
    Prefers values near the active offerUid, then product id.
    """
    if not html or len(html) < 2000:
        return None

    def _best_in_chunk(chunk: str) -> Optional[int]:
        vals = _max_qty_candidates(chunk)
        if not vals:
            return None
        # Order limit on PDP is usually the smallest explicit maxQuantity.
        mq = [v for v in vals if v <= 50]
        return min(mq) if mq else min(vals)

    if offer_uid:
        idx = html.find(offer_uid)
        if idx >= 0:
            hit = _best_in_chunk(html[max(0, idx - 6000) : idx + 6000])
            if hit:
                return hit

    if product_id:
        idx = html.find(product_id)
        if idx >= 0:
            hit = _best_in_chunk(html[max(0, idx - 12000) : idx + 12000])
            if hit:
                return hit

    vals = _max_qty_candidates(html)
    if not vals:
        return None
    bounded = [v for v in vals if v <= 50]
    return min(bounded) if bounded else min(vals)


def parse_revision_id(
    html: str,
    *,
    offer_uid: Optional[str] = None,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """revisionId from PDP dehydrated JSON (required by some AddItem calls)."""
    if not html:
        return None
    for anchor in (offer_uid, product_id):
        if not anchor:
            continue
        idx = html.find(str(anchor))
        if idx < 0:
            continue
        chunk = html[max(0, idx - 4000) : idx + 4000]
        for pat in (
            r'revisionId\\":\\"([0-9a-f-]{36})\\"',
            r'"revisionId"\s*:\s*"([0-9a-f-]{36})"',
            r'revisionId[^a-zA-Z]{0,20}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
        ):
            m = re.search(pat, chunk, re.I)
            if m:
                return m.group(1).lower()
    for pat in (
        r'"revisionId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        r'revisionId[^a-zA-Z]{0,20}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).lower()
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def bol_cart_limits() -> Tuple[int, int]:
    """(max units per line item, max distinct items per checkout)."""
    return (
        _env_int("BOL_MAX_UNITS_PER_ITEM", 2),
        _env_int("BOL_MAX_ITEMS_PER_CHECKOUT", 4),
    )


def parse_basket_product_ids(html: str) -> set[str]:
    """Distinct bol product ids visible on the basket page."""
    if not html:
        return set()
    pid = r"\d{10,}"
    ids = set(re.findall(rf'"productId"\s*:\s*"({pid})"', html))
    ids.update(re.findall(rf'\\"productId\\"\s*,\s*\\"({pid})\\"', html))
    ids.update(
        re.findall(
            rf'"product"\s*,\s*\{{[^{{}}]*"id"\s*:\s*"({pid})"',
            html,
            re.I | re.S,
        )
    )
    return ids


def resolve_atc_quantity(
    requested: int,
    html: Optional[str],
    *,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity_cap: int = 0,
) -> int:
    """
    Resolve ATC quantity. requested <= 0 means use max from product page.
    quantity_cap > 0 limits the result (metadata / task cap).
    """
    max_q = parse_max_order_quantity(
        html or "", product_id=product_id, offer_uid=offer_uid
    )
    if requested <= 0:
        qty = max_q or 1
    elif max_q:
        qty = min(requested, max_q)
    else:
        qty = max(1, requested)
    if quantity_cap > 0:
        qty = min(qty, quantity_cap)
    return max(1, qty)


def _add_item_problem_detail(result: Dict[str, Any]) -> str:
    basket_data = (
        result.get("basket", {}).get("addItem")
        or result.get("addItem")
        or {}
    )
    if not isinstance(basket_data, dict):
        return ""
    parts = []
    for key in ("description", "message", "code", "errorCode"):
        val = basket_data.get(key)
        if val:
            parts.append(f"{key}={val}")
    return " | ".join(parts)


def _basket_contains_product_live(
    session: requests.Session,
    product_id: str,
    offer_uid: Optional[str] = None,
) -> bool:
    try:
        br = _page_get(
            session,
            "https://www.bol.com/nl/nl/basket/",
            referer="https://www.bol.com/nl/nl/",
        )
        if br.status_code != 200:
            return False
        text = br.text or ""
        if product_id in parse_basket_product_ids(text):
            return True
        if offer_uid and offer_uid.lower() in text.lower():
            # offerUid in basket line items (not generic page chrome)
            idx = text.lower().find(offer_uid.lower())
            if idx >= 0:
                chunk = text[max(0, idx - 400) : idx + 400].lower()
                if "basket" in chunk or "winkelwagen" in chunk or "productid" in chunk:
                    return True
        return False
    except Exception:
        return False


def _gql_find_basket_with_product(
    data: Dict[str, Any],
    product_id: str,
) -> Optional[str]:
    """Return basket id from Basket GraphQL when product_id is already a line item."""
    me = data.get("me") or {}
    baskets = me.get("baskets")
    if not isinstance(baskets, list):
        single = data.get("basket")
        baskets = [single] if isinstance(single, dict) else []

    for basket in baskets:
        if not isinstance(basket, dict):
            continue
        bid = str(basket.get("id") or basket.get("basketId") or "").strip()
        for item in basket.get("items") or []:
            if not isinstance(item, dict):
                continue
            candidates = [
                item.get("productId"),
                (item.get("product") or {}).get("id"),
                ((item.get("sellingOffer") or {}).get("product") or {}).get("id"),
            ]
            if any(str(c or "") == product_id for c in candidates):
                return bid or None
    return None


def _find_basket_containing_product(
    session: requests.Session,
    product_id: str,
    page_id: str,
    *,
    offer_uid: Optional[str] = None,
) -> Optional[str]:
    """Locate the account basket that already contains product_id."""
    if _basket_contains_product_live(session, product_id, offer_uid):
        for op, h in (
            ("BasketQueryWithoutTextResources", HASH_BASKET_QUERY),
            ("Basket", HASH_BASKET),
        ):
            try:
                data = _graphql(
                    session,
                    op,
                    h,
                    variables={},
                    page_id=page_id,
                    label="find_basket_product",
                    referer="https://www.bol.com/nl/nl/basket/",
                    client_app="basket-web-fe",
                )
                bid = _gql_find_basket_with_product(data, product_id)
                if bid:
                    return bid
                bid = _extract_basket_id_from_me(data)
                if bid:
                    return bid
            except Exception:
                continue
        saved = _load_saved_basket_id()
        if saved:
            return saved

    for op, h in (
        ("BasketQueryWithoutTextResources", HASH_BASKET_QUERY),
        ("Basket", HASH_BASKET),
    ):
        try:
            data = _graphql(
                session,
                op,
                h,
                variables={},
                page_id=page_id,
                label="find_basket_product",
                referer="https://www.bol.com/nl/nl/basket/",
                client_app="basket-web-fe",
            )
            bid = _gql_find_basket_with_product(data, product_id)
            if bid:
                return bid
        except Exception:
            continue
    return None


def _atc_proceed_on_cart_problem() -> bool:
    """When AddItem fails but the product may already be in cart, continue to checkout."""
    if os.environ.get("BOL_ATC_STRICT", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return os.environ.get("BOL_ATC_PROCEED_ON_CART_FAIL", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _finish_atc_already_in_cart(
    product_id: str,
    *,
    basket_id: Optional[str] = None,
    reason: str = "already in cart",
) -> None:
    """Print success and persist basket id so checkout can run."""
    bid = (basket_id or _load_saved_basket_id() or "").strip()
    if bid:
        _save_basket_id(bid)
    print(f"\n[ok] Product {product_id} {reason} — proceeding to checkout")
    if bid:
        print(f"  basket_id={bid}")
    print("  https://www.bol.com/nl/nl/basket/")


def _add_item_ok(result: Dict[str, Any]) -> bool:
    basket_data = (
        result.get("basket", {}).get("addItem")
        or result.get("addItem")
        or {}
    )
    if not isinstance(basket_data, dict):
        return False
    typename = str(basket_data.get("__typename") or "")
    if "Failed" in typename or "Problem" in typename:
        if typename == "ItemIsAlreadyInBasketProblem":
            return True
        return False
    items = basket_data.get("items")
    if isinstance(items, list) and items:
        return True
    return typename == "" or typename.endswith("Basket")


def add_to_cart_with_quantity(
    session: requests.Session,
    product_id: str,
    offer_uid: str,
    basket_id: str,
    quantity: int,
    *,
    referer: str = "https://www.bol.com/",
    page_html: Optional[str] = None,
    revision_id: Optional[str] = None,
    page_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], int]:
    """Add a single item to cart (quantity defaults to 1)."""
    target = max(1, int(quantity or 1))

    rev = (
        revision_id
        or os.environ.get("BOL_REVISION_ID", "").strip()
        or parse_revision_id(
            page_html or "", offer_uid=offer_uid, product_id=product_id
        )
    )
    # Browser AddItem usually omits revisionId; stale values → "Error(s) redacted".
    use_revision = os.environ.get("BOL_ATC_USE_REVISION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    revision_attempts: list[Optional[str]] = [None]
    if rev and use_revision:
        revision_attempts.append(rev)
        print(f"[cart] revisionId={rev[:8]}… (BOL_ATC_USE_REVISION=1)")

    fallbacks = [target]
    if target > 1:
        fallbacks.append(1)

    strategies: list[tuple[str, Optional[str]]] = [("with_basket", basket_id)]
    if os.environ.get("BOL_ATC_TRY_NO_BASKET", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        strategies.append(("no_basket", None))

    gql_page_id = (
        page_id
        or os.environ.get("BOL_PAGE_ID", "").strip()
        or None
    )

    last: Dict[str, Any] = {}
    for rev_attempt in revision_attempts:
        rev_label = "no_rev" if not rev_attempt else f"rev={rev_attempt[:8]}"
        for strat_label, bid in strategies:
            for qty in fallbacks:
                try:
                    last = add_to_cart(
                        session,
                        product_id,
                        offer_uid,
                        bid or "",
                        qty,
                        referer=referer,
                        revision_id=rev_attempt,
                        omit_basket=not bid,
                        page_id=gql_page_id,
                    )
                except RuntimeError as exc:
                    err = str(exc)
                    print(
                        f"[cart] AddItem ({strat_label}/{rev_label}) qty={qty} "
                        f"request error: {err[:200]}"
                    )
                    last = {}
                    continue
                if _add_item_ok(last):
                    if qty != target:
                        print(
                            f"[cart] ATC OK ({strat_label}/{rev_label}) qty={qty} "
                            f"(target was {target})"
                        )
                    else:
                        print(f"[cart] ATC OK ({strat_label}/{rev_label}) qty={qty}")
                    return last, qty
                typename = str(
                    (
                        last.get("basket", {}).get("addItem")
                        or last.get("addItem")
                        or {}
                    ).get("__typename")
                    or ""
                )
                detail = _add_item_problem_detail(last)
                extra = f" — {detail}" if detail else ""
                print(
                    f"[cart] AddItem ({strat_label}/{rev_label}) qty={qty} "
                    f"rejected ({typename or 'unknown'}){extra}"
                )
                if typename in (
                    "FailedToAddItemToBasketProblem",
                    "ItemIsAlreadyInBasketProblem",
                ):
                    if _basket_contains_product_live(session, product_id, offer_uid):
                        print("[cart] product found in basket page — treating as ATC OK")
                        return last, qty
                    found_bid = _find_basket_containing_product(
                        session,
                        product_id,
                        gql_page_id or str(uuid.uuid4()),
                        offer_uid=offer_uid,
                    )
                    if found_bid:
                        _save_basket_id(found_bid)
                        print(
                            f"[cart] product in account basket {found_bid} — treating as ATC OK"
                        )
                        return last, qty

    allow_home_ip = os.environ.get("BOL_ATC_ALLOW_HOME_IP", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if (
        _SESSION_HOLDER
        and not _SESSION_HOLDER.get("atc_tried_direct")
        and allow_home_ip
        and os.environ.get("BOL_PROXY_URL", "").strip()
        and os.environ.get("BOL_NO_PROXY", "").strip().lower()
        not in {"1", "true", "yes"}
    ):
        typename = str(
            (
                last.get("basket", {}).get("addItem")
                or last.get("addItem")
                or {}
            ).get("__typename")
            or ""
        )
        if typename == "FailedToAddItemToBasketProblem":
            print("[cart] AddItem failed on proxy — retrying once on home IP")
            _SESSION_HOLDER["atc_tried_direct"] = True
            _SESSION_HOLDER["force_direct_gql"] = True
            _SESSION_HOLDER["curl"] = None
            try:
                last = add_to_cart(
                    session,
                    product_id,
                    offer_uid,
                    basket_id,
                    target,
                    referer=referer,
                    revision_id=None,
                    omit_basket=False,
                    page_id=gql_page_id,
                )
                if _add_item_ok(last):
                    print(f"[cart] ATC OK (home IP fallback) qty={target}")
                    return last, target
                if _basket_contains_product_live(session, product_id, offer_uid):
                    print("[cart] product in basket after home IP retry — ATC OK")
                    return last, target
            except RuntimeError as exc:
                print(f"[cart] home IP AddItem retry failed: {str(exc)[:200]}")

    return last, target


def _graphql(
    session: requests.Session,
    operation_name: str,
    sha256_hash: str,
    variables: Dict[str, Any],
    *,
    page_id: Optional[str] = None,
    label: str = "graphql",
    referer: str = "https://www.bol.com/",
    client_app: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a persisted GraphQL query and return the parsed response."""
    if page_id is None:
        page_id = str(uuid.uuid4())

    headers = _gql_headers(referer, client_app=client_app)
    headers["bol-app-operation-name"] = operation_name
    headers["bol-client-page-id"] = page_id
    headers["m2-page-id"] = page_id

    xsrf = get_cookie_value(session, "XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    body = {
        "operationName": operation_name,
        "variables": variables,
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": sha256_hash,
            }
        },
    }

    last_exc: Optional[Exception] = None
    used_proxy = bool(_proxy_dict())
    tried_direct = False
    tried_proxy_fallback = False
    for attempt in range(4):
        if attempt:
            time.sleep(1.5)
        active = _current_session() if _SESSION_HOLDER else session
        xsrf = get_cookie_value(active, "XSRF-TOKEN")
        if xsrf:
            headers["x-xsrf-token"] = xsrf

        force_direct = tried_direct and used_proxy
        resp = _request(
            active,
            "POST",
            GRAPHQL_URL,
            json=body,
            headers=headers,
            timeout=30,
            allow_redirects=False,
            force_no_proxy=force_direct,
        )
        _log_http(resp, label)

        if is_graphql_stub_response(resp):
            if used_proxy and not tried_direct:
                tried_direct = True
                if _SESSION_HOLDER is not None:
                    _SESSION_HOLDER["force_direct_gql"] = True
                print(
                    f"[{label}] GraphQL stub via proxy "
                    f"({len(resp.content)} bytes) — retrying on home IP"
                )
                continue
            last_exc = RuntimeError(
                f"[{label}] GraphQL stub/block ({len(resp.content)} bytes, "
                f"{resp.headers.get('Content-Type', 'no-ct')})"
            )
            if _is_blocked_status(resp.status_code) and attempt < 3:
                if _wait_rate_limit(resp.status_code, attempt):
                    continue
            if attempt < 3:
                time.sleep(1.0 + attempt)
                continue
            raise last_exc

        if _should_relogin(resp.status_code) and _recreate_session_after_block(
            resp.status_code
        ):
            continue
        if _is_blocked_status(resp.status_code) and attempt < 3:
            if _wait_rate_limit(resp.status_code, attempt):
                continue

        try:
            data = resp.json()
        except Exception as exc:
            last_exc = RuntimeError(
                f"[{label}] bol.com blocked ({resp.status_code})"
            )
            if _should_relogin(resp.status_code) and _recreate_session_after_block(
                resp.status_code
            ):
                continue
            if _is_blocked_status(resp.status_code) and attempt < 3:
                if _wait_rate_limit(resp.status_code, attempt):
                    continue
                continue
            if not _is_blocked_status(resp.status_code):
                raise last_exc from exc
            continue

        if "errors" in data and isinstance(data.get("errors"), list):
            err_text = str(data["errors"]).lower()
            fallback = os.environ.get("BOL_PROXY_FALLBACK_URL", "").strip()
            if (
                fallback
                and not tried_proxy_fallback
                and os.environ.get("BOL_NO_PROXY", "").strip().lower()
                in {"1", "true", "yes"}
                and any(
                    x in err_text
                    for x in ("redacted", "blocked", "forbidden", "unauthorized")
                )
            ):
                tried_proxy_fallback = True
                os.environ["BOL_USE_PROXY_FALLBACK"] = "1"
                if _SESSION_HOLDER is not None:
                    _SESSION_HOLDER.pop("force_direct_gql", None)
                print(
                    f"[{label}] GraphQL blocked on home IP — retrying via monitor proxy"
                )
                continue
            raise RuntimeError(f"[{label}] GraphQL errors: {data['errors']}")

        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or data.get("code") or data)[:300]
            raise RuntimeError(f"[{label}] HTTP {resp.status_code}: {msg or resp.reason}")

        payload = data.get("data")
        if payload is None:
            raise RuntimeError(f"[{label}] GraphQL response missing data: {data!r:.300}")

        return payload

    relogin_note = ""
    if _SESSION_HOLDER and _SESSION_HOLDER.get("relogin"):
        relogin_note = " A fresh login was already attempted this run."
    curl_hint = (
        "pip install curl_cffi  (then re-run bol_cart.py)"
        if not _CURL_AVAILABLE
        else f"using curl_cffi ({_CURL_IMPERSONATE}) but still blocked"
    )
    raise RuntimeError(
        f"[{label}] bol.com blocked add-to-cart (403/429).{relogin_note}\n"
        "Next steps:\n"
        "  • Wait 15–30 minutes without running scripts (Akamai cooldown)\n"
        "  • pip install curl_cffi — browser TLS fingerprint for www.bol.com\n"
        "  • Add roundproxies to bol_credentials.json (residential NL)\n"
        f"  • HTTP backend: {curl_hint}\n"
        "  • Run: $env:BOL_SKIP_WARM='1'; python main.py --bol-cart <productId>"
    )


def _load_saved_basket_id() -> Optional[str]:
    for path in (TOKEN_FILE, CREDENTIAL_FILE):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            bid = (data.get("basket_id") or data.get("basketId") or "").strip()
            if bid:
                return bid
        except Exception:
            continue
    return None


def _save_basket_id(basket_id: str) -> None:
    if not os.path.exists(TOKEN_FILE):
        return
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data["basket_id"] = basket_id
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _clear_saved_basket_id() -> None:
    """Drop stale basket id so the next run does not reuse a post-order cart state."""
    cleared = False
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if "basket_id" in data:
                data.pop("basket_id", None)
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                cleared = True
        except Exception:
            pass
    if os.path.exists(CREDENTIAL_FILE):
        try:
            with open(CREDENTIAL_FILE, encoding="utf-8") as f:
                creds = json.load(f)
            had_basket = "basket_id" in creds or "basketId" in creds
            creds.pop("basket_id", None)
            creds.pop("basketId", None)
            if had_basket:
                with open(CREDENTIAL_FILE, "w", encoding="utf-8") as f:
                    json.dump(creds, f, indent=2)
                cleared = True
        except Exception:
            pass
    if cleared:
        print("[basket] cleared saved basket_id")


def _refresh_offer_uid_from_live_pdp(
    session: requests.Session,
    product_id: str,
    product_page: str,
    page_id: str,
) -> Optional[str]:
    """Re-fetch offerUid from a live product page (fixes stale env/HTML offer after checkout)."""
    try:
        resp = _page_get(
            session,
            product_page,
            referer="https://www.bol.com/nl/nl/",
        )
        _log_http(resp, "refresh_offer_pdp")
        if resp.status_code != 200 or len(resp.text or "") < 5_000:
            return None
        html = resp.text
        referer = resp.url or product_page
        uid = _offer_uid_from_product_gql(
            session, product_id, page_id, referer=referer
        )
        if not uid:
            uid = _extract_best_selling_offer_uid(html, product_id)
        if not uid:
            uid = _extract_offer_uid_for_product(html, product_id)
        if not uid:
            uid = get_offer_uid(
                session,
                product_id,
                page_id,
                product_page_url=product_page,
                page_html=html,
            )
        if uid:
            print(f"[offer] refreshed offerUid from live PDP={uid}")
        return uid
    except Exception as exc:
        print(f"[offer] live PDP refresh failed: {exc}")
        return None


def _require_akamai_for_cart(session: requests.Session) -> None:
    if get_cookie_value(session, "_abck"):
        return
    print(
        "[warn] Akamai _abck cookie missing (product pages return ~2KB challenge HTML).\n"
        "  Cart GraphQL may fail. Wait and retry, use residential proxy, or browse bol.com\n"
        "  in Chrome then refresh bol_token.json cookies from DevTools."
    )


def verify_offer_uid(
    session: requests.Session,
    offer_uid: str,
    *,
    referer: str,
    page_id: str,
) -> bool:
    """Offer query expects offerUid (not productId)."""
    try:
        data = _graphql(
            session,
            "Offer",
            HASH_OFFER,
            variables={"offerUid": offer_uid},
            page_id=page_id,
            label="verify_offer",
            referer=referer,
        )
        offer = data.get("sellingOffer") or data.get("offer")
        if isinstance(offer, dict) and offer.get("__typename") == "SellingOffer":
            print("[offer] offer_uid verified (SellingOffer)")
            return True
        if offer:
            print(f"[offer] offer_uid verified ({offer.get('__typename', 'ok')})")
            return True
        print("[offer] Offer query returned no sellingOffer for this offer_uid")
        return False
    except Exception as exc:
        print(f"[offer] verify failed ({exc})")
        return False


def _basket_contains_product(html: str, product_id: str) -> bool:
    """True when product id is listed in basket line items (not recommendations)."""
    return product_id in parse_basket_product_ids(html)


_BOL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _is_bol_uuid(value: Optional[str]) -> bool:
    return bool(value and _BOL_UUID_RE.fullmatch(value.strip()))


def _extract_dehydrated(html: str, key: str) -> Optional[str]:
    """Extract a value from Remix dehydrated SSR state."""
    if not html:
        return None
    m = re.search(
        r'\\\"' + re.escape(key) + r'\\\"[,\s]*\\\"([^\\\"]+)\\\"',
        html,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'"' + re.escape(key) + r'"\s*[,:]\s*"([^"]+)"',
        html,
    )
    return m.group(1) if m else None


def _extract_dehydrated_basket_id(html: str) -> Optional[str]:
    """Basket id must be a UUID — ignore mis-parsed keys like basketShopCountry."""
    if not html:
        return None
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    for pat in (
        rf'\\\"Basket\\\"[,\s]*\\\"({uuid})\\\"',
        rf'"Basket"\s*[,:]\s*"({uuid})"',
        rf'\\\"Basket\\\",\\\"uuid\\\",\\\"({uuid})\\\"',
    ):
        m = re.search(pat, html, re.I)
        if m and _is_bol_uuid(m.group(1)):
            return m.group(1).lower()
    bid = _basket_id_from_page_html(html)
    return bid.lower() if bid and _is_bol_uuid(bid) else None


def _dehydrated_ctx_from_html(html: str) -> Dict[str, str]:
    ctx: Dict[str, str] = {}
    basket_id = _extract_dehydrated_basket_id(html)
    xsrf = _extract_dehydrated(html, "xsrf")
    page_id = _extract_dehydrated(html, "pageId")
    if basket_id:
        ctx["basket_id"] = basket_id
    if xsrf and _is_bol_uuid(xsrf):
        ctx["xsrf"] = xsrf
    if page_id and _is_bol_uuid(page_id):
        ctx["page_id"] = page_id
    return ctx


def _apply_basket_ctx(session: requests.Session, ctx: Dict[str, str]) -> None:
    xsrf = ctx.get("xsrf")
    if xsrf:
        session.cookies.set("XSRF-TOKEN", xsrf, domain=".bol.com", path="/")


def _basket_id_from_page_html(html: str) -> Optional[str]:
    """Best-effort parse from basket/product RSC stream."""
    for pat in (
        r'"baskets"\s*,\s*\[\s*\{[^{}]*"id"\s*:\s*"([0-9a-f-]{36})"',
        r'"basket"\s*,\s*\{[^{}]*"id"\s*:\s*"([0-9a-f-]{36})"',
        r'basketId["\']?\s*:\s*["\']([0-9a-f-]{36})',
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return m.group(1)
    return None


def _extract_basket_id_from_me(data: Dict[str, Any]) -> Optional[str]:
    """Parse basket id from BasketQueryWithoutTextResources (me.baskets)."""
    me = data.get("me") or {}
    baskets = me.get("baskets")
    if isinstance(baskets, list):
        for b in baskets:
            if not isinstance(b, dict):
                continue
            bid = b.get("id") or b.get("basketId")
            if bid:
                return str(bid)
    basket = data.get("basket") or {}
    bid = basket.get("id") or basket.get("basketId")
    return str(bid) if bid else None


def _create_basket_id(
    session: requests.Session,
    page_id: str,
    *,
    referer: str = "https://www.bol.com/nl/nl/basket/",
) -> Optional[str]:
    data = _graphql(
        session,
        "CreateBasket",
        HASH_CREATE_BASKET,
        variables={},
        page_id=page_id,
        label="create_basket",
        referer=referer,
        client_app="product-web-fe",
    )
    basket_mut = data.get("basket") or {}
    create = basket_mut.get("createBasketV2") or basket_mut.get("createBasket") or {}
    if create.get("__typename") == "Basket":
        return create.get("id")
    return create.get("id")


def get_basket_id(
    session: requests.Session,
    page_id: str,
    *,
    referer: str = "https://www.bol.com/nl/nl/basket/",
    basket_page_html: Optional[str] = None,
    product_page_html: Optional[str] = None,
) -> str:
    """
    Resolve the logged-in user's bol.com basket id for AddItem.

    CreateBasket (basket-web-fe) returns the account cart id when BUI is present.
    """
    if os.environ.get("BOL_SKIP_BASKET_QUERY", "").strip() in {"1", "true", "yes"}:
        raise RuntimeError(
            "BOL_SKIP_BASKET_QUERY is set — cannot resolve a real basket id. "
            "Unset it and re-run."
        )

    env_basket = os.environ.get("BOL_BASKET_ID", "").strip()
    auto_cart = os.environ.get("BOL_AUTO_CART", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if (
        not auto_cart
        and env_basket
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            env_basket,
            re.I,
        )
    ):
        print(f"[basket] id from BOL_BASKET_ID env={env_basket}")
        return env_basket

    if has_auth_cookies(session):
        try:
            bid = _create_basket_id(session, page_id, referer=referer)
            if bid and _is_bol_uuid(bid):
                print(f"[basket] id={bid} (CreateBasket)")
                _save_basket_id(bid)
                return bid
        except Exception as exc:
            print(f"[basket] CreateBasket failed ({exc})")

    for source, html in (
        ("product page dehydrated", product_page_html),
        ("basket page dehydrated", basket_page_html),
    ):
        if not html:
            continue
        bid = _dehydrated_ctx_from_html(html).get("basket_id")
        if bid and _is_bol_uuid(bid):
            print(f"[basket] id from {source}={bid}")
            _save_basket_id(bid)
            return bid

    if basket_page_html:
        bid = _basket_id_from_page_html(basket_page_html)
        if bid:
            print(f"[basket] id from basket page HTML={bid}")
            _save_basket_id(bid)
            return bid

    for op, h in (
        ("BasketQueryWithoutTextResources", HASH_BASKET_QUERY),
        ("Basket", HASH_BASKET),
    ):
        try:
            data = _graphql(
                session,
                op,
                h,
                variables={},
                page_id=page_id,
                label="get_basket",
                referer=referer,
                client_app="basket-web-fe",
            )
            basket_id = _extract_basket_id_from_me(data)
            if basket_id and _is_bol_uuid(basket_id):
                print(f"[basket] id={basket_id} ({op})")
                _save_basket_id(basket_id)
                return basket_id
        except Exception as exc:
            print(f"[basket] {op} failed ({exc})")

    for source, bid in (
        ("bol_credentials.json", _basket_id_from_credentials()),
        ("bol_token.json", _load_saved_basket_id()),
    ):
        if bid and _is_bol_uuid(bid):
            print(f"[basket] id from {source}={bid} (fallback)")
            return bid

    if has_auth_cookies(session):
        raise RuntimeError(
            "Could not load your bol.com basket id (logged-in session).\n"
            "CreateBasket and Basket GraphQL both failed.\n\n"
            "Fix: log in via bol_login.py, import fresh cookies (login.txt), then retry."
        )

    try:
        basket_id = _create_basket_id(session, page_id, referer=referer)
        if basket_id:
            print(f"[basket] id={basket_id} (created, anonymous)")
            return basket_id
    except Exception as exc:
        print(f"[basket] CreateBasket failed ({exc})")

    raise RuntimeError(
        "Could not resolve bol.com basket id.\n"
        "Run: python main.py --bol-login"
    )


def _basket_id_from_credentials() -> Optional[str]:
    if not os.path.exists(CREDENTIAL_FILE):
        return None
    try:
        with open(CREDENTIAL_FILE, encoding="utf-8") as f:
            cred = json.load(f)
        return (cred.get("basket_id") or cred.get("basketId") or "").strip() or None
    except Exception:
        return None


def _extract_best_selling_offer_uid(
    html: str, product_id: Optional[str] = None
) -> Optional[str]:
    """offerUid from bol's bestSellingOffer block (the buyable default offer)."""
    if not html:
        return None
    best = re.search(
        r'"bestSellingOffer"\s*:\s*\{[\s\S]{0,2500}?\}',
        html,
        re.I,
    )
    if not best:
        return None
    chunk = best.group(0)
    if product_id and product_id not in chunk:
        return None
    m = re.search(
        r'"offerUid"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        chunk,
        re.I,
    )
    return m.group(1).lower() if m else None


def _offer_uid_from_product_gql(
    session: requests.Session,
    product_id: str,
    page_id: str,
    *,
    referer: str,
) -> Optional[str]:
    """Resolve offerUid via Product GraphQL (same as monitor)."""
    try:
        data = _graphql(
            session,
            "Product",
            HASH_PRODUCT,
            variables={"productId": product_id},
            page_id=page_id,
            label="product_offer",
            referer=referer,
            client_app="product-web-fe",
        )
        product = data.get("product")
        if not isinstance(product, dict):
            return None
        best = product.get("bestSellingOffer")
        if not isinstance(best, dict):
            return None
        uid = best.get("offerUid")
        if uid:
            print(f"[offer] offerUid from Product GraphQL={uid}")
        return str(uid).lower() if uid else None
    except Exception as exc:
        print(f"[offer] Product GraphQL failed ({str(exc)[:120]})")
        return None


def _extract_offer_uid_for_product(html: str, product_id: str) -> Optional[str]:
    """Resolve offerUid for a specific product from bol PDP HTML (RSC or URL params)."""
    uid = _extract_best_selling_offer_uid(html, product_id)
    if uid:
        return uid
    pid = re.escape(product_id)
    for pat in (
        rf"/{pid}/\?offerUid=([0-9a-f-]{{36}})",
        rf"productId={pid}(?:&amp;|&)offerUid=([0-9a-f-]{{36}})",
        rf"productId={pid}[^\"'<>]{{0,160}}offerUid=([0-9a-f-]{{36}})",
        rf"offerUid=([0-9a-f-]{{36}})[^\"'<>]{{0,160}}productId={pid}",
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).lower()
    idx = html.find(product_id)
    if idx >= 0:
        chunk = html[max(0, idx - 250) : idx + 250]
        m = re.search(r"offerUid=([0-9a-f-]{36})", chunk, re.I)
        if m:
            return m.group(1).lower()
    return _parse_next_data_offer(html, product_id) or _extract_offer_uid_from_text(html)


def get_offer_uid_from_page(
    session: requests.Session,
    product_page_url: str,
    page_html: Optional[str] = None,
    *,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """Parse offerUid from product HTML (__NEXT_DATA__ or embedded JSON)."""
    body = page_html
    if body is None:
        for attempt in range(3):
            resp = _page_get(session, product_page_url, referer="https://www.bol.com/nl/nl/")
            _log_http(resp, "fetch_product_page")
            if _should_relogin(resp.status_code) and _recreate_session_after_block(
                resp.status_code
            ):
                continue
            if _is_blocked_status(resp.status_code) and attempt < 2:
                if _wait_rate_limit(resp.status_code, attempt):
                    continue
            if resp.status_code == 200:
                body = resp.text
                break
        if not body:
            return None
    uid = _extract_offer_uid_for_product(body, product_id or "")
    if not uid:
        uid = _parse_next_data_offer(body, product_id) or _extract_offer_uid_from_text(body)
    if uid:
        print(f"[offer] offerUid from page HTML={uid}")
    return uid


def get_offer_uid(
    session: requests.Session,
    product_id: str,
    page_id: str,
    *,
    product_page_url: Optional[str] = None,
    page_html: Optional[str] = None,
) -> str:
    """
    Fetch the default offerUid for a product.

    Order: HTML page (__NEXT_DATA__) first, then shop API fallback.
    """
    if product_page_url:
        uid = get_offer_uid_from_page(
            session, product_page_url, page_html, product_id=product_id
        )
        if uid:
            return uid

    # Offer persisted query uses offerUid, not productId (see product-network.txt).

    raise RuntimeError(
        f"Could not determine offerUid for product {product_id}.\n"
        "Fix options:\n"
        '  1) Add "offer_uid": "uuid-here" to bol_credentials.json (from DevTools -> Network)\n'
        "  2) Pass as 2nd CLI arg: python bol_cart.py <productId> <offerUid>\n"
        "  3) Configure roundproxies in bol_credentials.json (429/403 = IP rate limit)\n"
        "  4) Wait 5-10 minutes and retry"
    )


def add_to_cart(
    session: requests.Session,
    product_id: str,
    offer_uid: str,
    basket_id: str,
    quantity: int = 1,
    *,
    referer: str = "https://www.bol.com/",
    revision_id: Optional[str] = None,
    omit_basket: bool = False,
    page_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call the AddItem GraphQL mutation (product-web-fe persisted query).
    """
    gql_page_id = page_id or str(uuid.uuid4())
    inp: Dict[str, Any] = {
        "basketId": basket_id,
        "offerUid": offer_uid,
        "productId": product_id,
        "quantity": quantity,
    }
    if omit_basket:
        inp.pop("basketId", None)
    if revision_id:
        inp["revisionId"] = revision_id
    variables = {"input": inp}
    data = _graphql(
        session,
        "AddItem",
        HASH_ADD_ITEM,
        variables=variables,
        page_id=gql_page_id,
        label="add_to_cart",
        referer=referer,
        client_app="product-web-fe",
    )
    return data


def _product_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/(\d{10,})/?", url)
    return m.group(1) if m else None


def _load_defaults_from_credentials() -> Tuple[Optional[str], Optional[str], int, str]:
    if not os.path.exists(CREDENTIAL_FILE):
        return None, None, 1, ""
    try:
        with open(CREDENTIAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        url = data.get("product_url", "")
        pid = _product_id_from_url(url) if url else None
        qty = int(data.get("quantity", 1))
        offer = (data.get("offer_uid") or data.get("offerUid") or "").strip() or None
        return pid, offer, qty, url
    except Exception:
        return None, None, 1, ""


def _looks_like_offer_uid(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value.strip(),
            re.I,
        )
    )


def _parse_cli_args(args: list[str]) -> tuple[str, Optional[str], int]:
    product_id = args[0]
    offer_uid_arg: Optional[str] = None
    quantity = 1
    if len(args) > 1:
        if _looks_like_offer_uid(args[1]):
            offer_uid_arg = args[1].strip()
            quantity = int(args[2]) if len(args) > 2 else 1
        else:
            quantity = int(args[1])
    qty_env = os.environ.get("BOL_QUANTITY", "").strip()
    if qty_env:
        quantity = int(qty_env)
    return product_id, offer_uid_arg, quantity


def main(argv: list[str] | None = None) -> None:
    os.environ.pop("BOL_USE_PROXY_FALLBACK", None)
    args = argv if argv is not None else sys.argv[1:]
    product_url_cred = ""
    if args:
        product_id, offer_uid_arg, quantity = _parse_cli_args(args)
        auto_cart = os.environ.get("BOL_AUTO_CART", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if not auto_cart:
            env_offer = os.environ.get("BOL_OFFER_UID", "").strip()
            if env_offer and _looks_like_offer_uid(env_offer):
                offer_uid_arg = env_offer
            cred_pid, cred_offer, _, product_url_cred = _load_defaults_from_credentials()
            if not offer_uid_arg and cred_offer and cred_pid == product_id:
                offer_uid_arg = cred_offer
        else:
            cred_pid, cred_offer, _, product_url_cred = _load_defaults_from_credentials()
            env_offer = os.environ.get("BOL_OFFER_UID", "").strip()
            if env_offer and _looks_like_offer_uid(env_offer):
                offer_uid_arg = env_offer
            elif cred_offer and (not cred_pid or cred_pid == product_id):
                offer_uid_arg = cred_offer
            qty_env = os.environ.get("BOL_QUANTITY", "").strip()
            if qty_env:
                quantity = int(qty_env)
    else:
        product_id, offer_uid_arg, quantity, product_url_cred = _load_defaults_from_credentials()
        if not product_id:
            print("Usage: python bol_cart.py <productId> [offerUid] [quantity]")
            print("Or set product_url + quantity in bol_credentials.json")
            print("Example:")
            print("  python bol_cart.py 9300000271683065")
            sys.exit(1)
        print(f"[main] Using bol_credentials.json -> product_id={product_id}, quantity={quantity}")
        if offer_uid_arg:
            print(f"[main] offer_uid from credentials: {offer_uid_arg}")

    print(
        f"[main] product_id={product_id}, "
        f"offer_uid={offer_uid_arg!r}, quantity={quantity}"
    )

    # Step 1: Ensure logged-in session
    print("[main] Loading session...")
    session = ensure_session()
    dedupe_cookies(session)
    session.headers.update(DEFAULT_HEADERS)
    _init_session_holder(session)
    if _CURL_AVAILABLE:
        print(f"[main] Using curl_cffi TLS impersonation ({_CURL_IMPERSONATE})")
    else:
        print("[main] curl_cffi not installed — pip install curl_cffi (recommended)")
    _prime_www(session)
    print("[main] Session ready.")
    _warm_session(session)
    session = _current_session()

    product_page = os.environ.get("BOL_PRODUCT_URL") or product_url_cred or (
        f"https://www.bol.com/nl/nl/p/x/{product_id}/"
    )
    gql_referer = product_page

    page_html: Optional[str] = None
    html_file = os.environ.get("BOL_PRODUCT_HTML_FILE", "").strip()
    if html_file and os.path.isfile(html_file):
        try:
            cached = Path(html_file).read_text(encoding="utf-8", errors="replace")
            if len(cached) > 5000:
                page_html = cached
                print(f"[main] Using cached product HTML ({len(cached)} chars)")
        except OSError as exc:
            print(f"[warn] could not read BOL_PRODUCT_HTML_FILE: {exc}")

    auto_cart = os.environ.get("BOL_AUTO_CART", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    skip_product_page = os.environ.get("BOL_SKIP_PRODUCT_PAGE", "").strip() in {
        "1",
        "true",
        "yes",
    }
    if not skip_product_page and (not page_html or auto_cart):
        for attempt in range(3):
            try:
                prime = _page_get(
                    session, product_page, referer="https://www.bol.com/nl/nl/"
                )
                _log_http(prime, "prime_product_page")
                if prime.status_code == 200:
                    page_html = prime.text
                    gql_referer = prime.url
                    break
                if _should_relogin(prime.status_code) and _recreate_session_after_block(
                    prime.status_code
                ):
                    session = _current_session()
                    continue
                if _is_blocked_status(prime.status_code) and attempt < 2:
                    if _wait_rate_limit(prime.status_code, attempt):
                        continue
                print(f"[warn] product page returned {prime.status_code}")
            except Exception as exc:
                print(f"[warn] product page prime failed: {exc}")
            break
        time.sleep(2)

    if offer_uid_arg and not page_html:
        print("[main] Priming product page (fresh offerUid / session)...")
        try:
            prime = _page_get(
                session, product_page, referer="https://www.bol.com/nl/nl/"
            )
            _log_http(prime, "prime_product_page")
            if prime.status_code == 200 and len(prime.text) > 5000:
                page_html = prime.text
                gql_referer = prime.url
        except Exception as exc:
            print(f"[warn] product page prime failed: {exc}")

    basket_ctx: Dict[str, str] = {}
    if page_html:
        basket_ctx.update(_dehydrated_ctx_from_html(page_html))

    page_id = basket_ctx.get("page_id") or str(uuid.uuid4())
    if basket_ctx.get("page_id"):
        print(f"[main] pageId from dehydrated SSR={page_id}")
    if basket_ctx.get("xsrf"):
        _apply_basket_ctx(session, basket_ctx)
        print(f"[main] xsrf from dehydrated SSR={basket_ctx['xsrf'][:8]}…")

    offer_uid: Optional[str] = None
    offer_uid = _offer_uid_from_product_gql(
        session, product_id, page_id, referer=gql_referer
    )
    if not offer_uid and page_html:
        offer_uid = _extract_best_selling_offer_uid(page_html, product_id)
        if offer_uid:
            print(f"[offer] offerUid from bestSellingOffer HTML={offer_uid}")
    if not offer_uid and page_html:
        offer_uid = _extract_offer_uid_for_product(page_html, product_id)
        if offer_uid:
            print(f"[offer] offerUid from product page={offer_uid}")
    if not offer_uid and offer_uid_arg and _looks_like_offer_uid(offer_uid_arg):
        offer_uid = offer_uid_arg
        print(f"[offer] offerUid from CLI/env fallback={offer_uid}")
    if not offer_uid:
        print(f"[main] Resolving offerUid from product page for {product_id}...")
        offer_uid = get_offer_uid(
            session,
            product_id,
            page_id,
            product_page_url=product_page,
            page_html=page_html,
        )

    _require_akamai_for_cart(session)

    basket_page_html: Optional[str] = None
    try:
        br = _page_get(
            session,
            "https://www.bol.com/nl/nl/basket/",
            referer="https://www.bol.com/nl/nl/",
        )
        _log_http(br, "prime_basket_page")
        print(f"[main] basket page size={len(br.text)} chars")
        if br.status_code == 200:
            basket_page_html = br.text
            for key, value in _dehydrated_ctx_from_html(basket_page_html).items():
                if value and not basket_ctx.get(key):
                    basket_ctx[key] = value
            if basket_ctx.get("page_id") and basket_ctx["page_id"] != page_id:
                page_id = basket_ctx["page_id"]
                print(f"[main] pageId from basket page={page_id}")
            if basket_ctx.get("xsrf"):
                _apply_basket_ctx(session, basket_ctx)
    except Exception as exc:
        print(f"[warn] basket page prime failed: {exc}")

    os.environ["BOL_PAGE_ID"] = page_id

    if offer_uid and not verify_offer_uid(
        session, offer_uid, referer=gql_referer, page_id=page_id
    ):
        refreshed = _refresh_offer_uid_from_live_pdp(
            session, product_id, product_page, page_id
        )
        if refreshed:
            offer_uid = refreshed
            if not verify_offer_uid(
                session, offer_uid, referer=gql_referer, page_id=page_id
            ):
                print(
                    "[warn] refreshed offer_uid still unverified — "
                    "AddItem may fail; check product is buyable on bol.com"
                )
        else:
            print(
                "[warn] offer_uid could not be verified — "
                "live PDP refresh failed; update offer from DevTools if AddItem fails."
            )

    # Step 2: resolve real basket ID (required for AddItem to hit your account cart)
    warmed_basket = basket_ctx.get("basket_id") or ""
    if warmed_basket and not _is_bol_uuid(warmed_basket):
        print(f"[warn] ignoring invalid warmed basket id: {warmed_basket!r}")
        warmed_basket = ""
    if warmed_basket:
        basket_id = warmed_basket
        print(f"[basket] id from warmed context={basket_id}")
        _save_basket_id(basket_id)
    else:
        basket_id = get_basket_id(
            session,
            page_id,
            referer="https://www.bol.com/nl/nl/basket/",
            basket_page_html=basket_page_html,
            product_page_html=page_html,
        )

    max_units, max_items = bol_cart_limits()
    use_max_qty = os.environ.get("BOL_USE_MAX_QUANTITY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    requested_qty = 0 if use_max_qty else int(quantity or 1)
    quantity = resolve_atc_quantity(
        requested_qty,
        page_html,
        product_id=product_id,
        offer_uid=offer_uid,
        quantity_cap=max_units,
    )
    print(
        f"[main] ATC quantity={quantity} "
        f"(bol limits: {max_units}/item, {max_items} items/checkout)"
    )

    basket_pids = parse_basket_product_ids(basket_page_html or "")
    if product_id not in basket_pids:
        existing_bid = _find_basket_containing_product(
            session,
            product_id,
            page_id,
            offer_uid=offer_uid,
        )
        if existing_bid:
            basket_id = existing_bid
            _save_basket_id(basket_id)
            _finish_atc_already_in_cart(
                product_id,
                basket_id=basket_id,
                reason="already in account basket (GraphQL)",
            )
            return

    if product_id in basket_pids:
        if basket_id:
            _save_basket_id(str(basket_id))
        _finish_atc_already_in_cart(
            product_id,
            basket_id=basket_id,
            reason=f"already in basket ({len(basket_pids)} item(s))",
        )
        return

    if product_id not in basket_pids and len(basket_pids) >= max_items:
        raise RuntimeError(
            f"Basket already has {len(basket_pids)} distinct items "
            f"(bol limit is {max_items} per checkout). "
            f"Checkout or remove items before adding product {product_id}."
        )

    print(
        f"[main] Adding to cart: product={product_id}, offer={offer_uid}, "
        f"basket={basket_id}, qty={quantity}"
    )

    session = _current_session()
    save_session(session, source="cart_pre_add")

    revision_id = os.environ.get("BOL_REVISION_ID", "").strip() or None
    if not revision_id and page_html:
        revision_id = parse_revision_id(
            page_html, offer_uid=offer_uid, product_id=product_id
        )

    result, quantity = add_to_cart_with_quantity(
        session,
        product_id,
        offer_uid,
        basket_id,
        quantity,
        referer=gql_referer,
        page_html=page_html,
        revision_id=revision_id,
        page_id=page_id,
    )

    fail_data = result.get("basket", {}).get("addItem") or result.get("addItem") or {}
    if (
        isinstance(fail_data, dict)
        and fail_data.get("__typename") == "FailedToAddItemToBasketProblem"
        and offer_uid
    ):
        refreshed = _refresh_offer_uid_from_live_pdp(
            session, product_id, product_page, page_id
        )
        if refreshed and refreshed != offer_uid:
            offer_uid = refreshed
            revision_id = parse_revision_id(
                page_html or "", offer_uid=offer_uid, product_id=product_id
            ) if page_html else revision_id
            print("[cart] retrying AddItem with refreshed offerUid...")
            result, quantity = add_to_cart_with_quantity(
                session,
                product_id,
                offer_uid,
                basket_id,
                quantity,
                referer=gql_referer,
                page_html=page_html,
                revision_id=revision_id,
                page_id=page_id,
            )

    _print_add_to_cart_result(
        result,
        product_id=product_id,
        quantity=quantity,
        basket_page_html=basket_page_html,
        session=session,
        offer_uid=offer_uid,
    )


def _print_add_to_cart_result(
    result: Dict[str, Any],
    *,
    product_id: str,
    quantity: int,
    basket_page_html: Optional[str] = None,
    session: Optional[requests.Session] = None,
    offer_uid: Optional[str] = None,
) -> None:
    """Pretty-print AddItem GraphQL data; exit with error unless basket was updated."""
    basket_data = (
        result.get("basket", {}).get("addItem")
        or result.get("addItem")
        or {}
    )
    if not basket_data:
        print("\n[error] AddItem returned empty data payload.")
        if os.environ.get("BOL_HTTP_VERBOSE", "").strip() in {"1", "true", "yes"}:
            print(json.dumps(result, indent=2))
        raise RuntimeError("AddItem GraphQL response had no basket.addItem data")

    typename = basket_data.get("__typename") or ""
    if typename == "ItemIsAlreadyInBasketProblem":
        _finish_atc_already_in_cart(
            product_id,
            basket_id=basket_data.get("id"),
            reason="already in your cart (ItemIsAlreadyInBasketProblem)",
        )
        return

    if typename and typename != "Basket":
        in_basket = False
        found_bid: Optional[str] = None
        if typename in (
            "FailedToAddItemToBasketProblem",
            "ItemIsAlreadyInBasketProblem",
        ):
            if basket_page_html and _basket_contains_product(
                basket_page_html, product_id
            ):
                in_basket = True
            elif session and _basket_contains_product_live(
                session, product_id, offer_uid
            ):
                in_basket = True
            elif session:
                found_bid = _find_basket_containing_product(
                    session,
                    product_id,
                    str(uuid.uuid4()),
                    offer_uid=offer_uid,
                )
                if found_bid:
                    in_basket = True
            if in_basket or (
                _atc_proceed_on_cart_problem()
                and typename in (
                    "FailedToAddItemToBasketProblem",
                    "ItemIsAlreadyInBasketProblem",
                )
            ):
                _finish_atc_already_in_cart(
                    product_id,
                    basket_id=found_bid or basket_data.get("id"),
                    reason=(
                        f"AddItem returned {typename}"
                        if not in_basket
                        else f"already in cart ({typename})"
                    ),
                )
                return

        desc = _add_item_problem_detail(result) or (
            basket_data.get("description") or basket_data.get("message") or ""
        )
        print(f"\n[error] AddItem failed: {typename}")
        if desc:
            print(f"  {desc}")
        if os.environ.get("BOL_HTTP_VERBOSE", "").strip() in {"1", "true", "yes"}:
            print(json.dumps(result, indent=2))
        hint = (
            "Common causes:\n"
            "  • Product already in cart (normal if you ran the script twice).\n"
            "  • Wrong basket_id — copy fresh basketId from DevTools AddItem.\n"
            "  • Stale offer_uid / session — run: python main.py --import-cookies\n"
            "  • Missing Akamai _abck — import cookies from Chrome after bol.com loads."
        )
        if typename == "FailedToAddItemToBasketProblem":
            hint = (
                "bol rejected the add (offer/basket/session mismatch, or not buyable).\n"
                + hint
            )
        raise RuntimeError(f"AddItem rejected by bol.com ({typename}).\n{hint}")

    items = basket_data.get("items") or []
    total_qty = basket_data.get("totalQuantity")
    if total_qty is None:
        total_qty = basket_data.get("quantity")
    if total_qty is None and items:
        total_qty = sum(int(i.get("quantity") or 0) for i in items)
    if total_qty is None:
        total_qty = quantity

    basket_id = basket_data.get("id", "")
    if basket_id:
        _save_basket_id(str(basket_id))
    print(f"\n[ok] Added to cart!")
    if basket_id:
        print(f"  basket_id={basket_id}")
    print(f"  total_quantity={total_qty}")

    if not items:
        print(f"  product_id={product_id} (qty={quantity})")
        return

    for item in items:
        offer = item.get("sellingOffer", {})
        prod = offer.get("product", {})
        price = (offer.get("sellingPrice") or {}).get("price") or {}
        title = prod.get("title") or prod.get("id") or product_id
        print(
            f"  - {title} | qty={item.get('quantity', quantity)} | "
            f"EUR {price.get('amount', '?')}"
        )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        msg = str(exc).encode("ascii", errors="replace").decode("ascii")
        print(f"\n[error] {msg}")
        sys.exit(1)
