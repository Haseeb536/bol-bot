from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import yaml

from src.config.settings import get_settings
from src.proxy.roundproxies import build_proxy_pool, load_roundproxies_config


def _load_roundproxies_merged() -> Optional[object]:
    settings = get_settings()
    yaml_data: dict = {}
    if settings.roundproxies_path.is_file():
        with open(settings.roundproxies_path, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
    cred_data: dict = {}
    if settings.credentials_path.is_file():
        try:
            cred_data = json.loads(settings.credentials_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return load_roundproxies_config(yaml_data, cred_data)


def get_roundproxies_config():
    return _load_roundproxies_merged()


def get_roundproxies_pool() -> list[str]:
    from src.config.settings import get_settings
    import yaml

    cfg = _load_roundproxies_merged()
    if not cfg:
        return []
    extra = None
    rp_path = get_settings().roundproxies_path
    if rp_path.is_file():
        data = yaml.safe_load(rp_path.read_text(encoding="utf-8")) or {}
        if isinstance(data.get("proxy_lines"), list):
            extra = data["proxy_lines"]
    try:
        return build_proxy_pool(cfg, extra_lines=extra)
    except ValueError:
        return []


def requests_proxy_dict(proxy_url: Optional[str] = None) -> Optional[Dict[str, str]]:
    """http(s) proxy dict for requests / curl_cffi."""
    url = proxy_url
    if not url:
        pool = get_roundproxies_pool()
        url = pool[0] if pool else None
    if not url:
        return None
    return {"http": url, "https": url}


def proxy_label(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return "direct"
    try:
        host = proxy_url.split("@")[-1].split(":")[0]
        return f"proxy:{host}"
    except Exception:
        return "proxy"
