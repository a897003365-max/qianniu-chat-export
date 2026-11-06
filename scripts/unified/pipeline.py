"""Finite independent stages: message jobs, shared downloads, shared OCR. No automatic retries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
import uuid

import legacy_import
from message_export import collect_messages, ItemConflict
from media_store import MediaStore, MediaStorageError, claim_identity, safe_media_error, profile_value
from task_queue import TaskQueue, now, sha
from timed_bridge import TimedBrowser, BridgeError


SOURCE_PATHS = {'pipeline.py': Path(__file__).resolve(),
    **{name+'.py': Path(sys.modules[name].__file__).resolve() for name in (
        'message_export', 'media_store', 'ocr_content', 'task_queue', 'timed_bridge', 'legacy_import')}}
SOURCE_SHA256 = {name: sha(path) for name,path in SOURCE_PATHS.items()}
GLOBAL_BRIDGE_ERRORS = frozenset(('auth_required', 'verification_required', 'identity_failed', 'identity_mismatch',
    'invalid_runtime', 'invalid_identity', 'registry_invalid', 'process_mismatch'))


def load_core(task_root):
    sync = json.loads((task_root/'skill-sync.json').read_text(encoding='utf-8-sig'))
    scripts = Path(sync['master'])/'scripts'
    pins = {Path(row['path']).as_posix(): row['sha256'] for row in sync['files']}
    if len(pins) != len(sync['files']):
        raise RuntimeError('Frozen source manifest repeats a path')
    required = {**{'scripts/unified/'+name: path for name,path in SOURCE_PATHS.items()},
        **{'scripts/'+name: scripts/name for name in ('qianniu_export.py', 'month_support.py', 'ocr_worker.py')}}
    for relative,path in required.items():
        if relative not in pins or sha(path) != pins[relative]:
            raise RuntimeError('Frozen source missing or changed: '+relative)
    modules = []
    for name in ('qianniu_export.py', 'month_support.py'):
        path = scripts/name
        spec = importlib.util.spec_from_file_location('pipeline_'+path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return (*modules, scripts)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name+'.part')
    with part.open('w',encoding='utf-8',newline='\n') as handle:
        json.dump(value,handle,ensure_ascii=False,sort_keys=True,indent=2)
        handle.write('\n');handle.flush();os.fsync(handle.fileno())
    part.replace(path)


def make_profile(runtime, scripts):
    probe = """import json,hashlib,importlib.metadata as m
