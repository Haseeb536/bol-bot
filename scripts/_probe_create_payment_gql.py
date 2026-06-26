#!/usr/bin/env python3
import json, os, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR, get_cookie_value
from bol_cart import (
    _init_session_holder, _prime_www, _graphql, _load_saved_basket_id,
    get_basket_id, _get_curl_session, _gql_headers, _merge_cookies_from_response,
    GRAPHQL_URL, _request,
)

REF = "https://www.bol.com/nl/nl/checkout/"
APP = "checkout-web-fe"
H_BASKET = "sha256:bd1b3dda5fcfba2f1ed2fa4e53afe1dfb723f308deba643f05612bcd8aa18a31"
H_OFF = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
H_UPD = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"

QUERY = """
mutation CheckoutCreatePaymentMutation($createPaymentInput: PaymentCreationRequest!, $requestSource: RequestSource) {
  paymentExecutions {
    createPayment(createPaymentInput: $createPaymentInput, requestSource: $requestSource) {
      __typename
      ... on Payment {
        id
        status
        paymentFollowUpAction {
          __typename
          idealActionDetails { redirectUrl }
        }
      }
      ... on PaymentExecutionProblem { errorCode }
    }
  }
}
"""

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id() or get_basket_id(s, page_id, referer=REF)
print("basket", bid)

basket_data = _graphql(
    s, "CheckoutBasketQuery", H_BASKET,
    variables={},
    page_id=page_id, label="basket_q", referer=REF, client_app=APP,
)
print("basket query keys", json.dumps(basket_data, default=str)[:800])

off = _graphql(
    s, "CheckoutCreatePaymentOfferingMutation", H_OFF,
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="off", referer=REF, client_app=APP,
)
oid = off["paymentOfferings"]["createPaymentOffering"]["id"]
_graphql(
    s, "CheckoutUpdatePaymentChoiceMutation", H_UPD,
    variables={"input": {"paymentOfferingId": oid, "paymentMethodCode": "IDEAL"}, "requestSource": "CHECKOUT"},
    page_id=page_id, label="ideal", referer=REF, client_app=APP,
)
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
headers = _gql_headers(REF, client_app=APP)
headers["bol-app-operation-name"] = "CheckoutCreatePaymentMutation"
headers["bol-client-page-id"] = page_id
headers["m2-page-id"] = page_id
xsrf = get_cookie_value(s, "XSRF-TOKEN")
if xsrf:
    headers["x-xsrf-token"] = xsrf

body = {"operationName": "CheckoutCreatePaymentMutation", "variables": vars_create, "query": QUERY}
r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
_merge_cookies_from_response(r, s)
print("full query status", r.status_code)
print(r.text[:3000])

# Search other remix bundles for createPayment persisted hash
js_urls = [
    "https://assets.s-bol.com/_remix/src-BDdkT8dj.js",
    "https://assets.s-bol.com/_remix/entry.client-C0eesXKS.js",
]
for url in js_urls:
    try:
        t = _request(s, "GET", url, timeout=60).text
        if "createPayment" in t and "paymentExecutions" in t:
            print("FOUND createPayment in", url)
            i = t.find("paymentExecutions")
            print(t[max(0, i - 100) : i + 400][:500])
    except Exception as e:
        print(url, e)
