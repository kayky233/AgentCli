from pathlib import Path
from typing import Any, Dict, Optional

from .state import RunState
from .utils import ensure_dir, load_json, now_ts, write_json


class RunManager:
    def __init__(self, root: Path):
        self.root = root
        self.agent_dir = root / ".agent"
        self.runs_dir = self.agent_dir / "runs"
        ensure_dir(self.runs_dir)

    def create_run(self, task: str, auto: bool) -> RunState:
        ts = now_ts()
        run_dir = self.runs_dir / ts
        ensure_dir(run_dir)
        ensure_dir(run_dir / "context" / "files")
        ensure_dir(run_dir / "patches")
        ensure_dir(run_dir / "verify")
        state = RunState(task=task, run_ts=ts, run_dir=run_dir, auto=auto)
        self.save_state(state)
        self._write_latest(ts)
        return state

    def _write_latest(self, ts: str) -> None:
        latest = self.agent_dir / "latest_run"
        ensure_dir(latest.parent)
        latest.write_text(ts, encoding="utf-8")

    def load_latest(self) -> Optional[RunState]:
        latest = self.agent_dir / "latest_run"
        if not latest.exists():
            return None
        ts = latest.read_text(encoding="utf-8").strip()
        state_path = self.runs_dir / ts / "state.json"
        if not state_path.exists():
            return None
        data = load_json(state_path)
        return RunState.from_dict(data)

    def save_state(self, state: RunState) -> None:
        write_json(state.run_dir / "state.json", state.to_dict())
        self._write_latest(state.run_ts)

    def save_plan(self, state: RunState, plan: Dict[str, Any]) -> None:
        state.plan = plan
        write_json(state.run_dir / "plan.json", plan)
        self.save_state(state)

    def save_transcript(self, state: RunState) -> None:
        write_json(state.run_dir / "transcript.json", {"events": state.transcript})

    def save_context_file(self, state: RunState, name: str, content: str) -> None:
        path = state.run_dir / "context" / "files" / name
        ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")

    def save_context_search(self, state: RunState, content: str) -> None:
        path = state.run_dir / "context" / "rg.txt"
        ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")

    def save_patch(self, state: RunState, idx: int, patch: str) -> Path:
        path = state.run_dir / "patches" / f"{idx:03d}.diff"
        ensure_dir(path.parent)
        path.write_text(patch, encoding="utf-8")
        return path

    def save_verify_log(self, state: RunState, idx: int, name: str, content: str) -> Path:
        path = state.run_dir / "verify" / f"{idx:03d}_{name}.log"
        ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")
        return path

