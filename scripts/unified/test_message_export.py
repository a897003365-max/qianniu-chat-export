import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import message_export as e

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
import qianniu_export as q

MANIFEST = {"start":"2026-10-01","end":"2026-11-01","timezone":"Asia/Shanghai"}
ITEM = {"query_user_nick":"cntaobaofixture:staff","cid":"fixture-cid","user_id":"fixture-user"}
LOWER, UPPER = q.millis(MANIFEST["start"]), q.millis(MANIFEST["end"])


def raw(identifier,timestamp,content=None):
    return {"messageId":identifier,"sendTime":str(timestamp),"cid":{"appCid":ITEM["cid"],"domain":"cntaobao"},
        "senderId":{"appUid":"fixture-sender","domain":"cntaobao"},"receiverIds":[],"bizUnique":"first-observation",
        "content":json.dumps(content if content is not None else {"text":"fixture text","meta":{"x":1,"y":True}},ensure_ascii=False),
        "ext":'{"a":1,"b":true}'}


def page(messages,more=False,cursor=None):
    return {"api":q.MESSAGES,"v":"1.0","ret":["SUCCESS::ok"],"data":{"userMessages":messages,"hasMore":more,"nextCursor":cursor}}


class Browser:
    def __init__(self,responses):
        self.responses=list(responses)
        self.calls=[]
        self.last_metrics={"elapsed_ms":5,"transport":"fixture","Authorization":"PRIVATE_CREDENTIAL","url":"PRIVATE_URL","debug":"PRIVATE_BODY"}

    def request(self,api,data):
        self.calls.append((api,copy.deepcopy(data)))
        if not self.responses: raise AssertionError("unexpected API call")
        response=self.responses.pop(0)
        if isinstance(response,Exception):raise response
        return response


class MessageExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="message-export-test-",dir=HERE)
        self.root=Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def folder(self,item=ITEM,manifest=MANIFEST):
        return self.root/"messages"/e.context_key(manifest,item)

    def test_context_key_is_full_stable_scope_hash(self):
        key=e.context_key(MANIFEST,ITEM)
        self.assertEqual(len(key),64)
        self.assertEqual(key,e.context_key(dict(reversed(list(MANIFEST.items()))),{**ITEM,"lane":"other","display_name":"ignored"}))
        keys={key,e.context_key(MANIFEST,{**ITEM,"query_user_nick":"cntaobaofixture:other"}),
              e.context_key(MANIFEST,{**ITEM,"user_id":"other-user"}),e.context_key({**MANIFEST,"start":"2026-10-02"},ITEM)}
        self.assertEqual(len(keys),4)

    def test_100_plus_terminal_overlap_accepts_content_ext_serialization(self):
        before=raw("overlap",LOWER+100)
        after=copy.deepcopy(before)
        after.update({"bizUnique":"second-observation","readStatus":"1","content":'{"meta":{"y":true,"x":1}, "text":"fixture text"}',"ext":'{ "b":true, "a":1 }'})
        rows=[before]+[raw("neighbor-%03d"%i,LOWER+100+i) for i in range(1,100)]
        receipt=e.collect_messages(self.root,MANIFEST,ITEM,Browser([page(rows,"true",LOWER+100),page([after],"false",LOWER+100)]),q)
        self.assertEqual(receipt["counts"],{"messages":100,"image_refs":0,"pages":2,"cached_pages":0})
        self.assertEqual((receipt["schema"],receipt["stage"],receipt["completion_scope"]),(3,"messages","messages_only"))
        messages=[json.loads(line) for line in (self.folder()/"messages.jsonl").read_text(encoding="utf-8").split("\n") if line]
        overlap=next(message for message in messages if message["message_id"]=="overlap")
        self.assertEqual(overlap["raw_observations"],[before,after])
        self.assertEqual(messages[0]["send_time_ms"],LOWER+199)
        self.assertEqual(receipt["page_metrics"][0]["browser_metrics"],{"elapsed_ms":5,"transport":"fixture"})
        self.assertNotIn("PRIVATE",json.dumps(receipt["page_metrics"]))
        self.assertFalse((self.folder()/"receipt.json").exists())

    def test_genuine_body_time_sender_cid_conflicts_are_explicit_and_retained(self):
        for field,value in (("content",'{"text":"PRIVATE_CHANGED_BODY"}'),("sendTime",str(LOWER+101)),
                            ("senderId",{"appUid":"different","domain":"cntaobao"}),("cid",{"appCid":"other","domain":"cntaobao"})):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                before=raw("same",LOWER+100)
                after=copy.deepcopy(before);after[field]=value
                browser=Browser([page([before,raw("later",LOWER+200)],True,LOWER+100),page([after])])
                output=Path(temporary)
                with self.assertRaises(e.ItemConflict) as raised:
                    e.collect_messages(output,MANIFEST,ITEM,browser,q)
                error=raised.exception
                self.assertIn(field,error.changed_fields)
                self.assertNotIn("PRIVATE_CHANGED_BODY",str(error))
                failure=q.read_json(output/error.failure_file)
                self.assertEqual(failure["response"]["data"]["userMessages"][0],after)
                folder=output/"messages"/e.context_key(MANIFEST,ITEM)
                self.assertFalse((folder/"message-receipt.json").exists())
                self.assertEqual(len(q.read_json(folder/"checkpoint.json")["pages"]),1)
        other={**ITEM,"user_id":"other-context"}
        result=e.collect_messages(self.root,MANIFEST,other,Browser([page([raw("good",LOWER)])]),q)
        self.assertEqual(result["counts"]["messages"],1)

    def test_type_and_array_order_and_nested_fields_stay_strict(self):
        before=raw("same",LOWER)
        for content in ({"text":"fixture text","meta":{"x":1,"y":1}}, {"text":"fixture text","meta":{"x":1.0,"y":True}}):
            after=raw("same",LOWER,content)
            with self.assertRaises(e.ItemConflict):e._conflict(before,after,e.context_key(MANIFEST,ITEM))
        before["content"]='{"data":[1,2]}'
        after=copy.deepcopy(before);after["content"]='{"data":[2,1]}'
        with self.assertRaises(e.ItemConflict):e._conflict(before,after,"fixture")
        before["ext"]='{"nested":{"bizUnique":"a"}}'
        after=copy.deepcopy(before);after["ext"]='{"nested":{"bizUnique":"b"}}'
        with self.assertRaises(e.ItemConflict):e._conflict(before,after,"fixture")

    def test_interrupted_pagination_resumes_exact_next_cursor(self):
        first=page([raw("late",UPPER-2)],True,LOWER+10)
        with self.assertRaisesRegex(RuntimeError,"interruption"):
            e.collect_messages(self.root,MANIFEST,ITEM,Browser([first,RuntimeError("fixture interruption")]),q)
        checkpoint=q.read_json(self.folder()/"checkpoint.json")
        self.assertEqual(checkpoint["next_cursor"],LOWER+10)
        resumed=Browser([page([raw("start",LOWER),raw("outside",LOWER-1)],True,LOWER-2)])
        receipt=e.collect_messages(self.root,MANIFEST,ITEM,resumed,q)
        self.assertEqual((len(resumed.calls),resumed.calls[0][1]["cursor"]),(1,LOWER+10))
        self.assertEqual(receipt["counts"]["messages"],2)
        self.assertEqual(receipt["stop_reason"],"date_bound")
        self.assertEqual(len(list((self.folder()/"failures").glob("*.json"))),1)

    def test_verified_receipt_skip_scope_change_and_artifact_damage(self):
        receipt=e.collect_messages(self.root,MANIFEST,ITEM,Browser([page([raw("one",LOWER)])]),q)
        browser=Browser([])
        self.assertEqual(e.collect_messages(self.root,MANIFEST,ITEM,browser,q),receipt)
        self.assertFalse(browser.calls)
        with patch.object(q,"MESSAGES","different.message.api"):
            with self.assertRaisesRegex(e.MessageExportError,"scope/signature"):
                e.collect_messages(self.root,MANIFEST,ITEM,Browser([]),q)
        path=self.folder()/"messages.jsonl"
        path.write_bytes(path.read_bytes()+b"changed")
        with self.assertRaises(q.ExportError):e.collect_messages(self.root,MANIFEST,ITEM,Browser([]),q)

    def test_completed_receipt_cannot_change_interval_or_claim_extra_rows(self):
        original=e.collect_messages(self.root,MANIFEST,ITEM,Browser([page([raw("one",LOWER)])]),q)
        path=self.folder()/"message-receipt.json"
        for field in ("interval","counts"):
            changed=copy.deepcopy(original)
            if field=="interval":changed["interval"]["start_ms"]+=1
            else:changed["counts"]["messages"]+=1
            q.atomic_json(path,changed)
            with self.assertRaises(e.MessageExportError):
                e.collect_messages(self.root,MANIFEST,ITEM,Browser([]),q)
        q.atomic_json(path,original)

    def test_images_produce_all_stable_refs_without_download_or_ocr(self):
        source={"url":"https://img.alicdn.com/same.png","size":0,"width":20,"height":10}
        content={"type":7,"data":json.dumps(source)}
        browser=Browser([page([raw("image-one",LOWER,content),raw("image-two",LOWER+1,content),raw("excluded",UPPER,content)])])
        with patch.object(q,"download_image",side_effect=AssertionError("download forbidden")), patch.object(q,"OCR",side_effect=AssertionError("OCR forbidden")):
            receipt=e.collect_messages(self.root,MANIFEST,ITEM,browser,q)
        refs=[json.loads(line) for line in (self.folder()/"image_refs.jsonl").read_text(encoding="utf-8").split("\n") if line]
        self.assertEqual((receipt["counts"]["messages"],receipt["counts"]["image_refs"]),(2,2))
        self.assertEqual(len({ref["ref_key"] for ref in refs}),2)
        self.assertTrue(all(ref["source_url"]==source["url"] and ref["image_source"]==source and "status" not in ref for ref in refs))
        other={**ITEM,"query_user_nick":"cntaobaofixture:other"}
        second=e.collect_messages(self.root,MANIFEST,other,Browser([page([raw("image-one",LOWER,content)])]),q)
        self.assertNotEqual(receipt["job_key"],second["job_key"])
        second_ref=json.loads((self.folder(other)/"image_refs.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(second_ref["ref_key"],{ref["ref_key"] for ref in refs})

    def test_seed_exact_fingerprint_reuse_and_hash_failure_do_not_touch_old_pages(self):
        old=self.root/"legacy/old-page.json"
        request=e._request(e._scope(MANIFEST,ITEM),UPPER-1)
        saved={"request":request,"response":page([raw("seed",LOWER)])}
        q.atomic_json(old,saved)
        original=old.read_bytes()
        class Seeds:
            def lookup(self,requested):
                if requested!=request:return None
                if old.read_bytes()!=original:raise q.ExportError("seed hash changed")
                return {"response":saved["response"],"source":{"path":str(old),"sha256":q.digest(original),"bytes":len(original),"request_fingerprint":e._fingerprint(request,q)}}
        browser=Browser([])
        result=e.collect_messages(self.root,MANIFEST,ITEM,browser,q,seeds=Seeds())
        self.assertEqual(result["counts"]["cached_pages"],1)
        self.assertFalse(browser.calls)
        self.assertEqual(old.read_bytes(),original)
        self.assertIsNone(result["page_metrics"][0]["browser_metrics"])
        class WrongSeed:
            def lookup(self,requested):return {"response":saved["response"],"source":{"request_fingerprint":"wrong"}}
        with self.assertRaisesRegex(e.MessageExportError,"fingerprint"):
            e.collect_messages(self.root,MANIFEST,{**ITEM,"user_id":"other"},Browser([]),q,seeds=WrongSeed())

    def test_invalid_page_limits_fail_before_request_or_output(self):
        browser=Browser([])
        for value in (True,False,0,-1,1.5,"1",[],{}):
            with self.subTest(value=value), self.assertRaisesRegex(e.MessageExportError,"max_pages"):
                e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=value)
        self.assertFalse(browser.calls)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_page_limit_writes_sample_with_raw_messages_and_image_refs(self):
        source={"url":"https://img.alicdn.com/fixture.png","size":0,"width":20,"height":10}
        response=page([raw("image",UPPER-2,{"type":7,"data":source}),raw("text",LOWER+100)],True,LOWER+50)
        browser=Browser([response])
        with patch.object(q,"download_image",side_effect=AssertionError("download forbidden")), patch.object(q,"OCR",side_effect=AssertionError("OCR forbidden")):
            receipt=e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=1)
        self.assertEqual((receipt["status"],receipt["completion_scope"],receipt["stop_reason"]),("partial","messages_sample","page_limit"))
        self.assertEqual(receipt["counts"],{"messages":2,"image_refs":1,"pages":1,"cached_pages":0})
        self.assertEqual(len(browser.calls),1)
        self.assertEqual(q.read_json(self.folder()/"sample-receipt.json"),receipt)
        self.assertFalse((self.folder()/"message-receipt.json").exists())
        checkpoint=q.read_json(self.folder()/"checkpoint.json")
        self.assertIsNone(checkpoint["stop_reason"])
        self.assertEqual(checkpoint["next_cursor"],LOWER+50)
        q.verify_artifacts(self.root,receipt["artifacts"])
        saved=q.read_json(self.root/checkpoint["pages"][0]["path"])
        self.assertEqual(saved["response"],response)
        messages=[json.loads(line) for line in (self.folder()/"messages.jsonl").read_text(encoding="utf-8").splitlines()]
        refs=[json.loads(line) for line in (self.folder()/"image_refs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({message["message_id"] for message in messages},{"image","text"})
        self.assertEqual((refs[0]["message_id"],refs[0]["image_source"]),("image",source))

    def test_source_completion_at_page_limit_remains_complete(self):
        for response,stop in ((page([raw("last",LOWER)]),"exhausted"),
                              (page([raw("last",LOWER),raw("outside",LOWER-1)],True,LOWER-2),"date_bound")):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                output=Path(temporary)
                browser=Browser([response])
                receipt=e.collect_messages(output,MANIFEST,ITEM,browser,q,max_pages=1)
                folder=output/"messages"/e.context_key(MANIFEST,ITEM)
                self.assertEqual((receipt["status"],receipt["completion_scope"],receipt["stop_reason"]),("complete","messages_only",stop))
                self.assertTrue((folder/"message-receipt.json").exists())
                self.assertFalse((folder/"sample-receipt.json").exists())
                self.assertEqual((receipt["counts"]["messages"],len(browser.calls)),(1,1))

    def test_page_limit_counts_saved_pages_and_resumes_only_to_explicit_larger_limit(self):
        first=page([raw("late",UPPER-2)],True,LOWER+30)
        original=e.collect_messages(self.root,MANIFEST,ITEM,Browser([first]),q,max_pages=1)
        same_limit=Browser([])
        repeated=e.collect_messages(self.root,MANIFEST,ITEM,same_limit,q,max_pages=1)
        self.assertEqual(repeated,original)
        self.assertFalse(same_limit.calls)
        larger_limit=Browser([page([raw("middle",LOWER+20)],True,LOWER+10)])
        partial=e.collect_messages(self.root,MANIFEST,ITEM,larger_limit,q,max_pages=2)
        self.assertEqual(partial["counts"]["pages"],2)
        self.assertEqual(partial["status"],"partial")
        self.assertEqual([call[1]["cursor"] for call in larger_limit.calls],[LOWER+30])
        self.assertIsNone(q.read_json(self.folder()/"checkpoint.json")["stop_reason"])
        sample_bytes=(self.folder()/"sample-receipt.json").read_bytes()
        full=Browser([page([raw("start",LOWER)])])
        complete=e.collect_messages(self.root,MANIFEST,ITEM,full,q)
        self.assertEqual(complete["status"],"complete")
        self.assertEqual(complete["counts"]["pages"],3)
        self.assertEqual([call[1]["cursor"] for call in full.calls],[LOWER+10])
        self.assertEqual((self.folder()/"sample-receipt.json").read_bytes(),sample_bytes)
        self.assertEqual(q.read_json(self.folder()/"message-receipt.json"),complete)
        q.verify_artifacts(self.root,complete["artifacts"])

    def test_smaller_limit_than_existing_partial_pages_fails_without_mutating_evidence(self):
        responses=[page([raw("late",UPPER-2)],True,LOWER+30),page([raw("middle",LOWER+20)],True,LOWER+10)]
        e.collect_messages(self.root,MANIFEST,ITEM,Browser(responses),q,max_pages=2)
        before={path:path.read_bytes() for path in self.folder().rglob("*") if path.is_file()}
        browser=Browser([])
        with self.assertRaisesRegex(e.MessageExportError,"exceed max_pages"):
            e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=1)
        self.assertFalse(browser.calls)
        self.assertEqual({path:path.read_bytes() for path in self.folder().rglob("*") if path.is_file()},before)

    def test_validated_complete_receipt_may_exceed_new_sample_limit(self):
        responses=[page([raw("late",UPPER-2)],True,LOWER+10),page([raw("start",LOWER)])]
        complete=e.collect_messages(self.root,MANIFEST,ITEM,Browser(responses),q)
        browser=Browser([])
        self.assertEqual(e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=1),complete)
        self.assertEqual(complete["counts"]["pages"],2)
        self.assertFalse(browser.calls)

    def test_sample_receipt_cannot_claim_complete_or_replace_verified_checkpoint(self):
        e.collect_messages(self.root,MANIFEST,ITEM,Browser([page([raw("late",UPPER-2)],True,LOWER+10)]),q,max_pages=1)
        q.atomic_json(self.folder()/"sample-receipt.json",{"status":"complete","completion_scope":"messages_only"})
        browser=Browser([page([raw("start",LOWER)])])
        complete=e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=2)
        self.assertEqual((complete["status"],complete["counts"]["pages"]),("complete",2))
        self.assertEqual([call[1]["cursor"] for call in browser.calls],[LOWER+10])

    def test_page_limit_does_not_skip_saved_hash_request_or_cursor_validation(self):
        for damage in ("hash","request","cursor","checkpoint"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                output=Path(temporary)
                e.collect_messages(output,MANIFEST,ITEM,Browser([page([raw("late",UPPER-2)],True,LOWER+10)]),q,max_pages=1)
                folder=output/"messages"/e.context_key(MANIFEST,ITEM)
                checkpoint=q.read_json(folder/"checkpoint.json")
                path=output/checkpoint["pages"][0]["path"]
                if damage=="hash":
                    path.write_bytes(path.read_bytes()+b"changed")
                elif damage=="checkpoint":
                    checkpoint["next_cursor"]+=1
                else:
                    saved=q.read_json(path)
                    if damage=="request":saved["request"]["cursor"]-=1
                    else:saved["response"]["data"]["nextCursor"]=UPPER-1
                    q.atomic_json(path,saved)
                    checkpoint["pages"][0]=q.artifact(output,path)
                q.atomic_json(folder/"checkpoint.json",checkpoint)
                browser=Browser([])
                with self.assertRaises((e.MessageExportError,q.ExportError)):
                    e.collect_messages(output,MANIFEST,ITEM,browser,q,max_pages=1)
                self.assertFalse(browser.calls)
                self.assertFalse((folder/"message-receipt.json").exists())

    def test_sample_limit_preserves_current_page_parse_cursor_and_conflict_checks(self):
        original=raw("same",UPPER-2)
        changed=copy.deepcopy(original);changed["content"]='{"text":"changed fixture"}'
        wrong_cid=raw("wrong-cid",UPPER-2);wrong_cid["cid"]["appCid"]="other"
        bad_api=page([original],True,LOWER+10);bad_api["api"]="other.message.api"
        for response,error_type in ((page([wrong_cid],True,LOWER+10),q.ExportError),
                                    (page([original],True,UPPER-1),q.ExportError),
                                    (page([original,changed],True,LOWER+10),e.ItemConflict),
                                    (bad_api,e.MessageExportError)):
            with self.subTest(response=response), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                output=Path(temporary)
                browser=Browser([response])
                with self.assertRaises(error_type):
                    e.collect_messages(output,MANIFEST,ITEM,browser,q,max_pages=1)
                folder=output/"messages"/e.context_key(MANIFEST,ITEM)
                self.assertFalse((folder/"sample-receipt.json").exists())
                self.assertFalse((folder/"message-receipt.json").exists())
                self.assertEqual(len(browser.calls),1)

    def test_resume_to_larger_sample_limit_preserves_conflict_evidence(self):
        original=raw("same",UPPER-2)
        e.collect_messages(self.root,MANIFEST,ITEM,Browser([page([original],True,LOWER+10)]),q,max_pages=1)
        changed=copy.deepcopy(original);changed["content"]='{"text":"changed fixture"}'
        browser=Browser([page([changed])])
        with self.assertRaises(e.ItemConflict) as raised:
            e.collect_messages(self.root,MANIFEST,ITEM,browser,q,max_pages=2)
        self.assertEqual(q.read_json(self.root/raised.exception.failure_file)["response"],page([changed]))
        self.assertEqual(len(q.read_json(self.folder()/"checkpoint.json")["pages"]),1)
        self.assertFalse((self.folder()/"message-receipt.json").exists())
        self.assertEqual([call[1]["cursor"] for call in browser.calls],[LOWER+10])

    def test_seed_pages_count_toward_the_total_sample_limit(self):
        request=e._request(e._scope(MANIFEST,ITEM),UPPER-1)
        class Seeds:
            def lookup(self,requested):
                if requested!=request:raise AssertionError("unexpected seed page after sample limit")
                return {"response":page([raw("seed",UPPER-2)],True,LOWER+10),
                        "source":{"request_fingerprint":e._fingerprint(request,q)}}
        browser=Browser([])
        receipt=e.collect_messages(self.root,MANIFEST,ITEM,browser,q,seeds=Seeds(),max_pages=1)
        self.assertEqual((receipt["status"],receipt["counts"]["pages"],receipt["counts"]["cached_pages"]),("partial",1,1))
        self.assertFalse(browser.calls)
        self.assertIsNone(q.read_json(self.folder()/"checkpoint.json")["stop_reason"])


if __name__=="__main__":
    unittest.main()
