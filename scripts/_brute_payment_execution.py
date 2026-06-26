#!/usr/bin/env python3
"""Brute persisted hashes for paymentExecutions.createPayment."""
import json, os, re, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR, get_cookie_value
from bol_cart import (
    _init_session_holder, _prime_www, _graphql, _load_saved_basket_id,
    _get_curl_session, _gql_headers, _merge_cookies_from_response, GRAPHQL_URL, _page_get,
)

H_OFF = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
H_UPD = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
REF = "https://www.bol.com/nl/nl/checkout/"
APP = "checkout-web-fe"

js = open(os.path.join(os.path.dirname(__file__), "..", "_checkout_bundle.js"), encoding="utf-8").read()
hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", js)))

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
_page_get(s, REF, referer="https://www.bol.com/nl/nl/basket/")
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id()
off = _graphql(s, "CheckoutCreatePaymentOfferingMutation", H_OFF,
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="off", referer=REF, client_app=APP)
oid = off["paymentOfferings"]["createPaymentOffering"]["id"]
_graphql(s, "CheckoutUpdatePaymentChoiceMutation", H_UPD,
    variables={"input": {"paymentOfferingId": oid, "paymentMethodCode": "IDEAL"}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="ideal", referer=REF, client_app=APP)
print("offering", oid)

vars_in = {
    "createPaymentInput": {
        "offeringId": str(oid),
        "clientCallBackPath": "/nl/nl/checkout/",
        "returnUrlDetails": {
            "hostName": "www.bol.com",
            "path": "/nl/payment-execution/return",
            "pathSegments": ["ideal"],
        },
    },
    "requestSource": "CHECKOUT",
}
vars_alt = {"input": vars_in["createPaymentInput"], "requestSource": "CHECKOUT"}

ops = [
    "PaymentExecutionMutation",
    "PaymentCreatePaymentMutation",
    "CheckoutPaymentExecutionMutation",
    "CheckoutCreatePaymentMutation",
    "CreatePaymentMutation",
    "PaymentExecutionCreatePaymentMutation",
]

cs = _get_curl_session(s)
headers_base = _gql_headers(REF, client_app=APP)
headers_base["bol-client-page-id"] = page_id
xsrf = get_cookie_value(s, "XSRF-TOKEN")
if xsrf:
    headers_base["x-xsrf-token"] = xsrf

for h in hashes:
    if h in (H_OFF, H_UPD):
        continue
    for op in ops:
        for variables in (vars_in, vars_alt):
            headers = dict(headers_base)
            headers["bol-app-operation-name"] = op
            body = {
                "operationName": op,
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}},
            }
            r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
            _merge_cookies_from_response(r, s)
            try:
                data = r.json()
            except Exception:
                continue
            if r.status_code == 200 and data.get("data") and not data.get("errors"):
                print("HIT", op, h)
                print(json.dumps(data, indent=2)[:2000])
                sys.exit(0)
            errs = data.get("errors") or []
            for e in errs:
                msg = str(e.get("message", e))
                if "PersistedQueryNotFound" not in msg and "redacted" not in msg.lower():
                    print("hint", op, h[-12:], msg[:100])

print("no hit")
