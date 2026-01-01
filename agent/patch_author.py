import difflib
from pathlib import Path
from typing import Dict, List


class PatchAuthor:
    def __init__(self, tool_router, run_manager):
        self.tool_router = tool_router
        self.run_manager = run_manager

    def generate(self, state, diagnostics: Dict) -> Dict:
        patches: List[str] = []
        notes: List[str] = []

        subtract_patch = self._maybe_fix_subtract_bug()
        if subtract_patch:
            patches.append(subtract_patch)
            notes.append("Auto-fix subtract bug in demo calculator.")

        if not patches:
            notes.append("PatchAuthor placeholder：未接入模型，未生成补丁。")
            return {"patches": [], "notes": notes}

        # Save patches to disk
        for idx, patch in enumerate(patches, start=1):
            self.run_manager.save_patch(state, idx, patch)
        state.patches = patches
        return {"patches": patches, "notes": notes}

    def _maybe_fix_subtract_bug(self) -> str:
        path = Path("demo_c_project/src/calculator.c")
        if not path.exists():
            return ""
        text_raw = path.read_text(encoding="utf-8", errors="ignore")
        text = text_raw.replace("\r\n", "\n")
        target_block = "int subtract(int a, int b) {\n    return a + b;\n}\n"
        if target_block not in text:
            return ""
        new_text = text.replace(target_block, "int subtract(int a, int b) {\n    return a - b;\n}\n", 1)
        orig_lines = text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile="a/demo_c_project/src/calculator.c",
            tofile="b/demo_c_project/src/calculator.c",
        )
        return "".join(diff)

