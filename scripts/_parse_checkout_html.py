#!/usr/bin/env python3
import os
import re
import sys

os.environ.setdefault("BOL_NO_PROXY", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bol_login import ensure_session
from bol_cart import _init_session_holder, _prime_www, _page_get

CHECKOUT = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"

s = ensure_session()
_init_session_holder(s)
_prime_www(s)
html = _page_get(s, CHECKOUT, referer="https://www.bol.com/nl/nl/basket/").text
print("len", len(html))
for label, pat in [
    ("hash", r"hash"),
    ("PaymentOffering", r"PaymentOffering"),
    ("paymentPlan", r"paymentPlan"),
    ("orderId", r"orderId"),
    ("order", r"\\\"order\\\""),
    ("702753", r"702753\d+"),
    ("basket", r"73d8079b"),
    ("status", r"orderStatus|checkoutStatus|paymentStatus"),
]:
    ms = re.findall(pat, html[:500000], re.I)[:5]
    print(label, ms)

# dehydrated keys
keys = sorted(set(re.findall(r'\\"([a-zA-Z][a-zA-Z0-9]{2,40})\\"', html[:300000])))
print("dehydrated keys sample", [k for k in keys if "order" in k.lower() or "pay" in k.lower() or "plan" in k.lower()][:30])

for key in (
    "checkoutExecutePaymentUrl",
    "PaymentOffering",
    "PaymentPlan",
    "hash",
    "pageId",
    "xsrf",
):
    m = re.search(r'\\"' + re.escape(key) + r'\\"[,\s]*\\"([^\\"]+)\\"', html)
    print(key, "=", (m.group(1)[:200] if m else "N/A"))

m = re.search(r'PaymentOffering\\"[,\s]*\\"(\d+)\\"', html)
print("offering num", m.group(1) if m else "N/A")

for key in ("OrderStatus", "orderStatus", "checkoutStep", "currentStep"):
    m = re.search(r'\\"' + re.escape(key) + r'\\"[,\s]*\\"([^\\"]+)\\"', html)
    if m:
        print(key, "=", m.group(1)[:80])

# line item ids
items = re.findall(
    r'BasketDeliveryOrderItem\\"[^}]{0,200}?\\"id\\"[,\s]*\\"([^\\"]+)\\"',
    html,
)
print("line items", items[:5])
