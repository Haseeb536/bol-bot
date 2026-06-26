from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from src.config.settings import get_settings
from src.models.task import ProfileConfig

_ENV_PATTERN = re.compile(r"^\$\{([^}]+)\}$")


def _read_credentials() -> dict:
    path = get_settings().credentials_path
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_env_value(value: Optional[str], *, cred_key: Optional[str] = None) -> Optional[str]:
    if not value:
        return value
    m = _ENV_PATTERN.match(value.strip())
    if not m:
        return value
    env_name = m.group(1)
    resolved = os.environ.get(env_name, "").strip()
    if resolved:
        return resolved
    cred = _read_credentials()
    if cred_key and cred.get(cred_key):
        return str(cred[cred_key])
    fallback = {
        "BOL_USERNAME": "username",
        "BOL_PASSWORD": "password",
    }
    alt = fallback.get(env_name)
    if alt and cred.get(alt):
        return str(cred[alt])
    return value


def resolve_profile(profile: ProfileConfig) -> ProfileConfig:
    """Expand ${BOL_USERNAME} etc. from env or bol_credentials.json."""
    email = resolve_env_value(profile.email, cred_key="username")
    password = resolve_env_value(profile.password, cred_key="password")
    if email == profile.email and password == profile.password:
        return profile
    return profile.model_copy(update={"email": email, "password": password})
