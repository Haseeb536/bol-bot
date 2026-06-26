#!/usr/bin/env python3
"""
Automatic bol.com session helper.

Login requires solving Google reCAPTCHA v2 (site key 6Le4qaQsAAAAAFHTGTckpy4WkoCXpw9JJ8NgBtpk).
Set TWOCAPTCHA_API_KEY (env or bol_credentials.json) for automatic solving via 2captcha.com (~$0.001/solve).

The script:
- loads credentials from `bol_credentials.json` or environment variables
- primes the browser-like cookies used by bol.com
- solves reCAPTCHA via 2captcha (if TWOCAPTCHA_API_KEY is set)
- stores the resulting session cookies in `bol_token.json`
- reuses and refreshes the saved session whenever it is still valid

Usage:
    python bol_login.py                    # normal login (needs 2captcha key)
    BOL_ONCE=1 python bol_login.py         # login once and exit
    BOL_FORCE_REFRESH=1 python bol_login.py  # force fresh login

Browser snapshot (paste DevTools POST body into bol_login_snapshot.json):
    Copy Network → POST /wsp/api/login → Payload + Cookie header, run within ~2 min.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, urljoin

import requests

try:
    import certifi

    _ca_bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca_bundle)
except ImportError:
    certifi = None  # type: ignore[assignment]

try:
    from curl_cffi import requests as curl_requests

    _CURL_AVAILABLE = True
except ImportError:
    curl_requests = None  # type: ignore[misc, assignment]
    _CURL_AVAILABLE = False

_CURL_IMPERSONATE = os.environ.get("BOL_IMPERSONATE", "chrome131")

def _resolve_root_dir() -> str:
    try:
        from src.utils.app_root import get_app_root

        return str(get_app_root())
    except Exception:
        return os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


ROOT_DIR = _resolve_root_dir()
COOKIE_FILE = os.path.join(ROOT_DIR, "bol_token.json")
CREDENTIAL_FILE = os.path.join(ROOT_DIR, "bol_credentials.json")
LOGIN_SNAPSHOT_FILE = os.path.join(ROOT_DIR, "bol_login_snapshot.json")
HARDCODED_LOGIN_FILE = os.path.join(ROOT_DIR, "bol_hardcoded_login.json")
DEBUG_HTML_FILE = os.path.join(ROOT_DIR, "login_page_debug.html")

LOGIN_BASE_URL = "https://login.bol.com"
LOGIN_PAGE_URL = "https://login.bol.com/wsp/login"
LOGIN_API_URL = "https://login.bol.com/wsp/api/login"
ACCOUNT_INFO_URL = "https://www.bol.com/nl/account/select/top-header-info"
BOL_HOME_URL = "https://www.bol.com"

# reCAPTCHA site key embedded on login.bol.com/wsp/login (captured from browser)
RECAPTCHA_SITE_KEY = "6Le4qaQsAAAAAFHTGTckpy4WkoCXpw9JJ8NgBtpk"

# 2captcha API endpoints
TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Priority": "u=1, i",
    "Sec-Ch-Ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

REFRESH_MARGIN = 120  # seconds
TRACE_HTTP = True
BOL_FORCE_REFRESH_ENV = "BOL_FORCE_REFRESH"
BOL_ONE_SHOT_ENV = "BOL_ONCE"
BOL_HTTP_VERBOSE_ENV = "BOL_HTTP_VERBOSE"
BOL_KEEP_ALIVE_SECONDS_ENV = "BOL_KEEP_ALIVE_SECONDS"
BOL_DUMP_HTML_ENV = "BOL_DUMP_HTML"
TWOCAPTCHA_KEY_ENV = "TWOCAPTCHA_API_KEY"
BOL_CAPTCHA_TOKEN_ENV = "BOL_CAPTCHA_TOKEN"
BOL_CAPTCHA_NONCE_ENV = "BOL_CAPTCHA_NONCE"
BOL_CSRF_TOKEN_ENV = "BOL_CSRF_TOKEN"
BOL_CRVTOKEN_ENV = "BOL_CRVTOKEN"
BOL_LOGIN_COOKIES_ENV = "BOL_LOGIN_COOKIES"  # JSON object or "name=val; name2=val2"
BOL_LOGIN_SNAPSHOT_ENV = "BOL_LOGIN_SNAPSHOT"  # path to bol_login_snapshot.json


@dataclass
class LoginContext:
    # csrf_body_token: value for the _csrf body field (Spring Security form token)
    csrf_token: str = ""
    # csrf_header_token: value for the x-csrf-token request header (may differ from body token)
    # Captured: x-csrf-token header != _csrf body field on bol.com
    csrf_header: str = ""
    captcha_nonce: str = ""
    crvtoken: str = ""
    hidden_fields: Dict[str, str] = field(default_factory=dict)


# ---------------------------
# Utilities
# ---------------------------

def _load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _save_json_file(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _response_brief(resp: requests.Response) -> str:
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct.lower():
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            interesting = []
            for key in ("points", "mySelectUrl", "url", "error", "message"):
                if key in payload:
                    interesting.append(f"{key}={str(payload[key])[:60]!r}")
            if interesting:
                return f"json({', '.join(interesting)})"
            return f"json(keys={','.join(sorted(payload.keys())[:6])})"
        return "json"
    if "text/html" in ct.lower():
        title = _extract_token(resp.text or "", (r"<title>(.*?)</title>",))
        if title:
            return f"html(title={title[:48]})"
        return f"html({len(resp.text or '')} chars)"
    return f"{ct or 'body'}({len(resp.content)} bytes)"


def _log_http(resp: requests.Response, label: str = "") -> None:
    if not TRACE_HTTP:
        return
    prefix = f"[{label}] " if label else ""
    parsed = urlparse(resp.request.url)
    brief = _response_brief(resp)
    print(f"{prefix}{resp.request.method} {parsed.path} -> {resp.status_code} {resp.reason} | {brief}")
    if _env_truthy(BOL_HTTP_VERBOSE_ENV):
        body = getattr(resp.request, "body", None)
        if body:
            text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
            text = re.sub(r'(j_password["\s:]+)[^"&,\s}]+', r'\1***', text, flags=re.IGNORECASE)
            text = re.sub(r'(captcha-response-field-1["\s:]+)[^"]{30,}', r'\1***[CAPTCHA]***', text)
            print(f"  Req-Body: {text[:600]}")


def _ssl_verify() -> bool | str:
    if os.environ.get("BOL_SSL_VERIFY", "1").strip().lower() in {"0", "false", "no"}:
        return False
    if certifi is not None:
        return certifi.where()
    return True


def _request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    label: str,
    timeout: int = 20,
    **kwargs: Any,
) -> requests.Response:
    kwargs.setdefault("verify", _ssl_verify())
    if _CURL_AVAILABLE and "bol.com" in url:
        headers = dict(session.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        cookies = {c.name: c.value for c in session.cookies}
        resp = curl_requests.request(
            method,
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            impersonate=_CURL_IMPERSONATE,
            verify=kwargs.pop("verify", _ssl_verify()),
            allow_redirects=kwargs.pop("allow_redirects", True),
            **kwargs,
        )
        for name, value in resp.cookies.items():
            session.cookies.set(name, value)
        _log_http(resp, label)  # type: ignore[arg-type]
        return resp  # type: ignore[return-value]
    response = session.request(method, url, timeout=timeout, **kwargs)
    _log_http(response, label)
    return response


def _cookie_jar_from_dict(data: Dict[str, str]) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for key, value in data.items():
        jar.set(key, value)
    return jar


def dedupe_cookies(session: requests.Session) -> None:
    """
    Keep one cookie per name (www.bol.com wins over .bol.com).

    Duplicate XSRF-TOKEN entries break requests/curl cookie jars and GraphQL.
    """
    merged: Dict[str, Tuple[str, str, str]] = {}
    for cookie in list(session.cookies):
        domain = cookie.domain or ""
        score = 0
        if "www.bol.com" in domain:
            score = 3
        elif domain.startswith(".bol.com") or domain == "bol.com":
            score = 2
        elif domain:
            score = 1
        prev = merged.get(cookie.name)
        if prev is None or score >= prev[0]:
            merged[cookie.name] = (score, cookie.value, domain)

    jar = requests.cookies.RequestsCookieJar()
    for name, (_score, value, domain) in merged.items():
        jar.set(name, value, domain=domain or ".bol.com", path="/")
    session.cookies = jar


def get_cookie_value(session: requests.Session, name: str) -> Optional[str]:
    """Return a cookie value without triggering duplicate-name errors."""
    for cookie in session.cookies:
        if cookie.name == name:
            return cookie.value
    return None


def _extract_token(text: str, patterns: Tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
    return ""


def _cookie_max_expiry(session: requests.Session) -> Optional[float]:
    try:
        max_exp = None
        for cookie in session.cookies:
            expires = getattr(cookie, "expires", None)
            if expires:
                if max_exp is None or expires > max_exp:
                    max_exp = expires
        return float(max_exp) if max_exp is not None else None
    except Exception:
        return None


def _session_cookie_names(session: requests.Session) -> str:
    return ", ".join(sorted({c.name for c in session.cookies}))


def _session_cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


# ---------------------------
# 2captcha reCAPTCHA Solver
# ---------------------------

def _solve_recaptcha_2captcha(
    api_key: str,
    site_key: str,
    page_url: str,
    timeout: int = 120,
    cookies: str = "",
) -> str:
    """
    Solve Google reCAPTCHA v2 using 2captcha.com API.
    Returns the g-recaptcha-response token string.
    Raises RuntimeError on failure.
    """
    print(f"  [2captcha] Submitting reCAPTCHA task (site_key={site_key[:20]}...)...")

    # Submit task
    submit_data_fields: Dict[str, Any] = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    if cookies:
        submit_data_fields["cookies"] = cookies
    submit_resp = requests.post(
        TWOCAPTCHA_SUBMIT_URL,
        data=submit_data_fields,
        timeout=30,
    )
    submit_data = submit_resp.json()
    if submit_data.get("status") != 1:
        raise RuntimeError(f"2captcha submit failed: {submit_data}")

    task_id = submit_data["request"]
    print(f"  [2captcha] Task submitted, id={task_id}. Waiting for solution...")

    # Poll for result (up to timeout seconds)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10)
        result_resp = requests.get(
            TWOCAPTCHA_RESULT_URL,
            params={"key": api_key, "action": "get", "id": task_id, "json": 1},
            timeout=30,
        )
        result_data = result_resp.json()
        if result_data.get("status") == 1:
            token = result_data["request"]
            print(f"  [2captcha] Solved! token length={len(token)}")
            return token
        if result_data.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha error: {result_data}")
        print("  [2captcha] Not ready yet, waiting 10s...")

    raise RuntimeError(f"2captcha timed out after {timeout}s")


# ---------------------------
# Session Persistence
# ---------------------------

def save_session(session: requests.Session, source: str = "login") -> None:
    dedupe_cookies(session)
    new_cookies = requests.utils.dict_from_cookiejar(session.cookies)

    # ── Merge with existing bol_token.json to avoid wiping Akamai cookies ──
    # Problem: monitor_fetch calls save_session() on every poll cycle.
    # The curl/tls_client session never receives _abck (Akamai blocks it),
    # so a naive overwrite wipes _abck imported from login.txt.
    # Fix: read existing cookies and merge — new cookies win, but Akamai
    # cookies (_abck, ak_bmsc, bm_sv, sbsd*) are preserved if not in new set.
    AKAMAI_PROTECT = {"_abck", "ak_bmsc", "bm_sv", "bm_sz", "bm_lso", "sbsd", "sbsd_o", "sbsd_c"}
    AUTH_PROTECT = {
        "BUI",
        "shopping_session_id",
        "DYN_USER_ID",
        "DYN_USER_CONFIRM",
        "bltgSessionId",
        "XSRF-TOKEN",
        "chatrToken",
    }
    if os.path.exists(COOKIE_FILE):
        try:
            existing = _load_json_file(COOKIE_FILE)
            existing_cookies = existing.get("cookies", {})
            if isinstance(existing_cookies, dict):
                # Start with existing, then overlay new (new wins on non-Akamai cookies)
                merged = dict(existing_cookies)
                merged.update(new_cookies)
                # But for Akamai cookies: only update if new session actually has them
                # (prevents wiping a good _abck with a missing one)
                for akamai_name in AKAMAI_PROTECT:
                    if akamai_name not in new_cookies and akamai_name in existing_cookies:
                        merged[akamai_name] = existing_cookies[akamai_name]
                # Playwright seed must not wipe cart/login cookies from ATC
                if source.startswith("playwright"):
                    for auth_name in AUTH_PROTECT:
                        if auth_name not in new_cookies and auth_name in existing_cookies:
                            merged[auth_name] = existing_cookies[auth_name]
                new_cookies = merged
        except Exception:
            pass  # If read fails, just use what we have

    expires_at = _cookie_max_expiry(session)
    if expires_at is None:
        expires_at = time.time() + 1800
    payload = {
        "cookies": new_cookies,
        "saved_at": time.time(),
        "expires_at": expires_at,
        "source": source,
    }
    _save_json_file(COOKIE_FILE, payload)


def clear_saved_session() -> bool:
    """Remove bol_token.json so the next ensure_session() performs a fresh login."""
    removed = False
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)
        removed = True
        print(f"Deleted saved session: {COOKIE_FILE}")
    return removed


def load_session() -> Optional[Tuple[requests.Session, Dict[str, Any]]]:
    if not os.path.exists(COOKIE_FILE):
        return None
    try:
        data = _load_json_file(COOKIE_FILE)
        cookies = data.get("cookies", {})
        if not isinstance(cookies, dict) or not cookies:
            return None
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        session.cookies = _cookie_jar_from_dict({str(k): str(v) for k, v in cookies.items()})
        dedupe_cookies(session)
        return session, data
    except Exception:
        return None


# ---------------------------
# Login Page Parsing
# ---------------------------

def _prime_bol_cookies(session: requests.Session) -> None:
    """Visit login.bol.com to establish JSESSIONID and related cookies."""
    session.headers.update(DEFAULT_HEADERS)
    try:
        _request(
            session, "GET", LOGIN_PAGE_URL,
            label="prime_login_page", timeout=20, allow_redirects=True,
        )
    except Exception as exc:
        print(f"  [warn] login.bol.com prime failed: {exc}")


def prime_www_cookies(session: requests.Session, proxies: Optional[Dict[str, str]] = None) -> bool:
    """
    Visit www.bol.com so Akamai sets _abck and www-scoped XSRF / session cookies.
    Required before GraphQL add-to-cart on www.bol.com.

    Pass proxies= to route through the NL residential proxy (avoids machine IP ban).
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(
        {
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
    )
    try:
        resp = _request(
            session,
            "GET",
            f"{BOL_HOME_URL}/nl/nl/",
            label="prime_www_home",
            timeout=25,
            allow_redirects=True,
            headers=headers,
            proxies=proxies or {},
        )
        if session.cookies.get("_abck"):
            print("  [www] Akamai _abck cookie set")
            return True
        if resp.status_code == 200:
            print("  [www] Homepage OK (no _abck yet)")
            return True
        print(f"  [www] Homepage returned {resp.status_code}")
    except Exception as exc:
        print(f"  [warn] www.bol.com prime failed: {exc}")
    return False


def _parse_next_data(body: str) -> Dict[str, Any]:
    """
    bol.com's login page is a Next.js app.
    All dynamic data (csrf token, captcha nonce, etc.) is embedded in
    a <script id="__NEXT_DATA__" type="application/json"> tag.
    """
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        body, flags=re.IGNORECASE,
    )
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except Exception:
        return {}


