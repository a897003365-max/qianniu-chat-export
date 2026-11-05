"""Offline boundary, pagination, resume and content tests; no browser or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qianniu_export as q


DAY = "2026-10-31"
LOWER = q.millis(DAY)
UPPER = LOWER + 86400000
ITEM = {"day": DAY, "cid": "conversation.1", "user_id": "123", "display_name": "fixture", "lane": "codex"}
MANIFEST = {"schema": 1, "timezone": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-11-01",
            "expected_nick": "fixture:agent", "user_nick": "cntaobaofixture:agent", "items": [ITEM]}


def message(identifier, timestamp, content=None):
    return {"messageId": identifier, "sendTime": str(timestamp), "cid": {"appCid": ITEM["cid"]},
            "content": json.dumps(content if content is not None else {"text": "原始文字"}),
            "senderId": {"appUid": "123"}}


def response(messages, has_more=False, cursor=None):
    return {"ret": ["SUCCESS::调用成功"], "success": True,
            "data": {"userMessages": messages, "hasMore": has_more, "nextCursor": cursor}}


class FakeBrowser:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, api, data):
        self.requests.append((api, data))
        if not self.responses:
            raise AssertionError("Unexpected API call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeOCR:
    def __init__(self):
        self.calls = 0

    def recognize(self, path):
        self.calls += 1
        return {"engine": "fixture", "frames": [{"frame_index": 0, "text": "图片文字",
            "entries": [{"box": [[0, 0], [10, 0], [10, 10], [0, 10]], "text": "图片文字", "confidence": 0.9}]}]}


class ExportTests(unittest.TestCase):
    def test_dates_timezone_descending_and_exclusive_end(self):
        self.assertEqual(q.days("2026-10-30", "2026-11-01"), ["2026-10-31", "2026-10-30"])
        self.assertEqual(q.dt.datetime.fromtimestamp(LOWER / 1000, q.dt.timezone.utc).isoformat(), "2026-10-30T16:00:00+00:00")
        for invalid in ("20261031", "2026-2-01", "2026-13-01"):
            with self.assertRaises(q.ExportError):
                q.date(invalid)

    def test_manifest_duplicate_and_lane_validation(self):
        duplicate = copy.deepcopy(MANIFEST)
        duplicate["items"].append(ITEM)
        with self.assertRaises(q.ExportError):
            q.validate_manifest(duplicate, "codex")
        invalid = copy.deepcopy(MANIFEST)
        invalid["items"][0]["day"] = "2026-11-01"
        with self.assertRaises(q.ExportError):
            q.validate_manifest(invalid, "codex")
        self.assertEqual(q.validate_manifest(MANIFEST, "kimi"), [])

    def test_auth_rate_limit_and_captcha_fail_without_payload_logging(self):
        for code in ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TRAFFIC_LIMIT", "FAIL_SYS_USER_VALIDATE"):
            with self.assertRaisesRegex(q.ExportError, code) as failure:
                q.api_data({"ret": [code + "::https://sensitive.example/?token=secret"], "success": False})
            self.assertNotIn("secret", str(failure.exception))

    def test_conversation_shape_and_unknown_pagination(self):
        data = {"result": [{"cid": {"appCid": "a"}, "userID": {"appUid": "1"}, "displayName": "客户"}],
                "ret": ["SUCCESS::ok"], "success": True}
        self.assertEqual(q.conversation_items(data, DAY)[0]["user_id"], "1")
        self.assertEqual(q.conversation_items({"ret": data["ret"], "data": {"result": data["result"]}}, DAY)[0]["cid"], "a")
        data["hasMore"] = "true"
        with self.assertRaises(q.ExportError):
            q.conversation_items(data, DAY)

    def test_string_and_integer_image_type_and_actual_text(self):
        text = q.parse_message(message("text", LOWER), ITEM["cid"])
        self.assertEqual(text["text"], "原始文字")
        for value in ("7", 7):
            image = q.parse_message(message("image", LOWER, {"type": value, "data": json.dumps({"url": "https://img.alicdn.com/x.png", "size": 0})}), ITEM["cid"])
            self.assertEqual(image["image"]["source"]["size"], 0)
            self.assertIsNone(image["text"])
        with self.assertRaises(q.ExportError):
            q.parse_message(message("bad", LOWER), "wrong-cid")

    def test_cursor_stall_wrong_direction_and_missing_has_more(self):
        for next_cursor in (UPPER - 1, UPPER + 1, None):
            with self.assertRaises(q.ExportError):
                q.parse_page(response([message("a", UPPER - 10)], "true", next_cursor), ITEM, UPPER-1, "descending", LOWER, UPPER)
        with self.assertRaises(q.ExportError):
            q.parse_page(response([], "true", UPPER-10), ITEM, UPPER-1, "descending", LOWER, UPPER)
        with self.assertRaises(q.ExportError):
            q.as_bool("FALSE", "hasMore")

    def test_complete_pagination_boundary_and_dedup(self):
        first = [message("late", UPPER-10), message("middle", LOWER+100)]
        second = [message("middle", LOWER+100), message("start", LOWER), message("prior", LOWER-1)]
        browser = FakeBrowser([response(first, "true", LOWER+99), response(second, "true", LOWER-2)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counts = q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            self.assertEqual(counts["messages"], 3)
            path = root / "items" / q.work_id(ITEM)
            records = [json.loads(line) for line in (path/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["message_id"] for record in records], ["late", "middle", "start"])
            self.assertEqual(q.read_json(path/"receipt.json")["stop_reason"], "date_bound")
            self.assertEqual(len(browser.requests), 2)
            resumed = q.export_item(root, MANIFEST, ITEM, FakeBrowser([]), FakeOCR(), False, "descending")
            self.assertTrue(resumed["skipped"])
            (path/"messages.jsonl").write_text("corrupt", encoding="utf-8")
            with self.assertRaises(q.ExportError):
                q.export_item(root, MANIFEST, ITEM, FakeBrowser([]), FakeOCR(), False, "descending")

    def test_exclusive_upper_bound_in_ascending_mode(self):
        browser = FakeBrowser([response([message("start", LOWER), message("last", UPPER-1), message("next", UPPER)], "true", UPPER+1)])
        with tempfile.TemporaryDirectory() as temporary:
            counts = q.export_item(Path(temporary), MANIFEST, ITEM, browser, FakeOCR(), True, "ascending")
            self.assertEqual(counts["messages"], 2)

    def test_boundary_page_ignores_out_of_scope_content_schema(self):
        older = message("older", LOWER-1)
        older["content"] = "not-json"
        older_image = message("older-image", LOWER-2, {"type": 7, "data": {}})
        browser = FakeBrowser([response([message("inside", LOWER+100), older, older_image], "true", LOWER-3)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counts = q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            self.assertEqual(counts["messages"], 1)
            saved = q.read_json(root/"items"/q.work_id(ITEM)/"raw"/"000001.json")
            self.assertEqual(saved["response"]["data"]["userMessages"][1]["content"], "not-json")

    def test_interrupted_resume_uses_saved_raw_page(self):
        first = response([message("late", UPPER-10)], True, LOWER+100)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = FakeBrowser([first, q.ExportError("fixture interruption")])
            with self.assertRaises(q.ExportError):
                q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            resumed = FakeBrowser([response([message("early", LOWER)], False)])
            counts = q.export_item(root, MANIFEST, ITEM, resumed, FakeOCR(), False, "descending")
            self.assertEqual(counts["messages"], 2)
            self.assertEqual(resumed.requests[0][1]["cursor"], LOWER+100)
            self.assertEqual(len(resumed.requests), 1)

    def test_conflicting_duplicate_does_not_complete(self):
        browser = FakeBrowser([response([message("late", LOWER+300), message("same", LOWER+200)], True, LOWER+100),
                               response([message("same", LOWER+200, {"text": "changed"}), message("early", LOWER)], False)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(q.ExportError, "conflicting content"):
                q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            self.assertFalse((root/"items"/q.work_id(ITEM)/"receipt.json").exists())

    def test_complete_repeated_page_stops_and_retains_failed_response(self):
        repeated = [message("same", LOWER+200)]
        browser = FakeBrowser([response(repeated, True, LOWER+100), response(repeated, False)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(q.ExportError, "Complete message page repeated"):
                q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            directory = root/"items"/q.work_id(ITEM)
            self.assertFalse((directory/"receipt.json").exists())
            self.assertEqual(len(list((directory/"failures").glob("*.json"))), 1)

    def test_overlapping_read_state_change_preserves_observations(self):
        original = message("overlap", LOWER+200)
        original["readStatus"] = "0"
        changed = copy.deepcopy(original)
        changed["readStatus"] = "1"
        changed["unreadCount"] = "0"
        browser = FakeBrowser([response([message("late", LOWER+300), original], True, LOWER+100),
                               response([changed, message("early", LOWER)], False)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counts = q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            self.assertEqual(counts["messages"], 3)
            rows = [json.loads(line) for line in (root/"items"/q.work_id(ITEM)/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
            overlap = next(row for row in rows if row["message_id"] == "overlap")
            self.assertEqual([raw["readStatus"] for raw in overlap["raw_observations"]], ["0", "1"])

    def test_image_bytes_ocr_separate_and_resume_verified(self):
        image_content = {"type": 7, "data": {"url": "https://img.alicdn.com/image.png", "size": 0, "suffix": "jpg"}}
        browser = FakeBrowser([response([message("image", LOWER, image_content)])])
        raw_bytes = b"\x89PNG\r\n\x1a\nfixture"

        class Download:
            headers = {"Content-Type": "image/png"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return raw_bytes

        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen", return_value=Download()) as fetch:
            root, ocr = Path(temporary), FakeOCR()
            counts = q.export_item(root, MANIFEST, ITEM, browser, ocr, False, "descending")
            record = json.loads((root/"items"/q.work_id(ITEM)/"messages.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(counts["image_messages"], 1)
            self.assertEqual(record["image"]["actual_bytes"], len(raw_bytes))
            self.assertEqual(record["image"]["source"]["size"], 0)
            self.assertTrue(record["image"]["local_path"].endswith(".png"))
            self.assertIsNone(record["text"])
            self.assertEqual(record["ocr"]["result"]["frames"][0]["text"], "图片文字")
            self.assertEqual(ocr.calls, 1)
            self.assertEqual(fetch.call_count, 1)
            q.export_item(root, MANIFEST, ITEM, FakeBrowser([]), ocr, False, "descending")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(ocr.calls, 1)

    def test_path_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(q.ExportError):
                q.checked_path(Path(temporary), "../outside.json")


class OverlapObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = q.read_json(Path(__file__).with_name("fixtures")/"overlap-sanitized.json")["cases"]

    def test_three_observed_shapes_keep_100_unique_messages_and_all_raws(self):
        for case in self.cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
                before, after = copy.deepcopy(case["before"]), copy.deepcopy(case["after"])
                first = [before]+[message("neighbor-%03d"%index, LOWER+200+index) for index in range(1, 100)]
                browser = FakeBrowser([response(first, "true", LOWER+200), response([after], "false", LOWER+200)])
                root = Path(temporary)
                counts = q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
                self.assertEqual(counts["messages"], 100)
                rows = [json.loads(line) for line in (root/"items"/q.work_id(ITEM)/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
                record = next(row for row in rows if row["message_id"] == before["messageId"])
                self.assertEqual(q.json_bytes(record["raw"]), q.json_bytes(before))
                self.assertEqual([q.json_bytes(raw) for raw in record["raw_observations"]], [q.json_bytes(before), q.json_bytes(after)])
                self.assertEqual(record["raw"]["content"], before["content"])

    def test_content_time_and_identity_changes_remain_conflicts(self):
        mutations = (
            ("content", '{"text":"changed body"}'),
            ("sendTime", str(LOWER+201)),
            ("senderId", {"appUid": "different-sender", "domain": "cntaobao"}),
            ("receiverIds", [{"appUid": "different-receiver", "domain": "cntaobao"}]),
            ("cid", {"appCid": "different-conversation", "domain": "cntaobao"}),
            ("unknown_field", "new immutable value"),
        )
        for case in self.cases:
            for field, value in mutations:
                with self.subTest(case=case["name"], field=field):
                    changed = copy.deepcopy(case["after"])
                    changed[field] = value
                    self.assertFalse(q.same_message_observation(case["before"], changed))

    def test_ext_actual_values_and_extmap_remain_conflicts(self):
        for case in self.cases:
            before, changed = copy.deepcopy(case["before"]), copy.deepcopy(case["after"])
            ext = json.loads(changed["ext"])
            ext[next(iter(ext))] = "actual changed extension value"
            changed["ext"] = json.dumps(ext)
            self.assertFalse(q.same_message_observation(before, changed))
            changed = copy.deepcopy(case["after"])
            changed["extMap"]["new_value"] = "must remain checked"
            self.assertFalse(q.same_message_observation(before, changed))

    def test_object_ext_ignores_only_member_order(self):
        before = message("overlap", LOWER)
        before["ext"] = '{"first":{"enabled":true,"count":1},"second":[1,2]}'
        after = copy.deepcopy(before)
        after["ext"] = '{ "second": [1, 2], "first": {"count":1,"enabled":true} }'
        self.assertTrue(q.same_message_observation(before, after))
        after["ext"] = '{"second":[2,1],"first":{"count":1,"enabled":true}}'
        self.assertFalse(q.same_message_observation(before, after))

    def test_invalid_nonobject_and_mixed_ext_are_strict(self):
        pairs = (
            ('{"a":1', '{"a":1 '),
            ('[1,2]', '[1, 2]'),
            ('1', '1.0'),
            ('{"a":1}', {"a": 1}),
            ('{"a":1,"a":2}', '{"a":2}'),
            ('{"a":NaN}', '{"a": NaN}'),
        )
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                before, after = message("overlap", LOWER), message("overlap", LOWER)
                before["ext"], after["ext"] = left, right
                self.assertFalse(q.same_message_observation(before, after))
                self.assertTrue(q.same_message_observation(before, copy.deepcopy(before)))

    def test_ext_boolean_integer_and_float_types_remain_distinct(self):
        for left, right in (('{"flag":true}', '{"flag":1}'), ('{"count":1}', '{"count":1.0}')):
            before, after = message("overlap", LOWER), message("overlap", LOWER)
            before["ext"], after["ext"] = left, right
            self.assertFalse(q.same_message_observation(before, after))

    def test_only_top_level_biz_unique_is_observable(self):
        before, after = message("overlap", LOWER), message("overlap", LOWER)
        before["bizUnique"], after["bizUnique"] = "observation-one", "observation-two"
        self.assertTrue(q.same_message_observation(before, after))
        before["ext"], after["ext"] = '{"nested":{"bizUnique":"one"}}', '{"nested":{"bizUnique":"two"}}'
        self.assertFalse(q.same_message_observation(before, after))

    def test_raw_equality_fast_path_cannot_hide_boolean_number_change(self):
        before, after = message("overlap", LOWER+200), message("overlap", LOWER+200)
        before["immutable_flag"], after["immutable_flag"] = True, 1
        browser = FakeBrowser([response([before, message("later", LOWER+300)], True, LOWER+200), response([after], False)])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            with self.assertRaisesRegex(q.ExportError, "conflicting content"):
                q.export_item(Path(temporary), MANIFEST, ITEM, browser, FakeOCR(), False, "descending")

    def test_raw_observations_keep_boolean_and_number_read_states_separate(self):
        before, after = message("overlap", LOWER+200), message("overlap", LOWER+200)
        before["readStatus"], after["readStatus"] = True, 1
        browser = FakeBrowser([response([before, message("later", LOWER+300)], True, LOWER+200), response([after], False)])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            q.export_item(root, MANIFEST, ITEM, browser, FakeOCR(), False, "descending")
            rows = [json.loads(line) for line in (root/"items"/q.work_id(ITEM)/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
            overlap = next(row for row in rows if row["message_id"] == "overlap")
            states = [raw["readStatus"] for raw in overlap["raw_observations"]]
            self.assertEqual(len(states), 2)
            self.assertIs(type(states[0]), bool)
            self.assertIs(type(states[1]), int)


if __name__ == "__main__":
    unittest.main()
