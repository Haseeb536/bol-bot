"""bol.com cookie domains — must match Chrome (www vs .bol.com)."""
from __future__ import annotations

from typing import Dict, List

# From live basket → checkout capture (Chrome 148)
WWW_BOL_COM = frozenset(
    {
        "BUI",
        "bltgSessionId",
        "shopping_session_id",
        "XSRF-TOKEN",
        "locale",
        "language",
        "DYN_USER_ID",
        "DYN_USER_CONFIRM",
        "chatrToken",
    }
)

ROOT_BOL_COM = frozenset(
    {
        "XSC",
        "_abck",
        "ak_bmsc",
        "bm_sv",
        "bm_sz",
        "bm_so",
        "bm_lso",
        "sbsd",
        "sbsd_o",
        "bolConsentChoices",
    }
)


def cookie_domains(name: str) -> List[str]:
    if name in WWW_BOL_COM:
        return ["www.bol.com", ".www.bol.com"]
    if name in ROOT_BOL_COM:
        return [".bol.com", "www.bol.com"]
    return [".bol.com", "www.bol.com", ".www.bol.com"]


def parse_cookie_header(header: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def merge_cookie_dict(*sources: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for src in sources:
        for k, v in src.items():
            if v is not None and str(v).strip():
                merged[str(k)] = str(v)
    return merged
