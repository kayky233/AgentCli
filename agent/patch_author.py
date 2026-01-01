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
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "return a + b;" not in text:
            return ""
        patch = """diff --git a/demo_c_project/src/calculator.c b/demo_c_project/src/calculator.c
--- a/demo_c_project/src/calculator.c
+++ b/demo_c_project/src/calculator.c
@@
-int subtract(int a, int b) {
-    // Buggy implementation on purpose for the demo; should be a - b.
-    return a + b;
-}
+int subtract(int a, int b) {
+    return a - b;
+}
"""
        return patch

