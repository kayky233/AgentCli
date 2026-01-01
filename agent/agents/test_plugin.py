from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..tester import TestTriage
from ..framework.agent_types import AgentResult, Stage


@dataclass
class TestPlugin:
    id: str = "test"
    stage: Stage = Stage.VERIFY_TEST
    priority: int = 100

    def __post_init__(self):
        self.agent = TestTriage(tool_router=None, run_manager=None)

    def run(self, ctx, request=None) -> AgentResult:
        self.agent.tool_router = ctx.tool_router
        self.agent.run_manager = ctx.run_manager
        test_cmd = (ctx.env_decision or {}).get("commands", {}).get("test")
        if not test_cmd:
            ctx.events.emit("test.result", {"status": "fail", "error": "no test command"})
            return AgentResult(status="fail")
        res = self.agent.run(ctx, test_cmd, cwd=ctx.workspace)
        ctx.last_test_result = res
        ctx.save_json(f"test_{ctx.iteration}", res)
        ctx.events.emit("test.result", {"status": "ok" if res["success"] else "fail", "summary": res.get("summary", [])})
        return AgentResult(status="ok" if res["success"] else "fail", outputs={"test_result": res}, artifacts=[res.get("log", "")])

