"""Month planning and verified legacy sources for the unified pipeline."""
import hashlib
import json
from pathlib import Path


REQUEST_KEYS = {"userNick", "cid", "userId", "cursor", "forward", "count", "needRecalledContent"}


def month_work_id(manifest, item):
    identity = json.dumps([item["query_user_nick"], item["cid"], item["user_id"]], ensure_ascii=False, separators=(",", ":"))
    return manifest["start"] + "__" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def request_key(request, q):
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise q.ExportError("Seed page is missing the complete request fingerprint")
    if any(not isinstance(request[key], str) or not request[key] for key in ("userNick", "cid", "userId")):
        raise q.ExportError("Seed request has invalid identity fields")
    if isinstance(request["cursor"], bool) or not isinstance(request["cursor"], int) or type(request["forward"]) is not bool or request["count"] != 100 or request["needRecalledContent"] is not True:
        raise q.ExportError("Seed request has invalid paging fields")
    return q.digest(q.json_bytes({"api": q.MESSAGES, "v": "1.0", "ecode": 0, "data": request}))


def seed_transport(saved, q):
    response = saved.get("response")
    if not isinstance(response, dict) or response.get("api") != q.MESSAGES or response.get("v") != "1.0":
        raise q.ExportError("Seed response API/version differs from the message API contract")
    transport = {"api": q.MESSAGES, "v": "1.0", "ecode": 0}
    if "transport" in saved and saved["transport"] != transport:
        raise q.ExportError("Seed transport metadata differs from the message API contract")
    return {**transport, "ecode_evidence": "saved_transport" if "transport" in saved else "frozen_daily_browser_code_constant_not_recorded_in_legacy_page"}