def _build_login_context(session: requests.Session, login_page_resp: requests.Response) -> LoginContext:
    """
    Extract CSRF token, captcha-nonce, and crvtoken from the login page.

    bol.com login page is a Next.js app. All tokens live inside the
    __NEXT_DATA__ JSON blob embedded in a <script> tag:
      props.pageProps.data.csrf.token       -> _csrf body field & X-CSRF-TOKEN header
      props.pageProps.data.captcha.nonce    -> captcha-nonce body field

    crvtoken is generated by JavaScript at runtime (Date.now()) and is
    NOT present in the HTML — we replicate the browser behaviour.
    """
    body = login_page_resp.text or ""

    # Optionally dump the full HTML for offline inspection
    if _env_truthy(BOL_DUMP_HTML_ENV):
        with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"  [debug] Login page HTML saved to: {DEBUG_HTML_FILE}")

    # --- PRIMARY: parse the __NEXT_DATA__ JSON blob ---
    next_data = _parse_next_data(body)
    page_data: Dict[str, Any] = (
        next_data.get("props", {})
                 .get("pageProps", {})
                 .get("data", {})
    )

    # CSRF token: lives at data.csrf.token
    # Used for BOTH the x-csrf-token request header AND the _csrf body field
    csrf_obj = page_data.get("csrf", {})
    csrf_token = csrf_obj.get("token", "")
    csrf_header = csrf_token  # same value for both (per bol.com captured request)

    # Captcha nonce: lives at data.captcha.nonce
    captcha_nonce = page_data.get("captcha", {}).get("nonce", "")

    # crvtoken: NOT in the HTML — generated by the browser JS as Date.now() (milliseconds).
    # Captured value was a 16-digit integer e.g. "5014162020688527".
    # We replicate this by using the current time in milliseconds.
    crvtoken = str(int(time.time() * 1000))

    # --- FALLBACK: if __NEXT_DATA__ didn't have what we need, try headers / cookies / HTML ---
    if not csrf_token:
        csrf_token = (
            login_page_resp.headers.get("x-csrf-token", "")
            or login_page_resp.headers.get("X-CSRF-TOKEN", "")
            or login_page_resp.cookies.get("_csrf", "")
            or login_page_resp.cookies.get("XSRF-TOKEN", "")
            or session.cookies.get("_csrf", "")
            or session.cookies.get("XSRF-TOKEN", "")
        )
        if not csrf_token:
            csrf_token = _extract_token(body, (
                r'<meta\s+name=["\']_csrf["\'][^>]*content=["\']([^"\']+)["\']',
                r'<input[^>]+name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']',
                r'"_csrf"\s*:\s*"([^"]{20,})"',
                r'"csrfToken"\s*:\s*"([^"]{20,})"',
                r"_csrf\s*[=:]\s*['\"]([^'\"]{20,})['\"]",
            ))
        csrf_header = csrf_token

    if not captcha_nonce:
        captcha_nonce = _extract_token(body, (
            r'captcha-nonce["\s]*[:=]\s*["\']([^"\']{5,})["\']',
            r'"captchaNonce"\s*:\s*"([^"]+)"',
            r'name=["\']captcha-nonce["\'][^>]*value=["\']([^"\']+)["\']',
            r'"nonce"\s*:\s*"([A-Za-z0-9+/=]{20,})"',
        ))

    # --- hidden form fields (fallback) ---
    hidden_fields: Dict[str, str] = {}
    for match in re.finditer(
        r'<input[^>]+type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
        body, flags=re.IGNORECASE,
    ):
        hidden_fields[match.group(1)] = match.group(2)
    for match in re.finditer(
        r'<input[^>]+type=["\']hidden["\'][^>]*value=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\']',
        body, flags=re.IGNORECASE,
    ):
        if match.group(2) not in hidden_fields:
            hidden_fields[match.group(2)] = match.group(1)

    if not captcha_nonce and "captcha-nonce" in hidden_fields:
        captcha_nonce = hidden_fields["captcha-nonce"]
    if not csrf_token and "_csrf" in hidden_fields:
        csrf_token = csrf_header = hidden_fields["_csrf"]

    print(f"  [debug] csrf_token={bool(csrf_token)}, captcha_nonce={bool(captcha_nonce)}, crvtoken={bool(crvtoken)} (generated)")
    if not csrf_token:
        print("  [warn] CSRF token not found - login will likely be rejected")
    if not captcha_nonce:
        print("  [warn] captcha-nonce not found")

    return LoginContext(
        csrf_token=csrf_token,
        csrf_header=csrf_header,
        captcha_nonce=captcha_nonce,
        crvtoken=crvtoken,
        hidden_fields=hidden_fields,
    )


