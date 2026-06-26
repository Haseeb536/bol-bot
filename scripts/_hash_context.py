#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
js = _request(s, "GET", "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js", timeout=120).text
OUT = os.path.join(os.path.dirname(__file__), "..", "_checkout_bundle.js")
open(OUT, "w", encoding="utf-8").write(js)
print("saved", OUT, len(js))

# operation names near Checkout/Payment
for m in re.finditer(r"Checkout[A-Za-z0-9_]{3,80}", js):
    name = m.group(0)
    if "Mutation" in name or "Query" in name:
        i = m.start()
        ctx = js[max(0, i - 80) : i + 120]
        if "sha256" in ctx:
            print(name, "->", re.findall(r"sha256:[a-f0-9]{64}", ctx))

# find firefly references
for term in ("firefly", "payment/v1", "createPayment", "idealAction", "redirectUrl"):
    i = 0
    n = 0
    while n < 8:
        i = js.find(term, i + 1)
        if i < 0:
            break
        print(f"\n=== {term} @ {i} ===")
        print(js[max(0, i - 100) : i + 200].replace("\n", " ")[:280])
        n += 1

# hash with nearest identifier before it
for h in sorted(set(re.findall(r"sha256:[a-f0-9]{64}", js))):
    i = js.find(h)
    before = js[max(0, i - 200) : i]
    names = re.findall(r"Checkout[A-Za-z0-9_]{8,60}|Payment[A-Za-z0-9_]{8,60}", before)
    if names:
        print(h[-12:], "near", names[-3:])
