import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import message_export as e
import legacy_import as legacy
from task_queue import TaskQueue, QueueError

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
import qianniu_export as q
import month_support as m

ITEM={"query_user_nick":"cntaobaofixture:agent","cid":"legacy-cid","user_id":"123","display_name":"fixture-customer",
      "latest_seen_day":"2026-10-31","discovery_sources":[],"lane":"codex"}
MANIFEST={"schema":2,"scope":"month","start":"2026-10-01","end":"2026-11-01","timezone":"Asia/Shanghai",
          "expected_nick":"fixture:agent","items":[ITEM]}
LOWER,UPPER=q.millis(MANIFEST["start"]),q.millis(MANIFEST["end"])


def raw(identifier,timestamp,content=None):
    return {"messageId":identifier,"cid":{"appCid":ITEM["cid"],"domain":"cntaobao"},"sendTime":str(timestamp),
        "content":json.dumps(content or {"text":"fixture"}),"senderId":{"appUid":"buyer","domain":"cntaobao"}}


def page(rows,more=False,cursor=None):
    return {"api":q.MESSAGES,"v":"1.0","ret":["SUCCESS::ok"],"data":{"userMessages":rows,"hasMore":more,"nextCursor":cursor}}


class Browser:
    def __init__(self,values):self.values=list(values)
    def request(self,api,data):
        value=self.values.pop(0)
        if isinstance(value,Exception):raise value
        return value


