from __future__ import annotations

from src.sites.base import SiteAdapter
from src.sites.bol import BolSiteAdapter
from src.sites.generic import GenericSiteAdapter

_REGISTRY: dict[str, type[SiteAdapter]] = {
    "generic": GenericSiteAdapter,
    "bol": BolSiteAdapter,
    "bol.com": BolSiteAdapter,
}


def register_site(name: str, adapter_cls: type[SiteAdapter]) -> None:
    _REGISTRY[name.lower()] = adapter_cls


def get_site_adapter(site: str) -> SiteAdapter:
    cls = _REGISTRY.get(site.lower(), GenericSiteAdapter)
    return cls()
