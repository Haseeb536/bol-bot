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
pairs = re.findall(
    r'\["([A-Za-z][A-Za-z0-9_]{2,80})","(sha256:[a-f0-9]{64})"\]',
    js,
)
print("pairs", len(pairs))
for op, h in pairs:
    print(f"  {op} -> {h}")
hashes = sorted(set(re.findall(r"sha256:[a-f0-9]{64}", js)))
print("total hashes", len(hashes))