def _build_login_payload(
    username: str,
    password: str,
    context: LoginContext,
    captcha_token: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "j_username": username,
        "j_password": password,
    }
    if context.csrf_token:
        payload["_csrf"] = context.csrf_token
    if context.captcha_nonce:
        payload["captcha-nonce"] = context.captcha_nonce
    if captcha_token:
        payload["captcha-response-field-1"] = captcha_token
    if context.crvtoken:
        payload["crvtoken"] = context.crvtoken
    # Include any other hidden fields (e.g. remember-me flags)
    for key, value in context.hidden_fields.items():
        if key not in payload and key not in ("_csrf", "captcha-nonce", "crvtoken"):
            payload[key] = value
    return payload


def _parse_cookie_string(raw: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def _apply_cookies(session: requests.Session, cookies: Dict[str, str], domain: str) -> None:
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=domain)


def _login_context_from_snapshot(snapshot: Dict[str, Any]) -> LoginContext:
    csrf = str(snapshot.get("_csrf") or snapshot.get("csrf") or "").strip()
    return LoginContext(
        csrf_token=csrf,
        csrf_header=str(snapshot.get("x-csrf-token") or csrf).strip(),
        captcha_nonce=str(
            snapshot.get("captcha-nonce") or snapshot.get("captcha_nonce") or ""
        ).strip(),
        crvtoken=str(snapshot.get("crvtoken") or "").strip(),
    )


