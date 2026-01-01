from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..reposcout import RepoScout
from ..framework.agent_types import AgentResult, Stage


@dataclass
class RepoScoutPlugin:
    id: str = "reposcout"
    stage: Stage = Stage.GATHER
    priority: int = 100

    def __post_init__(self):
        self.agent = RepoScout(tool_router=None, run_manager=None)

    def run(self, ctx, request=None) -> AgentResult:
        # rebind router/manager to current ctx
        self.agent.tool_router = ctx.tool_router
        self.agent.run_manager = ctx.run_manager
        result = self.agent.gather(ctx, hints=request or [])
        ctx.context_pack = result
        ctx.save_json("context_pack", result)
        ctx.events.emit("gather.summary", {"terms": result.get("terms", []), "files": len(result.get("files", []))})
        return AgentResult(status="ok", outputs={"context_pack": result})

