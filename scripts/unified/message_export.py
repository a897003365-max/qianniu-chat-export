"""Immutable message-stage files. Image download/OCR and queue policy live elsewhere."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid


class MessageExportError(RuntimeError):
    pass


class ItemConflict(MessageExportError):
    def __init__(self, job_key, message_id, changed_fields):
        super().__init__("Message observations conflict in job "+job_key+"; fields="+",".join(changed_fields))
        self.job_key = job_key
        self.message_id = message_id
        self.changed_fields = changed_fields
        self.failure_file = None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _scope(manifest, item):
    scope = {"start": manifest["start"], "end": manifest["end"],
             **{key: item[key] for key in ("query_user_nick", "cid", "user_id")}}
    if manifest.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise MessageExportError("Unsupported message timezone")
    if any(not isinstance(value, str) or not value for value in scope.values()):
        raise MessageExportError("Month and context fields must be nonempty strings")
    try:
        first, last = dt.date.fromisoformat(scope["start"]), dt.date.fromisoformat(scope["end"])
        if first.isoformat() != scope["start"] or last.isoformat() != scope["end"] or first >= last:
            raise ValueError
    except ValueError:
        raise MessageExportError("Invalid message interval") from None
    return scope


def context_key(manifest, item):
    return hashlib.sha256(_canonical(_scope(manifest, item))).hexdigest()


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs): raise ValueError("duplicate JSON keys")
    return value


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _semantic(raw):
    result = {key: value for key, value in raw.items() if key not in ("readStatus", "unreadCount", "bizUnique")}
    for key in ("content", "ext"):
        if not isinstance(result.get(key), str): continue
        try:
            value = json.loads(result[key], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            result[key] = _canonical(value).decode("utf-8")
    return result


def _conflict(before, after, job_key):
    left, right = _semantic(before), _semantic(after)
    if _canonical(left) == _canonical(right): return
    fields = sorted(key for key in left.keys() | right.keys()
                    if (key in left) != (key in right) or _canonical(left.get(key)) != _canonical(right.get(key)))
    raise ItemConflict(job_key, before.get("messageId"), fields)


def _metrics(value):
    """Keep numeric timings/counts and fixed safe enums, never credentials/body/URLs."""
    if not isinstance(value, dict): return None
    safe = {}
    for key, item in value.items():
        if not isinstance(key, str) or re.search(r"cookie|token|auth|password|secret|key|header|body|url|payload|content|text|nick|user", key, re.I):
            continue
        if item is None or isinstance(item, bool) or (isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)):
            safe[key] = item
        elif isinstance(item, dict):
            safe[key] = _metrics(item)
        elif key in ("transport", "status", "cache_status", "method", "error_code", "channel") and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item):
            safe[key] = item
    return safe


def _request(scope, cursor):
    return {"userNick": scope["query_user_nick"], "cid": scope["cid"], "userId": scope["user_id"],
            "cursor": cursor, "forward": False, "count": 100, "needRecalledContent": True}


def _transport(request, q):
    return {"api": q.MESSAGES, "v": "1.0", "ecode": 0, "data": request}


def _fingerprint(request, q):
    return q.digest(q.json_bytes(_transport(request, q)))


def _artifact_paths(directory, root):
    return {name: (directory/name).relative_to(root).as_posix()
            for name in ("checkpoint.json", "messages.jsonl", "image_refs.jsonl")}


def _check_state(state, signature, job_key):
    if state.get("schema") != 3 or state.get("stage") != "messages" or state.get("job_key") != job_key or state.get("signature") != signature:
        raise MessageExportError("Message checkpoint scope/signature mismatch")
    if not isinstance(state.get("pages"), list):
        raise MessageExportError("Message checkpoint page list is invalid")


def _safe_page_entry(entry, directory, root):
    expected = (directory/"raw").relative_to(root).as_posix()+"/"
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"].startswith(expected) or ".." in Path(entry["path"]).parts:
        raise MessageExportError("Message page artifact is outside this job")


def _valid_artifacts(entries):
    if not isinstance(entries,list): raise MessageExportError("Message artifact list is invalid")
    for entry in entries:
        if not isinstance(entry,dict) or set(entry) != {"path","bytes","sha256"} or not isinstance(entry["path"],str) or type(entry["bytes"]) is not int or entry["bytes"] < 0 or not isinstance(entry["sha256"],str) or re.fullmatch(r"[0-9a-f]{64}",entry["sha256"]) is None:
            raise MessageExportError("Message artifact descriptor is invalid")
    if len({entry["path"] for entry in entries}) != len(entries):
        raise MessageExportError("Message artifact list repeats a path")


def collect_messages(output_root, manifest, item, browser, q, *, seeds=None, max_pages=None):
    """Collect all messages, or cap total verified pages without completing the job."""
    if max_pages is not None and (type(max_pages) is not int or max_pages <= 0):
        raise MessageExportError("max_pages must be a positive integer or None")
    root = Path(output_root).resolve()
    scope = _scope(manifest, item)
    job_key = context_key(manifest, item)
    directory = root/"messages"/job_key
    lower, upper = q.millis(scope["start"]), q.millis(scope["end"])
    initial = _request(scope, upper-1)
    signature = q.digest(q.json_bytes({"schema":3,"stage":"messages","scope":scope,
        "direction":"descending","comparison":"strict-content-ext-observations.v1",
        "initial_request":_transport(initial,q),"initial_request_fingerprint":_fingerprint(initial,q)}))
    checkpoint_path, receipt_path = directory/"checkpoint.json", directory/"message-receipt.json"
    paths = _artifact_paths(directory, root)
    state = {"schema":3,"stage":"messages","job_key":job_key,"signature":signature,
             "next_cursor":upper-1,"stop_reason":None,"pages":[]}
    if checkpoint_path.exists():
        state = q.read_json(checkpoint_path)
        _check_state(state, signature, job_key)
        _valid_artifacts(state["pages"])
        for entry in state["pages"]: _safe_page_entry(entry,directory,root)
        q.verify_artifacts(root,state["pages"])
    if receipt_path.exists():
        receipt = q.read_json(receipt_path)
        if receipt.get("schema") != 3 or receipt.get("stage") != "messages" or receipt.get("completion_scope") != "messages_only" or receipt.get("status") != "complete" or receipt.get("job_key") != job_key or receipt.get("signature") != signature or receipt.get("scope") != scope:
            raise MessageExportError("Message receipt scope/signature mismatch")
        expected_paths = set(paths.values()) | {entry["path"] for entry in state["pages"]}
        _valid_artifacts(receipt.get("artifacts"))
        if not checkpoint_path.exists() or state["stop_reason"] not in ("date_bound","exhausted") or {entry["path"] for entry in receipt["artifacts"]} != expected_paths:
            raise MessageExportError("Message receipt artifacts are incomplete")
        if receipt.get("interval") != {"start_ms":lower,"end_exclusive_ms":upper,"timezone":"Asia/Shanghai"} or receipt.get("initial_request_fingerprint") != _fingerprint(initial,q) or receipt.get("stop_reason") != state["stop_reason"]:
            raise MessageExportError("Message receipt interval or request binding mismatch")
        q.verify_artifacts(root,receipt["artifacts"])
        counts, metrics = receipt.get("counts",{}), receipt.get("page_metrics")
        if any(type(counts.get(name)) is not int or counts[name] < 0 for name in ("messages","image_refs","pages","cached_pages")) or counts["pages"] != len(state["pages"]) or not isinstance(metrics,list) or len(metrics) != counts["pages"] or any(type(metric.get("cache_hit")) is not bool for metric in metrics) or counts["cached_pages"] != sum(metric["cache_hit"] for metric in metrics):
            raise MessageExportError("Message receipt counts/metrics mismatch")
        for filename,name in (("messages.jsonl","messages"),("image_refs.jsonl","image_refs")):
            content=(directory/filename).read_bytes()
            lines=content.split(b"\n")
            if lines and lines[-1] == b"": lines.pop()
            if len(lines) != counts[name] or any(not line for line in lines):
                raise MessageExportError("Message receipt JSONL count mismatch")
        return receipt

    records, seen_cursors, seen_pages, page_metrics = {}, set(), set(), []
    previous_max = None

    def inspect_response(response, request):
        nonlocal previous_max
        payload = q.api_data(response)
        if response.get("api",q.MESSAGES) != q.MESSAGES or response.get("v","1.0") != "1.0":
            raise MessageExportError("Message response API/version mismatch")
        rows = payload.get("userMessages")
        if not isinstance(rows,list): raise MessageExportError("userMessages is not an array")
        # Check repeated raw identities before parsing a changed content type or CID.
        # A true duplicate conflict must remain distinguishable from a new bad row.
        observed = {key: record["raw"] for key,record in records.items()}
        for raw in rows:
            if not isinstance(raw,dict): raise MessageExportError("Message row is not an object")
            key = raw.get("messageId")
            if key in observed: _conflict(observed[key],raw,job_key)
            timestamp = q.message_timestamp(raw,scope["cid"])
            if lower <= timestamp < upper and isinstance(key,str): observed.setdefault(key,raw)
        messages, next_cursor, stop = q.parse_page(response,item,request["cursor"],"descending",lower,upper)
        page_key = tuple(sorted(message["message_id"] for message in messages))
        if page_key and page_key in seen_pages:
            raise MessageExportError("Complete message page repeated")
        times = [message["send_time_ms"] for message in messages]
        if times:
            edge = max(times)
            if previous_max is not None and edge > previous_max:
                raise MessageExportError("Message pages moved in the wrong time direction")
            previous_max = edge
        if page_key: seen_pages.add(page_key)
        for message in messages:
            if not lower <= message["send_time_ms"] < upper: continue
            key = message["message_id"]
            if key in records:
                previous = records[key]
                _conflict(previous["raw"],message["raw"],job_key)
                current_key = _canonical(message["raw"])
                if _canonical(previous["raw"]) != current_key:
                    observations = previous.setdefault("raw_observations",[previous["raw"]])
                    if all(_canonical(raw) != current_key for raw in observations): observations.append(message["raw"])
            else:
                records[key] = message
        return next_cursor, stop

    cursor, stop = upper-1, None
    for entry in state["pages"]:
        saved = q.read_json(q.checked_path(root,entry["path"]))
        request = _request(scope,cursor)
        if stop is not None or saved.get("job_key") != job_key or saved.get("signature") != signature or saved.get("request") != request or saved.get("transport") != _transport(request,q) or saved.get("request_fingerprint") != _fingerprint(request,q):
            raise MessageExportError("Saved message request does not continue the exact cursor chain")
        if cursor in seen_cursors: raise MessageExportError("Repeated saved request cursor")
        seen_cursors.add(cursor)
        cursor, stop = inspect_response(saved["response"],request)
        page_metrics.append(saved["metrics"])
    if state["next_cursor"] != cursor or state["stop_reason"] != stop:
        raise MessageExportError("Checkpoint differs from verified message pages")
    if max_pages is not None and len(state["pages"]) > max_pages:
        raise MessageExportError("Saved message pages exceed max_pages; use a larger limit or a new output directory")

    while stop is None and (max_pages is None or len(state["pages"]) < max_pages):
        if cursor in seen_cursors: raise MessageExportError("Message cursor cycle detected")
        request = _request(scope,cursor)
        response, reused = None, None
        started = time.perf_counter()
        metrics = {"cache_hit":False,"elapsed_seconds":0.0,"browser_metrics":None}
        try:
            reused = seeds.lookup(request) if seeds is not None else None
            if reused is not None:
                if not isinstance(reused,dict) or not isinstance(reused.get("source"),dict) or reused["source"].get("request_fingerprint") != _fingerprint(request,q):
                    raise MessageExportError("Seed is not bound to the complete request fingerprint")
                response = reused["response"]
                metrics["cache_hit"] = True
            else:
                response = browser.request(q.MESSAGES,request)
                metrics["browser_metrics"] = _metrics(getattr(browser,"last_metrics",None))
            next_cursor, next_stop = inspect_response(response,request)
            metrics["elapsed_seconds"] = time.perf_counter()-started
            metrics["request_fingerprint"] = _fingerprint(request,q)
            saved = {"schema":3,"stage":"messages","job_key":job_key,"signature":signature,
                "transport":_transport(request,q),"request":request,"request_fingerprint":_fingerprint(request,q),
                "response":response,"metrics":metrics}
            if reused is not None: saved["reused_from"] = reused["source"]
            # Unique filenames also retain any page whose checkpoint publication was interrupted.
            page_path = directory/"raw"/(f"{len(state['pages'])+1:06d}-"+uuid.uuid4().hex+".json")
            q.atomic_json(page_path,saved)
            state["pages"].append(q.artifact(root,page_path))
            state["next_cursor"], state["stop_reason"] = next_cursor,next_stop
            q.atomic_json(checkpoint_path,state)
            seen_cursors.add(cursor)
            cursor, stop = next_cursor,next_stop
            page_metrics.append(metrics)
        except Exception as error:
            metrics["elapsed_seconds"] = time.perf_counter()-started
            if not metrics["cache_hit"]: metrics["browser_metrics"] = _metrics(getattr(browser,"last_metrics",None))
            failure = {"schema":3,"stage":"messages","job_key":job_key,"signature":signature,
                "transport":_transport(request,q),"request":request,"request_fingerprint":_fingerprint(request,q),
                "response":response,"response_received":response is not None,"metrics":metrics,
                "error_type":type(error).__name__}
            if isinstance(error,ItemConflict):
                failure.update({"message_id":error.message_id,"changed_fields":error.changed_fields})
            elif isinstance(error,(MessageExportError,q.ExportError)):
                failure["safe_reason"] = str(error)
            failure_path = directory/"failures"/(dt.datetime.now(q.TZ).strftime("%Y-%m-%d_%H%M%S_%f")+".json")
            q.atomic_json(failure_path,failure)
            if isinstance(error,ItemConflict): error.failure_file = failure_path.relative_to(root).as_posix()
            raise

    ordered = sorted(records.values(),key=lambda record:(record["send_time_ms"],record["message_id"]),reverse=True)
    refs = []
    for message in ordered:
        if message.get("image") is None: continue
        source_image = message["image"]["source"]
        identity = {"job_key":job_key,"cid":scope["cid"],"message_id":message["message_id"],"source_url":source_image["url"]}
        ref_key = hashlib.sha256(_canonical(identity)).hexdigest()
        message["image_ref_key"] = ref_key
        refs.append({"ref_key":ref_key,**identity,"send_time_ms":message["send_time_ms"],
            "query_user_nick":scope["query_user_nick"],"user_id":scope["user_id"],"image_source":source_image})
    for name, rows in (("messages.jsonl",ordered),("image_refs.jsonl",refs)):
        q.atomic_bytes(directory/name,b"".join(_canonical(row)+b"\n" for row in rows))
    partial = stop is None
    if partial: receipt_path = directory/"sample-receipt.json"
    receipt = {"schema":3,"stage":"messages","status":"partial" if partial else "complete",
        "completion_scope":"messages_sample" if partial else "messages_only",
        "job_key":job_key,"scope":scope,"signature":signature,"initial_request_fingerprint":_fingerprint(initial,q),
        "interval":{"start_ms":lower,"end_exclusive_ms":upper,"timezone":"Asia/Shanghai"},"stop_reason":"page_limit" if partial else stop,
        "counts":{"messages":len(ordered),"image_refs":len(refs),"pages":len(state["pages"]),
                  "cached_pages":sum(metric["cache_hit"] for metric in page_metrics)},
        "page_metrics":page_metrics,"artifacts":state["pages"]+[q.artifact(root,directory/name) for name in paths]}
    q.atomic_json(receipt_path,receipt)
    return receipt
