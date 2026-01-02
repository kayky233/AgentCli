import argparse
import os
import sys
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

from .orchestrator import Orchestrator
from .run_manager import RunManager
from .tool_router import ToolRouter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="多-agent CLI for C projects")
    sub = parser.add_subparsers(dest="command", required=True)

    models = sub.add_parser("models", help="列出可用 LLM 模型")
    models.add_argument("filter", nargs="?", default="", help="可选过滤字符串（按模型 ID 包含匹配）")

    plan = sub.add_parser("plan", help="生成计划")
    plan.add_argument("task", help="任务描述")
    plan.add_argument("--json", action="store_true", dest="as_json", help="JSON 输出")
    plan.add_argument("--auto", action="store_true", help="自动模式（跳过交互）")
    plan.add_argument("--make-cmd", help="指定 make 命令或路径", dest="make_cmd")
    plan.add_argument("--no-make-fallback", action="store_true", help="禁止无 make 时的 python fallback")
    plan.add_argument("--use-wsl", action="store_true", help="在 Windows 下通过 WSL 执行构建命令")

    do = sub.add_parser("do", help="执行任务")
    do.add_argument("task", help="任务描述")
    do.add_argument("--auto", action="store_true", help="自动执行到底")
    do.add_argument("--build-only", action="store_true", help="仅构建，跳过测试")
    do.add_argument("--make-cmd", help="指定 make 命令或路径", dest="make_cmd")
    do.add_argument("--no-make-fallback", action="store_true", help="禁止无 make 时的 python fallback")
    do.add_argument("--use-wsl", action="store_true", help="在 Windows 下通过 WSL 执行构建命令")

    rollback = sub.add_parser("rollback", help="回滚到最近一次 run 的 checkpoint")

    resume = sub.add_parser("resume", help="继续上一次 run")
    resume.add_argument("--auto", action="store_true", help="切换为自动模式")
    resume.add_argument("--build-only", action="store_true", help="仅构建，跳过测试")
    resume.add_argument("--make-cmd", help="指定 make 命令或路径", dest="make_cmd")
    resume.add_argument("--no-make-fallback", action="store_true", help="禁止无 make 时的 python fallback")
    resume.add_argument("--use-wsl", action="store_true", help="在 Windows 下通过 WSL 执行构建命令")
    return parser


def main(argv=None):
    argv = argv or sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path.cwd()
    run_manager = RunManager(repo_root)
    tool_router = ToolRouter(repo_root)
    env_overrides = {
        "make_cmd": getattr(args, "make_cmd", None),
        "no_make_fallback": getattr(args, "no_make_fallback", False),
        "use_wsl": getattr(args, "use_wsl", False),
    }
    orchestrator = Orchestrator(
        repo_root,
        run_manager,
        tool_router,
        build_only=getattr(args, "build_only", False),
        env_overrides=env_overrides,
    )

    if args.command == "models":
        models = fetch_available_models()
        render_models(models, args.filter or "")
    elif args.command == "plan":
        orchestrator.plan_only(args.task, args.as_json, args.auto)
    elif args.command == "do":
        orchestrator.run(args.task, auto=args.auto)
    elif args.command == "rollback":
        orchestrator.rollback()
    elif args.command == "resume":
        orchestrator.run(task=None, auto=args.auto, resume=True)
    else:
        parser.print_help()


def fetch_available_models():
    """
    拉取模型列表。优先使用 AGENT_LLM_API_BASE；兼容 openrouter 和标准 openai /models 端点。
    """
    api_key = os.environ.get("AGENT_LLM_API_KEY", "").strip()
    base = os.environ.get("AGENT_LLM_API_BASE", "").strip() or os.environ.get("AGENT_LLM_BASE_URL", "").strip()
    if not base:
        # 默认 openrouter
        base = "https://openrouter.ai/api/v1"

    if "openrouter" in base:
        url = "https://openrouter.ai/api/v1/models"
    else:
        url = base.rstrip("/") + "/models"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    console = Console()
    console.print(f"[cyan]Base: {base}[/cyan]")
    console.print(f"[cyan]API Key 设置: {'是' if api_key else '否'}[/cyan]")
    if not api_key:
        console.print("[yellow]警告：未设置 AGENT_LLM_API_KEY，可能无法列出私有/付费模型[/yellow]")

    console.print(f"[cyan]请求模型列表: {url}[/cyan]")

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            console.print(f"[red]获取模型失败[/red]: http {resp.status_code}: {resp.text[:300]}")
            return []
        data = resp.json()
    except Exception as e:  # pragma: no cover - 调试输出
        console.print(f"[red]获取模型失败[/red]: {e}")
        return []

    # OpenRouter: {"data":[{"id":..., "pricing": {...}, "context_length": ...}, ...]}
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]
    # OpenAI 风格: {"object":"list","data":[{"id":...},...]}
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    # 兜底
    if isinstance(data, list):
        return data
    return []


def _classify_model(model_id: str) -> str:
    mid = model_id.lower()
    if any(k in mid for k in ["code", "codex", "dev", "devstral"]):
        return "Coding"
    if any(k in mid for k in ["reason", "r1", "opus", "sonnet", "haiku"]):
        return "Reasoning"
    return "Chat"


def render_models(models, filter_text: str = ""):
    console = Console()
    if not models:
        console.print("[yellow]未获取到模型列表，请检查网络或 API Key；或尝试设置 AGENT_LLM_API_BASE/AGENT_LLM_BASE_URL[/yellow]")
        return

    filter_text = filter_text.lower()
    table = Table(title="Available LLM Models")
    table.add_column("Model ID", style="cyan")
    table.add_column("Context Window")
    table.add_column("Pricing")
    table.add_column("Type")

    rows = 0
    for m in models:
        model_id = m.get("id", "") if isinstance(m, dict) else str(m)
        if filter_text and filter_text not in model_id.lower():
            continue

        ctx_len = ""
        pricing = ""
        if isinstance(m, dict):
            ctx_len = str(m.get("context_length") or m.get("context_window") or "")
            pr = m.get("pricing") or {}
            prompt_price = pr.get("prompt")
            comp_price = pr.get("completion")
            if prompt_price or comp_price:
                pricing = f"P:{prompt_price or '-'} / C:{comp_price or '-'}"

        mtype = _classify_model(model_id)
        table.add_row(model_id, ctx_len or "-", pricing or "-", mtype)
        rows += 1

    console.print(table)
    console.print(f"[green]共 {rows} 个模型[/green]" + (f"，过滤关键字：{filter_text}" if filter_text else ""))

    # 推荐模型
    recommendations = [
        "openai/gpt-5.1-codex",
        "openai/gpt-5.1-codex-max",
        "anthropic/claude-3.5-sonnet",
        "openai/gpt-4.1",
        "devstral",
    ]
    available_recs = [m for m in recommendations if any(m in (md.get("id", "") if isinstance(md, dict) else str(md)) for md in models)]
    console.print("\n[bold]推荐（Coding 优先）[/bold]:")
    if available_recs:
        for rid in available_recs:
            console.print(f" - [cyan]{rid}[/cyan]")
    else:
        for rid in recommendations:
            console.print(f" - [cyan]{rid}[/cyan] (若可用)")


if __name__ == "__main__":
    main()

