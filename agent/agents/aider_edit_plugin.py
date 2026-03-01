from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
        workdir = getattr(ctx, "workspace", Path("."))

        cmd = self._build_aider_command(ctx, task_text, files, workdir)
        ctx.events.emit(
            "aider_edit.start",
            {"cmd": shlex.join(cmd), "cwd": str(workdir), "files": files},
        )

        # Capture existing file state (for existence-based success detection)
        pre_existing_files = {str(Path(workdir) / f) for f in files}

        result = self._run_subprocess(cmd, cwd=workdir)
        if result.get("stdout"):
            print(result["stdout"])
        returncode = result.get("returncode", 1)

        # Determine status:
        # - ok if returncode == 0
        # - additionally, even if git diff is empty, consider success if aider created any of the target files
        status = "ok" if returncode == 0 else "error"

        if returncode == 0 and files:
            post_existing_files = {str(Path(workdir) / f) for f in files if (Path(workdir) / f).exists()}
            created_files = sorted(post_existing_files - pre_existing_files)
            if created_files:
                # Treat as successful edit because files were created
                status = "ok"

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
        return getattr(ctx, "workspace", repo_root)

    def _build_aider_command(self, ctx, task_text: str, files: List[str], workdir: Path) -> List[str]:
        cmd: List[str] = shlex.split(self.aider_cmd)

        # Ensure --no-git is present
        if "--no-git" not in cmd:
            cmd.append("--no-git")

        # Ensure --yes is present to avoid interactive prompts
        if "--yes" not in cmd:
            cmd.append("--yes")

        # Ensure model is explicitly set to gpt-5.1 (override any existing --model)
        # Remove any existing --model and its value
        cleaned_cmd: List[str] = []
        skip_next = False
        for i, part in enumerate(cmd):
            if skip_next:
                skip_next = False
                continue
            if part == "--model":
                skip_next = True
                continue
            if part.startswith("--model="):
                continue
            cleaned_cmd.append(part)
        cmd = cleaned_cmd
        cmd.extend(["--model", "gpt-5.1"])

        extra = os.environ.get("AIDER_EXTRA_ARGS", "").strip()
        if extra:
            extra_parts = shlex.split(extra)
            # Avoid duplicating --no-git or overriding model from AIDER_EXTRA_ARGS
            skip_next = False
            for part in extra_parts:
                if skip_next:
                    skip_next = False
                    continue
                if part == "--no-git" and "--no-git" in cmd:
                    continue
                if part == "--model":
                    skip_next = True
                    continue
                if part.startswith("--model="):
                    continue
                cmd.append(part)

        # Auto-include requirements.json if present in isolated workspace
        req_path = workdir / "requirements.json"
        if req_path.is_file():
            req_str = str(req_path.relative_to(workdir))
            if req_str not in files:
                files.append(req_str)

        # Auto-include all .py files under the isolated workspace only
        try:
            py_files: Set[str] = set()
            for p in workdir.rglob("*.py"):
                try:
                    rel = p.relative_to(workdir)
                except ValueError:
                    continue
                py_files.add(str(rel))
            for pf in sorted(py_files):
                if pf not in files:
                    files.append(pf)
        except Exception:
            # Best-effort; don't fail the whole run on filesystem issues
            pass

        # Strengthen the message to instruct aider to create missing files and use gpt-5.1
        # Also append last build/test failure summaries for TDD self-healing.
        extra_failure_context = ""

        # Try ctx.last_build_result / ctx.last_test_result if available
        last_build = getattr(ctx, "last_build_result", None)
        last_test = getattr(ctx, "last_test_result", None)

        def _format_build_failure(name: str, result: Any) -> str:
            try:
                success = result.get("success", True) if isinstance(result, dict) else getattr(result, "success", True)
            except Exception:
                success = True
            if success:
                return ""
            try:
                stdout = result.get("stdout", "") if isinstance(result, dict) else getattr(result, "stdout", "")
            except Exception:
                stdout = ""
            try:
                stderr = result.get("stderr", "") if isinstance(result, dict) else getattr(result, "stderr", "")
            except Exception:
                stderr = ""
            stdout_snip = truncate(str(stdout), self.max_log_chars // 2)
            stderr_snip = truncate(str(stderr), self.max_log_chars // 2)
            return (
                f"\n\n{name} 结果：success = False\n"
                f"stdout 摘要（截断）：\n{stdout_snip}\n\n"
                f"stderr 摘要（截断）：\n{stderr_snip}"
            )

        def _format_test_failure(name: str, result: Any) -> str:
            try:
                success = result.get("success", True) if isinstance(result, dict) else getattr(result, "success", True)
            except Exception:
                success = True
            if success:
                return ""
            try:
                log = result.get("log", "") if isinstance(result, dict) else getattr(result, "log", "")
            except Exception:
                log = ""
            try:
                summary = result.get("summary", "") if isinstance(result, dict) else getattr(result, "summary", "")
            except Exception:
                summary = ""
            # If log is a .log file path, read its contents for richer context
            if isinstance(log, str) and log.endswith(".log"):
                try:
                    with open(log, "r", encoding="utf-8", errors="ignore") as f:
                        log = f.read()
                except Exception:
                    # Fall back to the path string if we cannot read the file
                    pass
            log_snip = truncate(str(log), self.max_log_chars // 2)
            summary_snip = truncate(str(summary), self.max_log_chars // 2)
            return (
                f"\n\n{name} 结果：success = False\n"
                f"log 摘要（截断）：\n{log_snip}\n\n"
                f"summary 摘要（截断）：\n{summary_snip}"
            )

        failure_sections: List[str] = []
        if last_build is not None:
            s = _format_build_failure("上次构建", last_build)
            if s:
                failure_sections.append(s)
        if last_test is not None:
            s = _format_test_failure("上次测试", last_test)
            if s:
                failure_sections.append(s)

        if failure_sections:
            extra_failure_context = (
                "\n\n上次测试失败，请根据以下报错信息修复代码：" + "".join(failure_sections)
            )

        enhanced_task = (
            f"{task_text}\n\n"
            "重要：如果需要的文件不存在，请直接创建它们，不要只尝试编辑已有文件。\n"
            "这是基于 requirements.json 的项目。如果涉及文件不存在，必须创建它们。请使用 gpt-5.1 模型进行编辑。"
            f"{extra_failure_context}"
        )
        cmd.extend(["--message", enhanced_task])
        cmd.extend(files)
        return cmd

    def _run_subprocess(self, cmd: List[str], cwd: Path) -> Dict[str, Any]:
        try:
            # Print full, untruncated command for debugging
            full_cmd_str = shlex.join(cmd)
            print(f"[DEBUG] Running aider subprocess: {full_cmd_str} (cwd={cwd})")
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
