import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import standalone as s

LOWER = s.q.millis("2026-11-03")


def message(cid, number, content=None):
    return {"messageId": str(number), "sendTime": str(LOWER + number),
            "cid": {"appCid": cid, "domain": "cntaobao"},
            "senderId": {"appUid": "synthetic-sender", "domain": "cntaobao"},
            "content": json.dumps(content or {"text": "synthetic message"})}


class Browser:
    def __init__(self):
        self.expected_nick = "fixture:staff"
        self.runtime_summary = {"identity_verified": True}
        self.last_metrics = {}
        self.calls = []
        self.rows = [{"cid": {"appCid": "first"}, "userID": {"appUid": "u1"}, "displayName": "buyer"},
                     {"cid": {"appCid": "second"}, "userID": {"appUid": "u2"}}]
        self.more = True
        self.failure = None

    def request(self, api, data):
        self.calls.append((api, copy.deepcopy(data)))
        if self.failure:
            raise self.failure
        if api == s.CONVERSATIONS:
            payload = {"result": self.rows}
        else:
            image = {"type": 7, "data": json.dumps({"url": "https://example.invalid/image.png", "size": 0})}
            payload = {"userMessages": [message(data["cid"], 200), message(data["cid"], 100, image)],
                       "hasMore": self.more, "nextCursor": LOWER + 100}
        return {"api": api, "v": "1.0", "ret": ["SUCCESS::ok"], "data": payload}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qn-single-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "export"
        self.browser = Browser()
        self.runtime = {"node": str(self.base / "node.exe"), "opencli_script": str(self.base / "main.js"), "ocr_python": None}
        self.runtime_patch = self.enterContext(patch.object(s, "resolve_runtime", return_value=self.runtime))
        self.open = self.enterContext(patch.object(s.TimedBrowser, "open", return_value=self.browser))

    def args(self, *extra, command="collect"):
        values = [command, "--output", str(self.output)]
        if command == "collect":
            values += ["--start", "2026-11-03", "--end", "2026-11-04"]
        return s.parser().parse_args(values + list(extra))

    def test_fresh_default_collection_requires_no_manifest_owner_or_ocr(self):
        report = s.start(self.args())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"], {"messages": 2, "image_refs": 1, "pages": 1})
        self.assertEqual(len(self.browser.calls), 2)
        self.assertEqual(self.open.call_args.kwargs, {"bind_current_identity": True, "allow_discovery": True})
        self.assertIsNone(s.q.read_json(self.output / "codex-runtime.json")["ocr_python"])
        self.assertFalse((self.output / "unified/state.sqlite3").exists())
        self.assertFalse((self.output / "month-manifest.json").exists())
        job = report["jobs"][0]
        self.assertTrue(job["receipt"]["path"].endswith("sample-receipt.json"))
        directory = self.output / "unified/messages" / job["job_key"]
        self.assertFalse((directory / "message-receipt.json").exists())
        refs = [json.loads(x) for x in (directory / "image_refs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(refs[0]["source_url"], "https://example.invalid/image.png")

    def test_probe_only_verifies_identity_not_message_access(self):
        result = s.start(self.args(command="probe"))
        self.assertEqual(result["status"], "identity_verified")
        self.assertFalse(result["message_access_verified"])
        self.assertEqual(self.browser.calls, [])
        self.assertFalse(self.open.call_args.kwargs["allow_discovery"])

    def test_staff_customer_and_cid_filters_use_observed_context(self):
        report = s.start(self.args("--staff", "fixture:other", "--customer", "buyer", "--cid", "second"))
        request = self.browser.calls[0][1]
        self.assertEqual((request["selfWwNick"], request["targetWwNick"]), ("cntaobaofixture:other", "cntaobaobuyer"))
        self.assertEqual(self.browser.calls[1][1]["userNick"], "cntaobaofixture:other")
        self.assertEqual(self.browser.calls[1][1]["userId"], "u2")
        self.assertEqual(report["selected_contexts"], 1)

    def test_other_shop_staff_stops_before_discovery(self):
        report = s.start(self.args("--staff", "unrelated:other"))
        self.assertEqual(report["error_code"], "staff_outside_shop")
        self.assertEqual(self.browser.calls, [])

    def test_unknown_cid_never_uses_guessed_context(self):
        report = s.start(self.args("--cid", "unknown"))
        self.assertEqual(report["error_code"], "cid_not_in_authorized_discovery")
        self.assertEqual(len(self.browser.calls), 1)

    def test_one_agent_explicitly_collects_all_discovered_contexts(self):
        self.browser.more = False
        report = s.start(self.args("--all-conversations", "--all-messages"))
        self.assertEqual((report["status"], len(report["jobs"]), report["counts"]["messages"]), ("collected", 2, 4))
        self.assertTrue(report["all_discovered_contexts_selected"])
        self.assertFalse(report["enumeration_complete"])

    def test_capped_discovery_is_never_all_store_complete(self):
        self.browser.rows = [{"cid": {"appCid": str(i)}, "userID": {"appUid": str(i)}} for i in range(500)]
        report = s.start(self.args())
        self.assertEqual(report["capped_days"], ["2026-11-03"])
        self.assertFalse(report["enumeration_complete"])
        self.assertEqual(report["selected_contexts"], 1)

    def test_empty_list_does_not_prove_no_records(self):
        self.browser.rows = []
        report = s.start(self.args())
        self.assertEqual(report["status"], "empty_result")
        self.assertFalse(report["message_access_verified"])
        self.assertFalse(report["enumeration_complete"])

    def test_verification_stops_once_with_local_actionable_receipt(self):
        self.browser.failure = s.BridgeError("verification_required")
        report = s.start(self.args())
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["error_code"], "verification_required")
        self.assertEqual(len(self.browser.calls), 1)
        self.assertEqual(s.q.read_json(self.output / "run.json")["manual_entry_url"], s.HISTORY_URL)

    def test_expired_login_points_to_official_login_without_retrying(self):
        self.open.side_effect = s.BridgeError("auth_required")
        report = s.start(self.args())
        self.assertEqual(report["error_code"], "auth_required")
        self.assertEqual(report["manual_entry_url"], s.LOGIN_URL)
        self.assertTrue(report["manual_entry_url"].startswith("https://login.taobao.com/member/login.jhtml?"))
        self.assertEqual(self.browser.calls, [])
        self.open.assert_called_once()

    def test_invalid_dates_and_existing_outputs_do_not_open_browser(self):
        args = self.args()
        args.end = args.start
        with self.assertRaises(s.q.ExportError):
            s.start(args)
        self.output.mkdir()
        (self.output / "retained.txt").write_text("retain", encoding="utf-8")
        with self.assertRaisesRegex(s.StandaloneError, "output_must_be_new"):
            s.start(self.args())
        self.open.assert_not_called()
        self.assertEqual((self.output / "retained.txt").read_text(), "retain")

    def test_console_contains_counts_not_message_bodies(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = s.main(["collect", "--output", str(self.output), "--start", "2026-11-03", "--end", "2026-11-04"])
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic message", stream.getvalue())
        self.assertNotIn("fixture:staff", stream.getvalue())

    def test_message_failures_have_safe_reasons_evidence_and_actions_without_stopping_other_contexts(self):
        original = self.browser.request
        for kind, expected in (("structure", "message_validation_failed"), ("conflict", "message_conflict"),
                               ("source", "source_api_or_payload_error")):
            with self.subTest(kind=kind):
                def request(api, data):
                    response = original(api, data)
                    if api != s.CONVERSATIONS and data["cid"] == "first":
                        if kind == "structure":
                            response["data"]["userMessages"] = "INVALID_PRIVATE_PAYLOAD"
                        elif kind == "conflict":
                            response["data"]["userMessages"] = [message("first", 200), message("first", 200, {"text": "PRIVATE_CONFLICT"})]
                        else:
                            response["ret"] = ["FAIL_BIZ_DENIED::PRIVATE_REASON"]
                    return response
                self.browser.request = request
                output = self.base / kind
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = s.main(["collect", "--start", "2026-11-03", "--end", "2026-11-04", "--output", str(output), "--all-conversations"])
                result = json.loads(stdout.getvalue())
                self.assertEqual((code, result["status"], result["error_code"]), (2, "needs_attention", "message_items_failed"))
                self.assertEqual(result["counts"]["messages"], 2)
                failure = result["failures"][0]
                self.assertEqual(failure["error_code"], expected)
                self.assertTrue(failure["action"])
                self.assertEqual(len(failure["failure_artifacts"]), 1)
                s.q.verify_artifacts(output, failure["failure_artifacts"])
                self.assertNotIn("PRIVATE", stdout.getvalue())

    def test_explicit_media_uses_shared_store_without_month_job_owners(self):
        s.start(self.args())
        import pipeline
        from test_media_store import png
        def download(task):
            data = png()
            Path(task["output_path"]).write_bytes(data)
            return {"path": task["output_path"], "bytes": len(data), "sha256": s.q.digest(data)}
        args = s.parser().parse_args(["media", "--output", str(self.output), "--stage", "downloads", "--limit", "1"])
        with patch.object(pipeline, "download_task", side_effect=download), contextlib.redirect_stdout(io.StringIO()):
            result = s.media_stage(args)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed_review"], 0)
        with contextlib.closing(__import__("sqlite3").connect(self.output / "unified/state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 1)

    def test_media_rejects_a_regular_paused_batch_task(self):
        self.output.mkdir()
        s.q.atomic_json(self.output / "run.json", {"kind": "batch", "output": str(self.output)})
        args = s.parser().parse_args(["media", "--output", str(self.output), "--stage", "downloads"])
        with self.assertRaisesRegex(s.StandaloneError, "not_a_standalone"):
            s.media_stage(args)
        self.assertFalse((self.output / "unified").exists())


if __name__ == "__main__":
    unittest.main()
