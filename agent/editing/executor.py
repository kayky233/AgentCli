from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from .protocol import EditRequest, EditOp


@dataclass
class AppliedEdit:
    file_path: str
    old_string: str
    new_string: str
    occurrences: int


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""
    diff: str = ""
    applied_edits: List[AppliedEdit] = None


class EditExecutor:
    """
    Validate and apply edit/multi_edit with strong guarantees:
    - file must be pre-read into file_cache
    - occurrences must equal expected_replacements
    - multi_edit executes atomically (all-or-nothing)
    """

    def __init__(self, file_cache: Dict[str, str], workspace: Path):
        self.file_cache = file_cache
        self.workspace = workspace

    def ensure_file_loaded(self, file_path: str) -> str:
        if file_path not in self.file_cache:
            raise ValueError("File must be read before editing")
        return self.file_cache[file_path]

    def _count_occurrences(self, content: str, needle: str) -> int:
        return content.count(needle)

    def _apply_single_edit(self, content: str, op: EditOp, file_path: str, idx: int) -> Tuple[str, AppliedEdit]:
        occ = self._count_occurrences(content, op.old_string)
        if occ == 0:
            raise ValueError(f"old_string not found in {file_path} (edit {idx}); ensure exact match including whitespace")
        if occ != op.expected_replacements:
            raise ValueError(
                f"Expected {op.expected_replacements} replacement(s) but found {occ} occurrence(s). "
                "Set expected_replacements to the actual count or refine old_string."
            )
        new_content = content.replace(op.old_string, op.new_string, op.expected_replacements)
        return new_content, AppliedEdit(file_path=file_path, old_string=op.old_string, new_string=op.new_string, occurrences=occ)

    def apply(self, req: EditRequest, dry_run: bool = False) -> ApplyResult:
        try:
            original = self.ensure_file_loaded(req.file_path)
        except Exception as e:
            return ApplyResult(ok=False, error=str(e), applied_edits=[])

        working = original
        applied: List[AppliedEdit] = []

        # Pre-validate all edits for multi_edit
        if req.action == "multi_edit":
            for idx, op in enumerate(req.edits, start=1):
                occ = self._count_occurrences(working, op.old_string)
                if occ == 0:
                    return ApplyResult(ok=False, error=f"old_string not found in {req.file_path} (edit {idx}); ensure exact match including whitespace", applied_edits=[])
                if occ != op.expected_replacements:
                    return ApplyResult(ok=False, error=f"Expected {op.expected_replacements} replacement(s) but found {occ} occurrence(s). Set expected_replacements to the actual count or refine old_string.", applied_edits=[])
                # simulate apply for next step
                working = working.replace(op.old_string, op.new_string, op.expected_replacements)
            # all good, now commit to file_cache and write
            diff = self._make_diff(original, working, req.file_path)
            if not dry_run:
                self._write_back(req.file_path, working)
                self.file_cache[req.file_path] = working
            return ApplyResult(ok=True, error="", diff=diff, applied_edits=applied)

        # action == edit (single apply, but keep same pipeline)
        working = original
        for idx, op in enumerate(req.edits, start=1):
            try:
                working, applied_edit = self._apply_single_edit(working, op, req.file_path, idx)
                applied.append(applied_edit)
            except Exception as e:
                return ApplyResult(ok=False, error=str(e), applied_edits=[])

        diff = self._make_diff(original, working, req.file_path)
        if not dry_run:
            self._write_back(req.file_path, working)
            self.file_cache[req.file_path] = working
        return ApplyResult(ok=True, error="", diff=diff, applied_edits=applied)

    def _write_back(self, file_path: str, content: str):
        abs_path = self.workspace / file_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    def _make_diff(self, old: str, new: str, file_path: str) -> str:
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=file_path,
            tofile=file_path,
            lineterm="",
            n=3,
        )
        return "\n".join(diff_lines)

