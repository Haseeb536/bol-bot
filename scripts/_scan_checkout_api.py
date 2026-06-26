#!/usr/bin/env python3
import re
import os
ROOT = os.path.join(os.path.dirname(__file__), "..")
js = open(os.path.join(ROOT, "_checkout_bundle.js"), encoding="utf-8").read()

for pat in [
    r"https://[a-z0-9.-]*bol\.com[a-zA-Z0-9_/\-]*payment[a-zA-Z0-9_/\-]*",
    r"/payment/v[0-9]/[a-zA-Z]+",
    r"firefly\.bol\.com[^\"'`]{0,80}",
    r"operationName[`'\"]([A-Za-z]+Payment[A-Za-z]*)[`'\"]",
]:
    ms = sorted(set(re.findall(pat, js)))
    print("\n", pat, len(ms))
    for m in ms[:25]:
        print(" ", m if isinstance(m, str) else m[0] if m else m)

# find urql/graphql execute near createPayment offering
for term in ("createPaymentOffering", "updatePaymentChoice", "idealActionDetails", "redirectUrl"):
    idx = 0
    n = 0
    while n < 5:
        idx = js.find(term, idx + 1)
        if idx < 0:
            break
        ctx = js[max(0, idx - 120) : idx + 200]
        if "fetch" in ctx or "POST" in ctx or "mutation" in ctx or "E(" in ctx:
            print(f"\n--- {term} ---")
            print(ctx.replace("\n", " ")[:320])
        n += 1
