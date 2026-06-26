from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.config.settings import ROOT_DIR

_DISCORD_PATH = ROOT_DIR / "config" / "discord.yaml"


def resolve_discord_webhook_url() -> Optional[str]:
    env = os.environ.get("ECOM_DISCORD_WEBHOOK_URL", "").strip()
    if env:
        return env
    env_legacy = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env_legacy:
        return env_legacy
    if not _DISCORD_PATH.is_file():
        return None
    try:
        data = yaml.safe_load(_DISCORD_PATH.read_text(encoding="utf-8")) or {}
        url = (data.get("webhook_url") or data.get("url") or "").strip()
        return url or None
    except Exception:
        return None
