#!/usr/bin/env python3
"""
Merge browser cookies into bol_token.json (fixes missing Akamai _abck).

Usage:
  1. Chrome → bol.com (logged in) → DevTools → Network → any www.bol.com request
  2. Copy the full Request Header "cookie:" value (or all cookies from Application tab)
  3. Save to cookies.txt OR paste when prompted

  python scripts/bol_import_cookies.py cookies.txt
  python scripts/bol_import_cookies.py   # reads stdin

Then re-run: python scripts/bol_import_cookies.py && python scripts/bol_cart.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bol_login import (  # noqa: E402
    ROOT_DIR,
    dedupe_cookies,
    ensure_session,
    save_session,
    _parse_cookie_string,
    get_cookie_value,
)

TOKEN_FILE = os.path.join(ROOT_DIR, "bol_token.json")


def main() -> None:
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read().strip()
    else:
        login_txt = ROOT_DIR / "login.txt"
        if login_txt.is_file():
            from src.sites.akamai import parse_login_txt_cookie_header

            parsed = parse_login_txt_cookie_header(login_txt)
            if parsed:
                session = ensure_session()
                for name, value in parsed.items():
                    session.cookies.set(name, value, domain=".bol.com", path="/")
                dedupe_cookies(session)
                save_session(session, source="login_txt_import")
                print(f"Imported {len(parsed)} cookie(s) from login.txt -> {TOKEN_FILE}")
                if get_cookie_value(session, "_abck"):
                    print("  _abck: present")
                else:
                    print("  _abck: missing — export cookies from a loaded www.bol.com page")
                if get_cookie_value(session, "BUI"):
                    print("  BUI: present (logged in)")
                return
        print("Paste the Cookie header from Chrome (one line), then press Enter twice:")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip() and lines:
                break
            lines.append(line)
        raw = " ".join(lines).strip()

    if not raw:
        print("No cookies provided.")
        sys.exit(1)

    parsed = _parse_cookie_string(raw)
    if not parsed:
        print("Could not parse any cookies.")
        sys.exit(1)

    session = ensure_session()
    for name, value in parsed.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    dedupe_cookies(session)
    save_session(session, source="import_cookies")

    print(f"Imported {len(parsed)} cookie(s) into {TOKEN_FILE}")
    print("  names:", ", ".join(sorted(parsed.keys())))
    if get_cookie_value(session, "_abck"):
        print("  _abck: present")
    else:
        print("  _abck: still missing — copy cookies from a www.bol.com page after it loads fully")
    if get_cookie_value(session, "BUI"):
        print("  BUI: present (logged in)")


if __name__ == "__main__":
    main()
