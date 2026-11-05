"""Offline CLI fixtures and generated JavaScript in a synthetic Node DOM; no API."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import timed_bridge as b


TAB = "A" * 32
NICK = "示例店:测试"
REAL_RUN = subprocess.run


def api_data(response):
    if response.get("ret") != ["SUCCESS::调用成功"]:
        raise RuntimeError("synthetic business failure")
    return response["data"]


Q = SimpleNamespace(MESSAGES=b.MESSAGE_API, api_data=api_data)


def success(data):
    return {"ret": ["SUCCESS::调用成功"], "data": data}


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.url = None
        self.session = None
        self.tab = TAB
        self.mode = None
        self.response = success({"nick": NICK})
        self.mtop_ms = 0
        self.stderr = ""

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if isinstance(self.mode, Exception):
            raise self.mode
        if self.mode == "lost":
            return subprocess.CompletedProcess(command, 1, "SYNTHETIC_PRIVATE_BODY", "disconnected SYNTHETIC_SECRET")
        if self.mode == "wrong_session":
            return subprocess.CompletedProcess(command, 1, "", "target does not belong to session SYNTHETIC_SECRET")
        if self.mode == "guard":
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": False, "error": "BRIDGE_TARGET_MISMATCH SYNTHETIC_SECRET"}), "")
        if self.mode == "malformed":
            return subprocess.CompletedProcess(command, 0, "SYNTHETIC_SECRET not-json", "")
        if isinstance(self.mode, dict):
            return subprocess.CompletedProcess(command, 1, json.dumps(self.mode), "SYNTHETIC_STDERR_SECRET")
        if self.mode == "verification":
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok":True,"data":json.dumps({"error_code":"verification_required","mtop_ms":0})}), "")
        if command[2:5] == ["browser", "tab", "new"]:
            self.url = command[5]
            self.session = command[command.index("--session") + 1]
            value = {"page": self.tab, "url": self.url}
        elif command[2:5] == ["browser", "tab", "list"]:
            value = [{"page": self.tab if self.mode != "missing" else "B" * 32,
                      "url": self.url if self.mode != "wrong_url" else self.url.replace("market.m.taobao.com", "example.test")}]
        elif command[2:4] == ["browser", "eval"]:
            if "performance.now()" in command[4]:
                value = json.dumps({"response": self.response, "mtop_ms": self.mtop_ms}, ensure_ascii=False)
            else:
                parsed = urlsplit(self.url)
                value = {"origin": b.ORIGIN, "path": b.HISTORY_PATH,
                         "markers": {k: v[0] for k, v in parse_qs(parsed.query).items()}}
                if self.mode == "wrong_location":
                    value["path"] = "/different"
        else:
            raise AssertionError("Unexpected synthetic CLI command")
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "data": value}, ensure_ascii=False), self.stderr)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "node.exe").touch()
        (self.root / "main.js").touch()
        self.runtime = {"node": str(self.root / "node.exe"), "opencli_script": str(self.root / "main.js"),
                        "task_root": str(self.root), "secret_extra": "SYNTHETIC_SECRET"}
        self.fake = FakeCLI()
        self.mock_run = self.enterContext(patch.object(b.subprocess, "run", side_effect=self.fake))

    def open(self, run_id="run-1", worker_id="worker-1", **options):
        return b.TimedBrowser.open(self.runtime, run_id, worker_id, NICK, Q, **options)

    def test_bootstrap_uses_only_supplied_runtime_and_unique_context(self):
        first = self.open()
        second = self.open()
        self.assertNotEqual(first.session, second.session)
        self.assertNotEqual(first._markers["bridge_instance"], second._markers["bridge_instance"])
        self.assertEqual(first.runtime_summary["task_root"], str(self.root.resolve()))
        self.assertEqual(first.runtime_summary["bootstrap_cli_calls"], 4)
        self.assertTrue(first.runtime_summary["identity_verified"])
        self.assertEqual(first.expected_nick, NICK)
        self.assertEqual(first.runtime_summary["identity_binding"], "expected_nick")
        self.assertFalse(first.runtime_summary["allow_discovery"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(first.runtime_summary))
        for command, options in self.fake.calls:
            self.assertEqual(command[:2], [self.runtime["node"], self.runtime["opencli_script"]])
            self.assertFalse(options["check"])
            self.assertEqual(options["timeout"], 120)
            self.assertEqual(command[-2:], ["--window", "background"])

    def test_runtime_requires_explicit_task_root(self):
        del self.runtime["task_root"]
        with self.assertRaises(b.BridgeError) as error:
            self.open()
        self.assertEqual(error.exception.code, "invalid_runtime")
        self.mock_run.assert_not_called()

    def test_official_default_hash_route_is_allowed_but_other_routes_are_not(self):
        bridge = self.open()
        self.assertTrue(bridge._matches_url(bridge._url+'#/'))
        self.assertTrue(bridge._matches_url(bridge._url))
        self.assertFalse(bridge._matches_url(bridge._url+'#/other'))
        self.assertFalse(bridge._matches_url(bridge._url+'&extra=1#/'))

    def test_registered_targets_rejected_before_any_eval(self):
        for relative, record in (
            ("codex-runtime.json", {"tab": TAB}),
            ("kimi-runtime.json", {"opencli": {"target": TAB}}),
            ("parallel/codex-01/runtime.json", {"tab": TAB.lower()}),
            ("batches/b1/parallel/kimi-01/runtime.json", {"tab": TAB}),
            ("unified/runs/r1/workers/w1/runtime.json", {"runtime_summary": {"tab": TAB}}),
        ):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record), encoding="utf-8")
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as error:
                    self.open()
                self.assertEqual(error.exception.code, "target_registered")
                self.assertEqual(len(self.fake.calls) - before, 1)
                path.unlink()

    def test_bad_registry_fails_before_browser_creation(self):
        (self.root / "codex-runtime.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(b.BridgeError) as error:
            self.open()
        self.assertEqual(error.exception.code, "registry_invalid")
        self.mock_run.assert_not_called()

    def retire(self, relative="codex-runtime.json", record=None):
        record = record or {"tab": TAB.lower(), "session": "retired-session"}
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        nodes = [record] + [record[k] for k in ("runtime_summary", "opencli") if isinstance(record.get(k), dict)]
        item = {"runtime_path": relative, "runtime_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "targets": sorted({node[k].upper() for node in nodes for k in ("tab", "target") if node.get(k)}),
                "sessions": sorted({node["session"] for node in nodes if node.get("session")})}
        value = {"schema": 1, "contexts": [item], "evidence": {"operator": "synthetic"}}
        self.write_retirement(value)
        return value

    def write_retirement(self, value):
        (self.root / "retired-browser-contexts.json").write_text(json.dumps(value), encoding="utf-8")

    def test_exact_retirement_allows_target_but_preserves_all_sessions(self):
        self.retire(record={"tab": TAB.lower(), "session": "old-top",
                            "runtime_summary": {"target": "B" * 32, "session": "old-nested"},
                            "opencli": {"tab": TAB, "session": "old-opencli"}})
        original = (self.root / "codex-runtime.json").read_bytes()
        targets, sessions = b._registered(self.root)
        self.assertEqual(targets, set())
        self.assertEqual(sessions, {"old-top", "old-nested", "old-opencli"})
        self.assertTrue(self.open().runtime_summary["identity_verified"])
        self.assertEqual((self.root / "codex-runtime.json").read_bytes(), original)

    def test_same_target_in_unretired_new_registration_remains_protected(self):
        self.retire()
        path = self.root / "unified/runs/new/worker/runtime.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"runtime_summary": {"tab": TAB, "session": "new-owner"}}), encoding="utf-8")
        with self.assertRaises(b.BridgeError) as error:
            self.open()
        self.assertEqual(error.exception.code, "target_registered")
        self.assertEqual(len(self.fake.calls), 1)

    def test_retired_session_cannot_be_reused(self):
        marker = "a" * 32
        self.retire(record={"tab": TAB, "session": "qn-unified-" + marker})
        with patch.object(b.uuid, "uuid4", return_value=SimpleNamespace(hex=marker)):
            with self.assertRaises(b.BridgeError) as error:
                self.open()
        self.assertEqual(error.exception.code, "session_registered")
        self.mock_run.assert_not_called()

    def test_registration_added_during_new_tab_is_still_protected(self):
        self.retire()

        def add_registration(command, **kwargs):
            result = self.fake(command, **kwargs)
            path = self.root / "parallel/new/runtime.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"tab": TAB, "session": "new-owner"}), encoding="utf-8")
            return result

        self.mock_run.side_effect = add_registration
        with self.assertRaises(b.BridgeError) as error:
            self.open()
        self.assertEqual(error.exception.code, "target_registered")
        self.assertEqual(len(self.fake.calls), 1)

    def test_retired_file_hash_or_complete_context_set_change_stops_before_cli(self):
        for change in ("hash", "target", "session", "missing_target", "missing_session", "missing_file"):
            with self.subTest(change=change):
                value = self.retire()
                item = value["contexts"][0]
                if change == "hash":
                    path = self.root / "codex-runtime.json"
                    path.write_bytes(path.read_bytes() + b"\n")
                elif change == "missing_file":
                    (self.root / "codex-runtime.json").unlink()
                else:
                    field = "targets" if "target" in change else "sessions"
                    item[field] = [] if change.startswith("missing") else ["B" * 32 if field == "targets" else "wrong-session"]
                    self.write_retirement(value)
                with self.assertRaises(b.BridgeError) as error:
                    self.open()
                self.assertEqual(error.exception.code, "registry_invalid")
        self.mock_run.assert_not_called()

    def test_retirement_paths_must_be_canonical_known_registrations(self):
        for relative in ("../codex-runtime.json", "D:/outside/runtime.json", "/runtime.json",
                         "parallel\\x\\runtime.json", "./codex-runtime.json", "parallel//x/runtime.json",
                         "not-a-registration.json"):
            with self.subTest(relative=relative):
                value = self.retire()
                value["contexts"][0]["runtime_path"] = relative
                self.write_retirement(value)
                with self.assertRaises(b.BridgeError) as error:
                    self.open()
                self.assertEqual(error.exception.code, "registry_invalid")
        self.mock_run.assert_not_called()

    def test_invalid_retirement_is_not_ignored(self):
        for invalid in ("schema", "extra_field", "contexts_type", "duplicate_path", "duplicate_target",
                        "lowercase_target", "bad_hash", "missing_field", "evidence_type"):
            with self.subTest(invalid=invalid):
                value = self.retire()
                item = value["contexts"][0]
                if invalid == "schema": value["schema"] = True
                elif invalid == "extra_field": value["allow_all"] = True
                elif invalid == "contexts_type": value["contexts"] = {}
                elif invalid == "duplicate_path": value["contexts"].append(dict(item))
                elif invalid == "duplicate_target": item["targets"] *= 2
                elif invalid == "lowercase_target": item["targets"] = [TAB.lower()]
                elif invalid == "bad_hash": item["runtime_sha256"] = "bad"
                elif invalid == "missing_field": del item["sessions"]
                elif invalid == "evidence_type": value["evidence"] = []
                self.write_retirement(value)
                with self.assertRaises(b.BridgeError) as error:
                    self.open()
                self.assertEqual(error.exception.code, "registry_invalid")
        for raw in ('not-json', '{"schema":1,"schema":1,"contexts":[]}', '{"schema":1,"contexts":[],"evidence":{"n":NaN}}'):
            (self.root / "retired-browser-contexts.json").write_text(raw, encoding="utf-8")
            with self.assertRaises(b.BridgeError) as error:
                self.open()
            self.assertEqual(error.exception.code, "registry_invalid")
        self.mock_run.assert_not_called()

    def test_target_membership_and_location_checked_before_identity(self):
        for mode, calls in (("missing", 2), ("wrong_url", 2), ("wrong_location", 3)):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as error:
                    self.open()
                self.assertEqual(error.exception.code, "target_mismatch")
                self.assertEqual(len(self.fake.calls) - before, calls)
                self.assertTrue(error.exception.worker_unusable)

    def test_identity_mismatch(self):
        self.fake.response = success({"nick": "其他测试账号"})
        with self.assertRaises(b.BridgeError) as error:
            self.open()
        self.assertEqual(error.exception.safe(), {"code": "identity_mismatch", "worker_unusable": True})
        self.assertEqual(len(self.fake.calls), 4)

    def test_current_identity_binding_requires_opt_in_and_records_bound_login(self):
        with self.assertRaises(b.BridgeError) as error:
            b.TimedBrowser.open(self.runtime, "run-1", "worker-1", None, Q)
        self.assertEqual(error.exception.code, "invalid_identity")
        self.mock_run.assert_not_called()
        browser = b.TimedBrowser.open(self.runtime, "run-1", "worker-1", None, Q, bind_current_identity=True)
        self.assertEqual(browser.expected_nick, NICK)
        self.assertEqual(browser.runtime_summary["expected_nick"], NICK)
        self.assertEqual(browser.runtime_summary["identity_binding"], "current_login")
        self.assertEqual(len(self.fake.calls), 4)

    def test_explicit_expected_identity_remains_exact_with_binding_enabled(self):
        browser = self.open(bind_current_identity=True)
        self.assertEqual(browser.expected_nick, NICK)
        self.assertEqual(browser.runtime_summary["identity_binding"], "expected_nick")
        for actual in ("其他测试账号", " " + NICK, NICK + " "):
            with self.subTest(actual=actual):
                self.fake.response = success({"nick": actual})
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as error:
                    self.open(bind_current_identity=True)
                self.assertEqual(error.exception.code, "identity_mismatch")
                self.assertEqual(len(self.fake.calls) - before, 4)

    def test_explicit_invalid_identity_cannot_be_replaced_by_current_login(self):
        for expected in ("", "  ", 1, False):
            with self.subTest(expected=expected), self.assertRaises(b.BridgeError) as error:
                b.TimedBrowser.open(self.runtime, "run-1", "worker-1", expected, Q, bind_current_identity=True)
            self.assertEqual(error.exception.code, "invalid_identity")
        self.mock_run.assert_not_called()

    def test_current_identity_binding_rejects_missing_empty_or_non_string_nick(self):
        for identity in ({}, {"nick": None}, {"nick": ""}, {"nick": "  "}, {"nick": 1}, []):
            with self.subTest(identity=identity):
                self.fake.response = success(identity)
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as error:
                    b.TimedBrowser.open(self.runtime, "run-1", "worker-1", None, Q, bind_current_identity=True)
                self.assertEqual(error.exception.code, "identity_mismatch")
                self.assertTrue(error.exception.worker_unusable)
                self.assertEqual(len(self.fake.calls) - before, 4)

    def test_identity_and_discovery_flags_require_boolean_keyword_options(self):
        for option in ("bind_current_identity", "allow_discovery"):
            for value in (1, "true", None):
                with self.subTest(option=option, value=value), self.assertRaises(b.BridgeError) as error:
                    self.open(**{option: value})
                self.assertEqual(error.exception.code, "invalid_runtime")
        with self.assertRaises(TypeError):
            b.TimedBrowser.open(self.runtime, "run-1", "worker-1", NICK, Q, True)
        self.mock_run.assert_not_called()

    def test_request_preserves_response_and_measures_combined_overhead(self):
        browser = self.open()
        self.fake.response = success({"userMessages": [{"content": "测试正文"}], "nextCursor": 123})
        self.fake.mtop_ms = 25
        with patch.object(b.time, "perf_counter", side_effect=[10.0, 10.08, 10.1]):
            result = browser.request(Q.MESSAGES, {"cid": "synthetic-cid"})
        self.assertEqual(result, self.fake.response)
        self.assertEqual(len(self.fake.calls), 5)
        metrics = browser.last_metrics
        self.assertEqual(metrics["total_ms"], 100.0)
        self.assertEqual(metrics["mtop_ms"], 25.0)
        self.assertEqual(metrics["non_api_overhead_ms"], 75.0)
        self.assertEqual(metrics["returncode"], 0)
        self.assertEqual(metrics["response_bytes"], len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        self.assertTrue(metrics["timing_available"])
        self.assertNotIn("测试正文", json.dumps(metrics, ensure_ascii=False))

    def test_business_failure_is_returned_intact_and_worker_remains_usable(self):
        browser = self.open()
        self.fake.response = {"ret": ["FAIL_BIZ_CONTEXT::synthetic"], "data": {"detail": "synthetic business body"}, "success": False}
        self.assertEqual(browser.request(Q.MESSAGES, {}), self.fake.response)
        self.assertTrue(browser.usable)
        self.assertEqual(browser.last_metrics["status"], "response")

    def test_auth_failure_is_controlled_and_stops_further_calls(self):
        for ret in ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_USER_VALIDATE"):
            with self.subTest(ret=ret):
                self.fake.response = success({"nick": NICK})
                browser = self.open()
                self.fake.response = {"ret": [ret + "::SYNTHETIC_SECRET"], "data": {"secret": "SYNTHETIC_SECRET"}}
                with self.assertRaises(b.BridgeError) as error:
                    browser.request(Q.MESSAGES, {})
                self.assertEqual(error.exception.code, "auth_required")
                self.assertTrue(browser.last_metrics["timing_available"])
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as blocked:
                    browser.request(Q.MESSAGES, {})
                self.assertEqual(blocked.exception.code, "worker_unusable")
                self.assertEqual(before, len(self.fake.calls))
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception.safe()) + str(browser.last_metrics))

    def test_cli_failures_are_private_fatal_and_not_retried(self):
        for mode, code in (("lost", "cli_failed"), ("wrong_session", "target_mismatch"),
                           ("guard", "target_mismatch"), ("malformed", "protocol_invalid"),
                           (subprocess.TimeoutExpired("SYNTHETIC_SECRET", 120, output=b"SYNTHETIC_SECRET"), "cli_timeout"),
                           (FileNotFoundError("SYNTHETIC_SECRET"), "cli_unavailable")):
            with self.subTest(code=code):
                self.fake.mode = None
                browser = self.open()
                self.fake.mode = mode
                before = len(self.fake.calls)
                with self.assertRaises(b.BridgeError) as error:
                    browser.request(Q.MESSAGES, {})
                self.assertEqual(error.exception.code, code)
                self.assertFalse(browser.usable)
                self.assertEqual(len(self.fake.calls) - before, 1)
                self.assertFalse(browser.last_metrics["timing_available"])
                self.assertIsNone(browser.last_metrics["mtop_ms"])
                self.assertIsNone(browser.last_metrics["non_api_overhead_ms"])
                safe = json.dumps({"error": error.exception.safe(), "metrics": browser.last_metrics, "runtime": browser.runtime_summary})
                self.assertNotIn("SYNTHETIC_SECRET", safe)
                self.assertNotIn("SYNTHETIC_PRIVATE_BODY", safe)

    def test_official_failure_envelope_keeps_only_finite_fields(self):
        for code in b._CLI_ERROR_CODES:
            with self.subTest(code=code):
                self.fake.mode=None;browser=self.open()
                self.fake.mode={"ok":False,"error":{"code":code,"outcome":"unknown","exitCode":75,
                    "message":"SYNTHETIC_SECRET","hint":"SYNTHETIC_SECRET","cause":{"secret":"SYNTHETIC_SECRET"}},
                    "trace":{"path":"SYNTHETIC_SECRET"}}
                with self.assertRaises(b.BridgeError):browser.request(Q.MESSAGES,{})
                self.assertEqual(browser.last_metrics["cli_error"],{"error_code":code,"status":"unknown",
                    "outcome_known":True,"exit_code":75,"exit_code_unknown":False})
                self.assertNotIn("SYNTHETIC",json.dumps(browser.last_metrics))
        for envelope in ({"ok":False,"error":{"code":"SYNTHETIC_SECRET","outcome":"SYNTHETIC_SECRET","exitCode":True}},
                         {"ok":False,"error":{"exitCode":"75"}},
                         {"ok":False,"error":{"exitCode":999999}},
                         {"ok":False,"error":"SYNTHETIC_SECRET"},
                         {"ok":True,"error":{"code":"TIMEOUT","exitCode":75}}):
            with self.subTest(envelope=envelope):
                self.fake.mode=None;browser=self.open();self.fake.mode=envelope
                with self.assertRaises(b.BridgeError):browser.request(Q.MESSAGES,{})
                self.assertEqual(browser.last_metrics["cli_error"],{"error_code":"unknown","status":"unknown",
                    "outcome_known":False,"exit_code":None,"exit_code_unknown":True})

    def test_verification_result_is_fatal_and_stops_further_cli_calls(self):
        browser=self.open();self.fake.mode="verification"
        with self.assertRaises(b.BridgeError) as error:browser.request(Q.MESSAGES,{})
        self.assertEqual(error.exception.code,"verification_required")
        self.assertEqual(browser.last_metrics["error_code"],"verification_required")
        self.assertFalse(browser.last_metrics["timing_available"])
        before=len(self.fake.calls)
        with self.assertRaises(b.BridgeError):browser.request(Q.MESSAGES,{})
        self.assertEqual(len(self.fake.calls),before)

    def test_whitelist_and_invalid_data_reject_without_cli_and_keep_worker(self):
        browser = self.open()
        before = len(self.fake.calls)
        for api, data, code in (("mtop.send", {}, "api_not_allowed"),
                               ("mtop.taobao.wireless.amp2.paas.conversation.list", {}, "api_not_allowed"),
                               (Q.MESSAGES, [], "invalid_request"),
                               (Q.MESSAGES, {"x": float("nan")}, "invalid_request")):
            with self.subTest(api=api, code=code), self.assertRaises(b.BridgeError) as error:
                browser.request(api, data)
            self.assertEqual(error.exception.code, code)
            self.assertFalse(error.exception.worker_unusable)
        self.assertTrue(browser.usable)
        self.assertEqual(len(self.fake.calls), before)

    def test_discovery_permission_adds_only_the_conversation_list_api(self):
        browser = self.open(allow_discovery=True)
        self.assertTrue(browser.runtime_summary["allow_discovery"])
        self.fake.response = success({"conversations": []})
        self.assertEqual(browser.request(b.CONVERSATION_API, {"userNick": ""}), self.fake.response)
        self.assertEqual(browser.request(b.IDENTITY_API, {}), self.fake.response)
        before = len(self.fake.calls)
        for api in ("mtop.send", "mtop.taobao.wireless.amp2.paas.conversation.delete", b.CONVERSATION_API + ".other"):
            with self.subTest(api=api), self.assertRaises(b.BridgeError) as error:
                browser.request(api, {})
            self.assertEqual(error.exception.code, "api_not_allowed")
            self.assertFalse(error.exception.worker_unusable)
        self.assertEqual(len(self.fake.calls), before)
        self.assertTrue(browser.usable)

    def test_discovery_verification_stops_all_later_requests_without_retry(self):
        browser = self.open(allow_discovery=True)
        self.fake.mode = "verification"
        before = len(self.fake.calls)
        with self.assertRaises(b.BridgeError) as error:
            browser.request(b.CONVERSATION_API, {})
        self.assertEqual(error.exception.code, "verification_required")
        self.assertFalse(browser.usable)
        self.assertEqual(len(self.fake.calls), before + 1)
        for api in (b.IDENTITY_API, b.MESSAGE_API, b.CONVERSATION_API):
            with self.subTest(api=api), self.assertRaises(b.BridgeError) as blocked:
                browser.request(api, {})
            self.assertEqual(blocked.exception.code, "worker_unusable")
        self.assertEqual(len(self.fake.calls), before + 1)

    def test_js_only_calls_mtop_and_reads_own_route_markers(self):
        browser = self.open(run_id="REQUEST", worker_id="GUARD")
        browser.request(Q.MESSAGES, {"cid": "GUARD REQUEST"})
        js = self.fake.calls[-1][0][4]
        self.assertIn('"bridge_run":"REQUEST"', js)
        self.assertIn('"bridge_worker":"GUARD"', js)
        self.assertIn('"cid":"GUARD REQUEST"', js)
        self.assertIn("performance.now()", js)
        self.assertIn("Promise.race([operation,verification])", js)
        for command, _ in self.fake.calls:
            if command[2:4] == ["browser", "eval"]:
                for forbidden in ("cookie", "localstorage", "sessionstorage", "innerhtml", "textcontent", "contentwindow", "contentdocument"):
                    self.assertNotIn(forbidden, command[4].lower())
        location_js = self.fake.calls[2][0][4]
        self.assertNotIn("Object.fromEntries(u.searchParams)", location_js)

    def run_generated_script(self,mode,api=Q.MESSAGES,challenge_api=b.MESSAGE_API):
        browser=self.open(allow_discovery=api==b.CONVERSATION_API);browser.request(api,{"cid":"synthetic-cid"})
        script=self.fake.calls[-1][0][4]
        node=Path(os.environ["QIANNIU_TEST_NODE"])
        self.assertTrue(node.is_absolute() and node.is_file(),"QIANNIU_TEST_NODE must name an existing absolute Node executable")
        result=REAL_RUN([str(node),"-e",SYNTHETIC_DOM],input=json.dumps({"script":script,"url":browser._url,"mode":mode,"challenge_api":challenge_api}),
            capture_output=True,text=True,encoding="utf-8",timeout=5,check=True,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return json.loads(result.stdout)

    def test_existing_verification_iframe_prevents_api_and_reads_no_frame_content(self):
        result=self.run_generated_script("existing")
        self.assertEqual(result["payload"]["error_code"],"verification_required")
        self.assertEqual((result["api_calls"],result["observers"],result["active_observers"]),(0,0,0))

    def test_verification_added_or_src_changed_during_await_wins_and_disconnects(self):
        for mode in ("added","src_changed"):
            with self.subTest(mode=mode):
                result=self.run_generated_script(mode)
                self.assertEqual(result["payload"]["error_code"],"verification_required")
                self.assertEqual((result["api_calls"],result["observers"],result["disconnects"],result["active_observers"]),(1,1,1,0))
                self.assertFalse(result["original_request_settled"])

    def test_generated_script_preserves_normal_ret_errors_and_ignores_other_frames(self):
        for mode in ("normal","ret_error","unrelated_path","untrusted_host","api_throw"):
            with self.subTest(mode=mode):
                result=self.run_generated_script(mode)
                self.assertEqual((result["api_calls"],result["observers"],result["disconnects"],result["active_observers"]),(1,1,1,0))
                if mode=="api_throw":self.assertEqual(result["error"],"BRIDGE_MTOP_FAILURE")
                else:self.assertEqual(result["payload"]["response"],{"ret":["FAIL_BIZ_SYNTHETIC::synthetic"]} if mode=="ret_error" else {"ret":["SUCCESS::synthetic"],"data":{"ok":True}})

    def test_current_api_challenge_is_detected_before_and_during_each_allowed_request(self):
        for api in (b.IDENTITY_API, b.CONVERSATION_API):
            for mode in ("existing", "added", "src_changed"):
                with self.subTest(api=api, mode=mode):
                    result = self.run_generated_script(mode, api=api, challenge_api=api)
                    self.assertEqual(result["payload"]["error_code"], "verification_required")
                    self.assertEqual(result["api_calls"], 0 if mode == "existing" else 1)
                    self.assertEqual(result["active_observers"], 0)
                    self.assertFalse(result["original_request_settled"])

    def test_discovery_keeps_message_challenge_detection_and_ignores_unrelated_frames(self):
        result = self.run_generated_script("existing", api=b.CONVERSATION_API)
        self.assertEqual(result["payload"]["error_code"], "verification_required")
        self.assertEqual(result["api_calls"], 0)
        for mode in ("normal", "unrelated_path", "untrusted_host"):
            with self.subTest(mode=mode):
                result = self.run_generated_script(mode, api=b.CONVERSATION_API, challenge_api=b.CONVERSATION_API)
                self.assertEqual(result["payload"]["response"]["ret"], ["SUCCESS::synthetic"])
                self.assertEqual((result["api_calls"], result["active_observers"]), (1, 0))

    def test_invalid_timing_is_not_presented_as_measurement(self):
        for timing in (-1, True, 999999, "not-a-number"):
            with self.subTest(timing=timing):
                self.fake.mtop_ms = 0
                browser = self.open()
                self.fake.mtop_ms = timing
                with self.assertRaises(b.BridgeError) as error:
                    browser.request(Q.MESSAGES, {})
                self.assertEqual(error.exception.code, "timing_invalid")
                self.assertFalse(browser.last_metrics["timing_available"])

    def test_instance_cannot_move_to_another_python_process(self):
        browser = self.open()
        before = len(self.fake.calls)
        with patch.object(b.os, "getpid", return_value=browser._owner_pid + 1):
            with self.assertRaises(b.BridgeError) as error:
                browser.request(Q.MESSAGES, {})
        self.assertEqual(error.exception.code, "process_mismatch")
        self.assertEqual(len(self.fake.calls), before)


SYNTHETIC_DOM = r"""
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const route='/h5/'+input.challenge_api+'/1.0/_____tmd_____/punish';
const challenge='https://h5api.m.taobao.com'+route+'?ignored_private_value=synthetic';
let frames=[],apiCalls=0,observers=[],disconnects=0,settled=false,tick=0;
function frame(src){return {src,getAttribute(name){if(name!=='src')throw Error('unexpected_attribute');return this.src;},
 get textContent(){throw Error('forbidden_frame_text');},get contentWindow(){throw Error('forbidden_frame_window');}};}
