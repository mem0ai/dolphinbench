import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import claude_native_read_guard as guard


class NativeReadGuardTests(unittest.TestCase):
    def test_memory_only_including_symlink_and_parent_escape(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            memory = root / "auto-memory"
            memory.mkdir()
            (memory / "MEMORY.md").write_text("known fact")
            outside = root / "hidden.yaml"
            outside.write_text("hidden grading criteria")
            (memory / "escape.md").symlink_to(outside)
            for path, expected in (
                (memory / "MEMORY.md", True), (outside, False),
                (memory / ".." / "hidden.yaml", False),
                (memory / "escape.md", False), (memory / "missing.md", False),
            ):
                with self.subTest(path=path):
                    event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
                    self.assertEqual(guard.permits_read(event, memory), expected)
            event = {"tool_name": "Read", "cwd": str(memory), "tool_input": {"file_path": "MEMORY.md"}}
            self.assertTrue(guard.permits_read(event, memory))
            event["tool_name"] = "Write"
            self.assertFalse(guard.permits_read(event, memory))

    def test_malformed_input_emits_deny(self):
        result = subprocess.run(
            [sys.executable, guard.__file__, "/missing/auto-memory"],
            input="not JSON", text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
