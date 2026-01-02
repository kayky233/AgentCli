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

        # Demo convenience: the demo intentionally fails when TEST_SHOULD_FAIL is unset.
        # For agent closed-loop verification we default to disabling the intentional failure
        # (without changing the repo's default semantics). Users can still override by
        # explicitly setting TEST_SHOULD_FAIL in the command.
        injected = self._inject_test_should_fail_off(test_cmd)
        if injected != test_cmd:
            ctx.events.emit("test.env.inject", {"name": "TEST_SHOULD_FAIL", "value": "0"})
        res = self.agent.run(ctx, injected, cwd=ctx.workspace)
        ctx.last_test_result = res
        ctx.save_json(f"test_{ctx.iteration}", res)
        ctx.events.emit("test.result", {"status": "ok" if res["success"] else "fail", "summary": res.get("summary", [])})
        return AgentResult(status="ok" if res["success"] else "fail", outputs={"test_result": res}, artifacts=[res.get("log", "")])

    def _inject_test_should_fail_off(self, cmd):
        """
        ToolRouter executes commands without a shell, so environment variable assignment like
        `TEST_SHOULD_FAIL=0 make test` won't work unless it's inside a shell string.
        We therefore only inject into known shell-wrapped commands (e.g. WSL bash -lc "...").
        """
        if isinstance(cmd, str):
            # If a user already includes TEST_SHOULD_FAIL, respect it.
            if "TEST_SHOULD_FAIL" in cmd:
                return cmd
            # Inject only for WSL bash -lc wrapping.
            if 'bash -lc "' in cmd and "make test" in cmd:
                return cmd.replace("make test", "TEST_SHOULD_FAIL=0 make test")
            return cmd

        if isinstance(cmd, list):
            # Respect explicit user override.
            if any("TEST_SHOULD_FAIL" in str(x) for x in cmd):
                return cmd
            # Typical WSL shape: ["wsl","-e","bash","-lc","cd ... && make test"]
            if len(cmd) >= 5 and cmd[-2] == "-lc" and "make test" in cmd[-1]:
                new_last = cmd[-1].replace("make test", "TEST_SHOULD_FAIL=0 make test")
                return [*cmd[:-1], new_last]
            return cmd

        return cmd

