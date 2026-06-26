#!/usr/bin/env python3
import os, re, sys, json
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
mf = _request(s, "GET", "https://assets.s-bol.com/_remix/manifest-bad19f46.js", timeout=60).text
print("manifest len", len(mf))
# persisted query map patterns
for pat in [
    r'operationName":"([^"]+)"[^}]{0,200}sha256Hash":"(sha256:[a-f0-9]{64})"',
    r'"([A-Za-z][A-Za-z0-9_]{5,70})":\s*"(sha256:[a-f0-9]{64})"',
    r'\["([A-Za-z][A-Za-z0-9_]+)","(sha256:[a-f0-9]{64})"\]',
]:
    pairs = re.findall(pat, mf)
    print(pat, len(pairs))
    for op, h in pairs:
        if "ayment" in op or "Checkout" in op or "Basket" in op:
            print(" ", op, h)

# list all js files in manifest mentioning payment
for m in re.findall(r'"([^"]*payment[^"]*\.js)"', mf, re.I):
    print("payment chunk:", m)
