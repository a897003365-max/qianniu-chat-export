from contextlib import redirect_stdout, closing
import io
import json
import re
import sqlite3
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

import pipeline as p
from message_export import context_key, ItemConflict
from task_queue import TaskQueue
from media_store import MediaStore, MediaStoreError, MediaStorageError, claim_identity
from test_media_store import png, ref, result as ocr_result, PROFILE

HERE=Path(__file__).resolve().parent

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pipeline-test-',dir=HERE)
        self.root=Path(self.temp.name).resolve();self.unified=self.root/'unified';self.unified.mkdir()
        self.q=SimpleNamespace(read_json=lambda path:json.loads(Path(path).read_text(encoding='utf-8-sig')))
        p.atomic(self.root/'codex-runtime.json',{'node':'fixture','opencli_script':'fixture','ocr_python':'fixture'})
        p.atomic(self.unified/'ocr-profile.json',PROFILE)
        items=[{'query_user_nick':'cntaobaofixture:staff','cid':str(i),'user_id':'uid',
                'lane':'codex' if i%2==0 else 'kimi','latest_seen_day':'2026-10-31'} for i in range(6)]
        m={'schema':2,'scope':'month','timezone':'Asia/Shanghai','start':'2026-10-01','end':'2026-11-01',
           'expected_nick':'fixture:staff','items':items}
        p.atomic(self.root/'month-manifest.json',m)
        self.queue=TaskQueue(self.unified/'state.sqlite3');self.queue.initialize([self.root/'month-manifest.json'])
        self.store=MediaStore(self.unified,self.unified/'state.sqlite3')

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(Path(HERE).resolve()));self.temp.cleanup()

    def run_stage(self,stage,workers=2,limit=None):
        with patch.object(p,'load_core',return_value=(self.q,object(),self.root)),patch.object(p,'make_profile',return_value=PROFILE),redirect_stdout(io.StringIO()):
            return p.run_stage(self.root,stage,'codex' if stage=='messages' else None,workers,limit)

    def complete_message(self,output,manifest,item,browser,q,**kwargs):
        key=context_key(manifest,item);folder=output/'messages'/key;folder.mkdir(parents=True,exist_ok=True)
        (folder/'image_refs.jsonl').write_text('',encoding='utf-8')
        receipt={'schema':3,'stage':'messages','status':'complete','job_key':key,
                 'counts':{'messages':2,'image_refs':0,'pages':1,'cached_pages':0},'page_metrics':[]}
        p.atomic(folder/'message-receipt.json',receipt)
        return receipt

    def test_conflict_isolated_and_message_stage_never_downloads_or_ocrs(self):
        def collect(*args,**kwargs):
            if args[2]['cid']=='0':raise ItemConflict(context_key(args[1],args[2]),'message-1',['content'])
            return self.complete_message(*args,**kwargs)
        browser=SimpleNamespace(runtime_summary={'tab':'synthetic'},last_metrics={})
        def open_browser(runtime,run_id,worker_id,expected_nick,q):
            self.assertRegex(run_id,r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
            self.assertRegex(worker_id,r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
            self.assertEqual(runtime['task_root'],str(self.root))
            return browser
        with patch.object(p.TimedBrowser,'open',side_effect=open_browser),patch.object(p,'collect_messages',side_effect=collect),patch.object(p,'download_task') as download,patch.object(p,'OCRProcess') as ocr:
            report=self.run_stage('messages')
        self.assertEqual((report['completed'],report['failed_review']),(2,1))
        self.assertEqual(report['message_totals']['messages'],4)
        self.assertEqual(report['state'],'drained_with_failed_items')
        download.assert_not_called();ocr.assert_not_called()
        self.assertEqual(len(self.queue.summary()['failed_review']),1)

    def test_auth_failure_stops_new_claims_without_consuming_entire_queue(self):
        with patch.object(p.TimedBrowser,'open',side_effect=p.BridgeError('auth_required')):
            report=self.run_stage('messages',workers=1)
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual(report['submitted'],1)
        self.assertEqual(next(x['jobs'] for x in self.queue.summary()['groups'] if x['owner']=='codex' and x['state']=='pending'),2)

    def test_local_browser_failure_retires_only_that_slot(self):
        self.check_browser_failure('cli_timeout',global_failure=False)

    def test_global_identity_failure_stops_healthy_slots_from_new_claims(self):
        self.check_browser_failure('identity_mismatch',global_failure=True)

    def test_verification_during_request_stops_new_claims_but_preserves_inflight_work(self):
        self.check_browser_failure('verification_required',global_failure=True,during_request=True)

    def check_browser_failure(self,code,*,global_failure,during_request=False):
        ready=threading.Barrier(2)
        failure_saved=threading.Event()
        original_fail=TaskQueue.fail
        opened=[]
        def fail(queue,task,error):
            original_fail(queue,task,error)
            failure_saved.set()
        def open_browser(runtime,run_id,worker_id,expected_nick,q):
            opened.append(worker_id)
            ready.wait(timeout=5)
            if worker_id=='messages-1' and not during_request:raise p.BridgeError(code)
            return SimpleNamespace(runtime_summary={'tab':'fixture'},slot=worker_id)
        def collect(*args,**kwargs):
            if during_request and args[3].slot=='messages-1':
                folder=args[0]/'messages'/context_key(args[1],args[2])
                p.atomic(folder/'checkpoint.json',{'proof':'preserve-existing-checkpoint'})
                p.atomic(folder/'failures/synthetic.json',{'proof':'preserve-request-failure'})
                raise p.BridgeError(code)
            self.assertTrue(failure_saved.wait(timeout=5))
            return self.complete_message(*args,**kwargs)
        with patch.object(p.TimedBrowser,'open',side_effect=open_browser),patch.object(p,'collect_messages',side_effect=collect),patch.object(TaskQueue,'fail',new=fail):
            report=self.run_stage('messages',workers=2)
        self.assertEqual(sorted(opened),['messages-1','messages-2'])
        self.assertEqual(report['failed_review'],1)
        self.assertEqual(report['retired_workers'],1)
        self.assertEqual(report['global_stop'],global_failure)
        self.assertEqual(report['completed'],1 if global_failure else 2)
        self.assertEqual(report['state'],'needs_attention' if global_failure else 'drained_with_failed_items')
        if during_request:
            self.assertEqual(report['submitted'],2)
            self.assertEqual(next(row['jobs'] for row in self.queue.summary()['groups'] if row['owner']=='codex' and row['state']=='pending'),1)
            checkpoints=list((self.unified/'messages').glob('*/checkpoint.json'))
            failures=list((self.unified/'messages').glob('*/failures/synthetic.json'))
            self.assertEqual(json.loads(checkpoints[0].read_bytes()),{'proof':'preserve-existing-checkpoint'})
            self.assertEqual(json.loads(failures[0].read_bytes()),{'proof':'preserve-request-failure'})
            self.assertEqual(len(list((self.unified/'messages').glob('*/message-receipt.json'))),1)

    def test_all_browser_slots_unavailable_end_with_attention(self):
        with patch.object(p.TimedBrowser,'open',side_effect=p.BridgeError('cli_timeout')):
            report=self.run_stage('messages',workers=2)
        self.assertEqual((report['submitted'],report['failed_review'],report['retired_workers']),(2,2,2))
        self.assertEqual(report['state'],'needs_attention')
        self.assertFalse(report['global_stop'])

    def test_global_limit_and_run_source_hashes(self):
        browser=SimpleNamespace(runtime_summary={'tab':'fixture'})
        with patch.object(p.TimedBrowser,'open',return_value=browser),patch.object(p,'collect_messages',side_effect=self.complete_message):
            report=self.run_stage('messages',workers=3,limit=2)
        self.assertEqual((report['submitted'],report['completed']),(2,2))
        self.assertEqual(report['source_sha256'],p.SOURCE_SHA256)
        self.assertEqual(report['source_sha256']['pipeline.py'],p.sha(p.__file__))

    def test_shared_download_and_ocr_deduplicate_across_references(self):
        self.store.add_refs('one',[ref('one',1,'https://img.alicdn.com/one.png'),ref('one',2,'https://img.alicdn.com/one.png')])
        self.store.add_refs('two',[ref('two',1,'https://img.alicdn.com/two.png')])
        data=png()
        def download(task):
            path=Path(task['output_path']);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            return {'path':str(path),'sha256':p.hashlib.sha256(data).hexdigest(),'bytes':len(data)}
        with patch.object(p,'download_task',side_effect=download) as call:
            report=self.run_stage('downloads',workers=3)
            self.assertEqual(call.call_count,2)
        self.assertEqual(report['completed'],2)
        worker=Mock();worker.process=SimpleNamespace(pid=12345);worker.recognize.side_effect=ocr_result
        with patch.object(p,'OCRProcess',return_value=worker):
            report=self.run_stage('ocr',workers=3)
        self.assertEqual(report['completed'],1)
        self.assertEqual(worker.recognize.call_count,1)
        worker.close.assert_called_once()

    def test_empty_queue_does_not_create_browser(self):
        with patch.object(p.TimedBrowser,'open') as opened:
            for _ in range(3):
                task=self.queue.claim('codex','fixture',1);self.queue.fail(task,{'kind':'fixture'})
            report=self.run_stage('messages')
        self.assertEqual(report['submitted'],0);opened.assert_not_called()

    def test_dead_ocr_worker_does_not_fail_every_remaining_image(self):
        for number,color in enumerate(('red','blue')):
            self.store.add_refs('job',[ref('job',number,'https://img.alicdn.com/'+color+'.png')])
            task=self.store.next_download('fixture',1)
            file=Path(task['output_path']);file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(png(color))
            self.store.record_download(task)
        worker=Mock();worker.process=SimpleNamespace(pid=12345)
        worker.recognize.side_effect=p.OCRWorkerUnavailable('worker ended')
        with patch.object(p,'OCRProcess',return_value=worker):
            report=self.run_stage('ocr',workers=1)
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual((report['submitted'],report['failed_review']),(1,1))
        self.assertIsNotNone(self.store.next_ocr(PROFILE,'still-pending-check',9))
        self.assertEqual(worker.recognize.call_count,1)

    def test_changed_worker_configuration_is_not_an_image_failure(self):
        with self.assertRaises(p.OCRWorkerUnavailable):
            p.OCRProcess({'ocr_python':'fixture'},self.root,self.root/'stderr.log',PROFILE)

    def test_local_ocr_failure_retires_only_that_slot(self):
        self.check_ocr_failure(global_failure=False)

    def test_global_ocr_profile_failure_stops_healthy_slots_from_new_claims(self):
        self.check_ocr_failure(global_failure=True)

    def check_ocr_failure(self,*,global_failure):
        for number,color in enumerate(('red','blue','green')):
            self.store.add_refs('job',[ref('job',number,'https://img.alicdn.com/'+color+'.png')])
            task=self.store.next_download('fixture',1)
            file=Path(task['output_path']);file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(png(color))
            self.store.record_download(task)
        ready=threading.Barrier(2)
        failure_saved=threading.Event()
        original_fail=MediaStore.fail_ocr
        created={}
        def fail(store,task,error):
            value=original_fail(store,task,error);failure_saved.set();return value
        def create(runtime,scripts,log,profile,**kwargs):
            bad=log.name=='ocr-1.stderr.log'
            worker=Mock();worker.process=SimpleNamespace(pid=12345 if bad else 12346)
            created[log.name]=worker
            ready.wait(timeout=5)
            def recognize(task):
                if bad:raise p.OCRWorkerUnavailable('fixture OCR failure',
                    code='ocr_profile_mismatch' if global_failure else 'ocr_request_timeout',global_failure=global_failure)
                self.assertTrue(failure_saved.wait(timeout=5))
                return ocr_result(task)
            worker.recognize.side_effect=recognize
            return worker
        with patch.object(p,'OCRProcess',side_effect=create),patch.object(MediaStore,'fail_ocr',new=fail):
            report=self.run_stage('ocr',workers=2)
        self.assertEqual((report['submitted'],report['completed'],report['failed_review']),
                         (2,1,1) if global_failure else (3,2,1))
        self.assertEqual(report['retired_workers'],1)
        self.assertEqual(report['state'],'needs_attention' if global_failure else 'drained_with_failed_items')
        self.assertEqual(report['global_stop'],global_failure)
        self.assertEqual(created['ocr-1.stderr.log'].recognize.call_count,1)
        self.assertEqual(created['ocr-2.stderr.log'].recognize.call_count,1 if global_failure else 2)
        self.assertEqual(self.store.job_media_status('job')['counts']['failed_review'],1)
        for worker in created.values():worker.close.assert_called_once()

    def fixture_ocr(self,body,*,timeout=0.2):
        script=self.root/'ocr_worker.py';script.write_text(body,encoding='utf-8')
        image=self.root/'synthetic-input.bin';image.write_bytes(b'synthetic bytes; not a real image')
        profile={**PROFILE,'worker_sha256':p.sha(script)}
        worker=p.OCRProcess({'ocr_python':sys.executable},self.root,self.root/'fixture.stderr.log',profile,timeout_seconds=timeout)
        task={'image_path':str(image),'image_bytes':image.stat().st_size,'image_sha256':p.sha(image),
              'profile':profile,'profile_id':p.profile_value(profile)[1]}
        return worker,task

    def test_live_nonresponding_ocr_process_is_terminated_at_deadline(self):
        worker,task=self.fixture_ocr('import sys,time\nfor line in sys.stdin:\n time.sleep(60)\n')
        try:
            self.assertIsNone(worker.process.poll())
            begin=time.perf_counter()
            with self.assertRaises(p.OCRWorkerUnavailable) as caught:worker.recognize(task)
            self.assertEqual(caught.exception.code,'ocr_request_timeout')
            self.assertFalse(caught.exception.global_failure)
            self.assertLess(time.perf_counter()-begin,6)
            self.assertIsNotNone(worker.process.poll())
            with self.assertRaises(p.OCRWorkerUnavailable):worker.recognize(task)
        finally:worker.close()

    def test_ocr_deadline_also_covers_a_blocked_stdin_write(self):
        worker,task=self.fixture_ocr('import time\ntime.sleep(60)\n')
        try:
            begin=time.perf_counter()
            with patch.object(p.json,'dumps',return_value='x'*(1024*1024)):
                with self.assertRaises(p.OCRWorkerUnavailable) as caught:worker.recognize(task)
            self.assertEqual(caught.exception.code,'ocr_request_timeout')
            self.assertLess(time.perf_counter()-begin,6)
            self.assertIsNotNone(worker.process.poll())
        finally:worker.close()

    def test_invalid_ocr_protocol_disables_process_without_reusing_it(self):
        worker,task=self.fixture_ocr('import sys\nfor line in sys.stdin:\n print("not-json",flush=True)\n',timeout=5)
        try:
            with self.assertRaises(p.OCRWorkerUnavailable) as caught:worker.recognize(task)
            self.assertEqual(caught.exception.code,'ocr_protocol_invalid')
            self.assertFalse(worker.usable)
            self.assertIsNotNone(worker.process.poll())
            with self.assertRaises(p.OCRWorkerUnavailable):worker.recognize(task)
        finally:worker.close()

    def test_actual_ocr_profile_mismatch_is_a_global_failure(self):
        worker,task=self.fixture_ocr('import sys,json\nfor line in sys.stdin:\n print(json.dumps({"ok":True,"result":{"engine":"wrong","version":"wrong"}}),flush=True)\n',timeout=5)
        try:
            with self.assertRaises(p.OCRWorkerUnavailable) as caught:worker.recognize(task)
            self.assertEqual(caught.exception.code,'ocr_profile_mismatch')
            self.assertTrue(caught.exception.global_failure)
            self.assertIsNotNone(worker.process.poll())
        finally:worker.close()

    def test_runtime_profile_drift_stops_before_claiming_ocr(self):
        with patch.object(p,'load_core',return_value=(self.q,object(),self.root)),patch.object(p,'make_profile',return_value={**PROFILE,'engine_version':'different'}),patch.object(p.MediaStore,'next_ocr') as claim:
            with self.assertRaises(p.OCRWorkerUnavailable):
                p.run_stage(self.root,'ocr',None,1,1)
        claim.assert_not_called()

    def test_download_url_scope(self):
        self.assertEqual(p.checked_image_url('//img.alicdn.com/a.png'),'https://img.alicdn.com/a.png')
        for url in ('file:///secret','http://127.0.0.1/a','http://localhost/a','https://name:password@example.com/a'):
            with self.assertRaises(ValueError):p.checked_image_url(url)

    def events(self,report):
        return [json.loads(line) for line in (self.unified/'runs'/report['run_id']/'events.jsonl').read_text(encoding='utf-8').splitlines()]

    def assert_terminal(self,report):
        directory=self.unified/'runs'/report['run_id']
        self.assertEqual(json.loads((directory/'completion.json').read_bytes()),report)
        self.assertEqual(json.loads((directory/'runtime.json').read_bytes()),report)
        progress=json.loads((directory/'progress.json').read_bytes())
        self.assertEqual(progress['state'],report['state'])
        self.assertEqual(progress['active'],0)
        self.assertEqual(report['active'],0)

    @staticmethod
    def busy_error():
        error=sqlite3.OperationalError('INJECTED_CUSTOMER_TEXT_URL_KEY_HINT_TRACE')
        error.sqlite_errorcode=sqlite3.SQLITE_BUSY
        error.sqlite_errorname='SQLITE_BUSY'
        return error

    def seed_images(self,colors=('red','blue','green')):
        for number,color in enumerate(colors):
            self.store.add_refs('job',[ref('job',number,'https://example.invalid/'+color+'.png')])
            task=self.store.next_download('seed',1)
            Path(task['output_path']).write_bytes(png(color))
            self.store.record_download(task)

    def fixture_download(self,task):
        data=png()
        Path(task['output_path']).write_bytes(data)
        return {'path':task['output_path'],'sha256':p.hashlib.sha256(data).hexdigest(),'bytes':len(data)}

    def test_real_sqlite_busy_during_claim_has_attention_terminal_without_false_success(self):
        self.store.add_refs('job',[ref('job',1,'https://example.invalid/pending.png')])
        connect=self.store._connect
        def short_timeout():
            connection=connect();connection.execute('PRAGMA busy_timeout=1');return connection
        for stage in ('downloads','ocr'):
            with self.subTest(stage=stage),closing(connect()) as writer:
                writer.execute('BEGIN IMMEDIATE')
                try:
                    with patch.object(p,'TaskQueue',return_value=self.queue),patch.object(p,'MediaStore',return_value=self.store),patch.object(self.store,'_connect',side_effect=short_timeout):
                        report=self.run_stage(stage,workers=4)
                finally:writer.rollback()
            self.assertEqual(report['state'],'needs_attention')
            self.assertEqual((report['submitted'],report['completed'],report['failed_review']),(0,0,0))
            self.assertEqual(report['unresolved_claims'],[])
            self.assertEqual(len(report['run_errors']),1)
            error=report['run_errors'][0]
            self.assertEqual(error['phase'],'claim')
            self.assertEqual(error['error'],{'kind':'storage_error','error_code':'sqlite_busy','sqlite_errorcode':5,'sqlite_errorname':'SQLITE_BUSY'})
            self.assertEqual(self.events(report)[0]['outcome'],'needs_attention')
            self.assert_terminal(report)
        with closing(connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM media_claims').fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_urls WHERE state='pending'").fetchone()[0],1)

    def test_claim_error_with_unconfirmed_commit_keeps_opaque_identity(self):
        self.store.add_refs('job',[ref('job',1,'https://example.invalid/pending.png')])
        claim=MediaStore.next_download
        def missing_ack(store,*args):
            task=claim(store,*args)
            raise MediaStorageError(self.busy_error(),task=task)
        with patch.object(MediaStore,'next_download',new=missing_ack),patch.object(p,'download_task') as download:
            report=self.run_stage('downloads',workers=3)
        download.assert_not_called()
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual(report['submitted'],0)
        identity=claim_identity(self.store.claimed_tasks()[0])
        self.assertEqual(report['unresolved_claims'],[{**identity,'worker_id':report['run_errors'][0]['worker_id'],'phase':'claim'}])
        event=self.events(report)[0]
        self.assertEqual({key:event[key] for key in identity},identity)
        self.assertNotIn('INJECTED_CUSTOMER',json.dumps(report)+json.dumps(event))
        self.assert_terminal(report)

    def test_download_record_failure_keeps_prior_completion_and_prepared_cas(self):
        self.store.add_refs('job',[ref('job',number,'https://example.invalid/'+str(number)+'.png') for number in range(3)])
        insert=MediaStore._insert_blob
        count=0
        def fail_second(connection,blob):
            nonlocal count
            count+=1
            if count==2:raise self.busy_error()
            return insert(connection,blob)
        with patch.object(p,'download_task',side_effect=self.fixture_download),patch.object(MediaStore,'_insert_blob',side_effect=fail_second),patch.object(MediaStore,'fail_download') as fail:
            report=self.run_stage('downloads',workers=1)
        fail.assert_not_called()
        self.assertEqual((report['submitted'],report['completed'],report['failed_review']),(2,1,0))
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual(len(report['unresolved_claims']),1)
        fault=report['run_errors'][0]
        self.assertEqual((fault['phase'],fault['error']['kind']),('record_download','storage_error'))
        prepared=fault['error']['prepared_artifact']
        self.assertEqual(set(prepared),{'path','bytes','sha256'})
        self.assertEqual((self.unified/prepared['path']).read_bytes(),png())
        self.assertEqual(len(self.store.claimed_tasks()),1)
        with closing(self.store._connect()) as connection:
            self.assertEqual(dict(connection.execute('SELECT state,COUNT(*) FROM media_urls GROUP BY state')),{'claimed':1,'complete':1,'pending':1})
        events=self.events(report)
        self.assertEqual([row['outcome'] for row in events],['complete','needs_attention'])
        for row in events:
            self.assertRegex(row['claim_id'],r'^[0-9a-f]{32}$')
            self.assertRegex(row['task_key'],r'^[0-9a-f]{64}$')
        self.assertNotIn('INJECTED_CUSTOMER',json.dumps(report)+json.dumps(events))
        self.assert_terminal(report)

    def test_ocr_record_failure_reports_prepared_cas_without_failing_image(self):
        self.seed_images()
        worker=Mock();worker.process=SimpleNamespace(pid=12345)
        private='INJECTED_CUSTOMER_TEXT_URL_KEY_HINT_TRACE'
        worker.recognize.side_effect=lambda task:ocr_result(task,private)
        with patch.object(p,'OCRProcess',return_value=worker),patch.object(MediaStore,'_insert_ocr',side_effect=self.busy_error()),patch.object(MediaStore,'fail_ocr') as fail:
            report=self.run_stage('ocr',workers=1)
        fail.assert_not_called();worker.close.assert_called_once()
        self.assertEqual((report['submitted'],report['completed'],report['failed_review']),(1,0,0))
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual(report['run_errors'][0]['phase'],'record_ocr')
        self.assertEqual(len(report['unresolved_claims']),1)
        prepared=report['run_errors'][0]['error']['prepared_artifact']
        self.assertEqual(set(prepared),{'path','bytes','sha256'})
        data=(self.unified/prepared['path']).read_bytes()
        self.assertEqual((len(data),p.hashlib.sha256(data).hexdigest()),(prepared['bytes'],prepared['sha256']))
        self.assertEqual(json.loads(data)['frames'][0]['text'],private)
        self.assertNotIn(private,json.dumps(report)+json.dumps(self.events(report)))
        self.assertEqual(len(self.store.claimed_tasks()),1)
        self.assertEqual(self.store.ocr_versions(self.store.claimed_tasks()[0]['image_sha256']),[])
        self.assert_terminal(report)

    def test_failure_save_error_preserves_prior_results_and_unresolved_claim(self):
        self.store.add_refs('job',[ref('job',number,'https://example.invalid/'+str(number)+'.png') for number in range(3)])
        count=0
        def download(task):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('INJECTED_CUSTOMER_TEXT_URL_KEY_HINT_TRACE')
            return self.fixture_download(task)
        with patch.object(p,'download_task',side_effect=download),patch.object(MediaStore,'fail_download',side_effect=self.busy_error()) as fail:
            report=self.run_stage('downloads',workers=1)
        fail.assert_called_once()
        self.assertEqual((report['submitted'],report['completed'],report['failed_review']),(2,1,0))
        self.assertEqual(report['state'],'needs_attention')
        self.assertEqual(report['run_errors'][0]['phase'],'save_failure')
        self.assertEqual(report['run_errors'][0]['item_error'],{'kind':'item_error','error_code':'unexpected_error'})
        self.assertEqual(len(report['unresolved_claims']),1)
        self.assertEqual(len(self.store.claimed_tasks()),1)
        with closing(self.store._connect()) as connection:
            self.assertEqual(dict(connection.execute('SELECT state,COUNT(*) FROM media_urls GROUP BY state')),{'claimed':1,'complete':1,'pending':1})
        self.assertNotIn('INJECTED_CUSTOMER',json.dumps(report)+json.dumps(self.events(report)))
        self.assert_terminal(report)

    def test_http_failure_keeps_numeric_status_and_excludes_arbitrary_diagnostics(self):
        self.store.add_refs('job',[ref('job',1,'https://example.invalid/pending.png')])
        private='INJECTED_CUSTOMER_TEXT_URL_KEY_HINT_TRACE'
        error=HTTPError('https://example.invalid/'+private,418,private,{'hint':private},None)
        with patch.object(p,'download_task',side_effect=error):
            report=self.run_stage('downloads',workers=1)
        self.assertEqual((report['completed'],report['failed_review']),(0,1))
        self.assertEqual(report['state'],'drained_with_failed_items')
        self.assertEqual(report['run_errors'],[])
        row=self.events(report)[0]
        self.assertEqual(row['error'],{'kind':'item_error','error_code':'http_error','http_status':418})
        self.assertNotIn(private,json.dumps(report)+json.dumps(row))
        self.assertNotIn('error_type',row['error'])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT error_code FROM media_urls').fetchone()[0],'http_error')
        self.assert_terminal(report)

    def test_known_metadata_conflict_isolated_with_prepared_cas_evidence(self):
        self.store.add_refs('job',[ref('job',number,'https://example.invalid/'+str(number)+'.png') for number in range(2)])
        insert=MediaStore._insert_blob
        count=0
        def conflict_first(connection,blob):
            nonlocal count
            count+=1
            if count==1:raise MediaStoreError('blob_metadata_conflict')
            return insert(connection,blob)
        with patch.object(p,'download_task',side_effect=self.fixture_download),patch.object(MediaStore,'_insert_blob',side_effect=conflict_first):
            report=self.run_stage('downloads',workers=1)
        self.assertEqual((report['submitted'],report['completed'],report['failed_review']),(2,1,1))
        self.assertEqual(report['state'],'drained_with_failed_items')
        self.assertEqual(report['unresolved_claims'],[])
        self.assertEqual(report['run_errors'],[])
        row=self.events(report)[0]
        self.assertEqual(row['error']['error_code'],'blob_metadata_conflict')
        self.assertEqual(row['error']['kind'],'item_error')
        self.assertEqual(set(row['error']['prepared_artifact']),{'path','bytes','sha256'})
        self.assertEqual(self.store.claimed_tasks(),[])
        self.assert_terminal(report)


if __name__=='__main__':unittest.main()
