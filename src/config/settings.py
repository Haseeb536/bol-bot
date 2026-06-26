from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils.app_root import get_app_root

ROOT_DIR = get_app_root()


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_prefix="ECOM_",
        extra="ignore",
    )

    log_level: str = "INFO"
    log_file: Optional[str] = "logs/bot.log"
    headless: bool = True
    max_concurrent_tasks: int = 200
    default_proxy_url: Optional[str] = None
    tasks_path: Path = Field(default=ROOT_DIR / "tasks" / "tasks.yaml")
    profiles_path: Path = Field(default=ROOT_DIR / "config" / "profiles.yaml")
    proxies_path: Path = Field(default=ROOT_DIR / "config" / "proxies.yaml")
    roundproxies_path: Path = Field(default=ROOT_DIR / "config" / "roundproxies.yaml")
    credentials_path: Path = Field(default=ROOT_DIR / "bol_credentials.json")
    bol_token_path: Path = Field(default=ROOT_DIR / "bol_token.json")
    browser_data_dir: Path = Field(default=ROOT_DIR / "data" / "browser")
    sessions_dir: Path = Field(default=ROOT_DIR / "data" / "sessions")
    hot_reload_interval_sec: float = 5.0


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
