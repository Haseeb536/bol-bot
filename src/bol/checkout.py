#!/usr/bin/env python3
"""
bol.com HTTP checkout → iDEAL redirect URL.

Uses the same session as bol_cart.py (GraphQL checkout-web-fe).
Flow: CheckoutBasketQuery → CreatePaymentOffering → UpdatePaymentChoice(IDEAL)
      → execute-payment-plan → payment-execution (303 → pay.ideal.nl)
      → fallback: createPayment GraphQL / firefly REST.

Usage:
    python bol_checkout.py
    python bol_checkout.py <basket_id>
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

from src.bol.login import (
    ROOT_DIR,
    ensure_session,
    get_cookie_value,
    has_auth_cookies,
    load_session,
    _load_json_file,
)
from src.bol.cart import (
    GRAPHQL_URL,
    _get_curl_session,
    _gql_headers,
    _graphql,
    _init_session_holder,
    _load_saved_basket_id,
    _merge_cookies_from_response,
    _page_get,
    _prime_www,
    _request,
    get_basket_id,
    parse_basket_product_ids,
)

BASKET_URL = "https://www.bol.com/nl/nl/basket/"

CHECKOUT_REF = "https://www.bol.com/nl/nl/checkout/"
CHECKOUT_BUY_NOW = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"
EXECUTE_PAYMENT_PLAN_URL = (
    "https://www.bol.com/nl/nl/rnwy/checkout/command/execute-payment-plan"
)
CLIENT_APP = "checkout-web-fe"

HASH_BASKET = "sha256:bd1b3dda5fcfba2f1ed2fa4e53afe1dfb723f308deba643f05612bcd8aa18a31"
HASH_OFFERING = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
# From checkout-web-fe bundle (_checkout_bundle.js gb / hb)
HASH_IDEAL = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
_BNPL_SUGGESTION_TYPES = frozenset(
    {"BuyNowPayLaterSuggestion", "BnplPaymentSuggestion"}
)
_POST_PAYMENT_SUGGESTION_TYPES = frozenset({"PostPaymentSuggestion"})
_AFTERPAY_SUGGESTION_TYPES = _BNPL_SUGGESTION_TYPES | _POST_PAYMENT_SUGGESTION_TYPES
_IDEAL_SUGGESTION_TYPES = frozenset({"IdealPaymentSuggestion"})
_AUTH_COOKIE_NAMES = frozenset(
    {
        "BUI",
        "DYN_USER_ID",
        "DYN_USER_CONFIRM",
        "shopping_session_id",
        "XSRF-TOKEN",
        "bltgSessionId",
    }
)
IDEAL_SELECTION_ATTEMPTS = 3
IDEAL_SELECTION_RETRY_DELAY = 0.25
IDEAL_SELECTION_SETTLE = 0.25


def _skip_checkout_prime() -> bool:
    return os.environ.get("BOL_SKIP_CHECKOUT_PRIME", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _afterpay_fast_lane() -> bool:
    """Skip redundant fetches when checkout runs immediately after ATC."""
    if os.environ.get("BOL_AFTERPAY_FAST", "1").strip().lower() in {
        "0",
        "false",
        "no",
    }:
        return False
    return _skip_checkout_prime()
# APQ sha256 of CREATE_PAYMENT_QUERY document (scripts/_hash_query.py)
HASH_CREATE_PAYMENT = (
    "sha256:70f3078015c61774dc1895f3b52cdb84fa3d9c34dc6696fbe44d16312f291f38"
)

CREATE_PAYMENT_QUERY = """
mutation CheckoutCreatePaymentMutation(
  $createPaymentInput: PaymentCreationRequest!
  $requestSource: RequestSource
) {
  paymentExecutions {
    createPayment(
      createPaymentInput: $createPaymentInput
      requestSource: $requestSource
    ) {
      __typename
      ... on Payment {
        id
        status
        paymentFollowUpAction {
          __typename
          idealActionDetails {
            redirectUrl
          }
        }
      }
      ... on PaymentExecutionProblem {
        errorCode
      }
    }
  }
}
"""

# Must match checkout-web-fe bundle (hb) for APQ sha256:26d80a5c…
UPDATE_PAYMENT_CHOICE_QUERY = """
mutation CheckoutUpdatePaymentChoiceMutation(
  $input: UpdatePaymentChoiceInput!
  $requestSource: RequestSource
) {
  paymentOfferings {
    updatePaymentChoice(input: $input, requestSource: $requestSource) {
      __typename
      ... on PaymentOffering {
        paymentOfferingMessages {
          textBundleKey
          code
        }
      }
    }
  }
}
"""


_CHECKOUT_ROOT = Path(ROOT_DIR)
_STABLE_PAGE_ID: Optional[str] = None


def _page_id() -> str:
    global _STABLE_PAGE_ID
    if _STABLE_PAGE_ID:
        return _STABLE_PAGE_ID
    creds = _load_json_file(str(_CHECKOUT_ROOT / "bol_credentials.json")) or {}
    pid = str(creds.get("page_id") or "").strip()
    if pid:
        _STABLE_PAGE_ID = pid
        return pid
    _STABLE_PAGE_ID = str(uuid.uuid4())
    return _STABLE_PAGE_ID


def _checkout_page_id(checkout_data: Optional[Dict[str, Any]] = None) -> str:
    """One page id for the whole checkout GraphQL sequence."""
    global _STABLE_PAGE_ID
    if checkout_data and checkout_data.get("page_id"):
        _STABLE_PAGE_ID = str(checkout_data["page_id"])
        return _STABLE_PAGE_ID
    return _page_id()


def _reset_checkout_session_state() -> None:
    global _STABLE_PAGE_ID
    _STABLE_PAGE_ID = None


def _apply_checkout_proxy_env() -> None:
    """Use monitor proxy when set — checkout must match ATC IP (avoids 403 / CsrfNotValid)."""
    if os.environ.get("BOL_PROXY_URL", "").strip():
        os.environ.pop("BOL_NO_PROXY", None)
    else:
        os.environ.setdefault("BOL_NO_PROXY", "1")


def _sync_checkout_auth(session: Any, checkout_data: Dict[str, Any]) -> None:
    """Keep XSRF + page id aligned with the loaded checkout page."""
    cookie_xsrf = get_cookie_value(session, "XSRF-TOKEN")
    if cookie_xsrf:
        checkout_data["xsrf"] = cookie_xsrf
    pid = _checkout_page_id(checkout_data)
    checkout_data["page_id"] = pid


def _is_rnwy_login_redirect(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("statusText") or "").lower()
        if "login" in text:
            return True
    return False


def _warm_checkout_rnwy_session(
    session: Any,
    checkout_data: Dict[str, Any],
    *,
    referer: str,
) -> None:
    """Basket + BUY_NOW refresh so execute-payment-plan sees the same session as GraphQL."""
    if _afterpay_fast_lane() and checkout_data.get("orderCandidateHash"):
        _sync_checkout_auth(session, checkout_data)
        print(
            "[checkout] rnwy warm skipped — checkout hash already from ATC session "
            f"({str(checkout_data.get('orderCandidateHash') or '')[:8]}…)"
        )
        return
    try:
        _page_get(session, BASKET_URL, referer="https://www.bol.com/nl/nl/")
        cr = _page_get(session, CHECKOUT_BUY_NOW, referer=referer)
        if cr.status_code == 200 and len(cr.text or "") >= 5_000:
            parsed = _parse_checkout_page_html(cr.text, session)
            if parsed:
                checkout_data.update(parsed)
                checkout_data["checkout_url"] = CHECKOUT_BUY_NOW
        _sync_checkout_auth(session, checkout_data)
        if not checkout_data.get("orderCandidateHash"):
            _ensure_checkout_order_hash(session, checkout_data)
        if checkout_data.get("rsAnonymousId") in (None, ""):
            checkout_data["rsAnonymousId"] = _extract_rs_anonymous_id(
                session, cr.text if cr.status_code == 200 else ""
            )
        print(
            f"[checkout] rnwy session warm: xsrf={'yes' if checkout_data.get('xsrf') else 'no'} "
            f"hash={str(checkout_data.get('orderCandidateHash') or '')[:8] or '—'}"
        )
    except Exception as exc:
        print(f"[checkout] rnwy session warm failed: {exc}")


def normalize_payment_method(method: Optional[str] = None) -> str:
    """ideal = iDEAL. bnpl = deferred pay (POST_PAYMENT achteraf betalen or BNPL bol krediet)."""
    raw = (method or os.environ.get("BOL_PAYMENT_METHOD", "ideal")).strip().lower()
    if raw in (
        "afterpay",
        "bnpl",
        "achteraf",
        "bol_krediet",
        "bol-krediet",
        "pay_later",
        "riverty",
    ):
        return "bnpl"
    return "ideal"


def _payment_method_code(method: str) -> str:
    return "BNPL" if method == "bnpl" else "IDEAL"


def _suggestion_payment_code(typename: Optional[str]) -> Optional[str]:
    if typename in _BNPL_SUGGESTION_TYPES:
        return "BNPL"
    if typename in _POST_PAYMENT_SUGGESTION_TYPES:
        return "POST_PAYMENT"
    return None


def _resolve_afterpay_payment_codes(
    offering: Optional[Dict[str, Any]],
) -> list[str]:
    """
    Map offering suggestions to GraphQL PaymentMethodCode values.
    Books often expose PostPayment (achteraf betalen), not BNPL (bol krediet).
    """
    codes: list[str] = []
    if isinstance(offering, dict):
        for suggestion in offering.get("paymentSuggestions") or []:
            if not isinstance(suggestion, dict):
                continue
            code = _suggestion_payment_code(suggestion.get("__typename"))
            if code and code not in codes:
                codes.append(code)
    if not codes:
        return ["POST_PAYMENT", "BNPL"]
    return codes


def afterpay_available_on_offering(offering: Optional[Dict[str, Any]]) -> bool:
    """True when checkout offering exposes Afterpay/BNPL or achteraf betalen."""
    if not isinstance(offering, dict):
        return False
    for suggestion in offering.get("paymentSuggestions") or []:
        if not isinstance(suggestion, dict):
            continue
        if suggestion.get("__typename") in _AFTERPAY_SUGGESTION_TYPES:
            return True
    for pm in offering.get("paymentMethods") or []:
        if not isinstance(pm, dict):
            continue
        typename = str(pm.get("__typename") or "")
        if "BuyNowPayLater" in typename or "PostPayment" in typename:
            if pm.get("available") is not False and pm.get("allowed") is not False:
                return True
    return False


def _selected_afterpay_label(offering: Optional[Dict[str, Any]]) -> str:
    if not isinstance(offering, dict):
        return "Afterpay/deferred payment"
    for suggestion in offering.get("paymentSuggestions") or []:
        if not isinstance(suggestion, dict) or not suggestion.get("selected"):
            continue
        typename = suggestion.get("__typename")
        if typename == "PostPaymentSuggestion":
            return "PostPayment (achteraf betalen)"
        if typename in _BNPL_SUGGESTION_TYPES:
            return "BNPL (bol krediet)"
    return "Afterpay/deferred payment"


def _payment_preselected(
    offering: Optional[Dict[str, Any]], method: str
) -> bool:
    if not isinstance(offering, dict):
        return False
    want = (
        _AFTERPAY_SUGGESTION_TYPES if method == "bnpl" else _IDEAL_SUGGESTION_TYPES
    )
    for suggestion in offering.get("paymentSuggestions") or []:
        if not isinstance(suggestion, dict):
            continue
        if suggestion.get("__typename") in want and suggestion.get("selected"):
            return True
    return False


def _log_offering_payment_state(
    offering: Optional[Dict[str, Any]], method: str
) -> None:
    if not isinstance(offering, dict):
        print("[checkout] no payment offering snapshot for diagnostics")
        return
    suggestions = offering.get("paymentSuggestions") or []
    if suggestions:
        labels = []
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            name = str(suggestion.get("__typename") or "?")
            if suggestion.get("selected"):
                name += "*"
            labels.append(name)
        print(f"[checkout] offering paymentSuggestions: {labels}")
    for pm in offering.get("paymentMethods") or []:
        if not isinstance(pm, dict):
            continue
        typename = str(pm.get("__typename") or "")
        if method == "bnpl" and "BuyNowPayLater" in typename:
            print(
                "[checkout] BNPL method: "
                f"allowed={pm.get('allowed')} "
                f"available={pm.get('available')} "
                f"reasons={pm.get('notAllowedReasons')}"
            )
        elif method == "bnpl" and "PostPayment" in typename:
            print(
                "[checkout] PostPayment method: "
                f"allowed={pm.get('allowed')} "
                f"available={pm.get('available')} "
                f"reasons={pm.get('notAllowedReasons')}"
            )
        elif method == "ideal" and "Ideal" in typename:
            print(
                "[checkout] iDEAL method: "
                f"allowed={pm.get('allowed')} "
                f"available={pm.get('available')}"
            )


def _merge_saved_auth_cookies(session: Any) -> bool:
    """Re-apply bol_token.json auth cookies before rnwy execute-payment-plan."""
    loaded = load_session()
    if not loaded:
        return has_auth_cookies(session)
    _, meta = loaded
    cookies = meta.get("cookies") or {}
    if not isinstance(cookies, dict):
        return has_auth_cookies(session)
    merged = 0
    for name in _AUTH_COOKIE_NAMES:
        value = cookies.get(name)
        if value:
            session.cookies.set(name, str(value), domain=".bol.com")
            merged += 1
    if merged:
        print(f"[checkout] merged {merged} auth cookies from bol_token.json")
    return has_auth_cookies(session)


def _ensure_rnwy_logged_in(
    session: Any,
    checkout_data: Dict[str, Any],
    *,
    referer: str,
) -> bool:
    if not _merge_saved_auth_cookies(session):
        print(
            "[checkout] rnwy auth: no BUI cookie — export login.txt / "
            "bol_token.json from logged-in Chrome on the same proxy"
        )
        return False
    try:
        acct = _page_get(
            session,
            "https://www.bol.com/nl/account/overzicht/",
            referer=referer,
        )
        final_url = str(getattr(acct, "url", "") or "").lower()
        if "login" in final_url:
            print("[checkout] rnwy auth: account page redirected to login")
            return False
    except Exception as exc:
        print(f"[checkout] rnwy auth account check: {exc}")
    _warm_checkout_rnwy_session(session, checkout_data, referer=referer)
    return has_auth_cookies(session)


def _ideal_preselected(offering: Optional[Dict[str, Any]]) -> bool:
    return _payment_preselected(offering, "ideal")


def _resolve_basket_id(session: Any, page_id: str, arg: Optional[str]) -> str:
    if arg:
        return arg
    bid = _load_saved_basket_id()
    if bid:
        return bid
    creds = _load_json_file(str(_CHECKOUT_ROOT / "bol_credentials.json")) or {}
    if creds.get("basket_id"):
        return str(creds["basket_id"])
    return get_basket_id(session, page_id, referer=CHECKOUT_REF)


def _prepare_fresh_checkout_basket(
    session: Any,
    *,
    product_id: str,
    offer_uid: str,
    quantity: int = 1,
    product_url: str = "",
) -> str:
    """
    Create a new basket and re-add the product so orderCandidateHash / offering
    are not stuck (fixes execute-payment-plan 400055 on reused basket 73d8079b…).
    """
    from src.bol.cart import _create_basket_id, _save_basket_id, add_to_cart

    page_id = str(uuid.uuid4())
    referer = (product_url or "").strip() or CHECKOUT_REF
    bid = _create_basket_id(session, page_id, referer=referer)
    if not bid:
        print("[checkout] CreateBasket failed — using existing basket id")
        return _resolve_basket_id(session, page_id, None)

    try:
        add_to_cart(
            session,
            str(product_id),
            str(offer_uid),
            bid,
            int(quantity),
            referer=referer,
        )
        print(f"[checkout] fresh basket ready: {bid}")
    except Exception as exc:
        err = str(exc).lower()
        if "already" in err or "in cart" in err:
            print(f"[checkout] fresh basket {bid} (product already in cart)")
        else:
            print(f"[checkout] re-ATC on fresh basket: {exc}")

    _save_basket_id(bid)
    time.sleep(0.4)
    creds_path = _CHECKOUT_ROOT / "bol_credentials.json"
    try:
        creds = _load_json_file(str(creds_path)) or {}
        creds["basket_id"] = bid
        creds["product_id"] = str(product_id)
        creds["offer_uid"] = str(offer_uid)
        creds_path.write_text(
            json.dumps(creds, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    return bid


def _clear_checkout_offering_state(checkout_data: Dict[str, Any]) -> None:
    """Drop cached payment offering so CreatePaymentOffering runs with fresh basket state."""
    for key in ("paymentPlanId", "paymentOffering", "_last_offering", "_execute_error"):
        checkout_data.pop(key, None)


def _resolve_checkout_product_context(
    *,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity: int = 1,
    product_referer: Optional[str] = None,
) -> Tuple[str, str, int, str]:
    pid = (product_id or "").strip()
    ouid = (offer_uid or "").strip()
    creds = _load_json_file(str(_CHECKOUT_ROOT / "bol_credentials.json")) or {}
    if not pid:
        pid = str(creds.get("product_id") or "").strip()
        if not pid and creds.get("product_url"):
            m = re.search(r"/(\d{10,20})/?", str(creds["product_url"]))
            if m:
                pid = m.group(1)
    if not ouid:
        ouid = str(creds.get("offer_uid") or creds.get("offerUid") or "").strip()
    qty = max(1, int(quantity or creds.get("quantity") or 1))
    referer = (product_referer or creds.get("product_url") or "").strip() or CHECKOUT_REF
    return pid, ouid, qty, referer


def _checkout_retry_after_400055(
    session: Any,
    *,
    basket_id: str,
    referer: str,
    checkout_data: Optional[Dict[str, Any]],
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity: int = 1,
    product_referer: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Re-ATC + fresh offering when execute-payment-plan returns 400055 stale basket."""
    pid, ouid, qty, pref = _resolve_checkout_product_context(
        product_id=product_id,
        offer_uid=offer_uid,
        quantity=quantity,
        product_referer=product_referer,
    )
    if not pid or not ouid:
        return None, "execute-payment-plan 400055 (no product context for retry)"

    print(
        "[checkout] 400055 stale basket — re-ATC + fresh payment offering..."
    )
    bid = _prepare_fresh_checkout_basket(
        session,
        product_id=pid,
        offer_uid=ouid,
        quantity=qty,
        product_url=pref,
    )
    bid, referer, _html_len, peek = _prime_checkout_session(
        session,
        basket_id=bid,
        product_referer=product_referer or pref,
    )
    peek = peek or checkout_data or {}
    _clear_checkout_offering_state(peek)
    _sync_checkout_auth(session, peek)
    if not peek.get("orderCandidateHash"):
        _ensure_checkout_order_hash(session, peek)
    _checkout_basket_query_sync(session, peek)
    _warm_checkout_rnwy_session(session, peek, referer=referer)
    _clear_checkout_offering_state(peek)
    return _rnwy_checkout_single_pass(
        session,
        basket_id=bid or basket_id,
        referer=referer,
        checkout_data=peek,
        payment_method=payment_method,
        product_id=pid,
    )


