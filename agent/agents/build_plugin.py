from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..build import BuildDiagnoser
from ..framework.agent_types import AgentResult, Stage


@dataclass
class BuildPlugin:
    id: str = "build"
    stage: Stage = Stage.VERIFY_BUILD
    priority: int = 100

    def __post_init__(self):
        self.agent = BuildDiagnoser(tool_router=None, run_manager=None)

    def run(self, ctx, request=None) -> AgentResult:
        self.agent.tool_router = ctx.tool_router
        self.agent.run_manager = ctx.run_manager
        build_cmd = (ctx.env_decision or {}).get("commands", {}).get("build")
        if not build_cmd:
            ctx.events.emit("build.result", {"status": "fail", "error": "no build command"})
            return AgentResult(status="fail")
        res = self.agent.run(ctx, build_cmd, cwd=ctx.workspace)
        ctx.last_build_result = res
        ctx.save_json(f"build_{ctx.iteration}", res)
        ctx.events.emit("build.result", {"status": "ok" if res["success"] else "fail", "summary": res.get("summary", [])})
        return AgentResult(status="ok" if res["success"] else "fail", outputs={"build_result": res}, artifacts=[res.get("log", "")])

