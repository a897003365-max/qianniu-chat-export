"""Run offline synthetic tests with short temporary fixture paths on Windows."""
import importlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

scripts = Path(__file__).resolve().parent
sys.path[:0] = [str(scripts), str(scripts / "unified")]
node = os.environ.get("QIANNIU_TEST_NODE") or shutil.which("node")
if not node:
    raise SystemExit("Set QIANNIU_TEST_NODE to an installed Node executable for offline JavaScript tests")
os.environ["QIANNIU_TEST_NODE"] = str(Path(node).resolve())
modules = [importlib.import_module(p.stem) for directory in (scripts, scripts / "unified") for p in sorted(directory.glob("test_*.py"))]
with tempfile.TemporaryDirectory(prefix="qn-test-") as temporary:
    # These constants locate generated fixtures only, not bundled source fixtures.
    for module in modules:
        if module.__name__ in {"test_media_store", "test_pipeline", "test_message_export", "test_legacy_import"}:
            module.HERE = Path(temporary)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module) for module in modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
