from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import TypeAdapter

from src.models.task import ProfileConfig, ProxyGroupConfig, TaskConfig
from src.proxy.roundproxies import build_proxy_pool, load_roundproxies_config
from src.utils.logging import get_logger

log = get_logger("tasks")

_task_list_adapter = TypeAdapter(List[TaskConfig])

_BOL_PRODUCT_ID_RE = re.compile(r"/(\d{10,})/?")
_BOL_SLUG_RE = re.compile(r"/p/([^/]+)/\d", re.I)

# Applied when tasks.yaml only lists product URLs (bol.com).
_BOL_TASK_DEFAULTS: Dict[str, Any] = {
    "site": "bol",
    "enabled": True,
    "profile": "bol_main",
    "proxy_group": "roundproxies",
    "quantity": 1,
    "payment_method": "ideal",
    "auto_checkout": True,
    "monitor_mode": "api_first",
    "retry": {
        "atc_max_attempts": 8,
        "atc_base_delay_ms": 300,
        "atc_max_delay_ms": 2000,
        "checkout_max_attempts": 4,
    },
}

_MAX_AFTERPAY_PRODUCTS = 5


def _extract_bol_product_id(url: str) -> Optional[str]:
    m = _BOL_PRODUCT_ID_RE.search(url)
    return m.group(1) if m else None


def _extract_bol_slug(url: str) -> Optional[str]:
    m = _BOL_SLUG_RE.search(url)
    return m.group(1) if m else None


