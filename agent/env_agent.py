import platform
import shlex
import shutil
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class EnvRequest:
    workspace: Path
    preferred_build: str
    preferred_test: str
    interactive: bool = True
    allow_wsl: bool = True
    allow_fallback: bool = True
    prefer_gnu_make: bool = True
    override_make_cmd: Optional[str] = None
    override_use_wsl: bool = False
    force_strategy: Optional[str] = None  # "wsl" | "fallback" | None


@dataclass
class EnvDecision:
    platform: str
    strategy: str
    commands: Dict[str, str]
    detections: Dict[str, Dict]
    fallback: Dict[str, str] = field(default_factory=dict)
    user_actions: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class EnvAgent:
    def __init__(self):
        pass

    def decide(self, req: EnvRequest) -> EnvDecision:
        plat = self._detect_platform()
        det = self._detect_all(req.workspace)

        # overrides make_cmd
        if req.override_make_cmd:
            if self._can_execute(req.override_make_cmd):
                build = self._replace_make(req.preferred_build, req.override_make_cmd)
                test = self._replace_make(req.preferred_test, req.override_make_cmd)
                return self._decision(
                    plat,
                    "gnu_make",
                    build,
                    test,
                    det,
                    note=f"Using user-specified make: {req.override_make_cmd}",
                )
            return self._error(plat, det, f"指定的 --make-cmd 不可执行：{req.override_make_cmd}")

        # force strategy from interactive switch
        if req.force_strategy == "wsl":
            wsl_dec = self._wsl_path_and_wrap(req, det, plat)
            if wsl_dec:
                return wsl_dec
            return self._error(plat, det, "请求使用 WSL 但不可用。")
        if req.force_strategy == "fallback":
            fb = self._fallback_commands(req.workspace, det)
            if fb:
                return self._decision(
                    plat,
                    "fallback_py",
                    fb["build_cmd"],
                    fb["test_cmd"],
                    det,
                    warn=fb.get("warn"),
                )
            return self._error(plat, det, "请求使用 fallback 但无法生成命令。")

        # WSL override
        if req.override_use_wsl:
            wsl_dec = self._wsl_path_and_wrap(req, det, plat)
            if wsl_dec:
                return wsl_dec
            return self._error(plat, det, "请求使用 WSL 但不可用。")

        # Native decisions
        if plat == "windows":
            mk = self._first_available(det, ["mingw32-make", "make", "gmake"])
            if mk:
                build = self._replace_make(req.preferred_build, mk)
                test = self._replace_make(req.preferred_test, mk)
                return self._decision(plat, "gnu_make", build, test, det, note=f"Detected {mk}")
            if det.get("nmake"):
                build = "nmake"
                test = "nmake test"
                return self._decision(plat, "nmake", build, test, det, warn="使用 nmake，需确保 Makefile 兼容")
            if det.get("wsl", {}).get("available") and req.allow_wsl:
                wsl_dec = self._wsl_path_and_wrap(req, det, plat)
                if wsl_dec:
                    return wsl_dec
            if req.allow_fallback:
                fb = self._fallback_commands(req.workspace, det)
                if fb:
                    return self._decision(plat, "fallback_py", fb["build_cmd"], fb["test_cmd"], det, warn=fb.get("warn"))
            return self._error(plat, det, "未找到 make，且 fallback 被禁用或不可用。")
        else:
            mk = self._first_available(det, ["make", "gmake"])
            if mk:
                build = self._replace_make(req.preferred_build, mk)
                test = self._replace_make(req.preferred_test, mk)
                return self._decision(plat, "gnu_make", build, test, det, note=f"Detected {mk}")
            if req.allow_fallback:
                fb = self._fallback_commands(req.workspace, det)
                if fb:
                    return self._decision(plat, "fallback_py", fb["build_cmd"], fb["test_cmd"], det, warn=fb.get("warn"))
            return self._error(plat, det, "未找到 make。")

    # ------------------ helpers ------------------ #
    def _detect_platform(self) -> str:
        sys_name = platform.system().lower()
        if "windows" in sys_name:
            return "windows"
        if "darwin" in sys_name:
            return "mac"
        return "linux"

    def _can_execute(self, cmd: str) -> bool:
        if Path(cmd).exists():
            return True
        return shutil.which(cmd) is not None

    def _detect_all(self, workspace: Path) -> Dict[str, Dict]:
        return {
            "make": self._which_info("make"),
            "mingw32-make": self._which_info("mingw32-make"),
            "gmake": self._which_info("gmake"),
            "nmake": self._which_info("nmake"),
            "wsl": self._detect_wsl(),
            "compiler": self._detect_compilers(),
            "python": self._detect_python(),
            "workspace": {
                "path": str(workspace),
                "has_makefile": (workspace / "Makefile").exists(),
                "has_build_py": (workspace / "build.py").exists(),
            },
        }

    def _which_info(self, name: str) -> Optional[Dict[str, str]]:
        path = shutil.which(name)
        if not path:
            return None
        kind = "gnu" if "make" in name else "unknown"
        if name == "nmake":
            kind = "nmake"
        return {"path": path, "kind": kind, "cmd": name}

    def _detect_wsl(self) -> Dict:
        wsl = shutil.which("wsl")
        return {
            "available": bool(wsl),
            "path": wsl,
            "wslpath": bool(shutil.which("wslpath")) if wsl else False,
        }

    def _detect_compilers(self) -> Dict:
        candidates = []
        if self._detect_platform() == "windows":
            candidates = ["cl", "gcc", "clang"]
        else:
            candidates = ["gcc", "clang", "cc"]
        for c in candidates:
            p = shutil.which(c)
            if p:
                return {"cc": c, "path": p, "kind": c}
        return {}

    def _detect_python(self) -> Dict:
        py = shutil.which("python") or shutil.which("py")
        if not py:
            return {}
        return {"path": py, "version": sys.version.split()[0]}

    def _fallback_commands(self, workspace: Path, det: Dict) -> Optional[Dict[str, str]]:
        build_py = workspace / "build.py"
        if not build_py.exists():
            return None
        py_cmd = det.get("python", {}).get("path") or "python"
        build_cmd = f"{py_cmd} {build_py} build"
        test_cmd = f"{py_cmd} {build_py} test"
        warn = "未检测到 make，使用 python fallback 构建器。"
        return {"build_cmd": build_cmd, "test_cmd": test_cmd, "warn": warn}

    def _replace_make(self, cmd: str, make_cmd: str) -> str:
        parts = shlex.split(cmd)
        if not parts:
            return cmd
        if parts[0] == "make":
            parts[0] = make_cmd
        return " ".join(parts)

    def _first_available(self, det: Dict, names: List[str]) -> Optional[str]:
        for n in names:
            if det.get(n):
                return det[n]["cmd"]
        return None

    def _wsl_path_and_wrap(self, req: EnvRequest, det: Dict, plat: str) -> Optional[EnvDecision]:
        if not det.get("wsl", {}).get("available"):
            return None
        wsl_path = self._to_wsl_path(req.workspace)
        build = f'wsl -e bash -lc "cd {shlex.quote(wsl_path)} && {req.preferred_build}"'
        test = f'wsl -e bash -lc "cd {shlex.quote(wsl_path)} && {req.preferred_test}"'
        return self._decision(
            plat,
            "wsl_make",
            build,
            test,
            det,
            warn="使用 WSL 执行 make",
        )

    def _to_wsl_path(self, path: Path) -> str:
        proc = shutil.which("wsl")
        if not proc:
            return str(path)
        try:
            res = subprocess.run(
                ["wsl", "wslpath", "-a", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
        # 简单手工转换：D:\path -> /mnt/d/path
        p = str(path).replace("\\", "/")
        if len(p) > 1 and p[1] == ":":
            drive = p[0].lower()
            rest = p[2:]
            return f"/mnt/{drive}{rest}"
        return p

    def _decision(
        self,
        plat: str,
        strategy: str,
        build: str,
        test: str,
        det: Dict,
        note: Optional[str] = None,
        warn: Optional[str] = None,
    ) -> EnvDecision:
        warnings = []
        if warn:
            warnings.append(warn)
        commands = {
            "build": build,
            "test": test,
            "explain_build": note or "",
            "explain_test": note or "",
        }
        user_actions = []
        if strategy == "fallback_py":
            user_actions.append({"title": "安装 GNU Make", "detail": "安装后可切换到 make 构建", "optional": True})
        elif strategy == "wsl_make":
            user_actions.append({"title": "在 Windows 安装 GNU Make", "detail": "避免依赖 WSL", "optional": True})
        return EnvDecision(
            platform=plat,
            strategy=strategy,
            commands=commands,
            detections=det,
            fallback={"enabled": strategy == "fallback_py", "build_cmd": build, "test_cmd": test},
            user_actions=user_actions,
            warnings=warnings,
        )

    def _error(self, plat: str, det: Dict, message: str) -> EnvDecision:
        return EnvDecision(
            platform=plat,
            strategy="error",
            commands={"build": "", "test": "", "explain_build": message, "explain_test": message},
            detections=det,
            fallback={"enabled": False},
            user_actions=[{"title": "安装 GNU Make 或启用 fallback", "detail": message, "optional": False}],
            warnings=[message],
        )

