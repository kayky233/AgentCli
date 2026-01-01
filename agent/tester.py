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

    def run(self, state, test_cmd: Union[str, List[str]], cwd: Path) -> Dict:
        self.counter += 1
        res = self.tool_router.run_command(test_cmd, cwd=cwd)
        log_path = self.run_manager.save_verify_log(state, self.counter, "test", res["stdout"] + "\n" + res["stderr"])
        summary = self._parse_xml(cwd) or self._parse_stdout(res["stdout"])
        return {
            "success": res["exit_code"] == 0,
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

