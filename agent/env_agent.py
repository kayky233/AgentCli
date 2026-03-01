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
        print(f'[DEBUG] EnvAgent scanning workspace: {req.workspace}')
        plat = self._detect_platform()
        det = self._detect_all(req.workspace)
        print(f'[DEBUG] Environment detection result: {det}')

        # 优先识别 Python 项目：requirements.txt / requirements.json / setup.py / main.py 存在，且没有 Makefile
        ws = det.get("workspace", {})
        task_text = getattr(req, "task", "") or ""
        task_lower = task_text.lower()
        has_requirements_json = (req.workspace / "requirements.json").exists()
        is_python_project = (
            ws.get("has_requirements_txt")
            or has_requirements_json
            or ws.get("has_setup_py")
            or ws.get("has_main_py")
            or ("python" in task_lower)
        ) and not ws.get("has_makefile")

        # 强制线索：如果存在 calculator.py，则一定视为 Python 项目
        if (req.workspace / "calculator.py").exists():
            is_python_project = True

        if is_python_project:
            build_cmd = 'echo "No build needed"'
            test_cmd = "pytest"
            note = "Detected Python project; using no-op build and pytest as test command."

            return self._decision(
                plat,
                "python",
                build_cmd,
                test_cmd,
                det,
                note=note,
            )

        # overrides make_cmd（仅对 make 策略有意义）
        if req.override_make_cmd and not is_python_project:
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

        # 如果明确强制策略
        if req.force_strategy == "wsl" and not is_python_project:
            wsl_dec = self._wsl_path_and_wrap(req, det, plat)
            if wsl_dec:
                return wsl_dec
            return self._error(plat, det, "请求使用 WSL 但不可用。")
        if req.force_strategy == "fallback":
            fb = self._fallback_commands(req.workspace, det, prefer_python=is_python_project)
            if fb:
                return self._decision(
                    plat,
                    fb["strategy"],
                    fb["build_cmd"],
                    fb["test_cmd"],
                    det,
                    warn=fb.get("warn"),
                )
            return self._error(plat, det, "请求使用 fallback 但无法生成命令。")

        # 针对 Python 项目：优先使用 python_script/fallback_py 策略，而不是 gnu_make
        if is_python_project:
            fb = self._fallback_commands(req.workspace, det, prefer_python=True)
            if fb:
                return self._decision(
                    plat,
                    fb["strategy"],
                    fb["build_cmd"],
                    fb["test_cmd"],
                    det,
                    warn=fb.get("warn"),
                )
            return self._error(plat, det, "检测到 Python 项目，但无法生成 Python 构建命令。")

        # WSL override（仅对 make 流程有意义）
        if req.override_use_wsl and not is_python_project:
            wsl_dec = self._wsl_path_and_wrap(req, det, plat)
            if wsl_dec:
                return wsl_dec
            return self._error(plat, det, "请求使用 WSL 但不可用。")

        # 通用原生决策（以 make 为主）
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
        else:
            mk = self._first_available(det, ["make", "gmake"])
            if mk:
                build = self._replace_make(req.preferred_build, mk)
                test = self._replace_make(req.preferred_test, mk)
                return self._decision(plat, "gnu_make", build, test, det, note=f"Detected {mk}")

        # make 不可用时的通用 fallback
        if req.allow_fallback:
            fb = self._fallback_commands(req.workspace, det, prefer_python=is_python_project)
            if fb:
                return self._decision(plat, fb["strategy"], fb["build_cmd"], fb["test_cmd"], det, warn=fb.get("warn"))
        err_msg = "未找到 make。" if not is_python_project else "未找到合适的构建策略。"
        return self._error(plat, det, err_msg)

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
                "has_requirements_txt": (workspace / "requirements.txt").exists(),
                "has_setup_py": (workspace / "setup.py").exists(),
                "has_main_py": (workspace / "main.py").exists(),
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

    def _fallback_commands(self, workspace: Path, det: Dict, prefer_python: bool = False) -> Optional[Dict[str, str]]:
        """
        通用 fallback 决策：
        - prefer_python=True 时优先为 Python 项目生成命令（python_script 策略）
        - 否则退回到现有的 build.py 驱动的 fallback_py 策略
        """
        py_cmd = det.get("python", {}).get("path") or "python3.11"

        # 针对 Python 项目：使用 build.py 作为 fallback 入口（如存在）
        ws = det.get("workspace", {})
        if prefer_python:
            build_py = workspace / "build.py"
            if build_py.exists():
                build_cmd = f"{py_cmd} {build_py} build"
                test_cmd = f"{py_cmd} {build_py} test"
                warn = "检测到 Python 项目，使用 Python build.py 作为构建入口。"
                return {
                    "build_cmd": build_cmd,
                    "test_cmd": test_cmd,
                    "warn": warn,
                    "strategy": "fallback_py",
                }

        build_py = workspace / "build.py"
        if not build_py.exists():
            return None
        build_cmd = f"{py_cmd} {build_py} build"
        test_cmd = f"{py_cmd} {build_py} test"
        warn = "未检测到 make，使用 python fallback 构建器。"
        return {
            "build_cmd": build_cmd,
            "test_cmd": test_cmd,
            "warn": warn,
            "strategy": "fallback_py",
        }

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

