from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from task_queue import TaskQueue, QueueError

HERE = Path(__file__).parent.resolve()


def manifest(items):
    return {'schema':2,'scope':'month','timezone':'Asia/Shanghai','expected_nick':'fixture:staff',
            'start':'2026-10-01','end':'2026-11-01','items':items}


def item(number, owner, day='2026-10-31'):
    return {'query_user_nick':'cntaobaofixture:staff','cid':str(number),'user_id':'owner',
            'lane':owner,'latest_seen_day':day,'display_name':'fixture'}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='queue-test-', dir=HERE)
        self.root = Path(self.temp.name).resolve()
        self.queue = TaskQueue(self.root/'state.sqlite3')

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(HERE))
        self.temp.cleanup()

    def source(self, name, items):
        directory=self.root/name;directory.mkdir()
        path=directory/'month-manifest.json';path.write_text(json.dumps(manifest(items)),encoding='utf-8')
        return path

    def test_global_newest_priority_has_no_old_batch_barrier(self):
        old=self.source('A',[item('old-c','codex','2026-10-10'),item('old-k','kimi','2026-10-10')])
        new=self.source('B',[item('new-c','codex'),item('new-k','kimi')])
        result=self.queue.initialize([old,new])
        self.assertEqual(result['owners'],{'codex':2,'kimi':2})
        first=self.queue.claim('codex','c',101)
        self.assertEqual(first['cid'],'new-c')
        self.queue.fail(first,{'kind':'conflict','changed_fields':['content'],'private_text':'not stored'})
        second=self.queue.claim('codex','c',101)
        self.assertEqual(second['cid'],'old-c')
        self.assertNotIn('private_text',self.queue.summary()['failed_review'][0]['error_json'])
        self.assertEqual(self.queue.claim('kimi','k',102)['cid'],'new-k')

    def test_concurrent_claims_are_unique_and_owner_bound(self):
        source=self.source('A',[item(i,'codex' if i%2==0 else 'kimi') for i in range(40)])
        self.queue.initialize([source])
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed=list(pool.map(lambda i:self.queue.claim('codex','w'+str(i),1000+i), range(20)))
        self.assertEqual(len({row['job_key'] for row in claimed}),20)
        self.assertTrue(all(row['owner']=='codex' for row in claimed))
        self.assertIsNone(self.queue.claim('codex','another',2000))
        self.assertIsNotNone(self.queue.claim('kimi','k',3000))

    def test_source_changes_duplicates_and_uneven_owners_are_rejected(self):
        path=self.source('A',[item(1,'codex'),item(2,'kimi')])
        self.queue.initialize([path]);self.queue.initialize([path])
        value=json.loads(path.read_text());value['items'][0]['display_name']='changed'
        path.write_text(json.dumps(value))
        with self.assertRaises(QueueError):self.queue.initialize([path])
        with self.assertRaises(QueueError):self.queue.initialize([path,path])
        uneven=self.source('uneven',[item(3,'codex'),item(4,'codex')])
        with self.assertRaises(QueueError):TaskQueue(self.root/'uneven.sqlite3').initialize([uneven])

    def test_finish_is_message_only_and_stale_claim_cannot_commit(self):
        path=self.source('A',[item(1,'codex'),item(2,'kimi')]);self.queue.initialize([path])
        task=self.queue.claim('codex','w',1)
        receipt=self.root/'message-receipt.json'
        receipt.write_text(json.dumps({'status':'complete','stage':'messages','job_key':task['job_key'],'counts':{'messages':3,'image_refs':1}}))
        self.queue.finish(task,self.root,receipt)
        with self.assertRaises(QueueError):self.queue.finish(task,self.root,receipt)
        with self.assertRaises(QueueError):self.queue.fail(task,{'kind':'late'})
        self.assertEqual(self.queue.summary()['groups'][0]['state'],'messages_complete')

    def test_failed_item_is_not_automatically_reclaimed(self):
        path=self.source('A',[item(1,'codex'),item(2,'kimi')]);self.queue.initialize([path])
        task=self.queue.claim('codex','w',1);self.queue.fail(task,{'kind':'conflict'})
        self.assertIsNone(self.queue.claim('codex','w',1))
        with self.assertRaises(QueueError):self.queue.release_reviewed(task['job_key'],'review')
        self.queue.release_reviewed(task['job_key'],'verified correction',confirmed_not_running=True)
        self.assertIsNotNone(self.queue.claim('codex','w',1))

    def test_page_cache_binds_request_and_rechecks_source(self):
        request={'userNick':'cntaobaofixture:staff','cid':'1','userId':'2','cursor':123,
                 'forward':False,'count':100,'needRecalledContent':True}
        raw=self.root/'page.json';raw.write_text(json.dumps({'request':request,'response':{'data':{}}}))
        entry={'path':'page.json','bytes':raw.stat().st_size,'sha256':hashlib.sha256(raw.read_bytes()).hexdigest()}
        self.queue.add_page('fixture-fingerprint',request,self.root,entry,{'source':'fixture'})
        saved,binding=self.queue.lookup_page('fixture-fingerprint',request)
        self.assertEqual(saved['request'],request)
        self.assertEqual(binding['sha256'],entry['sha256'])
        with self.assertRaises(QueueError):self.queue.lookup_page('fixture-fingerprint',{**request,'cid':'other'})
        raw.write_bytes(b'changed')
        with self.assertRaises(QueueError):self.queue.lookup_page('fixture-fingerprint',request)


if __name__=='__main__':unittest.main()
