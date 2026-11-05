"""Read-only legacy proof adoption and exact-request raw cache registration."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import message_export as messages


class LegacyImportError(RuntimeError):
    pass


def _same(left, right):
    return messages._canonical(left) == messages._canonical(right)


def _inside(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise LegacyImportError("Legacy path escapes task root")
    return path


def _relative_artifacts(entries):
    messages._valid_artifacts(entries)
    if any(Path(entry["path"]).anchor or ".." in Path(entry["path"]).parts for entry in entries):
        raise LegacyImportError("Legacy artifact paths must be strictly relative")


def _request(request):
    keys = {"userNick","cid","userId","cursor","forward","count","needRecalledContent"}
    if not isinstance(request,dict) or set(request) != keys or any(not isinstance(request[k],str) or not request[k] for k in ("userNick","cid","userId")) or type(request["cursor"]) is not int or type(request["forward"]) is not bool or type(request["count"]) is not int or request["count"] != 100 or request["needRecalledContent"] is not True:
        raise LegacyImportError("Legacy page lacks the exact seven-field request")


def _transport(saved, request, q, m=None, provenance=None):
    response = saved.get("response")
    if not isinstance(response,dict) or response.get("api") != q.MESSAGES or response.get("v") != "1.0":
        raise LegacyImportError("Legacy response API/version mismatch")
    expected = {"api":q.MESSAGES,"v":"1.0","ecode":0}
    transport = saved.get("transport")
    if transport is not None:
        if _same(transport,{**expected,"data":request}):
            return {**expected,"ecode_evidence":"explicit_saved_transport_with_data"}
        if not _same(transport,expected):
            raise LegacyImportError("Legacy transport fields differ from the request contract")
    if m is not None:
        return m.seed_transport(saved,q)
    evidence = (provenance or {}).get("transport_evidence")
    if transport is not None:
        return {**expected,"ecode_evidence":"saved_transport"}
    if not isinstance(evidence,dict) or any(not _same(evidence.get(k),v) for k,v in expected.items()) or evidence.get("ecode_evidence") != "frozen_daily_browser_code_constant_not_recorded_in_legacy_page":
        raise LegacyImportError("Old page has no recorded ecode code-contract evidence")
    return evidence


class PageSeeds:
    def __init__(self, queue, q):
        self.queue, self.q = queue, q

    def lookup(self, request):
        q = self.q
        _request(request)
        fingerprint = messages._fingerprint(request,q)
        found = self.queue.lookup_page(fingerprint,request)
        if found is None: return None
        saved, row = found
        row = dict(row)
        root = Path(row["artifact_root"]).resolve()
        entry = {key:row[key] for key in ("path","bytes","sha256")}
        _relative_artifacts([entry])
        q.verify_artifacts(root,[entry])
        if not _same(saved.get("request"),request):
            raise LegacyImportError("Cached raw request differs from the requested fingerprint")
        if "request_fingerprint" in saved and saved["request_fingerprint"] != fingerprint:
            raise LegacyImportError("Cached wrapper fingerprint differs")
        provenance = json.loads(row["provenance_json"]) if isinstance(row["provenance_json"],str) else row["provenance_json"]
        evidence = _transport(saved,request,q,provenance=provenance)
        payload = q.api_data(saved["response"])
        if not isinstance(payload.get("userMessages"),list):
            raise LegacyImportError("Cached page has no message array")
        q.as_bool(payload.get("hasMore"),"hasMore")
        for raw in payload["userMessages"]: q.message_timestamp(raw,request["cid"])
        return {"response":saved["response"],"source":{"path":str(q.checked_path(root,entry["path"])),
            "sha256":entry["sha256"],"bytes":entry["bytes"],"request_fingerprint":fingerprint,
            "transport":evidence,"provenance":provenance}}


def _month_signature(manifest, item, q):
    return q.digest(q.json_bytes({"schema":2,"scope":"month","start":manifest["start"],"end":manifest["end"],
        "query_user_nick":item["query_user_nick"],"cid":item["cid"],"user_id":item["user_id"],
        "forward":False,"cursor_order":"descending"}))


def _pages(directory, artifact_root, signature, identity, lower, upper, forward, q, m, provenance):
    checkpoint_path = directory/"checkpoint.json"
    if not checkpoint_path.is_file(): return [], {}, None
    checkpoint_bytes = checkpoint_path.read_bytes()
    state = json.loads(checkpoint_bytes)
    if state.get("signature") != signature or not isinstance(state.get("pages"),list):
        raise LegacyImportError("Legacy checkpoint signature/scope mismatch")
    _relative_artifacts(state["pages"])
    q.verify_artifacts(artifact_root,state["pages"])
    raw_prefix = directory.relative_to(artifact_root).as_posix()+"/raw/"
    cursor = lower if forward else upper-1
    stop, previous_edge, seen, page_keys, ledger, validated = None, None, set(), set(), {}, []
    direction = "ascending" if forward else "descending"
    job_key = provenance["job_key"]
    for entry in state["pages"]:
        if not entry["path"].startswith(raw_prefix) or ".." in Path(entry["path"]).parts:
            raise LegacyImportError("Checkpoint raw path is outside its old job")
        saved = q.read_json(q.checked_path(artifact_root,entry["path"]))
        request = saved.get("request")
        _request(request)
        expected = {"userNick":identity["query_user_nick"],"cid":identity["cid"],"userId":identity["user_id"],
                    "cursor":cursor,"forward":forward,"count":100,"needRecalledContent":True}
        if stop is not None or cursor in seen or not _same(request,expected):
            raise LegacyImportError("Legacy checkpoint does not form a continuous request chain")
        evidence = _transport(saved,request,q,m=m)
        fingerprint = messages._fingerprint(request,q)
        if "request_fingerprint" in saved and saved["request_fingerprint"] != fingerprint:
            raise LegacyImportError("Legacy wrapper request fingerprint mismatch")
        parsed, next_cursor, next_stop = q.parse_page(saved["response"],identity,cursor,direction,lower,upper)
        key = tuple(sorted(row["message_id"] for row in parsed))
        if key and key in page_keys: raise LegacyImportError("Legacy checkpoint repeats a complete page")
        if key: page_keys.add(key)
        times = [row["send_time_ms"] for row in parsed]
        if times:
            edge = min(times) if forward else max(times)
            if previous_edge is not None and (edge < previous_edge if forward else edge > previous_edge):
                raise LegacyImportError("Legacy pages move in the wrong time direction")
            previous_edge = edge
        for row in parsed:
            if not lower <= row["send_time_ms"] < upper: continue
            if row["message_id"] in ledger:
                messages._conflict(ledger[row["message_id"]]["raw"],row["raw"],job_key)
            else: ledger[row["message_id"]] = row
        validated.append({"fingerprint":fingerprint,"request":request,"artifact_root":artifact_root,"entry":entry,
            "provenance":{**provenance,"checkpoint_path":str(checkpoint_path),"checkpoint_sha256":q.digest(checkpoint_bytes),
                          "transport_evidence":evidence}})
        seen.add(cursor)
        cursor, stop = next_cursor, next_stop
    if not _same(state.get("next_cursor"),cursor) or state.get("stop_reason") != stop:
        raise LegacyImportError("Legacy checkpoint next cursor/stop differs from its pages")
    return validated, ledger, state


def _full_month(directory, artifact_root, manifest, item, job_key, q, m, provenance):
    path = directory/"receipt.json"
    receipt = q.read_json(path)
    expected_interval = {"start_ms":q.millis(manifest["start"]),"end_exclusive_ms":q.millis(manifest["end"]),"timezone":"Asia/Shanghai"}
    signature = _month_signature(manifest,item,q)
    if type(receipt.get("schema")) is not int or receipt["schema"] != 2 or receipt.get("scope") != "month" or receipt.get("status") != "complete" or receipt.get("signature") != signature or not _same(receipt.get("item"),item) or not _same(receipt.get("interval"),expected_interval) or receipt.get("query_user_nick") != item["query_user_nick"] or receipt.get("stop_reason") not in ("date_bound","exhausted"):
        raise LegacyImportError("Old month receipt schema/scope/item/signature differs")
    artifacts = receipt.get("artifacts")
    _relative_artifacts(artifacts)
    q.verify_artifacts(artifact_root,artifacts)
    entries = {entry["path"]:entry for entry in artifacts}
    prefix = directory.relative_to(artifact_root).as_posix()+"/"
    if not {prefix+"messages.jsonl",prefix+"checkpoint.json"} <= set(entries):
        raise LegacyImportError("Old full receipt lacks message/checkpoint artifacts")
    pages, ledger, state = _pages(directory,artifact_root,signature,item,expected_interval["start_ms"],expected_interval["end_exclusive_ms"],False,q,m,provenance)
    counts = receipt.get("counts",{})
    if state is None or state["stop_reason"] != receipt["stop_reason"] or type(counts.get("pages")) is not int or counts["pages"] != len(pages) or not pages:
        raise LegacyImportError("Old full receipt pagination is incomplete")
    if any(entries.get(page["entry"]["path"]) != page["entry"] for page in pages):
        raise LegacyImportError("Old receipt does not bind its checkpoint pages")
    data = (directory/"messages.jsonl").read_bytes()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"": lines.pop()
    if any(not line for line in lines) or type(counts.get("messages")) is not int or counts["messages"] != len(lines) or len(ledger) != len(lines):
        raise LegacyImportError("Old normalized message count differs from verified pages")
    refs, seen = [], set()
    for line in lines:
        row = json.loads(line)
        key = row.get("message_id")
        if key in seen or key not in ledger or row.get("cid") != item["cid"]:
            raise LegacyImportError("Old normalized message identity differs from raw pages")
        seen.add(key)
        parsed = q.parse_message(row["raw"],item["cid"])
        if parsed["message_id"] != key or type(row.get("send_time_ms")) is not int or row["send_time_ms"] != parsed["send_time_ms"] or not _same(row.get("content"),parsed["content"]):
            raise LegacyImportError("Old normalized content/time differs from raw")
        messages._conflict(ledger[key]["raw"],row["raw"],job_key)
        observations = row.get("raw_observations")
        if observations is not None:
            if not isinstance(observations,list) or not observations or not _same(observations[0],row["raw"]):
                raise LegacyImportError("Old raw observation lineage differs")
            for observed in observations: messages._conflict(row["raw"],observed,job_key)
        if parsed["image"] is None:
            if row.get("image") is not None or row.get("ocr") is not None: raise LegacyImportError("Unexpected old image/OCR association")
            continue
        image, ocr = row.get("image"), row.get("ocr")
        if not isinstance(image,dict) or not isinstance(ocr,dict) or not _same(image.get("source"),parsed["image"]["source"]):
            raise LegacyImportError("Old image source differs from raw content")
        image_entry, ocr_entry = entries.get(image.get("local_path")), entries.get(ocr.get("path"))
        if image_entry is None or ocr_entry is None or image.get("sha256") != image_entry["sha256"] or type(image.get("actual_bytes")) is not int or image["actual_bytes"] != image_entry["bytes"]:
            raise LegacyImportError("Old image/OCR is not bound to the complete receipt")
        ocr_result = q.read_json(q.checked_path(artifact_root,ocr_entry["path"]))
        if ocr_result.get("image_sha256") != image["sha256"] or not isinstance(ocr_result.get("frames"),list) or not _same(ocr.get("result"),ocr_result):
            raise LegacyImportError("Old local OCR/image binding differs")
        source_image = image["source"]
        normalized_url = source_image["url"]
        if normalized_url.startswith("//"): normalized_url = "https:"+normalized_url
        metadata_path = "images/"+q.digest(normalized_url.encode("utf-8"))+".json"
        if metadata_path not in entries: raise LegacyImportError("Old image URL metadata is missing")
        metadata = q.read_json(q.checked_path(artifact_root,metadata_path))
        if metadata.get("url") != normalized_url or not _same(metadata.get("artifact"),image_entry) or metadata.get("sha256") != image_entry["sha256"] or type(metadata.get("actual_bytes")) is not int or metadata["actual_bytes"] != image_entry["bytes"]:
            raise LegacyImportError("Old image URL/blob binding differs")
        identity = {"job_key":job_key,"cid":item["cid"],"message_id":key,"source_url":source_image["url"]}
        refs.append({"ref_key":hashlib.sha256(messages._canonical(identity)).hexdigest(),**identity,
            "send_time_ms":row["send_time_ms"],"query_user_nick":item["query_user_nick"],"user_id":item["user_id"],"image_source":source_image})
    if type(counts.get("image_messages")) is not int or counts["image_messages"] != len(refs):
        raise LegacyImportError("Old image count differs from complete message data")
    refs.sort(key=lambda ref:(ref["send_time_ms"],ref["message_id"]),reverse=True)
    return receipt,pages,refs


def _publish_refs(unified_root, job_key, refs, q):
    path = unified_root/"messages"/job_key/"image_refs.jsonl"
    data = b"".join(messages._canonical(ref)+b"\n" for ref in refs)
    if path.exists():
        if path.read_bytes() != data: raise LegacyImportError("Existing unified image refs differ; preserved unchanged")
    else: q.atomic_bytes(path,data)
    return path


def _offer_pages(queue, pages):
    for page in pages:
        queue.add_page(page["fingerprint"],page["request"],page["artifact_root"],page["entry"],page["provenance"])


def import_legacy(task_root, unified_root, queue, media_store, q, m):
    task_root, unified_root = Path(task_root).resolve(), Path(unified_root).resolve()
    binding = queue.binding()
    sources = []
    for source in binding["sources"]:
        path = _inside(task_root,source["path"])
        if q.file_digest(path) != source["sha256"]: raise LegacyImportError("Bound source manifest changed")
        manifest = q.read_json(path)
        if manifest.get("schema") != 2 or manifest.get("scope") != "month" or not _same({key:manifest[key] for key in binding["scope"]},binding["scope"]):
            raise LegacyImportError("Bound month manifest scope differs")
        if unified_root == path.parent or any(unified_root.is_relative_to(path.parent/name) for name in ("codex","kimi","parallel")):
            raise LegacyImportError("Unified output overlaps old collector directories")
        sources.append((path,manifest,source["sha256"]))
    summary = {"sources_verified":len(sources),"month_receipts_verified":0,"message_jobs_adopted":0,"already_complete":0,
               "partial_month_checkpoints":0,"day_jobs_verified":0,"page_registrations_checked":0,
               "image_refs_persisted":0,"image_refs_submitted":0,"media_refs_deferred":0,"unbound_directories_ignored":0}
    for manifest_path, manifest, manifest_sha in sources:
        source_root = manifest_path.parent
        by_folder = {m.month_work_id(manifest,item):item for item in manifest["items"]}
        if len(by_folder) != len(manifest["items"]): raise LegacyImportError("Old month directory identities collide")
        directories = set()
        for lane in ("codex","kimi"):
            for filename in ("receipt.json","checkpoint.json"):
                directories.update(path.parent for path in (source_root/lane/"month_items").glob("*/"+filename))
                directories.update(path.parent for path in (source_root/"parallel").glob("*/"+lane+"/month_items/*/"+filename))
        for directory in sorted(directories):
            directory = _inside(task_root,directory)
            item = by_folder.get(directory.name)
            if item is None:
                summary["unbound_directories_ignored"] += 1
                continue
            key = messages.context_key(manifest,item)
            job = queue.job(key)
            expected_mini = {"schema":2,"scope":"month",**binding["scope"]}
            if job is None or not _same(job["item"],item) or not _same(job["manifest"],expected_mini) or Path(job["source_root"]).resolve() != source_root:
                raise LegacyImportError("Old month item/scope differs from queue source")
            artifact_root = directory.parents[1]
            provenance = {"kind":"legacy_month","job_key":key,"source_manifest":str(manifest_path),"source_manifest_sha256":manifest_sha}
            receipt_path = directory/"receipt.json"
            full = receipt_path.exists() and q.read_json(receipt_path).get("status") == "complete"
            if full:
                receipt,pages,refs = _full_month(directory,artifact_root,manifest,item,key,q,m,provenance)
                summary["month_receipts_verified"] += 1
                if job["state"] == "messages_complete":
                    same_source = job.get("completion_kind") == "legacy_full" and Path(job["receipt_root"]).resolve() == artifact_root and q.checked_path(artifact_root,job["receipt_path"]) == receipt_path
                    if not same_source:
                        summary["already_complete"] += 1
                        _offer_pages(queue,pages)
                        summary["page_registrations_checked"] += len(pages)
                        continue
                    if job.get("receipt_sha256") != q.file_digest(receipt_path):
                        raise LegacyImportError("Previously adopted legacy receipt changed")
                elif job["state"] != "pending":
                    raise LegacyImportError("Do not adopt over running or failed unified work")
                _publish_refs(unified_root,key,refs,q)
                summary["image_refs_persisted"] += len(refs)
                if media_store is not None:
                    media_store.add_refs(key,refs)
                    summary["image_refs_submitted"] += len(refs)
                else: summary["media_refs_deferred"] += len(refs)
                _offer_pages(queue,pages)
                counts = {"messages":receipt["counts"]["messages"],"image_refs":len(refs),"pages":len(pages),"cached_pages":len(pages)}
                adopted = queue.adopt_verified_legacy(key,artifact_root,receipt_path,counts)
                summary["message_jobs_adopted"] += bool(adopted)
                summary["already_complete"] += not adopted
            else:
                pages,_,_ = _pages(directory,artifact_root,_month_signature(manifest,item,q),item,q.millis(manifest["start"]),q.millis(manifest["end"]),False,q,m,provenance)
                _offer_pages(queue,pages)
                summary["partial_month_checkpoints"] += 1
            summary["page_registrations_checked"] += len(pages)

        day_directories = [directory for lane in ("codex","kimi") for directory in (source_root/lane/"items").glob("*")
                           if (directory/"checkpoint.json").is_file()]
        if not day_directories: continue
        day_manifest_path = source_root/"manifest.json"
        day_manifest = q.read_json(day_manifest_path)
        if day_manifest.get("schema") != 1 or any(day_manifest.get(k) != manifest.get(k) for k in ("start","end","timezone","expected_nick")) or day_manifest.get("user_nick") != "cntaobao"+manifest["expected_nick"]:
            raise LegacyImportError("Legacy day manifest scope differs from bound month")
        day_items = {q.work_id(item):item for item in day_manifest["items"]}
        for directory in sorted(day_directories):
            directory = _inside(task_root,directory)
            item = day_items.get(directory.name)
            if item is None:
                summary["unbound_directories_ignored"] += 1
                continue
            identity = {"query_user_nick":day_manifest["user_nick"],"cid":item["cid"],"user_id":item["user_id"]}
            key = messages.context_key(manifest,identity)
            if queue.job(key) is None:
                summary["unbound_directories_ignored"] += 1
                continue
            lower, upper = q.millis(item["day"]),q.millis(item["day"])+86400000
            if lower < q.millis(manifest["start"]) or upper > q.millis(manifest["end"]):
                raise LegacyImportError("Legacy day lies outside authorized month")
            artifact_root = directory.parents[1]
            checkpoint = q.read_json(directory/"checkpoint.json")
            if not checkpoint.get("pages"): continue
            first_entry = checkpoint["pages"][0]
            q.verify_artifacts(artifact_root,[first_entry])
            first = q.read_json(q.checked_path(artifact_root,first_entry["path"]))
            _request(first.get("request"))
            forward = first["request"]["forward"]
            direction = "ascending" if forward else "descending"
            signature = q.digest(q.json_bytes({"item":item,"start":day_manifest["start"],"end":day_manifest["end"],
                "user_nick":day_manifest["user_nick"],"forward":forward,"cursor_order":direction}))
            provenance = {"kind":"legacy_day","job_key":key,"source_manifest":str(day_manifest_path),
                          "source_manifest_sha256":q.file_digest(day_manifest_path),"bound_month_manifest_sha256":manifest_sha}
            pages,_,_ = _pages(directory,artifact_root,signature,identity,lower,upper,forward,q,m,provenance)
            receipt_path = directory/"receipt.json"
            if receipt_path.exists():
                receipt = q.read_json(receipt_path)
                if receipt.get("status") == "complete":
                    if type(receipt.get("schema")) is not int or receipt["schema"] != 1 or receipt.get("signature") != signature or not _same(receipt.get("item"),item) or not _same(receipt.get("interval"),{"start_ms":lower,"end_exclusive_ms":upper,"timezone":"Asia/Shanghai"}):
                        raise LegacyImportError("Legacy day receipt signature/item/interval differs")
                    _relative_artifacts(receipt.get("artifacts"))
                    q.verify_artifacts(artifact_root,receipt["artifacts"])
            _offer_pages(queue,pages)
            summary["day_jobs_verified"] += 1
            summary["page_registrations_checked"] += len(pages)
    return summary
