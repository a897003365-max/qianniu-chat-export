from pathlib import Path
import tempfile
import unittest
from source_manifest import source_manifest


class Tests(unittest.TestCase):
    def test_source_fingerprint_ignores_local_data_git_and_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            allowed = ("SKILL.md", "README.md", "requirements.txt", "scripts/main.py", "references/flow.md", "scripts/unified/worker.py", "scripts/fixtures/overlap-sanitized.json")
            excluded = (".git/config", ".env", "jev.txt", "gpt_key.txt", "runtime.json", "data/chat.jsonl", "scripts/__pycache__/main.pyc", "scripts/test-output/private.json", "scripts/fixtures/unreviewed.json")
            for relative in allowed + excluded:
                p = root / relative
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("synthetic", encoding="utf-8")
            before = source_manifest(root)
            self.assertEqual({r["path"] for r in before}, set(allowed))
            (root / ".git/config").write_text("changed git state", encoding="utf-8")
            (root / "data/chat.jsonl").write_text("changed local data", encoding="utf-8")
            self.assertEqual(source_manifest(root), before)
            (root / "scripts/main.py").write_text("changed code", encoding="utf-8")
            self.assertNotEqual(source_manifest(root), before)


if __name__ == "__main__":
    unittest.main()
