from pathlib import Path
from typing import Dict, List


class RepoScout:
    def __init__(self, tool_router, run_manager):
        self.tool_router = tool_router
        self.run_manager = run_manager

    def gather(self, state, hints: List[str]) -> Dict:
        summary = []
        search_terms = [h for h in hints if h]
        search_terms.append(state.task)
        unique_terms = list({t for t in search_terms if t})
        for term in unique_terms:
            out = self.tool_router.search(term, cwd=Path.cwd())
            self.run_manager.save_context_search(state, out)
            summary.append({"term": term, "matches": out.splitlines()[:20]})
        return {"terms": unique_terms, "files": summary}

    def snapshot_files(self, state, files: List[Path]) -> None:
        for path in files:
            if not path.exists() or path.is_dir():
                continue
            content = self.tool_router.read_file(path, start=1, end=200)
            name = path.name.replace("/", "_")
            self.run_manager.save_context_file(state, name, content)

