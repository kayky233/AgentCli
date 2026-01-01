import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .utils import ensure_dir, truncate


class ToolRouter:
    def __init__(
        self,
        repo_root: Path,
        run_manager=None,
        state=None,
        make_cmd: Optional[str] = None,
        no_make_fallback: bool = False,
        use_wsl: bool = False,
    ):
        self.repo_root = repo_root
        self.run_manager = run_manager
        self.state = state
        self.no_make_fallback = no_make_fallback
        self.make_cmd_override = make_cmd
        self.use_wsl = use_wsl
        self.make_info = (
            {"cmd": make_cmd, "kind": "custom"} if make_cmd else self.detect_make()
        )
        self.wsl_available = self.detect_wsl() if use_wsl else None

    # ------------------ detection ------------------ #
    def detect_make(self) -> Optional[Dict[str, str]]:
        candidates: List[Tuple[str, str]] = [
            ("make", "gnu"),
            ("mingw32-make", "gnu"),
            ("gmake", "gnu"),
            ("nmake", "nmake"),
        ]
        system = platform.system().lower()
        if system == "windows":
            candidates = [
                ("mingw32-make", "gnu"),
                ("make", "gnu"),
                ("gmake", "gnu"),
                ("nmake", "nmake"),
            ]
        for name, kind in candidates:
            if shutil.which(name):
                return {"cmd": name, "kind": kind}
        return None

    def detect_wsl(self) -> Optional[str]:
        wsl = shutil.which("wsl")
        return wsl

    # ------------------ execution helpers ------------------ #
    def _alias_make(self, cmd: List[str]) -> List[str]:
        if not cmd:
            return cmd
        if cmd[0] != "make":
            return cmd
        chosen = None
        if self.make_cmd_override:
            chosen = self.make_cmd_override
        elif self.make_info:
            chosen = self.make_info.get("cmd")
        if chosen:
            return [chosen] + cmd[1:]
        return cmd

    def _wrap_wsl(self, cmd: List[str], workdir: Path) -> List[str]:
        # convert cwd to wsl path if possible
        wsl_cwd = str(workdir)
        if self.wsl_available:
            try:
                proc = subprocess.run(
                    ["wsl", "wslpath", "-a", str(workdir)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    wsl_cwd = proc.stdout.strip()
            except Exception:
                pass
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        return ["wsl", "-e", "bash", "-lc", f"cd {shlex.quote(wsl_cwd)} && {quoted_cmd}"]

    def run_command(self, cmd: List[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> Dict:
        workdir = cwd or self.repo_root
        ensure_dir(workdir)
        resolved_cmd = self._alias_make(cmd)
        final_cmd = self._wrap_wsl(resolved_cmd, workdir) if self.use_wsl else resolved_cmd
        try:
            proc = subprocess.run(
                final_cmd,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            result = {
                "cmd": final_cmd,
                "cwd": str(workdir),
                "exit_code": proc.returncode,
                "stdout": truncate(proc.stdout),
                "stderr": truncate(proc.stderr),
            }
        except subprocess.TimeoutExpired as ex:
            result = {
                "cmd": final_cmd,
                "cwd": str(workdir),
                "exit_code": -1,
                "stdout": truncate(ex.stdout or ""),
                "stderr": truncate((ex.stderr or "") + "\n[timeout]"),
            }
        return result

    def search(self, pattern: str, cwd: Optional[Path] = None) -> str:
        workdir = cwd or self.repo_root
        if shutil.which("rg"):
            cmd = ["rg", "-n", pattern]
            res = self.run_command(cmd, cwd=workdir)
            if res["exit_code"] == 0 or res["stdout"]:
                return res["stdout"]
        # fallback simple grep
        matches = []
        for path in workdir.rglob("*"):
            if path.is_file() and path.suffix not in {".o", ".a", ".so", ".dll", ".exe"}:
                try:
                    for idx, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
                        if pattern in line:
                            matches.append(f"{path}:{idx}:{line}")
                except (UnicodeDecodeError, OSError):
                    continue
        return "\n".join(matches)

    def read_file(self, path: Path, start: Optional[int] = None, end: Optional[int] = None) -> str:
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if start is None:
            start = 1
        if end is None or end > len(content):
            end = len(content)
        selected = content[start - 1 : end]
        numbered = [f"{start + i}:{line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    def git_checkpoint(self, label: str) -> Optional[str]:
        if not shutil.which("git"):
            return None
        res = self.run_command(["git", "rev-parse", "--is-inside-work-tree"])
        if res["exit_code"] != 0:
            return None
        stash_label = f"agent-{label}"
        _ = self.run_command(["git", "stash", "push", "-u", "-m", stash_label])
        # Restore working tree to keep user changes while keeping checkpoint
        stash_list = self.run_command(["git", "stash", "list"])
        if stash_list["exit_code"] == 0 and stash_label in stash_list["stdout"]:
            ref = stash_list["stdout"].splitlines()[0].split(":")[0]
            self.run_command(["git", "stash", "apply", ref])
            return ref
        return None

    def git_apply_patch(self, patch: str, cwd: Optional[Path] = None) -> Dict:
        workdir = cwd or self.repo_root
        ensure_dir(workdir)
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".patch") as tmp:
            tmp.write(patch)
            tmp.flush()
            tmp_path = tmp.name
        res = self.run_command(["git", "apply", "--3way", "--whitespace=nowarn", tmp_path], cwd=workdir)
        if res["exit_code"] != 0:
            # fallback to direct apply without 3-way (useful before first commit / CRLF)
            res = self.run_command(
                ["git", "apply", "--ignore-space-change", "--ignore-whitespace", "--whitespace=nowarn", tmp_path],
                cwd=workdir,
            )
        Path(tmp_path).unlink(missing_ok=True)
        return res

    def git_rollback(self, checkpoint: Optional[str]) -> Dict:
        if not checkpoint:
            return {"exit_code": 1, "stderr": "no checkpoint recorded", "stdout": ""}
        steps = []
        steps.append(self.run_command(["git", "reset", "--hard"]))
        steps.append(self.run_command(["git", "clean", "-fd"]))
        steps.append(self.run_command(["git", "stash", "apply", checkpoint]))
        return {"steps": steps}

