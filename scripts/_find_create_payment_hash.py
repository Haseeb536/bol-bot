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
js = _request(
    s, "GET", "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js", timeout=120
).text
for m in re.finditer(
    r"mutation\s+(\w*[Cc]reatePayment\w*)\s*\([^)]*\)[^`]{0,2000}?createPayment",
    js,
):
    print("mutation block at", m.start())
    print(m.group(0)[:500])
    print("---")
for m in re.finditer(r"D\.persisted\(`(sha256:[a-f0-9]{64})`", js):
    h = m.group(1)
    start = m.start()
    ctx = js[start : start + 200]
    if "createPayment" in js[start : start + 2500] or "CreatePayment" in ctx:
        print(h, ctx[:120])
