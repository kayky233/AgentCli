import json
from pathlib import Path
from typing import Any, Dict, Optional, List

from .framework.agent_types import Stage
from .framework.context import RunContext
from .framework.events import EventBus
from .framework.pipeline import PipelineRunner
from .framework.registry import AgentRegistry
from .agents.env_agent_plugin import EnvAgentPlugin
from .agents.reposcout_plugin import RepoScoutPlugin
from .agents.patch_author_plugin import PatchAuthorPlugin
from .agents.build_plugin import BuildPlugin
from .agents.test_plugin import TestPlugin
from .llm.service import LLMService
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
            pipeline.run_stage(Stage.PREPARE, ctx)
            if not ctx.env_decision or ctx.env_decision.get("strategy") == "error":
                print(colored("环境决策失败，无法继续。", "red"))
                self._flush_events(ctx)
                return
            self._print_env(ctx.env_decision)
            if not auto:
                choice = input("环境已选择，继续？(y/n): ").strip().lower()
                if choice not in ("y", "yes", ""):
                    return

            iteration = 0
            while iteration < self.max_iters:
                iteration += 1
                ctx.iteration = iteration

                pipeline.run_stage(Stage.GATHER, ctx, request=self._collect_hints(ctx))
                print(colored("GATHER 完成", "blue"))

                pipeline.run_stage(Stage.EDIT, ctx)
                print(colored(f"EDIT 完成，补丁数：{len(ctx.patch_queue)}", "blue"))

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
                        break
                else:
                    ctx.events.emit("apply.result", {"status": "skip", "patches": []})
                ctx.events.emit("stage.exit", {"stage": Stage.APPLY.name, "status": "ok"})

                build_results = pipeline.run_stage(Stage.VERIFY_BUILD, ctx)
                build_ok = build_results and build_results[-1].status == "ok"
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
                    break

                test_results = pipeline.run_stage(Stage.VERIFY_TEST, ctx)
                test_ok = test_results and test_results[-1].status == "ok"
                print(colored(f"TEST 结果：{'成功' if test_ok else '失败'}", "yellow" if test_ok else "red"))
                if test_ok:
                    print(colored("全部通过！", "green"))
                    break
                if auto:
                    continue
                ans = input("测试失败，继续迭代？(y/n): ").strip().lower()
                if ans not in ("y", "yes", ""):
                    break
        except Exception as exc:
            if 'ctx' in locals():
                ctx.events.emit("run.error", {"error": str(exc)}, level="error")
                self._flush_events(ctx)
            print(colored(f"运行异常：{exc}", "red"))
        else:
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
            iteration=state.iteration,
            services={"llm": LLMService.from_env()},
        )
        return ctx

    def _make_pipeline(self):
        reg = AgentRegistry()
        reg.register(Stage.PREPARE, EnvAgentPlugin())
        reg.register(Stage.GATHER, RepoScoutPlugin())
        reg.register(Stage.EDIT, PatchAuthorPlugin())
        reg.register(Stage.VERIFY_BUILD, BuildPlugin())
        reg.register(Stage.VERIFY_TEST, TestPlugin())
        return PipelineRunner(reg)

    def _apply_patches(self, ctx: RunContext) -> bool:
        if not ctx.patch_queue:
            return True
        
        import json
        for patch_path in ctx.patch_queue:
            patch_text = Path(patch_path).read_text(encoding="utf-8")
            
            # Try to parse as JSON (Search & Replace mode)
            try:
                edits = json.loads(patch_text)
                if isinstance(edits, list) and all(isinstance(e, dict) and "search_block" in e for e in edits):
                    # Apply Search & Replace edits
                    success, error_msg = self._apply_search_replace(ctx, edits)
                    if not success:
                        print(colored(f"应用编辑失败: {error_msg}", "red"))
                        return False
                    print(colored(f"应用了 {len(edits)} 个编辑。", "green"))
                    continue
            except json.JSONDecodeError:
                pass
            
            # Fallback: try git apply (legacy diff format)
            res = self.tool_router.git_apply_patch(patch_text, cwd=ctx.workspace)
            if res["exit_code"] != 0:
                print(colored("应用补丁失败", "red"))
                print(res["stderr"])
                return False
        print(colored("补丁应用成功。", "green"))
        return True

    def _apply_search_replace(self, ctx: RunContext, edits: list[dict]) -> tuple[bool, str]:
        """Apply Search & Replace edits to files."""
        for i, edit in enumerate(edits):
            file_path = edit.get("file_path", "")
            search_block = edit.get("search_block", "")
            replace_block = edit.get("replace_block", "")
            
            if not file_path or not search_block:
                return False, f"编辑 {i+1} 缺少必要字段"
            
            # Resolve file path relative to workspace and repo_root, with prefix stripping fallback
            candidates = []
            ws = Path(ctx.workspace)
            repo_root = getattr(ctx.tool_router, "repo_root", ws)
            candidates.append(ws / file_path)
            candidates.append(Path(repo_root) / file_path)
            if file_path.startswith("demo_c_project/"):
                trimmed = file_path.split("/", 1)[1]
                candidates.append(ws / trimmed)
                candidates.append(Path(repo_root) / trimmed)

            full_path = next((p for p in candidates if p.exists()), None)
            if full_path is None:
                return False, f"文件不存在: {file_path}"
            
            try:
                content = full_path.read_text(encoding="utf-8")
                
                if search_block not in content:
                    return False, f"在 {file_path} 中找不到 search_block（编辑 {i+1}）"
                
                if content.count(search_block) > 1:
                    return False, f"在 {file_path} 中 search_block 出现多次（编辑 {i+1}）"
                
                new_content = content.replace(search_block, replace_block, 1)
                full_path.write_text(new_content, encoding="utf-8")
                
                ctx.events.emit("apply.edit", {
                    "file": file_path,
                    "search_len": len(search_block),
                    "replace_len": len(replace_block)
                })
                
            except Exception as e:
                return False, f"处理文件 {file_path} 时出错: {e}"
        
        return True, ""

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