def _normalize_task_entry(
    entry: Union[str, Dict[str, Any]],
    yaml_defaults: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    """
    Expand a bare URL (or minimal dict) into a full task config.

    tasks.yaml can be:
      products:
        - https://www.bol.com/nl/nl/p/.../9300000123456789/
    Optional overrides per line or under top-level `defaults:`.
    """
    if isinstance(entry, str):
        item: Dict[str, Any] = {"product_url": entry.strip()}
    elif isinstance(entry, dict):
        item = dict(entry)
        if "url" in item and "product_url" not in item:
            item["product_url"] = item.pop("url")
    else:
        raise ValueError(f"Invalid task entry at index {index}: {entry!r}")

    product_url = str(item.get("product_url") or "").strip()
    if not product_url:
        raise ValueError(f"Task at index {index} is missing product_url")

    is_bol = "bol.com" in product_url.lower()
    base = dict(_BOL_TASK_DEFAULTS if is_bol else {"site": "generic", "enabled": True})
    base.update(yaml_defaults)
    base.update(item)
    base["product_url"] = product_url

    meta = dict(base.get("metadata") or {})
    pid = meta.get("product_id") or _extract_bol_product_id(product_url)
    if pid:
        meta.setdefault("product_id", str(pid))
    slug = meta.get("product_slug") or _extract_bol_slug(product_url)
    if slug:
        meta.setdefault("product_slug", slug)
    for limit_key, default in (
        ("max_units_per_item", 2),
        ("max_items_per_checkout", 4),
    ):
        if limit_key not in meta:
            raw = yaml_defaults.get(limit_key, base.get(limit_key, default))
            try:
                meta[limit_key] = max(1, int(raw))
            except (TypeError, ValueError):
                meta[limit_key] = default
    base["metadata"] = meta

    if not base.get("id"):
        base["id"] = f"bol-monitor-{pid}" if pid else f"task-{index + 1}"

    if base.get("enabled") is False:
        base["enabled"] = False

    return base


class TaskStore:
    """Loads tasks, profiles, and proxy groups from YAML with hot-reload."""

    def __init__(
        self,
        tasks_path: Path,
        profiles_path: Path,
        proxies_path: Path,
        roundproxies_path: Optional[Path] = None,
        credentials_path: Optional[Path] = None,
    ) -> None:
        self.tasks_path = tasks_path
        self.profiles_path = profiles_path
        self.proxies_path = proxies_path
        self.roundproxies_path = roundproxies_path
        self.credentials_path = credentials_path
        self._tasks_mtime: float = 0.0
        self._tasks: List[TaskConfig] = []
        self._profiles: Dict[str, ProfileConfig] = {}
        self._proxy_groups: Dict[str, ProxyGroupConfig] = {}
        self.reload()

    def reload(self) -> None:
        self._profiles = self._load_profiles()
        self._proxy_groups = self._load_proxies()
        self._tasks = self._load_tasks()
        self._tasks_mtime = self.tasks_path.stat().st_mtime if self.tasks_path.exists() else 0.0

    async def reload_if_changed(self) -> bool:
        if not self.tasks_path.exists():
            return False
        mtime = self.tasks_path.stat().st_mtime
        if mtime > self._tasks_mtime:
            self.reload()
            return True
        return False

    def _load_yaml(self, path: Path) -> dict:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_tasks(self) -> List[TaskConfig]:
        data = self._load_yaml(self.tasks_path)
        yaml_defaults = data.get("defaults") or {}
        if not isinstance(yaml_defaults, dict):
            yaml_defaults = {}

        raw_entries: List[Union[str, Dict[str, Any]]] = []
        for key in ("products", "tasks", "urls"):
            chunk = data.get(key)
            if not chunk:
                continue
            if isinstance(chunk, list):
                raw_entries.extend(chunk)
            elif isinstance(chunk, str):
                raw_entries.append(chunk)

        afterpay_chunk = data.get("afterpay_products") or []
        if isinstance(afterpay_chunk, list):
            if len(afterpay_chunk) > _MAX_AFTERPAY_PRODUCTS:
                log.warning(
                    f"afterpay_products has {len(afterpay_chunk)} entries — "
                    f"only first {_MAX_AFTERPAY_PRODUCTS} use Afterpay (rest ignored)"
                )
            for entry in afterpay_chunk[:_MAX_AFTERPAY_PRODUCTS]:
                if isinstance(entry, str):
                    raw_entries.append(
                        {"product_url": entry.strip(), "payment_method": "afterpay"}
                    )
                elif isinstance(entry, dict):
                    item = dict(entry)
                    item["payment_method"] = "afterpay"
                    raw_entries.append(item)

        if not raw_entries:
            return []

        normalized = [
            _normalize_task_entry(entry, yaml_defaults, i)
            for i, entry in enumerate(raw_entries)
        ]
        return _task_list_adapter.validate_python(normalized)

    def _load_profiles(self) -> Dict[str, ProfileConfig]:
        data = self._load_yaml(self.profiles_path)
        profiles = {}
        for item in data.get("profiles", []):
            p = ProfileConfig.model_validate(item)
            profiles[p.name] = p
        if "default" not in profiles:
            profiles["default"] = ProfileConfig(name="default")
        return profiles

    def _load_credentials_json(self) -> dict:
        if not self.credentials_path or not self.credentials_path.exists():
            return {}
        try:
            with open(self.credentials_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning(f"Could not read credentials: {exc}")
            return {}

    def _load_roundproxies_yaml(self) -> dict:
        if not self.roundproxies_path or not self.roundproxies_path.exists():
            return {}
        return self._load_yaml(self.roundproxies_path)

    def _inject_roundproxies(self, groups: Dict[str, ProxyGroupConfig]) -> None:
        rp = load_roundproxies_config(
            self._load_roundproxies_yaml(),
            self._load_credentials_json(),
        )
        if not rp:
            return
        yaml_raw = self._load_roundproxies_yaml()
        extra = yaml_raw.get("proxy_lines") if isinstance(yaml_raw.get("proxy_lines"), list) else None
        try:
            pool = build_proxy_pool(rp, extra_lines=extra)
        except ValueError as exc:
            log.error(f"RoundProxies config invalid: {exc}")
            return
        target = groups.get("roundproxies")
        if target:
            target.proxies = list(dict.fromkeys(target.proxies + pool))
        else:
            groups["roundproxies"] = ProxyGroupConfig(
                name="roundproxies",
                proxies=pool,
                max_failures=5,
                health_check_url="https://www.bol.com/nl/",
            )
        log.info(f"RoundProxies: loaded {len(pool)} residential sessions (country={rp.country})")

    def _load_proxies(self) -> Dict[str, ProxyGroupConfig]:
        data = self._load_yaml(self.proxies_path)
        groups = {}
        for item in data.get("proxy_groups", []):
            g = ProxyGroupConfig.model_validate(item)
            groups[g.name] = g
        self._inject_roundproxies(groups)
        return groups

    def get_enabled_tasks(self) -> List[TaskConfig]:
        return [t for t in self._tasks if t.enabled]

    def get_profile(self, name: str) -> ProfileConfig:
        return self._profiles.get(name, ProfileConfig(name="default"))

    @property
    def proxy_groups(self) -> Dict[str, ProxyGroupConfig]:
        return self._proxy_groups
