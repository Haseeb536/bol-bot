#!/usr/bin/env python3
"""Diagnose + test checkout steps on current session."""
import json
import os
import sys

os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bol_login import ensure_session
from bol_cart import _init_session_holder, _prime_www, _graphql, _load_saved_basket_id
from bol_checkout import (
    _prime_checkout_session,
    _create_payment_offering_id,
    _select_ideal_payment_plan,
    _execute_payment_plan,
    _ideal_url_from_payment_execution,
    _fetch_checkout_page_data,
    _sync_checkout_auth,
    HASH_BASKET,
    CLIENT_APP,
    CHECKOUT_BUY_NOW,
)

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
bid = _load_saved_basket_id()
_, ref, _, peek = _prime_checkout_session(s, basket_id=bid, product_referer=None)
_sync_checkout_auth(s, peek)

print("=== basket query ===")
b = _graphql(
    s,
    "CheckoutBasketQuery",
    "sha256:bd1b3dda5fcfba2f1ed2fa4e53afe1dfb723f308deba643f05612bcd8aa18a31",
    variables={},
    page_id=peek["page_id"],
    label="basket",
    referer=CHECKOUT_BUY_NOW,
    client_app=CLIENT_APP,
)
print(json.dumps(b, indent=2)[:4000])

print("=== offering ===")
plan = _create_payment_offering_id(s, bid, peek)
print("plan", plan, "offering", json.dumps(peek.get("_last_offering"), indent=2)[:800])

print("=== ideal select (forced) ===")
ok = _select_ideal_payment_plan(s, peek, plan)
print("ideal ok", ok)

print("=== execute ===")
pay = _execute_payment_plan(s, peek)
print("pay", json.dumps(pay, indent=2) if pay else None)

if pay:
    url = _ideal_url_from_payment_execution(s, peek, pay)
    print("ideal url", url)
