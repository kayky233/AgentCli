from __future__ import annotations

from typing import List, Optional

from .agent_types import AgentResult, Stage
from .registry import AgentRegistry


class PipelineRunner:
    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def run_stage(self, stage: Stage, ctx, request: Optional[dict] = None) -> List[AgentResult]:
        results: List[AgentResult] = []
        ctx.events.emit("stage.enter", {"stage": stage.name})
        ctx.events.stage_start(stage)
        for agent in self.registry.get(stage):
            ctx.events.agent_start(agent.id, stage)
            try:
                res = agent.run(ctx, request)
            except Exception as exc:  # pragma: no cover
                ctx.events.emit("agent.error", {"stage": stage.name, "agent": agent.id, "error": str(exc)}, level="error")
                res = AgentResult(status="fail")
            results.append(res)
            ctx.events.agent_end(agent.id, stage, status=res.status)
            if res.events:
                for evt in res.events:
                    ctx.events.emit(evt.type, evt.payload, evt.level)
            if res.status == "fail":
                break
        ctx.events.stage_end(stage, status=_stage_status(results))
        ctx.events.emit("stage.exit", {"stage": stage.name, "status": _stage_status(results)})
        return results


def _stage_status(results: List[AgentResult]) -> str:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"

