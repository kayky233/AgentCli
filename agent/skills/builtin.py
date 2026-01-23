from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .base import SkillResult


@dataclass
class SearchSkill:
    id: str = "search"
    description: str = "搜索仓库文本内容（rg 优先）"

    def run(self, ctx, pattern: str, cwd: Optional[Path] = None, **kwargs) -> SkillResult:
        try:
            text = ctx.tool_router.search(pattern, cwd=cwd)
            return SkillResult(ok=True, data=text)
        except Exception as exc:
            return SkillResult(ok=False, error=str(exc))


@dataclass
class ReadFileSkill:
    id: str = "read_file"
    description: str = "读取文件内容（raw 或带行号）"

    def run(
        self,
        ctx,
        path: str | Path,
        start: Optional[int] = None,
        end: Optional[int] = None,
        mode: str = "raw",
        **kwargs,
    ) -> SkillResult:
        try:
            target = Path(path)
            if not target.is_absolute():
                repo_root = Path(getattr(ctx.tool_router, "repo_root", "."))
                target = repo_root / target
            if mode == "numbered":
                text = ctx.tool_router.read_file(target, start=start, end=end)
            else:
                text = target.read_text(encoding="utf-8", errors="replace")
            return SkillResult(ok=True, data=text)
        except Exception as exc:
            return SkillResult(ok=False, error=str(exc))


@dataclass
class RunCommandSkill:
    id: str = "run_command"
    description: str = "执行外部命令并返回 stdout/stderr"

    def run(self, ctx, cmd: Any, cwd: Optional[Path] = None, timeout: Optional[int] = None, **kwargs) -> SkillResult:
        try:
            res = ctx.tool_router.run_command(cmd, cwd=cwd, timeout=timeout)
            return SkillResult(ok=True, data=res)
        except Exception as exc:
            return SkillResult(ok=False, error=str(exc))

