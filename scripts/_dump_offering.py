#!/usr/bin/env python3
import json, os, sys, uuid
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session, _load_json_file, ROOT_DIR
from bol_cart import _init_session_holder, _prime_www, _graphql, _load_saved_basket_id, _page_get

REF = "https://www.bol.com/nl/nl/checkout/"
s = ensure_session()
_init_session_holder(s)
_prime_www(s)
_page_get(s, REF, referer="https://www.bol.com/nl/nl/basket/")
page_id = str((_load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}).get("page_id") or uuid.uuid4())
bid = _load_saved_basket_id()
off = _graphql(s, "CheckoutCreatePaymentOfferingMutation",
    "sha256:117b325d03c7fc8060d9bac2219151325614d10093d4262af3be0dab7d7a6f96",
    variables={"input": {"subjects": [{"id": bid, "type": "ORDER"}]}, "requestSource": "CHECKOUT"},
    page_id=page_id, referer=REF, client_app="checkout-web-fe")
print(json.dumps(off, indent=2))