def plan_month(args, q):
    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise q.ExportError("Month manifest already exists; choose a new path to preserve assignments")
    wanted_days = q.days(args.start, args.end)
    base_scope = q.read_json(root/"discovery-scope.json")
    expected = base_scope.get("expected_nick")
    if not isinstance(expected, str) or not expected:
        raise q.ExportError("Discovery scope has no expected_nick")
    if base_scope.get("start") != args.start or base_scope.get("end") != args.end:
        raise q.ExportError("Requested month interval differs from the initial discovery scope")
    initial = Path(args.initial_discovery).resolve() if args.initial_discovery else root/"discovery"
    supplemental = Path(args.supplemental_discovery).resolve() if args.supplemental_discovery else root/"coverage"/"observed-filter-discovery"
    scopes, sources, capped, missing = {}, [], [], []

    def ingest(path, day, query_nick, response, request):
        payload = q.api_data(response)
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise q.ExportError("Discovery result is not an array")
        if "hasMore" in payload and q.as_bool(payload["hasMore"], "hasMore"):
            raise q.ExportError("Discovery reports uncollected pagination; retain source and resolve its contract")
        source_index = len(sources)
        source = {"path": str(path), "sha256": q.file_digest(path), "day": day,
                  "query_user_nick": query_nick, "request": request, "rows": len(rows)}
        sources.append(source)
        if len(rows) >= 500:
            capped.append(source_index)
        for row in rows:
            try:
                cid, uid = row["cid"]["appCid"], row["userID"]["appUid"]
                if not isinstance(cid, str) or not cid or not isinstance(uid, str) or not uid:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise q.ExportError("Discovery row has invalid cid or userID; no identity variant was chosen") from None
            key = (query_nick, cid, uid)
            item = scopes.setdefault(key, {"query_user_nick": query_nick, "cid": cid, "user_id": uid,
                "display_name": row.get("displayName", ""), "latest_seen_day": day, "discovery_sources": []})
            item["latest_seen_day"] = max(item["latest_seen_day"], day)
            if source_index not in item["discovery_sources"]:
                item["discovery_sources"].append(source_index)

    for day in wanted_days:
        path = initial/(day+".json")
        receipt = initial/(day+".receipt.json")
        if not path.exists():
            missing.append(day)
            continue
        if not receipt.exists() or q.read_json(receipt).get("sha256") != q.file_digest(path):
            raise q.ExportError("Initial discovery lacks a matching raw-page hash receipt: "+day)
        saved = q.read_json(path)
        expected_request = {"selfWwNick": None, "targetWwNick": None,
                            "beginDate": day.replace("-", ""), "endDate": day.replace("-", "")}
        if "request" in saved:
            if saved["request"] != expected_request:
                raise q.ExportError("Initial discovery wrapper request differs from its date/account scope")
            response = saved["response"]
        else:
            response = saved
        ingest(path, day, "cntaobao"+expected, response, expected_request)

    supplemental_queries = 0
    if supplemental.exists():
        supplement_scope = q.read_json(supplemental/"scope.json")
        if any(supplement_scope.get(key) != value for key, value in (("start", args.start), ("end", args.end), ("expected_nick", expected))):
            raise q.ExportError("Supplemental discovery identity or interval differs from initial scope")
        seen_files = set()
        for query in q.read_json(supplemental/"query-counts.json"):
            day = query.get("day")
            if day not in wanted_days:
                raise q.ExportError("Supplemental discovery query day lies outside the month")
            path = q.checked_path(supplemental, query["response_file"])
            if path in seen_files:
                raise q.ExportError("Supplemental discovery index repeats a response file")
            seen_files.add(path)
            saved = q.read_json(path)
            request = saved.get("request", {})
            query_nick = request.get("selfWwNick")
            if saved.get("api") != q.CONVERSATIONS or saved.get("v") != "1.0" or not isinstance(query_nick, str) or not query_nick.startswith("cntaobao") or request.get("beginDate") != day.replace("-", "") or request.get("endDate") != day.replace("-", "") or request.get("targetWwNick") is not None:
                raise q.ExportError("Supplemental discovery wrapper has invalid request provenance")
            if query_nick.split(":", 1)[0] != ("cntaobao"+expected).split(":", 1)[0]:
                raise q.ExportError("Supplemental discovery account belongs to a different shop")
            response = saved["response"]
            if len(q.api_data(response).get("result", [])) != query.get("count"):
                raise q.ExportError("Supplemental discovery row count differs from its query index")
            ingest(path, day, query_nick, response, request)
            supplemental_queries += 1
    items = sorted(scopes.values(), key=lambda item: (item["latest_seen_day"], item["query_user_nick"], item["cid"], item["user_id"]), reverse=True)
    for index, item in enumerate(items):
        item["lane"] = "codex" if index % 2 == 0 else "kimi"
    variants = {}
    for item in items:
        variants.setdefault(item["cid"], set()).add((item["query_user_nick"], item["user_id"]))
    manifest = {"schema": 2, "scope": "month", "timezone": "Asia/Shanghai", "start": args.start,
        "end": args.end, "expected_nick": expected, "items": items, "discovery_sources": sources,
        "coverage": {"enumeration_complete": False, "known_query_scopes": len(items),
            "known_unique_cids": len(variants), "cids_with_scope_variants": sum(len(value)>1 for value in variants.values()),
            "source_queries": len(sources), "supplemental_queries": supplemental_queries,
            "capped_source_indexes": capped, "missing_initial_days": missing,
            "unresolved": "Unknown CIDs and capped conversation enumeration are not proved complete."}}
    q.atomic_json(output, manifest)
    return {"event": "month_plan_created", "manifest": str(output), "query_scopes": len(items),
        "codex": (len(items)+1)//2, "kimi": len(items)//2, "unique_cids": len(variants),
        "capped_queries": len(capped), "enumeration_complete": False}


def validate_month(manifest, lane, q):
    if manifest.get("schema") != 2 or manifest.get("scope") != "month" or manifest.get("timezone") != "Asia/Shanghai":
        raise q.ExportError("Month manifest needs schema=2, scope=month, timezone=Asia/Shanghai")
    valid_days = set(q.days(manifest["start"], manifest["end"]))
    if not isinstance(manifest.get("expected_nick"), str) or not manifest["expected_nick"]:
        raise q.ExportError("Month manifest requires expected_nick")
    seen, selected, counts = set(), [], {"codex": 0, "kimi": 0}
    for item in manifest["items"]:
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("query_user_nick", "cid", "user_id")) or not item["query_user_nick"].startswith("cntaobao"):
            raise q.ExportError("Month item has invalid query account/cid/userId")
        if item.get("latest_seen_day") not in valid_days or item.get("lane") not in counts:
            raise q.ExportError("Month item has invalid latest_seen_day or lane")
        key = (item["query_user_nick"], item["cid"], item["user_id"])
        if key in seen:
            raise q.ExportError("Duplicate month query scope")
        seen.add(key)
        counts[item["lane"]] += 1
        if item["lane"] == lane:
            selected.append(item)
    if abs(counts["codex"]-counts["kimi"]) > 1:
        raise q.ExportError("Month lane counts differ by more than one")
    return sorted(selected, key=lambda item: (item["latest_seen_day"], item["query_user_nick"], item["cid"], item["user_id"]), reverse=True)


