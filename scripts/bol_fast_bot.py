#!/usr/bin/env python3
"""
bol.com fast bot — GraphQL monitor + ATC + iDEAL checkout (curl_cffi chrome124).

Same architecture as standalone monitor bots:
  • Stock: POST /api/graphql Product (bestSellingOffer) — no PDP HTML needed
  • Cart: CreateBasket + AddItem (persisted queries)
  • Checkout: iDEAL select → execute-payment-plan → payment-execution → pay.ideal.nl

Setup (run from BOL-BOT folder):
  1. Export Chrome cookies while logged in on www.bol.com (same proxy you will use):
     - cookies.txt  — JSON array [{name, value}, ...]  OR
     - bol_token.json (auto-loaded if cookies.txt missing)
  2. Proxies — proxy.txt (host:port:user:pass per line) OR config/roundproxies.yaml
  3. product.csv — one column product_url
  4. Optional: discord_webhook.txt

  python scripts/bol_fast_bot.py
  python scripts/bol_fast_bot.py --product-url "https://www.bol.com/nl/nl/p/.../9300000182508099/"
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlencode

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

REQUIRED_PIP = {"curl_cffi": "curl_cffi", "yaml": "pyyaml"}


def ensure_deps() -> None:
    missing = [pkg for mod, pkg in REQUIRED_PIP.items() if not _try_import(mod)]
    if not missing:
        return
    print(f"Installing: {', '.join(missing)}")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *missing],
        cwd=str(ROOT),
    )
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


ensure_deps()

from curl_cffi.requests import Session  # noqa: E402

# ── Config ───────────────────────────────────────────────────────────────────

QUANTITY = int(os.environ.get("BOL_QUANTITY", "1"))
PROXY_FILE = os.environ.get("BOL_PROXY_FILE", str(ROOT / "proxy.txt"))
COOKIES_FILE = os.environ.get("BOL_COOKIES_FILE", str(ROOT / "cookies.txt"))
TOKEN_FILE = str(ROOT / "bol_token.json")
PRODUCTS_FILE = os.environ.get("BOL_PRODUCTS_CSV", str(ROOT / "product.csv"))
DISCORD_WEBHOOK_FILE = str(ROOT / "discord_webhook.txt")
PAYMENT_URLS_FILE = str(ROOT / "payment_urls.txt")
ROUNDPROXIES_YAML = ROOT / "config" / "roundproxies.yaml"

GRAPHQL_URL = "https://www.bol.com/api/graphql"
CHECKOUT_PAGE_URL = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"
EXECUTE_PAYMENT_URL = (
    "https://www.bol.com/nl/nl/rnwy/checkout/command/execute-payment-plan"
)

CREATE_BASKET_HASH = "sha256:92b016f96aa83a630f5cc5ebcd48d6da90e155aed1119a492e71856d99e590e0"
ADD_ITEM_HASH = "sha256:fda23bccf49694870747c1a4a5003944bca994020fc3cb05ae9c6cdf029aaa7c"
PRODUCT_HASH = "19a9e78148968e88bb63ef930b33d63b788c66d287ae658c413fe670389bcce4"
RETAILER_HASH = "sha256:5c82e256f671fb54f6775707b4cf11a857243a01109e10130daf3bb0320cc3d4"
IDEAL_CHOICE_HASH = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
UPDATE_PAYMENT_CHOICE_QUERY = """
mutation CheckoutUpdatePaymentChoiceMutation(
  $input: UpdatePaymentChoiceInput!
  $requestSource: RequestSource
) {
  paymentOfferings {
    updatePaymentChoice(input: $input, requestSource: $requestSource) {
      __typename
      ... on PaymentOffering {
        paymentOfferingMessages { textBundleKey code }
      }
    }
  }
}
"""
OFFERING_HASH = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"

BOL_SELLER_NAME = "bol"
BOL_RETAILER_ID = "0"
CHECK_DELAY = float(os.environ.get("BOL_CHECK_DELAY", "3"))
WORKERS = int(os.environ.get("BOL_WORKERS", "1"))
REQUIRE_BOL_SELLER = os.environ.get("BOL_REQUIRE_BOL_SELLER", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PURCHASE_LOCK = threading.Lock()

PAGE_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "accept-encoding": "gzip, deflate, br, zstd",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
PAGE_HEADERS_ORIGIN = {**PAGE_HEADERS, "sec-fetch-site": "same-origin"}
GQL_HEADERS = {
    "accept": (
        "application/graphql-response+json, application/graphql+json, "
        "application/json, text/event-stream"
    ),
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "content-type": "application/json",
    "dnt": "1",
    "origin": "https://www.bol.com",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": PAGE_HEADERS["user-agent"],
}
FORM_HEADERS = {
    **GQL_HEADERS,
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "content-type": "application/x-www-form-urlencoded",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
}
JSON_HEADERS = {**GQL_HEADERS, "accept": "application/json, text/plain, */*"}


class RotateProxy(Exception):
    pass


class CheckoutFatal(BaseException):
    pass


def load_cookies_dict() -> Dict[str, str]:
    if os.path.isfile(COOKIES_FILE):
        with open(COOKIES_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return {c["name"]: c["value"] for c in raw if c.get("name")}
        if isinstance(raw, dict) and "cookies" in raw:
            return dict(raw["cookies"])
    if os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("cookies"), dict):
            print(f"Loaded cookies from {TOKEN_FILE}")
            return dict(data["cookies"])
    raise FileNotFoundError(
        f"Need {COOKIES_FILE} (Chrome export) or {TOKEN_FILE}"
    )


def load_proxies() -> List[str]:
    lines: List[str] = []
    if os.path.isfile(PROXY_FILE):
        with open(PROXY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, password = parts
                    lines.append(f"http://{user}:{password}@{host}:{port}")
                elif line.startswith("http"):
                    lines.append(line)
    if not lines and ROUNDPROXIES_YAML.is_file():
        try:
            import yaml

            cfg = yaml.safe_load(ROUNDPROXIES_YAML.read_text(encoding="utf-8")) or {}
            for line in cfg.get("proxy_lines") or []:
                parts = str(line).strip().split(":")
                if len(parts) >= 4:
                    host, port = parts[0], parts[1]
                    user = ":".join(parts[2:-1])
                    password = parts[-1]
                    lines.append(f"http://{user}:{password}@{host}:{port}")
        except Exception as exc:
            print(f"roundproxies.yaml: {exc}")
    if not lines:
        print("WARNING: No proxies — using direct connection (may be Akamai-blocked)")
    return lines


def build_session(cookies: dict, proxy_url: Optional[str], timeout: int = 12) -> Session:
    kw: dict = dict(
        impersonate="chrome124",
        allow_redirects=True,
        timeout=timeout,
        headers=PAGE_HEADERS,
        cookies=cookies,
    )
    if proxy_url:
        kw["proxies"] = {"http": proxy_url, "https": proxy_url}
    return Session(**kw)


class ProxyManager:
    COOLDOWN = 60

    def __init__(self, proxies: List[str], cookies: dict):
        self.proxies = proxies or [None]
        self.cookies = cookies
        self.lock = threading.Lock()
        self.blocked_until: Dict[str, float] = {}
        self._idx = 0
        self._local = threading.local()

    def _next(self, after: int) -> Tuple[int, Optional[str]]:
        n = len(self.proxies)
        for off in range(1, n + 1):
            i = (after + off) % n
            p = self.proxies[i]
            key = p or "direct"
            if time.time() >= self.blocked_until.get(key, 0):
                return i, p
        i = min(range(n), key=lambda j: self.blocked_until.get(self.proxies[j] or "direct", 0))
        return i, self.proxies[i]

    def get_proxy(self) -> Optional[str]:
        if not hasattr(self._local, "proxy"):
            with self.lock:
                self._idx, p = self._next(self._idx)
            self._local.proxy = p
        return self._local.proxy

    def rotate(self) -> None:
        cur = getattr(self._local, "proxy", self.proxies[0])
        key = cur or "direct"
        self.blocked_until[key] = time.time() + self.COOLDOWN
        with self.lock:
            cur_i = self.proxies.index(cur) if cur in self.proxies else 0
            _, self._local.proxy = self._next(cur_i)

    def _session(self, proxy: Optional[str]) -> Session:
        sessions = getattr(self._local, "sessions", None)
        if sessions is None:
            sessions = {}
            self._local.sessions = sessions
        key = proxy or "direct"
        if key not in sessions:
            sessions[key] = build_session(self.cookies, proxy)
        return sessions[key]

    def _check(self, resp) -> None:
        if resp.status_code in (403, 429, 503):
            raise RotateProxy(f"HTTP {resp.status_code}")

    def get(self, url: str, checkout: bool = False, **kw):
        proxy = self.get_proxy()
        try:
            r = self._session(proxy).get(url, **kw)
            self._check(r)
            return r
        except RotateProxy:
            raise
        except Exception as e:
            raise RotateProxy(str(e)) from e

    def post(self, url: str, checkout: bool = False, **kw):
        proxy = self.get_proxy()
        try:
            r = self._session(proxy).post(url, **kw)
            self._check(r)
            return r
        except RotateProxy:
            raise
        except Exception as e:
            raise RotateProxy(str(e)) from e

    @property
    def checkout_session(self) -> Session:
        return self._session(self.get_proxy())


def extract_product_id(url: str) -> Optional[str]:
    m = re.search(r"/p/[^/]+/(\d{10,})", url)
    return m.group(1) if m else None


def _dehydrated(html: str, key: str) -> Optional[str]:
    m = re.search(r'\\\"' + re.escape(key) + r'\\\"[,\s]*\\\"([^\\\"]+)\\\"', html)
    if m:
        return m.group(1)
    m = re.search(r'"' + re.escape(key) + r'"\s*[,:]\s*"([^"]+)"', html)
    return m.group(1) if m else None


def xsrf_token(pm: ProxyManager, ctx: dict) -> Optional[str]:
    if ctx.get("xsrf"):
        return ctx["xsrf"]
    return pm.checkout_session.cookies.get("XSRF-TOKEN")


def gql_headers(pm: ProxyManager, product_url: str, op: str, ctx: dict) -> dict:
    h = {
        **GQL_HEADERS,
        "referer": product_url,
        "bol-app-country": "NL",
        "bol-app-operation-name": op,
        "bol-client-app-name": "product-web-fe",
    }
    x = xsrf_token(pm, ctx)
    if x:
        h["x-xsrf-token"] = x
    if ctx.get("page_id"):
        h["bol-client-page-id"] = ctx["page_id"]
        h["m2-page-id"] = ctx["page_id"]
    return h


def fetch_product_gql(pm: ProxyManager, product_id: str, url: str, ctx: dict) -> dict:
    r = pm.post(
        GRAPHQL_URL,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": PRODUCT_HASH, "version": 1}},
            "operationName": "Product",
            "variables": {"productId": product_id},
        },
        headers=gql_headers(pm, url, "Product", ctx),
    )
    data = r.json()
    product = (data.get("data") or {}).get("product")
    if not isinstance(product, dict):
        return {"exists": False, "offer_uid": None}
    best = product.get("bestSellingOffer") or {}
    uid = best.get("offerUid") if isinstance(best, dict) else None
    return {"exists": True, "offer_uid": uid, "best": best}


def fetch_retailer_gql(pm: ProxyManager, offer_uid: str, url: str, ctx: dict) -> dict:
    r = pm.post(
        GRAPHQL_URL,
        json={
            "operationName": "retailerInfo",
            "extensions": {"persistedQuery": {"sha256Hash": RETAILER_HASH, "version": 1}},
            "variables": {"offerUid": offer_uid},
        },
        headers=gql_headers(pm, url, "retailerInfo", ctx),
    )
    data = r.json()
    so = (data.get("data") or {}).get("sellingOffer") or {}
    ret = so.get("retailer") or {}
    return {
        "name": ret.get("name"),
        "id": str(ret["id"]) if ret.get("id") is not None else None,
    }


def is_bol_seller(name: Optional[str], sid: Optional[str]) -> bool:
    return (name or "").strip().casefold() == BOL_SELLER_NAME or (sid or "").strip() == BOL_RETAILER_ID


def warm_basket(pm: ProxyManager, ctx: dict) -> bool:
    try:
        r = pm.get(
            "https://www.bol.com/nl/nl/basket/",
            headers={**PAGE_HEADERS_ORIGIN, "referer": "https://www.bol.com/nl/nl/"},
        )
        if r.status_code == 200:
            html = r.text
            ctx["basket_id"] = _dehydrated(html, "Basket") or ctx.get("basket_id")
            ctx["xsrf"] = _dehydrated(html, "xsrf") or xsrf_token(pm, ctx)
            ctx["page_id"] = _dehydrated(html, "pageId") or ctx.get("page_id")
            if ctx.get("basket_id"):
                return True
    except RotateProxy:
        raise
    except Exception:
        pass
    h = {**GQL_HEADERS, "referer": "https://www.bol.com/nl/nl/", "bol-app-operation-name": "CreateBasket"}
    x = xsrf_token(pm, ctx)
    if x:
        h["x-xsrf-token"] = x
    pm.post(
        GRAPHQL_URL,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": CREATE_BASKET_HASH, "version": 1}},
            "operationName": "CreateBasket",
            "variables": {},
        },
        headers=h,
    )
    return bool(ctx.get("basket_id"))


def add_to_cart(pm: ProxyManager, url: str, pid: str, offer_uid: str, ctx: dict) -> dict:
    bid = ctx.get("basket_id")
    if not bid:
        raise ValueError("no basket_id")
    h = {
        **GQL_HEADERS,
        "referer": url,
        "bol-app-country": "NL",
        "bol-app-operation-name": "AddItem",
        "bol-client-app-name": "product-web-fe",
    }
    x = xsrf_token(pm, ctx)
    if x:
        h["x-xsrf-token"] = x
    if ctx.get("page_id"):
        h["bol-client-page-id"] = ctx["page_id"]
        h["m2-page-id"] = ctx["page_id"]
    r = pm.post(
        GRAPHQL_URL,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": ADD_ITEM_HASH, "version": 1}},
            "operationName": "AddItem",
            "variables": {
                "input": {
                    "basketId": bid,
                    "offerUid": offer_uid,
                    "productId": pid,
                    "quantity": QUANTITY,
                }
            },
        },
        headers=h,
    )
    return r.json()


def cart_ok(data: dict) -> bool:
    add = (data.get("data") or {}).get("basket", {}).get("addItem") or {}
    if "Failed" in str(add.get("__typename", "")):
        return False
    items = add.get("items") or []
    return bool(items)


def create_payment_offering(
    pm: ProxyManager, basket_id: str, cd: dict, product_url: str
) -> Optional[str]:
    """GraphQL paymentPlanId when checkout HTML no longer embeds PaymentOffering."""
    for subject_type in ("ORDER", "BASKET"):
        h = {
            **GQL_HEADERS,
            "referer": CHECKOUT_PAGE_URL,
            "bol-app-country": "NL",
            "bol-app-operation-name": "CheckoutCreatePaymentOfferingMutation",
            "bol-client-app-name": "checkout-web-fe",
        }
        if cd.get("xsrf"):
            h["x-xsrf-token"] = cd["xsrf"]
        if cd.get("page_id"):
            h["bol-client-page-id"] = cd["page_id"]
            h["m2-page-id"] = cd["page_id"]
        try:
            r = pm.post(
                GRAPHQL_URL,
                checkout=True,
                json={
                    "extensions": {
                        "persistedQuery": {"sha256Hash": OFFERING_HASH, "version": 1}
                    },
                    "operationName": "CheckoutCreatePaymentOfferingMutation",
                    "variables": {
                        "input": {
                            "subjects": [{"id": basket_id, "type": subject_type}],
                        },
                        "requestSource": "CHECKOUT",
                    },
                },
                headers=h,
            )
            offering = (
                (r.json().get("data") or {})
                .get("paymentOfferings", {})
                .get("createPaymentOffering")
            )
            if isinstance(offering, dict) and offering.get("id"):
                oid = str(offering["id"])
                print(f"[checkout] paymentPlanId from GQL ({subject_type})={oid}")
                return oid
        except Exception as e:
            print(f"[checkout] CreatePaymentOffering ({subject_type}): {e}")
    return None


def checkout_page(pm: ProxyManager, product_url: str) -> Optional[dict]:
    r = pm.get(
        CHECKOUT_PAGE_URL,
        headers={**PAGE_HEADERS_ORIGIN, "referer": product_url},
    )
    if r.status_code != 200:
        return None
    html = r.text
    order_hash = None
    m = re.search(r'\\\"hash\\\"[,\s]*\\\"([a-f0-9]{32})\\\"', html) or re.search(
        r'"hash"\s*[,:]\s*"([a-f0-9]{32})"', html
    )
    if m:
        order_hash = m.group(1)
    plan = None
    m = re.search(r'\\\"PaymentOffering\\\"[,\s]*\\\"(\d{6,12})\\\"', html) or re.search(
        r'"PaymentOffering"[,\s]*"(\d{6,12})"', html
    )
    if m:
        plan = m.group(1)
    if not order_hash:
        raise CheckoutFatal("orderCandidateHash missing — re-export cookies from checkout")
    return {
        "orderCandidateHash": order_hash,
        "paymentPlanId": plan,
        "xsrf": _dehydrated(html, "xsrf") or pm.checkout_session.cookies.get("XSRF-TOKEN"),
        "page_id": _dehydrated(html, "pageId"),
        "rsSessionId": int(time.time() * 1000),
    }


def select_ideal(pm: ProxyManager, cd: dict) -> bool:
    oid = cd["paymentPlanId"]
    if str(oid).isdigit():
        oid = int(oid)
    ideal_input: Dict[str, Any] = {
        "paymentOfferingId": oid,
        "paymentMethodCode": "IDEAL",
        "idealDetails": {},
    }
    bank_id = os.environ.get("BOL_IDEAL_BANK_ID", "").strip()
    if bank_id:
        ideal_input["idealDetails"] = {"bankId": bank_id}
    variables = {"input": ideal_input, "requestSource": "CHECKOUT"}

    h = {
        **GQL_HEADERS,
        "referer": CHECKOUT_PAGE_URL,
        "bol-app-country": "NL",
        "bol-app-operation-name": "CheckoutUpdatePaymentChoiceMutation",
        "bol-client-app-name": "checkout-web-fe",
    }
    if cd.get("xsrf"):
        h["x-xsrf-token"] = cd["xsrf"]
    if cd.get("page_id"):
        h["bol-client-page-id"] = cd["page_id"]
        h["m2-page-id"] = cd["page_id"]

    for body in (
        {
            "extensions": {
                "persistedQuery": {"sha256Hash": IDEAL_CHOICE_HASH, "version": 1}
            },
            "operationName": "CheckoutUpdatePaymentChoiceMutation",
            "variables": variables,
        },
        {
            "operationName": "CheckoutUpdatePaymentChoiceMutation",
            "variables": variables,
            "query": UPDATE_PAYMENT_CHOICE_QUERY,
        },
    ):
        r = pm.post(GRAPHQL_URL, checkout=True, json=body, headers=h)
        data = r.json()
        if data.get("errors"):
            continue
        result = (data.get("data") or {}).get("paymentOfferings", {}).get(
            "updatePaymentChoice"
        )
        if isinstance(result, dict) and "Problem" not in str(
            result.get("__typename") or ""
        ):
            return True
    return False


def execute_plan(pm: ProxyManager, cd: dict) -> Optional[dict]:
    h = {**JSON_HEADERS, "referer": CHECKOUT_PAGE_URL}
    if cd.get("xsrf"):
        h["x-xsrf-token"] = cd["xsrf"]
    r = pm.post(
        EXECUTE_PAYMENT_URL,
        checkout=True,
        json={
            "orderCandidateHash": cd["orderCandidateHash"],
            "paymentPlanId": cd["paymentPlanId"],
            "encryptedSecurityCode": "",
            "rsAnonymousId": "",
            "rsSessionId": cd.get("rsSessionId", int(time.time() * 1000)),
        },
        headers=h,
    )
    inner = r.json()
    if isinstance(inner, dict) and inner.get("callbackPath"):
        return inner
    data = inner.get("data") if isinstance(inner, dict) else None
    if isinstance(data, dict) and data.get("callbackPath"):
        return data
    print(f"execute-payment-plan: {str(inner)[:400]}")
    return None


def ideal_redirect(pm: ProxyManager, cd: dict, pay: dict) -> Optional[str]:
    h = {**FORM_HEADERS, "referer": CHECKOUT_PAGE_URL}
    if cd.get("xsrf"):
        h["x-xsrf-token"] = cd["xsrf"]
    url = pay.get("redirectUrl", "https://www.bol.com/nl/payment-execution/")
    r = pm.checkout_session.post(
        url,
        data={
            "client-callback-path": pay.get("callbackPath", ""),
            "encrypted-security-code": "",
            "payment-plan-id": pay.get("paymentPlanId") or cd["paymentPlanId"],
            "hash": pay.get("hash", ""),
        },
        headers=h,
        allow_redirects=False,
    )
    if r.status_code in (301, 302, 303, 307, 308):
        return r.headers.get("location") or r.headers.get("Location")
    return None


def is_bank_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(
        k in u
        for k in (
            "ideal.ing",
            "ideal.nl",
            "pay.ideal",
            "rabobank",
            "ing.nl",
            "abnamro",
            "adyen.com",
        )
    )


def do_checkout(
    pm: ProxyManager, product_url: str, basket_id: Optional[str] = None
) -> Optional[str]:
    for attempt in range(3):
        try:
            cd = checkout_page(pm, product_url)
            if not cd:
                print("Checkout page missing orderCandidateHash")
                continue
            if not cd.get("paymentPlanId") and basket_id:
                plan = create_payment_offering(pm, basket_id, cd, product_url)
                if plan:
                    cd["paymentPlanId"] = plan
            if not cd.get("paymentPlanId"):
                print("Checkout page missing paymentPlanId (HTML + GQL)")
                continue
            if not select_ideal(pm, cd):
                print("iDEAL selection failed")
                continue
            time.sleep(0.25)
            pay = execute_plan(pm, cd)
            if not pay:
                continue
            loc = ideal_redirect(pm, cd, pay)
            if loc and is_bank_url(loc):
                return loc
        except RotateProxy as e:
            print(f"Checkout blocked ({e}), rotate ({attempt + 1}/3)")
            pm.rotate()
        except CheckoutFatal as e:
            raise
        except Exception as e:
            print(f"Checkout error: {e}")
            return None
    return None


def send_discord(webhook: Optional[str], text: str) -> None:
    if not webhook:
        return

    def _send():
        body = json.dumps({"content": text[:1900]}).encode()
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def load_webhook() -> Optional[str]:
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        return os.environ["DISCORD_WEBHOOK_URL"]
    if os.path.isfile(DISCORD_WEBHOOK_FILE):
        with open(DISCORD_WEBHOOK_FILE, encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    p = ROOT / "config" / "discord.yaml"
    if p.is_file():
        try:
            import yaml

            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return d.get("webhook_url") or d.get("url")
        except Exception:
            pass
    return None


def load_product_urls() -> List[str]:
    if not os.path.isfile(PRODUCTS_FILE):
        return []
    urls = []
    with open(PRODUCTS_FILE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        field = "product_url"
        if reader.fieldnames:
            for name in reader.fieldnames:
                if name and name.strip().lower() == "product_url":
                    field = name
                    break
        for row in reader:
            u = (row.get(field) or "").strip()
            if u.startswith("http"):
                urls.append(u)
    return urls


def monitor_product(
    pm: ProxyManager,
    product_url: str,
    label: str,
    stop: threading.Event,
    webhook: Optional[str],
) -> None:
    pid = extract_product_id(product_url)
    if not pid:
        print(f"[{label}] invalid URL")
        return
    ctx: dict = {"basket_id": None, "xsrf": None, "page_id": None}
    offer_uid: Optional[str] = None
    misses = 0

    while not stop.is_set():
        try:
            info = fetch_product_gql(pm, pid, product_url, ctx)
        except RotateProxy as e:
            print(f"[{label}] GraphQL blocked ({e}) — rotate")
            pm.rotate()
            time.sleep(1)
            continue
        except Exception as e:
            print(f"[{label}] GraphQL error: {e}")
            misses += 1
            time.sleep(CHECK_DELAY)
            continue

        if not info.get("exists"):
            print(f"[{label}] product not found")
            time.sleep(30)
            continue

        if not info.get("offer_uid"):
            print(f"[{label}] stock out")
            offer_uid = None
            time.sleep(CHECK_DELAY)
            continue

        uid = info["offer_uid"]
        if REQUIRE_BOL_SELLER:
            try:
                ret = fetch_retailer_gql(pm, uid, product_url, ctx)
            except RotateProxy as e:
                print(f"[{label}] retailer blocked ({e})")
                pm.rotate()
                continue
            if not is_bol_seller(ret.get("name"), ret.get("id")):
                print(f"[{label}] seller {ret.get('name') or ret.get('id')} — skip")
                time.sleep(CHECK_DELAY)
                continue

        offer_uid = uid
        print(f"[{label}] IN STOCK offerUid={offer_uid}")

        if not warm_basket(pm, ctx):
            print(f"[{label}] basket not ready")
            time.sleep(CHECK_DELAY)
            continue

        with PURCHASE_LOCK:
            if stop.is_set():
                return
            try:
                cart = add_to_cart(pm, product_url, pid, offer_uid, ctx)
            except RotateProxy as e:
                print(f"[{label}] ATC blocked ({e})")
                pm.rotate()
                continue
            except Exception as e:
                print(f"[{label}] ATC error: {e}")
                continue

            if not cart_ok(cart):
                print(f"[{label}] ATC failed: {str(cart)[:300]}")
                time.sleep(CHECK_DELAY)
                continue

        print(f"\n{'=' * 60}\n  CART OK  product={pid}  offer={offer_uid}\n{'=' * 60}")
        send_discord(webhook, f"Cart OK: {product_url}")
        stop.set()

        try:
            os.environ["BOL_OFFER_UID"] = offer_uid
            ideal = do_checkout(pm, product_url, basket_id=ctx.get("basket_id"))
        except CheckoutFatal as e:
            print(f"FATAL: {e}")
            stop.set()
            return

        if ideal:
            print(f"\n  PAY HERE:\n  {ideal}\n")
            send_discord(webhook, f"Pay here: {ideal}")
            pid = extract_product_id(product_url) or "unknown"
            offer = os.environ.get("BOL_OFFER_UID", "").strip() or "unknown"
            line = (
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\tproductId={pid}\t"
                f"offerUid={offer}\tseller=bol\tproductUrl={product_url}\t"
                f"payUrl={ideal}\n"
            )
            with open(PAYMENT_URLS_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            print("Checkout failed — check bol.com account")
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="bol.com GraphQL fast bot")
    parser.add_argument("--product-url", help="Single product URL (overrides product.csv)")
    args = parser.parse_args()

    os.chdir(ROOT)
    cookies = load_cookies_dict()
    proxies = load_proxies()
    pm = ProxyManager(proxies, cookies)
    webhook = load_webhook()

    if proxies:
        for i in range(len(proxies)):
            try:
                pm.get("https://www.bol.com/nl/nl/", headers=PAGE_HEADERS)
                print(f"Proxy warm-up OK ({i + 1}/{len(proxies)})")
                break
            except RotateProxy:
                pm.rotate()
        else:
            print("WARNING: all proxies blocked on homepage")

    urls = [args.product_url] if args.product_url else load_product_urls()
    if not urls:
        print(f"No products — add URLs to {PRODUCTS_FILE}")
        sys.exit(1)

    print(f"Monitoring {len(urls)} product(s) | workers={WORKERS} | delay={CHECK_DELAY}s")
    stop = threading.Event()
    threads = []
    for i, url in enumerate(urls):
        for w in range(WORKERS):
            label = f"{i + 1}.{w + 1}" if WORKERS > 1 else str(i + 1)
            t = threading.Thread(
                target=monitor_product,
                args=(pm, url, label, stop, webhook),
                daemon=True,
            )
            t.start()
            threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()

    for t in threads:
        t.join(timeout=2)


if __name__ == "__main__":
    main()
