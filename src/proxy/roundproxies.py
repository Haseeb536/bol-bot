"""
RoundProxies residential proxy URL builder.

Docs: https://docs.roundproxies.com/residentials/basics

Auth username format:
  client-{CLIENT_ID}-country-{COUNTRY}-session-{SESSION_ID}

HTTP URL for aiohttp/Playwright:
  http://{username}:{password}@residential.roundproxies.com:5000
"""

from __future__ import annotations

import os
import secrets
import string
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RoundProxiesConfig(BaseModel):
    enabled: bool = False
    host: str = "residential.roundproxies.com"
    port: int = 5000
    client_id: str = ""
    password: str = ""
    country: str = "Netherlands"
    state: Optional[str] = None
    city: Optional[str] = None
    session_count: int = 5
    session_prefix: str = "bol"

    model_config = {"extra": "ignore"}


def _slug(value: str) -> str:
    """RoundProxies uses hyphenated names (e.g. country-Netherlands)."""
    return value.strip().replace(" ", "").replace("_", "-")


def _random_session_id(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_username(cfg: RoundProxiesConfig, session_id: Optional[str] = None) -> str:
    if not cfg.client_id:
        raise ValueError("roundproxies client_id is required (from dashboard)")
    sid = session_id or _random_session_id()
    parts = [
        f"client-{_slug(cfg.client_id)}",
        f"country-{_slug(cfg.country)}",
    ]
    if cfg.state:
        parts.append(f"state-{_slug(cfg.state)}")
    if cfg.city:
        parts.append(f"city-{_slug(cfg.city)}")
    parts.append(f"session-{cfg.session_prefix}{sid}")
    return "-".join(parts)


def build_proxy_url(cfg: RoundProxiesConfig, session_id: Optional[str] = None) -> str:
    if not cfg.password:
        raise ValueError("roundproxies password is required")
    username = build_username(cfg, session_id=session_id)
    return f"http://{username}:{cfg.password}@{cfg.host}:{cfg.port}"


def ensure_sticky_session_username(username: str, session_id: str) -> str:
    """Append -session-{id} when dashboard line has no sticky session (avoids IP rotation)."""
    if "-session-" in username:
        return username
    return f"{username}-session-{session_id}"


def proxy_line_to_http_url(
    line: str,
    *,
    session_id: Optional[str] = None,
    session_prefix: str = "bol",
) -> str:
    """Convert dashboard line host:port:user:pass to http://user:pass@host:port."""
    import re

    raw = line.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    m = re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", raw)
    if not m:
        raise ValueError(f"Invalid proxy line: {line[:60]}...")
    host, port, user, password = m.group(1), m.group(2), m.group(3), m.group(4)
    sid = session_id or _random_session_id()
    user = ensure_sticky_session_username(user, f"{session_prefix}{sid}")
    return f"http://{user}:{password}@{host}:{port}"


def build_proxy_pool(
    cfg: RoundProxiesConfig,
    *,
    extra_lines: Optional[List[str]] = None,
) -> List[str]:
    """Dashboard lines (sticky) first, then generated sessions — Netherlands before Uganda."""
    pool: List[str] = []
    for line in extra_lines or []:
        try:
            pool.append(
                proxy_line_to_http_url(
                    line,
                    session_prefix=cfg.session_prefix,
                )
            )
        except ValueError:
            continue
    count = max(1, cfg.session_count)
    pool.extend(
        build_proxy_url(cfg, session_id=_random_session_id()) for _ in range(count)
    )
    return list(dict.fromkeys(pool))


_PLACEHOLDER_CLIENT_IDS = frozenset(
    {
        "",
        "replace_with_dashboard_client_name",
        "your_client_name",
        "your_client_name_from_dashboard",
        "your_client_id",
    }
)


def client_id_is_placeholder(client_id: str) -> bool:
    return client_id.strip().lower().replace("-", "_") in _PLACEHOLDER_CLIENT_IDS


def load_roundproxies_config(
    yaml_data: Optional[Dict[str, Any]] = None,
    credentials_data: Optional[Dict[str, Any]] = None,
) -> Optional[RoundProxiesConfig]:
    merged: Dict[str, Any] = {}
    if yaml_data:
        merged.update(yaml_data)
    if credentials_data:
        rp = credentials_data.get("roundproxies")
        if isinstance(rp, dict):
            merged.update(rp)
    env_map = {
        "client_id": "ROUNDPROXIES_CLIENT_ID",
        "password": "ROUNDPROXIES_PASSWORD",
        "country": "ROUNDPROXIES_COUNTRY",
        "host": "ROUNDPROXIES_HOST",
        "port": "ROUNDPROXIES_PORT",
    }
    for field, env_name in env_map.items():
        if os.environ.get(env_name):
            merged[field] = os.environ[env_name]
    if os.environ.get("ROUNDPROXIES_ENABLED", "").lower() in ("1", "true", "yes"):
        merged["enabled"] = True
    if not merged.get("enabled") and not merged.get("client_id"):
        return None
    cfg = RoundProxiesConfig.model_validate(merged)
    if not cfg.enabled and not (cfg.client_id and cfg.password):
        return None
    if cfg.client_id and cfg.password:
        cfg.enabled = True
    if not cfg.enabled:
        return None
    if client_id_is_placeholder(cfg.client_id):
        return None
    return cfg
