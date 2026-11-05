"""Isolated initialization and mocked stage contracts; standard library only."""
from contextlib import closing, redirect_stdout
import copy
import io
import json
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import init_task as i
import pipeline


class InitTests(unittest.TestCase):
    def setUp(self):
        # Keep test artifacts outside the candidate and inside its staging root.
        self.temp = tempfile.TemporaryDirectory(prefix="init-fixture-", dir=i.SKILL_ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = {}
        for name in ("node", "opencli_script", "ocr_python"):
            path = self.base / (name + ".fixture")
            path.touch(); self.runtime[name] = path
        self.manifest = {"schema": 2, "scope": "month", "start": "2031-02-01", "end": "2031-03-01",
            "timezone": "Asia/Shanghai", "expected_nick": "synthetic:operator", "coverage": {"enumeration_complete": False},
            "items": [{"cid": "same-cid", "user_id": "user-a", "query_user_nick": "cntaobaosynthetic:one", "lane": "codex", "latest_seen_day": "2031-02-27"},
                      {"cid": "same-cid", "user_id": "user-b", "query_user_nick": "cntaobaosynthetic:two", "lane": "kimi", "latest_seen_day": "2031-02-28"}]}
        self.source = self.base / "input.json"
        self.save_source()
        self.root = self.base / "new-task"

    def save_source(self):
        self.source.write_bytes(i.json_bytes(self.manifest))

    def initialize(self, sources=None):
        with patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")), patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")), patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network")):
            return i.initialize(self.root, sources or [self.source], **self.runtime)

    def test_new_directory_queue_and_explicit_runtime_source_pins(self):
        original = self.source.read_bytes()
        receipt = self.initialize()
        self.assertEqual(receipt["queue"]["jobs"], 2)
        self.assertEqual(receipt["queue"]["owners"], {"codex": 1, "kimi": 1})
        self.assertEqual((self.root / "month-manifest.json").read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        runtime = json.loads((self.root / "codex-runtime.json").read_bytes())
        self.assertEqual(runtime, {key: str(value.resolve()) for key, value in self.runtime.items()})
        q, m, scripts = pipeline.load_core(self.root)
        self.assertEqual(scripts, i.SKILL_ROOT / "scripts")
        self.assertEqual(q.MESSAGES, "mtop.taobao.wireless.amp2.im.paas.message.list")
        with closing(sqlite3.connect(self.root / "unified/state.sqlite3")) as connection:
            rows = connection.execute("SELECT owner,query_user_nick,user_id,manifest_json FROM queue_jobs ORDER BY owner").fetchall()
        self.assertEqual([row[0] for row in rows], ["codex", "kimi"])
        self.assertEqual(json.loads(rows[0][3])["start"], "2031-02-01")
        self.assertFalse(receipt["browser_created"])
        self.assertFalse(receipt["ocr_profile_created"])

    def test_existing_root_and_bad_manifest_never_overwrite(self):
        self.root.mkdir(); sentinel = self.root / "keep.txt"; sentinel.write_text("keep")
        with self.assertRaises(i.InitError): self.initialize()
        self.assertEqual(sentinel.read_text(), "keep")
        self.root = self.base / "invalid-task"
        self.manifest["items"][1]["lane"] = "codex"; self.save_source()
        with self.assertRaises(i.InitError): self.initialize()
        self.assertFalse(self.root.exists())

    def test_cli_has_one_export_path_and_requires_explicit_plan_dates(self):
        cli = i.SKILL_ROOT / "scripts/qianniu_export.py"
        for command in ("run", "run-month"):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable,"-X","utf8","-B",str(cli),command],
                    capture_output=True,text=True,encoding="utf-8",timeout=10,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                self.assertEqual(result.returncode,2)
                self.assertIn("invalid choice",result.stderr)
        result = subprocess.run([sys.executable,"-X","utf8","-B",str(cli),"plan-month",
            "--root",str(self.base),"--output",str(self.base/"planned.json")],
            capture_output=True,text=True,encoding="utf-8",timeout=10,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,2)
        self.assertIn("--start",result.stderr)
        self.assertIn("--end",result.stderr)
        self.assertFalse((self.base/"planned.json").exists())

    def test_multiple_manifests_preserve_owner_and_global_date_priority(self):
        second = copy.deepcopy(self.manifest)
        second["items"] = [{**item, "cid": "other-cid", "latest_seen_day": "2031-02-26"} for item in second["items"]]
        path = self.base / "second.json"; path.write_bytes(i.json_bytes(second))
        receipt = self.initialize([path, self.source])
        self.assertEqual(receipt["queue"]["jobs"], 4)
        queue = i.TaskQueue(self.root / "unified/state.sqlite3")
        claimed = queue.claim("codex", "fixture", 1)
        self.assertEqual(claimed["priority_day"], "2031-02-27")
        self.assertEqual(claimed["owner"], "codex")

    def test_mismatched_scope_duplicate_context_and_missing_runtime_reject(self):
        with self.assertRaises(i.InitError): self.initialize([self.source, self.source])
        other = copy.deepcopy(self.manifest); other["end"] = "2031-03-02"
        path = self.base / "other.json"; path.write_bytes(i.json_bytes(other))
        with self.assertRaises(i.InitError): self.initialize([self.source, path])
        self.runtime["node"] = self.base / "missing"
        with self.assertRaises(i.InitError): self.initialize()
        self.assertFalse(self.root.exists())

    def test_initialized_task_messages_only_contract_and_idle_media_stages_no_network(self):
        self.initialize()
        queue = i.TaskQueue(self.root / "unified/state.sqlite3")
        for owner in ("codex", "kimi"):
            task = queue.claim(owner, "fixture", 1)
            queue.fail(task, {"kind": "synthetic_terminal"})
        profile = {"engine": "fixture", "engine_version": "1", "worker_sha256": "a"*64,
                   "models": [{"name": "fixture", "sha256": "b"*64}], "parameters": {}}
        (self.root / "unified/ocr-profile.json").write_bytes(i.json_bytes(profile))
        with patch.object(pipeline.TimedBrowser, "open", side_effect=AssertionError("browser must stay idle")), patch.object(pipeline, "download_task", side_effect=AssertionError("network must stay idle")), patch.object(pipeline, "OCRProcess", side_effect=AssertionError("OCR must stay idle")), patch.object(pipeline, "make_profile", return_value=profile), patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")), redirect_stdout(io.StringIO()):
            results = [pipeline.run_stage(self.root, stage, "codex" if stage == "messages" else None, 1, 1) for stage in ("messages", "downloads", "ocr")]
        self.assertTrue(all(result["submitted"] == 0 and result["state"] == "drained" for result in results))

    def test_new_task_real_message_writer_with_mock_browser_publishes_messages_only(self):
        self.initialize()
        q, _, _ = pipeline.load_core(self.root)
        def request(api, data):
            self.assertEqual(api, q.MESSAGES)
            self.assertEqual(data["userNick"], self.manifest["items"][0]["query_user_nick"])
            raw = {"messageId": "synthetic-message", "cid": {"appCid": data["cid"], "domain": "cntaobao"},
                "sendTime": str(q.millis(self.manifest["start"])+1), "content": json.dumps({"text": "synthetic condition: not thicker than 20"}),
                "senderId": {"appUid": "synthetic-user", "domain": "cntaobao"}}
            return {"api": q.MESSAGES, "v": "1.0", "ret": ["SUCCESS::ok"], "data": {"userMessages": [raw], "hasMore": False, "nextCursor": None}}
        browser = SimpleNamespace(request=request, runtime_summary={"transport": "synthetic"}, last_metrics={})
        with patch.object(pipeline.TimedBrowser, "open", return_value=browser), patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")), patch.object(pipeline, "download_task", side_effect=AssertionError("unexpected download")), patch.object(pipeline, "OCRProcess", side_effect=AssertionError("unexpected OCR")), redirect_stdout(io.StringIO()):
            result = pipeline.run_stage(self.root, "messages", "codex", 1, 1)
        self.assertEqual((result["completed"], result["failed_review"]), (1, 0))
        receipts = list((self.root / "unified/messages").glob("*/message-receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_bytes())
        self.assertEqual(receipt["completion_scope"], "messages_only")
        self.assertEqual(receipt["scope"]["start"], self.manifest["start"])
        self.assertEqual(receipt["counts"]["messages"], 1)
        self.assertFalse((self.root / "unified/ocr-profile.json").exists())

    def test_fresh_process_rejects_unified_code_drift_before_claim_or_external_io(self):
        copied = self.base / "copied-skill"
        shutil.copytree(i.SKILL_ROOT, copied)
        with patch.object(i, "SKILL_ROOT", copied):
            self.initialize()
        module = copied / "scripts/unified/message_export.py"
        module.write_bytes(module.read_bytes()+b"\n# synthetic source drift after initialization\n")
        script = f'''import sys, sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(copied / "scripts/unified")!r})
import pipeline
root=Path({str(self.root)!r})
with patch.object(pipeline.TimedBrowser,"open",side_effect=AssertionError("external I/O boundary reached")):
    try:pipeline.run_stage(root,"messages","codex",1,1)
    except RuntimeError as error:
        assert str(error)=="Frozen source missing or changed: scripts/unified/message_export.py",str(error)
    else:raise AssertionError("source drift accepted")
with sqlite3.connect(root/"unified/state.sqlite3") as db:
    assert db.execute("SELECT COUNT(*) FROM queue_jobs WHERE state='pending' AND attempts=0").fetchone()[0]==2
assert not (root/"unified/runs").exists()
print("source drift rejected before claim")
'''
        result = subprocess.run([sys.executable,"-X","utf8","-B","-c",script],capture_output=True,
            text=True,encoding="utf-8",timeout=15,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("source drift rejected before claim",result.stdout)


if __name__ == "__main__":
    unittest.main()
