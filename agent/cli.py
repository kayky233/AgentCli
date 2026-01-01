import argparse
import sys
from pathlib import Path

from .orchestrator import Orchestrator
from .run_manager import RunManager
from .tool_router import ToolRouter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="多-agent CLI for C projects")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="生成计划")
    plan.add_argument("task", help="任务描述")
    plan.add_argument("--json", action="store_true", dest="as_json", help="JSON 输出")
    plan.add_argument("--auto", action="store_true", help="自动模式（跳过交互）")

    do = sub.add_parser("do", help="执行任务")
    do.add_argument("task", help="任务描述")
    do.add_argument("--auto", action="store_true", help="自动执行到底")

    rollback = sub.add_parser("rollback", help="回滚到最近一次 run 的 checkpoint")

    resume = sub.add_parser("resume", help="继续上一次 run")
    resume.add_argument("--auto", action="store_true", help="切换为自动模式")
    return parser


def main(argv=None):
    argv = argv or sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path.cwd()
    run_manager = RunManager(repo_root)
    tool_router = ToolRouter(repo_root)
    orchestrator = Orchestrator(repo_root, run_manager, tool_router)

    if args.command == "plan":
        orchestrator.plan_only(args.task, args.as_json, args.auto)
    elif args.command == "do":
        orchestrator.run(args.task, auto=args.auto)
    elif args.command == "rollback":
        orchestrator.rollback()
    elif args.command == "resume":
        orchestrator.run(task=None, auto=args.auto, resume=True)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

