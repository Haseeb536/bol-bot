#!/usr/bin/env python3
import os
import re
import sys

os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
url = "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js"
t = _request(s, "GET", url, timeout=90).text
idx = t.find("createPayment(createPaymentInput")
print("createPayment idx", idx)
if idx >= 0:
    chunk = t[max(0, idx - 2500) : idx + 200]
    hashes = re.findall(r"sha256:[a-f0-9]{64}", chunk)
    print("hashes near createPayment:", hashes[-5:])
idx2 = t.find("CheckoutCreatePaymentMutation")
print("mutation idx", idx2)
if idx2 >= 0:
    chunk = t[max(0, idx2 - 500) : idx2 + 800]
    print(chunk[:900])
