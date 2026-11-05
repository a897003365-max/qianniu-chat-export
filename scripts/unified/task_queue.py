"""One durable, owner-preserving queue. Failed items stay separate; claims never expire."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from message_export import context_key


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()


class QueueError(RuntimeError):
    pass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS queue_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue_jobs(
 job_key TEXT PRIMARY KEY,owner TEXT NOT NULL,priority_day TEXT NOT NULL,
 cid TEXT NOT NULL,query_user_nick TEXT NOT NULL,user_id TEXT NOT NULL,
 manifest_json TEXT NOT NULL,item_json TEXT NOT NULL,source_root TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',claim_id TEXT,worker_id TEXT,pid INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,finished_at TEXT,
 receipt_root TEXT,receipt_path TEXT,receipt_sha256 TEXT,completion_kind TEXT,
 counts_json TEXT,error_json TEXT);
CREATE INDEX IF NOT EXISTS queue_pending ON queue_jobs(owner,state,priority_day DESC,job_key);
CREATE TABLE IF NOT EXISTS queue_attempts(
 claim_id TEXT PRIMARY KEY,job_key TEXT NOT NULL,worker_id TEXT NOT NULL,pid INTEGER NOT NULL,
 started_at TEXT NOT NULL,ended_at TEXT,outcome TEXT,error_json TEXT);
CREATE TABLE IF NOT EXISTS queue_pages(
 fingerprint TEXT NOT NULL,artifact_root TEXT NOT NULL,path TEXT NOT NULL,
 bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,request_json TEXT NOT NULL,
 provenance_json TEXT NOT NULL,PRIMARY KEY(fingerprint,artifact_root,path,sha256));
CREATE INDEX IF NOT EXISTS queue_page_lookup ON queue_pages(fingerprint);
'''


class TaskQueue:
    def __init__(self, db):
        self.db = Path(db).resolve()
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self, manifest_paths):
        sources, jobs, seen, common = [], [], set(), None
        for path in map(lambda p: Path(p).resolve(), manifest_paths):
            data = path.read_bytes()
            manifest = json.loads(data)
            scope = {key: manifest[key] for key in ('start', 'end', 'timezone', 'expected_nick')}
            if manifest.get('schema') != 2 or manifest.get('scope') != 'month' or scope['timezone'] != 'Asia/Shanghai':
                raise QueueError('Invalid month manifest')
            if common is not None and scope != common:
                raise QueueError('Source intervals or signed-in identities differ')
            common = scope
            sources.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()})
            mini = {**scope, 'schema': 2, 'scope': 'month'}
            for item in manifest['items']:
                key = context_key(mini, item)
                if key in seen or item.get('lane') not in ('codex', 'kimi'):
                    raise QueueError('Duplicate context or invalid assigned owner')
                day = item['latest_seen_day']
                if not scope['start'] <= day < scope['end'] or dt.date.fromisoformat(day).isoformat() != day:
                    raise QueueError('Priority date outside authorized interval')
                seen.add(key)
                jobs.append((key, item['lane'], day, item['cid'], item['query_user_nick'], item['user_id'],
                             encoded(mini), encoded(item), str(path.parent)))
        owners = {owner: sum(job[1] == owner for job in jobs) for owner in ('codex', 'kimi')}
        if not jobs or abs(owners['codex']-owners['kimi']) > 1:
            raise QueueError('Combined source ownership must remain evenly assigned')
        binding = encoded({'sources': sources, 'scope': common, 'owners': owners})
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                old = connection.execute("SELECT value FROM queue_meta WHERE key='sources'").fetchone()
                if old:
                    if old['value'] != binding or connection.execute('SELECT COUNT(*) FROM queue_jobs').fetchone()[0] != len(jobs):
                        raise QueueError('Queue source binding differs; do not rewrite active jobs')
                else:
                    if connection.execute('SELECT COUNT(*) FROM queue_jobs').fetchone()[0]:
                        raise QueueError('Unbound existing queue')
                    connection.executemany('INSERT INTO queue_jobs(job_key,owner,priority_day,cid,query_user_nick,user_id,manifest_json,item_json,source_root) VALUES(?,?,?,?,?,?,?,?,?)', jobs)
                    connection.execute('INSERT INTO queue_meta VALUES(?,?)', ('sources', binding))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {'jobs': len(jobs), 'owners': owners, 'source_binding_sha256': hashlib.sha256(binding.encode()).hexdigest()}

    def claim(self, owner, worker_id, pid, *, source_root=None):
        if owner not in ('codex', 'kimi') or not worker_id or type(pid) is not int or pid <= 0:
            raise QueueError('Invalid claim identity')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                clause, arguments = '', [owner]
                if source_root is not None:
                    source_root = str(Path(source_root).resolve())
                    allowed = {str(Path(source['path']).parent) for source in self.binding()['sources']}
                    if source_root not in allowed:
                        raise QueueError('Requested source root is not registered')
                    clause = ' AND source_root=?'
                    arguments.append(source_root)
                row = connection.execute("SELECT * FROM queue_jobs WHERE owner=? AND state='pending'"+clause+" ORDER BY priority_day DESC,job_key LIMIT 1", arguments).fetchone()
                if row is None:
                    connection.commit()
                    return None
                token, started = uuid.uuid4().hex, now()
                connection.execute("UPDATE queue_jobs SET state='running',claim_id=?,worker_id=?,pid=?,attempts=attempts+1,started_at=?,finished_at=NULL,error_json=NULL WHERE job_key=? AND state='pending'", (token, worker_id, pid, started, row['job_key']))
                connection.execute('INSERT INTO queue_attempts(claim_id,job_key,worker_id,pid,started_at) VALUES(?,?,?,?,?)', (token,row['job_key'],worker_id,pid,started))
                connection.commit()
                result = dict(row)
                result.update(claim_id=token, worker_id=worker_id, pid=pid, state='running',
                              manifest=json.loads(row['manifest_json']), item=json.loads(row['item_json']))
                return result
            except Exception:
                connection.rollback()
                raise

    def finish(self, task, receipt_root, receipt_path, *, kind='messages_only'):
        root, path = Path(receipt_root).resolve(), Path(receipt_path).resolve()
        if not path.is_relative_to(root):
            raise QueueError('Receipt path outside artifact root')
        receipt = json.loads(path.read_text(encoding='utf-8-sig'))
        if receipt.get('status') != 'complete' or (kind == 'messages_only' and (receipt.get('stage') != 'messages' or receipt.get('job_key') != task['job_key'])):
            raise QueueError('Receipt is not complete for this message job')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                changed = connection.execute("UPDATE queue_jobs SET state='messages_complete',finished_at=?,receipt_root=?,receipt_path=?,receipt_sha256=?,completion_kind=?,counts_json=? WHERE job_key=? AND state='running' AND claim_id=?", (now(),str(root),str(path.relative_to(root)),sha(path),kind,encoded(receipt['counts']),task['job_key'],task['claim_id'])).rowcount
                if changed != 1:
                    raise QueueError('Claim no longer belongs to this task')
                connection.execute("UPDATE queue_attempts SET ended_at=?,outcome='messages_complete' WHERE claim_id=?", (now(),task['claim_id']))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def fail(self, task, error):
        safe = {key: error[key] for key in ('kind','error_type','error_code','failure_file','message_id','changed_fields') if key in error}
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                changed = connection.execute("UPDATE queue_jobs SET state='failed_review',finished_at=?,error_json=? WHERE job_key=? AND state='running' AND claim_id=?", (now(),encoded(safe),task['job_key'],task['claim_id'])).rowcount
                if changed != 1:
                    raise QueueError('Cannot fail a different or completed claim')
                connection.execute("UPDATE queue_attempts SET ended_at=?,outcome='failed_review',error_json=? WHERE claim_id=?", (now(),encoded(safe),task['claim_id']))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def release_reviewed(self, job_key, reason, *, confirmed_not_running=False):
        """Explicit operator action after checking actual process/claim state, never a lease."""
        if not confirmed_not_running or not reason:
            raise QueueError('An explicit stopped-process/review confirmation is required')
        with self.connect() as connection:
            with connection:
                row = connection.execute('SELECT state,claim_id FROM queue_jobs WHERE job_key=?', (job_key,)).fetchone()
                if row is None or row['state'] not in ('running', 'failed_review'):
                    raise QueueError('Only reviewed incomplete work can return to pending')
                connection.execute("UPDATE queue_attempts SET ended_at=?,outcome='explicit_release',error_json=? WHERE claim_id=?", (now(),encoded({'review_reason':reason}),row['claim_id']))
                connection.execute("UPDATE queue_jobs SET state='pending',claim_id=NULL,worker_id=NULL,pid=NULL WHERE job_key=?", (job_key,))

    def adopt_verified_legacy(self, job_key, receipt_root, receipt_path, counts):
        """Caller verifies complete legacy signatures and every artifact before adoption."""
        root, path = Path(receipt_root).resolve(), Path(receipt_path).resolve()
        if not path.is_relative_to(root):
            raise QueueError('Legacy receipt escapes artifact root')
        with self.connect() as connection:
            with connection:
                row = connection.execute('SELECT state,receipt_sha256 FROM queue_jobs WHERE job_key=?', (job_key,)).fetchone()
                if row is None:
                    raise QueueError('Legacy context is outside current sources')
                if row['state'] == 'messages_complete':
                    return False
                if row['state'] != 'pending':
                    raise QueueError('Do not adopt over running or failed work')
                connection.execute("UPDATE queue_jobs SET state='messages_complete',finished_at=?,receipt_root=?,receipt_path=?,receipt_sha256=?,completion_kind='legacy_full',counts_json=? WHERE job_key=?", (now(),str(root),str(path.relative_to(root)),sha(path),encoded(counts),job_key))
        return True

    def add_page(self, fingerprint, request, artifact_root, entry, provenance):
        root = Path(artifact_root).resolve()
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root) or path.stat().st_size != entry['bytes'] or sha(path) != entry['sha256']:
            raise QueueError('Page cache source does not match artifact')
        with self.connect() as connection:
            with connection:
                connection.execute('INSERT OR IGNORE INTO queue_pages VALUES(?,?,?,?,?,?,?)', (fingerprint,str(root),entry['path'],entry['bytes'],entry['sha256'],encoded(request),encoded(provenance)))

    def lookup_page(self, fingerprint, request):
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM queue_pages WHERE fingerprint=? ORDER BY artifact_root,path,sha256', (fingerprint,)).fetchall()
        if not rows:
            return None
        row = rows[0]
        if row['request_json'] != encoded(request):
            raise QueueError('Page fingerprint and request disagree')
        root = Path(row['artifact_root']).resolve()
        path = (root / row['path']).resolve()
        if not path.is_relative_to(root) or path.stat().st_size != row['bytes'] or sha(path) != row['sha256']:
            raise QueueError('Indexed raw page changed')
        return json.loads(path.read_text(encoding='utf-8-sig')), dict(row)

    def summary(self):
        with self.connect() as connection:
            groups = [dict(row) for row in connection.execute('SELECT owner,state,COUNT(*) jobs FROM queue_jobs GROUP BY owner,state')]
            failed = [dict(row) for row in connection.execute("SELECT job_key,owner,error_json FROM queue_jobs WHERE state='failed_review'")]
            running = [dict(row) for row in connection.execute("SELECT job_key,owner,claim_id,worker_id,pid FROM queue_jobs WHERE state='running'")]
        return {'groups':groups,'failed_review':failed,'running_claims':running}

    def binding(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM queue_meta WHERE key='sources'").fetchone()
        if row is None:
            raise QueueError('Queue is not initialized')
        return json.loads(row['value'])

    def job(self, job_key):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM queue_jobs WHERE job_key=?', (job_key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(manifest=json.loads(row['manifest_json']), item=json.loads(row['item_json']))
        return result
