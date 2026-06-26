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
from bol_checkout import run_ideal_checkout, _create_payment_graphql, _create_payment_firefly

REF = "https://www.bol.com/nl/nl/checkout/"
APP = "checkout-web-fe"
H_OFF = "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96"
H_UPD = "sha256:26d80a5c46f0fb7241c1b602c9785b3e01243ae9f77f7d3c5c75e4912cee7305"

QUERY = """
mutation CheckoutCreatePaymentMutation(
  $createPaymentInput: PaymentCreationRequest!
  $requestSource: RequestSource
) {
  paymentExecutions {
    createPayment(createPaymentInput: $createPaymentInput, requestSource: $requestSource) {
      __typename
      ... on Payment {
        id
        status
        paymentFollowUpAction {
          __typename
          idealActionDetails { redirectUrl }
          redirectActionDetails { redirectUrl }
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
_page_get(s, REF, referer="https://www.bol.com/nl/nl/basket/")
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id()
off = _graphql(s, "CheckoutCreatePaymentOfferingMutation", H_OFF,
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, referer=REF, client_app=APP)
oid = off["paymentOfferings"]["createPaymentOffering"]["id"]
print("offering full:", json.dumps(off, indent=2)[:3000])
_graphql(s, "CheckoutUpdatePaymentChoiceMutation", H_UPD,
    variables={"input": {"paymentOfferingId": oid, "paymentMethodCode": "IDEAL"}, "requestSource": "CHECKOUT"},
    page_id=page_id, referer=REF, client_app=APP)

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
for app in (APP, "payment-web-fe", "payment-execution-web-fe", "firefly-web-fe"):
    headers = _gql_headers(REF, client_app=app)
    headers["bol-app-operation-name"] = "CheckoutCreatePaymentMutation"
    headers["bol-client-page-id"] = page_id
    xsrf = get_cookie_value(s, "XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    r = cs.post(GRAPHQL_URL, json={"operationName": "CheckoutCreatePaymentMutation", "variables": vars_create, "query": QUERY}, headers=headers, timeout=45)
    print("\n=== app", app, "status", r.status_code, "===")
    print(r.text[:2000])

# URLs query
URLS_Q = "query Q { shopUrls { paymentExecutionPageUrl paymentMethodsUrl } }"
for app in (APP, "checkout-web-fe"):
    headers = _gql_headers(REF, client_app=app)
    headers["bol-app-operation-name"] = "ShopUrlsQuery"
    r = cs.post(GRAPHQL_URL, json={"operationName": "ShopUrlsQuery", "variables": {}, "query": URLS_Q}, headers=headers, timeout=30)
    print("\nurls", app, r.status_code, r.text[:500])