def _should_prepare_fresh_basket(basket_id: Optional[str] = None) -> bool:
    """Reuse ATC basket by default — fresh basket only when forced or no basket id."""
    if os.environ.get("BOL_FORCE_FRESH_BASKET", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    if os.environ.get("BOL_SKIP_FRESH_BASKET", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return False
    return not bool((basket_id or "").strip())


def _is_bank_redirect(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        from src.checkout.playwright_flow import is_ideal_payment_url

        return is_ideal_payment_url(url)
    except Exception:
        u = url.lower()
        return any(
            k in u
            for k in (
                "ideal.ing",
                "ideal.nl",
                "adyen.com",
                "checkout.ideal",
                "rabobank",
                "ing.nl",
                "abnamro",
            )
        )


def _extract_ideal_from_html(html: str) -> Optional[str]:
    patterns = (
        r'https://pay\.ideal\.nl[^\s"\'\\<>]+',
        r'https://[^"\'\\s]*ideal\.ing\.nl[^\s"\'\\<>]+',
        r'"redirectUrl"\s*:\s*"([^"]+)"',
        r'"redirect_url"\s*:\s*"([^"]+)"',
        r'location\.href\s*=\s*["\']([^"\']+)',
        r'https?://[^\s"\'<>]+ideal[^\s"\'<>]*',
    )
    for pat in patterns:
        for m in re.findall(pat, html, re.I):
            url = m if isinstance(m, str) else m[0]
            url = url.replace("\\/", "/").replace("\\u0026", "&")
            if _is_bank_redirect(url):
                return url
    return None


def _resolve_ideal_bank_url(
    session: Any,
    *,
    headers: Dict[str, str],
    offering_id: str,
    referer: str,
) -> Optional[str]:
    """GET payment-execution page and follow redirects until pay.ideal.nl."""
    paths = (
        f"https://www.bol.com/nl/nl/payment-execution/?offeringId={offering_id}&paymentMethod=IDEAL",
        f"https://www.bol.com/nl/payment-execution/?offeringId={offering_id}&paymentMethod=IDEAL",
    )
    for url in paths:
        try:
            resp = _request(
                session,
                "GET",
                url,
                headers=headers,
                timeout=45,
                allow_redirects=True,
            )
            _merge_cookies_from_response(resp, session)
            final = getattr(resp, "url", None) or url
            if _is_bank_redirect(final):
                print(f"[checkout] iDEAL redirect (GET): {final[:100]}")
                return final
            found = _extract_ideal_from_html(resp.text or "")
            if found:
                print(f"[checkout] iDEAL URL scraped from payment-execution HTML")
                return found
            followed = _follow_ideal_redirect_chain(session, final, headers)
            if followed:
                return followed
        except Exception as exc:
            print(f"[checkout] payment-execution GET {url[:60]}: {exc}")
    return None


def _try_payment_execution_page(
    session: Any, offering_id: str
) -> Tuple[Optional[str], str]:
    """After execute-payment-plan, follow payment-execution to pay.ideal.nl."""
    headers = _gql_headers(CHECKOUT_REF, client_app=CLIENT_APP)
    xsrf = get_cookie_value(session, "XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    url = _resolve_ideal_bank_url(
        session,
        headers=headers,
        offering_id=str(offering_id),
        referer=CHECKOUT_REF,
    )
    if url:
        return url, "payment-execution GET → pay.ideal.nl"
    return None, "payment-execution page had no pay.ideal.nl URL"


def _extract_ideal_url(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("redirectUrl", "redirect_url", "paymentUrl", "payment_url", "url"):
        val = payload.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    for val in payload.values():
        found = _extract_ideal_url(val)
        if found:
            return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_ideal_url(item)
            if found:
                return found
    return None


def _parse_create_payment_response(data: dict) -> Tuple[Optional[str], str]:
    if data.get("errors"):
        return None, json.dumps(data["errors"])[:800]
    pay = (
        (data.get("data") or {})
        .get("paymentExecutions", {})
        .get("createPayment")
    )
    if not pay:
        return None, json.dumps(data)[:800]
    url = _extract_ideal_url(pay)
    if url:
        return url, "ok"
    return None, json.dumps(pay)[:800]


def _create_payment_graphql(
    session: Any,
    offering_id: str,
    page_id: str,
    *,
    bank_id: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """createPayment via APQ persisted hash, then full query fallback."""
    create_input: Dict[str, Any] = {
        "offeringId": str(offering_id),
        "clientCallBackPath": "/nl/nl/checkout/",
        "returnUrlDetails": {
            "hostName": "www.bol.com",
            "path": "/nl/payment-execution/return",
            "pathSegments": ["ideal"],
        },
    }

    variables = {
        "createPaymentInput": create_input,
        "requestSource": "CHECKOUT",
    }

    cs = _get_curl_session(session)
    last_detail = "graphql createPayment failed"
    for app in (CLIENT_APP, "payment-web-fe", "payment-execution-web-fe"):
        headers = _gql_headers(CHECKOUT_REF, client_app=app)
        headers["bol-app-operation-name"] = "CheckoutCreatePaymentMutation"
        headers["bol-client-page-id"] = page_id
        headers["m2-page-id"] = page_id
        xsrf = get_cookie_value(session, "XSRF-TOKEN")
        if xsrf:
            headers["x-xsrf-token"] = xsrf

        # 1) Persisted query (APQ)
        apq_body = {
            "operationName": "CheckoutCreatePaymentMutation",
            "variables": variables,
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": HASH_CREATE_PAYMENT},
            },
        }
        resp = cs.post(GRAPHQL_URL, json=apq_body, headers=headers, timeout=45)
        _merge_cookies_from_response(resp, session)
        try:
            data = resp.json()
        except Exception:
            continue
        if resp.status_code == 200:
            url, detail = _parse_create_payment_response(data)
            if url:
                return url, f"graphql-apq ({app})"
            last_detail = f"apq {app}: {detail}"
            print(f"[checkout] createPayment APQ ({app}): {detail[:300]}")
        else:
            last_detail = f"apq HTTP {resp.status_code}: {resp.text[:300]}"

        # 2) Full query document
        full_body = {
            "operationName": "CheckoutCreatePaymentMutation",
            "variables": variables,
            "query": CREATE_PAYMENT_QUERY,
        }
        resp2 = cs.post(GRAPHQL_URL, json=full_body, headers=headers, timeout=45)
        _merge_cookies_from_response(resp2, session)
        try:
            data2 = resp2.json()
        except Exception:
            continue
        if resp2.status_code == 200:
            url, detail = _parse_create_payment_response(data2)
            if url:
                return url, f"graphql-full ({app})"
            last_detail = f"full {app}: {detail}"
            print(f"[checkout] createPayment full ({app}): {detail[:300]}")
        else:
            last_detail = f"full HTTP {resp2.status_code}: {resp2.text[:300]}"

    return None, last_detail


def _bundle_persisted_hashes() -> list[str]:
    """sha256 hashes from saved checkout remix bundle (for createPayment brute)."""
    paths = (
        _CHECKOUT_ROOT / "_checkout_bundle.js",
        _CHECKOUT_ROOT / "scripts" / "_checkout_bundle.js",
    )
    out: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        out.extend(re.findall(r"sha256:[a-f0-9]{64}", text))
    return sorted(set(out))


def _try_brute_create_payment(
    session: Any,
    offering_id: str,
    page_id: str,
) -> Tuple[Optional[str], str]:
    """Try persisted hashes from checkout JS bundle for createPayment."""
    hashes = _bundle_persisted_hashes()
    if not hashes:
        return None, "no bundle hashes"

    create_input: Dict[str, Any] = {
        "offeringId": str(offering_id),
        "clientCallBackPath": "/nl/nl/checkout/",
        "returnUrlDetails": {
            "hostName": "www.bol.com",
            "path": "/nl/payment-execution/return",
            "pathSegments": ["ideal"],
        },
    }
    variables = {"createPaymentInput": create_input, "requestSource": "CHECKOUT"}
    skip = {HASH_BASKET, HASH_OFFERING, HASH_IDEAL, HASH_CREATE_PAYMENT}

    cs = _get_curl_session(session)
    for app in (CLIENT_APP, "payment-web-fe", "payment-execution-web-fe"):
        headers = _gql_headers(CHECKOUT_REF, client_app=app)
        headers["bol-app-operation-name"] = "CheckoutCreatePaymentMutation"
        headers["bol-client-page-id"] = page_id
        headers["m2-page-id"] = page_id
        xsrf = get_cookie_value(session, "XSRF-TOKEN")
        if xsrf:
            headers["x-xsrf-token"] = xsrf

        for h in hashes:
            if h in skip:
                continue
            body = {
                "operationName": "CheckoutCreatePaymentMutation",
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}},
            }
            try:
                resp = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
                _merge_cookies_from_response(resp, session)
                data = resp.json()
            except Exception:
                continue
            if resp.status_code != 200 or data.get("errors"):
                continue
            url, _ = _parse_create_payment_response(data)
            if url:
                print(f"[checkout] createPayment HIT hash={h[:24]} app={app}")
                return url, f"graphql-brute ({app})"
    return None, "bundle brute: no createPayment hash"


def _create_payment_firefly(
    session: Any,
    offering_id: str,
    *,
    bank_id: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    body: Dict[str, Any] = {
        "offeringId": str(offering_id),
        "paymentMethodCode": "IDEAL",
        "clientCallBackPath": "/nl/nl/checkout/",
        "returnUrlDetails": {
            "hostName": "www.bol.com",
            "path": "/nl/payment-execution/return",
            "pathSegments": ["ideal"],
        },
    }
    if bank_id:
        body["idealDetails"] = {"bankId": bank_id}

    xsrf = get_cookie_value(session, "XSRF-TOKEN")
    bui = get_cookie_value(session, "BUI")
    from src.bol.login import DEFAULT_HEADERS

    ua = DEFAULT_HEADERS.get("User-Agent", "")
    header_sets = [
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "nl-NL,en;q=0.9",
            "Origin": "https://www.bol.com",
            "Referer": CHECKOUT_REF,
            "bol-client-app-name": "checkout-web-fe",
            "bol-app-country": "NL",
            "bol-app-operation-name": "createPayment",
            "User-Agent": ua,
        },
        {
            "Accept": "application/vnd.bol.api+json",
            "Content-Type": "application/vnd.bol.api+json",
            "Accept-Language": "nl-NL,en;q=0.9",
            "Origin": "https://www.bol.com",
            "Referer": CHECKOUT_REF,
            "bol-client-app-name": "payment-web-fe",
            "User-Agent": ua,
        },
    ]
    cs = _get_curl_session(session)
    for headers in header_sets:
        if xsrf:
            headers["x-xsrf-token"] = xsrf
        if bui:
            headers["BOL-SHOP-IDENTITY"] = bui
        for url in (
            "https://firefly.bol.com/payment/v1/create",
            "https://firefly.bol.com/payment/v1/payments",
        ):
            resp = cs.post(url, json=body, headers=headers, timeout=45)
            _merge_cookies_from_response(resp, session)
            try:
                data = resp.json()
            except Exception:
                continue
            url_found = _extract_ideal_url(data)
            if url_found:
                return url_found, f"firefly {url}"
            if resp.status_code in (200, 201) and isinstance(data, dict):
                return None, json.dumps(data)[:500]
            print(f"[checkout] firefly {url} -> {resp.status_code} {resp.text[:200]}")
    return None, "firefly create failed"


def _extract_rs_anonymous_id(session: Any, html: str = "") -> Any:
    """RudderStack anonymous id — required by execute-payment-plan (standalone bot parity)."""
    anon_cookie = get_cookie_value(session, "rl_anonymous_id")
    if anon_cookie:
        try:
            raw = unquote(anon_cookie)
            if raw.startswith("RS_ENC_v3_"):
                padded = raw[len("RS_ENC_v3_") :]
                decoded = base64.b64decode(padded + "==").decode("utf-8")
                return json.loads(decoded)
        except Exception:
            pass
    if html:
        dehydrated = _extract_dehydrated(html, "anonymousId")
        if dehydrated:
            return dehydrated
    return ""


def _extract_dehydrated(html: str, key: str) -> Optional[str]:
    """Remix SSR dehydrated state (double-escaped JSON in HTML)."""
    m = re.search(r'\\\"' + re.escape(key) + r'\\\"[,\s]*\\\"([^\\\"]+)\\\"', html)
    if m:
        return m.group(1)
    m = re.search(r'"' + re.escape(key) + r'"\s*[,:]\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _parse_payment_plan_id_from_html(html: str) -> Optional[str]:
    """Legacy dehydrated PaymentOffering id; bol checkout HTML often omits it now."""
    patterns = (
        r'\\\"PaymentOffering\\\"[,\s]*\\\"(\d{6,12})\\\"',
        r'"PaymentOffering"[,\s]*"(\d{6,12})"',
        r'paymentOfferingId["\s:,\\]+"?(\d{6,12})',
        r'paymentOfferingId\\":\\"?(\d{6,12})',
        r'PaymentOffering[^}]{0,200}?[\\"]id[\\"]\s*[,:]\s*[\\"](\d{6,12})',
    )
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None


def _create_payment_offering_id(
    session: Any,
    basket_id: str,
    checkout_data: Dict[str, Any],
) -> Optional[str]:
    """GraphQL fallback when checkout HTML has no PaymentOffering id (new Remix SSR)."""
    _sync_checkout_auth(session, checkout_data)
    page_id = _checkout_page_id(checkout_data)
    referer = checkout_data.get("checkout_url") or CHECKOUT_REF
    for subject_type in ("ORDER",):
        try:
            off = _graphql(
                session,
                "CheckoutCreatePaymentOfferingMutation",
                HASH_OFFERING,
                variables={
                    "input": {
                        "subjects": [{"id": basket_id, "type": subject_type}],
                    },
                    "requestSource": "CHECKOUT",
                },
                page_id=page_id,
                label=f"checkout_offering_{subject_type.lower()}",
                referer=referer,
                client_app=CLIENT_APP,
            )
            offering = (off or {}).get("paymentOfferings", {}).get(
                "createPaymentOffering"
            )
            if not isinstance(offering, dict):
                continue
            typename = str(offering.get("__typename") or "")
            if "Problem" in typename:
                print(
                    f"[checkout] CreatePaymentOffering {subject_type} problem: "
                    f"{str(offering)[:300]}"
                )
                continue
            oid = offering.get("id")
            if oid:
                print(
                    f"[checkout] paymentPlanId from CreatePaymentOffering "
                    f"({subject_type})={oid}"
                )
                checkout_data["paymentPlanId"] = str(oid)
                checkout_data["_last_offering"] = offering
                checkout_data["paymentOffering"] = offering
                suggestions = offering.get("paymentSuggestions") or []
                if suggestions:
                    labels = []
                    for suggestion in suggestions:
                        if not isinstance(suggestion, dict):
                            continue
                        name = str(suggestion.get("__typename") or "?")
                        if suggestion.get("selected"):
                            name += "*"
                        labels.append(name)
                    print(
                        f"[checkout] CreatePaymentOffering suggestions: {labels}"
                    )
                if _payment_preselected(offering, "bnpl"):
                    print(
                        "[checkout] "
                        f"{_selected_afterpay_label(offering)} already selected "
                        "on payment offering"
                    )
                elif _ideal_preselected(offering):
                    print("[checkout] iDEAL already selected on payment offering")
                return str(oid)
        except Exception as exc:
            print(f"[checkout] CreatePaymentOffering ({subject_type}): {exc}")
    return None


def _parse_checkout_page_html(html: str, session: Any) -> Optional[Dict[str, Any]]:
    order_hash = None
    hash_patterns = (
        r'\\\"hash\\\"[,\s]*\\\"([a-f0-9]{32})\\\"',
        r'"hash"\s*[,:]\s*"([a-f0-9]{32})"',
        r'\\"hash\\"[,\s]*\\"([a-f0-9]{32})\\"',
        r'orderCandidateHash\\":\\"([a-f0-9]{32})\\"',
        r'orderCandidateHash["\s:]+["\']([a-f0-9]{32})["\']',
        r'"orderCandidateHash"\s*:\s*"([a-f0-9]{32})"',
        r'([a-f0-9]{32})',
    )
    for pat in hash_patterns[:-1]:
        m = re.search(pat, html)
        if m:
            order_hash = m.group(1)
            break

    payment_plan_id = _parse_payment_plan_id_from_html(html)

    xsrf = _extract_dehydrated(html, "xsrf") or get_cookie_value(session, "XSRF-TOKEN")
    page_id = _extract_dehydrated(html, "pageId")

    if not order_hash:
        return None
    return {
        "orderCandidateHash": order_hash,
        "paymentPlanId": payment_plan_id,
        "xsrf": xsrf,
        "page_id": page_id,
        "rsAnonymousId": _extract_rs_anonymous_id(session, html),
        "rsSessionId": int(time.time() * 1000),
    }


def _fetch_checkout_page_data(
    session: Any, referer: str = CHECKOUT_REF
) -> Optional[Dict[str, Any]]:
    for url in (CHECKOUT_BUY_NOW, CHECKOUT_REF):
        try:
            resp = _page_get(session, url, referer=referer)
            text_len = len(resp.text or "")
            if resp.status_code != 200 or text_len < 5_000:
                print(
                    f"[checkout] checkout page {url[:50]} -> {resp.status_code} "
                    f"len={text_len}"
                )
                continue
            data = _parse_checkout_page_html(resp.text, session)
            if data:
                data["checkout_url"] = url
                print(
                    f"[checkout] parsed checkout page: hash={data['orderCandidateHash'][:8]}… "
                    f"plan={data.get('paymentPlanId')}"
                )
                return data
            print(
                f"[checkout] checkout page {url[:50]} had no hash "
                f"(status={resp.status_code} len={text_len})"
            )
        except Exception as exc:
            print(f"[checkout] checkout page fetch {url[:40]}: {exc}")
    return None


def _ensure_checkout_order_hash(
    session: Any, checkout_data: Dict[str, Any]
) -> bool:
    """Fill orderCandidateHash from CheckoutBasketQuery when HTML parse fails."""
    if checkout_data.get("orderCandidateHash"):
        return True
    basket_hash = _checkout_basket_query_sync(session, checkout_data)
    return bool(basket_hash or checkout_data.get("orderCandidateHash"))


def _checkout_gql_post(
    session: Any,
    operation: str,
    sha_hash: str,
    variables: dict,
    checkout_data: Dict[str, Any],
) -> Optional[dict]:
    _sync_checkout_auth(session, checkout_data)
    page_id = _checkout_page_id(checkout_data)
    headers = _gql_headers(
        checkout_data.get("checkout_url") or CHECKOUT_REF,
        client_app=CLIENT_APP,
    )
    headers["bol-app-operation-name"] = operation
    headers["bol-client-page-id"] = page_id
    headers["m2-page-id"] = page_id
    xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    body = {
        "operationName": operation,
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha_hash}},
    }
    cs = _get_curl_session(session)
    resp = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=45)
    _merge_cookies_from_response(resp, session)
    try:
        return resp.json()
    except Exception:
        return None


