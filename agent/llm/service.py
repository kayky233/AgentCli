from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from .providers.agent_cli import AgentCLIProvider
from .providers.base import LLMProvider
from .providers.ollama import OllamaProvider
from .providers.openai_compat import OpenAICompatProvider
from .types import ChatMessage, LLMRequest, LLMResponse


def _load_provider_from_env() -> Optional[LLMProvider]:
    provider_name = os.environ.get("AGENT_LLM_PROVIDER", "openai_compat").strip()
    base_url = os.environ.get("AGENT_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("AGENT_LLM_API_KEY", "").strip()
    if provider_name == "openai_compat":
        # normalize openrouter base url if user omits /api
        if base_url.startswith("https://openrouter.ai") and "/api" not in base_url:
            base_url = base_url.rstrip("/") + "/api"
        if not base_url or not api_key:
            return None
        return OpenAICompatProvider(base_url=base_url, api_key=api_key)
    if provider_name == "ollama":
        return OllamaProvider(base_url=base_url or "http://localhost:11434")
    if provider_name == "agent_cli":
        return AgentCLIProvider()
    return None


class LLMService:
    def __init__(self, provider: Optional[LLMProvider]):
        self.provider = provider
        self.model = os.environ.get("AGENT_LLM_MODEL", "gpt-4.1-mini")
        self.timeout = int(os.environ.get("AGENT_LLM_TIMEOUT", "60"))
        self.max_tokens = int(os.environ.get("AGENT_LLM_MAX_TOKENS", "2048"))
        self.temperature = float(os.environ.get("AGENT_LLM_TEMPERATURE", "0.2"))
        self.max_retries = 2

    @classmethod
    def from_env(cls) -> "LLMService":
        provider = _load_provider_from_env()
        return cls(provider)

    def enabled(self) -> bool:
        return self.provider is not None

    def generate_patch(self, messages: List[ChatMessage]) -> Dict[str, any]:
        if not self.provider:
            return {"ok": False, "error": "LLM provider not configured", "content": ""}
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            req = LLMRequest(
                messages=messages,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                timeout=self.timeout,
            )
            resp = self.provider.generate(req)
            if not resp.ok:
                last_error = resp.error or "unknown error"
                continue
            if self._is_valid_response(resp.content):
                return {
                    "ok": True,
                    "content": resp.content,
                    "latency_ms": resp.latency_ms,
                    "usage": resp.usage,
                    "attempt": attempt,
                }
            last_error = f"invalid response format (content: {resp.content[:200]}...)"
        return {"ok": False, "error": last_error or "failed", "content": ""}

    def _is_valid_response(self, content: str) -> bool:
        if not content:
            return False
        # Accept both JSON (Search & Replace) and unified diff formats
        return ("diff --git" in content) or ("[" in content and "{" in content)

