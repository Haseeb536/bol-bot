#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
chunks = [
    "https://assets.s-bol.com/_remix/src-BDdkT8dj.js",
    "https://assets.s-bol.com/_remix/entry.client-C0eesXKS.js",
    "https://assets.s-bol.com/_remix/chunk-4N6VE7H7-CDap_1bn.js",
    "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js",
]
for url in chunks:
    try:
        t = _request(s, "GET", url, timeout=90).text
    except Exception as e:
        print(url, "ERR", e)
        continue
    if "paymentExecutions" not in t and "createPayment" not in t:
        print(url, "skip", len(t))
        continue
    print("\n===", url, len(t), "===")
    for term in ("paymentExecutions", "CheckoutCreatePayment", "createPayment("):
        if term in t:
            i = t.find(term)
            print(term, ":", t[max(0, i - 80) : i + 250].replace("\n", " ")[:330])
    pairs = re.findall(
        r'mutation\s+(\w*[Pp]ayment\w*)\s*\(',
        t,
    )
    if pairs:
        print("mutations:", sorted(set(pairs))[:20])
    for m in re.finditer(r"D\.persisted\(`(sha256:[a-f0-9]{64})`[^`]{0,400}createPayment", t):
        print("persisted near createPayment:", m.group(1))
