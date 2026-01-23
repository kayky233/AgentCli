from __future__ import annotations

from typing import Dict, List

from .base import Skill, SkillResult


class SkillRegistry:
    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.id in self._skills:
            raise ValueError(f"Skill id already registered: {skill.id}")
        self._skills[skill.id] = skill

    def get(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise KeyError(f"Skill not found: {skill_id}")
        return self._skills[skill_id]

    def list(self) -> List[str]:
        return list(self._skills.keys())

    def run(self, skill_id: str, ctx, **kwargs) -> SkillResult:
        skill = self.get(skill_id)
        return skill.run(ctx, **kwargs)

