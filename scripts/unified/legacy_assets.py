"""Finite asset-only recovery from verified message evidence; no network or OCR."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import uuid

import legacy_import as legacy
import message_export as message
from media_store import MediaStoreError, decode_image, validate_ocr, encoded, sha, read_artifact


class AssetImportError(RuntimeError):
    pass


def _same(left,right):
    return message._canonical(left)==message._canonical(right)


def _failure(kind,path,error,url_key=None):
    value={"kind":kind,"path":str(path),"error_type":type(error).__name__}
    if url_key is not None:value["url_key"]=url_key
    if isinstance(error,(AssetImportError,MediaStoreError)):
        value["error_code"]=str(error)
    return value


def _bound_sources(task_root,queue,q):
    binding=queue.binding()
    sources=[]
    for source in binding["sources"]:
        path=legacy._inside(task_root,source["path"])
        if q.file_digest(path)!=source["sha256"]:raise AssetImportError("global_source_manifest_hash_changed")
        manifest=q.read_json(path)
        if manifest.get("schema")!=2 or manifest.get("scope")!="month" or not _same({key:manifest[key] for key in binding["scope"]},binding["scope"]):
            raise AssetImportError("global_source_scope_mismatch")
        sources.append((path,manifest,source["sha256"]))
    return binding,sources


def _ref(scope,item,raw,q):
    parsed=q.parse_message(raw,item["cid"])
    if parsed["image"] is None:return None
    job_key=message.context_key(scope,item)
    original=parsed["image"]["source"]
    identity={"job_key":job_key,"cid":item["cid"],"message_id":parsed["message_id"],"source_url":original["url"]}
    return {"ref_key":hashlib.sha256(message._canonical(identity)).hexdigest(),**identity,
        "send_time_ms":parsed["send_time_ms"],"query_user_nick":item["query_user_nick"],"user_id":item["user_id"],"image_source":original}


def verified_refs(task_root,unified_root,queue,q,m):
    """One grounding ref per exact URL, not a replacement for per-message ref edges."""
    task_root,unified_root=Path(task_root).resolve(),Path(unified_root).resolve()
    binding,sources=_bound_sources(task_root,queue,q)
    lower,upper=q.millis(binding["scope"]["start"]),q.millis(binding["scope"]["end"])
    urls,failures,lane_roots={},[],set()
    observed_refs=0
    def add(ref,evidence):
        nonlocal observed_refs
        observed_refs+=1
        urls.setdefault(ref["source_url"],{"ref":ref,"evidence":evidence})
    for manifest_path,manifest,manifest_sha in sources:
        source_root=manifest_path.parent
        month_items={m.month_work_id(manifest,item):item for item in manifest["items"]}
        month_dirs=[]
        day_dirs=[]
        for lane in ("codex","kimi"):
            base=source_root/lane
            if base.is_dir():lane_roots.add(legacy._inside(task_root,base))
            day_dirs.extend(path.parent for path in (base/"items").glob("*/checkpoint.json"))
            month_dirs.extend(path.parent for path in (base/"month_items").glob("*/checkpoint.json"))
            for base in (source_root/"parallel").glob("*/"+lane):
                if base.is_dir():lane_roots.add(legacy._inside(task_root,base))
                month_dirs.extend(path.parent for path in (base/"month_items").glob("*/checkpoint.json"))
        specifications=[]
        for directory in sorted(set(month_dirs)):
            item=month_items.get(directory.name)
            if item is None:continue
            specifications.append((directory,item,legacy._month_signature(manifest,item,q),lower,upper,False,
                {"kind":"legacy_month_checkpoint","source_manifest":str(manifest_path),"source_manifest_sha256":manifest_sha}))
        if day_dirs:
            day_path=legacy._inside(task_root,source_root/"manifest.json")
            day_manifest=q.read_json(day_path)
            if day_manifest.get("schema")!=1 or any(day_manifest.get(k)!=binding["scope"][k] for k in binding["scope"]) or day_manifest.get("user_nick")!="cntaobao"+binding["scope"]["expected_nick"]:
                raise AssetImportError("global_day_manifest_scope_mismatch")
            day_items={q.work_id(item):item for item in day_manifest["items"]}
            for directory in sorted(set(day_dirs)):
                old=day_items.get(directory.name)
                if old is None:continue
                day_lower=q.millis(old["day"]);day_upper=day_lower+86400000
                if day_lower<lower or day_upper>upper:raise AssetImportError("global_day_outside_authorized_scope")
                identity={"query_user_nick":day_manifest["user_nick"],"cid":old["cid"],"user_id":old["user_id"]}
                try:
                    directory=legacy._inside(task_root,directory)
                    state=q.read_json(directory/"checkpoint.json")
                    if not state.get("pages"):continue
                    first=state["pages"][0];legacy._relative_artifacts([first])
                    q.verify_artifacts(directory.parents[1],[first])
                    request=q.read_json(q.checked_path(directory.parents[1],first["path"]))["request"]
                    legacy._request(request)
                    forward=request["forward"]
                    signature=q.digest(q.json_bytes({"item":old,"start":day_manifest["start"],"end":day_manifest["end"],"user_nick":day_manifest["user_nick"],"forward":forward,"cursor_order":"ascending" if forward else "descending"}))
                    specifications.append((directory,identity,signature,day_lower,day_upper,forward,
                        {"kind":"legacy_day_checkpoint","source_manifest":str(day_path),"source_manifest_sha256":q.file_digest(day_path)}))
                except Exception as error:failures.append(_failure("checkpoint",directory,error))
        for directory,item,signature,page_lower,page_upper,forward,provenance in specifications:
            try:
                directory=legacy._inside(task_root,directory)
                key=message.context_key(manifest,item)
                job=queue.job(key)
                if job is None:raise AssetImportError("checkpoint_context_not_in_queue")
                pages,_,_=legacy._pages(directory,directory.parents[1],signature,item,page_lower,page_upper,forward,q,m,{**provenance,"job_key":key})
                for page in pages:
                    path=q.checked_path(page["artifact_root"],page["entry"]["path"])
                    saved=q.read_json(path)
                    for raw in q.api_data(saved["response"])["userMessages"]:
                        timestamp=q.message_timestamp(raw,item["cid"])
                        if not lower<=timestamp<upper:continue
                        try:
                            ref=_ref(manifest,item,raw,q)
                            if ref is not None:add(ref,{"kind":"verified_checkpoint_raw","artifact_root":str(page["artifact_root"]),
                                "raw":page["entry"],"request_fingerprint":page["fingerprint"],"provenance":page["provenance"],
                                "message_id":ref["message_id"],"send_time_ms":timestamp})
                        except Exception as error:failures.append(_failure("raw_message",path,error))
            except Exception as error:failures.append(_failure("checkpoint",directory,error))

    for receipt_path in sorted((unified_root/"messages").glob("*/message-receipt.json")):
        try:
            receipt_path=legacy._inside(unified_root,receipt_path)
            key=receipt_path.parent.name
            job=queue.job(key)
            if job is None or message.context_key(job["manifest"],job["item"])!=key:raise AssetImportError("message_context_not_in_queue")
            scope=message._scope(job["manifest"],job["item"])
            initial=message._request(scope,q.millis(scope["end"])-1)
            signature=q.digest(q.json_bytes({"schema":3,"stage":"messages","scope":scope,"direction":"descending",
                "comparison":"strict-content-ext-observations.v1","initial_request":message._transport(initial,q),
                "initial_request_fingerprint":message._fingerprint(initial,q)}))
            receipt=q.read_json(receipt_path)
            if receipt.get("schema")!=3 or receipt.get("stage")!="messages" or receipt.get("status")!="complete" or receipt.get("completion_scope")!="messages_only" or receipt.get("signature")!=signature or receipt.get("job_key")!=key or not _same(receipt.get("scope"),scope):
                raise AssetImportError("message_receipt_scope_or_signature_mismatch")
            legacy._relative_artifacts(receipt.get("artifacts"));q.verify_artifacts(unified_root,receipt["artifacts"])
            entries={entry["path"]:entry for entry in receipt["artifacts"]}
            ref_path=receipt_path.parent/"image_refs.jsonl"
            message_path=receipt_path.parent/"messages.jsonl"
            checkpoint_path=receipt_path.parent/"checkpoint.json"
            for path in (ref_path,message_path,checkpoint_path):
                if path.relative_to(unified_root).as_posix() not in entries:raise AssetImportError("message_receipt_missing_ref_artifact")
            state=q.read_json(checkpoint_path)
            message._check_state(state,signature,key)
            message._valid_artifacts(state["pages"])
            for entry in state["pages"]:message._safe_page_entry(entry,receipt_path.parent,unified_root)
            expected_paths={path.relative_to(unified_root).as_posix() for path in (ref_path,message_path,checkpoint_path)}|{entry["path"] for entry in state["pages"]}
            interval={"start_ms":lower,"end_exclusive_ms":upper,"timezone":"Asia/Shanghai"}
            if set(entries)!=expected_paths or not state["pages"] or state["stop_reason"] not in ("date_bound","exhausted") or receipt.get("stop_reason")!=state["stop_reason"] or receipt.get("counts",{}).get("pages")!=len(state["pages"]) or not _same(receipt.get("interval"),interval):
                raise AssetImportError("message_receipt_core_evidence_incomplete")
            rows=[json.loads(line) for line in message_path.read_bytes().split(b"\n") if line]
            refs=[json.loads(line) for line in ref_path.read_bytes().split(b"\n") if line]
            if len(rows)!=receipt["counts"]["messages"] or len(refs)!=receipt["counts"]["image_refs"]:raise AssetImportError("message_ref_count_mismatch")
            expected={}
            for row in rows:
                raw=row["raw"]
                timestamp=q.message_timestamp(raw,job["item"]["cid"])
                if not lower<=timestamp<upper:raise AssetImportError("message_outside_authorized_scope")
                ref=_ref(job["manifest"],job["item"],raw,q)
                if ref is not None:expected[ref["ref_key"]]=ref
            if len(expected)!=len(refs) or any(not _same(expected.get(ref.get("ref_key")),ref) for ref in refs):raise AssetImportError("ref_not_bound_to_original_image_message")
            for ref in refs:add(ref,{"kind":"verified_messages_only","artifact_root":str(unified_root),
                "receipt":q.artifact(unified_root,receipt_path),"refs":entries[ref_path.relative_to(unified_root).as_posix()],
                "messages":entries[message_path.relative_to(unified_root).as_posix()],"ref_key":ref["ref_key"]})
        except Exception as error:failures.append(_failure("message_receipt",receipt_path,error))
    return {"refs":list(urls.values()),"observed_image_messages":observed_refs,"failures":failures,
        "lane_roots":sorted(lane_roots),"sources":[{"path":str(path),"sha256":digest} for path,_,digest in sources],"scope":binding["scope"]}


def _claimed(connection,url_key,image_hashes):
    url=connection.execute("SELECT state,image_sha FROM media_urls WHERE url_key=?",(url_key,)).fetchone()
    if url and url[0]=="claimed":return True
    hashes=set(image_hashes)
    if url and url[1] is not None:hashes.add(url[1])
    return any(connection.execute("SELECT 1 FROM media_ocr_tasks WHERE image_sha=? AND state='claimed' LIMIT 1",(digest,)).fetchone() for digest in hashes)


def _register(store,url,candidates):
    """Publish all validated versions before making one atomic URL decision."""
    url_key=sha(url.encode("utf-8"))
    image_hashes={candidate["source"]["image"]["sha256"] for candidate in candidates}
    with store._transaction() as connection:
        if _claimed(connection,url_key,image_hashes):return "deferred_claimed"
    blobs,artifacts={},{}
    for candidate in candidates:
        image_sha=candidate["source"]["image"]["sha256"]
        if image_sha not in blobs:
            blobs[image_sha]=store._store_blob(candidate["image_data"],candidate["info"])
        ocr_data=candidate["ocr_data"]
        if ocr_data is not None:
            ocr_sha=sha(ocr_data)
            if (image_sha,ocr_sha) not in artifacts:
                artifacts[image_sha,ocr_sha]=store._publish(f"shared/ocr/{image_sha[:2]}/{image_sha}/{ocr_sha}.json",ocr_data)
    with store._transaction() as connection:
        if _claimed(connection,url_key,image_hashes):return "deferred_claimed"
        old=connection.execute("SELECT * FROM media_urls WHERE url_key=?",(url_key,)).fetchone()
        if old and old["source_url"]!=url:raise MediaStoreError("asset_only_url_identity_conflict")
        previous_hashes={row[0] for row in connection.execute("SELECT image_sha FROM legacy_asset_sources WHERE url_key=?",(url_key,))}
        known_hashes=image_hashes|previous_hashes
        if old and old["image_sha"] is not None:known_hashes.add(old["image_sha"])
        conflict=len(known_hashes)>1 or bool(old and old["error_code"]=="url_content_versions_conflict")
        for blob in blobs.values():store._insert_blob(connection,blob)
        connection.execute("INSERT OR IGNORE INTO media_urls(url_key,source_url,state) VALUES(?,?,'pending')",(url_key,url))
        if conflict:
            if old and old["image_sha"] is not None and old["image_sha"] not in previous_hashes:
                previous_blob=connection.execute("SELECT * FROM media_blobs WHERE image_sha=?",(old["image_sha"],)).fetchone()
                snapshot={"kind":"revoked_existing_url_mapping","evidence_scope":"previously_registered_mapping",
                    "source_url_key":url_key,"artifact_root":str(store.root),
                    "image":{"path":previous_blob["path"],"bytes":previous_blob["bytes"],"sha256":old["image_sha"]},
                    "previous_mapping":{key:old[key] for key in ("state","image_sha","claim_id","provenance","error_code")},
                    "ocr_versions":[dict(row) for row in connection.execute("SELECT * FROM media_ocr_versions WHERE image_sha=? ORDER BY ocr_sha",(old["image_sha"],))],
                    "ocr_source_keys":[row[0] for row in connection.execute("SELECT source_key FROM media_ocr_sources WHERE image_sha=? ORDER BY source_key",(old["image_sha"],))]}
                serialized=encoded(snapshot)
                connection.execute("INSERT OR IGNORE INTO legacy_asset_sources VALUES(?,?,?,?,?)",(sha(serialized),url_key,old["image_sha"],serialized.decode(),dt.datetime.now(dt.timezone.utc).isoformat()))
            connection.execute("UPDATE media_urls SET state='failed_review',image_sha=NULL,provenance='imported_verified_asset_only',error_code='url_content_versions_conflict' WHERE url_key=?",(url_key,))
        elif not old or old["state"]!="complete":
            connection.execute("UPDATE media_urls SET state='complete',image_sha=?,provenance='imported_verified_asset_only',error_code=NULL WHERE url_key=?",(next(iter(known_hashes)),url_key))
        for candidate in candidates:
            source=candidate["source"];image_sha=source["image"]["sha256"]
            if candidate["ocr_data"] is not None:
                artifact=artifacts[image_sha,sha(candidate["ocr_data"])]
                existing=connection.execute("SELECT * FROM media_ocr_versions WHERE image_sha=? AND ocr_sha=?",(image_sha,artifact["sha256"])).fetchone()
                profile,provenance=(existing["profile_id"],existing["provenance"]) if existing else (None,"imported_verified_asset_only")
                store._insert_ocr(connection,image_sha,artifact,profile,provenance,source)
            serialized=encoded(source)
            connection.execute("INSERT OR IGNORE INTO legacy_asset_sources VALUES(?,?,?,?,?)",(sha(serialized),url_key,image_sha,serialized.decode(),dt.datetime.now(dt.timezone.utc).isoformat()))
    return "conflict_urls" if conflict else "mapped_urls"


def import_assets(task_root,unified_root,queue,media_store,q,m):
    task_root,unified_root=Path(task_root).resolve(),Path(unified_root).resolve()
    if media_store.root!=unified_root:raise AssetImportError("media_store_root_mismatch")
    verified=verified_refs(task_root,unified_root,queue,q,m)
    with media_store._transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS legacy_asset_sources(
            source_key TEXT PRIMARY KEY,url_key TEXT NOT NULL REFERENCES media_urls(url_key),
            image_sha TEXT NOT NULL REFERENCES media_blobs(image_sha),source_json TEXT NOT NULL,verified_at TEXT NOT NULL)""")
    failures=list(verified["failures"])
    summary={"scope":"asset_only","candidate_urls":len(verified["refs"]),"observed_image_messages":verified["observed_image_messages"],
        "matching_metadata_sources":0,"ocr_and_blob":0,"blob_only":0,"deferred_claimed":0,"urls_without_metadata":0,
        "mapped_urls":0,"conflict_urls":0,"asset_sources_saved":0,
        "message_jobs_changed":0,"network_calls":0,"ocr_inference_calls":0}
    infos={}
    for grounding in verified["refs"]:
        url=grounding["ref"]["source_url"]
        normalized="https:"+url if url.startswith("//") else url
        key=sha(normalized.encode("utf-8"));found=False;candidates=[]
        for base in verified["lane_roots"]:
            metadata_path=base/"images"/(key+".json")
            try:
                metadata_path=q.checked_path(base,"images/"+key+".json")
                if not metadata_path.is_file():continue
                found=True;summary["matching_metadata_sources"]+=1
                metadata_entry=q.artifact(base,metadata_path)
                metadata=json.loads(read_artifact(base,metadata_entry))
                image_entry=metadata.get("artifact")
                if not isinstance(image_entry,dict) or not isinstance(image_entry.get("path"),str) or Path(image_entry["path"]).parent.as_posix()!="images" or Path(image_entry["path"]).stem!=key or metadata.get("url")!=normalized or metadata.get("sha256")!=image_entry.get("sha256") or type(metadata.get("actual_bytes")) is not int or metadata["actual_bytes"]!=image_entry.get("bytes"):
                    raise AssetImportError("metadata_url_blob_binding_mismatch")
                image_data=read_artifact(base,image_entry)
                image_sha=sha(image_data)
                if image_sha not in infos:infos[image_sha]=decode_image(image_data)
                info=infos[image_sha]
                ocr_path=base/"ocr"/(image_sha+".json")
                ocr_entry,ocr_data=None,None
                try:
                    ocr_path=q.checked_path(base,"ocr/"+image_sha+".json")
                    if not ocr_path.is_file():raise AssetImportError("old_ocr_missing")
                    ocr_entry=q.artifact(base,ocr_path)
                    ocr_data=read_artifact(base,ocr_entry)
                    validate_ocr(json.loads(ocr_data),image_sha,info["frames"])
                except Exception as error:
                    failures.append(_failure("ocr_source",ocr_path,error,sha(url.encode("utf-8"))))
                    ocr_data=None
                source={"kind":"imported_verified_asset_only","completion_scope":"asset_only","source_url_key":sha(url.encode("utf-8")),
                    "artifact_root":str(base),"metadata":metadata_entry,"image":image_entry,"ocr":ocr_entry if ocr_data is not None else None,
                    "message_evidence":grounding["evidence"],"grounding_ref":grounding["ref"],
                    "transport_rule":"prefix_https_for_protocol_relative_url" if url.startswith("//") else "exact_original_url"}
                candidates.append({"image_data":image_data,"info":info,"ocr_data":ocr_data,"source":source})
            except Exception as error:
                failures.append(_failure("asset_source",metadata_path,error,sha(url.encode("utf-8"))))
        if candidates:
            try:
                outcome=_register(media_store,url,candidates)
                if outcome=="deferred_claimed":summary[outcome]+=len(candidates)
                else:
                    summary[outcome]+=1;summary["asset_sources_saved"]+=len(candidates)
                    for candidate in candidates:summary["ocr_and_blob" if candidate["ocr_data"] is not None else "blob_only"]+=1
            except Exception as error:
                failures.append(_failure("url_registration",candidates[0]["source"]["artifact_root"],error,sha(url.encode("utf-8"))))
        if not found:summary["urls_without_metadata"]+=1
    report={"schema":1,"created_at":dt.datetime.now(dt.timezone.utc).isoformat(),"sources":verified["sources"],"authorized_scope":verified["scope"],
            "summary":{**summary,"source_failures":len(failures)},"failures":failures,"old_job_completion_not_inferred":True}
    path=unified_root/"legacy-assets"/(dt.datetime.now(dt.timezone.utc).strftime("report-%Y-%m-%d_%H%M%S_")+uuid.uuid4().hex[:8]+".json")
    q.atomic_json(path,report)
    return {**report["summary"],"report_path":str(path)}
