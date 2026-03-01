from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.framework.agent_types import AgentResult, Stage
from agent.utils import truncate


@dataclass
class AiderEditPlugin:
    """
    EDIT 阶段基于 aider 命令行的编辑 Agent。

    行为：
    - 从 ctx / request 中获取任务描述与相关文件列表
    - 在仓库根目录下调用 `aider` CLI
    - 让 aider 直接在工作区中编辑代码
    """

    id: str = "aider_edit"
    stage: Stage = Stage.EDIT
    priority: int = 90

    aider_cmd: str = os.environ.get("AIDER_CLI", "aider")
    max_log_chars: int = int(os.environ.get("AGENT_AIDER_MAX_LOG_CHARS", "8000"))
    timeout: int = int(os.environ.get("AGENT_AIDER_TIMEOUT", "3600"))

    def run(self, ctx, request: Optional[dict] = None) -> AgentResult:
        task_text = self._resolve_task_text(ctx, request)
        files = self._resolve_files(ctx, request)

        if not task_text:
            return AgentResult(
                status="skip",
                outputs={"notes": ["AiderEditPlugin: task text is empty; nothing to do."]},
            )

        if not files:
            ctx.events.emit(
                "aider_edit.no_files",
                {"reason": "no files detected; invoking aider without file args"},
            )

        repo_root: Path = getattr(ctx, "repo_root", Path("."))
        workdir = self._resolve_workdir(ctx, repo_root)

        cmd = self._build_aider_command(task_text, files)
        ctx.events.emit(
            "aider_edit.start",
            {"cmd": shlex.join(cmd), "cwd": str(workdir), "files": files},
        )

        result = self._run_subprocess(cmd, cwd=workdir)
        returncode = result.get("returncode", 1)
        status = "ok" if returncode == 0 else "error"

        self._persist_logs(ctx, cmd, result)

        message = (
            f"aider exited with code {returncode}\n\n"
            f"stdout (truncated):\n{truncate(result.get('stdout', ''), self.max_log_chars)}\n\n"
            f"stderr (truncated):\n{truncate(result.get('stderr', ''), self.max_log_chars)}"
        )

        ctx.events.emit(
            "aider_edit.finish",
            {
                "returncode": returncode,
                "status": status,
            },
        )

        return AgentResult(
            status=status,
            message=message,
            outputs={
                "cmd": cmd,
                "cwd": str(workdir),
                "returncode": returncode,
                "stdout": truncate(result.get("stdout", ""), self.max_log_chars),
                "stderr": truncate(result.get("stderr", ""), self.max_log_chars),
                "files": files,
                "task": task_text,
            },
        )

    # ---------- helpers ----------

    def _resolve_task_text(self, ctx, request: Optional[dict]) -> str:
        if request and isinstance(request, dict) and request.get("task"):
            return str(request["task"])
        if getattr(ctx, "task", None):
            return str(ctx.task)
        state = getattr(ctx, "state", None) or getattr(ctx, "run_state", None)
        if state and getattr(state, "task", None):
            return str(state.task)
        return ""

    def _resolve_files(self, ctx, request: Optional[dict]) -> List[str]:
        if request and isinstance(request, dict) and request.get("files"):
            files = request["files"]
            if isinstance(files, list):
                return [str(f) for f in files]

        for attr in ("edit_files", "target_files", "files"):
            if hasattr(ctx, attr):
                val = getattr(ctx, attr)
                if isinstance(val, list):
                    return [str(f) for f in val]

        state = getattr(ctx, "state", None) or getattr(ctx, "run_state", None)
        if state:
            for attr in ("edit_files", "target_files", "files"):
                if hasattr(state, attr):
                    val = getattr(state, attr)
                    if isinstance(val, list):
                        return [str(f) for f in val]

        # 兜底：如果有 context_pack.files，就抽取路径前缀
        files: List[str] = []
        context_pack = getattr(ctx, "context_pack", None) or {}
        for item in context_pack.get("files", []):
            if isinstance(item, dict):
                for m in item.get("matches", []):
                    path = m.split(":", 1)[0]
                    if path and path not in files:
                        files.append(path)
        return files

    def _resolve_workdir(self, ctx, repo_root: Path) -> Path:
        workdir = getattr(ctx, "workspace", None) or getattr(ctx, "workdir", None)
        if workdir:
            return Path(workdir)
        return Path(repo_root)

    def _build_aider_command(self, task_text: str, files: List[str]) -> List[str]:
        cmd: List[str] = shlex.split(self.aider_cmd)
        extra = os.environ.get("AIDER_EXTRA_ARGS", "").strip()
        if extra:
            cmd.extend(shlex.split(extra))
        cmd.extend(["--message", task_text])
        cmd.extend(files)
        return cmd

    def _run_subprocess(self, cmd: List[str], cwd: Path) -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "returncode": -1,
                "stdout": e.stdout or "",
                "stderr": (e.stderr or "") + f"\nAiderEditPlugin: process timed out after {self.timeout}s",
            }
        except FileNotFoundError:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"AiderEditPlugin: aider command not found: {self.aider_cmd}",
            }
        except Exception as e:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"AiderEditPlugin: unexpected error: {e}",
            }

    def _persist_logs(self, ctx, cmd: List[str], result: Dict[str, Any]) -> None:
        try:
            payload = {
                "cmd": cmd,
                "returncode": result.get("returncode"),
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
            }
            ctx.save_json("aider_edit_log.json", payload)
        except Exception:
            pass
