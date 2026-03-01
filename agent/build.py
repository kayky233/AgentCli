import re
from pathlib import Path
from typing import Dict, List, Union

from .utils import truncate


class BuildDiagnoser:
    def __init__(self, tool_router, run_manager):
        self.tool_router = tool_router
        self.run_manager = run_manager
        self.counter = 0

    def run(self, ctx_or_state, build_cmd: Union[str, List[str]], cwd: Path) -> Dict:
        self.counter += 1
        # If a target_workspace exists under the repo root, always run builds there.
        repo_root = getattr(ctx_or_state, "repo_root", None)
        if repo_root is not None:
            tw = Path(repo_root) / "target_workspace"
            if tw.is_dir():
                cwd = tw
        res = self._run_command(ctx_or_state, build_cmd, cwd=cwd)
        rm = getattr(ctx_or_state, "run_manager", self.run_manager)
        log_path = rm.save_verify_log(ctx_or_state, self.counter, "make", res["stdout"] + "\n" + res["stderr"])
        summary = self._parse_errors(res["stderr"])
        return {
            "success": res["exit_code"] == 0,
            "log": str(log_path),
            "raw": res,
            "summary": summary,
        }

    def _parse_errors(self, stderr: str) -> List[Dict[str, str]]:
        errors = []
        pattern = re.compile(r"(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+)?:?\s*(?P<rest>.*)")
        for line in truncate(stderr).splitlines():
            if "error" not in line.lower():
                continue
            m = pattern.match(line.strip())
            if m:
                errors.append(
                    {
                        "file": m.group("file"),
                        "line": m.group("line"),
                        "message": m.group("rest"),
                    }
                )
            else:
                errors.append({"file": "", "line": "", "message": line.strip()})
            if len(errors) >= 10:
                break
        return errors

    def _run_command(self, ctx_or_state, cmd: Union[str, List[str]], cwd: Path) -> Dict:
        skills = getattr(ctx_or_state, "skills", None)
        if skills:
            res = skills.run("run_command", ctx_or_state, cmd=cmd, cwd=cwd)
            if res.ok and isinstance(res.data, dict):
                return res.data
            return {
                "cmd": cmd,
                "cwd": str(cwd),
                "exit_code": -1,
                "stdout": "",
                "stderr": f"run_command skill failed: {res.error or 'unknown'}",
            }
        return self.tool_router.run_command(cmd, cwd=cwd)

