#!/usr/bin/env python3
"""Try persisted hashes for payment.createPayment."""
import json, os, re, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR, get_cookie_value
from bol_cart import (
    _init_session_holder, _prime_www, _graphql, _get_curl_session, _gql_headers,
    _load_saved_basket_id, get_basket_id, GRAPHQL_URL, _request, _merge_cookies_from_response,
)

HASH_CREATE_OFFERING = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
HASH_UPDATE_PAYMENT = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
REF = "https://www.bol.com/nl/nl/checkout/"
APP = "checkout-web-fe"

js = _request(
    ensure_session(), "GET", "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js", timeout=120
).text
hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", js)))
# also search operation name strings in bundle
ops = sorted(set(re.findall(r"Checkout[A-Za-z]{5,60}Mutation", js)))
ops += sorted(set(re.findall(r"Payment[A-Za-z]{5,60}Mutation", js)))
ops += sorted(set(re.findall(r"createPayment[A-Za-z]{0,40}", js, re.I)))
print("ops in js:", ops[:30])

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id() or get_basket_id(s, page_id, referer=REF)
off = _graphql(s, "CheckoutCreatePaymentOfferingMutation", HASH_CREATE_OFFERING,
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="off", referer=REF, client_app=APP)
oid = off["paymentOfferings"]["createPaymentOffering"]["id"]
_graphql(s, "CheckoutUpdatePaymentChoiceMutation", HASH_UPDATE_PAYMENT,
    variables={"input": {"paymentOfferingId": oid, "paymentMethodCode": "IDEAL"}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="ideal", referer=REF, client_app=APP)
print("offering", oid)

vars_create = {
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
vars_alt = {
    "input": vars_create["createPaymentInput"],
    "requestSource": "CHECKOUT",
}

op_names = [
    "CheckoutCreatePaymentMutation",
    "CheckoutPaymentCreateMutation",
    "PaymentCreatePaymentMutation",
    "CheckoutExecutePaymentMutation",
    "CheckoutPlaceOrderMutation",
    "CheckoutSubmitPaymentMutation",
    "createPayment",
    "CheckoutCreatePaymentExecutionMutation",
] + [o for o in ops if "Mutation" in o or o == "createPayment"]

cs = _get_curl_session(s)
headers = _gql_headers(REF, client_app=APP)
headers["bol-client-page-id"] = page_id
headers["m2-page-id"] = page_id
xsrf = get_cookie_value(s, "XSRF-TOKEN")
if xsrf:
    headers["x-xsrf-token"] = xsrf

for h in hashes:
    if h in (HASH_CREATE_OFFERING, HASH_UPDATE_PAYMENT):
        continue
    for op in op_names:
        for variables in (vars_create, vars_alt):
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
                print("HIT", op, h[:20], json.dumps(data)[:500])
                sys.exit(0)
            errs = data.get("errors") or []
            for e in errs:
                msg = str(e.get("message", e))
                if "PersistedQueryNotFound" not in msg and "operation" not in msg.lower():
                    print("maybe", op, h[:24], msg[:120])

print("no hit among", len(hashes), "hashes")