class OCR:
    def recognize(self,path):return {"engine":"fixture","frames":[{"text":"fixture OCR","entries":[],"width":10,"height":10}]}


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="legacy-test-",dir=HERE)
        self.root=Path(self.temp.name)
        self.unified=self.root/"new-pipeline"
        self.manifest_path=self.root/"month-manifest.json"
        q.atomic_json(self.manifest_path,MANIFEST)
        self.queue=TaskQueue(self.unified/"queue.sqlite3")
        self.queue.initialize([self.manifest_path])
        self.key=e.context_key(MANIFEST,ITEM)
        self.lane=self.root/"parallel/codex-01/codex"
        self.directory=self.lane/"month_items"/m.month_work_id(MANIFEST,ITEM)

    def tearDown(self):self.temp.cleanup()

    def old_month(self,values=None):
        return q.export_item(self.lane,MANIFEST,ITEM,Browser(values or [page([raw("one",LOWER)])]),OCR(),False,"descending",month=True)

    def migrate(self,store=None):
        return legacy.import_legacy(self.root,self.unified,self.queue,store,q,m)

    def cache_count(self):
        with self.queue.connect() as connection:return connection.execute("SELECT COUNT(*) FROM queue_pages").fetchone()[0]

    def test_full_month_adoption_is_verified_idempotent_and_preserves_sources(self):
        self.old_month()
        originals={path:path.read_bytes() for path in self.root.rglob("*") if path.is_file() and not path.is_relative_to(self.unified)}
        summary=self.migrate()
        self.assertEqual(summary["message_jobs_adopted"],1)
        job=self.queue.job(self.key)
        self.assertEqual((job["state"],job["completion_kind"]),("messages_complete","legacy_full"))
        self.assertEqual(self.cache_count(),1)
        self.assertTrue((self.unified/"messages"/self.key/"image_refs.jsonl").exists())
        again=self.migrate()
        self.assertEqual((again["message_jobs_adopted"],again["already_complete"]),(0,1))
        for path,data in originals.items():self.assertEqual(path.read_bytes(),data)

    def test_day_receipt_is_cache_only_and_never_month_complete(self):
        item={"day":"2026-10-31","cid":ITEM["cid"],"user_id":ITEM["user_id"],"display_name":"fixture-customer","lane":"codex"}
        manifest={"schema":1,"start":MANIFEST["start"],"end":MANIFEST["end"],"timezone":"Asia/Shanghai",
                  "expected_nick":MANIFEST["expected_nick"],"user_nick":ITEM["query_user_nick"],"items":[item]}
        q.atomic_json(self.root/"manifest.json",manifest)
        q.export_item(self.root/"codex",manifest,item,Browser([page([raw("day",q.millis(item["day"]))])]),OCR(),False,"descending")
        summary=self.migrate()
        self.assertEqual((summary["day_jobs_verified"],summary["message_jobs_adopted"]),(1,0))
        self.assertEqual(self.queue.job(self.key)["state"],"pending")
        request=e._request(e._scope(MANIFEST,ITEM),UPPER-1)
        cached=legacy.PageSeeds(self.queue,q).lookup(request)
        self.assertIsNotNone(cached)
        self.assertIn("not_recorded",cached["source"]["transport"]["ecode_evidence"])

    def test_partial_month_uses_only_checkpoint_pages_and_stays_pending(self):
        with self.assertRaises(q.ExportError):
            self.old_month([page([raw("first",UPPER-5)],True,LOWER+100),q.ExportError("fixture interruption")])
        q.atomic_json(self.directory/"raw/orphan.json",{"not":"a registered page"})
        q.atomic_json(self.directory/"failures/ignored.json",{"not":"a successful page"})
        summary=self.migrate()
        self.assertEqual((summary["partial_month_checkpoints"],summary["message_jobs_adopted"]),(1,0))
        self.assertEqual(self.queue.job(self.key)["state"],"pending")
        self.assertEqual(self.cache_count(),1)

    def test_bad_artifact_or_scope_cannot_mutate_queue_or_publish_refs(self):
        self.old_month()
        path=self.directory/"messages.jsonl"
        original=path.read_bytes()
        path.write_bytes(original+b"changed")
        with self.assertRaises(q.ExportError):self.migrate()
        self.assertEqual((self.queue.job(self.key)["state"],self.cache_count()),("pending",0))
        self.assertFalse((self.unified/"messages").exists())
        path.write_bytes(original)
        receipt_path=self.directory/"receipt.json"
        receipt=q.read_json(receipt_path)
        receipt["interval"]["start_ms"]+=1
        q.atomic_json(receipt_path,receipt)
        with self.assertRaisesRegex(legacy.LegacyImportError,"scope/item/signature"):
            self.migrate()
        self.assertEqual(self.queue.job(self.key)["state"],"pending")

    def test_bound_manifest_hash_change_is_rejected_before_queue_changes(self):
        self.old_month()
        self.manifest_path.write_bytes(self.manifest_path.read_bytes()+b" ")
        with self.assertRaisesRegex(legacy.LegacyImportError,"manifest changed"):
            self.migrate()
        self.assertEqual(self.cache_count(),0)
        self.assertEqual(self.queue.job(self.key)["state"],"pending")

    def image_month(self):
        data=b"\x89PNG\r\n\x1a\nfixture"
        image={"url":"https://img.alicdn.com/fixture.png","size":0,"width":10,"height":10}
        content={"type":7,"data":json.dumps(image)}
        class Download:
            headers={"Content-Type":"image/png"}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return data
        with patch("urllib.request.urlopen",return_value=Download()):
            self.old_month([page([raw("image-1",LOWER,content),raw("image-2",LOWER+1,content)])])

    def test_refs_register_before_adoption_and_same_url_edges_are_preserved(self):
        self.image_month()
        outer=self
        class Store:
            def __init__(self):self.refs=[]
            def add_refs(self,job_key,refs):
                outer.assertEqual(outer.queue.job(job_key)["state"],"pending")
                self.refs.extend(refs)
        store=Store()
        result=self.migrate(store)
        self.assertEqual((result["message_jobs_adopted"],len(store.refs)),(1,2))
        self.assertEqual(len({ref["ref_key"] for ref in store.refs}),2)
        self.assertEqual(len({ref["source_url"] for ref in store.refs}),1)
        self.assertTrue(all(ref["job_key"]==self.key and "image_source" in ref and "status" not in ref for ref in store.refs))

    def test_media_failure_cannot_mark_messages_complete_and_none_keeps_refs(self):
        self.image_month()
        class Reject:
            def add_refs(self,job_key,refs):raise RuntimeError("fixture media rejection")
        with self.assertRaisesRegex(RuntimeError,"media rejection"):
            self.migrate(Reject())
        self.assertEqual(self.queue.job(self.key)["state"],"pending")
        result=self.migrate(None)
        self.assertEqual((result["message_jobs_adopted"],result["media_refs_deferred"]),(1,2))
        refs_path=self.unified/"messages"/self.key/"image_refs.jsonl"
        refs=[json.loads(line) for line in refs_path.read_text(encoding="utf-8").split("\n") if line]
        class Store:
            def __init__(self):self.refs=[]
            def add_refs(self,job_key,values):self.refs.extend(values)
        store=Store()
        self.migrate(store)
        self.assertEqual(store.refs,refs)

    def test_page_seeds_exact_request_and_source_hash_and_new_transport(self):
        self.old_month()
        self.migrate()
        request=e._request(e._scope(MANIFEST,ITEM),UPPER-1)
        seeds=legacy.PageSeeds(self.queue,q)
        self.assertIsNotNone(seeds.lookup(request))
        for field,value in (("userNick","cntaobaofixture:other"),("userId","other"),("cursor",UPPER-2)):
            self.assertIsNone(seeds.lookup({**request,field:value}))
        raw_path=next((self.directory/"raw").glob("*.json"))
        raw_path.write_bytes(raw_path.read_bytes()+b" ")
        with self.assertRaises(QueueError):seeds.lookup(request)
        other={**request,"cursor":LOWER+1}
        new_path=self.unified/"new-page.json"
        q.atomic_json(new_path,{"request":other,"transport":e._transport(other,q),"request_fingerprint":e._fingerprint(other,q),"response":page([raw("new",LOWER)])})
        self.queue.add_page(e._fingerprint(other,q),other,self.unified,q.artifact(self.unified,new_path),{"kind":"unified_partial"})
        self.assertEqual(seeds.lookup(other)["source"]["transport"]["ecode_evidence"],"explicit_saved_transport_with_data")

    def test_changed_partial_chain_is_rejected_without_cache_registration(self):
        with self.assertRaises(q.ExportError):
            self.old_month([page([raw("one",UPPER-2)],True,LOWER+100),q.ExportError("interrupted")])
        checkpoint_path=self.directory/"checkpoint.json"
        checkpoint=q.read_json(checkpoint_path)
        checkpoint["next_cursor"]=LOWER
        q.atomic_json(checkpoint_path,checkpoint)
        with self.assertRaisesRegex(legacy.LegacyImportError,"next cursor"):
            self.migrate()
        self.assertEqual(self.cache_count(),0)
        self.assertEqual(self.queue.job(self.key)["state"],"pending")

    def test_absolute_artifact_is_rejected_even_when_inside_old_root(self):
        self.old_month()
        receipt_path=self.directory/"receipt.json"
        receipt=q.read_json(receipt_path)
        duplicate=q.artifact(self.lane,self.directory/"messages.jsonl")
        duplicate["path"]=str(self.directory/"messages.jsonl")
        receipt["artifacts"].append(duplicate)
        q.atomic_json(receipt_path,receipt)
        with self.assertRaisesRegex(legacy.LegacyImportError,"strictly relative"):
            self.migrate()
        self.assertEqual(self.cache_count(),0)
        self.assertEqual(self.queue.job(self.key)["state"],"pending")

    def test_running_job_is_not_adopted_or_given_legacy_refs(self):
        self.old_month()
        with self.queue.connect() as connection:
            with connection:
                connection.execute("UPDATE queue_jobs SET state='running' WHERE job_key=?",(self.key,))
        with self.assertRaisesRegex(legacy.LegacyImportError,"running or failed"):
            self.migrate()
        self.assertFalse((self.unified/"messages").exists())
        self.assertEqual(self.cache_count(),0)


if __name__=="__main__":unittest.main()
