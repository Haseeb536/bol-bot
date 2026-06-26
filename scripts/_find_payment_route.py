#!/usr/bin/env python3
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _page_get, _request

s = ensure_session()
_init_session_holder(s)

# payment execution page
for url in (
    "https://www.bol.com/nl/payment-execution/",
    "https://www.bol.com/nl/nl/payment-execution/",
):
    r = _page_get(s, url, referer="https://www.bol.com/nl/nl/checkout/")
    print(url, r.status_code, len(r.text))
    chunks = sorted(set(re.findall(r"https://assets\.s-bol\.com/_remix/[^\"']+\.js", r.text)))
    for c in chunks[:15]:
        print(" ", c)
    if "payment" in r.text.lower():
        for m in re.findall(r"payment[^\"']{0,40}\.js", r.text, re.I)[:10]:
            print(" ref", m)

# search entry.client routes
ec = _request(s, "GET", "https://assets.s-bol.com/_remix/entry.client-C0eesXKS.js", timeout=90).text
for term in ("payment-execution", "paymentExecution", "firefly"):
    if term in ec:
        i = ec.find(term)
        print("entry.client", term, ec[max(0,i-50):i+150])
