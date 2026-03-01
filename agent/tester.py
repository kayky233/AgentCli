import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Union

from .utils import truncate


class TestTriage:
    def __init__(self, tool_router, run_manager):
        self.tool_router = tool_router
        self.run_manager = run_manager
        self.counter = 0

    def run(self, ctx_or_state, test_cmd: Union[str, List[str]], cwd: Path) -> Dict:
        self.counter += 1
        # If a target_workspace exists under the repo root, always run tests there.
        repo_root = getattr(ctx_or_state, "repo_root", None)
        if repo_root is not None:
            tw = Path(repo_root) / "target_workspace"
            if tw.is_dir():
                cwd = tw
        res = self._run_command(ctx_or_state, test_cmd, cwd=cwd)
        rm = getattr(ctx_or_state, "run_manager", self.run_manager)
        log_path = rm.save_verify_log(ctx_or_state, self.counter, "test", res["stdout"] + "\n" + res["stderr"])
        combined = (res["stdout"] or "") + "\n" + (res["stderr"] or "")
        summary = self._parse_xml(cwd) or self._parse_stdout(combined)
        return {
            # NOTE: demo Makefile uses `|| true`, so exit_code may be 0 even if tests failed.
            # If we parsed any failed tests, treat as failure.
            "success": (res["exit_code"] == 0) and (len(summary) == 0),
            "log": str(log_path),
            "raw": res,
            "summary": summary,
        }

    def _parse_xml(self, cwd: Path) -> List[Dict[str, str]]:
        report = cwd / "build" / "tests" / "report.xml"
        if not report.exists():
            return []
        items: List[Dict[str, str]] = []
        try:
            root = ET.parse(report).getroot()
            for suite in root.findall("testsuite"):
                for case in suite.findall("testcase"):
                    failures = case.findall("failure")
                    if failures:
                        items.append(
                            {
                                "suite": suite.attrib.get("name", ""),
                                "case": case.attrib.get("name", ""),
                                "message": failures[0].text or failures[0].attrib.get("message", ""),
                            }
                        )
            return items
        except ET.ParseError:
            return []

    def _parse_stdout(self, stdout: str) -> List[Dict[str, str]]:
        items = []
        for line in truncate(stdout).splitlines():
            m = re.search(r"\[  FAILED  \]\s+([^.]+)\.([^\s]+)", line)
            if m:
                items.append({"suite": m.group(1), "case": m.group(2), "message": line.strip()})
        return items

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

