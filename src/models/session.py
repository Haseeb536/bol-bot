from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SessionBundle:
    cookies: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    proxy_url: Optional[str] = None
    user_agent: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CartResult:
    success: bool
    verified: bool = False
    message: str = ""
    cart_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    success: bool
    payment_url: Optional[str] = None
    checkout_url: Optional[str] = None
    stage: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
