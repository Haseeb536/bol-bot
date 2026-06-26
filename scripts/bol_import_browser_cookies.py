#!/usr/bin/env python3
"""
Import a Cookie header from Chrome DevTools into bol_token.json + browser_cookies.txt.

Usage:
  1. On bol.com basket (logged in), DevTools → Network → any www.bol.com request
  2. Copy Request Headers → cookie: ...
  3. Save to browser_cookies.txt OR pass as argument

  python scripts/bol_import_browser_cookies.py
  python scripts/bol_import_browser_cookies.py "BUI=...; XSC=...; _abck=..."
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from bol_login import ensure_session, save_session, dedupe_cookies  # noqa: E402
from src.sites.bol_cookies import parse_cookie_header  # noqa: E402
from src.config.settings import get_settings  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        raw = " ".join(sys.argv[1:])
    else:
        path = get_settings().bol_token_path.parent / "browser_cookies.txt"
        if not path.is_file():
            print(f"Paste Cookie header into {path} or pass as argv")
            sys.exit(1)
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if raw.lower().startswith("cookie:"):
            raw = raw.split(":", 1)[1].strip()

    cookies = parse_cookie_header(raw)
    if "_abck" not in cookies:
        print("Warning: _abck missing — export cookies from www.bol.com while on basket/checkout")
    session = ensure_session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    dedupe_cookies(session)
    save_session(session, source="browser_cookies_import")
    out = get_settings().bol_token_path.parent / "browser_cookies.txt"
    out.write_text(raw if not raw.startswith("cookie") else raw, encoding="utf-8")
    print(f"Imported {len(cookies)} cookies → bol_token.json and {out.name}")


if __name__ == "__main__":
    main()
