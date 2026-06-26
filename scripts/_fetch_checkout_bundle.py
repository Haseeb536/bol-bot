#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _request
s = ensure_session(); _init_session_holder(s)
js = _request(s, "GET", "https://assets.s-bol.com/_remix/checkout-Cp1AXz9u.js", timeout=120).text
for term in ["offeringId", "createPaymentInput", "clientCallBackPath", "payment-execution"]:
    i=0; n=0
    while n<6:
        i=js.find(term,i+1)
        if i<0: break
        print(js[max(0,i-60):i+120].replace("\n"," "))
        n+=1
        print("---")
