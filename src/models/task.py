from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class MonitorMode(str, Enum):
    API_FIRST = "api_first"
    BROWSER_FALLBACK = "browser_fallback"
    BROWSER_ONLY = "browser_only"


class ProfileConfig(BaseModel):
    name: str = "default"
    email: Optional[str] = None
    password: Optional[str] = None
    shipping: Dict[str, Any] = Field(default_factory=dict)
    billing: Dict[str, Any] = Field(default_factory=dict)
    payment: Dict[str, Any] = Field(default_factory=dict)
    payment_method: str = "ideal"
    extra: Dict[str, Any] = Field(default_factory=dict)


class ProxyGroupConfig(BaseModel):
    name: str
    proxies: List[str] = Field(default_factory=list)
    max_failures: int = 3
    health_check_url: str = "https://www.bol.com/nl/"


class RetrySettings(BaseModel):
    atc_max_attempts: int = 8
    atc_base_delay_ms: int = 150
    atc_max_delay_ms: int = 3000
    checkout_max_attempts: int = 3
    request_timeout_sec: float = 20.0


class PollingIntervals(BaseModel):
    offline_min_sec: float = 3.0
    offline_max_sec: float = 7.0
    online_min_sec: float = 1.0
    online_max_sec: float = 2.0


class TaskConfig(BaseModel):
    id: str
    site: str = "generic"
    product_url: HttpUrl
    enabled: bool = True
    monitor_mode: MonitorMode = MonitorMode.API_FIRST
    profile: str = "default"
    proxy_group: Optional[str] = None
    quantity: int = 1
    payment_method: str = "ideal"
    auto_checkout: bool = True
    retry: RetrySettings = Field(default_factory=RetrySettings)
    polling: PollingIntervals = Field(default_factory=PollingIntervals)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}