def _snapshot_payload(snapshot: Dict[str, Any], username: str, password: str) -> Dict[str, Any]:
    """Build POST body from a DevTools-captured login request."""
    captcha = str(
        snapshot.get("captcha-response-field-1")
        or snapshot.get("captcha_response")
        or ""
    ).strip()
    context = _login_context_from_snapshot(snapshot)
    return _build_login_payload(username, password, context, captcha)


def _snapshot_has_payload(snapshot: Dict[str, Any]) -> bool:
    if not snapshot:
        return False
    ctx = _login_context_from_snapshot(snapshot)
    captcha = str(
        snapshot.get("captcha-response-field-1")
        or snapshot.get("captcha_response")
        or ""
    ).strip()
    return bool(ctx.csrf_token and ctx.captcha_nonce and captcha)


def _snapshot_has_cookies(snapshot: Dict[str, Any]) -> bool:
    cookies = snapshot.get("cookies", "")
    if isinstance(cookies, str):
        return bool(cookies.strip())
    if isinstance(cookies, dict):
        return bool(cookies)
    return False


def _snapshot_is_complete(snapshot: Dict[str, Any]) -> bool:
    return _snapshot_has_payload(snapshot) and _snapshot_has_cookies(snapshot)


def _diagnose_login_config() -> str:
    lines = ["Login is not configured yet:"]
    snap_path = os.environ.get(BOL_LOGIN_SNAPSHOT_ENV, "").strip() or LOGIN_SNAPSHOT_FILE
    if os.path.exists(snap_path):
        try:
            snap = _load_json_file(snap_path)
        except Exception as exc:
            lines.append(f"  bol_login_snapshot.json: unreadable ({exc})")
            snap = {}
        if _snapshot_has_payload(snap):
            lines.append("  bol_login_snapshot.json: payload fields OK")
            if not _snapshot_has_cookies(snap):
                lines.append(
                    "  MISSING cookies - DevTools -> POST /wsp/api/login -> Headers -> "
                    'copy "Cookie" into the "cookies" field, then run again within ~2 min'
                )
        else:
            lines.append(
                "  bol_login_snapshot.json: incomplete - fill _csrf, captcha-nonce, "
                "captcha-response-field-1 (see bol_login_snapshot.example.json)"
            )
    else:
        lines.append(f"  bol_login_snapshot.json: not found ({snap_path})")
    if _load_twocaptcha_key():
        lines.append("  TWOCAPTCHA_API_KEY: set")
    else:
        lines.append("  TWOCAPTCHA_API_KEY: not set")
    lines.append("")
    lines.append(_captcha_setup_hint())
    return "\n".join(lines)


