from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMRequest:
    model: str
    messages: List[ChatMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout: int = 60
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    ok: bool
    content: str
    latency_ms: Optional[float] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

