#!/usr/bin/env python3
import json
import os
import sys
import uuid

os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bol_login import ensure_session, _load_json_file, ROOT_DIR
from bol_cart import (
    _init_session_holder,
    _prime_www,
    add_to_cart,
    get_basket_id,
    get_offer_uid,
    _page_get,
)
from bol_checkout import (
    _fetch_checkout_page_data,
    _create_payment_offering_id,
    _execute_payment_plan,
    _select_ideal_payment_plan,
    _sync_checkout_auth,
    CHECKOUT_BUY_NOW,
)

creds = _load_json_file(os.path.join(ROOT_DIR, "bol_credentials.json")) or {}
pid = creds.get("product_url", "").split("/")[-2] or "9300000182508099"
offer = creds.get("offer_uid")
qty = int(creds.get("quantity") or 1)

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
page_id = str(uuid.uuid4())
bid = get_basket_id(s, page_id)
print("re-adding to cart...")
try:
    add_to_cart(s, str(pid), offer, bid, qty, referer=creds.get("product_url", ""))
except Exception as e:
    print("add (may already in cart):", e)

peek = _fetch_checkout_page_data(s, referer=creds.get("product_url", ""))
print("hash after readd", peek.get("orderCandidateHash") if peek else None)
_sync_checkout_auth(s, peek)
plan = _create_payment_offering_id(s, bid, peek)
print("plan", plan)
_select_ideal_payment_plan(s, peek, plan)
pay = _execute_payment_plan(s, peek)
print("execute", json.dumps(pay)[:500] if pay else None)
