from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import LLMRequest, LLMResponse


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, req: LLMRequest) -> LLMResponse:
        ...

