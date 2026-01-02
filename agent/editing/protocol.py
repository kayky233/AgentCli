from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional


ActionType = Literal["edit", "multi_edit"]


@dataclass
class EditOp:
    old_string: str
    new_string: str
    expected_replacements: int


@dataclass
class EditRequest:
    action: ActionType
    file_path: str
    edits: List[EditOp]
    message: Optional[str] = ""


def parse_request(obj) -> EditRequest:
    """
    Validate the JSON object against the editing protocol schema.
    Raises ValueError on invalid input.
    """
    if not isinstance(obj, dict):
        raise ValueError("Payload must be a JSON object with fields: action, file_path, edits[]")
    action = obj.get("action")
    if action not in ("edit", "multi_edit"):
        raise ValueError("action must be 'edit' or 'multi_edit'")
    file_path = obj.get("file_path")
    if not file_path or not isinstance(file_path, str):
        raise ValueError("file_path is required")
    edits_raw = obj.get("edits")
    if not isinstance(edits_raw, list) or not edits_raw:
        raise ValueError("edits must be a non-empty array")
    edits: List[EditOp] = []
    for idx, e in enumerate(edits_raw, start=1):
        if not isinstance(e, dict):
            raise ValueError(f"edit #{idx} must be object")
        if "old_string" not in e or "new_string" not in e:
            raise ValueError(f"edit #{idx} missing old_string/new_string")
        if "expected_replacements" not in e:
            raise ValueError(f"edit #{idx} missing expected_replacements (must be int)")
        old = e["old_string"]
        new = e["new_string"]
        exp = e["expected_replacements"]
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"edit #{idx} old/new must be strings")
        if not isinstance(exp, int) or exp <= 0:
            raise ValueError(f"edit #{idx} expected_replacements must be positive int")
        edits.append(EditOp(old, new, exp))
    return EditRequest(action=action, file_path=file_path, edits=edits, message=obj.get("message", ""))

