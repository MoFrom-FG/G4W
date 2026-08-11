"""Fail-closed guards for optional vector addon injection."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.memory.conversation import ConversationStore


class VectorFailClosedTests(unittest.TestCase):
    def test_read_memory_omits_vector_when_vector_import_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory")
            store.user_memory_path("sender").parent.mkdir(parents=True, exist_ok=True)
            store.user_memory_path("sender").write_text("# User Memory\nplain memory", encoding="utf-8")

            def import_side_effect(name, *args, **kwargs):
                if name.startswith("G4W.memory.vector"):
                    raise ImportError("boom-vector-import")
                return real_import(name, *args, **kwargs)

            real_import = __import__
            with mock.patch("builtins.__import__", side_effect=import_side_effect):
                text = store.read_memory("sender", query="hello")

        self.assertIn("## User Memory", text)
        self.assertIn("## L1 Memory Index", text)
        self.assertNotIn("## Vector Retrieval Hits", text)
        self.assertNotIn("boom-vector-import", text)


if __name__ == "__main__":
    unittest.main()
