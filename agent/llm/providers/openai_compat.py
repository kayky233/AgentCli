from __future__ import annotations

import time
from typing import Any, Dict

from ..types import ChatMessage, LLMRequest, LLMResponse
from .base import LLMProvider


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def generate(self, req: LLMRequest) -> LLMResponse:
        try:
            import requests
        except ImportError:
            return LLMResponse(ok=False, content="", error="requests not installed")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": req.model,
            "messages": [msg.__dict__ for msg in req.messages],
        }
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        payload.update(req.extra or {})

        # Handle both base_url styles: /api and /api/v1
        if self.base_url.endswith("/v1"):
            url = f"{self.base_url}/chat/completions"
        else:
            url = f"{self.base_url}/v1/chat/completions"
        start = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=req.timeout)
            latency_ms = (time.time() - start) * 1000
            if resp.status_code != 200:
                return LLMResponse(ok=False, content="", latency_ms=latency_ms, error=f"http {resp.status_code}: {resp.text}")
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return LLMResponse(ok=True, content=content, latency_ms=latency_ms, usage=usage)
        except Exception as exc:  # pragma: no cover
            return LLMResponse(ok=False, content="", error=str(exc))

