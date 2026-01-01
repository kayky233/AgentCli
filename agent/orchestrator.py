import json
from pathlib import Path
from typing import Any, Dict, Optional

from .build import BuildDiagnoser
from .env_agent import EnvAgent, EnvDecision, EnvRequest
from .patch_author import PatchAuthor
from .reposcout import RepoScout
from .tester import TestTriage
from .utils import colored


class Orchestrator:
    def __init__(
        self,
        repo_root: Path,
        run_manager,
        tool_router,
        max_iters: int = 8,
        build_only: bool = False,
        env_overrides: Optional[Dict[str, Any]] = None,
    ):
        self.repo_root = repo_root
        self.run_manager = run_manager
        self.tool_router = tool_router
        self.max_iters = max_iters
        self.build_only = build_only
        self.env_overrides = env_overrides or {}
        self.build = BuildDiagnoser(tool_router, run_manager)
        self.test = TestTriage(tool_router, run_manager)
        self.scout = RepoScout(tool_router, run_manager)
        self.author = PatchAuthor(tool_router, run_manager)
        self.workdir = self._resolve_workdir()
        self.env_agent = EnvAgent()
        self.env_decision: Optional[EnvDecision] = None

    def plan_only(self, task: str, as_json: bool, auto: bool) -> Dict:
        state = self.run_manager.create_run(task, auto)
        checkpoint = self.tool_router.git_checkpoint(state.run_ts)
        state.checkpoint = checkpoint
        plan = self._build_plan(task)
        self.run_manager.save_plan(state, plan)
        if as_json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            self._print_plan(plan)
        return plan

    def run(self, task: Optional[str], auto: bool, resume: bool = False) -> None:
        state = None
        if resume:
            state = self.run_manager.load_latest()
            if not state:
                print("没有可恢复的 run。")
                return
            print(f"恢复 run {state.run_ts}，任务：{state.task}")
            auto = auto or state.auto
        else:
            state = self.run_manager.create_run(task or "", auto)
            checkpoint = self.tool_router.git_checkpoint(state.run_ts)
            state.checkpoint = checkpoint
            plan = self._build_plan(task or "")
            self.run_manager.save_plan(state, plan)
            if not self._prompt_plan(plan, state):
                return
            if not self._env_decision_phase(state, auto):
                return

        iteration = state.iteration
        if not self.env_decision and state.env_decision:
            self.env_decision = EnvDecision(
                platform=state.env_decision.get("platform", ""),
                strategy=state.env_decision.get("strategy", ""),
                commands=state.env_decision.get("commands", {}),
                detections=state.env_decision.get("detections", {}),
                fallback=state.env_decision.get("fallback", {}),
                user_actions=state.env_decision.get("user_actions", []),
                warnings=state.env_decision.get("warnings", []),
            )
        if not self.env_decision:
            if not self._env_decision_phase(state, auto):
                return
        while iteration < self.max_iters:
            iteration += 1
            state.iteration = iteration
            self.run_manager.save_state(state)

            state.stage = "GATHER"
            self.run_manager.save_state(state)
            gather_summary = self.scout.gather(state, hints=self._collect_hints(state))

            state.stage = "PATCH"
            self.run_manager.save_state(state)
            patch_result = self.author.generate(state, state.diagnostics)
            if not self._prompt_patches(state, patch_result):
                return
            apply_ok = self._apply_patches(patch_result)
            if not apply_ok:
                self._record(state, "patch_apply_failed", {"patches": patch_result})
                return

            state.stage = "BUILD"
            self.run_manager.save_state(state)
            build_cmd = self.env_decision.commands["build"]
            test_cmd = self.env_decision.commands["test"]
            build_res = self.build.run(state, build_cmd, cwd=self.workdir)
            state.diagnostics["build"] = build_res
            if not build_res["success"]:
                if not self._handle_failure(state, "build", build_res):
                    return
                continue

            if self.build_only:
                print(colored("仅构建模式，跳过测试。", "blue"))
                state.stage = "FINALIZE"
                self.run_manager.save_state(state)
                self.run_manager.save_transcript(state)
                return

            state.stage = "TEST"
            self.run_manager.save_state(state)
            test_res = self.test.run(state, test_cmd, cwd=self.workdir)
            state.diagnostics["test"] = test_res
            if not test_res["success"]:
                if not self._handle_failure(state, "test", test_res):
                    return
                continue

            # success
            state.stage = "FINALIZE"
            self.run_manager.save_state(state)
            print(colored("全部通过！", "green"))
            self.run_manager.save_transcript(state)
            return

        print(colored("已达到最大迭代次数，退出。", "yellow"))
        self.run_manager.save_transcript(state)

    def rollback(self) -> None:
        state = self.run_manager.load_latest()
        if not state:
            print("没有可回滚的 run。")
            return
        res = self.tool_router.git_rollback(state.checkpoint)
        print(f"已尝试回滚到 {state.checkpoint}: {res}")

    def _build_plan(self, task: str) -> Dict[str, Any]:
        plan = {
            "task": task,
            "steps": [
                "RepoScout：搜索相关文件与上下文",
                "PatchAuthor：生成补丁（遵循 patch-first）",
                "应用补丁：git apply --3way",
                "BuildDiagnose：make -j（无 make 时将自动 fallback python 构建器）",
                "TestTriage：make test（无 make 时将自动 fallback python 构建器）",
            ],
            "commands": ["make -j", "make test"],
            "risks": ["补丁可能失败，需回滚", "构建/测试失败需要多轮迭代"],
            "max_iterations": self.max_iters,
        }
        return plan

    def _print_plan(self, plan: Dict[str, Any]) -> None:
        print(colored("执行计划", "blue"))
        print(f"任务：{plan.get('task')}")
        for idx, step in enumerate(plan.get("steps", []), start=1):
            print(f"{idx}. {step}")
        print("将运行命令：", ", ".join(plan.get("commands", [])))
        print("风险点：", "; ".join(plan.get("risks", [])))
        print(f"迭代上限：{plan.get('max_iterations')}")
        print(f"工作目录：{self.workdir}")

    def _prompt_plan(self, plan: Dict[str, Any], state) -> bool:
        if state.auto:
            self._print_plan(plan)
            return True
        self._print_plan(plan)
        choice = input("继续？(y=继续 / p=只生成patch / q=退出): ").strip().lower()
        state.transcript.append({"stage": "PLAN", "choice": choice})
        self.run_manager.save_transcript(state)
        if choice == "q":
            print("用户退出。")
            return False
        if choice == "p":
            state.auto = False
            return True
        return True

    def _prompt_patches(self, state, patch_result: Dict) -> bool:
        patches = patch_result.get("patches", [])
        print(colored(f"Patch 摘要：{len(patches)} 个补丁", "blue"))
        for idx, p in enumerate(patches, start=1):
            print(f"补丁 {idx}: {len(p.splitlines())} 行")
        for note in patch_result.get("notes", []):
            print(f"- {note}")
        if state.auto:
            return True
        choice = input("应用补丁？(y=应用 / s=跳过 / a=应用后停 / q=退出): ").strip().lower()
        state.transcript.append({"stage": "PATCH", "choice": choice})
        self.run_manager.save_transcript(state)
        if choice == "q":
            print("用户退出。")
            return False
        if choice == "s":
            print("跳过补丁。")
            return False
        if choice == "a":
            state.auto = False
        return True

    def _apply_patches(self, patch_result: Dict) -> bool:
        patches = patch_result.get("patches", [])
        if not patches:
            print("无补丁可应用。")
            return True
        for patch in patches:
            res = self.tool_router.git_apply_patch(patch)
            if res["exit_code"] != 0:
                print(colored("应用补丁失败", "red"))
                print(res["stderr"])
                return False
        print(colored("补丁应用成功。", "green"))
        return True

    def _handle_failure(self, state, stage: str, result: Dict) -> bool:
        summary = result.get("summary", [])
        print(colored(f"{stage} 失败：", "red"))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if state.auto:
            print("自动模式：进入下一轮修复。")
            return True
        choice = input("下一步？(c=继续修复 / q=退出 / r=回滚 / a=切换自动): ").strip().lower()
        state.transcript.append({"stage": f"{stage}_fail", "choice": choice, "summary": summary})
        self.run_manager.save_transcript(state)
        if choice == "q":
            return False
        if choice == "r":
            self.rollback()
            return False
        if choice == "a":
            state.auto = True
        return True

    def _record(self, state, stage: str, payload: Dict[str, Any]) -> None:
        state.transcript.append({"stage": stage, "payload": payload})
        self.run_manager.save_transcript(state)

    def _collect_hints(self, state) -> list:
        hints = []
        if "test" in state.diagnostics:
            fails = state.diagnostics["test"].get("summary", [])
            for f in fails:
                hints.append(f.get("suite", ""))
                hints.append(f.get("case", ""))
        if "build" in state.diagnostics:
            errs = state.diagnostics["build"].get("summary", [])
            for e in errs:
                hints.append(e.get("message", ""))
        return [h for h in hints if h]

    def _resolve_commands(self):
        # deprecated
        return ["make", "-j"], ["make", "test"], ""

    def _resolve_workdir(self) -> Path:
        # 优先当前仓库根的 Makefile，否则尝试 demo_c_project
        if (self.repo_root / "Makefile").exists():
            return self.repo_root
        demo = self.repo_root / "demo_c_project"
        if demo.exists():
            return demo
        return self.repo_root

    def _env_decision_phase(self, state, auto: bool) -> bool:
        req = EnvRequest(
            workspace=self.workdir,
            preferred_build="make -j",
            preferred_test="make test",
            interactive=not auto,
            allow_wsl=True,
            allow_fallback=not self.env_overrides.get("no_make_fallback", False),
            prefer_gnu_make=True,
            override_make_cmd=self.env_overrides.get("make_cmd"),
            override_use_wsl=self.env_overrides.get("use_wsl", False),
        )
        decision = self.env_agent.decide(req)
        self.env_decision = decision
        state.env_decision = decision.__dict__
        self.run_manager.save_state(state)
        self._print_env_decision(decision)
        if decision.strategy == "error":
            print(colored("环境决策失败，无法继续。", "red"))
            return False
        if auto:
            return True
        while True:
            choice = input("环境策略：c=继续 / w=改用WSL / f=改用fallback / q=退出: ").strip().lower()
            if choice == "c":
                return True
            if choice == "q":
                return False
            if choice == "w":
                req.force_strategy = "wsl"
                decision = self.env_agent.decide(req)
                self.env_decision = decision
                state.env_decision = decision.__dict__
                self.run_manager.save_state(state)
                self._print_env_decision(decision)
                if decision.strategy != "error":
                    return True
                print(colored("切换 WSL 失败。", "red"))
            if choice == "f":
                req.force_strategy = "fallback"
                decision = self.env_agent.decide(req)
                self.env_decision = decision
                state.env_decision = decision.__dict__
                self.run_manager.save_state(state)
                self._print_env_decision(decision)
                if decision.strategy != "error":
                    return True
                print(colored("切换 fallback 失败。", "red"))

    def _print_env_decision(self, decision: EnvDecision) -> None:
        print(colored("环境决策", "blue"))
        print(f"平台：{decision.platform}，策略：{decision.strategy}")
        print(f"构建命令：{decision.commands.get('build')}")
        print(f"测试命令：{decision.commands.get('test')}")
        if decision.warnings:
            for w in decision.warnings:
                print(colored(f"提示：{w}", "yellow"))
        det = decision.detections or {}
        detected_make = det.get("make") or det.get("mingw32-make") or det.get("gmake")
        print(f"检测：python={det.get('python')} make={detected_make} wsl={det.get('wsl')}")
        if decision.user_actions:
            for act in decision.user_actions:
                print(f"- 建议：{act.get('title')} ({act.get('detail')})")

