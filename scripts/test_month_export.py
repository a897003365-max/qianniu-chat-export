import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import qianniu_export as q
import month_support as m
from test_qianniu_export import FakeBrowser, FakeOCR, message, response as base_response, ITEM, MANIFEST, LOWER, UPPER


HERE = Path(__file__).resolve().parent
MONTH_START, MONTH_END = q.millis("2026-10-01"), q.millis("2026-11-01")


def response(messages, has_more=False, cursor=None):
    return {**base_response(messages, has_more, cursor), "api": q.MESSAGES, "v": "1.0"}


def month_manifest():
    return {"schema": 2, "scope": "month", "timezone": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-11-01",
            "expected_nick": MANIFEST["expected_nick"], "items": []}


def month_item(**changes):
    return {"query_user_nick": MANIFEST["user_nick"], "cid": ITEM["cid"], "user_id": ITEM["user_id"],
            "latest_seen_day": "2026-10-10", "lane": "codex", **changes}


def rows_response(rows):
    return {"ret": ["SUCCESS::ok"], "data": {"result": rows}}


def row(cid, uid):
    return {"cid": {"appCid": cid}, "userID": {"appUid": uid}, "displayName": "fixture"}


class MonthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="month-test-", dir=HERE)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_month_collects_days_missing_from_discovery(self):
        item = month_item()
        october30, october05 = q.millis("2026-10-30"), q.millis("2026-10-05")
        browser = FakeBrowser([response([message("newer", october30)], True, q.millis("2026-10-06")),
                               response([message("prior_month", MONTH_START-1), message("early", october05)], True, MONTH_START-2)])
        counts = q.export_item(self.root/"codex", month_manifest(), item, browser, FakeOCR(), False, "descending", month=True)
        self.assertEqual(counts["messages"], 2)
        self.assertEqual(browser.requests[0][1]["cursor"], MONTH_END-1)
        directory = self.root/"codex"/"month_items"/m.month_work_id(month_manifest(), item)
        output = [json.loads(line) for line in (directory/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["message_id"] for record in output], ["newer", "early"])
        receipt = q.read_json(directory/"receipt.json")
        self.assertEqual(receipt["interval"]["start_ms"], MONTH_START)
        self.assertFalse(receipt["enumeration_complete"])

    def seed_daily_page(self):
        first = response([message("oct20", q.millis("2026-10-20")), message("oct31", UPPER-10)], True, q.millis("2026-10-19"))
        q.export_item(self.root/"codex", MANIFEST, ITEM, FakeBrowser([first]), FakeOCR(), False, "descending")
        directory = self.root/"codex"/"items"/q.work_id(ITEM)
        raw = directory/"raw"/"000001.json"
        return raw, q.read_json(raw)["request"]

    def test_exact_seed_chain_continues_past_daily_boundary(self):
        self.seed_daily_page()
        original = {path: path.read_bytes() for path in (self.root/"codex"/"items").rglob("*") if path.is_file()}
        browser = FakeBrowser([response([message("before", MONTH_START-1), message("month_start", MONTH_START)], True, MONTH_START-2)])
        item = month_item()
        counts = q.export_item(self.root/"codex", month_manifest(), item, browser, FakeOCR(), False, "descending", month=True, seeds=m.Seeds(self.root, q))
        self.assertEqual(counts["messages"], 3)
        self.assertEqual(counts["seed_pages_reused"], 1)
        self.assertEqual(len(browser.requests), 1)
        self.assertEqual(browser.requests[0][1]["cursor"], q.millis("2026-10-19"))
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertTrue(q.export_item(self.root/"codex", MANIFEST, ITEM, FakeBrowser([]), FakeOCR(), False, "descending")["skipped"])

    def test_seed_fingerprint_identity_cursor_and_hash(self):
        raw, request = self.seed_daily_page()
        seeds = m.Seeds(self.root, q)
        self.assertIsNotNone(seeds.lookup(request))
        for key, value in (("userNick", "cntaobaoanother"), ("userId", "456"), ("cursor", MONTH_END-2), ("forward", True)):
            other = {**request, key: value}
            self.assertIsNone(seeds.lookup(other))
        raw.write_text("changed", encoding="utf-8")
        with self.assertRaises(q.ExportError):
            seeds.lookup(request)
        with self.assertRaises(q.ExportError):
            m.Seeds(self.root, q)

    def test_seed_transport_api_version_and_legacy_ecode_evidence(self):
        raw, request = self.seed_daily_page()
        reused = m.Seeds(self.root, q).lookup(request)
        self.assertIn("not_recorded", reused["source"]["transport"]["ecode_evidence"])
        original = q.read_json(raw)
        for field, value in (("api", "different.api"), ("v", "2.0")):
            altered = copy.deepcopy(original)
            altered["response"][field] = value
            with self.assertRaisesRegex(q.ExportError, "API/version"):
                m.seed_transport(altered, q)
        altered = copy.deepcopy(original)
        altered["transport"] = {"api": q.MESSAGES, "v": "1.0", "ecode": 1}
        with self.assertRaisesRegex(q.ExportError, "transport metadata"):
            m.seed_transport(altered, q)

    def test_checkpoints_seed_only_committed_verified_pages(self):
        first = response([message("oct31", UPPER-10)], True, LOWER+10)
        with self.assertRaises(q.ExportError):
            q.export_item(self.root/"codex", MANIFEST, ITEM, FakeBrowser([first, q.ExportError("interrupted")]), FakeOCR(), False, "descending")
        directory = self.root/"codex"/"items"/q.work_id(ITEM)
        self.assertFalse((directory/"receipt.json").exists())
        request = q.read_json(directory/"raw"/"000001.json")["request"]
        self.assertIsNotNone(m.Seeds(self.root, q).lookup(request))

    def test_different_query_nicks_and_uid_variants_have_distinct_paths_and_requests(self):
        variants = [month_item(), month_item(query_user_nick="cntaobaoother"), month_item(user_id="different")]
        self.assertEqual(len({m.month_work_id(month_manifest(), item) for item in variants}), 3)
        for item in variants:
            browser = FakeBrowser([response([message("id", MONTH_START)], False)])
            q.export_item(self.root/"codex", month_manifest(), item, browser, FakeOCR(), False, "descending", month=True)
            self.assertEqual(browser.requests[0][1]["userNick"], item["query_user_nick"])
            self.assertEqual(browser.requests[0][1]["userId"], item["user_id"])

    def test_month_resume_checks_contiguous_chain(self):
        item = month_item()
        with self.assertRaises(q.ExportError):
            q.export_item(self.root/"codex", month_manifest(), item, FakeBrowser([
                response([message("late", UPPER-10)], True, LOWER), q.ExportError("interrupted")]), FakeOCR(), False, "descending", month=True)
        directory = self.root/"codex"/"month_items"/m.month_work_id(month_manifest(), item)
        checkpoint = q.read_json(directory/"checkpoint.json")
        checkpoint["next_cursor"] = MONTH_START
        q.atomic_json(directory/"checkpoint.json", checkpoint)
        with self.assertRaisesRegex(q.ExportError, "checkpoint cursor"):
            q.export_item(self.root/"codex", month_manifest(), item, FakeBrowser([]), FakeOCR(), False, "descending", month=True)

    def test_cross_lane_receipted_images_and_ocr_are_reused(self):
        raw_bytes = b"\x89PNG\r\n\x1a\nfixture"
        content = {"type": 7, "data": {"url": "https://img.alicdn.com/fixture.png", "size": 0}}
        class Download:
            headers = {"Content-Type": "image/png"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return raw_bytes
        with patch("urllib.request.urlopen", return_value=Download()):
            q.export_item(self.root/"codex", MANIFEST, ITEM, FakeBrowser([response([message("image", LOWER, content)], False)]), FakeOCR(), False, "descending")
        ocr = FakeOCR()
        with patch("urllib.request.urlopen", side_effect=AssertionError("must use verified image cache")):
            counts = q.export_item(self.root/"kimi", month_manifest(), month_item(lane="kimi"), FakeBrowser([]), ocr, False, "descending", month=True, seeds=m.Seeds(self.root, q))
        self.assertEqual(counts["image_messages"], 1)
        self.assertEqual(ocr.calls, 0)
        self.assertEqual((self.root/"kimi"/"images").glob("*.png").__next__().read_bytes(), raw_bytes)

    def test_planner_preserves_all_raw_scope_variants_balances_and_keeps_gap_visible(self):
        q.atomic_json(self.root/"discovery-scope.json", {"start": "2026-10-01", "end": "2026-11-01", "expected_nick": "fixture:agent"})
        initial = self.root/"discovery"/"2026-10-31.json"
        q.atomic_json(initial, rows_response([row("same", "1"), row("initial_only", "2")]))
        q.atomic_json(initial.with_name("2026-10-31.receipt.json"), {"sha256": q.file_digest(initial)})
        supplement = self.root/"coverage"/"observed-filter-discovery"
        q.atomic_json(supplement/"scope.json", {"start": "2026-10-01", "end": "2026-11-01", "expected_nick": "fixture:agent"})
        request = {"selfWwNick": "cntaobaofixture:other", "targetWwNick": None, "beginDate": "20261010", "endDate": "20261010"}
        q.atomic_json(supplement/"2026-10-10"/"other.json", {"api": q.CONVERSATIONS, "v": "1.0", "request": request,
            "response": rows_response([row("same", "3"), row("same", "4"), row("new", "5")])})
        q.atomic_json(supplement/"query-counts.json", [{"day": "2026-10-10", "response_file": "2026-10-10/other.json", "count": 3}])
        output = self.root/"month.json"
        args = SimpleNamespace(root=str(self.root), output=str(output), start="2026-10-01", end="2026-11-01", initial_discovery=None, supplemental_discovery=None)
        result = m.plan_month(args, q)
        manifest = q.read_json(output)
        self.assertEqual(result["query_scopes"], 5)
        codex, kimi = m.validate_month(manifest, "codex", q), m.validate_month(manifest, "kimi", q)
        self.assertEqual((len(codex), len(kimi)), (3, 2))
        keys = lambda items: {(i["query_user_nick"], i["cid"], i["user_id"]) for i in items}
        self.assertFalse(keys(codex) & keys(kimi))
        self.assertEqual(len(keys(codex) | keys(kimi)), 5)
        self.assertEqual({i["user_id"] for i in manifest["items"] if i["cid"] == "same"}, {"1", "3", "4"})
        self.assertEqual(manifest["items"][0]["latest_seen_day"], "2026-10-31")
        self.assertFalse(manifest["coverage"]["enumeration_complete"])
        self.assertEqual(len(manifest["coverage"]["missing_initial_days"]), 30)
        with self.assertRaises(q.ExportError):
            m.plan_month(args, q)

    def test_existing_lane_ocr_variant_is_verified_and_never_replaced(self):
        data = b"\x89PNG\r\n\x1a\nfixture"
        content = {"type": 7, "data": {"url": "https://img.alicdn.com/shared.png", "size": 0}}
        class Download:
            headers = {"Content-Type": "image/png"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return data
        class OtherOCR(FakeOCR):
            def recognize(self, path):
                result = super().recognize(path)
                result["version"] = "second-observation"
                return result
        with patch("urllib.request.urlopen", return_value=Download()):
            for lane, ocr in (("codex", FakeOCR()), ("kimi", OtherOCR())):
                q.export_item(self.root/lane, MANIFEST, ITEM, FakeBrowser([response([message("image", LOWER, content)])]), ocr, False, "descending")
        ocr_path = self.root/"kimi"/"ocr"/(q.digest(data)+".json")
        before = ocr_path.read_bytes()
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")):
            q.export_item(self.root/"kimi", month_manifest(), month_item(lane="kimi"), FakeBrowser([]), FakeOCR(), False, "descending", month=True, seeds=m.Seeds(self.root, q))
        self.assertEqual(ocr_path.read_bytes(), before)
        self.assertEqual(q.read_json(ocr_path)["version"], "second-observation")

    def test_capped_discovery_remains_unresolved(self):
        q.atomic_json(self.root/"discovery-scope.json", {"start": "2026-10-01", "end": "2026-11-01", "expected_nick": "fixture:agent"})
        initial = self.root/"discovery"/"2026-10-31.json"
        q.atomic_json(initial, rows_response([row(str(i), "1") for i in range(500)]))
        q.atomic_json(initial.with_name("2026-10-31.receipt.json"), {"sha256": q.file_digest(initial)})
        args = SimpleNamespace(root=str(self.root), output=str(self.root/"month.json"), start="2026-10-01", end="2026-11-01", initial_discovery=None, supplemental_discovery=None)
        result = m.plan_month(args, q)
        self.assertEqual(result["capped_queries"], 1)
        self.assertFalse(result["enumeration_complete"])


if __name__ == "__main__":
    unittest.main()
