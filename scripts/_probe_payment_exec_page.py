#!/usr/bin/env python3
"""Follow payment-execution page and look for iDEAL redirect URL."""
import os, re, sys
os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _prime_www, _request, _page_get

OFFERING = os.environ.get("BOL_OFFERING_ID", "702753242")
URL = f"https://www.bol.com/nl/payment-execution/?offeringId={OFFERING}&paymentMethod=IDEAL"

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
r = _page_get(s, URL, referer="https://www.bol.com/nl/nl/checkout/")
print("status", r.status_code, "len", len(r.text), "final", r.url)
for pat in [
    r"https?://[^\"'\s]*ideal[^\"'\s]*",
    r"redirectUrl[\"']?\s*[:=]\s*[\"']([^\"']+)",
    r"location\.href\s*=\s*[\"']([^\"']+)",
]:
    ms = re.findall(pat, r.text, re.I)
    if ms:
        print(pat[:30], ms[:5])
# try graphql createPayment APQ from bol_checkout
from bol_checkout import run_ideal_checkout
res = run_ideal_checkout(s, verbose=False)
print("run_ideal_checkout", res)
