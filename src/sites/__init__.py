from src.sites.base import SiteAdapter
from src.sites.registry import get_site_adapter
from src.sites.generic import GenericSiteAdapter
from src.sites.bol import BolSiteAdapter

__all__ = [
    "SiteAdapter",
    "get_site_adapter",
    "GenericSiteAdapter",
    "BolSiteAdapter",
]
