import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, List

from .framework.agent_types import Stage
from .framework.context import RunContext
from .framework.events import EventBus
from .framework.pipeline import PipelineRunner
from .framework.registry import AgentRegistry
from .agents.env_agent_plugin import EnvAgentPlugin
from .agents.reposcout_plugin import RepoScoutPlugin
from .agents.aider_edit_plugin import AiderEditPlugin
from .agents.build_plugin import BuildPlugin
from .agents.test_plugin import TestPlugin
from .agents.requirements_plugin import RequirementsAgentPlugin
from .llm.service import LLMService
from .skills import ReadFileSkill, RunCommandSkill, SearchSkill, SkillRegistry
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

    def run_daemon(self) -> None:
        """
        Daemon mode: continuously watch for pending_tasks.json and process tasks.

        - If pending_tasks.json exists:
            * read ALL lines (one JSON object per line)
            * delete the file
            * execute each task sequentially via self.run(task, auto=True)
        - If file does not exist: sleep 2 seconds and poll again.
        """
        pending_path = self.repo_root / "pending_tasks.json"
        print(colored(f"[daemon] Watching for tasks in {pending_path}", "blue"))
        while True:
            if pending_path.exists():
                try:
                    lines = pending_path.read_text(encoding="utf-8").splitlines()
                    pending_path.unlink()
                except Exception as e:
                    print(colored(f"[daemon] Failed to read/delete pending_tasks.json: {e}", "red"))
                    time.sleep(2)
                    continue

                tasks: List[str] = []
                for ln in lines:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                        task_text = str(obj.get("task", "")).strip()
                        if task_text:
                            tasks.append(task_text)
                    except Exception as e:
                        print(colored(f"[daemon] Failed to parse task line: {e}", "red"))

                for task in tasks:
                    print(colored(f"[daemon] Starting task: {task}", "green"))
                    # auto=True to avoid blocking prompts; state updates still go through _update_state_file
                    self.run(task, auto=True)
                    print(colored(f"[daemon] Finished task: {task}", "green"))

            time.sleep(2)

    def run(self, task: Optional[str], auto: bool, resume: bool = False) -> None:
        if resume:
            state = self.run_manager.load_latest()
            if not state:
                print("没有可恢复的 run。")
                return
            task = state.task
            auto = auto or state.auto
        else:
            state = self.run_manager.create_run(task or "", auto)
            checkpoint = self.tool_router.git_checkpoint(state.run_ts)
            state.checkpoint = checkpoint
            plan = self._build_plan(task or "")
            self.run_manager.save_plan(state, plan)
            if not self._prompt_plan(plan, state):
                return

        ctx = self._make_context(state, auto)
        pipeline = self._make_pipeline()

        try:
            # mark run created/start lifecycle
            ctx.status = "running"
            ctx.started_at = time.time()
            ctx.events.emit("run.lifecycle", {"phase": "created", "run_id": ctx.run_id})
            ctx.events.emit("run.lifecycle", {"phase": "started", "run_id": ctx.run_id})
            self._update_state_file(ctx)

            # PLAN: generate structured requirements spec (if LLM available)
            ctx.current_stage = Stage.PLAN.name
            self._mark_stage_start(ctx, Stage.PLAN.name)
            ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.PLAN.name})
            self._update_state_file(ctx)
            print("--- [DEBUG] Orchestrator is calling PLAN stage ---")
            pipeline.run_stage(Stage.PLAN, ctx)
            self._mark_stage_end(ctx, Stage.PLAN.name, success=True)
            ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.PLAN.name, "status": "succeeded"})
            self._update_state_file(ctx)

            iteration = 0
            while iteration < self.max_iters:
                iteration += 1
                if iteration >= 3:
                    print(colored("达到最大基础设施重试次数（3），终止迭代以避免无限循环。", "red"))
                    break
                ctx.iteration = iteration
                if ctx.iteration >= 3:
                    print(colored("达到最大迭代次数（3），终止以避免无限循环。", "red"))
                    break

                # GATHER: understand repo / context for current iteration
                ctx.current_stage = Stage.GATHER.name
                self._mark_stage_start(ctx, Stage.GATHER.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.GATHER.name})
                self._update_state_file(ctx)
                pipeline.run_stage(Stage.GATHER, ctx, request=self._collect_hints(ctx))
                self._mark_stage_end(ctx, Stage.GATHER.name, success=True)
                ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.GATHER.name, "status": "succeeded"})
                self._update_state_file(ctx)
                print(colored("GATHER 完成", "blue"))

                # EDIT: generate or modify code BEFORE environment decision
                ctx.current_stage = Stage.EDIT.name
                self._mark_stage_start(ctx, Stage.EDIT.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.EDIT.name})
                self._update_state_file(ctx)
                pipeline.run_stage(Stage.EDIT, ctx)
                self._mark_stage_end(ctx, Stage.EDIT.name, success=True)
                ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.EDIT.name, "status": "succeeded"})
                self._update_state_file(ctx)
                print(colored(f"EDIT 完成，补丁数：{len(ctx.patch_queue)}", "blue"))

                # APPLY: apply patches to workspace

                ctx.current_stage = Stage.APPLY.name
                self._mark_stage_start(ctx, Stage.APPLY.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.APPLY.name})
                self._update_state_file(ctx)
                ctx.events.emit("stage.enter", {"stage": Stage.APPLY.name})
                if ctx.patch_queue:
                    if not auto:
                        print(colored(f"Patch 摘要：{len(ctx.patch_queue)} 个，继续应用？(y/n)", "blue"))
                        ans = input().strip().lower()
                        if ans not in ("y", "yes", ""):
                            break
                    apply_ok = self._apply_patches(ctx)
                    ctx.events.emit("apply.result", {"status": "ok" if apply_ok else "fail", "patches": ctx.patch_queue})
                    if not apply_ok:
                        ctx.events.emit("stage.exit", {"stage": Stage.APPLY.name, "status": "fail"})
                        self._mark_stage_end(ctx, Stage.APPLY.name, success=False, message="apply failed")
                        ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.APPLY.name, "status": "failed"})
                        self._update_state_file(ctx)
                        break
                else:
                    ctx.events.emit("apply.result", {"status": "skip", "patches": []})
                ctx.events.emit("stage.exit", {"stage": Stage.APPLY.name, "status": "ok"})
                self._mark_stage_end(ctx, Stage.APPLY.name, success=True)
                ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.APPLY.name, "status": "succeeded"})
                self._update_state_file(ctx)

                # PREPARE: environment decision AFTER code is materialized on disk
                ctx.current_stage = Stage.PREPARE.name
                self._mark_stage_start(ctx, Stage.PREPARE.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.PREPARE.name})
                self._update_state_file(ctx)
                pipeline.run_stage(Stage.PREPARE, ctx)
                self._mark_stage_end(ctx, Stage.PREPARE.name, success=True)
                ctx.events.emit("stage.lifecycle", {"phase": "end", "stage": Stage.PREPARE.name, "status": "succeeded"})
                self._update_state_file(ctx)
                if not ctx.env_decision or ctx.env_decision.get("strategy") == "error":
                    print(colored("环境决策失败，无法继续。", "red"))
                    self._flush_events(ctx)
                    return
                self._print_env(ctx.env_decision)
                if not auto:
                    choice = input("环境已选择，继续构建与测试？(y/n): ").strip().lower()
                    if choice not in ("y", "yes", ""):
                        break

                # Enforce test coverage: if code files changed but no test file changed, require another iteration to add tests.
                need_tests, reason = self._check_test_coverage_needed(ctx)
                if need_tests:
                    self._update_state_file(ctx)
                    ctx.policy["need_tests"] = True
                    ctx.events.emit("policy.need_tests", {"reason": reason, "applied_files": list(ctx.applied_files)})
                    print(colored(f"需要补齐测试用例覆盖：{reason}", "yellow"))
                    if auto:
                        # Skip build/test; loop back to request test edits.
                        continue
                    ans = input("需要补齐测试覆盖，继续迭代自动补测试？(y/n): ").strip().lower()
                    if ans in ("y", "yes", ""):
                        continue
                    break

                # VERIFY_BUILD: build after env decision
                ctx.current_stage = Stage.VERIFY_BUILD.name
                self._mark_stage_start(ctx, Stage.VERIFY_BUILD.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.VERIFY_BUILD.name})
                self._update_state_file(ctx)
                build_results = pipeline.run_stage(Stage.VERIFY_BUILD, ctx)
                build_ok = build_results and build_results[-1].status == "ok"
                self._mark_stage_end(ctx, Stage.VERIFY_BUILD.name, success=build_ok)
                ctx.events.emit(
                    "stage.lifecycle",
                    {
                        "phase": "end",
                        "stage": Stage.VERIFY_BUILD.name,
                        "status": "succeeded" if build_ok else "failed",
                    },
                )
                self._update_state_file(ctx)
                print(colored(f"BUILD 结果：{'成功' if build_ok else '失败'}", "yellow" if build_ok else "red"))
                if not build_ok:
                    if auto:
                        continue
                    ans = input("构建失败，继续迭代？(y/n): ").strip().lower()
                    if ans in ("y", "yes", ""):
                        continue
                    break
                if ctx.options.get("build_only"):
                    print(colored("仅构建模式，结束。", "blue"))
                    self._update_state_file(ctx)
                    break

                ctx.current_stage = Stage.VERIFY_TEST.name
                self._mark_stage_start(ctx, Stage.VERIFY_TEST.name)
                ctx.events.emit("stage.lifecycle", {"phase": "start", "stage": Stage.VERIFY_TEST.name})
                self._update_state_file(ctx)
                test_results = pipeline.run_stage(Stage.VERIFY_TEST, ctx)
                test_ok = test_results and test_results[-1].status == "ok"
                self._mark_stage_end(ctx, Stage.VERIFY_TEST.name, success=test_ok)
                ctx.events.emit(
                    "stage.lifecycle",
                    {
                        "phase": "end",
                        "stage": Stage.VERIFY_TEST.name,
                        "status": "succeeded" if test_ok else "failed",
                    },
                )
                self._update_state_file(ctx)
                print(colored(f"TEST 结果：{'成功' if test_ok else '失败'}", "yellow" if test_ok else "red"))
                if test_ok and not ctx.policy.get("need_tests"):
                    print(colored("全部通过！", "green"))
                    self._update_state_file(ctx)
                    break
                if iteration > 3:
                    print(colored("达到最大测试重试次数，终止迭代", "red"))
                    break
                if test_ok and ctx.policy.get("need_tests"):
                    # Tests passed but policy still requires coverage; keep iterating.
                    print(colored("测试通过，但仍需补齐测试用例覆盖变更，继续迭代。", "yellow"))
                    ctx.policy["need_tests"] = False
                    if auto:
                        continue
                    ans = input("继续迭代补齐测试覆盖？(y/n): ").strip().lower()
                    if ans in ("y", "yes", ""):
                        continue
                    break
                if auto:
                    continue
                ans = input("测试失败，继续迭代？(y/n): ").strip().lower()
                if ans not in ("y", "yes", ""):
                    break
        except Exception as exc:
            if 'ctx' in locals():
                import traceback

                tb = traceback.format_exc()
                ctx.events.emit("run.error", {"error": str(exc), "traceback": tb}, level="error")
                ctx.status = "failed"
                ctx.ended_at = time.time()
                if ctx.started_at is not None:
                    ctx.elapsed_ms = int((ctx.ended_at - ctx.started_at) * 1000)
                # store last error so Evidence/Logs can surface it
                ctx.last_error = {"error": str(exc), "traceback": tb}
                self._update_state_file(ctx)
                ctx.events.emit(
                    "run.lifecycle",
                    {
                        "phase": "ended",
                        "run_id": ctx.run_id,
                        "status": "failed",
                    },
                )
                self._flush_events(ctx)
            print(colored(f"运行异常：{exc}", "red"))
        else:
            if 'ctx' in locals():
                ctx.status = "succeeded"
                ctx.ended_at = time.time()
                if ctx.started_at is not None:
                    ctx.elapsed_ms = int((ctx.ended_at - ctx.started_at) * 1000)
                self._update_state_file(ctx)
                ctx.events.emit(
                    "run.lifecycle",
                    {
                        "phase": "ended",
                        "run_id": ctx.run_id,
                        "status": "succeeded",
                    },
                )
            self._flush_events(ctx)

    def rollback(self) -> None:
        state = self.run_manager.load_latest()
        if not state:
            print("没有可回滚的 run。")
            return
        res = self.tool_router.git_rollback(state.checkpoint)
        print(f"已尝试回滚到 {state.checkpoint}: {res}")

    def _build_plan(self, task: str) -> Dict[str, Any]:
        return {
            "task": task,
            "steps": [
                "EnvAgent：决策构建/测试命令",
                "RepoScout：搜索相关文件与上下文",
                "PatchAuthor：生成补丁",
                "应用补丁：Search & Replace",
                "BuildDiagnose：构建并解析错误",
                "TestTriage：测试并解析失败",
            ],
            "commands": ["make -j", "make test"],
            "risks": ["补丁可能失败，需回滚", "构建/测试失败需要多轮迭代"],
            "max_iterations": self.max_iters,
        }

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
        choice = input("继续？(y=继续 / q=退出): ").strip().lower()
        if choice == "q":
            print("用户退出。")
            return False
        return True

    def _make_context(self, state, auto: bool) -> RunContext:
        events = EventBus()
        workdir = self._resolve_workdir()
        opts = {
            "interactive": not auto,
            "allow_wsl": True,
            "allow_fallback": not self._env_overrides().get("no_make_fallback", False),
            "make_cmd": self._env_overrides().get("make_cmd"),
            "use_wsl": self._env_overrides().get("use_wsl", False),
            "force_strategy": None,
            "build_only": self.build_only,
        }
        ctx = RunContext(
            run_id=state.run_ts,
            task=state.task,
            workspace=workdir,
            run_dir=state.run_dir,
            options=opts,
            policy={},
            tool_router=self.tool_router,
            run_manager=self.run_manager,
            events=events,
            skills=self._make_skills(),
            iteration=state.iteration,
            services={"llm": LLMService.from_env()},
            file_contents={},
        )

        # Initialize simple stage tracking for /api/runs/{run_id}
        ctx.stages = []
        for name in [
            Stage.PLAN.name,
            Stage.GATHER.name,
            Stage.EDIT.name,
            Stage.APPLY.name,
            Stage.PREPARE.name,
            Stage.VERIFY_BUILD.name,
            Stage.VERIFY_TEST.name,
        ]:
            ctx.stages.append(
                {
                    "name": name,
                    "status": "pending",
                    "message": "",
                    "started_at": None,
                    "ended_at": None,
                }
            )
        ctx.status = "queued"
        ctx.started_at = None
        ctx.ended_at = None
        ctx.elapsed_ms = None

        return ctx

    def _make_pipeline(self):
        reg = AgentRegistry()
        # PLAN: requirements analysis
        reg.register(Stage.PLAN, RequirementsAgentPlugin())
        # PREPARE: environment decision
        reg.register(Stage.PREPARE, EnvAgentPlugin())
        # GATHER / EDIT / VERIFY
        reg.register(Stage.GATHER, RepoScoutPlugin())
        reg.register(Stage.EDIT, AiderEditPlugin())
        reg.register(Stage.VERIFY_BUILD, BuildPlugin())
        reg.register(Stage.VERIFY_TEST, TestPlugin())
        return PipelineRunner(reg)

    def _make_skills(self) -> SkillRegistry:
        registry = SkillRegistry()
        registry.register(SearchSkill())
        registry.register(ReadFileSkill())
        registry.register(RunCommandSkill())
        return registry

    def _apply_patches(self, ctx: RunContext) -> bool:
        if not ctx.patch_queue:
            return True
        
        import json
        from .editing.protocol import parse_request
        from .editing.executor import EditExecutor

        executor = EditExecutor(ctx.file_contents, Path(ctx.workspace))

        for patch_path in ctx.patch_queue:
            patch_text = Path(patch_path).read_text(encoding="utf-8")
            try:
                payload = json.loads(patch_text)
                # backward compat: patch file may contain a JSON-encoded string of JSON
                if isinstance(payload, str):
                    payload = json.loads(payload)
                req = parse_request(payload)
            except Exception as e:
                print(colored(f"应用补丁失败：非法 JSON 或 schema 错误: {e}", "red"))
                return False

            result = executor.apply(req)
            if not result.ok:
                print(colored(f"应用编辑失败: {result.error}", "red"))
                return False

            if req.file_path not in ctx.applied_files:
                ctx.applied_files.append(req.file_path)
            ctx.events.emit("apply.diff", {"file": req.file_path, "diff": result.diff})
            print(colored(f"应用编辑成功: {req.file_path}", "green"))
        print(colored("所有补丁应用成功。", "green"))
        return True

    def _check_test_coverage_needed(self, ctx: RunContext) -> tuple[bool, str]:
        """
        Heuristic: if we modified any non-test code files but did not touch any test files,
        require adding/updating tests in a subsequent iteration.
        """
        applied = list(ctx.applied_files)
        if not applied:
            return False, ""

        def is_test_file(p: str) -> bool:
            s = p.replace("\\", "/").lower()
            name = s.split("/")[-1]
            return ("/tests/" in s) or ("/test/" in s) or name.startswith("test_") or name.endswith("_test.cpp") or name.endswith("_test.c") or name.endswith("_test.py")

        def is_code_file(p: str) -> bool:
            s = p.replace("\\", "/").lower()
            return any(s.endswith(ext) for ext in [".c", ".h", ".cpp", ".hpp", ".cc"])

        touched_tests = [p for p in applied if is_test_file(p)]
        touched_code = [p for p in applied if is_code_file(p) and not is_test_file(p)]

        if touched_code and not touched_tests:
            return True, f"已修改代码文件 {touched_code} 但未修改任何测试文件"
        return False, ""

    def _collect_hints(self, ctx: RunContext) -> List[str]:
        hints: List[str] = []
        if ctx.last_test_result:
            for f in ctx.last_test_result.get("summary", []):
                hints.append(f.get("suite", ""))
                hints.append(f.get("case", ""))
        if ctx.last_build_result:
            for e in ctx.last_build_result.get("summary", []):
                hints.append(e.get("message", ""))
        return [h for h in hints if h]

    def _flush_events(self, ctx: RunContext):
        transcript = ctx.run_dir / "transcript.json"
        ctx.events.flush_to(transcript)

    def _mark_stage_start(self, ctx: RunContext, stage_name: str):
        if not hasattr(ctx, "stages"):
            return
        now = time.time()
        for st in ctx.stages:
            if st.get("name") == stage_name:
                st["status"] = "running"
                st["started_at"] = now
                break

    def _mark_stage_end(self, ctx: RunContext, stage_name: str, success: bool, message: str = ""):
        if not hasattr(ctx, "stages"):
            return
        now = time.time()
        for st in ctx.stages:
            if st.get("name") == stage_name:
                st["status"] = "succeeded" if success else "failed"
                st["ended_at"] = now
                if message:
                    st["message"] = message
                break

    def _update_state_file(self, ctx: RunContext) -> None:
        """
        Dump a lightweight JSON state snapshot to agent_state.json in the current workspace.
        Only includes JSON-serializable core fields and a tail of recent events.
        """
        state_path = Path(ctx.workspace) / "agent_state.json"

        # Collect recent events if EventBus supports in-memory storage; otherwise, empty list.
        recent_events: List[Dict[str, Any]] = []
        events_obj = getattr(ctx, "events", None)
        if events_obj is not None:
            raw_events = getattr(events_obj, "events", None)
            if isinstance(raw_events, list):
                recent_events = raw_events[-50:]

        # Build a minimal changes summary: list of applied files and last few apply.diff events.
        changes_files: List[str] = list(getattr(ctx, "applied_files", []) or [])
        changes_diffs: Dict[str, str] = {}
        for ev in recent_events:
            if ev.get("type") == "apply.diff":
                payload = ev.get("payload") or {}
                file_path = payload.get("file")
                diff_text = payload.get("diff")
                if isinstance(file_path, str) and isinstance(diff_text, str):
                    changes_diffs[file_path] = diff_text

        def default(o: Any):
            # Fallback serializer: use string repr to avoid JSON errors.
            try:
                return str(o)
            except Exception:
                return "<unserializable>"

        # compute steps_done / total from ctx.stages if present
        stages_meta = getattr(ctx, "stages", [])
        total_steps = len(stages_meta) if isinstance(stages_meta, list) else 0
        done_steps = 0
        if isinstance(stages_meta, list):
            for st in stages_meta:
                if st.get("status") in ("succeeded", "failed"):
                    done_steps += 1

        snapshot: Dict[str, Any] = {
            "run_id": getattr(ctx, "run_id", None),
            "status": getattr(ctx, "status", None),
            "iteration": getattr(ctx, "iteration", None),
            "current_stage": getattr(ctx, "current_stage", None),
            "task": getattr(ctx, "task", None),
            "policy": getattr(ctx, "policy", {}),
            "options": getattr(ctx, "options", {}),
            "applied_files": changes_files,
            "changes_diffs": changes_diffs,
            "last_build_result": getattr(ctx, "last_build_result", None),
            "last_test_result": getattr(ctx, "last_test_result", None),
            "artifacts": getattr(ctx, "artifacts", {}),
            "events_tail": recent_events,
            "stages": stages_meta,
            "steps_done": done_steps,
            "steps_total": total_steps,
            "elapsed_ms": getattr(ctx, "elapsed_ms", None),
            "last_error": getattr(ctx, "last_error", None),
        }

        try:
            state_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, default=default),
                encoding="utf-8",
            )
        except Exception as e:
            # Best-effort; dashboard is non-critical.
            print(colored(f"写入 agent_state.json 失败: {e}", "red"))

    def _print_env(self, decision: Dict[str, Any]):
        print(colored("环境决策", "blue"))
        print(f"平台：{decision.get('platform')}，策略：{decision.get('strategy')}")
        cmds = decision.get("commands", {})
        print(f"构建命令：{cmds.get('build')}")
        print(f"测试命令：{cmds.get('test')}")
        for w in decision.get("warnings", []):
            print(colored(f"提示：{w}", "yellow"))

    def _env_overrides(self) -> Dict[str, Any]:
        return getattr(self, "env_overrides", {}) or {}

    def _resolve_workdir(self) -> Path:
        if (self.repo_root / "Makefile").exists():
            return self.repo_root
        demo = self.repo_root / "demo_c_project"
        if demo.exists():
            return demo
        return self.repo_root

