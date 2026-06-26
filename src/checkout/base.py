from __future__ import annotations

from abc import ABC, abstractmethod

from src.models.session import CheckoutResult
from src.models.task import ProfileConfig, TaskConfig


class CheckoutFlow(ABC):
    @abstractmethod
    async def run(self, task: TaskConfig, profile: ProfileConfig) -> CheckoutResult:
        ...
