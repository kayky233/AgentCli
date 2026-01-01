from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List

from .agent_types import Stage


@dataclass
class Event:
    ts: float
    type: str
    payload: Dict[str, Any]
    level: str = "info"


class EventBus:
    def __init__(self):
        self.events: List[Event] = []

    def emit(self, type_: str, payload: Dict[str, Any], level: str = "info"):
        evt = Event(ts=time.time(), type=type_, payload=payload, level=level)
        self.events.append(evt)
        return evt

    def stage_start(self, stage: Stage):
        self.emit("stage.start", {"stage": stage.name})

    def stage_end(self, stage: Stage, status: str = "ok"):
        self.emit("stage.end", {"stage": stage.name, "status": status})

    def agent_start(self, agent_id: str, stage: Stage):
        self.emit("agent.start", {"stage": stage.name, "agent": agent_id})

    def agent_end(self, agent_id: str, stage: Stage, status: str = "ok"):
        self.emit("agent.end", {"stage": stage.name, "agent": agent_id, "status": status})

    def flush_to(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self.events]
        import json

        with path.open("w", encoding="utf-8") as f:
            json.dump({"events": data}, f, ensure_ascii=False, indent=2)

