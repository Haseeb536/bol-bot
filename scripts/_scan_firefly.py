#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request

s = ensure_session()
_init_session_holder(s)
for name in ("entry.client-C0eesXKS.js", "chunk-4N6VE7H7-CDap_1bn.js", "src-BDdkT8dj.js"):
    t = _request(s, "GET", f"https://assets.s-bol.com/_remix/{name}", timeout=90).text
    if "firefly" not in t.lower():
        continue
    print("\n===", name, "===")
    i = 0
    n = 0
    while n < 10:
        i = t.lower().find("firefly", i + 1)
        if i < 0:
            break
        print(t[max(0, i - 60) : i + 120].replace("\n", " "))
        n += 1
