#!/usr/bin/env python3
"""
Quick diagnostic: fetch login.bol.com/wsp/login and show key sections.
Run this to find where CSRF token and crvtoken are embedded.

Usage:
    python debug_login_page.py
"""
import re
import requests

LOGIN_PAGE_URL = "https://login.bol.com/wsp/login"
OUTPUT_FILE = "login_page_debug.html"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

session = requests.Session()
resp = session.get(LOGIN_PAGE_URL, headers=HEADERS, timeout=20)
html = resp.text

print(f"Status: {resp.status_code}")
print(f"Content-Length: {len(html)}")
print()

# --- Response headers that might carry CSRF ---
print("=== RESPONSE HEADERS (relevant) ===")
for k, v in resp.headers.items():
    if any(x in k.lower() for x in ("csrf", "xsrf", "token", "set-cookie")):
        print(f"  {k}: {v[:120]}")
print()

# --- Cookies set ---
print("=== COOKIES SET ===")
for c in session.cookies:
    print(f"  {c.domain} | {c.name}={c.value[:60]}")
print()

# --- All <meta> tags ---
print("=== META TAGS ===")
for m in re.findall(r'<meta[^>]+>', html, re.IGNORECASE):
    print(f"  {m[:200]}")
print()

# --- All <input type="hidden"> fields ---
print("=== HIDDEN INPUT FIELDS ===")
for m in re.findall(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE):
    print(f"  {m[:200]}")
print()

# --- Lines containing csrf / crvtoken / captcha ---
print("=== LINES WITH: csrf | crvtoken | captcha | nonce ===")
for i, line in enumerate(html.splitlines(), 1):
    low = line.lower()
    if any(x in low for x in ("csrf", "crvtoken", "captcha", "nonce", "recaptcha")):
        print(f"  L{i:4d}: {line.strip()[:200]}")
print()

# --- Save full HTML ---
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write(html)
print(f"Full HTML saved to: {OUTPUT_FILE}")