def _complete_login_after_post(
    session: requests.Session, resp: requests.Response
) -> Tuple[bool, str]:
    """Follow bol.com redirect chain after POST /wsp/api/login. Returns (ok, error)."""
    redirect_url = ""
    if resp.status_code in (301, 302, 303, 307, 308):
        try:
            redirect_url = resp.json().get("url", "")
        except Exception:
            pass
        if not redirect_url:
            redirect_url = resp.headers.get("Location", "")

    login_payload: Dict[str, Any] = {}
    try:
        login_payload = resp.json()
        redirect_url = login_payload.get("url", redirect_url)
        if _env_truthy(BOL_HTTP_VERBOSE_ENV):
            print(f"  [debug] login JSON: {login_payload}")
    except Exception:
        pass

    print(f"  [debug] login response={resp.status_code}, redirect_url={redirect_url!r}")

    if redirect_url and "error" in redirect_url.lower():
        return False, f"login rejected: {redirect_url}"

    # Follow post-login redirect (often "/" on login.bol.com -> www.bol.com SSO chain).
    if redirect_url:
        if not redirect_url.startswith("http"):
            redirect_url = urljoin(LOGIN_BASE_URL, redirect_url)
        try:
            _request(
                session, "GET", redirect_url,
                label="login_redirect_follow",
                timeout=20, allow_redirects=True,
            )
        except Exception as exc:
            print(f"  [debug] redirect follow error: {exc}")

    # Account overview + top-header-info set BUI / DYN_USER_ID on www.bol.com.
    account_referer = f"{BOL_HOME_URL}/nl/rnwy/account/overzicht"
    try:
        _request(
            session, "GET", account_referer,
            label="login_account_overview",
            timeout=20, allow_redirects=True,
        )
    except Exception as exc:
        print(f"  [debug] account overview error: {exc}")

    try:
        _request(
            session, "GET", ACCOUNT_INFO_URL,
            label="login_account_info_handshake",
            timeout=20, allow_redirects=True,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, */*",
                "Referer": account_referer,
            },
        )
    except Exception as exc:
        print(f"  [debug] account-info handshake error: {exc}")

    prime_www_cookies(session)

    time.sleep(1)
    print(f"  [debug] cookies after login: {_session_cookie_names(session)}")
    if is_valid(session):
        save_session(session, source="login")
        return True, ""
    return False, f"session invalid after login (status={resp.status_code}, redirect={redirect_url!r})"


def has_auth_cookies(session: requests.Session) -> bool:
    """True if bol.com login cookies (BUI, etc.) are present."""
    return _has_auth_cookies(session)


def _has_auth_cookies(session: requests.Session) -> bool:
    names = {c.name for c in session.cookies}
    return bool(names & {"BUI", "DYN_USER_ID", "DYN_USER_CONFIRM"})


def is_valid(session: requests.Session) -> bool:
    """Return True if the session is a logged-in bol.com account."""
    if _has_auth_cookies(session):
        return True
    try:
        response = _request(
            session, "GET", ACCOUNT_INFO_URL,
            label="validate_session", timeout=20,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and any(
            k in payload for k in ("points", "mySelectUrl")
        )
    except Exception:
        return False


# ---------------------------
# Auth Logic
# ---------------------------

def _load_hardcoded_login() -> Dict[str, Any]:
    """DevTools-captured fields (email, password, captcha). Cookies come from a live session."""
    if _load_twocaptcha_key():
        return {}
    if os.path.exists(HARDCODED_LOGIN_FILE):
        try:
            data = _load_json_file(HARDCODED_LOGIN_FILE)
            if _snapshot_has_payload(data):
                return data
        except Exception as exc:
            print(f"  [warn] could not read {HARDCODED_LOGIN_FILE}: {exc}")
    return {}


def _post_login(
    session: requests.Session,
    payload: Dict[str, Any],
    csrf_header: str,
    *,
    label: str,
) -> Tuple[bool, str]:
    headers = {
        "Content-Type": "application/json",
        "Origin": LOGIN_BASE_URL,
        "Referer": LOGIN_PAGE_URL,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=1, i",
    }
    if csrf_header:
        headers["x-csrf-token"] = csrf_header
    resp = _request(
        session, "POST", LOGIN_API_URL,
        label=label, json=payload, headers=headers,
        timeout=30, allow_redirects=False,
    )
    return _complete_login_after_post(session, resp)


def do_login_with_session(
    username: str,
    password: str,
    static: Dict[str, Any],
) -> requests.Session:
    """
  Login using bol_hardcoded_login.json.

  - Hardcoded path: use _csrf + captcha-nonce + captcha-response from the SAME
    file (one DevTools capture). GET /wsp/login only to obtain JSESSIONID cookies.
  - 2captcha path: fresh _csrf/nonce from the page + newly solved captcha token.
    """
    user = str(static.get("j_username") or username)
    pwd = str(static.get("j_password") or password)
    hardcoded_captcha = str(
        static.get("captcha-response-field-1") or static.get("captcha_response") or ""
    ).strip()
    static_ctx = _login_context_from_snapshot(static)
    static_bundle = bool(
        hardcoded_captcha and static_ctx.csrf_token and static_ctx.captcha_nonce
    )
    twocaptcha_key = _load_twocaptcha_key()
    last_error = ""
    use_hardcoded_captcha = bool(hardcoded_captcha)

    for attempt in range(1, 3):
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

        # Optional: browser Cookie header from the same DevTools POST as the payload
        raw_cookies = static.get("cookies", "")
        if isinstance(raw_cookies, dict) and raw_cookies:
            _apply_cookies(session, {str(k): str(v) for k, v in raw_cookies.items()}, "login.bol.com")
            print(f"  [session] applied {len(raw_cookies)} cookie(s) from file")
        elif isinstance(raw_cookies, str) and raw_cookies.strip():
            parsed = _parse_cookie_string(raw_cookies)
            _apply_cookies(session, parsed, "login.bol.com")
            print(f"  [session] applied {len(parsed)} cookie(s) from file")

        try:
            login_page = _request(
                session, "GET", LOGIN_PAGE_URL,
                label=f"login_session_attempt_{attempt}",
                timeout=20, allow_redirects=True,
            )
        except Exception as exc:
            last_error = f"login page failed: {exc}"
            time.sleep(2)
            continue

        live_ctx = _build_login_context(session, login_page)
        captcha_token = ""

        if use_hardcoded_captcha and static_bundle:
            # All three must be from the same browser moment (bol_hardcoded_login.json)
            context = static_ctx
            context.crvtoken = str(int(time.time() * 1000))
            captcha_token = hardcoded_captcha
            print(
                "  [session] using hardcoded _csrf + captcha-nonce + captcha-response "
                "(same DevTools capture)"
            )
        elif twocaptcha_key:
            context = live_ctx
            if not context.csrf_token or not context.captcha_nonce:
                last_error = "could not parse _csrf or captcha-nonce from login page"
                time.sleep(2)
                continue
            try:
                captcha_token = _solve_recaptcha_2captcha(
                    api_key=twocaptcha_key,
                    site_key=RECAPTCHA_SITE_KEY,
                    page_url=LOGIN_PAGE_URL,
                    cookies=_session_cookie_header(session),
                )
            except Exception as exc:
                print(f"  [warn] 2captcha failed: {exc}")
        else:
            last_error = (
                "hardcoded captcha expired or incomplete. Update bol_hardcoded_login.json "
                "within 2 min of solving captcha in Chrome, OR add twocaptcha_api_key to "
                "bol_credentials.json for automatic solving."
            )
            break

        if not captcha_token:
            last_error = "no captcha token available"
            break

        payload = _build_login_payload(user, pwd, context, captcha_token)
        print(
            f"  [session] POST login (cookies={_session_cookie_names(session)})"
        )
        ok, err = _post_login(
            session, payload, context.csrf_header, label=f"login_post_session_{attempt}"
        )
        if ok:
            return session
        last_error = err
        if "captcha" in err.lower() and use_hardcoded_captcha and twocaptcha_key:
            print("  [session] hardcoded captcha rejected, retrying with 2captcha...")
            use_hardcoded_captcha = False
            continue
        time.sleep(2)

    raise RuntimeError(f"bol.com session login failed: {last_error}")


def _load_login_snapshot() -> Dict[str, Any]:
    """Load a DevTools-captured login payload (file, credentials, or env vars)."""
    snapshot: Dict[str, Any] = {}

    snapshot_path = os.environ.get(BOL_LOGIN_SNAPSHOT_ENV, "").strip() or LOGIN_SNAPSHOT_FILE
    if os.path.exists(snapshot_path):
        try:
            snapshot.update(_load_json_file(snapshot_path))
        except Exception as exc:
            print(f"  [warn] could not read {snapshot_path}: {exc}")

    creds = _load_credentials_file()
    nested = creds.get("login_snapshot")
    if isinstance(nested, dict):
        snapshot.update(nested)

    env_map = {
        "_csrf": BOL_CSRF_TOKEN_ENV,
        "captcha-nonce": BOL_CAPTCHA_NONCE_ENV,
        "captcha-response-field-1": BOL_CAPTCHA_TOKEN_ENV,
        "crvtoken": BOL_CRVTOKEN_ENV,
        "j_username": "BOL_USERNAME",
        "j_password": "BOL_PASSWORD",
    }
    for field, env_name in env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            snapshot[field] = value

    raw_cookies = os.environ.get(BOL_LOGIN_COOKIES_ENV, "").strip()
    if raw_cookies:
        if raw_cookies.startswith("{"):
            try:
                parsed = json.loads(raw_cookies)
                if isinstance(parsed, dict):
                    snapshot["cookies"] = parsed
            except Exception:
                pass
        else:
            snapshot["cookies"] = _parse_cookie_string(raw_cookies)

    return snapshot


def do_login_with_snapshot(
    username: str,
    password: str,
    snapshot: Dict[str, Any],
) -> requests.Session:
    """
    Replay a login POST captured from Chrome DevTools.

    The captcha token is bound to captcha-nonce + _csrf + JSESSIONID from the
    same browser session. Copy the full request body AND the Cookie header from
    the POST to /wsp/api/login, then run within ~2 minutes.
    """
    if not _snapshot_is_complete(snapshot):
        raise RuntimeError(
            "login snapshot incomplete — need _csrf, captcha-nonce, "
            "captcha-response-field-1, and cookies from the same DevTools request."
        )

    username = str(snapshot.get("j_username") or username)
    password = str(snapshot.get("j_password") or password)

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    cookies = snapshot.get("cookies", {})
    if isinstance(cookies, dict) and cookies:
        _apply_cookies(session, {str(k): str(v) for k, v in cookies.items()}, "login.bol.com")
        print(f"  [snapshot] loaded {len(cookies)} cookie(s): {', '.join(sorted(cookies))}")
    else:
        print(
            "  [warn] No cookies in snapshot — login will likely fail.\n"
            "         In DevTools: Network → POST /wsp/api/login → Headers → "
            "copy the Cookie value into bol_login_snapshot.json as \"cookies\": \"JSESSIONID=...\""
        )

    context = _login_context_from_snapshot(snapshot)
    payload = _snapshot_payload(snapshot, username, password)
    print("  [snapshot] POSTing captured login payload (no fresh page fetch)")
    ok, err = _post_login(session, payload, context.csrf_header, label="login_post_snapshot")
    if ok:
        return session
    raise RuntimeError(f"bol.com snapshot login failed: {err}")


def do_login(username: str, password: str, max_retries: int = 2) -> requests.Session:
    """
    Log in to bol.com.
    Default: bol_hardcoded_login.json + live session cookies/csrf from GET /wsp/login.
    Fallback: 2captcha (TWOCAPTCHA_API_KEY) or bol_login_snapshot.json with browser cookies.
    """
    twocaptcha_key = _load_twocaptcha_key()
    hardcoded = _load_hardcoded_login()
    hardcoded_failed = False
    if hardcoded and not twocaptcha_key:
        try:
            return do_login_with_session(username, password, hardcoded)
        except RuntimeError as exc:
            hardcoded_failed = True
            print(f"  [warn] hardcoded session login failed: {exc}")

    snapshot = _load_login_snapshot()
    if _snapshot_has_payload(snapshot) and not _snapshot_has_cookies(snapshot):
        try:
            return do_login_with_session(username, password, snapshot)
        except RuntimeError as exc:
            print(f"  [warn] snapshot session login failed: {exc}")
    if _snapshot_is_complete(snapshot):
        return do_login_with_snapshot(username, password, snapshot)

    manual_captcha = os.environ.get(BOL_CAPTCHA_TOKEN_ENV, "").strip()
    manual_nonce = os.environ.get(BOL_CAPTCHA_NONCE_ENV, "").strip()
    manual_csrf = os.environ.get(BOL_CSRF_TOKEN_ENV, "").strip()
    last_error: Optional[str] = None

    if not twocaptcha_key and not manual_captcha:
        if hardcoded_failed:
            raise RuntimeError(
                "Login failed: hardcoded captcha in bol_hardcoded_login.json is expired.\n"
                "reCAPTCHA tokens only work for ~2 minutes.\n\n"
                "Fix (pick one):\n"
                "  1) Add to bol_credentials.json:\n"
                '       "twocaptcha_api_key": "your_key"\n'
                "     (sign up at https://2captcha.com, ~$3 balance)\n"
                "  2) Solve captcha in Chrome, copy fresh captcha-response-field-1 +\n"
                "     captcha-nonce + _csrf + Cookie header into bol_hardcoded_login.json,\n"
                "     then run this script within 2 minutes."
            )
        raise RuntimeError(_diagnose_login_config())
    if manual_captcha and not (manual_nonce and manual_csrf):
        raise RuntimeError(
            "BOL_CAPTCHA_TOKEN is set but captcha tokens are tied to a specific page load.\n"
            "Also set BOL_CAPTCHA_NONCE and BOL_CSRF_TOKEN from the same DevTools request,\n"
            "or save the full payload to bol_login_snapshot.json (see bol_login_snapshot.example.json)."
        )

    for attempt in range(1, max_retries + 1):
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

        # Step 1: Prime login.bol.com to get JSESSIONID cookie
        _prime_bol_cookies(session)

        # Step 2: Fetch login page (second GET gives us the CSRF tokens)
        try:
            login_page = _request(
                session, "GET", LOGIN_PAGE_URL,
                label=f"login_page_attempt_{attempt}",
                timeout=20, allow_redirects=True,
            )
        except Exception as exc:
            last_error = f"login page failed: {exc}"
            time.sleep(2)
            continue

        context = _build_login_context(session, login_page)
        if manual_nonce:
            context.captcha_nonce = manual_nonce
        if manual_csrf:
            context.csrf_token = context.csrf_header = manual_csrf
        crv = os.environ.get(BOL_CRVTOKEN_ENV, "").strip()
        if crv:
            context.crvtoken = crv

        # Step 3: Solve reCAPTCHA (required by bol.com)
        captcha_token = manual_captcha if attempt == 1 and manual_captcha else ""
        if not captcha_token and twocaptcha_key:
            try:
                captcha_token = _solve_recaptcha_2captcha(
                    api_key=twocaptcha_key,
                    site_key=RECAPTCHA_SITE_KEY,
                    page_url=LOGIN_PAGE_URL,
                    cookies=_session_cookie_header(session),
                )
            except Exception as exc:
                print(f"  [warn] 2captcha failed: {exc}")
        elif not captcha_token:
            print(f"  [warn] No captcha token available.\n{_captcha_setup_hint()}")

        # Step 4: POST login — do NOT follow redirect so we can inspect
        # Payload mirrors exactly what the browser sends (captured via DevTools)
        payload = _build_login_payload(username, password, context, captcha_token)
        headers = {
            "Content-Type": "application/json",
            "Origin": LOGIN_BASE_URL,
            "Referer": LOGIN_PAGE_URL,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Priority": "u=1, i",
        }
        # x-csrf-token header is separate from the _csrf body field on bol.com
        if context.csrf_header:
            headers["x-csrf-token"] = context.csrf_header

        try:
            resp = _request(
                session, "POST", LOGIN_API_URL,
                label=f"login_post_attempt_{attempt}",
                json=payload, headers=headers,
                timeout=30, allow_redirects=False,
            )
        except Exception as exc:
            last_error = f"login request failed: {exc}"
            time.sleep(2)
            continue

        ok, err = _complete_login_after_post(session, resp)
        if ok:
            return session
        last_error = err
        time.sleep(2)

    raise RuntimeError(f"bol.com login failed after {max_retries} attempts: {last_error}")


def _load_credentials_file() -> Dict[str, Any]:
    try:
        if os.path.exists(CREDENTIAL_FILE):
            return _load_json_file(CREDENTIAL_FILE)
    except Exception:
        pass
    return {}


def _load_default_credentials() -> Tuple[Optional[str], Optional[str]]:
    username = os.environ.get("BOL_USERNAME")
    password = os.environ.get("BOL_PASSWORD")
    if username and password:
        return username, password
    data = _load_credentials_file()
    return data.get("username"), data.get("password")


def _load_twocaptcha_key() -> str:
    key = os.environ.get(TWOCAPTCHA_KEY_ENV, "").strip()
    if key:
        return key
    data = _load_credentials_file()
    for field in ("twocaptcha_api_key", "TWOCAPTCHA_API_KEY", "2captcha_api_key"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _captcha_setup_hint() -> str:
    return (
        "reCAPTCHA is required. Choose one:\n"
        "  1) 2captcha (automatic): sign up at https://2captcha.com, add balance, then set the key:\n"
        "       PowerShell:  $env:TWOCAPTCHA_API_KEY = 'your_key_here'\n"
        "       Or add to bol_credentials.json:  \"twocaptcha_api_key\": \"your_key_here\"\n"
        "  2) Browser snapshot (one-off): copy the full POST body from DevTools into\n"
        f"       {LOGIN_SNAPSHOT_FILE} (see bol_login_snapshot.example.json), including\n"
        "       the Cookie header from the same request. Run within ~2 minutes.\n"
        "  3) Env vars (one-off): set BOL_CAPTCHA_TOKEN, BOL_CAPTCHA_NONCE, BOL_CSRF_TOKEN,\n"
        "       BOL_CRVTOKEN, and BOL_LOGIN_COOKIES from the same DevTools request."
    )


def ensure_session(
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> requests.Session:
    loaded = load_session()
    if loaded and not force_refresh:
        session, meta = loaded
        expires_at = meta.get("expires_at")
        if is_valid(session):
            try:
                if expires_at is None or time.time() + REFRESH_MARGIN < float(expires_at):
                    save_session(session, source="reuse")
                    return session
            except Exception:
                save_session(session, source="reuse")
                return session

    if not username or not password:
        username, password = _load_default_credentials()
    if not username or not password:
        raise RuntimeError(
            "bol.com credentials missing. Set BOL_USERNAME/BOL_PASSWORD or fill bol_credentials.json."
        )

    session = do_login(username, password)
    return session


def get_token_value(session: requests.Session) -> Dict[str, Any]:
    cookies = requests.utils.dict_from_cookiejar(session.cookies)
    expires_at = _cookie_max_expiry(session)
    if expires_at is None:
        expires_at = time.time() + 1800
    return {
        "cookies": cookies,
        "saved_at": time.time(),
        "expires_at": expires_at,
        "cookie_names": _session_cookie_names(session),
    }


def maintain_session_forever(
    session: requests.Session,
    username: str,
    password: str,
    poll_seconds: int,
) -> None:
    current = session
    sleep_seconds = max(30, poll_seconds)
    while True:
        if not is_valid(current):
            print("Session expired. Refreshing now.")
            try:
                current = do_login(username, password)
                print("Session refreshed:", COOKIE_FILE)
            except Exception as exc:
                print(f"Refresh failed: {exc}. Retrying in {sleep_seconds}s.")
                time.sleep(sleep_seconds)
                continue
        else:
            save_session(current, source="heartbeat")
            print(f"Session still valid. Next check in {poll_seconds}s.")
        time.sleep(sleep_seconds)


# ---------------------------
# Entry Point
# ---------------------------

def main() -> None:
    username, password = _load_default_credentials()
    force_refresh = _env_truthy(BOL_FORCE_REFRESH_ENV)
    session = ensure_session(username, password, force_refresh=force_refresh)
    token = get_token_value(session)
    save_session(session, source="main")

    print("Session ready and saved:", COOKIE_FILE)
    print("Cookies:", token["cookie_names"])

    if _env_truthy(BOL_ONE_SHOT_ENV):
        print(json.dumps(token, indent=2, sort_keys=True))
        return

    if not username or not password:
        print("No credentials loaded — skipping background monitoring.")
        return

    poll_seconds = int(os.environ.get(BOL_KEEP_ALIVE_SECONDS_ENV, "300"))
    print(f"Monitoring session every {poll_seconds}s. Set BOL_ONCE=1 to exit after one check.")
    maintain_session_forever(session, username, password, poll_seconds)


if __name__ == "__main__":
    main()
