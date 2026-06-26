#!/usr/bin/env python3
"""Test RoundProxies connectivity (no bol login required)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp

from src.proxy.roundproxies import build_proxy_pool, load_roundproxies_config


async def main() -> None:
    creds_path = ROOT / "bol_credentials.json"
    rp_path = ROOT / "config" / "roundproxies.yaml"
    creds = {}
    if creds_path.exists():
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
    yaml_data = {}
    if rp_path.exists():
        import yaml
        yaml_data = yaml.safe_load(rp_path.read_text(encoding="utf-8")) or {}

    cfg = load_roundproxies_config(yaml_data, creds)
    if not cfg:
        print("RoundProxies not configured (or client_id still a placeholder).")
        print()
        print("1. Open https://app.roundproxies.com/dashboard/residential")
        print("2. Create / copy a proxy — note the Client name (e.g. client-MYNAME-...)")
        print("3. Edit config/roundproxies.yaml:")
        print('     client_id: MYNAME          # only the name after "client-"')
        print('     password: your_proxy_password')
        print()
        print("Or paste the full proxy line from the dashboard into:")
        print("  python scripts/roundproxies_setup.py")
        sys.exit(1)

    pool = build_proxy_pool(cfg)
    proxy = pool[0]
    print(f"Testing proxy (country={cfg.country})...")
    print(f"  host={cfg.host}:{cfg.port}")
    print(f"  user={proxy.split('@')[0].split('//')[1].split(':')[0][:50]}...")

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get("https://www.bol.com/nl/", proxy=proxy) as resp:
                print(f"  bol.com -> {resp.status} ({len(await resp.read())} bytes)")
                if resp.status == 200:
                    print("OK — RoundProxies working for bol.com")
                elif resp.status == 403:
                    print("403 — proxy works but Akamai may need sticky session / browser")
                else:
                    print(f"Unexpected status {resp.status}")
        except aiohttp.ClientHttpProxyError as exc:
            if exc.status == 407:
                print("407 Proxy Authentication Required")
                print("  Fix client_id + password in config/roundproxies.yaml")
                print("  client_id = Client name from residential dashboard (not login email)")
                print("  https://app.roundproxies.com/dashboard/residential")
                sys.exit(1)
            raise


if __name__ == "__main__":
    asyncio.run(main())
