"""Measured, process-local OpenCLI bridge; no retries or browser reconnection.

Each request launches one Node/OpenCLI process. non_api_overhead_ms is the
combined process startup, bridge, serialization and Python overhead, NOT a
measurement of Node startup alone. response_bytes is the UTF-8 length of the
decoded response re-encoded as compact JSON, not network traffic size.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid


IDENTITY_API = "mtop.user.getUserSimple"
MESSAGE_API = "mtop.taobao.wireless.amp2.im.paas.message.list"
CONVERSATION_API = "mtop.taobao.wireless.amp2.paas.conversation.list"
ORIGIN = "https://market.m.taobao.com"
HISTORY_PATH = "/app/qn/im-history-search/index.html"
TIMEOUT_SECONDS = 120
_CODES = {
    "invalid_runtime", "invalid_identity", "invalid_request", "api_not_allowed",
    "registry_invalid", "target_registered", "session_registered", "target_mismatch",
    "cli_unavailable", "cli_timeout", "cli_failed", "protocol_invalid",
    "mtop_not_ready", "mtop_failed", "timing_invalid", "auth_required",
    "identity_mismatch", "identity_failed", "worker_unusable", "process_mismatch", "verification_required",
}
# Installed OpenCLI errors.js, command-output.js and browser/daemon error codes.
# Keep this finite: an arbitrary error.code may contain private diagnostic text.
_CLI_ERROR_CODES = frozenset((
    "cdp_timeout", "detached_mid_command", "command_result_unknown", "command_lost",
    "result_evicted", "outcome_unknown", "target_navigated", "attach_failed", "tab_gone",
    "profile_disconnected", "profile_required", "extension_not_connected", "daemon_shutting_down",
    "max_attempts_exhausted", "javascript_dialog_open", "bound_tab_mismatch",
    "bound_tab_not_debuggable", "bound_tab_gone", "BROWSER_CONNECT", "COMMAND_EXEC", "CONFIG",
    "AUTH_REQUIRED", "TIMEOUT", "ARGUMENT", "SESSION_BUSY", "UNKNOWN", "usage_error",
))
_CLI_EXIT_CODES = frozenset((1, 2, 66, 69, 75, 77, 78, 130))


def _cli_error_fields(envelope):
    error = envelope.get("error") if isinstance(envelope, dict) and envelope.get("ok") is False else None
    error = error if isinstance(error, dict) else {}
    code, outcome, exit_code = error.get("code"), error.get("outcome"), error.get("exitCode")
    known_exit = type(exit_code) is int and exit_code in _CLI_EXIT_CODES
    # status stores the official outcome in the existing safe metrics vocabulary.
    return {"error_code": code if isinstance(code, str) and code in _CLI_ERROR_CODES else "unknown",
            "status": "unknown", "outcome_known": outcome == "unknown",
            "exit_code": exit_code if known_exit else None, "exit_code_unknown": not known_exit}


class BridgeError(RuntimeError):
    """Only controlled codes are exposed; raw CLI diagnostics are discarded."""

    def __init__(self, code, worker_unusable=True):
        if code not in _CODES:
            code = "protocol_invalid"
        self.code = code
        self.worker_unusable = bool(worker_unusable)
        super().__init__(code)

    def safe(self):
        return {"code": self.code, "worker_unusable": self.worker_unusable}


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _loads(value):
    def reject(value):
        raise ValueError("nonfinite_json")
    return json.loads(value, parse_constant=reject)


def _byte_count(value):
    if isinstance(value, bytes):
        return len(value)
    return len(value.encode("utf-8", errors="replace")) if isinstance(value, str) else 0


def _diagnostic_code(value):
    """Inspect error diagnostics solely to choose a fixed safe code."""
    text = value.lower()
    for marker, code in (
        ("bridge_target_mismatch", "target_mismatch"),
        ("does not belong to session", "target_mismatch"),
        ("bridge_mtop_not_ready", "mtop_not_ready"),
        ("bridge_mtop_failure", "mtop_failed"),
    ):
        if marker in text:
            return code
    return "cli_failed"


def _retired_contexts(root):
    """Only an explicit, exact file snapshot can retire a target registration."""
    path = root / "retired-browser-contexts.json"
    if not path.exists():
        return {}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError

    try:
        path.resolve().relative_to(root)
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
        if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
                or not {"schema", "contexts"} <= set(value) <= {"schema", "contexts", "evidence"}
                or not isinstance(value["contexts"], list) or not isinstance(value.get("evidence", {}), dict)):
            raise ValueError
        retired = {}
        for item in value["contexts"]:
            if not isinstance(item, dict) or set(item) != {"runtime_path", "runtime_sha256", "targets", "sessions"}:
                raise ValueError
            relative = item["runtime_path"]
            if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                    or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
                    or PurePosixPath(relative).as_posix() != relative or relative in retired):
                raise ValueError
            (root / relative).resolve().relative_to(root)
            if not isinstance(item["runtime_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["runtime_sha256"]):
                raise ValueError
            for field in ("targets", "sessions"):
                values = item[field]
                if (not isinstance(values, list) or not all(isinstance(v, str) and v for v in values)
                        or len(values) != len(set(values))):
                    raise ValueError
            if not item["targets"] or not all(re.fullmatch(r"[0-9A-F]{32}", v) for v in item["targets"]):
                raise ValueError
            retired[relative] = item
        return retired
    except (OSError, ValueError, TypeError, UnicodeError):
        raise BridgeError("registry_invalid") from None


def _registered(root):
    """Protect every registration except explicitly retired, unchanged files."""
    root = root.resolve()
    retired = _retired_contexts(root)
    paths = [root / "codex-runtime.json", root / "kimi-runtime.json"]
    for pattern in ("parallel/*/runtime.json", "batches/*/parallel/*/runtime.json",
                    "unified/runs/**/runtime.json"):
        paths.extend(root.glob(pattern))
    targets, sessions, matched = set(), set(), set()
    for path in paths:
        if not path.exists():
            continue
        try:
            path.resolve().relative_to(root)
            raw = path.read_bytes()
            record = _loads(raw.decode("utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError
            records = [record]
            for field in ("runtime_summary", "opencli"):
                nested = record.get(field)
                if isinstance(nested, dict):
                    records.append(nested)
            file_targets, file_sessions = set(), set()
            for item in records:
                for field in ("tab", "target"):
                    if isinstance(item.get(field), str) and item[field]:
                        file_targets.add(item[field].upper())
                if isinstance(item.get("session"), str) and item["session"]:
                    file_sessions.add(item["session"])
            relative = path.relative_to(root).as_posix()
            snapshot = retired.get(relative)
            if snapshot is not None:
                if (hashlib.sha256(raw).hexdigest() != snapshot["runtime_sha256"]
                        or file_targets != set(snapshot["targets"]) or file_sessions != set(snapshot["sessions"])):
                    raise ValueError
                matched.add(relative)
            else:
                targets.update(file_targets)
            sessions.update(file_sessions)
        except (OSError, ValueError, UnicodeError):
            raise BridgeError("registry_invalid") from None
    if matched != set(retired):
        raise BridgeError("registry_invalid")
    return targets, sessions


class TimedBrowser:
    @classmethod
    def open(cls, runtime, run_id, worker_id, expected_nick, q, *, bind_current_identity=False, allow_discovery=False):
        """Create and verify one new context; caller persists runtime_summary.

        runtime requires absolute node/opencli_script files and a task_root
        directory. No environment lookup or installation-path inference occurs.
        Current-login binding requires explicit opt-in and expected_nick=None.
        Conversation-list reads require the separate allow_discovery permission.
        """
        self = cls()
        self.usable = False
        self.last_metrics = {}
        self.runtime_summary = {}
        self._cli_calls = 0
        self._owner_pid = os.getpid()
        if not isinstance(runtime, dict) or type(bind_current_identity) is not bool or type(allow_discovery) is not bool:
            raise BridgeError("invalid_runtime")
        self._allow_discovery = allow_discovery
        try:
            paths = {key: Path(runtime[key]) for key in ("node", "opencli_script", "task_root")}
            if not all(path.is_absolute() for path in paths.values()):
                raise ValueError
            if not all(paths[key].is_file() for key in ("node", "opencli_script")) or not paths["task_root"].is_dir():
                raise ValueError
            self._root = paths["task_root"].resolve()
            self._command = [str(paths["node"].resolve()), str(paths["opencli_script"].resolve())]
            if getattr(q, "MESSAGES", None) != MESSAGE_API:
                raise ValueError
            if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
                       for value in (run_id, worker_id)):
                raise ValueError
        except (KeyError, TypeError, ValueError, OSError):
            raise BridgeError("invalid_runtime") from None
        bind_current = bind_current_identity and expected_nick is None
        if not bind_current and (not isinstance(expected_nick, str) or not expected_nick.strip()):
            raise BridgeError("invalid_identity")
        self._markers = {"bridge_run": run_id, "bridge_worker": worker_id, "bridge_instance": uuid.uuid4().hex}
        self.session = "qn-unified-" + self._markers["bridge_instance"]
        self._url = ORIGIN + HISTORY_PATH + "?" + urlencode(self._markers)
        targets, sessions = _registered(self._root)
        if self.session in sessions:
            raise BridgeError("session_registered")
        begin = time.perf_counter()
        self._reset_metrics()
        created = self._cli(["browser", "tab", "new", self._url, *self._session_args()])
        self.tab = created.get("page") if isinstance(created, dict) else None
        if not isinstance(self.tab, str) or not re.fullmatch(r"[0-9A-Fa-f]{32}", self.tab):
            raise BridgeError("protocol_invalid")
        # Refresh registrations after creation; a registered target is never probed.
        targets.update(_registered(self._root)[0])
        if self.tab.upper() in targets:
            raise BridgeError("target_registered")
        listed = self._cli(["browser", "tab", "list", *self._session_args()])
        if not isinstance(listed, list):
            raise BridgeError("protocol_invalid")
        matches = [item for item in listed if isinstance(item, dict) and item.get("page") == self.tab]
        if len(matches) != 1 or not self._matches_url(matches[0].get("url")):
            raise BridgeError("target_mismatch")
        location_js = ("(()=>{const u=new URL(location.href);return {origin:u.origin,path:u.pathname,markers:"
                       "Object.fromEntries(" + _json(list(self._markers)) + ".map(k=>[k,u.searchParams.get(k)]))};})()")
        location = self._cli(self._eval_args(location_js))
        if isinstance(location, str):
            try:
                location = _loads(location)
            except (TypeError, ValueError):
                raise BridgeError("protocol_invalid") from None
        if location != {"origin": ORIGIN, "path": HISTORY_PATH, "markers": self._markers}:
            raise BridgeError("target_mismatch")
        self.usable = True
        response = self.request(IDENTITY_API, {})
        try:
            identity = q.api_data(response)
        except Exception:
            self.usable = False
            raise BridgeError("identity_failed") from None
        nick = identity.get("nick") if isinstance(identity, dict) else None
        if not isinstance(nick, str) or not nick.strip() or (not bind_current and nick != expected_nick):
            self.usable = False
            raise BridgeError("identity_mismatch")
        self.expected_nick = nick
        self.runtime_summary = {
            "task_root": str(self._root), "node": self._command[0], "opencli_script": self._command[1],
            "run_id": run_id, "worker_id": worker_id, "owner_pid": self._owner_pid,
            "session": self.session, "tab": self.tab, "origin": ORIGIN, "path": HISTORY_PATH,
            "markers": dict(self._markers), "identity_verified": True,
            "expected_nick": self.expected_nick, "identity_binding": "current_login" if bind_current else "expected_nick",
            "allow_discovery": self._allow_discovery, "bootstrap_cli_calls": self._cli_calls,
            "bootstrap_ms": round((time.perf_counter() - begin) * 1000, 3),
            "transport": "opencli_process",
        }
        return self

    def _matches_url(self, value):
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value)
            return (parsed.scheme + "://" + parsed.netloc == ORIGIN and parsed.path == HISTORY_PATH
                    and parsed.fragment in ("", "/") and parse_qs(parsed.query) == {k: [v] for k, v in self._markers.items()})
        except ValueError:
            return False

    def _session_args(self):
        return ["--session", self.session, "--window", "background"]

    def _eval_args(self, js):
        return ["browser", "eval", js, "--tab", self.tab, *self._session_args()]

    def _reset_metrics(self):
        self.last_metrics = {
            "total_ms": None, "mtop_ms": None, "non_api_overhead_ms": None,
            "response_bytes": None, "returncode": None, "cli_stdout_bytes": 0,
            "cli_stderr_bytes": 0, "timing_available": False,
            "transport": "opencli_process", "status": "pending", "error_code": "none",
            "cli_error": _cli_error_fields(None),
        }

    def _cli(self, args):
        self._cli_calls += 1
        try:
            result = subprocess.run(self._command + args, capture_output=True, text=True,
                                    encoding="utf-8", errors="strict", timeout=TIMEOUT_SECONDS,
                                    check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            self.last_metrics["cli_stdout_bytes"] = _byte_count(error.stdout)
            self.last_metrics["cli_stderr_bytes"] = _byte_count(error.stderr)
            raise BridgeError("cli_timeout") from None
        except (OSError, UnicodeError):
            raise BridgeError("cli_unavailable") from None
        self.last_metrics.update(returncode=result.returncode,
                                 cli_stdout_bytes=_byte_count(result.stdout), cli_stderr_bytes=_byte_count(result.stderr))
        try:
            envelope = _loads(result.stdout)
        except (TypeError, ValueError):
            if result.returncode:
                raise BridgeError(_diagnostic_code(result.stderr + "\n" + result.stdout)) from None
            raise BridgeError("protocol_invalid") from None
        self.last_metrics["cli_error"] = _cli_error_fields(envelope)
        if result.returncode:
            raise BridgeError(_diagnostic_code(result.stderr + "\n" + result.stdout)) from None
        if not isinstance(envelope, dict):
            raise BridgeError("protocol_invalid")
        if envelope.get("ok") is not True:
            raise BridgeError(_diagnostic_code(_json(envelope))) from None
        return envelope.get("data")

    def request(self, api, data):
        begin = time.perf_counter()
        self._reset_metrics()
        try:
            if not self.usable:
                raise BridgeError("worker_unusable")
            if os.getpid() != self._owner_pid:
                raise BridgeError("process_mismatch")
            if api not in (IDENTITY_API, MESSAGE_API) and not (api == CONVERSATION_API and self._allow_discovery):
                raise BridgeError("api_not_allowed", False)
            if not isinstance(data, dict):
                raise BridgeError("invalid_request", False)
            try:
                request = _json({"api": api, "v": "1.0", "ecode": 0, "data": data})
            except (TypeError, ValueError, OverflowError):
                raise BridgeError("invalid_request", False) from None
            guard = _json({"origin": ORIGIN, "path": HISTORY_PATH, "markers": self._markers})
            js = """(async()=>{
const expected=GUARD, u=new URL(location.href);
if(u.origin!==expected.origin||u.pathname!==expected.path||(u.hash&&u.hash!=="#/")||
   [...u.searchParams].length!==Object.keys(expected.markers).length||
   Object.entries(expected.markers).some(([k,v])=>u.searchParams.get(k)!==v))
  throw new Error('BRIDGE_TARGET_MISMATCH');
const verificationRequired=()=>[...document.querySelectorAll('iframe[src]')].some(frame=>{
  try{const f=new URL(frame.getAttribute('src'),location.href);
    return f.protocol==='https:'&&(f.hostname==='taobao.com'||f.hostname.endsWith('.taobao.com'))&&
      PUNISH_PATHS.includes(f.pathname);
  }catch(_){return false;}
});
const start=performance.now();let observer;
const verificationResult=()=>({error_code:'verification_required',mtop_ms:performance.now()-start});
try{
  if(verificationRequired())return JSON.stringify(verificationResult());
  if(!window.lib||!window.lib.mtop||typeof window.lib.mtop.request!=='function')
    throw new Error('BRIDGE_MTOP_NOT_READY');
  const verification=new Promise(resolve=>{
    observer=new MutationObserver(()=>{if(verificationRequired())resolve(verificationResult());});
    observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src']});
  });
  if(verificationRequired())return JSON.stringify(verificationResult());
  const operation=(async()=>{let response;
    try{response=await window.lib.mtop.request(REQUEST);}
    catch(e){if(e&&typeof e==='object'&&Array.isArray(e.ret)){response=e;}
             else{throw new Error('BRIDGE_MTOP_FAILURE');}}
    return {response,mtop_ms:performance.now()-start};
  })();
  return JSON.stringify(await Promise.race([operation,verification]));
}finally{if(observer)observer.disconnect();}
})()""".replace("PUNISH_PATHS", _json(["/h5/" + name + "/1.0/_____tmd_____/punish" for name in dict.fromkeys((MESSAGE_API, api))]), 1).replace("REQUEST", request, 1).replace("GUARD", guard, 1)
            payload = self._cli(self._eval_args(js))
            if isinstance(payload, str):
                try:
                    payload = _loads(payload)
                except (ValueError, TypeError):
                    raise BridgeError("protocol_invalid") from None
            if isinstance(payload, dict) and set(payload) == {"error_code", "mtop_ms"} and payload["error_code"] == "verification_required":
                raise BridgeError("verification_required")
            if not isinstance(payload, dict) or set(payload) != {"response", "mtop_ms"} or not isinstance(payload["response"], dict):
                raise BridgeError("protocol_invalid")
            response, mtop_ms = payload["response"], payload["mtop_ms"]
            elapsed = (time.perf_counter() - begin) * 1000
            if type(mtop_ms) not in (int, float) or not math.isfinite(mtop_ms) or mtop_ms < 0 or mtop_ms > elapsed + 1:
                raise BridgeError("timing_invalid")
            try:
                response_bytes = len(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError):
                raise BridgeError("protocol_invalid") from None
            self.last_metrics.update(mtop_ms=round(mtop_ms, 3), response_bytes=response_bytes, timing_available=True)
            ret = response.get("ret")
            if isinstance(ret, list) and any(isinstance(value, str) and value.split("::", 1)[0].startswith((
                "FAIL_SYS_SESSION", "FAIL_SYS_LOGIN", "FAIL_SYS_TOKEN", "FAIL_SYS_USER_VALIDATE",
                "FAIL_SYS_NEED_LOGIN", "FAIL_SYS_AUTH")) for value in ret):
                raise BridgeError("auth_required")
            self.last_metrics["status"] = "response"
            return response
        except BridgeError as error:
            self.last_metrics.update(status="failed", error_code=error.code)
            if error.worker_unusable:
                self.usable = False
            raise
        finally:
            total = (time.perf_counter() - begin) * 1000
            self.last_metrics["total_ms"] = round(total, 3)
            if self.last_metrics["timing_available"]:
                self.last_metrics["non_api_overhead_ms"] = round(max(0, total - self.last_metrics["mtop_ms"]), 3)
