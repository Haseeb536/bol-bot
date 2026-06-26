#!/usr/bin/env python3
import json, os, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR, get_cookie_value
from bol_cart import _init_session_holder, _prime_www, _get_curl_session, _gql_headers, _merge_cookies_from_response, _graphql, get_basket_id, _load_saved_basket_id, GRAPHQL_URL, _request

HASH_CREATE_OFFERING = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
HASH_UPDATE_PAYMENT = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"
REF = "https://www.bol.com/nl/nl/checkout/"
APP = "checkout-web-fe"

s = ensure_session(); _init_session_holder(s); _prime_www(s)
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
cs = _get_curl_session(s)
headers = _gql_headers(REF, client_app=APP)
headers["Content-Type"] = "application/json"
xsrf = get_cookie_value(s, "XSRF-TOKEN")
if xsrf: headers["x-xsrf-token"] = xsrf
for url in [
    f"https://www.bol.com/nl/payment-execution/return/ideal?offeringId={oid}",
    f"https://firefly.bol.com/payment/v1/create",
    f"https://www.bol.com/api/payment/v1/payments",
    f"https://www.bol.com/api/payments",
]:
    body = {"offeringId": str(oid), "paymentMethodCode": "IDEAL", "clientCallBackPath": "/nl/nl/checkout/",
            "returnUrlDetails": {"hostName": "www.bol.com", "path": "/nl/payment-execution/return", "pathSegments": ["ideal"]}}
    for method in ("POST", "GET"):
        try:
            r = cs.request(method, url, json=body if method=="POST" else None, headers=headers, timeout=30, allow_redirects=False)
            print(method, url, r.status_code, r.headers.get("location","")[:80], r.text[:200])
        except Exception as e:
            print(method, url, "err", e)
