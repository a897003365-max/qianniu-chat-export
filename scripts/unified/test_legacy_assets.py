import contextlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import legacy_assets as a
import media_store as media
import message_export as messages
import test_legacy_import as lf
from test_media_store import png,PROFILE

q,m=lf.q,lf.m


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.legacy=lf.LegacyTests();self.legacy.setUp()
        self.root,self.unified,self.queue=self.legacy.root,self.legacy.unified,self.legacy.queue
        self.store=media.MediaStore(self.unified,self.unified/"queue.sqlite3")
        self.url="https://example.invalid/old-success.png"

    def tearDown(self):self.legacy.tearDown()

    def partial_ref(self,url=None):
        url=url or self.url
        content={"type":7,"data":json.dumps({"url":url,"size":0})}
        with self.assertRaises(q.ExportError):
            self.legacy.old_month([lf.page([lf.raw("image-1",lf.LOWER+10,content),lf.raw("image-2",lf.LOWER+11,content)],True,lf.LOWER+5),q.ExportError("fixture stop")])

    def old_asset(self,base=None,url=None,text="legacy success",bad_ocr=False,missing_ocr=False,data=None):
        base=base or self.legacy.lane;url=url or self.url
        normalized="https:"+url if url.startswith("//") else url
        data=png() if data is None else data;image_sha=media.sha(data);url_key=media.sha(normalized.encode())
        image_path=base/"images"/(url_key+".png");image_path.parent.mkdir(parents=True,exist_ok=True);image_path.write_bytes(data)
        metadata_path=base/"images"/(url_key+".json")
        q.atomic_json(metadata_path,{"url":normalized,"artifact":q.artifact(base,image_path),"actual_bytes":len(data),"sha256":image_sha})
        ocr_path=base/"ocr"/(image_sha+".json")
        if not missing_ocr:
            result={"image_sha256":image_sha,"engine":"legacy_engine","version":"unknown-fingerprint",
                "frames":[{"frame_index":0,"width":9 if bad_ocr else 8,"height":6,"text":text,
                    "entries":[{"text":text,"confidence":0.8,"box":[[0,0],[4,0],[4,4],[0,4]]}]}]}
            q.atomic_json(ocr_path,result)
        return image_sha,image_path,ocr_path,metadata_path

    def run_import(self):return a.import_assets(self.root,self.unified,self.queue,self.store,q,m)

    def test_partial_success_reused_without_completing_message_job_or_changing_originals(self):
        self.partial_ref();image_sha,*_=self.old_asset()
        original={path:path.read_bytes() for path in self.legacy.lane.rglob("*") if path.is_file()}
        result=self.run_import()
        self.assertEqual((result["candidate_urls"],result["observed_image_messages"],result["ocr_and_blob"]),(1,2,1))
        self.assertEqual(self.queue.job(self.legacy.key)["state"],"pending")
        versions=self.store.ocr_versions(image_sha)
        self.assertEqual(len(versions),1)
        self.assertIsNone(versions[0]["profile_id"])
        self.assertEqual(versions[0]["provenance"],"imported_verified_asset_only")
        with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_refs").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM legacy_asset_sources").fetchone()[0],1)
        self.run_import()
        self.assertEqual(len(self.store.ocr_versions(image_sha)[0]["sources"]),1)
        for path,data in original.items():self.assertEqual(path.read_bytes(),data)

    def test_multiple_ocr_versions_are_preserved_and_bad_source_does_not_block_good(self):
        self.partial_ref();image_sha,*_=self.old_asset(text="first")
        self.old_asset(self.root/"parallel/kimi-01/kimi",text="second")
        self.old_asset(self.root/"parallel/api-01/codex",bad_ocr=True)
        result=self.run_import()
        self.assertEqual(result["ocr_and_blob"],2)
        self.assertEqual(result["blob_only"],1)
        self.assertEqual(result["source_failures"],1)
        self.assertEqual(len(self.store.ocr_versions(image_sha)),2)

    def test_missing_ocr_reuses_only_valid_blob_and_reports_pending_ocr(self):
        self.partial_ref();self.old_asset(missing_ocr=True)
        grounding=a.verified_refs(self.root,self.unified,self.queue,q,m)["refs"][0]["ref"]
        self.store.add_refs(self.legacy.key,[grounding])
        result=self.run_import()
        self.assertEqual((result["blob_only"],result["ocr_and_blob"]),(1,0))
        self.assertIsNone(self.store.next_download("no-repeat-download",1))
        self.assertEqual(self.store.job_media_status(self.legacy.key)["counts"]["pending_ocr"],1)

    def test_claimed_download_or_ocr_is_not_overwritten(self):
        self.partial_ref();image_sha,*_=self.old_asset()
        ref=a.verified_refs(self.root,self.unified,self.queue,q,m)["refs"][0]["ref"]
        self.store.add_refs(self.legacy.key,[ref])
        task=self.store.next_download("active-download",1)
        result=self.run_import()
        self.assertEqual(result["deferred_claimed"],1)
        self.assertEqual(self.store.claimed_tasks()[0]["claim_id"],task["claim_id"])
        self.assertEqual(self.store.ocr_versions(image_sha),[])
        Path(task["output_path"]).write_bytes(png());self.store.record_download(task)
        ocr=self.store.next_ocr(PROFILE,"active-ocr",2)
        result=self.run_import()
        self.assertEqual(result["deferred_claimed"],1)
        self.assertEqual(self.store.claimed_tasks()[0]["claim_id"],ocr["claim_id"])
        self.assertEqual(self.store.ocr_versions(image_sha),[])

    def test_protocol_relative_url_is_kept_while_only_known_transport_rule_is_used(self):
        url="//example.invalid/protocol-relative.png"
        self.partial_ref(url);self.old_asset(url=url)
        self.run_import()
        with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
            stored=conn.execute("SELECT source_url FROM media_urls").fetchone()[0]
        self.assertEqual(stored,url)

    def test_verified_messages_only_refs_can_anchor_asset_without_old_checkpoint(self):
        self.old_asset()
        content={"type":7,"data":json.dumps({"url":self.url,"size":0})}
        messages.collect_messages(self.unified,lf.MANIFEST,lf.ITEM,lf.Browser([lf.page([lf.raw("new-stage",lf.LOWER,content)])]),q)
        result=self.run_import()
        self.assertEqual((result["candidate_urls"],result["ocr_and_blob"]),(1,1))
        self.assertEqual(self.queue.job(self.legacy.key)["state"],"pending")

    def test_global_source_hash_stops_and_bad_checkpoint_does_not_supply_urls(self):
        self.partial_ref();self.old_asset()
        checkpoint=self.legacy.directory/"checkpoint.json"
        state=q.read_json(checkpoint);state["pages"][0]["sha256"]="0"*64;q.atomic_json(checkpoint,state)
        result=self.run_import()
        self.assertEqual(result["candidate_urls"],0)
        self.assertEqual(result["source_failures"],1)
        path=self.root/"month-manifest.json";path.write_bytes(path.read_bytes()+b" ")
        with self.assertRaisesRegex(a.AssetImportError,"global_source_manifest_hash"):
            self.run_import()

    def test_existing_verified_ocr_version_keeps_its_provenance(self):
        self.partial_ref();image_sha,image_path,ocr_path,metadata_path=self.old_asset()
        base=self.legacy.lane
        receipt=base/"month_items/another-complete-source/receipt.json"
        entries=[q.artifact(base,path) for path in (metadata_path,image_path,ocr_path)]
        q.atomic_json(receipt,{"status":"complete","artifacts":entries})
        index=self.root/"old-complete-index.json"
        q.atomic_json(index,{"schema":1,"scope":"completed_media_only","source_root":str(self.root),
            "receipts":[{**q.artifact(self.root,receipt),"artifact_root":base.relative_to(self.root).as_posix()}],
            "media":[{"receipt_index":0,"url_sha256":media.sha(self.url.encode()),"image_sha256":image_sha,
                      "metadata":entries[0],"image":entries[1],"ocr":entries[2]}]})
        self.store.import_verified_legacy(self.root,index,expected_index_sha256=q.file_digest(index))
        before=self.store.ocr_versions(image_sha)[0]
        self.run_import()
        versions=self.store.ocr_versions(image_sha)
        self.assertEqual(len(versions),1)
        self.assertEqual(versions[0]["provenance"],before["provenance"])
        self.assertEqual(versions[0]["profile_id"],before["profile_id"])
        self.assertEqual(len(versions[0]["sources"]),2)

    def test_message_refs_without_checkpoint_proof_are_rejected(self):
        self.old_asset()
        content={"type":7,"data":json.dumps({"url":self.url,"size":0})}
        receipt=messages.collect_messages(self.unified,lf.MANIFEST,lf.ITEM,lf.Browser([lf.page([lf.raw("new-stage",lf.LOWER,content)])]),q)
        folder=self.unified/"messages"/receipt["job_key"]
        changed=dict(receipt)
        changed["artifacts"]=[entry for entry in receipt["artifacts"] if not entry["path"].endswith("checkpoint.json")]
        q.atomic_json(folder/"message-receipt.json",changed)
        result=self.run_import()
        self.assertEqual((result["candidate_urls"],result["source_failures"]),(0,1))

    def test_url_conflicts_keep_all_assets_and_sources_independent_of_input_order(self):
        # Real diagnosis: 3 identical-URL groups have 2 verified WEBP byte versions each.
        self.partial_ref();first,*_=self.old_asset(text="first",data=png("red"))
        second,*_=self.old_asset(self.root/"parallel/kimi-01/kimi",text="second",data=png("blue"))
        verified=a.verified_refs(self.root,self.unified,self.queue,q,m)
        originals={path:path.read_bytes() for base in verified["lane_roots"] for path in base.rglob("*") if path.is_file()}
        for reverse in (False,True):
            with self.subTest(reverse=reverse):
                target=self.root/("order-reversed" if reverse else "order-original")
                store=media.MediaStore(target,target/"state.sqlite3")
                store.add_refs(self.legacy.key,[verified["refs"][0]["ref"]])
                evidence={**verified,"lane_roots":list(reversed(verified["lane_roots"])) if reverse else verified["lane_roots"]}
                with patch.object(a,"verified_refs",return_value=evidence):
                    result=a.import_assets(self.root,target,self.queue,store,q,m)
                self.assertEqual((result["conflict_urls"],result["mapped_urls"],result["asset_sources_saved"]),(1,0,2))
                self.assertEqual(result["ocr_and_blob"],2)
                with contextlib.closing(sqlite3.connect(store.db)) as conn:
                    self.assertEqual(conn.execute("SELECT state,image_sha,error_code FROM media_urls").fetchone(),("failed_review",None,"url_content_versions_conflict"))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM legacy_asset_sources").fetchone()[0],2)
                    blobs=list(conn.execute("SELECT image_sha,path,bytes FROM media_blobs"))
                    self.assertEqual({row[0] for row in blobs},{first,second})
                    for digest,path,size in blobs:self.assertEqual(media.sha(media.read_artifact(target,{"path":path,"bytes":size,"sha256":digest})),digest)
                for digest in (first,second):
                    versions=store.ocr_versions(digest)
                    self.assertEqual(len(versions),1)
                    self.assertEqual(len(versions[0]["sources"]),1)
                    media.read_artifact(target,{key:versions[0][key] for key in ("path","bytes","sha256")})
                self.assertIsNone(store.next_download("no-ambiguous-download",1))
                self.assertIsNone(store.next_ocr(PROFILE,"no-ambiguous-ocr",1))
                self.assertEqual(store.job_media_status(self.legacy.key)["counts"]["failed_review"],1)
        for path,data in originals.items():self.assertEqual(path.read_bytes(),data)
        self.assertEqual(self.queue.job(self.legacy.key)["state"],"pending")

    def test_new_conflict_removes_prior_mapping_and_cannot_be_forgotten_on_resume(self):
        self.partial_ref();first,*_=self.old_asset()
        self.run_import();prior=self.store.ocr_versions(first)[0]
        second,*_=self.old_asset(self.root/"parallel/kimi-01/kimi",data=png("blue"))
        self.assertEqual(self.run_import()["conflict_urls"],1)
        verified=a.verified_refs(self.root,self.unified,self.queue,q,m)
        with patch.object(a,"verified_refs",return_value={**verified,"lane_roots":[self.legacy.lane]}):
            self.assertEqual(self.run_import()["conflict_urls"],1)
        with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
            self.assertEqual(conn.execute("SELECT state,image_sha FROM media_urls").fetchone(),("failed_review",None))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM legacy_asset_sources").fetchone()[0],2)
        self.assertEqual(self.store.ocr_versions(first)[0],prior)
        self.assertEqual(len(self.store.ocr_versions(second)),1)

    def test_conflicting_url_does_not_block_other_blob_only_url(self):
        other="https://example.invalid/other.png"
        rows=[lf.raw("one",lf.LOWER+1,{"type":7,"data":{"url":self.url}}),
              lf.raw("two",lf.LOWER+2,{"type":7,"data":{"url":other}})]
        messages.collect_messages(self.unified,lf.MANIFEST,lf.ITEM,lf.Browser([lf.page(rows)]),q)
        self.old_asset(data=png("red"));self.old_asset(self.root/"parallel/kimi-01/kimi",data=png("blue"))
        normal,*_=self.old_asset(url=other,missing_ocr=True,data=png("green"))
        verified=a.verified_refs(self.root,self.unified,self.queue,q,m)
        self.store.add_refs(self.legacy.key,[value["ref"] for value in verified["refs"]])
        result=self.run_import()
        self.assertEqual((result["conflict_urls"],result["mapped_urls"],result["blob_only"]),(1,1,1))
        status=self.store.job_media_status(self.legacy.key)
        self.assertEqual((status["counts"]["failed_review"],status["counts"]["pending_ocr"]),(1,1))
        self.assertIsNone(self.store.next_download("already-downloaded",1))
        task=self.store.next_ocr(PROFILE,"only-unambiguous",1)
        self.assertEqual(task["image_sha256"],normal)

    def test_conflict_preserves_active_claim_and_does_not_publish_partial_group(self):
        self.partial_ref();first,*_=self.old_asset(missing_ocr=True)
        ref=a.verified_refs(self.root,self.unified,self.queue,q,m)["refs"][0]["ref"]
        self.store.add_refs(self.legacy.key,[ref]);self.run_import()
        task=self.store.next_ocr(PROFILE,"active-ocr",1)
        second,*_=self.old_asset(self.root/"parallel/kimi-01/kimi",data=png("blue"))
        result=self.run_import()
        self.assertEqual((result["deferred_claimed"],result["asset_sources_saved"],result["conflict_urls"]),(2,0,0))
        self.assertEqual(self.store.claimed_tasks()[0]["claim_id"],task["claim_id"])
        with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
            self.assertEqual(conn.execute("SELECT state,image_sha FROM media_urls").fetchone(),("complete",first))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_blobs WHERE image_sha=?",(second,)).fetchone()[0],0)

    def assert_external_mapping_stays_quarantined(self,first):
        with contextlib.closing(self.store._connect()) as conn:
            previous=dict(conn.execute("SELECT * FROM media_urls").fetchone())
            original_blob=dict(conn.execute("SELECT * FROM media_blobs WHERE image_sha=?",(first,)).fetchone())
        original_ocr=self.store.ocr_versions(first)
        second,*_=self.old_asset(data=png("blue"))
        self.assertNotEqual(first,second)
        for _ in range(2):
            result=self.run_import()
            self.assertEqual((result["conflict_urls"],result["mapped_urls"]),(1,0))
            with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
                self.assertEqual(conn.execute("SELECT state,image_sha,error_code FROM media_urls").fetchone(),("failed_review",None,"url_content_versions_conflict"))
                sources=[(row[0],json.loads(row[1])) for row in conn.execute("SELECT image_sha,source_json FROM legacy_asset_sources")]
                self.assertEqual({row[0] for row in sources},{first,second})
                self.assertEqual(len(sources),2)
                snapshot=next(value for digest,value in sources if digest==first)
                self.assertEqual(snapshot["kind"],"revoked_existing_url_mapping")
                self.assertEqual(snapshot["evidence_scope"],"previously_registered_mapping")
                self.assertEqual(snapshot["previous_mapping"],{key:previous[key] for key in ("state","image_sha","claim_id","provenance","error_code")})
                self.assertEqual(snapshot["image"],{"path":original_blob["path"],"bytes":original_blob["bytes"],"sha256":first})
                self.assertEqual(len(snapshot["ocr_versions"]),len(original_ocr))
                self.assertEqual(len(snapshot["ocr_source_keys"]),sum(len(row["sources"]) for row in original_ocr))
                media.read_artifact(self.unified,snapshot["image"])
            self.assertEqual(self.store.ocr_versions(first),original_ocr)
            self.assertIsNone(self.store.next_download("quarantined",1))
            self.assertIsNone(self.store.next_ocr(PROFILE,"quarantined",1))

    def test_prior_normal_download_a_then_only_import_b_stays_conflicted_on_resume(self):
        self.partial_ref()
        ref=a.verified_refs(self.root,self.unified,self.queue,q,m)["refs"][0]["ref"]
        self.store.add_refs(self.legacy.key,[ref])
        task=self.store.next_download("synthetic-completed-download",1)
        Path(task["output_path"]).write_bytes(png("red"))
        first=self.store.record_download(task)["image_sha256"]
        self.assert_external_mapping_stays_quarantined(first)
        with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM media_claims WHERE claim_id=?",(task["claim_id"],)).fetchone()[0],"complete")

    def test_prior_complete_index_a_then_only_import_b_retains_receipt_lineage(self):
        self.partial_ref()
        base=self.root/"prior-import/kimi"
        first,image_path,ocr_path,metadata_path=self.old_asset(base,data=png("red"))
        receipt=base/"month_items/prior/receipt.json"
        entries=[q.artifact(base,path) for path in (metadata_path,image_path,ocr_path)]
        q.atomic_json(receipt,{"status":"complete","artifacts":entries})
        index=self.root/"prior-complete-index.json"
        q.atomic_json(index,{"schema":1,"scope":"completed_media_only","source_root":str(self.root),
            "receipts":[{**q.artifact(self.root,receipt),"artifact_root":base.relative_to(self.root).as_posix()}],
            "media":[{"receipt_index":0,"url_sha256":media.sha(self.url.encode()),"image_sha256":first,
                      "metadata":entries[0],"image":entries[1],"ocr":entries[2]}]})
        self.store.import_verified_legacy(self.root,index,expected_index_sha256=q.file_digest(index))
        self.assert_external_mapping_stays_quarantined(first)
        source=self.store.ocr_versions(first)[0]["sources"][0]
        self.assertEqual(source["receipt"]["sha256"],q.file_digest(receipt))

    def test_existing_conflict_marker_is_sticky_even_without_saved_old_version(self):
        self.partial_ref();self.old_asset()
        ref=a.verified_refs(self.root,self.unified,self.queue,q,m)["refs"][0]["ref"]
        self.store.add_refs(self.legacy.key,[ref])
        with self.store._transaction() as conn:
            conn.execute("UPDATE media_urls SET state='failed_review',image_sha=NULL,error_code='url_content_versions_conflict'")
        for _ in range(2):
            self.assertEqual(self.run_import()["conflict_urls"],1)
            with contextlib.closing(sqlite3.connect(self.store.db)) as conn:
                self.assertEqual(conn.execute("SELECT state,image_sha,error_code FROM media_urls").fetchone(),("failed_review",None,"url_content_versions_conflict"))


if __name__=="__main__":unittest.main()
