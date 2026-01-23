from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


@dataclass
class SkillResult:
    ok: bool
    data: Any = None
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class Skill(Protocol):
    id: str
    description: str

    def run(self, ctx, **kwargs) -> SkillResult:
        ...

