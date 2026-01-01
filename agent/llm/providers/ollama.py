from __future__ import annotations

import time
from typing import Any, Dict

from ..types import LLMRequest, LLMResponse
from .base import LLMProvider


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    def generate(self, req: LLMRequest) -> LLMResponse:
        try:
            import requests
        except ImportError:
            return LLMResponse(ok=False, content="", error="requests not installed")
        payload: Dict[str, Any] = {
            "model": req.model,
            "messages": [msg.__dict__ for msg in req.messages],
            "stream": False,
        }
        url = f"{self.base_url}/api/chat"
        start = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=req.timeout)
            latency_ms = (time.time() - start) * 1000
            if resp.status_code != 200:
                return LLMResponse(ok=False, content="", latency_ms=latency_ms, error=f"http {resp.status_code}: {resp.text}")
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            return LLMResponse(ok=True, content=content, latency_ms=latency_ms)
        except Exception as exc:  # pragma: no cover
            return LLMResponse(ok=False, content="", error=str(exc))

