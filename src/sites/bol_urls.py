"""bol.com product URL helpers — slug vs placeholder (-/) paths."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

BOL_PRODUCT_PATH = "/nl/nl/p"
BOL_ORIGIN = "https://www.bol.com"
BOL_BASKET_URL = f"{BOL_ORIGIN}/nl/nl/basket/"
BOL_CHECKOUT_URL = f"{BOL_ORIGIN}/nl/nl/checkout/"


def is_placeholder_product_url(url: str) -> bool:
    """True for bol short links like /p/-/9300000256665012/ (pre-title placeholder)."""
    return bool(re.search(r"/p/-/(\d{10,})/?", url))


def extract_slug_from_url(url: str) -> Optional[str]:
    m = re.search(r"/p/([^/]+)/(\d{10,})/?", url)
    if m and m.group(1) != "-":
        return m.group(1)
    return None


def build_product_url(product_id: str, slug: Optional[str] = None) -> str:
    pid = str(product_id).strip()
    if slug:
        return f"{BOL_ORIGIN}{BOL_PRODUCT_PATH}/{slug.strip('/')}/{pid}/"
    return f"{BOL_ORIGIN}{BOL_PRODUCT_PATH}/-/{pid}/"


def _load_credentials() -> Dict[str, Any]:
    from src.utils.app_root import get_app_root

    path = get_app_root() / "bol_credentials.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_product_url(
    product_id: str,
    configured_url: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Pick the canonical PDP URL (slug beats /-/ placeholder)."""
    meta = metadata or {}
    pid = str(product_id).strip()

    canonical = (meta.get("canonical_url") or meta.get("product_url") or "").strip()
    if canonical and pid in canonical and not is_placeholder_product_url(canonical):
        return canonical

    slug = (meta.get("product_slug") or meta.get("slug") or "").strip()
    if slug:
        return build_product_url(pid, slug)

    if is_placeholder_product_url(configured_url):
        return configured_url.strip()

    creds = _load_credentials()
    cred_url = (creds.get("product_url") or "").strip()
    if cred_url and pid in cred_url and not is_placeholder_product_url(cred_url):
        return cred_url

    cred_slug = extract_slug_from_url(cred_url)
    if cred_slug and pid in cred_url:
        return build_product_url(pid, cred_slug)

    return configured_url.strip() or build_product_url(pid)


def monitoring_urls_for_task(
    product_id: str,
    configured_url: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Ordered list of PDP URLs to try (canonical slug first)."""
    meta = metadata or {}
    primary = resolve_product_url(product_id, configured_url, meta)
    urls: List[str] = []

    def add(u: str) -> None:
        if u and u not in urls:
            urls.append(u)

    add(primary)
    slug = (meta.get("product_slug") or meta.get("slug") or "").strip()
    if not slug:
        slug = extract_slug_from_url(primary) or extract_slug_from_url(
            (_load_credentials().get("product_url") or "")
        )
    if slug:
        add(build_product_url(product_id, slug))
    if is_placeholder_product_url(configured_url):
        add(configured_url.strip())
    return urls
