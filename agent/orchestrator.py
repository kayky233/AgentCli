import json
from pathlib import Path
from typing import Any, Dict, Optional

from .build import BuildDiagnoser
from .patch_author import PatchAuthor
from .reposcout import RepoScout
from .tester import TestTriage
from .utils import colored


class Orchestrator:
    def __init__(self, repo_root: Path, run_manager, tool_router, max_iters: int = 8):
        self.repo_root = repo_root
        self.run_manager = run_manager
        self.tool_router = tool_router
        self.max_iters = max_iters
        self.build = BuildDiagnoser(tool_router, run_manager)
        self.test = TestTriage(tool_router, run_manager)
        self.scout = RepoScout(tool_router, run_manager)
        self.author = PatchAuthor(tool_router, run_manager)

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

        iteration = state.iteration
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
            build_res = self.build.run(state)
            state.diagnostics["build"] = build_res
            if not build_res["success"]:
                if not self._handle_failure(state, "build", build_res):
                    return
                continue

            state.stage = "TEST"
            self.run_manager.save_state(state)
            test_res = self.test.run(state)
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
                "BuildDiagnose：make -j 并解析错误",
                "TestTriage：make test 并解析 gtest XML/stdout",
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

