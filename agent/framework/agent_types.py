from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Protocol


class Stage(Enum):
    PLAN = auto()
    PREPARE = auto()
    GATHER = auto()
    EDIT = auto()
    APPLY = auto()
    VERIFY_BUILD = auto()
    VERIFY_TEST = auto()
    REVIEW = auto()
    FINALIZE = auto()


@dataclass
class AgentResult:
    status: str = "ok"  # ok|warn|fail|skip
    events: List[Any] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    suggest_next: Dict[str, Any] = field(default_factory=dict)


class Agent(Protocol):
    id: str
    stage: Stage
    priority: int

    def run(self, ctx, request=None) -> AgentResult:
        ...

