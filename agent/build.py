import re
from pathlib import Path
from typing import Dict, List

from .utils import truncate


class BuildDiagnoser:
    def __init__(self, tool_router, run_manager):
        self.tool_router = tool_router
        self.run_manager = run_manager
        self.counter = 0

    def run(self, state, build_cmd, cwd: Path) -> Dict:
        self.counter += 1
        res = self.tool_router.run_command(build_cmd, cwd=cwd)
        log_path = self.run_manager.save_verify_log(state, self.counter, "make", res["stdout"] + "\n" + res["stderr"])
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

