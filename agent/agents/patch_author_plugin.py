from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..patch_author import PatchAuthor
from ..framework.agent_types import AgentResult, Stage


@dataclass
class PatchAuthorPlugin:
    id: str = "patch_author"
    stage: Stage = Stage.EDIT
    priority: int = 100

    def __post_init__(self):
        self.agent = PatchAuthor(tool_router=None, run_manager=None)

    def run(self, ctx, request=None) -> AgentResult:
        self.agent.tool_router = ctx.tool_router
        self.agent.run_manager = ctx.run_manager
        result = self.agent.generate(ctx, ctx.last_build_result or {})
        patches = result.get("patches", [])
        artifacts = []
        for idx, patch in enumerate(patches, start=1):
            path = ctx.run_manager.save_patch(ctx, idx, patch)
            ctx.patch_queue.append(str(path))
            artifacts.append(str(path))
        ctx.events.emit("patch.proposed", {"count": len(patches), "artifacts": artifacts})
        status = "ok" if patches else "skip"
        return AgentResult(status=status, artifacts=artifacts, outputs={"patches": patches, "notes": result.get("notes", [])})

