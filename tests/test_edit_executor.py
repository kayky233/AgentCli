import unittest
from pathlib import Path
from agent.editing.protocol import EditRequest, EditOp
from agent.editing.executor import EditExecutor


class EditExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("runtime/test_tmp")
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.file_path = "foo.txt"
        self.abs_path = self.tmp / self.file_path
        self.abs_path.parent.mkdir(parents=True, exist_ok=True)
        self.abs_path.write_text("hello world\nhello world\n", encoding="utf-8")
        self.cache = {self.file_path: self.abs_path.read_text(encoding="utf-8")}
        self.executor = EditExecutor(self.cache, self.tmp)

    def tearDown(self):
        if self.tmp.exists():
            for p in sorted(self.tmp.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()

    def test_old_string_not_found(self):
        req = EditRequest(action="edit", file_path=self.file_path, edits=[EditOp("missing", "x", 1)])
        result = self.executor.apply(req, dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_occurrences_mismatch(self):
        req = EditRequest(action="edit", file_path=self.file_path, edits=[EditOp("hello", "x", 1)])
        result = self.executor.apply(req, dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("Expected 1 replacement(s) but found 2", result.error)

    def test_multi_edit_atomic_failure(self):
        req = EditRequest(
            action="multi_edit",
            file_path=self.file_path,
            edits=[
                EditOp("hello world", "hi", 1),
                EditOp("missing", "x", 1),  # will fail
            ],
        )
        result = self.executor.apply(req, dry_run=True)
        self.assertFalse(result.ok)
        # ensure original file not modified
        self.assertEqual(self.abs_path.read_text(encoding="utf-8"), "hello world\nhello world\n")

    def test_multi_edit_success(self):
        req = EditRequest(
            action="multi_edit",
            file_path=self.file_path,
            edits=[
                EditOp("hello world", "hi", 2),
            ],
        )
        result = self.executor.apply(req, dry_run=True)
        self.assertTrue(result.ok)
        self.assertIn("--- foo.txt", result.diff)


if __name__ == "__main__":
    unittest.main()