function notify(){queueMicrotask(()=>{for(const observer of observers)if(observer.active)observer.callback([]);});}
class Observer{
 constructor(callback){this.callback=callback;this.active=false;observers.push(this);}
 observe(root,options){if(root!==document.documentElement||!options.subtree||!options.childList||!options.attributes||JSON.stringify(options.attributeFilter)!=='["src"]')throw Error('invalid_observer');this.active=true;}
 disconnect(){this.active=false;disconnects++;}
}
const document={documentElement:{},querySelectorAll(selector){if(selector!=='iframe[src]')throw Error('unexpected_dom_read');return frames;}};
if(input.mode==='existing')frames=[frame(challenge)];
if(input.mode==='src_changed')frames=[frame('about:blank')];
if(input.mode==='unrelated_path')frames=[frame('https://h5api.m.taobao.com/h5/other/1.0/_____tmd_____/punish')];
if(input.mode==='untrusted_host')frames=[frame('https://taobao.com.example.invalid'+route)];
const request=()=>{apiCalls++;
 if(input.mode==='added'||input.mode==='src_changed'){
   queueMicrotask(()=>{if(input.mode==='added')frames.push(frame(challenge));else frames[0].src=challenge;notify();});
   return new Promise(()=>{});
 }
 settled=true;
 if(input.mode==='ret_error')return Promise.reject({ret:['FAIL_BIZ_SYNTHETIC::synthetic']});
 if(input.mode==='api_throw')throw Error('private_sdk_error');
 return Promise.resolve({ret:['SUCCESS::synthetic'],data:{ok:true}});
};
const sandbox={URL,location:{href:input.url},document,MutationObserver:Observer,
 performance:{now:()=>++tick},window:{lib:{mtop:{request}}}};
const timer=setTimeout(()=>{process.stderr.write('synthetic_timeout');process.exit(2);},1000);
(async()=>{
 let result;
 try{result={payload:JSON.parse(await vm.runInNewContext(input.script,sandbox))};}
 catch(error){result={error:error.message};}
 clearTimeout(timer);
 process.stdout.write(JSON.stringify({...result,api_calls:apiCalls,observers:observers.length,disconnects,
   active_observers:observers.filter(value=>value.active).length,original_request_settled:settled}));
})();
"""


if __name__ == "__main__":
    unittest.main()
