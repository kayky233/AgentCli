from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RunState:
    task: str
    run_ts: str
    run_dir: Path
    auto: bool = False
    checkpoint: Optional[str] = None
    stage: str = "PLAN"
    iteration: int = 0
    plan: Dict[str, Any] = field(default_factory=dict)
    env_decision: Dict[str, Any] = field(default_factory=dict)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    patches: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "run_ts": self.run_ts,
            "run_dir": str(self.run_dir),
            "auto": self.auto,
            "checkpoint": self.checkpoint,
            "stage": self.stage,
            "iteration": self.iteration,
            "plan": self.plan,
            "env_decision": self.env_decision,
            "transcript": self.transcript,
            "diagnostics": self.diagnostics,
            "patches": self.patches,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunState":
        return cls(
            task=data.get("task", ""),
            run_ts=data.get("run_ts", ""),
            run_dir=Path(data.get("run_dir", ".")),
            auto=data.get("auto", False),
            checkpoint=data.get("checkpoint"),
            stage=data.get("stage", "PLAN"),
            iteration=int(data.get("iteration", 0)),
            plan=data.get("plan", {}),
            env_decision=data.get("env_decision", {}),
            transcript=data.get("transcript", []),
            diagnostics=data.get("diagnostics", {}),
            patches=data.get("patches", []),
        )