class Seeds:
    """Read only receipted/checkpointed daily pages and receipted media."""
    def __init__(self, root, q):
        self.root, self.q = Path(root).resolve(), q
        self.pages, self.images, self.ocr = {}, {}, {}
        for lane in ("codex", "kimi"):
            lane_root = self.root/lane
            for directory in sorted((lane_root/"items").glob("*")):
                receipt_path, checkpoint_path = directory/"receipt.json", directory/"checkpoint.json"
                if receipt_path.exists():
                    receipt = q.read_json(receipt_path)
                    if receipt.get("status") != "complete":
                        raise q.ExportError("Seed receipt is not complete")
                    entries = receipt["artifacts"]
                    pages = [entry for entry in entries if "/raw/" in entry["path"]]
                    by_path = {entry["path"]: entry for entry in entries}
                    for entry in entries:
                        relative = entry["path"]
                        if relative.startswith("images/") and relative.endswith(".json"):
                            self.images.setdefault(Path(relative).stem, []).append((lane_root, entry, by_path))
                        elif relative.startswith("ocr/") and relative.endswith(".json"):
                            self.ocr.setdefault(Path(relative).stem, []).append((lane_root, entry))
                elif checkpoint_path.exists():
                    pages = q.read_json(checkpoint_path)["pages"]
                else:
                    continue
                for entry in pages:
                    q.verify_artifacts(lane_root, [entry])
                    saved = q.read_json(q.checked_path(lane_root, entry["path"]))
                    seed_transport(saved, q)
                    key = request_key(saved.get("request"), q)
                    q.api_data(saved.get("response"))
                    self.pages.setdefault(key, (lane_root, entry))

    def lookup(self, request):
        q = self.q
        found = self.pages.get(request_key(request, q))
        if found is None:
            return None
        root, entry = found
        q.verify_artifacts(root, [entry])
        saved = q.read_json(q.checked_path(root, entry["path"]))
        transport = seed_transport(saved, q)
        if saved.get("request") != request:
            raise q.ExportError("Seed request changed after indexing")
        return {"response": saved["response"], "source": {"path": str(q.checked_path(root, entry["path"])),
                "sha256": entry["sha256"], "bytes": entry["bytes"], "request_fingerprint": request_key(request, q),
                "transport": transport}}

    def copy_verified(self, source_root, entry, destination):
        q = self.q
        parts = Path(entry["path"]).parts
        if len(parts) != 2 or parts[0] not in ("images", "ocr") or parts[1] in (".", ".."):
            raise q.ExportError("Seed media path is outside the media cache namespace")
        q.verify_artifacts(source_root, [entry])
        target = q.checked_path(destination, entry["path"])
        if target.exists():
            q.verify_artifacts(destination, [entry])
        else:
            q.atomic_bytes(target, q.checked_path(source_root, entry["path"]).read_bytes())

    def prime_image(self, destination, url):
        q = self.q
        if url.startswith("//"):
            url = "https:"+url
        key = q.digest(url.encode("utf-8"))
        candidates = self.images.get(key)
        if not candidates:
            return
        root, metadata_entry, entries = next((found for found in candidates if found[0] == destination.resolve()), candidates[0])
        q.verify_artifacts(root, [metadata_entry])
        metadata = q.read_json(q.checked_path(root, metadata_entry["path"]))
        blob = metadata.get("artifact", {})
        if not isinstance(blob.get("path"), str) or Path(blob["path"]).parent.as_posix() != "images" or Path(blob["path"]).stem != key or metadata.get("url") != url or entries.get(blob.get("path")) != blob or metadata.get("sha256") != blob.get("sha256") or metadata.get("actual_bytes") != blob.get("bytes"):
            raise q.ExportError("Seed image metadata differs from its receipt or requested URL")
        self.copy_verified(root, blob, destination)
        self.copy_verified(root, metadata_entry, destination)

    def prime_ocr(self, destination, image_sha):
        q = self.q
        candidates = self.ocr.get(image_sha)
        if not candidates:
            return
        root, entry = next((found for found in candidates if found[0] == destination.resolve()), candidates[0])
        q.verify_artifacts(root, [entry])
        result = q.read_json(q.checked_path(root, entry["path"]))
        if result.get("image_sha256") != image_sha or not isinstance(result.get("frames"), list):
            raise q.ExportError("Seed OCR differs from its image identity")
        self.copy_verified(root, entry, destination)


