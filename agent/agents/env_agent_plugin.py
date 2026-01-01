from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..env_agent import EnvAgent, EnvRequest
from ..framework.agent_types import AgentResult, Stage


@dataclass
class EnvAgentPlugin:
    id: str = "env_agent"
    stage: Stage = Stage.PREPARE
    priority: int = 100

    def __post_init__(self):
        self.agent = EnvAgent()

    def run(self, ctx, request=None) -> AgentResult:
        opts = ctx.options or {}
        req = EnvRequest(
            workspace=ctx.workspace,
            preferred_build="make -j",
            preferred_test="make test",
            interactive=opts.get("interactive", True),
            allow_wsl=opts.get("allow_wsl", True),
            allow_fallback=opts.get("allow_fallback", True),
            prefer_gnu_make=True,
            override_make_cmd=opts.get("make_cmd"),
            override_use_wsl=opts.get("use_wsl", False),
            force_strategy=opts.get("force_strategy"),
        )
        decision = self.agent.decide(req)
        ctx.env_decision = decision.__dict__
        ctx.save_json("env_decision", ctx.env_decision)
        ctx.events.emit("env.decision", {"strategy": decision.strategy, "commands": decision.commands, "warnings": decision.warnings})
        return AgentResult(
            status="ok" if decision.strategy != "error" else "fail",
            outputs={"env_decision": ctx.env_decision},
        )

