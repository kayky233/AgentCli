from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from .agent_types import Agent, Stage


class AgentRegistry:
    def __init__(self):
        self._agents: Dict[Stage, List[Agent]] = defaultdict(list)

    def register(self, stage: Stage, agent: Agent, priority: int = 0):
        agent.priority = priority
        self._agents[stage].append(agent)
        self._agents[stage].sort(key=lambda a: getattr(a, "priority", 0), reverse=True)

    def get(self, stage: Stage) -> List[Agent]:
        return list(self._agents.get(stage, []))

