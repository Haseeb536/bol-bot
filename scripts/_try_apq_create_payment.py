#!/usr/bin/env python3
import json, os, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR, get_cookie_value
from bol_cart import (
    _init_session_holder, _prime_www, _graphql, _load_saved_basket_id,
    _get_curl_session, _gql_headers, _merge_cookies_from_response, GRAPHQL_URL, _page_get,
)

HASH_CREATE_PAYMENT = "sha256:70f3078015c61774dc1895f3b52cdb84fa3d9c34dc6696fbe44d16312f291f38"
H_OFF = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
H_UPD = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
REF = "https://www.bol.com/nl/nl/checkout/"

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
try:
    from src.sites.akamai import ensure_akamai_cookies
    ensure_akamai_cookies(s)
except Exception:
    pass
_page_get(s, REF, referer="https://www.bol.com/nl/nl/basket/")
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id()
print("basket", bid)

off = _graphql(s, "CheckoutCreatePaymentOfferingMutation", H_OFF,
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="off", referer=REF, client_app="checkout-web-fe")
oid = off["paymentOfferings"]["createPaymentOffering"]["id"]
_graphql(s, "CheckoutUpdatePaymentChoiceMutation", H_UPD,
    variables={"input": {"paymentOfferingId": oid, "paymentMethodCode": "IDEAL"}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="ideal", referer=REF, client_app="checkout-web-fe")
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

cs = _get_curl_session(s)
headers = _gql_headers(REF, client_app="checkout-web-fe")
headers["bol-app-operation-name"] = "CheckoutCreatePaymentMutation"
headers["bol-client-page-id"] = page_id
xsrf = get_cookie_value(s, "XSRF-TOKEN")
if xsrf:
    headers["x-xsrf-token"] = xsrf

# persisted only
body = {
    "operationName": "CheckoutCreatePaymentMutation",
    "variables": vars_create,
    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH_CREATE_PAYMENT}},
}
r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=45)
print("APQ-only status", r.status_code)
print(r.text[:2500])