from pathlib import Path
import rapidocr_onnxruntime as r
base=Path(r.__file__).parent
models=[{'name':str(p.relative_to(base)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(base.rglob('*.onnx'))]
print(json.dumps({'version':m.version('rapidocr_onnxruntime'),'models':models,'ort':m.version('onnxruntime'),'pillow':m.version('Pillow'),'numpy':m.version('numpy')}))
"""
    result = subprocess.run([runtime['ocr_python'],'-X','utf8','-B','-c',probe],capture_output=True,text=True,encoding='utf-8',timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Cannot verify OCR runtime profile')
    metadata = json.loads(result.stdout)
    profile = {'engine':'rapidocr_onnxruntime','engine_version':metadata['version'],
        'worker_sha256':sha(scripts/'ocr_worker.py'),'models':metadata['models'],
        'parameters':{'opencv_threads':1,'intra_op_num_threads':1,'inter_op_num_threads':1,'all_frames':True,
                      'onnxruntime_version':metadata['ort'],'pillow_version':metadata['pillow'],'numpy_version':metadata['numpy']}}
    return profile_value(profile)[0]


def checked_image_url(url):
    transport = 'https:'+url if url.startswith('//') else url
    parsed = urlsplit(transport)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('unsupported_image_url')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'):
        raise ValueError('non_public_image_host')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError('non_public_image_host')
    return transport


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        checked_image_url(newurl)
        return super().redirect_request(request,fp,code,message,headers,newurl)


def download_task(task):
    url = checked_image_url(task['source_url'])
    output = Path(task['output_path'])
    try:
        output.parent.mkdir(parents=True,exist_ok=True)
    except OSError as error:
        raise MediaStorageError(error,task=task) from None
    request = urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://market.m.taobao.com/'})
    digest = hashlib.sha256();total=0
    with urllib.request.build_opener(ImageRedirect()).open(request,timeout=90) as response:
        file_operation=True
        try:
            with output.open('xb') as handle:
                while True:
                    file_operation=False
                    data=response.read(256*1024)
                    file_operation=True
                    if not data:break
                    handle.write(data);digest.update(data);total+=len(data)
                handle.flush();os.fsync(handle.fileno())
        except OSError as error:
            if file_operation:raise MediaStorageError(error,task=task) from None
            raise
    return {'path':str(output),'sha256':digest.hexdigest(),'bytes':total}


class OCRWorkerUnavailable(RuntimeError):
    def __init__(self, message, *, code='ocr_worker_unavailable', global_failure=False):
        super().__init__(message)
        self.code = code
        self.global_failure = global_failure


def safe_stage_error(error):
    if isinstance(error, HTTPError):
        value = {'error_code':'http_error'}
        if type(error.code) is int and 100 <= error.code <= 599:
            value['http_status'] = error.code
        return value
    if isinstance(error, OCRWorkerUnavailable):
        codes = ('ocr_worker_unavailable','ocr_request_timeout','ocr_protocol_invalid','ocr_profile_mismatch','ocr_termination_failed')
        return {'error_code':error.code if error.code in codes else 'ocr_worker_unavailable'}
    return safe_media_error(error)


class OCRProcess:
    def __init__(self, runtime, scripts, log, profile, *, timeout_seconds=600):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('OCR timeout must be positive and finite')
        self.timeout_seconds = timeout_seconds
        self.usable = True
        self._io_thread = None
        try:
            if sha(scripts/'ocr_worker.py') != profile['worker_sha256']:
                raise OCRWorkerUnavailable('OCR worker changed', global_failure=True)
            self.stderr = log.open('ab')
        except OSError:
            raise OCRWorkerUnavailable('OCR worker configuration unavailable', global_failure=True) from None
        try:
            self.process = subprocess.Popen([runtime['ocr_python'],'-X','utf8','-B',str(scripts/'ocr_worker.py')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,text=True,encoding='utf-8',bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            self.stderr.close()
            raise OCRWorkerUnavailable('OCR process could not start', global_failure=True) from None

    def _terminate(self):
        """Only terminate this object's child; never discover or signal other PIDs."""
        self.usable = False
        if self.process.poll() is None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    raise OCRWorkerUnavailable('OCR process could not terminate',
                        code='ocr_termination_failed',global_failure=True) from None

    def _unavailable(self, message, *, code='ocr_worker_unavailable', global_failure=False):
        self._terminate()
        raise OCRWorkerUnavailable(message, code=code, global_failure=global_failure)

    def recognize(self, task):
        image = Path(task['image_path'])
        try:
            unchanged=image.stat().st_size == task['image_bytes'] and sha(image) == task['image_sha256']
        except OSError as error:
            raise MediaStorageError(error,task=task) from None
        if not unchanged:
            raise RuntimeError('OCR input changed')
        if not self.usable or self.process.poll() is not None:
            raise OCRWorkerUnavailable('OCR process ended')
        # A single deadline covers both a blocked pipe write and a blocked response read.
        done = threading.Event()
        response = {}
        deadline = time.perf_counter()+self.timeout_seconds
        def exchange():
            try:
                self.process.stdin.write(json.dumps({'path':str(image)},ensure_ascii=True)+'\n')
                self.process.stdin.flush()
                response['line'] = self.process.stdout.readline()
            except (OSError, ValueError, UnicodeError):
                response['pipe_failed'] = True
            finally:
                response['finished_at'] = time.perf_counter()
                done.set()
        self._io_thread = threading.Thread(target=exchange, daemon=True)
        self._io_thread.start()
        if not done.wait(max(0,deadline-time.perf_counter())) or response['finished_at']>deadline:
            self._unavailable('OCR request timed out', code='ocr_request_timeout')
        if response.get('pipe_failed'):
            self._unavailable('OCR pipe failed')
        line = response.get('line')
        if not line:
            self._unavailable('OCR response missing')
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            self._unavailable('OCR response protocol differs', code='ocr_protocol_invalid')
        if not isinstance(value,dict) or type(value.get('ok')) is not bool:
            self._unavailable('OCR response protocol differs', code='ocr_protocol_invalid')
        if value.get('ok') is not True:
            raise RuntimeError('OCR recognition failed')
        result=value.get('result')
        if not isinstance(result,dict):
            self._unavailable('OCR response protocol differs', code='ocr_protocol_invalid')
        if result.get('engine') != task['profile']['engine'] or result.get('version') != task['profile']['engine_version']:
            self._unavailable('OCR runtime profile differs', code='ocr_profile_mismatch', global_failure=True)
        return {**result,'image_sha256':task['image_sha256'],'profile_id':task['profile_id']}

    def close(self):
        # Stop before closing pipes: close() can wait on the blocked I/O thread's lock.
        self._terminate()
        if self._io_thread is not None:
            self._io_thread.join(timeout=2)
        if self._io_thread is None or not self._io_thread.is_alive():
            for stream in (self.process.stdin, self.process.stdout):
                try:stream.close()
                except OSError:pass
        self.stderr.close()


def run_stage(task_root, stage, owner, workers, limit, *, source_root=None, ocr_timeout_seconds=600):
    if workers < 1 or workers > 64 or (limit is not None and limit < 1):
        raise ValueError('Invalid workers or limit')
    if not math.isfinite(ocr_timeout_seconds) or ocr_timeout_seconds <= 0:
        raise ValueError('OCR timeout must be positive and finite')
    if any(sha(SOURCE_PATHS[name]) != digest for name,digest in SOURCE_SHA256.items()):
        raise RuntimeError('Pipeline source changed after import; start a fresh process')
    root=task_root/'unified';db=root/'state.sqlite3'
    q,m,scripts=load_core(task_root);queue=TaskQueue(db);store=MediaStore(root,db)
    runtime=q.read_json(task_root/'codex-runtime.json')
    runtime={**runtime,'task_root':str(task_root)}
    profile=q.read_json(root/'ocr-profile.json') if stage=='ocr' else None
    if stage=='ocr' and make_profile(runtime,scripts)!=profile:
        raise OCRWorkerUnavailable('Saved OCR profile differs from actual runtime', global_failure=True)
    run_id=now().replace(':','').replace('+','_').replace('.','_')+'_'+uuid.uuid4().hex[:8]
    directory=root/'runs'/run_id;directory.mkdir(parents=True,exist_ok=False)
    record={'run_id':run_id,'stage':stage,'owner':owner,'source_root_filter':str(source_root) if source_root else None,'pid':os.getpid(),'command':sys.orig_argv,
            'task_root':str(task_root),'workers':workers,'limit':limit,'started_at':now(),'state':'running',
            'source_sha256':dict(SOURCE_SHA256),'collector_source_sha256':{
                Path(module.__file__).name:sha(module.__file__) for module in (q,m) if hasattr(module,'__file__')},
            'ocr_timeout_seconds':ocr_timeout_seconds if stage=='ocr' else None}
    if profile is not None:record['ocr_profile']=profile
    atomic(directory/'runtime.json',record)
    lock=threading.Lock()
    started=completed=failed=active=retired=0
    stop_claims=False
    cleanup_errors=[]
    run_errors=[]
    unresolved_claims=[]
    totals={'messages':0,'image_refs':0,'pages':0,'cached_pages':0}
    def claim(worker_id):
        identity=run_id+'_'+worker_id
        if stage=='messages':return queue.claim(owner,identity,os.getpid(),source_root=source_root)
        if stage=='downloads':return store.next_download(identity,os.getpid())
        return store.next_ocr(profile,identity,os.getpid())
    def worker(number):
        nonlocal started,completed,failed,active,retired,stop_claims
        worker_id=stage+'-'+str(number)
        browser=ocr=None
        task=None;inflight=False;phase='claim';item_error=None;began=time.perf_counter()
        try:
            while True:
                task=None;phase='claim';item_error=None;began=time.perf_counter()
                with lock:
                    if stop_claims or (limit is not None and started>=limit):return
                    try:
                        task=claim(worker_id)
                    except BaseException:
                        stop_claims=True
                        raise
                    if task is None:return
                    started+=1;active+=1;inflight=True
                began=time.perf_counter()
                row={'stage':stage,'worker_id':worker_id}
                if stage!='messages':row.update(claim_identity(task))
                retire=False
                try:
                    if stage=='messages':
                        phase='collect_messages'
                        if browser is None:
                            browser=TimedBrowser.open(runtime,run_id,worker_id,task['manifest']['expected_nick'],q)
                            atomic(directory/worker_id/'runtime.json',{'pid':os.getpid(),'runtime_summary':browser.runtime_summary})
                        value=collect_messages(root,task['manifest'],task['item'],browser,q,seeds=legacy_import.PageSeeds(queue,q))
                        path=root/'messages'/task['job_key']/'image_refs.jsonl'
                        refs=[json.loads(line) for line in path.read_text(encoding='utf-8').split('\n') if line]
                        store.add_refs(task['job_key'],refs)
                        queue.finish(task,root,root/'messages'/task['job_key']/'message-receipt.json')
                        row.update(job_key=task['job_key'],counts=value['counts'],page_metrics=value['page_metrics'])
                    elif stage=='downloads':
                        phase='download'
                        value=download_task(task)
                        phase='record_download'
                        store.record_download(task,value['path'],expected_sha256=value['sha256'],expected_bytes=value['bytes'])
                    else:
                        phase='recognize'
                        if ocr is None:
                            ocr=OCRProcess(runtime,scripts,directory/(worker_id+'.stderr.log'),profile,timeout_seconds=ocr_timeout_seconds)
                            atomic(directory/worker_id/'runtime.json',{'pid':ocr.process.pid,'parent_pid':os.getpid(),'stage':'ocr',
                                'worker_id':worker_id,'profile':profile,'ocr_timeout_seconds':ocr_timeout_seconds})
                        value=ocr.recognize(task)
                        phase='record_ocr'
                        store.record_ocr(task,value)
                    row['outcome']='complete'
                except Exception as error:
                    if stage!='messages' and (isinstance(error,(MediaStorageError,sqlite3.Error)) or
                            (phase in ('record_download','record_ocr') and isinstance(error,OSError))):
                        raise
                    safe={'kind':'item_error',**safe_stage_error(error)}
                    global_failure=False
                    if stage=='messages' and isinstance(error,ItemConflict):
                        safe.update(kind='conflict',message_id=error.message_id,changed_fields=error.changed_fields,failure_file=error.failure_file)
                    if stage=='messages' and isinstance(error,BridgeError) and error.worker_unusable:
                        safe.update(kind='browser_attention',error_code=error.code)
                        retire=True;global_failure=error.code in GLOBAL_BRIDGE_ERRORS
                    if isinstance(error,OCRWorkerUnavailable):
                        safe.update(kind='ocr_worker_attention')
                        retire=True;global_failure=error.global_failure
                    if global_failure:
                        with lock:stop_claims=True
                    item_error=safe
                    phase='save_failure'
                    if stage=='messages':queue.fail(task,safe);row['job_key']=task['job_key']
                    elif stage=='downloads':store.fail_download(task,safe['error_code'])
                    else:store.fail_ocr(task,safe['error_code'])
                    row.update(outcome='failed_review',error=safe,worker_retired=retire,global_failure=global_failure)
                row.update(elapsed_seconds=time.perf_counter()-began,at=now())
                with lock:
                    active-=1;inflight=False
                    if row['outcome']=='complete':
                        completed+=1
                        for key in totals:totals[key]+=row.get('counts',{}).get(key,0)
                    else:failed+=1
                    if retire:retired+=1
                    phase='event_write'
                    with (directory/'events.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(row,ensure_ascii=True)+'\n')
                    phase='progress_write'
                    atomic(directory/'progress.json',{'at':now(),'submitted':started,'completed':completed,'failed_review':failed,
                        'active':active,'retired_workers':retired,'global_stop':stop_claims,'message_totals':totals})
                    phase='console_write'
                    print(json.dumps({k:row[k] for k in ('stage','outcome','elapsed_seconds')},ensure_ascii=True),flush=True)
                if retire:return
        except BaseException as error:
            # No fail/release/retry here: a storage failure can have an unknown commit outcome.
            stop_claims=True
            identity=claim_identity(task)
            if not identity and isinstance(error,MediaStorageError):identity=claim_identity(error.claim)
            error_kind='storage_error' if isinstance(error,(MediaStorageError,sqlite3.Error)) or phase in ('claim','record_download','record_ocr','save_failure') else 'run_error'
            fault={'stage':stage,'worker_id':worker_id,'phase':phase,'error':{'kind':error_kind,**safe_stage_error(error)},**identity}
            if item_error is not None:fault['item_error']=item_error
            with lock:
                if identity and (inflight or phase=='claim'):
                    unresolved_claims.append({**identity,'worker_id':worker_id,'phase':phase})
                if inflight:
                    active-=1;inflight=False
                run_errors.append(fault)
                if phase not in ('event_write','progress_write','console_write'):
                    try:
                        with (directory/'events.jsonl').open('a',encoding='utf-8') as handle:
                            handle.write(json.dumps({**fault,'outcome':'needs_attention','at':now(),
                                'elapsed_seconds':time.perf_counter()-began},ensure_ascii=True)+'\n')
                    except OSError as evidence_error:
                        run_errors.append({'stage':stage,'worker_id':worker_id,'phase':'event_write',
                            'error':safe_stage_error(evidence_error)})
        finally:
            if ocr is not None:
                try:ocr.close()
                except Exception as error:
                    with lock:
                        stop_claims=True
                        fault={'stage':stage,'worker_id':worker_id,'phase':'worker_cleanup','error':safe_stage_error(error)}
                        cleanup_errors.append(fault);run_errors.append(fault)
    clock=time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(worker,number) for number in range(1,workers+1)]
        try:
            for future in futures:future.result()
        except BaseException as error:
            with lock:
                stop_claims=True
                run_errors.append({'stage':stage,'worker_id':None,'phase':'workers','error':safe_stage_error(error)})
    elapsed=time.perf_counter()-clock
    record.update(state='needs_attention' if stop_claims or retired==workers else ('drained_with_failed_items' if failed else 'drained'),
        finished_at=now(),submitted=started,completed=completed,failed_review=failed,elapsed_seconds=elapsed,
        retired_workers=retired,global_stop=stop_claims,cleanup_errors=cleanup_errors,
        run_errors=run_errors,unresolved_claims=unresolved_claims,active=active,
        completed_per_minute=completed*60/elapsed if elapsed else None,message_totals=totals,
        completed_messages_per_minute=totals['messages']*60/elapsed if elapsed else None,
        completed_live_pages_per_minute=(totals['pages']-totals['cached_pages'])*60/elapsed if elapsed else None)
    atomic(directory/'completion.json',record)
    atomic(directory/'runtime.json',record)
    atomic(directory/'progress.json',{'at':now(),'state':record['state'],'submitted':started,'completed':completed,
        'failed_review':failed,'active':active,'unresolved_claims':unresolved_claims,'run_errors':run_errors,
        'retired_workers':retired,'global_stop':stop_claims,'message_totals':totals})
    print(json.dumps(record,ensure_ascii=True),flush=True)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-root',type=Path,required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    init=commands.add_parser('init');init.add_argument('--manifest',type=Path,action='append',required=True)
    commands.add_parser('profile')
    commands.add_parser('status')
    for stage in ('messages','downloads','ocr'):
        sub=commands.add_parser(stage);sub.add_argument('--workers',type=int,required=True);sub.add_argument('--limit',type=int)
        if stage=='ocr':sub.add_argument('--ocr-timeout-seconds',type=float,default=600)
        if stage=='messages':
            sub.add_argument('--owner',choices=('codex','kimi'),required=True)
            sub.add_argument('--source-root',type=Path,help='Bounded validation of one registered source; ordinary runs omit it')
    args=parser.parse_args();task_root=args.task_root.resolve();root=task_root/'unified'
    if not root.resolve().is_relative_to(task_root):raise RuntimeError('Unified root escapes task root')
    if args.action=='init':print(json.dumps(TaskQueue(root/'state.sqlite3').initialize(args.manifest),ensure_ascii=True));return
    if args.action=='status':print(json.dumps(TaskQueue(root/'state.sqlite3').summary(),ensure_ascii=True));return
    if args.action=='profile':
        q,m,scripts=load_core(task_root);profile=make_profile(q.read_json(task_root/'codex-runtime.json'),scripts)
        path=root/'ocr-profile.json'
        if path.exists() and q.read_json(path)!=profile:raise RuntimeError('Existing OCR profile differs')
        atomic(path,profile);print(json.dumps({'profile_id':profile_value(profile)[1]},ensure_ascii=True));return
    if args.workers<1 or args.workers>64 or (args.limit is not None and args.limit<1):parser.error('Invalid workers or limit')
    timeout=getattr(args,'ocr_timeout_seconds',600)
    if not math.isfinite(timeout) or timeout<=0:parser.error('OCR timeout must be positive and finite')
    result=run_stage(task_root,args.action,getattr(args,'owner',None),args.workers,args.limit,
        source_root=getattr(args,'source_root',None),ocr_timeout_seconds=timeout)
    return 0 if result['state']=='drained' else 2


if __name__=='__main__':raise SystemExit(main())
