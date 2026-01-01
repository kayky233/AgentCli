from __future__ import annotations

from ..types import LLMRequest, LLMResponse
from .base import LLMProvider


class AgentCLIProvider(LLMProvider):
    name = "agent_cli"

    def generate(self, req: LLMRequest) -> LLMResponse:
        # Placeholder for internal agent CLI invocation
        return LLMResponse(ok=False, content="", error="AgentCLI provider not implemented")

