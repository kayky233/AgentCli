from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import EventBus
from .agent_types import Stage


@dataclass
class RunContext:
    run_id: str
    task: str
    workspace: Path
    run_dir: Path
    options: Dict[str, Any]
    policy: Dict[str, Any]
    tool_router: Any
    run_manager: Any
    events: EventBus
    services: Dict[str, Any] = field(default_factory=dict)
    env_decision: Optional[Dict[str, Any]] = None
    context_pack: Optional[Dict[str, Any]] = None
    patch_queue: List[str] = field(default_factory=list)
    last_build_result: Optional[Dict[str, Any]] = None
    last_test_result: Optional[Dict[str, Any]] = None
    iteration: int = 0
    file_contents: Dict[str, str] = field(default_factory=dict)
    applied_files: List[str] = field(default_factory=list)

    def save_json(self, name: str, obj: Dict[str, Any]):
        import json

        path = self.run_dir / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return path

    def write_text(self, relpath: str | Path, text: str):
        path = self.run_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

