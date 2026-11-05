#!/usr/bin/env python3
"""Discover authorized Qianniu history and plan frozen monthly query contexts.

No cookies are read. Browser requests are read-only. All dates are Asia/Shanghai;
--end is exclusive. Message/media execution uses scripts/unified/pipeline.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
CONVERSATIONS = "mtop.taobao.wireless.amp2.paas.conversation.list"
MESSAGES = "mtop.taobao.wireless.amp2.im.paas.message.list"


class ExportError(RuntimeError):
    pass


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path, value):
    atomic_bytes(Path(path), json_bytes(value))


def retain_failure(directory, request, response):
    """Keep failed API evidence locally without putting its content in diagnostics."""
    evidence = {"request": request, "response": response}
    stamp = dt.datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S_%f")
    atomic_json(directory / "failures" / (stamp + ".json"), evidence)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON object keys")
    return result


def _reject_json_constant(value):
    raise ValueError("Non-finite JSON value")


def same_message_observation(left, right):
    """Compare sent-message fields; preserve raw observations unchanged."""
    ignored = ("readStatus", "unreadCount", "bizUnique")
    before = {key: value for key, value in left.items() if key not in ignored}
    after = {key: value for key, value in right.items() if key not in ignored}
    # Only the two observed string-valued ext fields get object normalization.
    # Invalid JSON, arrays/scalars and object-vs-string retain strict raw identity.
    if isinstance(before.get("ext"), str) and isinstance(after.get("ext"), str):
        try:
            ext_before = json.loads(before["ext"], object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
            ext_after = json.loads(after["ext"], object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(ext_before, dict) and isinstance(ext_after, dict):
                before["ext"] = json.dumps(ext_before, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                after["ext"] = json.dumps(ext_after, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # JSON serialization distinguishes booleans from numbers (unlike dict ==).
    return json_bytes(before) == json_bytes(after)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def date(value):
    try:
        parsed = dt.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ExportError("Dates must use YYYY-MM-DD") from None


def millis(day):
    return int(dt.datetime.combine(date(day), dt.time(), TZ).timestamp() * 1000)


def days(start, end):
    first, last = date(start), date(end)
    if first >= last:
        raise ExportError("start must be before exclusive end")
    return [(first + dt.timedelta(days=n)).isoformat() for n in reversed(range((last-first).days))]


def as_bool(value, name):
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ExportError(f"{name} must be a boolean or 'true'/'false'")


def api_data(response):
    if not isinstance(response, dict):
        raise ExportError("API response is not an object")
    ret = response.get("ret")
    if response.get("success") is False or not isinstance(ret, list) or not ret or any(
        not str(code).startswith("SUCCESS::") for code in ret
    ):
        codes = [re.sub(r"[^A-Z0-9_]", "", str(code).split("::", 1)[0])[:80] for code in (ret or [])]
        raise ExportError("API stopped: " + ",".join(codes or ["INVALID_RESPONSE"]))
    payload = response.get("data", response)
    if not isinstance(payload, dict):
        raise ExportError("API data is not an object")
    return payload


class Browser:
    def __init__(self, args):
        node = args.node or os.environ.get("QIANNIU_NODE") or shutil.which("node")
        script = args.opencli_script or os.environ.get("QIANNIU_OPENCLI_SCRIPT")
        if not node or not script or not Path(script).is_file():
            raise ExportError("Supply --node and --opencli-script (or QIANNIU_NODE/QIANNIU_OPENCLI_SCRIPT)")
        self.prefix = [node, script, "browser", "eval"]
        self.suffix = ["--tab", str(args.tab), "--session", args.session, "--window", "background"]

    def request(self, api, data):
        request = json.dumps({"api": api, "v": "1.0", "ecode": 0, "data": data}, ensure_ascii=True)
        js = "(async()=>{if(!window.lib?.mtop)throw new Error('MTOP_NOT_READY');" \
             "try{return JSON.stringify(await window.lib.mtop.request(" + request + "));}" \
             "catch(e){return JSON.stringify({ret:e?.ret||['REQUEST_FAILED'],success:false});}})()"
        try:
            result = subprocess.run(self.prefix + [js] + self.suffix, capture_output=True,
                                    encoding="utf-8", errors="strict", timeout=120, check=False)
        except subprocess.TimeoutExpired:
            raise ExportError("Browser request timed out; no automatic retry") from None
        if result.returncode:
            raise ExportError(f"OpenCLI failed with exit {result.returncode}; inspect the signed-in H5 tab")
        try:
            envelope = json.loads(result.stdout)
            if envelope.get("ok") is not True:
                raise ValueError
            response = envelope["data"]
            if isinstance(response, str):
                response = json.loads(response)
        except (ValueError, KeyError, TypeError):
            raise ExportError("OpenCLI did not return its expected JSON envelope") from None
        return response

    def identity(self, expected):
        data = api_data(self.request("mtop.user.getUserSimple", {}))
        if data.get("nick") != expected:
            raise ExportError("Signed-in nick does not match expected_nick; export stopped")


def conversation_items(response, day):
    payload = api_data(response)
    if "hasMore" in payload and as_bool(payload["hasMore"], "hasMore"):
        raise ExportError("Conversation list reports another page; its paging contract must be established")
    records = payload.get("result")
    if not isinstance(records, list):
        raise ExportError("Conversation result is not an array")
    items, seen = [], set()
    for row in records:
        try:
            cid, uid = row["cid"]["appCid"], row["userID"]["appUid"]
            if not isinstance(cid, str) or not cid or not isinstance(uid, str) or not uid:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ExportError("Conversation is missing cid.appCid or userID.appUid") from None
        if cid in seen:
            raise ExportError("Conversation list contains duplicate cid")
        seen.add(cid)
        items.append({"day": day, "cid": cid, "user_id": uid, "display_name": row.get("displayName", "")})
    return items


def discover(args, browser):
    scope_days = days(args.start, args.end)
    root = Path(args.output).resolve()
    scope = {"start": args.start, "end": args.end, "expected_nick": args.expected_nick, "timezone": "Asia/Shanghai"}
    scope_path = root / "discovery-scope.json"
    if scope_path.exists() and read_json(scope_path) != scope:
        raise ExportError("Discovery output belongs to a different scope")
    atomic_json(scope_path, scope)
    browser.identity(args.expected_nick)
    items = []
    for day in scope_days:
        raw_path = root / "discovery" / f"{day}.json"
        receipt_path = root / "discovery" / f"{day}.receipt.json"
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if not raw_path.is_file() or file_digest(raw_path) != receipt["sha256"]:
                raise ExportError(f"Discovery raw hash mismatch: {day}")
            response = read_json(raw_path)
        else:
            request = {"selfWwNick": None, "targetWwNick": None,
                "beginDate": day.replace("-", ""), "endDate": day.replace("-", "")}
            response = browser.request(CONVERSATIONS, request)
            # Validate before marking this day's enumeration complete.
            try:
                conversation_items(response, day)
            except ExportError:
                retain_failure(root / "discovery", request, response)
                raise
            atomic_json(raw_path, response)
            atomic_json(receipt_path, {"day": day, "sha256": file_digest(raw_path)})
        daily = conversation_items(response, day)
        items.extend(daily)
        print(json.dumps({"event": "discovered", "day": day, "conversations": len(daily)}), flush=True)
    manifest = {"schema": 1, **scope, "user_nick": "cntaobao" + args.expected_nick, "items": items}
    atomic_json(root / "manifest.unassigned.json", manifest)
    return {"event": "discovery_complete", "days": len(scope_days), "work_items": len(items),
            "manifest": str(root / "manifest.unassigned.json")}


def work_id(item):
    return item["day"] + "__" + digest((item["cid"] + "\0" + item["user_id"]).encode("utf-8"))[:20]


def validate_manifest(manifest, lane):
    if manifest.get("schema") != 1 or manifest.get("timezone") != "Asia/Shanghai":
        raise ExportError("Manifest requires schema=1 and timezone=Asia/Shanghai")
    scope_days = set(days(manifest["start"], manifest["end"]))
    if not manifest.get("expected_nick") or manifest.get("user_nick") != "cntaobao" + manifest["expected_nick"]:
        raise ExportError("Manifest user_nick must equal cntaobao + expected_nick")
    selected, seen = [], set()
    if not isinstance(manifest.get("items"), list):
        raise ExportError("Manifest items must be an array")
    for item in manifest["items"]:
        if not isinstance(item, dict) or item.get("day") not in scope_days:
            raise ExportError("Work item day lies outside manifest scope")
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("cid", "user_id")):
            raise ExportError("Work item needs nonempty cid and user_id strings")
        key = (item["day"], item["cid"])
        if key in seen:
            raise ExportError("Duplicate day/cid in manifest")
        seen.add(key)
        if item.get("lane") not in ("codex", "kimi"):
            raise ExportError("Assign every item to lane codex or kimi before running")
        if item["lane"] == lane:
            selected.append(item)
    return sorted(selected, key=lambda item: (item["day"], item["cid"]), reverse=True)


def message_timestamp(raw, cid):
    if not isinstance(raw, dict) or not isinstance(raw.get("cid"), dict) or raw["cid"].get("appCid") != cid:
        raise ExportError("Message cid differs from requested conversation")
    try:
        timestamp = int(raw["sendTime"])
        if timestamp <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ExportError("Invalid message sendTime") from None
    return timestamp


def parse_message(raw, cid):
    timestamp = message_timestamp(raw, cid)
    try:
        if not isinstance(raw["messageId"], str) or not raw["messageId"]:
            raise ValueError
        content = raw["content"]
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ExportError("Invalid messageId, sendTime or content object") from None
    record = {"message_id": raw["messageId"], "cid": cid, "send_time_ms": timestamp,
              "send_time": dt.datetime.fromtimestamp(timestamp / 1000, TZ).isoformat(timespec="milliseconds"),
              "sender_id": raw.get("senderId"), "sender_name": raw.get("senderName"),
              "text": content.get("text") if "type" not in content else None,
              "content": content, "image": None, "ocr": None, "raw": raw}
    if str(content.get("type")) == "7":
        image = content.get("data")
        try:
            if isinstance(image, str):
                image = json.loads(image)
            if not isinstance(image, dict) or not isinstance(image.get("url"), str) or not image["url"]:
                raise ValueError
        except (TypeError, ValueError):
            raise ExportError("Image content has no valid data.url") from None
        record["image"] = {"source": image}
    return record


def parse_page(response, item, cursor, order, lower, upper):
    payload = api_data(response)
    raw_messages = payload.get("userMessages")
    if not isinstance(raw_messages, list):
        raise ExportError("userMessages is not an array")
    has_more = as_bool(payload.get("hasMore"), "hasMore")
    messages = []
    for raw in raw_messages:
        timestamp = message_timestamp(raw, item["cid"])
        # Out-of-interval rows establish the paging boundary. Their full payload
        # remains in raw pages, but their content is outside this work item.
        if lower <= timestamp < upper:
            messages.append(parse_message(raw, item["cid"]))
        else:
            messages.append({"message_id": str(raw.get("messageId", "")), "send_time_ms": timestamp, "raw": raw})
    times = [record["send_time_ms"] for record in messages]
    # A bound is crossed only by a received message, not by an assumed cursor meaning.
    crossed = bool(times) and (min(times) < lower if order == "descending" else max(times) >= upper)
    stop = "date_bound" if crossed else ("exhausted" if not has_more else None)
    next_cursor = payload.get("nextCursor")
    if not stop:
        try:
            next_cursor = int(next_cursor)
        except (ValueError, TypeError):
            raise ExportError("hasMore=true without a numeric nextCursor") from None
        if next_cursor == cursor or (next_cursor > cursor if order == "descending" else next_cursor < cursor):
            raise ExportError("Pagination cursor repeated or moved in the wrong direction")
        if not messages:
            raise ExportError("hasMore=true returned an empty message page")
    return messages, next_cursor, stop


def checked_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ExportError("Artifact path escapes its lane directory")
    return path


def artifact(root, path):
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": file_digest(path)}


def verify_artifacts(root, entries):
    for entry in entries:
        path = checked_path(root, entry["path"])
        if not path.is_file() or path.stat().st_size != entry["bytes"] or file_digest(path) != entry["sha256"]:
            raise ExportError("Artifact missing or changed: " + entry["path"])


def image_extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\0", b"MM\0*")):
        return ".tiff"
    raise ExportError("Image URL did not return a supported image signature")


def download_image(root, url):
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ExportError("Unsupported image URL")
    key = digest(url.encode("utf-8"))
    metadata_path = root / "images" / (key + ".json")
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        verify_artifacts(root, [metadata["artifact"]])
        if metadata["sha256"] != metadata["artifact"]["sha256"] or metadata["actual_bytes"] != metadata["artifact"]["bytes"]:
            raise ExportError("Cached image metadata disagrees with actual image artifact")
        return metadata, metadata_path
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://market.m.taobao.com/"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        raise ExportError(f"Image download HTTP {error.code}; export stopped") from None
    except (urllib.error.URLError, TimeoutError):
        raise ExportError("Image download failed; no automatic retry") from None
    extension = image_extension(data)
    path = root / "images" / (key + extension)
    atomic_bytes(path, data)
    metadata = {"url": url, "content_type": content_type, "actual_bytes": len(data), "sha256": digest(data),
                "artifact": artifact(root, path)}
    atomic_json(metadata_path, metadata)
    return metadata, metadata_path


class OCR:
    def __init__(self, executable):
        self.executable = executable
        self.process = None

    def recognize(self, path):
        if self.process is None:
            self.process = subprocess.Popen([self.executable, str(Path(__file__).with_name("ocr_worker.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1)
        self.process.stdin.write(json.dumps({"path": str(path)}, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        try:
            result = json.loads(line)
            if result.get("ok") is not True:
                raise ValueError
        except (ValueError, TypeError):
            raise ExportError("OCR worker failed; source image preserved; item remains incomplete") from None
        return result["result"]

    def close(self):
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)


def export_item(root, manifest, item, browser, ocr, forward, order, *, month=False, seeds=None):
    if month:
        from month_support import month_work_id
        directory = root / "month_items" / month_work_id(manifest, item)
        query_nick = item["query_user_nick"]
        lower, upper = millis(manifest["start"]), millis(manifest["end"])
        signature = digest(json_bytes({"schema": 2, "scope": "month", "start": manifest["start"],
            "end": manifest["end"], "query_user_nick": query_nick, "cid": item["cid"], "user_id": item["user_id"],
            "forward": forward, "cursor_order": order}))
    else:
        directory = root / "items" / work_id(item)
        query_nick = manifest["user_nick"]
        lower, upper = millis(item["day"]), millis(item["day"]) + 86400000
        # Keep the daily signature byte-for-byte compatible with frozen receipts.
        signature = digest(json_bytes({"item": item, "start": manifest["start"], "end": manifest["end"],
            "user_nick": manifest["user_nick"], "forward": forward, "cursor_order": order}))
    receipt_path, checkpoint_path = directory / "receipt.json", directory / "checkpoint.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("signature") != signature or receipt.get("status") != "complete":
            raise ExportError("Receipt does not match work scope")
        verify_artifacts(root, receipt["artifacts"])
        return {**receipt["counts"], "skipped": True}
    initial_cursor = upper - 1 if order == "descending" else lower
    state = {"signature": signature, "next_cursor": initial_cursor, "pages": [], "stop_reason": None}
    if checkpoint_path.exists():
        state = read_json(checkpoint_path)
        if state.get("signature") != signature:
            raise ExportError("Checkpoint does not match work scope")
        verify_artifacts(root, state["pages"])
    seen_cursors = set()
    seen_page_keys = set()
    records = {}
    previous_edge = None
    reused_pages = 0
    artifacts = list(state["pages"])

    def absorb(messages):
        nonlocal previous_edge
        page_key = tuple(sorted(message["message_id"] for message in messages))
        if page_key:
            if page_key in seen_page_keys:
                raise ExportError("Complete message page repeated; item remains incomplete")
            seen_page_keys.add(page_key)
        times = [message["send_time_ms"] for message in messages]
        if times:
            edge = max(times) if order == "descending" else min(times)
            if previous_edge is not None and (edge > previous_edge if order == "descending" else edge < previous_edge):
                raise ExportError("Message pages moved in the wrong time direction")
            previous_edge = edge
        for message in messages:
            if not lower <= message["send_time_ms"] < upper:
                continue
            key = message["message_id"]
            if key in records:
                previous = records[key]
                current_raw_key = json_bytes(message["raw"])
                if json_bytes(previous["raw"]) == current_raw_key:
                    continue
                if not same_message_observation(previous["raw"], message["raw"]):
                    raise ExportError("Repeated messageId has conflicting content, time, identity or other immutable fields")
                observations = previous.setdefault("raw_observations", [previous["raw"]])
                if all(json_bytes(raw) != current_raw_key for raw in observations):
                    observations.append(message["raw"])
                continue
            records[key] = message

    replay_cursor, replay_stop = initial_cursor, None
    for entry in state["pages"]:
        saved = read_json(checked_path(root, entry["path"]))
        reused_pages += bool(saved.get("reused_from"))
        cursor = saved["request"]["cursor"]
        if month:
            expected_request = {"userNick": query_nick, "cid": item["cid"], "userId": item["user_id"],
                "cursor": replay_cursor, "forward": forward, "count": 100, "needRecalledContent": True}
            if replay_stop is not None or saved["request"] != expected_request or saved.get("transport") != {"api": MESSAGES, "v": "1.0", "ecode": 0}:
                raise ExportError("Month checkpoint does not form a continuous request cursor chain")
        if cursor in seen_cursors:
            raise ExportError("Checkpoint contains a repeated request cursor")
        seen_cursors.add(cursor)
        messages, replay_cursor, replay_stop = parse_page(saved["response"], item, cursor, order, lower, upper)
        absorb(messages)
    if month and (state["next_cursor"] != replay_cursor or state["stop_reason"] != replay_stop):
        raise ExportError("Month checkpoint cursor or stop reason differs from verified raw pages")
    while not state["stop_reason"]:
        cursor = state["next_cursor"]
        if cursor in seen_cursors:
            raise ExportError("Pagination cursor cycle detected")
        request = {"userNick": query_nick, "cid": item["cid"], "userId": item["user_id"],
                   "cursor": cursor, "forward": forward, "count": 100, "needRecalledContent": True}
        reused = seeds.lookup(request) if seeds is not None else None
        response = reused["response"] if reused is not None else browser.request(MESSAGES, request)
        try:
            messages, next_cursor, stop = parse_page(response, item, cursor, order, lower, upper)
            absorb(messages)
        except ExportError:
            retain_failure(directory, request, response)
            raise
        page_path = directory / "raw" / f"{len(state['pages']) + 1:06d}.json"
        saved = {"request": request, "response": response}
        if month:
            saved["transport"] = {"api": MESSAGES, "v": "1.0", "ecode": 0}
        if reused is not None:
            saved["reused_from"] = reused["source"]
            reused_pages += 1
        atomic_json(page_path, saved)
        page_artifact = artifact(root, page_path)
        artifacts.append(page_artifact)
        state["pages"].append(page_artifact)
        state["next_cursor"], state["stop_reason"] = next_cursor, stop
        atomic_json(checkpoint_path, state)
        seen_cursors.add(cursor)
    ordered = sorted(records.values(), key=lambda message: (message["send_time_ms"], message["message_id"]), reverse=True)
    image_count = 0
    for message in ordered:
        if message["image"] is None:
            continue
        if seeds is not None:
            seeds.prime_image(root, message["image"]["source"]["url"])
        metadata, metadata_path = download_image(root, message["image"]["source"]["url"])
        image_path = checked_path(root, metadata["artifact"]["path"])
        ocr_path = root / "ocr" / (metadata["sha256"] + ".json")
        if seeds is not None:
            seeds.prime_ocr(root, metadata["sha256"])
        if not ocr_path.exists():
            atomic_json(ocr_path, {"image_sha256": metadata["sha256"], **ocr.recognize(image_path)})
        ocr_data = read_json(ocr_path)
        if ocr_data.get("image_sha256") != metadata["sha256"] or not isinstance(ocr_data.get("frames"), list):
            raise ExportError("OCR cache does not match downloaded image")
        message["image"].update({"local_path": metadata["artifact"]["path"], "actual_bytes": metadata["actual_bytes"],
                                  "sha256": metadata["sha256"]})
        message["ocr"] = {"path": ocr_path.relative_to(root).as_posix(), "result": ocr_data}
        artifacts.extend([metadata["artifact"], artifact(root, metadata_path), artifact(root, ocr_path)])
        image_count += 1
    messages_path = directory / "messages.jsonl"
    atomic_bytes(messages_path, b"".join((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8") for message in ordered))
    artifacts.extend([artifact(root, messages_path), artifact(root, checkpoint_path)])
    unique_artifacts = {entry["path"]: entry for entry in artifacts}
    counts = {"messages": len(ordered), "image_messages": image_count, "pages": len(state["pages"])}
    if month:
        counts["seed_pages_reused"] = reused_pages
    receipt = {"schema": 1, "status": "complete", "signature": signature, "item": item,
               "interval": {"start_ms": lower, "end_exclusive_ms": upper, "timezone": "Asia/Shanghai"},
               "stop_reason": state["stop_reason"], "counts": counts, "artifacts": list(unique_artifacts.values())}
    if month:
        receipt.update({"schema": 2, "scope": "month", "query_user_nick": query_nick,
                        "enumeration_complete": False})
    atomic_json(receipt_path, receipt)
    return {**counts, "skipped": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover", help="Enumerate day/conversation work items, newest day first")
    discovery.add_argument("--start", required=True)
    discovery.add_argument("--end", required=True, help="Exclusive YYYY-MM-DD")
    discovery.add_argument("--expected-nick", required=True)
    planner = commands.add_parser("plan-month", help="Plan every observed account/cid/userId scope for the entire interval; no API calls")
    planner.add_argument("--root", required=True)
    planner.add_argument("--output", required=True, help="New month manifest file")
    planner.add_argument("--start", required=True)
    planner.add_argument("--end", required=True)
    planner.add_argument("--initial-discovery")
    planner.add_argument("--supplemental-discovery")
    discovery.add_argument("--output", required=True)
    discovery.add_argument("--node")
    discovery.add_argument("--opencli-script")
    discovery.add_argument("--session", required=True)
    discovery.add_argument("--tab", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan-month":
            from month_support import plan_month
            result = plan_month(args, sys.modules[__name__])
        else:
            result = discover(args, Browser(args))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except (ExportError, OSError, ValueError, KeyError) as error:
        # Source URLs, message bodies and subprocess output never enter CLI diagnostics.
        detail = str(error) if isinstance(error, ExportError) else type(error).__name__
        print(json.dumps({"event": "stopped", "reason": detail}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
