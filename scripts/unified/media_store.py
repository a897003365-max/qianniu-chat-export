"""Shared media ledger. No network, OCR execution, background work or automatic retry.

Download workers write only the claim's output_path. The controller records a
result, or records a safe failure code. A dead claim is released only explicitly.
Legacy imports retain their original OCR bytes and verified source provenance.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from ocr_content import group_ocr_versions


MEDIA_ERROR_CODES = frozenset("""
relative_path_required path_outside_root invalid_sha256 invalid_artifact artifact_hash_or_size_mismatch
empty_image image_decode_failed invalid_image_dimensions invalid_ocr_profile profile_model_hashes_required
invalid_profile_model duplicate_profile_model ocr_image_mismatch ocr_frame_count_mismatch
ocr_frame_dimensions_mismatch invalid_ocr_entries invalid_ocr_entry invalid_ocr_confidence invalid_ocr_box
ocr_text_entries_mismatch database_outside_root media_database_binding_mismatch existing_cas_file_changed
blob_metadata_conflict invalid_job_key invalid_media_reference url_hash_conflict reference_identity_conflict
invalid_worker_identity claim_not_current_or_owned download_task_identity_mismatch ocr_task_identity_mismatch
invalid_claim_kind download_claim_required download_result_path_mismatch download_hash_mismatch
download_size_mismatch safe_error_code_required wrong_claim_kind explicit_verified_death_required
profile_identity_conflict ocr_claim_required ocr_running_profile_mismatch ocr_version_metadata_conflict
legacy_index_outside_source_root legacy_index_hash_mismatch invalid_legacy_index invalid_legacy_receipt_index
legacy_receipt_not_complete_or_bound duplicate_legacy_artifact legacy_artifact_not_receipted legacy_url_missing
legacy_image_association_mismatch legacy_image_sha_mismatch legacy_url_content_conflict asset_only_url_identity_conflict
media_store_error unexpected_error invalid_value io_error http_error download_failed invalid_download
invalid_ocr_result ocr_worker_unavailable ocr_request_timeout ocr_protocol_invalid ocr_profile_mismatch
ocr_termination_failed sqlite_error sqlite_busy sqlite_locked
""".split())

# Only SQLite result-code enums, excluding authorizer actions and configuration constants.
SQLITE_RESULT_NAMES = frozenset(name for name in dir(sqlite3) if any(
    name == base or name.startswith(base+"_") for base in (
        "SQLITE_OK", "SQLITE_ERROR", "SQLITE_INTERNAL", "SQLITE_PERM", "SQLITE_ABORT", "SQLITE_BUSY",
        "SQLITE_LOCKED", "SQLITE_NOMEM", "SQLITE_READONLY", "SQLITE_INTERRUPT", "SQLITE_IOERR",
        "SQLITE_CORRUPT", "SQLITE_NOTFOUND", "SQLITE_FULL", "SQLITE_CANTOPEN", "SQLITE_PROTOCOL",
        "SQLITE_EMPTY", "SQLITE_SCHEMA", "SQLITE_TOOBIG", "SQLITE_CONSTRAINT", "SQLITE_MISMATCH",
        "SQLITE_MISUSE", "SQLITE_NOLFS", "SQLITE_AUTH", "SQLITE_FORMAT", "SQLITE_RANGE", "SQLITE_NOTADB",
        "SQLITE_NOTICE", "SQLITE_WARNING", "SQLITE_ROW", "SQLITE_DONE")))


class MediaStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MEDIA_ERROR_CODES else "media_store_error"
        super().__init__(self.code)


def claim_identity(task):
    """Opaque correlation only; never include URL, runtime paths or worker payloads."""
    if not isinstance(task, dict) or task.get("kind") not in ("download", "ocr"):
        return {}
    claim_id, key = task.get("claim_id"), task.get("task_key")
    pattern = r"[0-9a-f]{64}" if task["kind"] == "download" else r"[0-9a-f]{64}:[0-9a-f]{64}"
    if not isinstance(claim_id, str) or not re.fullmatch(r"[0-9a-f]{32}", claim_id) or not isinstance(key, str) or not re.fullmatch(pattern, key):
        return {}
    return {"kind": task["kind"], "claim_id": claim_id, "task_key": key}


def artifact_description(entry):
    if not isinstance(entry, dict) or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
        return None
    digest, path = entry.get("sha256"), entry.get("path")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(path, str):
        return None
    blob = f"shared/blobs/{digest[:2]}/{digest}.image"
    ocr = re.fullmatch(r"shared/ocr/([0-9a-f]{2})/([0-9a-f]{64})/"+digest+r"\.json", path)
    if path != blob and not (ocr and ocr[1] == ocr[2][:2]):
        return None
    return {"path": path, "bytes": entry["bytes"], "sha256": digest}


class MediaStorageError(RuntimeError):
    """A storage operation has an unconfirmed outcome; recovery is controller-only."""
    def __init__(self, error, *, task=None, prepared_artifact=None):
        super().__init__("media_storage_unavailable")
        self.diagnostic = safe_media_error(error)
        self.claim = claim_identity(task) or (error.claim if isinstance(error, MediaStorageError) else {})
        prepared = prepared_artifact or (error.prepared_artifact if isinstance(error, MediaStorageError) else None)
        self.prepared_artifact = artifact_description(prepared)


def safe_media_error(error):
    if isinstance(error, MediaStorageError):
        value = dict(error.diagnostic)
        if error.prepared_artifact is not None:
            value["prepared_artifact"] = dict(error.prepared_artifact)
        return value
    if isinstance(error, sqlite3.Error):
        value = {"error_code": "sqlite_error"}
        code = getattr(error, "sqlite_errorcode", None)
        if type(code) is int:
            value["sqlite_errorcode"] = code
            value["error_code"] = {sqlite3.SQLITE_BUSY: "sqlite_busy", sqlite3.SQLITE_LOCKED: "sqlite_locked"}.get(code & 255, "sqlite_error")
            name = getattr(error, "sqlite_errorname", None)
            if isinstance(name, str) and name in SQLITE_RESULT_NAMES and getattr(sqlite3, name) == code:
                value["sqlite_errorname"] = name
        return value
    if isinstance(error, MediaStoreError):
        value = {"error_code": error.code if isinstance(error.code, str) and error.code in MEDIA_ERROR_CODES else "media_store_error"}
        prepared = artifact_description(getattr(error, "prepared_artifact", None))
        if prepared is not None:
            value["prepared_artifact"] = prepared
        return value
    return {"error_code": "io_error" if isinstance(error, OSError) else ("invalid_value" if isinstance(error, ValueError) else "unexpected_error")}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_path(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise MediaStoreError("relative_path_required")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise MediaStoreError("path_outside_root")
    return path


def digest_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MediaStoreError("invalid_sha256")
    return value


def read_artifact(root, entry):
    if not isinstance(entry, dict) or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
        raise MediaStoreError("invalid_artifact")
    digest_value(entry.get("sha256"))
    data = safe_path(root, entry["path"]).read_bytes()
    if len(data) != entry["bytes"] or sha(data) != entry["sha256"]:
        raise MediaStoreError("artifact_hash_or_size_mismatch")
    return data


def decode_image(data):
    from PIL import Image
    if not data:
        raise MediaStoreError("empty_image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            frames = []
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                image.load()
                frames.append({"frame_index": index, "width": image.width, "height": image.height})
            result = {"format": image.format, "frames": frames}
    except Exception:
        raise MediaStoreError("image_decode_failed") from None
    if not frames or any(frame["width"] < 1 or frame["height"] < 1 for frame in frames):
        raise MediaStoreError("invalid_image_dimensions")
    return result


def profile_value(profile):
    if not isinstance(profile, dict) or set(profile) != {"engine", "engine_version", "worker_sha256", "models", "parameters"}:
        raise MediaStoreError("invalid_ocr_profile")
    if any(not isinstance(profile[key], str) or not profile[key] for key in ("engine", "engine_version")) or not isinstance(profile["parameters"], dict):
        raise MediaStoreError("invalid_ocr_profile")
    digest_value(profile["worker_sha256"])
    if not isinstance(profile["models"], list) or not profile["models"]:
        raise MediaStoreError("profile_model_hashes_required")
    models = []
    for model in profile["models"]:
        if not isinstance(model, dict) or set(model) != {"name", "sha256"} or not isinstance(model["name"], str) or not model["name"]:
            raise MediaStoreError("invalid_profile_model")
        digest_value(model["sha256"])
        models.append(dict(model))
    if len({model["name"] for model in models}) != len(models):
        raise MediaStoreError("duplicate_profile_model")
    value = {**profile, "models": sorted(models, key=lambda item: item["name"])}
    return json.loads(encoded(value)), sha(encoded(value))


def validate_ocr(result, image_sha, dimensions):
    if not isinstance(result, dict) or result.get("image_sha256") != image_sha:
        raise MediaStoreError("ocr_image_mismatch")
    frames = result.get("frames")
    if not isinstance(frames, list) or len(frames) != len(dimensions):
        raise MediaStoreError("ocr_frame_count_mismatch")
    for frame, expected in zip(frames, dimensions):
        if not isinstance(frame, dict) or any(type(frame.get(key)) is not int or frame[key] != expected[key] for key in ("frame_index", "width", "height")):
            raise MediaStoreError("ocr_frame_dimensions_mismatch")
        entries = frame.get("entries")
        if not isinstance(entries, list) or not isinstance(frame.get("text"), str):
            raise MediaStoreError("invalid_ocr_entries")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                raise MediaStoreError("invalid_ocr_entry")
            score = entry.get("confidence")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise MediaStoreError("invalid_ocr_confidence")
            box = entry.get("box")
            if not isinstance(box, list) or len(box) != 4 or any(not isinstance(point, list) or len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point) for point in box):
                raise MediaStoreError("invalid_ocr_box")
        if frame["text"] != "\n".join(entry["text"] for entry in entries):
            raise MediaStoreError("ocr_text_entries_mismatch")
    encoded(result)


SCHEMA = """
CREATE TABLE IF NOT EXISTS media_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_blobs(
 image_sha TEXT PRIMARY KEY,path TEXT NOT NULL,bytes INTEGER NOT NULL,info_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_profiles(profile_id TEXT PRIMARY KEY,profile_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_claims(
 claim_id TEXT PRIMARY KEY,kind TEXT NOT NULL,task_key TEXT NOT NULL,worker_id TEXT NOT NULL,
 pid INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,ended_at TEXT,error_code TEXT,result_json TEXT);
CREATE TABLE IF NOT EXISTS media_urls(
 url_key TEXT PRIMARY KEY,source_url TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('pending','claimed','complete','failed_review')),
 image_sha TEXT REFERENCES media_blobs(image_sha),claim_id TEXT REFERENCES media_claims(claim_id),
 provenance TEXT,error_code TEXT);
CREATE TABLE IF NOT EXISTS media_refs(
 job_key TEXT NOT NULL,ref_key TEXT NOT NULL,url_key TEXT NOT NULL REFERENCES media_urls(url_key),
 message_id TEXT NOT NULL,send_time_ms INTEGER NOT NULL,ref_json TEXT NOT NULL,PRIMARY KEY(job_key,ref_key));
CREATE TABLE IF NOT EXISTS media_ocr_tasks(
 image_sha TEXT NOT NULL REFERENCES media_blobs(image_sha),profile_id TEXT NOT NULL REFERENCES media_profiles(profile_id),
 state TEXT NOT NULL CHECK(state IN ('pending','claimed','complete','failed_review')),
 claim_id TEXT REFERENCES media_claims(claim_id),ocr_sha TEXT,error_code TEXT,PRIMARY KEY(image_sha,profile_id));
CREATE TABLE IF NOT EXISTS media_ocr_versions(
 image_sha TEXT NOT NULL REFERENCES media_blobs(image_sha),ocr_sha TEXT NOT NULL,path TEXT NOT NULL,bytes INTEGER NOT NULL,
 profile_id TEXT REFERENCES media_profiles(profile_id),provenance TEXT NOT NULL,PRIMARY KEY(image_sha,ocr_sha));
CREATE TABLE IF NOT EXISTS media_ocr_sources(
 image_sha TEXT NOT NULL,ocr_sha TEXT NOT NULL,source_key TEXT NOT NULL,source_json TEXT NOT NULL,
 PRIMARY KEY(image_sha,ocr_sha,source_key),FOREIGN KEY(image_sha,ocr_sha) REFERENCES media_ocr_versions(image_sha,ocr_sha));
CREATE INDEX IF NOT EXISTS media_refs_url_priority ON media_refs(url_key,send_time_ms DESC);
CREATE INDEX IF NOT EXISTS media_urls_state_image ON media_urls(state,image_sha,url_key);
DROP INDEX IF EXISTS media_refs_url;
"""


DOWNLOAD_CANDIDATE_SQL = """SELECT u.url_key,u.source_url,
    (SELECT MAX(r.send_time_ms) FROM media_refs r WHERE r.url_key=u.url_key) priority
    FROM media_urls u WHERE u.state='pending' AND priority IS NOT NULL
    ORDER BY priority DESC,u.url_key LIMIT 1"""

# Filter completed/claimed images before looking up their newest eligible
# reference. Covering indexes avoid expanding duplicate image references.
OCR_CANDIDATE_SQL = """SELECT b.*,
    (SELECT MAX((SELECT MAX(r.send_time_ms) FROM media_refs r WHERE r.url_key=u.url_key))
     FROM media_urls u WHERE u.image_sha=b.image_sha AND u.state='complete') priority
    FROM media_blobs b
    LEFT JOIN media_ocr_tasks t ON t.image_sha=b.image_sha AND t.profile_id=?
    WHERE (t.state IS NULL OR t.state='pending')
    AND NOT EXISTS(SELECT 1 FROM media_ocr_tasks active WHERE active.image_sha=b.image_sha AND active.state='claimed')
    AND NOT EXISTS(SELECT 1 FROM media_ocr_versions v WHERE v.image_sha=b.image_sha)
    AND priority IS NOT NULL ORDER BY priority DESC,b.image_sha LIMIT 1"""


class MediaStore:
    def __init__(self, root, db):
        self.root, self.db = Path(root).resolve(), Path(db).resolve()
        if not self.db.is_relative_to(self.root):
            raise MediaStoreError("database_outside_root")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
        with self._transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO media_meta VALUES('root',?)", (str(self.root),))
            connection.execute("INSERT OR IGNORE INTO media_meta VALUES('schema','1')")
            values = dict(connection.execute("SELECT key,value FROM media_meta"))
            if values.get("root") != str(self.root) or values.get("schema") != "1":
                raise MediaStoreError("media_database_binding_mismatch")
        for relative in ("shared/incoming", "shared/blobs", "shared/ocr"):
            safe_path(self.root, relative).mkdir(parents=True, exist_ok=True)

    def _connect(self):
        connection = sqlite3.connect(self.db, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _transaction(self):
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass  # Preserve the primary failure; the caller treats its outcome as unconfirmed.
                raise

    def _publish(self, relative, data):
        """Fully written unique temp + atomic no-clobber link; never share .part names."""
        destination = safe_path(self.root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".media-", suffix=".part", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = destination.read_bytes()
                if len(existing) != len(data) or sha(existing) != sha(data):
                    raise MediaStoreError("existing_cas_file_changed")
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": relative, "bytes": len(data), "sha256": sha(data)}

    def _store_blob(self, data, info=None):
        info = decode_image(data) if info is None else info
        image_sha = sha(data)
        artifact = self._publish(f"shared/blobs/{image_sha[:2]}/{image_sha}.image", data)
        return {"image_sha256": image_sha, **artifact, "info": info}

    @staticmethod
    def _insert_blob(connection, blob):
        values = (blob["image_sha256"], blob["path"], blob["bytes"], encoded(blob["info"]).decode())
        connection.execute("INSERT OR IGNORE INTO media_blobs VALUES(?,?,?,?)", values)
        row = connection.execute("SELECT * FROM media_blobs WHERE image_sha=?", (blob["image_sha256"],)).fetchone()
        if tuple(row) != values:
            raise MediaStoreError("blob_metadata_conflict")

    def add_refs(self, job_key, refs):
        if not isinstance(job_key, str) or not job_key:
            raise MediaStoreError("invalid_job_key")
        prepared = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("job_key") != job_key or not isinstance(ref.get("message_id"), str) or not ref["message_id"]:
                raise MediaStoreError("invalid_media_reference")
            digest_value(ref.get("ref_key"))
            url = ref.get("source_url")
            if not isinstance(url, str) or not url or type(ref.get("send_time_ms")) is not int or ref["send_time_ms"] <= 0:
                raise MediaStoreError("invalid_media_reference")
            prepared.append((ref, sha(url.encode("utf-8")), encoded(ref).decode()))
        added = 0
        with self._transaction() as connection:
            for ref, key, serialized in prepared:
                connection.execute("INSERT OR IGNORE INTO media_urls(url_key,source_url,state) VALUES(?,?,'pending')", (key, ref["source_url"]))
                row = connection.execute("SELECT source_url FROM media_urls WHERE url_key=?", (key,)).fetchone()
                if row[0] != ref["source_url"]:
                    raise MediaStoreError("url_hash_conflict")
                old = connection.execute("SELECT ref_json FROM media_refs WHERE job_key=? AND ref_key=?", (job_key, ref["ref_key"])).fetchone()
                if old:
                    if old[0] != serialized:
                        raise MediaStoreError("reference_identity_conflict")
                    continue
                connection.execute("INSERT INTO media_refs VALUES(?,?,?,?,?,?)", (job_key, ref["ref_key"], key, ref["message_id"], ref["send_time_ms"], serialized))
                added += 1
        return {"job_key": job_key, "added_refs": added, "submitted_refs": len(prepared)}

    @staticmethod
    def _new_claim(connection, kind, key, worker_id, pid):
        if not isinstance(worker_id, str) or not worker_id or type(pid) is not int or pid <= 0:
            raise MediaStoreError("invalid_worker_identity")
        claim_id = uuid.uuid4().hex
        task = {"kind": kind, "task_key": key, "claim_id": claim_id, "worker_id": worker_id, "pid": pid}
        try:
            connection.execute("INSERT INTO media_claims(claim_id,kind,task_key,worker_id,pid,status,created_at) VALUES(?,?,?,?,?,'claimed',?)", (claim_id, kind, key, worker_id, pid, timestamp()))
        except sqlite3.Error as error:
            raise MediaStorageError(error, task=task) from None
        return task

    @staticmethod
    def _require_claim(connection, task):
        row = connection.execute("SELECT * FROM media_claims WHERE claim_id=?", (task.get("claim_id"),)).fetchone()
        if row is None or row["status"] != "claimed" or any(type(task.get(key)) is not type(row[key]) or task[key] != row[key] for key in ("kind", "task_key", "worker_id", "pid")):
            raise MediaStoreError("claim_not_current_or_owned")
        if row["kind"] == "download":
            owner = connection.execute("SELECT state,claim_id,source_url FROM media_urls WHERE url_key=?", (row["task_key"],)).fetchone()
            if owner is None or task.get("url_key") != row["task_key"] or task.get("source_url") != owner["source_url"]:
                raise MediaStoreError("download_task_identity_mismatch")
        elif row["kind"] == "ocr":
            image_sha, profile_id = row["task_key"].split(":")
            if task.get("image_sha256") != image_sha or task.get("profile_id") != profile_id:
                raise MediaStoreError("ocr_task_identity_mismatch")
            owner = connection.execute("SELECT state,claim_id FROM media_ocr_tasks WHERE image_sha=? AND profile_id=?", (image_sha, profile_id)).fetchone()
        else:
            raise MediaStoreError("invalid_claim_kind")
        if owner is None or owner["state"] != "claimed" or owner["claim_id"] != row["claim_id"]:
            raise MediaStoreError("claim_not_current_or_owned")
        return row

    def next_download(self, worker_id, pid):
        task = None
        try:
            with self._transaction() as connection:
                row = connection.execute(DOWNLOAD_CANDIDATE_SQL).fetchone()
                if row is None:
                    return None
                task = self._new_claim(connection, "download", row["url_key"], worker_id, pid)
                connection.execute("UPDATE media_urls SET state='claimed',claim_id=?,error_code=NULL WHERE url_key=?", (task["claim_id"], row["url_key"]))
                return {**task, "url_key": row["url_key"], "source_url": row["source_url"],
                        "output_path": str(safe_path(self.root, "shared/incoming/"+task["claim_id"]+".download"))}
        except Exception as error:
            raise MediaStorageError(error, task=task) from None

    def record_download(self, task, local_file=None, *, expected_sha256=None, expected_bytes=None):
        with contextlib.closing(self._connect()) as connection:
            claim = self._require_claim(connection, task)
            if claim["kind"] != "download":
                raise MediaStoreError("download_claim_required")
        expected = safe_path(self.root, "shared/incoming/"+task["claim_id"]+".download")
        path = Path(local_file or task["output_path"]).resolve()
        if path != expected:
            raise MediaStoreError("download_result_path_mismatch")
        data = path.read_bytes()
        if expected_sha256 is not None and (digest_value(expected_sha256) != sha(data)):
            raise MediaStoreError("download_hash_mismatch")
        if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes != len(data)):
            raise MediaStoreError("download_size_mismatch")
        blob = self._store_blob(data)
        try:
            with self._transaction() as connection:
                self._require_claim(connection, task)
                self._insert_blob(connection, blob)
                connection.execute("UPDATE media_urls SET state='complete',image_sha=?,provenance='downloaded_verified',error_code=NULL WHERE url_key=?", (blob["image_sha256"], task["task_key"]))
                connection.execute("UPDATE media_claims SET status='complete',ended_at=?,result_json=? WHERE claim_id=?", (timestamp(), encoded(blob).decode(), task["claim_id"]))
        except MediaStoreError as error:
            error.prepared_artifact = artifact_description(blob)
            raise
        except Exception as error:
            raise MediaStorageError(error, task=task, prepared_artifact=blob) from None
        # A cleanup error does not turn a verified, committed blob into a failed download.
        try:
            path.unlink(missing_ok=True)
            blob["staging_cleanup_pending"] = False
        except OSError:
            blob["staging_cleanup_pending"] = True
        return blob

    def _fail(self, task, kind, error_code):
        if not isinstance(error_code, str) or error_code not in MEDIA_ERROR_CODES:
            raise MediaStoreError("safe_error_code_required")
        with self._transaction() as connection:
            claim = self._require_claim(connection, task)
            if claim["kind"] != kind:
                raise MediaStoreError("wrong_claim_kind")
            if kind == "download":
                connection.execute("UPDATE media_urls SET state='failed_review',error_code=? WHERE url_key=?", (error_code, claim["task_key"]))
            else:
                connection.execute("UPDATE media_ocr_tasks SET state='failed_review',error_code=? WHERE image_sha=? AND profile_id=?", (error_code, *claim["task_key"].split(":")))
            connection.execute("UPDATE media_claims SET status='failed_review',ended_at=?,error_code=? WHERE claim_id=?", (timestamp(), error_code, claim["claim_id"]))
        return {"status": "failed_review", "error_code": error_code}

    def fail_download(self, task, error_code):
        return self._fail(task, "download", error_code)

    def fail_ocr(self, task, error_code):
        return self._fail(task, "ocr", error_code)

    def release_claim(self, task, *, confirmed_dead=False):
        """Controller-only decision after independently verifying the recorded PID died."""
        if confirmed_dead is not True:
            raise MediaStoreError("explicit_verified_death_required")
        with self._transaction() as connection:
            claim = self._require_claim(connection, task)
            if claim["kind"] == "download":
                connection.execute("UPDATE media_urls SET state='pending',claim_id=NULL WHERE url_key=?", (claim["task_key"],))
            else:
                connection.execute("UPDATE media_ocr_tasks SET state='pending',claim_id=NULL WHERE image_sha=? AND profile_id=?", tuple(claim["task_key"].split(":")))
            connection.execute("UPDATE media_claims SET status='released_confirmed_dead',ended_at=? WHERE claim_id=?", (timestamp(), claim["claim_id"]))
        return {"status": "pending", "released_claim_id": task["claim_id"]}

    def next_ocr(self, profile, worker_id, pid):
        value, profile_id = profile_value(profile)
        task = None
        try:
            with self._transaction() as connection:
                connection.execute("INSERT OR IGNORE INTO media_profiles VALUES(?,?)", (profile_id, encoded(value).decode()))
                if connection.execute("SELECT profile_json FROM media_profiles WHERE profile_id=?", (profile_id,)).fetchone()[0] != encoded(value).decode():
                    raise MediaStoreError("profile_identity_conflict")
                row = connection.execute(OCR_CANDIDATE_SQL, (profile_id,)).fetchone()
                if row is None:
                    return None
                image_path = str(safe_path(self.root, row["path"]))
                task = self._new_claim(connection, "ocr", row["image_sha"]+":"+profile_id, worker_id, pid)
                connection.execute("INSERT OR IGNORE INTO media_ocr_tasks(image_sha,profile_id,state) VALUES(?,?,'pending')", (row["image_sha"], profile_id))
                connection.execute("UPDATE media_ocr_tasks SET state='claimed',claim_id=?,error_code=NULL WHERE image_sha=? AND profile_id=?", (task["claim_id"], row["image_sha"], profile_id))
                blob = dict(row)
            return {**task, "image_sha256": blob["image_sha"], "profile_id": profile_id, "profile": value,
                    "image_path": image_path, "image_bytes": blob["bytes"], "frames": json.loads(blob["info_json"])["frames"]}
        except Exception as error:
            raise MediaStorageError(error, task=task) from None

    def record_ocr(self, task, result):
        with contextlib.closing(self._connect()) as connection:
            claim = self._require_claim(connection, task)
            if claim["kind"] != "ocr":
                raise MediaStoreError("ocr_claim_required")
            image_sha, profile_id = claim["task_key"].split(":")
            blob = dict(connection.execute("SELECT * FROM media_blobs WHERE image_sha=?", (image_sha,)).fetchone())
            profile = json.loads(connection.execute("SELECT profile_json FROM media_profiles WHERE profile_id=?", (profile_id,)).fetchone()[0])
        read_artifact(self.root, {"path": blob["path"], "bytes": blob["bytes"], "sha256": image_sha})
        validate_ocr(result, image_sha, json.loads(blob["info_json"])["frames"])
        if result.get("profile_id") != profile_id or result.get("engine") != profile["engine"] or result.get("version") != profile["engine_version"]:
            raise MediaStoreError("ocr_running_profile_mismatch")
        if "profile" in result and encoded(result["profile"]) != encoded(profile):
            raise MediaStoreError("ocr_running_profile_mismatch")
        data = encoded({**result, "profile": profile})
        artifact = self._publish(f"shared/ocr/{image_sha[:2]}/{image_sha}/{sha(data)}.json", data)
        source = {"kind": "profiled_worker", "claim_id": task["claim_id"], "worker_id": task["worker_id"], "pid": task["pid"], "profile_id": profile_id}
        try:
            with self._transaction() as connection:
                self._require_claim(connection, task)
                self._insert_ocr(connection, image_sha, artifact, profile_id, "generated_profiled", source)
                connection.execute("UPDATE media_ocr_tasks SET state='complete',ocr_sha=?,error_code=NULL WHERE image_sha=? AND profile_id=?", (artifact["sha256"], image_sha, profile_id))
                connection.execute("UPDATE media_claims SET status='complete',ended_at=?,result_json=? WHERE claim_id=?", (timestamp(), encoded(artifact).decode(), task["claim_id"]))
        except MediaStoreError as error:
            error.prepared_artifact = artifact_description(artifact)
            raise
        except Exception as error:
            raise MediaStorageError(error, task=task, prepared_artifact=artifact) from None
        return {"image_sha256": image_sha, "profile_id": profile_id, "provenance": "generated_profiled", **artifact}

    @staticmethod
    def _insert_ocr(connection, image_sha, artifact, profile_id, provenance, source):
        values = (image_sha, artifact["sha256"], artifact["path"], artifact["bytes"], profile_id, provenance)
        connection.execute("INSERT OR IGNORE INTO media_ocr_versions VALUES(?,?,?,?,?,?)", values)
        old = connection.execute("SELECT * FROM media_ocr_versions WHERE image_sha=? AND ocr_sha=?", (image_sha, artifact["sha256"])).fetchone()
        if tuple(old) != values:
            raise MediaStoreError("ocr_version_metadata_conflict")
        serialized = encoded(source)
        connection.execute("INSERT OR IGNORE INTO media_ocr_sources VALUES(?,?,?,?)", (image_sha, artifact["sha256"], sha(serialized), serialized.decode()))

    def job_media_status(self, job_key):
        with contextlib.closing(self._connect()) as connection:
            rows = connection.execute("SELECT r.ref_key,r.message_id,u.* FROM media_refs r JOIN media_urls u USING(url_key) WHERE r.job_key=? ORDER BY r.ref_key", (job_key,)).fetchall()
            references, checked = [], {}
            for row in rows:
                value = {"ref_key": row["ref_key"], "message_id": row["message_id"], "url_key": row["url_key"], "image_sha256": row["image_sha"], "error_code": row["error_code"]}
                if row["state"] != "complete":
                    value["state"] = {"pending": "pending_download", "claimed": "downloading", "failed_review": "failed_review"}[row["state"]]
                else:
                    image_sha = row["image_sha"]
                    if image_sha not in checked:
                        blob = dict(connection.execute("SELECT * FROM media_blobs WHERE image_sha=?", (image_sha,)).fetchone())
                        version = connection.execute("SELECT * FROM media_ocr_versions WHERE image_sha=? ORDER BY ocr_sha LIMIT 1", (image_sha,)).fetchone()
                        detail = {"blob": {"path": blob["path"], "bytes": blob["bytes"], "sha256": image_sha, "info": json.loads(blob["info_json"])}}
                        try:
                            read_artifact(self.root, detail["blob"])
                            if version:
                                version = dict(version)
                                read_artifact(self.root, {"path": version["path"], "bytes": version["bytes"], "sha256": version["ocr_sha"]})
                                detail.update(state="complete", ocr={"path": version["path"], "bytes": version["bytes"],
                                    "sha256": version["ocr_sha"], "profile_id": version["profile_id"], "provenance": version["provenance"]})
                            else:
                                tasks = list(connection.execute("SELECT state,error_code FROM media_ocr_tasks WHERE image_sha=?", (image_sha,)))
                                active = next((task for task in tasks if task["state"] == "claimed"), None)
                                failed = next((task for task in tasks if task["state"] == "failed_review"), None)
                                detail["state"] = "recognizing" if active else ("failed_review" if failed else "pending_ocr")
                                if failed and not active:
                                    detail["error_code"] = failed["error_code"]
                        except (OSError, MediaStoreError):
                            detail.update(state="failed_review", error_code="shared_artifact_missing_or_changed")
                        checked[image_sha] = detail
                    value.update(checked[image_sha])
                references.append(value)
        counts = {state: sum(ref["state"] == state for ref in references) for state in ("pending_download", "downloading", "pending_ocr", "recognizing", "failed_review", "complete")}
        return {"job_key": job_key, "total_refs": len(references), "counts": counts, "complete": counts["complete"] == len(references), "refs": references}

    def claimed_tasks(self):
        """Read-only inventory for an explicit controller death/recovery decision."""
        with contextlib.closing(self._connect()) as connection:
            tasks = [dict(row) for row in connection.execute("SELECT kind,task_key,claim_id,worker_id,pid FROM media_claims WHERE status='claimed' ORDER BY created_at,claim_id")]
            for task in tasks:
                if task["kind"] == "download":
                    task.update(url_key=task["task_key"], source_url=connection.execute("SELECT source_url FROM media_urls WHERE url_key=?", (task["task_key"],)).fetchone()[0],
                                output_path=str(safe_path(self.root, "shared/incoming/"+task["claim_id"]+".download")))
                else:
                    image_sha, profile_id = task["task_key"].split(":")
                    blob = connection.execute("SELECT * FROM media_blobs WHERE image_sha=?", (image_sha,)).fetchone()
                    task.update(image_sha256=image_sha, profile_id=profile_id,
                                profile=json.loads(connection.execute("SELECT profile_json FROM media_profiles WHERE profile_id=?", (profile_id,)).fetchone()[0]),
                                image_path=str(safe_path(self.root, blob["path"])), image_bytes=blob["bytes"], frames=json.loads(blob["info_json"])["frames"])
            return tasks

    def ocr_versions(self, image_sha):
        digest_value(image_sha)
        with contextlib.closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM media_ocr_versions WHERE image_sha=? ORDER BY ocr_sha", (image_sha,)).fetchall()
            return [{"image_sha256": image_sha, "sha256": row["ocr_sha"], "path": row["path"], "bytes": row["bytes"],
                     "profile_id": row["profile_id"], "provenance": row["provenance"],
                     "sources": [json.loads(source[0]) for source in connection.execute("SELECT source_json FROM media_ocr_sources WHERE image_sha=? AND ocr_sha=? ORDER BY source_key", (image_sha, row["ocr_sha"]))]} for row in rows]

    def ocr_content_groups(self, image_sha):
        """Read verified recognition groups without dropping archival versions."""
        versions = self.ocr_versions(image_sha)
        items = []
        for version in versions:
            value = json.loads(read_artifact(self.root, version))
            if value.get("image_sha256") != image_sha:
                raise MediaStoreError("ocr_image_mismatch")
            items.append({"value": value, "version": version})
        return [{"image_sha256": image_sha, "content_sha256": fingerprint,
                 "versions": [item["version"] for item in group]}
                for fingerprint, group in group_ocr_versions(items).items()]

    def import_verified_legacy(self, source_root, media_index_path, *, expected_index_sha256):
        source_root, media_index_path = Path(source_root).resolve(), Path(media_index_path).resolve()
        if not media_index_path.is_relative_to(source_root):
            raise MediaStoreError("legacy_index_outside_source_root")
        index_bytes = media_index_path.read_bytes()
        if sha(index_bytes) != digest_value(expected_index_sha256):
            raise MediaStoreError("legacy_index_hash_mismatch")
        index = json.loads(index_bytes)
        if type(index.get("schema")) is not int or index["schema"] != 1 or index.get("scope") != "completed_media_only" or not isinstance(index.get("source_root"), str) or not Path(index["source_root"]).is_absolute() or Path(index["source_root"]).resolve() != source_root or not isinstance(index.get("receipts"), list) or not isinstance(index.get("media"), list):
            raise MediaStoreError("invalid_legacy_index")
        receipts = {}
        imported = 0
        for item in index["media"]:
            number = item.get("receipt_index")
            if type(number) is not int or not 0 <= number < len(index["receipts"]):
                raise MediaStoreError("invalid_legacy_receipt_index")
            entry = index["receipts"][number]
            if number not in receipts:
                data = read_artifact(source_root, entry)
                receipt_path = safe_path(source_root, entry["path"])
                artifact_root = safe_path(source_root, entry["artifact_root"])
                receipt = json.loads(data)
                if receipt.get("status") != "complete" or artifact_root != receipt_path.parents[2]:
                    raise MediaStoreError("legacy_receipt_not_complete_or_bound")
                artifacts = {value["path"]: value for value in receipt["artifacts"]}
                if len(artifacts) != len(receipt["artifacts"]):
                    raise MediaStoreError("duplicate_legacy_artifact")
                receipts[number] = (artifact_root, artifacts)
            artifact_root, artifacts = receipts[number]
            for field in ("metadata", "image", "ocr"):
                artifact = item[field]
                parts = Path(artifact["path"]).parts
                namespace = "ocr" if field == "ocr" else "images"
                if len(parts) != 2 or parts[0] != namespace or encoded(artifacts.get(artifact["path"])) != encoded(artifact):
                    raise MediaStoreError("legacy_artifact_not_receipted")
            metadata = json.loads(read_artifact(artifact_root, item["metadata"]))
            url = metadata.get("url")
            if not isinstance(url, str) or not url:
                raise MediaStoreError("legacy_url_missing")
            url_key = sha(url.encode("utf-8"))
            if url_key != item["url_sha256"] or Path(item["metadata"]["path"]).stem != url_key or encoded(metadata.get("artifact")) != encoded(item["image"]) or metadata.get("sha256") != item["image_sha256"] or metadata.get("actual_bytes") != item["image"]["bytes"]:
                raise MediaStoreError("legacy_image_association_mismatch")
            image_data = read_artifact(artifact_root, item["image"])
            if sha(image_data) != item["image_sha256"]:
                raise MediaStoreError("legacy_image_sha_mismatch")
            info = decode_image(image_data)
            ocr_data = read_artifact(artifact_root, item["ocr"])
            validate_ocr(json.loads(ocr_data), item["image_sha256"], info["frames"])
            blob = self._store_blob(image_data, info)
            ocr_artifact = self._publish(f"shared/ocr/{blob['image_sha256'][:2]}/{blob['image_sha256']}/{sha(ocr_data)}.json", ocr_data)
            source = {"kind": "imported_verified_legacy", "source_root": str(source_root), "index_sha256": expected_index_sha256,
                      "index_path": media_index_path.relative_to(source_root).as_posix(),
                      "receipt": entry, "metadata": item["metadata"], "image": item["image"], "ocr": item["ocr"]}
            with self._transaction() as connection:
                self._insert_blob(connection, blob)
                old = connection.execute("SELECT state,image_sha FROM media_urls WHERE url_key=?", (url_key,)).fetchone()
                if old and old["image_sha"] is not None and old["image_sha"] != blob["image_sha256"]:
                    raise MediaStoreError("legacy_url_content_conflict")
                connection.execute("INSERT OR IGNORE INTO media_urls(url_key,source_url,state) VALUES(?,?,'pending')", (url_key, url))
                connection.execute("UPDATE media_urls SET state='complete',image_sha=?,provenance='imported_verified_legacy',error_code=NULL WHERE url_key=? AND state!='claimed'", (blob["image_sha256"], url_key))
                self._insert_ocr(connection, blob["image_sha256"], ocr_artifact, None, "imported_verified_legacy", source)
            imported += 1
        return {"verified_source_entries": imported, "index_sha256": expected_index_sha256}
