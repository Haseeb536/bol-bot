#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
for name in ("chunk-4N6VE7H7-CDap_1bn.js", "src-BDdkT8dj.js"):
    url = f"https://assets.s-bol.com/_remix/{name}"
    t = _request(s, "GET", url, timeout=90).text
    print(name, len(t))
    for term in ("createPayment", "firefly", "paymentExecutions", "idealActionDetails", "CheckoutCreatePaymentMutation"):
        if term in t:
            print("  HAS", term)
            i = t.find(term)
            print("   ", t[max(0, i - 80) : i + 180].replace("\n", " ")[:260])
    pairs = re.findall(r'mutation\s+(\w*Payment\w*)\s*\(', t)
    if pairs:
        print("  mutations:", sorted(set(pairs))[:15])
