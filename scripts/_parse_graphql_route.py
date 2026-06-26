#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
url = "https://assets.s-bol.com/_remix/graphql-h07Dq-_4.js"
t = _request(s, "GET", url, timeout=90).text
out = os.path.join(os.path.dirname(__file__), "..", "_graphql_route.js")
open(out, "w", encoding="utf-8").write(t)
print("len", len(t), "saved", out)

pairs = re.findall(
    r'\["([A-Za-z][A-Za-z0-9_]{2,80})","(sha256:[a-f0-9]{64})"\]',
    t,
)
print("pairs", len(pairs))
for op, h in pairs:
    if "ayment" in op or "Payment" in op or "Create" in op:
        print(op, h)

for term in ("createPayment", "paymentExecutions", "CheckoutCreatePayment"):
    if term in t:
        i = t.find(term)
        print(term, t[max(0, i - 60) : i + 200])

hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", t)))
print("hash count", len(hashes))