def _coerce_offering_id(payment_offering_id: Any) -> str:
    return str(payment_offering_id).strip()


def _payment_choice_input(
    payment_offering_id: Any,
    method: str,
    *,
    as_int: bool = False,
    payment_code: Optional[str] = None,
) -> Dict[str, Any]:
    code = payment_code or _payment_method_code(method)
    oid = _coerce_offering_id(payment_offering_id)
    offering_ref: Any = int(oid) if as_int and oid.isdigit() else oid
    choice_input: Dict[str, Any] = {
        "paymentOfferingId": offering_ref,
        "paymentMethodCode": code,
    }
    if code == "IDEAL":
        choice_input["idealDetails"] = {}
        bank_id = os.environ.get("BOL_IDEAL_BANK_ID", "").strip()
        if bank_id:
            choice_input["idealDetails"] = {"bankId": bank_id}
    return choice_input


def _payment_choice_variable_sets(
    payment_offering_id: Any,
    method: str,
    *,
    payment_code: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """APQ variable shapes: with/without requestSource, string/int offering id."""
    oid = _coerce_offering_id(payment_offering_id)
    candidates: list[Dict[str, Any]] = [
        {
            "input": _payment_choice_input(
                payment_offering_id, method, payment_code=payment_code
            ),
            "requestSource": "CHECKOUT",
        },
        {
            "input": _payment_choice_input(
                payment_offering_id, method, payment_code=payment_code
            )
        },
    ]
    if oid.isdigit():
        int_input = _payment_choice_input(
            payment_offering_id,
            method,
            as_int=True,
            payment_code=payment_code,
        )
        candidates.extend(
            [
                {"input": int_input, "requestSource": "CHECKOUT"},
                {"input": int_input},
            ]
        )
    seen: set[str] = set()
    unique: list[Dict[str, Any]] = []
    for variables in candidates:
        key = json.dumps(variables, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(variables)
    return unique


def _payment_choice_variables(
    payment_offering_id: Any, method: str
) -> Dict[str, Any]:
    return _payment_choice_variable_sets(payment_offering_id, method)[0]


def _ideal_choice_variables(payment_offering_id: Any) -> Dict[str, Any]:
    return _payment_choice_variables(payment_offering_id, "ideal")


def _parse_ideal_choice_response(data: Any) -> bool:
    """Accept full GraphQL JSON or inner data payload from _graphql()."""
    if not isinstance(data, dict):
        return False
    if data.get("errors"):
        return False
    inner = data.get("data") if "data" in data else data
    if not isinstance(inner, dict):
        return False
    result = inner.get("paymentOfferings", {}).get("updatePaymentChoice")
    if not isinstance(result, dict):
        return False
    typename = str(result.get("__typename") or "")
    if "Problem" in typename:
        msgs = result.get("paymentOfferingMessages") or []
        if msgs:
            print(f"[checkout] iDEAL choice problem: {msgs[:2]}")
        return False
    return True


def _log_ideal_gql_failure(label: str, data: Any) -> None:
    if not isinstance(data, dict):
        return
    if data.get("errors"):
        print(f"[checkout] {label} errors: {data['errors'][:2]}")
    inner = data.get("data") or {}
    result = (inner.get("paymentOfferings") or {}).get("updatePaymentChoice")
    if isinstance(result, dict) and result:
        print(f"[checkout] {label} data: {str(result)[:400]}")


def _select_payment_method_plan(
    session: Any,
    checkout_data: Dict[str, Any],
    payment_offering_id: Any,
    method: str,
    *,
    offering: Optional[Dict[str, Any]] = None,
) -> bool:
    """Select iDEAL or deferred payment (POST_PAYMENT / BNPL) via GraphQL."""
    method = normalize_payment_method(method)
    _sync_checkout_auth(session, checkout_data)
    page_id = _checkout_page_id(checkout_data)
    referer = checkout_data.get("checkout_url") or CHECKOUT_REF

    offering_data = (
        offering
        or checkout_data.get("paymentOffering")
        or checkout_data.get("_last_offering")
        or {}
    )
    if method == "bnpl":
        payment_codes = _resolve_afterpay_payment_codes(offering_data)
        if _afterpay_fast_lane():
            payment_codes = payment_codes[:1]
        print(f"[checkout] Afterpay payment codes to try: {payment_codes}")
    else:
        payment_codes = ["IDEAL"]

    for payment_code in payment_codes:
        label = payment_code if method == "bnpl" else "iDEAL"
        variable_sets = _payment_choice_variable_sets(
            payment_offering_id, method, payment_code=payment_code
        )
        if _afterpay_fast_lane():
            variable_sets = variable_sets[:1]

        for idx, variables in enumerate(variable_sets, start=1):
            apq = _checkout_gql_post(
                session,
                "CheckoutUpdatePaymentChoiceMutation",
                HASH_IDEAL,
                variables,
                checkout_data,
            )
            if _parse_ideal_choice_response(apq):
                checkout_data["_selected_payment_code"] = payment_code
                print(
                    f"[checkout] {label} selected via APQ "
                    f"(offering={payment_offering_id}, variant={idx})"
                )
                return True
            _log_ideal_gql_failure(f"{label} APQ variant {idx}", apq)

            try:
                payload = _graphql(
                    session,
                    "CheckoutUpdatePaymentChoiceMutation",
                    HASH_IDEAL,
                    variables=variables,
                    page_id=page_id,
                    label=f"checkout_{method}_{payment_code.lower()}",
                    referer=referer,
                    client_app=CLIENT_APP,
                )
                wrapped = {"data": payload}
                if _parse_ideal_choice_response(wrapped):
                    checkout_data["_selected_payment_code"] = payment_code
                    print(
                        f"[checkout] {label} selected via APQ retry "
                        f"(offering={payment_offering_id}, variant={idx})"
                    )
                    return True
            except Exception as exc:
                print(f"[checkout] {label} APQ retry variant {idx}: {str(exc)[:200]}")

            headers = _gql_headers(referer, client_app=CLIENT_APP)
            headers["bol-app-operation-name"] = "CheckoutUpdatePaymentChoiceMutation"
            headers["bol-client-page-id"] = page_id
            headers["m2-page-id"] = page_id
            xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
            if xsrf:
                headers["x-xsrf-token"] = xsrf

            for body_idx, body in enumerate(
                (
                    {
                        "operationName": "CheckoutUpdatePaymentChoiceMutation",
                        "variables": variables,
                        "query": UPDATE_PAYMENT_CHOICE_QUERY.strip(),
                    },
                    {
                        "operationName": "CheckoutUpdatePaymentChoiceMutation",
                        "variables": variables,
                        "query": UPDATE_PAYMENT_CHOICE_QUERY.strip(),
                        "extensions": {
                            "persistedQuery": {"version": 1, "sha256Hash": HASH_IDEAL}
                        },
                    },
                )[:1 if _afterpay_fast_lane() else 2],
                start=1,
            ):
                try:
                    cs = _get_curl_session(session)
                    resp = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=45)
                    _merge_cookies_from_response(resp, session)
                    ct = (resp.headers.get("Content-Type") or "").lower()
                    if "json" not in ct and not (resp.text or "").lstrip().startswith(
                        "{"
                    ):
                        print(
                            f"[checkout] {label} full mutation variant "
                            f"{idx}.{body_idx}: non-JSON ({resp.status_code}, {ct[:40]})"
                        )
                        continue
                    data = resp.json()
                    if _parse_ideal_choice_response(data):
                        checkout_data["_selected_payment_code"] = payment_code
                        print(
                            f"[checkout] {label} selected via full mutation "
                            f"(offering={payment_offering_id}, variant={idx}.{body_idx})"
                        )
                        return True
                    _log_ideal_gql_failure(
                        f"{label} full mutation variant {idx}.{body_idx}", data
                    )
                except Exception as exc:
                    print(
                        f"[checkout] {label} full mutation variant "
                        f"{idx}.{body_idx}: {exc}"
                    )

    return False


def _select_ideal_payment_plan(
    session: Any,
    checkout_data: Dict[str, Any],
    payment_offering_id: Any,
) -> bool:
    return _select_payment_method_plan(
        session, checkout_data, payment_offering_id, "ideal"
    )


def _execute_payment_plan(
    session: Any,
    checkout_data: Dict[str, Any],
    *,
    for_bnpl: bool = False,
) -> Optional[Dict[str, Any]]:
    plan_id = checkout_data.get("paymentPlanId")
    if not plan_id:
        print("[checkout] execute-payment-plan: missing paymentPlanId")
        return None

    headers = _gql_headers(
        checkout_data.get("checkout_url") or CHECKOUT_REF,
        client_app=CLIENT_APP,
    )
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/plain, */*"
    xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    rs_anon = checkout_data.get("rsAnonymousId")
    if rs_anon is None or rs_anon == "":
        rs_anon = _extract_rs_anonymous_id(session)
    body = {
        "orderCandidateHash": checkout_data["orderCandidateHash"],
        "paymentPlanId": str(plan_id),
        "encryptedSecurityCode": "",
        "rsAnonymousId": rs_anon or "",
        "rsSessionId": checkout_data.get("rsSessionId") or int(time.time() * 1000),
    }
    referer = checkout_data.get("checkout_url") or CHECKOUT_BUY_NOW
    headers["Referer"] = referer
    headers["Origin"] = "https://www.bol.com"

    if not _ensure_rnwy_logged_in(session, checkout_data, referer=referer):
        return None

    def _post_execute() -> Any:
        return _request(
            session,
            "POST",
            EXECUTE_PAYMENT_PLAN_URL,
            json=body,
            headers=headers,
            timeout=45,
        )

    try:
        resp = _post_execute()
        _merge_cookies_from_response(resp, session)
        data = resp.json()
        if _is_rnwy_login_redirect(data):
            print(
                "[checkout] execute-payment-plan login redirect — "
                "refreshing auth + checkout session..."
            )
            if not _ensure_rnwy_logged_in(session, checkout_data, referer=referer):
                print(
                    "[checkout] execute-payment-plan still unauthenticated — "
                    "re-export bol_token.json from checkout page (same NL proxy)"
                )
                return None
            xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
            if xsrf:
                headers["x-xsrf-token"] = xsrf
            body["orderCandidateHash"] = checkout_data.get(
                "orderCandidateHash", body["orderCandidateHash"]
            )
            body["rsAnonymousId"] = checkout_data.get("rsAnonymousId") or body.get(
                "rsAnonymousId", ""
            )
            resp = _post_execute()
            _merge_cookies_from_response(resp, session)
            data = resp.json()
            if _is_rnwy_login_redirect(data):
                print(
                    "[checkout] execute-payment-plan login redirect persisted "
                    "after auth refresh"
                )
                return None
        if isinstance(data, dict) and data.get("callbackPath"):
            print("[checkout] execute-payment-plan OK (top-level)")
            if not data.get("hash") and checkout_data.get("orderCandidateHash"):
                data["hash"] = checkout_data["orderCandidateHash"]
            if not data.get("paymentPlanId") and checkout_data.get("paymentPlanId"):
                data["paymentPlanId"] = checkout_data["paymentPlanId"]
            return data
        inner = data.get("data") if isinstance(data, dict) else data
        if isinstance(inner, dict) and inner.get("callbackPath"):
            print("[checkout] execute-payment-plan OK")
            if not inner.get("hash") and checkout_data.get("orderCandidateHash"):
                inner["hash"] = checkout_data["orderCandidateHash"]
            if not inner.get("paymentPlanId") and checkout_data.get("paymentPlanId"):
                inner["paymentPlanId"] = checkout_data["paymentPlanId"]
            return inner
        msg = str(data)[:400]
        print(f"[checkout] execute-payment-plan unexpected: {msg}")
        if "400055" in msg or "transition not allowed" in msg.lower():
            checkout_data["_execute_error"] = "400055"
            print(
                "[checkout] hint: order not ready for payment — stale basket/offering; "
                "will retry with fresh re-ATC if product context is available"
            )
    except Exception as exc:
        print(f"[checkout] execute-payment-plan failed: {exc}")
    return None


def _follow_ideal_redirect_chain(
    session: Any,
    url: str,
    headers: Dict[str, str],
    *,
    max_hops: int = 6,
) -> Optional[str]:
    """Follow bol payment redirects until pay.ideal.nl (or other bank host)."""
    from urllib.parse import urljoin

    current = url
    for hop in range(max_hops):
        if _is_bank_redirect(current):
            return current
        try:
            resp = _request(
                session,
                "GET",
                current,
                headers=headers,
                timeout=45,
                allow_redirects=False,
            )
            _merge_cookies_from_response(resp, session)
        except Exception as exc:
            print(f"[checkout] payment redirect hop {hop + 1}: {exc}")
            break

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if not loc:
                break
            current = urljoin(current, loc)
            print(f"[checkout] payment redirect {hop + 1} -> {current[:100]}")
            continue

        for pat in (
            r"https://pay\.ideal\.nl[^\s\"'<>]+",
            r"https://[^\"'\\s]*ideal\.ing\.nl[^\s\"'<>]+",
        ):
            m = re.search(pat, resp.text or "", re.I)
            if m:
                found = m.group(0).replace("\\/", "/")
                print(f"[checkout] iDEAL URL from HTML hop {hop + 1}")
                return found
        break
    return None


def _basket_contains_product(session: Any, product_id: str) -> bool:
    if not product_id:
        return False
    try:
        resp = _page_get(
            session,
            BASKET_URL,
            referer="https://www.bol.com/nl/nl/",
        )
        if resp.status_code != 200:
            return False
        return product_id in parse_basket_product_ids(resp.text or "")
    except Exception:
        return False


def _html_indicates_order_placed(html: str) -> bool:
    if not html or len(html) < 500:
        return False
    for pat in (
        r"bestelling\s+(is\s+)?geplaatst",
        r"bedankt voor je bestelling",
        r"je bestelling is bevestigd",
        r"order\s+confirmed",
        r"bestelnummer",
        r'"orderId"\s*:',
        r"order-confirmation",
    ):
        if re.search(pat, html, re.I):
            return True
    return False


def _url_indicates_order_placed(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "order-confirmation",
            "bedankt",
            "bestelling-bevestig",
            "order/confirm",
            "thank-you",
            "checkout/return",
        )
    )


def _submit_payment_execution(
    session: Any,
    checkout_data: Dict[str, Any],
    payment_data: Dict[str, Any],
) -> Tuple[int, str, str]:
    """POST bol payment-execution (same step as iDEAL). Returns status, location/body hint, html."""
    callback_path = (payment_data.get("callbackPath") or "").strip()
    hash_val = payment_data.get("hash", "") or checkout_data.get("orderCandidateHash", "")
    plan_id = payment_data.get("paymentPlanId") or checkout_data.get("paymentPlanId")
    redirect_url = (payment_data.get("redirectUrl") or "").strip()
    if not redirect_url.startswith("http"):
        redirect_url = "https://www.bol.com/nl/payment-execution/"
    xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
    referer = checkout_data.get("checkout_url") or CHECKOUT_REF

    headers = _gql_headers(referer, client_app=CLIENT_APP)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    form = {
        "client-callback-path": callback_path,
        "encrypted-security-code": "",
        "payment-plan-id": str(plan_id),
        "hash": hash_val,
    }
    try:
        resp = _request(
            session,
            "POST",
            redirect_url,
            data=form,
            headers=headers,
            timeout=45,
            allow_redirects=False,
        )
        _merge_cookies_from_response(resp, session)
        location = resp.headers.get("Location") or resp.headers.get("location") or ""
        if location.startswith("/"):
            from urllib.parse import urljoin

            location = urljoin(redirect_url, location)
        return resp.status_code, location, resp.text or ""
    except Exception as exc:
        print(f"[checkout] payment-execution POST failed: {exc}")
        return 0, "", ""


def _verify_bnpl_order_placed(
    session: Any,
    checkout_data: Dict[str, Any],
    payment_data: Dict[str, Any],
    *,
    product_id: Optional[str] = None,
) -> bool:
    """
    Afterpay HTTP must clear the basket or land on a confirmation page.
    execute-payment-plan alone only prepares payment — not a placed order.
    """
    if not payment_data.get("callbackPath"):
        print("[checkout] BNPL verify: execute-payment-plan missing callbackPath")
        return False

    status, location, html = _submit_payment_execution(
        session, checkout_data, payment_data
    )
    print(
        f"[checkout] BNPL payment-execution status={status} "
        f"location={location[:80] if location else '—'} len={len(html)}"
    )
    if _url_indicates_order_placed(location) or _html_indicates_order_placed(html):
        print("[checkout] BNPL order confirmed (confirmation page)")
        return True

    if location and not _is_bank_redirect(location):
        try:
            follow = _page_get(session, location, referer=CHECKOUT_REF)
            if _html_indicates_order_placed(follow.text or ""):
                print("[checkout] BNPL order confirmed (follow-up page)")
                return True
        except Exception as exc:
            print(f"[checkout] BNPL follow confirm page: {exc}")

    if product_id and not _afterpay_fast_lane():
        try:
            resp = _page_get(
                session,
                BASKET_URL,
                referer="https://www.bol.com/nl/nl/",
            )
            if resp.status_code == 200:
                in_basket = product_id in parse_basket_product_ids(resp.text or "")
                if not in_basket:
                    print(
                        f"[checkout] BNPL order likely placed — product {product_id} "
                        "no longer in basket"
                    )
                    return True
                print(
                    f"[checkout] BNPL NOT confirmed — product {product_id} still in basket"
                )
                return False
        except Exception as exc:
            print(f"[checkout] BNPL basket verify failed: {exc}")

    print("[checkout] BNPL NOT confirmed — no confirmation page and basket not verified")
    return False


def _ideal_url_from_payment_execution(
    session: Any,
    checkout_data: Dict[str, Any],
    payment_data: Dict[str, Any],
) -> Optional[str]:
    callback_path = (payment_data.get("callbackPath") or "").strip()
    hash_val = payment_data.get("hash", "") or checkout_data.get("orderCandidateHash", "")
    plan_id = payment_data.get("paymentPlanId") or checkout_data.get("paymentPlanId")
    redirect_url = (payment_data.get("redirectUrl") or "").strip()
    if not redirect_url.startswith("http"):
        redirect_url = "https://www.bol.com/nl/payment-execution/"
    xsrf = checkout_data.get("xsrf") or get_cookie_value(session, "XSRF-TOKEN")
    referer = checkout_data.get("checkout_url") or CHECKOUT_REF

    headers = _gql_headers(referer, client_app=CLIENT_APP)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    form = {
        "client-callback-path": callback_path,
        "encrypted-security-code": "",
        "payment-plan-id": str(plan_id),
        "hash": hash_val,
    }
    try:
        resp = _request(
            session,
            "POST",
            redirect_url,
            data=form,
            headers=headers,
            timeout=45,
            allow_redirects=False,
        )
        _merge_cookies_from_response(resp, session)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") or resp.headers.get("location")
            if location:
                if location.startswith("/"):
                    from urllib.parse import urljoin

                    location = urljoin(redirect_url, location)
                if _is_bank_redirect(location):
                    return location
                followed = _follow_ideal_redirect_chain(session, location, headers)
                if followed:
                    return followed
            print(
                f"[checkout] payment-execution POST {resp.status_code} "
                f"Location={str(location)[:120]}"
            )
        else:
            found = _extract_ideal_from_html(resp.text or "")
            if found:
                print(
                    f"[checkout] iDEAL URL scraped from payment-execution POST "
                    f"({resp.status_code})"
                )
                return found
            print(
                f"[checkout] payment-execution POST status={resp.status_code} "
                f"len={len(resp.text or '')}"
            )

        resp_follow = _request(
            session,
            "POST",
            redirect_url,
            data=form,
            headers=headers,
            timeout=45,
            allow_redirects=True,
        )
        _merge_cookies_from_response(resp_follow, session)
        final = getattr(resp_follow, "url", None) or ""
        if _is_bank_redirect(final):
            print(f"[checkout] iDEAL URL after POST follow: {final[:100]}")
            return final
        found = _extract_ideal_from_html(resp_follow.text or "")
        if found:
            return found
    except Exception as exc:
        print(f"[checkout] payment-execution POST: {exc}")

    if plan_id:
        got = _resolve_ideal_bank_url(
            session,
            headers=headers,
            offering_id=str(plan_id),
            referer=referer,
        )
        if got:
            return got
    return None


def _checkout_basket_query_sync(
    session: Any, checkout_data: Dict[str, Any]
) -> Optional[str]:
    existing = str(checkout_data.get("orderCandidateHash") or "").strip()
    if _afterpay_fast_lane() and existing:
        print(
            f"[checkout] CheckoutBasketQuery skipped — hash already set ({existing[:8]}…)"
        )
        return existing
    page_id = checkout_data.get("page_id") or _page_id()
    referer = checkout_data.get("checkout_url") or CHECKOUT_REF
    try:
        result = _graphql(
            session,
            "CheckoutBasketQuery",
            HASH_BASKET,
            variables={},
            page_id=page_id,
            label="checkout_basket",
            referer=referer,
            client_app=CLIENT_APP,
        )
    except Exception as exc:
        print(f"[checkout] CheckoutBasketQuery: {exc}")
        return None

    me = (result or {}).get("me") if isinstance(result, dict) else None
    if not isinstance(me, dict) or me.get("__typename") != "IdentifiedCustomer":
        return None

    baskets = me.get("baskets") or []
    if not baskets or not isinstance(baskets[0], dict):
        return None

    basket = baskets[0]
    basket_hash = str(basket.get("hash") or "").strip()
    basket_id = str(basket.get("id") or "").strip()
    if basket_id:
        checkout_data["basket_id"] = basket_id
    if basket_hash:
        checkout_data["orderCandidateHash"] = basket_hash
        print(
            f"[checkout] orderCandidateHash from CheckoutBasketQuery: "
            f"{basket_hash[:8]}…"
        )
        return basket_hash
    return None


def _refresh_order_hash(
    session: Any,
    checkout_data: Dict[str, Any],
    referer: str,
) -> None:
    if _afterpay_fast_lane() and checkout_data.get("orderCandidateHash"):
        return
    fresh = _fetch_checkout_page_data(session, referer=referer)
    if not fresh:
        return
    if fresh.get("orderCandidateHash"):
        checkout_data["orderCandidateHash"] = fresh["orderCandidateHash"]
    if fresh.get("xsrf"):
        checkout_data["xsrf"] = fresh["xsrf"]
    if fresh.get("page_id"):
        checkout_data["page_id"] = fresh["page_id"]


def _rnwy_checkout_single_pass(
    session: Any,
    *,
    basket_id: str,
    offering_id: Optional[str] = None,
    referer: str = CHECKOUT_REF,
    checkout_data: Optional[Dict[str, Any]] = None,
    payment_method: Optional[str] = None,
    product_id: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """
    One offering + payment method select + execute-payment-plan (no retry re-offering).
    """
    method = normalize_payment_method(payment_method)
    checkout_data = checkout_data or _fetch_checkout_page_data(session, referer=referer)
    if not checkout_data:
        checkout_data = {
            "checkout_url": referer or CHECKOUT_BUY_NOW,
            "rsSessionId": int(time.time() * 1000),
        }
    _sync_checkout_auth(session, checkout_data)
    if not _ensure_checkout_order_hash(session, checkout_data):
        return None, "checkout page missing orderCandidateHash"

    _warm_checkout_rnwy_session(session, checkout_data, referer=referer)
    _checkout_basket_query_sync(session, checkout_data)
    _clear_checkout_offering_state(checkout_data)

    plan_id = offering_id
    if not plan_id:
        plan_id = _create_payment_offering_id(session, basket_id, checkout_data)
    if not plan_id:
        return None, "no paymentPlanId (checkout HTML + CreatePaymentOffering)"

    checkout_data["paymentPlanId"] = str(plan_id)
    offering_stub = (
        checkout_data.get("paymentOffering")
        or checkout_data.get("_last_offering")
        or {}
    )

    def _payment_already_selected() -> bool:
        return _payment_preselected(offering_stub, method)

    def _try_select_payment() -> bool:
        if _payment_already_selected():
            if method == "bnpl":
                print(
                    "[checkout] "
                    f"{_selected_afterpay_label(offering_stub)} already selected "
                    "on checkout"
                )
            else:
                print("[checkout] iDEAL already selected on checkout")
            return True
        return _select_payment_method_plan(
            session,
            checkout_data,
            plan_id,
            method,
            offering=offering_stub,
        )

    selected = False
    select_attempts = (
        2 if method == "bnpl" and _afterpay_fast_lane() else IDEAL_SELECTION_ATTEMPTS
    )
    for select_attempt in range(1, select_attempts + 1):
        if _try_select_payment():
            selected = True
            break
        if select_attempt >= select_attempts:
            break
        print(
            f"[checkout] payment select failed ({select_attempt}/"
            f"{select_attempts}) — refreshing checkout state..."
        )
        if IDEAL_SELECTION_RETRY_DELAY > 0:
            time.sleep(IDEAL_SELECTION_RETRY_DELAY)
        refreshed = _fetch_checkout_page_data(session, referer=referer)
        if refreshed:
            checkout_data.update(refreshed)
            _sync_checkout_auth(session, checkout_data)
            if refreshed.get("paymentPlanId"):
                plan_id = refreshed["paymentPlanId"]
                checkout_data["paymentPlanId"] = str(plan_id)

    if not selected:
        label = "Afterpay" if method == "bnpl" else "iDEAL"
        _log_offering_payment_state(offering_stub, method)
        if method == "bnpl":
            return None, (
                f"{label} select failed after {select_attempts} attempt(s) — "
                "check BNPL allowed on account; refresh bol_token.json from checkout "
                "(same proxy); browser checkout will be used"
            )
        print(
            f"[checkout] {label} select failed after {select_attempts} "
            "attempt(s) — trying execute-payment-plan anyway"
        )

    settle = 0.0 if method == "bnpl" and _afterpay_fast_lane() else IDEAL_SELECTION_SETTLE
    if os.environ.get("BOL_IDEAL_SETTLE_SEC"):
        try:
            settle = float(os.environ["BOL_IDEAL_SETTLE_SEC"])
        except ValueError:
            pass
    if settle > 0:
        time.sleep(settle)

    _refresh_order_hash(session, checkout_data, referer)

    payment_data = _execute_payment_plan(
        session, checkout_data, for_bnpl=(method == "bnpl")
    )
    if not payment_data:
        if checkout_data.get("_execute_error") == "400055":
            return None, "execute-payment-plan 400055"
        return None, "execute-payment-plan failed"

    if method == "bnpl":
        pid = product_id or os.environ.get("BOL_PRODUCT_ID", "").strip() or None
        if _verify_bnpl_order_placed(
            session, checkout_data, payment_data, product_id=pid
        ):
            print("[checkout] Afterpay/BNPL order confirmed")
            return CHECKOUT_REF, "bnpl_order_placed"
        return None, (
            "Afterpay execute-payment-plan ran but order not confirmed "
            "(items still in basket — use browser checkout or complete bol checkout manually)"
        )

    url = _ideal_url_from_payment_execution(session, checkout_data, payment_data)
    if url and _is_bank_redirect(url):
        return url, "execute-payment-plan + payment-execution"

    plan_id = checkout_data.get("paymentPlanId")
    if plan_id:
        url, via = _try_payment_execution_page(session, str(plan_id))
        if url and _is_bank_redirect(url):
            return url, via

        try:
            url, via = _create_payment_graphql(
                session, str(plan_id), _checkout_page_id(checkout_data)
            )
            if url and _is_bank_redirect(url):
                return url, via
        except Exception as exc:
            print(f"[checkout] createPayment after execute: {exc}")

    return None, "no pay.ideal.nl URL (execute or payment-execution failed)"


def _try_rnwy_checkout_once(
    session: Any,
    page_id: str,
    *,
    offering_id: Optional[str] = None,
    basket_id: Optional[str] = None,
    referer: str = CHECKOUT_REF,
) -> Tuple[Optional[str], str]:
    """Legacy wrapper — delegates to single-pass pipeline."""
    if not basket_id:
        basket_id = _resolve_basket_id(session, page_id, None)
    return _rnwy_checkout_single_pass(
        session,
        basket_id=basket_id,
        offering_id=offering_id,
        referer=referer,
    )


def _try_rnwy_checkout(
    session: Any,
    page_id: str,
    *,
    offering_id: Optional[str] = None,
    basket_id: Optional[str] = None,
    referer: str = CHECKOUT_REF,
    max_attempts: int = 1,
) -> Tuple[Optional[str], str]:
    """rnwy checkout; default one pass to avoid duplicate CreatePaymentOffering."""
    last_via = "rnwy checkout failed"
    for attempt in range(max_attempts):
        url, via = _try_rnwy_checkout_once(
            session,
            page_id,
            offering_id=offering_id,
            basket_id=basket_id,
            referer=referer,
        )
        if url:
            return url, via
        last_via = via
        if max_attempts > 1:
            print(f"[checkout] rnwy attempt {attempt + 1}/{max_attempts}: {via}")
        if attempt + 1 < max_attempts:
            time.sleep(0.5)
    return None, last_via


def _prime_checkout_session(
    session: Any,
    *,
    basket_id: Optional[str],
    product_referer: Optional[str],
) -> Tuple[Optional[str], str, int, Optional[Dict[str, Any]]]:
    """Basket → BUY_NOW checkout page; return bid, referer, html len, parsed checkout data."""
    page_id = _page_id()
    bid = _resolve_basket_id(session, page_id, basket_id)
    basket_url = "https://www.bol.com/nl/nl/basket/"
    referer = (product_referer or "").strip() or basket_url
    checkout_html_len = 0
    peek: Optional[Dict[str, Any]] = None

    try:
        if not _afterpay_fast_lane():
            br = _page_get(session, basket_url, referer="https://www.bol.com/nl/nl/")
            print(
                f"[checkout] basket prime status={br.status_code} len={len(br.text)} "
                f"shopping_session={bool(get_cookie_value(session, 'shopping_session_id'))}"
            )
        else:
            print("[checkout] basket prime skipped — going straight to BUY_NOW checkout")
        cr = _page_get(session, CHECKOUT_BUY_NOW, referer=referer)
        checkout_html_len = len(cr.text or "")
        print(
            f"[checkout] BUY_NOW from product referer status={cr.status_code} "
            f"len={checkout_html_len}"
        )
        if cr.status_code == 200 and len(cr.text or "") >= 5_000:
            peek = _parse_checkout_page_html(cr.text, session)
            if peek:
                peek["checkout_url"] = CHECKOUT_BUY_NOW
    except Exception as exc:
        print(f"[warn] basket→checkout prime: {exc}")

    if not peek:
        peek = _fetch_checkout_page_data(session, referer=referer)

    if not peek or not peek.get("orderCandidateHash"):
        stub = peek or {
            "checkout_url": CHECKOUT_BUY_NOW,
            "rsSessionId": int(time.time() * 1000),
        }
        _sync_checkout_auth(session, stub)
        if _ensure_checkout_order_hash(session, stub):
            peek = stub

    return bid, referer, checkout_html_len, peek


def _apply_checkout_product_context(
    session: Any,
    basket_id: Optional[str],
    *,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity: int = 1,
    product_referer: Optional[str] = None,
) -> Optional[str]:
    """Fresh basket + re-ATC only when no ATC basket exists (or forced)."""
    if not _should_prepare_fresh_basket(basket_id):
        if basket_id:
            print(f"[checkout] reusing ATC basket: {basket_id}")
        return basket_id
    pid = (product_id or "").strip()
    ouid = (offer_uid or "").strip()
    if not pid or not ouid:
        creds = _load_json_file(str(_CHECKOUT_ROOT / "bol_credentials.json")) or {}
        pid = pid or str(creds.get("product_id") or "").strip()
        if not pid and creds.get("product_url"):
            m = re.search(r"/(\d{10,20})/?", str(creds["product_url"]))
            if m:
                pid = m.group(1)
        ouid = ouid or str(creds.get("offer_uid") or "").strip()
    if not pid or not ouid:
        print("[checkout] no product_id/offer_uid — skipping fresh basket prep")
        return basket_id
    return _prepare_fresh_checkout_basket(
        session,
        product_id=pid,
        offer_uid=ouid,
        quantity=max(1, int(quantity)),
        product_url=(product_referer or "").strip(),
    )


def run_rnwy_ideal_checkout(
    session: Any,
    basket_id: Optional[str] = None,
    *,
    product_referer: Optional[str] = None,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity: int = 1,
) -> Dict[str, Any]:
    """
    Primary checkout path (standalone bot parity):
    GET checkout/?entryPoint=BUY_NOW → iDEAL → execute-payment-plan → payment-execution.
    """
    _apply_checkout_proxy_env()
    _reset_checkout_session_state()
    _init_session_holder(session)
    skip_prime = _skip_checkout_prime()
    if not skip_prime:
        _prime_www(session)
    else:
        print("[checkout] skipping www prime — reusing ATC session cookies")
    try:
        from src.sites.akamai import ensure_akamai_cookies

        ensure_akamai_cookies(session)
    except Exception:
        pass

    basket_id = _apply_checkout_product_context(
        session,
        basket_id,
        product_id=product_id,
        offer_uid=offer_uid,
        quantity=quantity,
        product_referer=product_referer,
    )

    bid, referer, checkout_html_len, peek = _prime_checkout_session(
        session,
        basket_id=basket_id,
        product_referer=product_referer,
    )
    if peek:
        print(
            f"[checkout] parsed checkout page: hash={peek['orderCandidateHash'][:8]}… "
            f"plan={peek.get('paymentPlanId')}"
        )

    payment_url, via = _rnwy_checkout_single_pass(
        session,
        basket_id=bid,
        referer=referer,
        checkout_data=peek,
    )
    if payment_url:
        print(f"[ok] iDEAL payment URL ({via}):\n{payment_url}")
        return {
            "success": True,
            "payment_url": payment_url,
            "offering_id": (peek or {}).get("paymentPlanId"),
            "basket_id": bid,
            "stage": "ideal_payment",
            "via": via,
            "checkout_html_len": checkout_html_len,
            "browser_viable": checkout_html_len > 50_000,
        }

    return {
        "success": False,
        "payment_url": None,
        "basket_id": bid,
        "message": via,
        "checkout_html_len": checkout_html_len,
        "browser_viable": checkout_html_len > 50_000,
        "checkout_url": CHECKOUT_BUY_NOW,
    }


def _try_ideal_payment_fallbacks(
    session: Any,
    *,
    bid: str,
    peek: Optional[Dict[str, Any]],
    page_id: str,
    bank_id: Optional[str],
    referer: str,
    offering_id: Optional[str],
) -> Tuple[Optional[str], str, Optional[str]]:
    """GraphQL / firefly / payment-execution fallbacks when rnwy iDEAL path fails."""
    oid = offering_id
    if not oid and bid:
        stub = peek or {
            "page_id": page_id,
            "checkout_url": CHECKOUT_BUY_NOW,
            "xsrf": get_cookie_value(session, "XSRF-TOKEN"),
        }
        oid = _create_payment_offering_id(session, bid, stub)

    payment_url: Optional[str] = None
    via = "ideal fallbacks exhausted"
    if not oid:
        return None, via, oid

    for label, fn in (
        ("createPayment graphql", lambda: _create_payment_graphql(session, str(oid), page_id, bank_id=bank_id)),
        ("firefly", lambda: _create_payment_firefly(session, str(oid), bank_id=bank_id)),
        ("bundle brute", lambda: _try_brute_create_payment(session, str(oid), page_id)),
        ("payment-execution page", lambda: _try_payment_execution_page(session, str(oid))),
    ):
        if payment_url:
            break
        try:
            payment_url, via = fn()
        except Exception as exc:
            print(f"[checkout] {label}: {exc}")

    return payment_url, via, oid


def _checkout_result_success(
    *,
    payment_url: Optional[str],
    via: str,
    offering_id: Optional[str],
    bid: str,
    stage: str,
    checkout_html_len: int,
    browser_viable: bool,
) -> Dict[str, Any]:
    return {
        "success": True,
        "payment_url": payment_url,
        "offering_id": offering_id,
        "basket_id": bid,
        "stage": stage,
        "via": via,
        "checkout_html_len": checkout_html_len,
        "browser_viable": browser_viable,
    }


def run_ideal_checkout(
    session: Any,
    basket_id: Optional[str] = None,
    *,
    bank_id: Optional[str] = None,
    verbose: bool = False,
    product_referer: Optional[str] = None,
    product_id: Optional[str] = None,
    offer_uid: Optional[str] = None,
    quantity: int = 1,
    payment_method: Optional[str] = None,
) -> Dict[str, Any]:
    """Return dict with success, payment_url, offering_id, message."""
    method = normalize_payment_method(payment_method)
    os.environ["BOL_PAYMENT_METHOD"] = method
    _apply_checkout_proxy_env()
    _reset_checkout_session_state()
    _init_session_holder(session)
    skip_prime = _skip_checkout_prime()
    if not skip_prime:
        _prime_www(session)
    else:
        print("[checkout] skipping www prime — reusing ATC session cookies")
    try:
        from src.sites.akamai import ensure_akamai_cookies

        ensure_akamai_cookies(session)
    except Exception:
        pass

    basket_id = _apply_checkout_product_context(
        session,
        basket_id,
        product_id=product_id,
        offer_uid=offer_uid,
        quantity=quantity,
        product_referer=product_referer,
    )

    bid, referer, checkout_html_len, peek = _prime_checkout_session(
        session,
        basket_id=basket_id,
        product_referer=product_referer,
    )

    def _browser_viable() -> bool:
        return checkout_html_len > 50_000

    if peek:
        print(
            f"[checkout] parsed checkout page: hash={peek['orderCandidateHash'][:8]}… "
            f"plan={peek.get('paymentPlanId')}"
        )

    payment_url, via = _rnwy_checkout_single_pass(
        session,
        basket_id=bid,
        referer=referer,
        checkout_data=peek,
        payment_method=method,
        product_id=product_id,
    )
    if not payment_url and (peek or {}).get("_execute_error") == "400055":
        payment_url, via = _checkout_retry_after_400055(
            session,
            basket_id=bid,
            referer=referer,
            checkout_data=peek,
            product_id=product_id,
            offer_uid=offer_uid,
            quantity=quantity,
            product_referer=product_referer,
            payment_method=method,
        )
    offering_id = (peek or {}).get("paymentPlanId") if peek else None
    page_id = (peek or {}).get("page_id") or _page_id()

    if via == "bnpl_order_placed":
        print("[ok] Afterpay/BNPL checkout complete (no iDEAL redirect needed)")
        try:
            from src.bol.cart import _clear_saved_basket_id
            from src.bol.login import save_session

            _clear_saved_basket_id()
            save_session(session, source="checkout_order_placed")
        except Exception as exc:
            print(f"[checkout] post-order session save: {exc}")
        return _checkout_result_success(
            payment_url=None,
            via=via,
            offering_id=offering_id,
            bid=bid,
            stage="afterpay_order",
            checkout_html_len=checkout_html_len,
            browser_viable=_browser_viable(),
        )

    if payment_url and _is_bank_redirect(payment_url):
        print(f"[ok] iDEAL payment URL ({via}):\n{payment_url}")
        return _checkout_result_success(
            payment_url=payment_url,
            via=via,
            offering_id=offering_id,
            bid=bid,
            stage="ideal_payment",
            checkout_html_len=checkout_html_len,
            browser_viable=_browser_viable(),
        )

    if method != "bnpl":
        payment_url, via, offering_id = _try_ideal_payment_fallbacks(
            session,
            bid=bid,
            peek=peek,
            page_id=page_id,
            bank_id=bank_id,
            referer=referer,
            offering_id=offering_id,
        )

    # Afterpay first, iDEAL backup only when Afterpay is not offered on checkout
    if method == "bnpl" and not payment_url:
        offering_stub = (peek or {}).get("paymentOffering") or (peek or {}).get("_last_offering")
        if offering_stub and afterpay_available_on_offering(offering_stub):
            print(
                f"[checkout] Afterpay/BNPL failed ({via}) but was offered — "
                "skipping iDEAL backup (browser fallback is faster)"
            )
        else:
            if offering_stub and not afterpay_available_on_offering(offering_stub):
                print("[checkout] Afterpay not offered on this product — switching to iDEAL")
            else:
                print(
                    f"[checkout] Afterpay/BNPL failed ({via}) — "
                    "falling back to iDEAL backup"
                )
            os.environ["BOL_PAYMENT_METHOD"] = "ideal"
            fresh_peek = _fetch_checkout_page_data(session, referer=referer) or peek
            if fresh_peek:
                peek = fresh_peek
                page_id = peek.get("page_id") or page_id
            payment_url, via = _rnwy_checkout_single_pass(
                session,
                basket_id=bid,
                referer=referer,
                checkout_data=peek,
                payment_method="ideal",
                product_id=product_id,
            )
            if payment_url and _is_bank_redirect(payment_url):
                via = f"ideal_backup:{via}"
                print(f"[ok] iDEAL backup payment URL ({via}):\n{payment_url}")
                return _checkout_result_success(
                    payment_url=payment_url,
                    via=via,
                    offering_id=offering_id,
                    bid=bid,
                    stage="ideal_payment",
                    checkout_html_len=checkout_html_len,
                    browser_viable=_browser_viable(),
                )
            payment_url, via_fb, offering_id = _try_ideal_payment_fallbacks(
                session,
                bid=bid,
                peek=peek,
                page_id=page_id,
                bank_id=bank_id,
                referer=referer,
                offering_id=offering_id,
            )
            if payment_url:
                via = f"ideal_backup:{via_fb}"
                print(f"[ok] iDEAL backup payment URL ({via}):\n{payment_url}")
                return _checkout_result_success(
                    payment_url=payment_url,
                    via=via,
                    offering_id=offering_id,
                    bid=bid,
                    stage="ideal_payment",
                    checkout_html_len=checkout_html_len,
                    browser_viable=_browser_viable(),
                )

    if payment_url:
        print(f"[ok] iDEAL payment URL ({via}):\n{payment_url}")
        return _checkout_result_success(
            payment_url=payment_url,
            via=via,
            offering_id=offering_id,
            bid=bid,
            stage="ideal_payment",
            checkout_html_len=checkout_html_len,
            browser_viable=_browser_viable(),
        )

    msg = via if isinstance(via, str) else "no payment URL"
    print(f"[warn] HTTP did not return iDEAL bank URL ({msg}) — use browser capture")
    return {
        "success": False,
        "payment_url": None,
        "offering_id": offering_id,
        "basket_id": bid,
        "checkout_url": CHECKOUT_REF,
        "message": msg,
        "browser_viable": _browser_viable() or bool(offering_id),
        "checkout_html_len": checkout_html_len,
    }


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("BOL_NO_PROXY", "1")
    cli = argv if argv is not None else sys.argv[1:]
    bid_arg = cli[0] if cli and cli[0] != "--verbose" else None
    session = ensure_session()
    result = run_ideal_checkout(session, bid_arg, verbose="--verbose" in cli)
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
