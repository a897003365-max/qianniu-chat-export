import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
import media_store as media


HERE = Path(__file__).resolve().parent
PROFILE = {"engine": "synthetic_ocr", "engine_version": "1.0", "worker_sha256": media.sha(b"synthetic worker"),
           "models": [{"name": "detector", "sha256": media.sha(b"synthetic model")}], "parameters": {"threads": 1}}


def png(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buffer, format="PNG")
    return buffer.getvalue()


def ref(job, number, url):
    return {"ref_key": media.sha((job+":"+str(number)).encode()), "job_key": job, "cid": "synthetic-cid",
            "message_id": str(number), "send_time_ms": 1790784000000+number,
            "query_user_nick": "synthetic-shop:worker", "user_id": "synthetic-user", "source_url": url,
            "image_source": {"url": url, "size": 0}}


def result(task, text="synthetic text"):
    frames = []
    for dimensions in task["frames"]:
        entries = [] if not text else [{"text": text, "confidence": 0.7, "box": [[0, 0], [4, 0], [4, 4], [0, 4]]}]
        frames.append({**dimensions, "entries": entries, "text": text})
    return {"image_sha256": task["image_sha256"], "profile_id": task["profile_id"],
            "engine": task["profile"]["engine"], "version": task["profile"]["engine_version"], "frames": frames}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(media.encoded(value))


def artifact(root, path):
    data = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": media.sha(data)}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="media-store-test-", dir=HERE)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base/"unified"
        self.root.mkdir()
        self.db = self.root/"state.sqlite3"
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            connection.execute("CREATE TABLE controller_fixture(value TEXT)")
            connection.execute("INSERT INTO controller_fixture VALUES('unchanged')")
            connection.commit()
        self.store = media.MediaStore(self.root, self.db)

    def tearDown(self):
        self.temporary.cleanup()

    def download(self, data, worker="download-fixture", pid=123):
        task = self.store.next_download(worker, pid)
        self.assertIsNotNone(task)
        Path(task["output_path"]).write_bytes(data)
        return task, self.store.record_download(task, expected_sha256=media.sha(data), expected_bytes=len(data))

    def test_same_url_keeps_every_reference_and_downloads_once(self):
        references = [ref("job-a", 1, "https://example.invalid/one.png"), ref("job-a", 2, "https://example.invalid/one.png")]
        self.assertEqual(self.store.add_refs("job-a", references)["added_refs"], 2)
        self.assertEqual(self.store.add_refs("job-a", references)["added_refs"], 0)
        self.download(png())
        self.store.add_refs("job-b", [ref("job-b", 1, references[0]["source_url"])])
        self.assertIsNone(self.store.next_download("second-worker", 124))
        task = self.store.next_ocr(PROFILE, "ocr-fixture", 125)
        self.store.record_ocr(task, result(task))
        state = self.store.job_media_status("job-a")
        self.assertEqual(state["total_refs"], 2)
        self.assertEqual(state["counts"]["complete"], 2)
        self.assertEqual(len({value["ref_key"] for value in state["refs"]}), 2)
        self.assertEqual(self.store.job_media_status("job-b")["counts"]["complete"], 1)
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM controller_fixture").fetchone()[0], "unchanged")

    def test_two_urls_same_blob_require_two_downloads_but_one_ocr(self):
        urls = ["https://example.invalid/a.png", "https://example.invalid/b.png"]
        self.store.add_refs("job", [ref("job", number, url) for number, url in enumerate(urls)])
        first, second = self.store.next_download("download-a", 1), self.store.next_download("download-b", 2)
        self.assertNotEqual(first["url_key"], second["url_key"])
        self.assertIsNone(self.store.next_ocr(PROFILE, "ocr-before-download", 3))
        for task in (first, second):
            Path(task["output_path"]).write_bytes(png())
        with ThreadPoolExecutor(max_workers=2) as pool:
            blobs = list(pool.map(self.store.record_download, (first, second)))
        self.assertEqual(blobs[0]["image_sha256"], blobs[1]["image_sha256"])
        self.assertEqual(len(list((self.root/"shared/blobs").glob("*/*.image"))), 1)
        self.assertFalse(list((self.root/"shared").rglob("*.part")))
        task = self.store.next_ocr(PROFILE, "ocr", 4)
        self.assertIsNone(self.store.next_ocr(PROFILE, "other-ocr", 5))
        self.store.record_ocr(task, result(task))
        self.assertIsNone(self.store.next_ocr(PROFILE, "third-ocr", 6))
        self.assertTrue(self.store.job_media_status("job")["complete"])

    def test_url_identity_is_exact_and_references_cannot_be_rewritten(self):
        values = [ref("job", 1, "HTTPS://example.invalid/a.png"), ref("job", 2, "https://example.invalid/a.png")]
        self.store.add_refs("job", values)
        self.assertNotEqual(self.store.next_download("a", 1)["url_key"], self.store.next_download("b", 2)["url_key"])
        altered = {**values[0], "message_id": "different"}
        with self.assertRaisesRegex(media.MediaStoreError, "reference_identity_conflict"):
            self.store.add_refs("job", [altered])

    def test_concurrent_claim_is_exclusive(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/a.png")])
        barrier = threading.Barrier(8)
        def claim(number):
            barrier.wait(timeout=10)
            return self.store.next_download("worker-"+str(number), 100+number)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(claim, range(8)))
        tasks = [value for value in values if value is not None]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(self.store.claimed_tasks(), tasks)
        wrong = {**tasks[0], "pid": 999}
        with self.assertRaisesRegex(media.MediaStoreError, "claim_not_current"):
            self.store.fail_download(wrong, "unexpected_error")

    def test_same_image_cannot_be_claimed_concurrently_across_profiles(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/shared.png")])
        self.download(png())
        other_profile = {**copy.deepcopy(PROFILE), "parameters": {"threads": 2}}
        barrier = threading.Barrier(2)
        def claim(number):
            barrier.wait(timeout=10)
            return self.store.next_ocr((PROFILE, other_profile)[number], "ocr-"+str(number), 200+number)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2)))
        tasks = [task for task in claims if task is not None]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len([task for task in self.store.claimed_tasks() if task["kind"] == "ocr"]), 1)
        self.store.record_ocr(tasks[0], result(tasks[0]))
        self.assertIsNone(self.store.next_ocr(PROFILE, "later-a", 301))
        self.assertIsNone(self.store.next_ocr(other_profile, "later-b", 302))
        state = self.store.job_media_status("job")
        self.assertEqual(state["refs"][0]["ocr"]["profile_id"], tasks[0]["profile_id"])

    def test_only_explicit_dead_claim_release_allows_a_new_owner(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/a.png")])
        old = self.store.next_download("old", 100)
        with self.assertRaisesRegex(media.MediaStoreError, "explicit_verified_death"):
            self.store.release_claim(old)
        self.store.release_claim(old, confirmed_dead=True)
        new = self.store.next_download("new", 101)
        self.assertNotEqual(old["claim_id"], new["claim_id"])
        with self.assertRaisesRegex(media.MediaStoreError, "claim_not_current"):
            self.store.fail_download(old, "unexpected_error")
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM media_claims WHERE claim_id=?", (old["claim_id"],)).fetchone()[0], "released_confirmed_dead")

    def test_bad_hash_path_and_decode_are_rejected_without_false_success(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/a.png")])
        task = self.store.next_download("a", 1)
        source = Path(task["output_path"])
        source.write_bytes(png())
        with self.assertRaisesRegex(media.MediaStoreError, "download_hash_mismatch"):
            self.store.record_download(task, expected_sha256="0"*64)
        outside = self.base/"outside.png"
        outside.write_bytes(png())
        with self.assertRaisesRegex(media.MediaStoreError, "download_result_path"):
            self.store.record_download(task, outside)
        source.write_bytes(b"not an image")
        with self.assertRaisesRegex(media.MediaStoreError, "image_decode_failed"):
            self.store.record_download(task)
        self.store.fail_download(task, "invalid_download")
        self.assertEqual(self.store.job_media_status("job")["counts"]["failed_review"], 1)
        self.assertFalse(list((self.root/"shared/blobs").glob("*/*.image")))

    def test_failed_download_does_not_block_other_urls_or_retry_itself(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/one.png"), ref("job", 2, "https://example.invalid/two.png")])
        task = self.store.next_download("bad", 1)
        self.store.fail_download(task, "http_error")
        self.download(png(), "good", 2)
        ocr = self.store.next_ocr(PROFILE, "ocr", 3)
        self.store.record_ocr(ocr, result(ocr))
        state = self.store.job_media_status("job")
        self.assertEqual((state["counts"]["failed_review"], state["counts"]["complete"]), (1, 1))
        self.assertIsNone(self.store.next_download("no_retry", 4))

    def test_failed_ocr_isolated_and_profile_and_frames_are_verified(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/red.png"), ref("job", 2, "https://example.invalid/blue.png")])
        self.download(png("red"))
        self.download(png("blue"))
        first = self.store.next_ocr(PROFILE, "first", 1)
        bad = result(first)
        bad["profile_id"] = "0"*64
        with self.assertRaisesRegex(media.MediaStoreError, "ocr_running_profile"):
            self.store.record_ocr(first, bad)
        bad = result(first)
        bad["frames"][0]["width"] = 9
        with self.assertRaisesRegex(media.MediaStoreError, "frame_dimensions"):
            self.store.record_ocr(first, bad)
        self.store.fail_ocr(first, "invalid_ocr_result")
        second = self.store.next_ocr(PROFILE, "second", 2)
        self.assertNotEqual(first["image_sha256"], second["image_sha256"])
        self.store.record_ocr(second, result(second))
        self.assertIsNone(self.store.next_ocr(PROFILE, "no_retry", 3))
        state = self.store.job_media_status("job")
        self.assertEqual((state["counts"]["failed_review"], state["counts"]["complete"]), (1, 1))

    def test_animation_requires_evidence_for_every_decoded_frame(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), "red").save(buffer, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (8, 6), "blue")], duration=100, loop=0)
        self.store.add_refs("animation", [ref("animation", 1, "https://example.invalid/animation.gif")])
        _, blob = self.download(buffer.getvalue())
        self.assertEqual(len(blob["info"]["frames"]), 2)
        task = self.store.next_ocr(PROFILE, "ocr", 1)
        incomplete = result(task)
        incomplete["frames"] = incomplete["frames"][:1]
        with self.assertRaisesRegex(media.MediaStoreError, "frame_count_mismatch"):
            self.store.record_ocr(task, incomplete)
        self.store.record_ocr(task, result(task))
        self.assertTrue(self.store.job_media_status("animation")["complete"])

    def make_legacy_index(self, texts=("legacy-one", "legacy-two", "legacy-one")):
        source = self.base/"legacy"
        source.mkdir(exist_ok=True)
        data = png()
        image_sha, url = media.sha(data), "https://example.invalid/legacy.png"
        url_sha = media.sha(url.encode())
        receipts, rows = [], []
        for index, text in enumerate(texts):
            artifact_root = source/"parallel"/("legacy-"+str(index))/"codex"
            image = artifact_root/"images"/(url_sha+".png")
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(data)
            image_entry = artifact(artifact_root, image)
            metadata = artifact_root/"images"/(url_sha+".json")
            write_json(metadata, {"url": url, "sha256": image_sha, "actual_bytes": len(data), "artifact": image_entry})
            ocr = artifact_root/"ocr"/(image_sha+".json")
            write_json(ocr, {"image_sha256": image_sha, "engine": "legacy-engine", "version": "unknown-parameters",
                "frames": [{"frame_index": 0, "width": 8, "height": 6, "text": text,
                    "entries": [{"text": text, "confidence": 0.9, "box": [[0, 0], [4, 0], [4, 4], [0, 4]]}]}]})
            metadata_entry, ocr_entry = artifact(artifact_root, metadata), artifact(artifact_root, ocr)
            receipt = artifact_root/"month_items"/("fixture-"+str(index))/"receipt.json"
            write_json(receipt, {"status": "complete", "artifacts": [metadata_entry, image_entry, ocr_entry,
                {"path": "month_items/fixture/raw/not-readable.json", "bytes": 1, "sha256": "0"*64}]})
            receipts.append({**artifact(source, receipt), "artifact_root": artifact_root.relative_to(source).as_posix()})
            rows.append({"receipt_index": index, "url_sha256": url_sha, "image_sha256": image_sha,
                         "metadata": metadata_entry, "image": image_entry, "ocr": ocr_entry})
        index_path = source/"media-source-index.json"
        write_json(index_path, {"schema": 1, "scope": "completed_media_only", "source_root": str(source), "receipts": receipts, "media": rows})
        return source, index_path, url, image_sha

    def test_legacy_variants_and_every_source_are_kept_without_relabeling_profile(self):
        source, index, url, image_sha = self.make_legacy_index()
        before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
        imported = self.store.import_verified_legacy(source, index, expected_index_sha256=media.sha(index.read_bytes()))
        self.assertEqual(imported["verified_source_entries"], 3)
        self.store.add_refs("job", [ref("job", 1, url), ref("job", 2, url)])
        self.assertIsNone(self.store.next_download("not_needed", 1))
        self.assertIsNone(self.store.next_ocr(PROFILE, "not_needed", 2))
        versions = self.store.ocr_versions(image_sha)
        self.assertEqual(len(versions), 2)
        self.assertEqual(sum(len(version["sources"]) for version in versions), 3)
        self.assertTrue(all(version["profile_id"] is None and version["provenance"] == "imported_verified_legacy" for version in versions))
        state = self.store.job_media_status("job")
        self.assertTrue(state["complete"])
        self.assertEqual(state["refs"][0]["ocr"]["sha256"], min(version["sha256"] for version in versions))
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.store.import_verified_legacy(source, index, expected_index_sha256=media.sha(index.read_bytes()))
        self.assertEqual(sum(len(version["sources"]) for version in self.store.ocr_versions(image_sha)), 3)

    def test_legacy_bad_index_hash_media_hash_and_path_are_rejected(self):
        source, index, _, _ = self.make_legacy_index(("legacy",))
        with self.assertRaisesRegex(media.MediaStoreError, "legacy_index_hash"):
            self.store.import_verified_legacy(source, index, expected_index_sha256="0"*64)
        value = json.loads(index.read_bytes())
        image = source/value["receipts"][0]["artifact_root"]/value["media"][0]["image"]["path"]
        original = image.read_bytes()
        image.write_bytes(png("blue"))
        with self.assertRaisesRegex(media.MediaStoreError, "artifact_hash_or_size"):
            self.store.import_verified_legacy(source, index, expected_index_sha256=media.sha(index.read_bytes()))
        image.write_bytes(original)
        value["media"][0]["image"]["path"] = "../outside.png"
        write_json(index, value)
        with self.assertRaisesRegex(media.MediaStoreError, "legacy_artifact_not_receipted"):
            self.store.import_verified_legacy(source, index, expected_index_sha256=media.sha(index.read_bytes()))
        self.assertFalse(list((self.root/"shared/ocr").rglob("*.json")))

    def test_completed_blob_corruption_is_reported_without_overwriting_it(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/a.png")])
        _, blob = self.download(png())
        path = self.root/blob["path"]
        path.write_bytes(b"changed")
        state = self.store.job_media_status("job")
        self.assertEqual(state["counts"]["failed_review"], 1)
        self.assertEqual(path.read_bytes(), b"changed")

    def test_database_boundary_and_empty_media_job(self):
        with self.assertRaisesRegex(media.MediaStoreError, "database_outside"):
            media.MediaStore(self.root, self.base/"outside.sqlite3")
        self.store.add_refs("no-images", [])
        self.assertTrue(self.store.job_media_status("no-images")["complete"])

    @contextlib.contextmanager
    def locked_database(self):
        connect = self.store._connect
        def short_timeout():
            connection = connect()
            connection.execute("PRAGMA busy_timeout=1")
            return connection
        with contextlib.closing(connect()) as writer, patch.object(self.store, "_connect", side_effect=short_timeout):
            writer.execute("BEGIN IMMEDIATE")
            try:
                yield
            finally:
                writer.rollback()

    def test_download_cas_is_reported_when_database_is_busy_without_adoption(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/one.png")])
        task = self.store.next_download("download", 1)
        Path(task["output_path"]).write_bytes(png())
        with self.locked_database(), self.assertRaises(media.MediaStorageError) as caught:
            self.store.record_download(task)
        error = caught.exception
        self.assertEqual(error.claim, media.claim_identity(task))
        self.assertEqual(error.diagnostic["sqlite_errorcode"], sqlite3.SQLITE_BUSY)
        self.assertEqual(error.diagnostic["sqlite_errorname"], "SQLITE_BUSY")
        prepared = error.prepared_artifact
        self.assertEqual(set(prepared), {"path", "bytes", "sha256"})
        self.assertEqual(media.read_artifact(self.root, prepared), png())
        self.assertTrue(Path(task["output_path"]).exists())
        self.assertEqual(len(self.store.claimed_tasks()), 1)
        self.assertIsNone(self.store.next_download("no-automatic-release", 2))
        with contextlib.closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT state FROM media_urls").fetchone()[0], "claimed")

    def test_ocr_cas_is_reported_when_database_is_busy_without_adoption(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/one.png")])
        self.download(png())
        task = self.store.next_ocr(PROFILE, "ocr", 1)
        private_text = "INJECTED_CUSTOMER_TEXT_URL_KEY_HINT_TRACE"
        with self.locked_database(), self.assertRaises(media.MediaStorageError) as caught:
            self.store.record_ocr(task, result(task, private_text))
        error = caught.exception
        self.assertEqual(error.claim, media.claim_identity(task))
        self.assertEqual(error.diagnostic["error_code"], "sqlite_busy")
        prepared = error.prepared_artifact
        self.assertEqual(set(prepared), {"path", "bytes", "sha256"})
        self.assertEqual(json.loads(media.read_artifact(self.root, prepared))["frames"][0]["text"], private_text)
        self.assertNotIn(private_text, json.dumps(media.safe_media_error(error)))
        self.assertEqual(self.store.ocr_versions(task["image_sha256"]), [])
        self.assertEqual(len(self.store.claimed_tasks()), 1)
        self.assertIsNone(self.store.next_ocr(PROFILE, "no-automatic-release", 2))

    def test_claim_commit_ack_failure_retains_opaque_attempt_identity(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/one.png")])
        transaction = self.store._transaction
        @contextlib.contextmanager
        def missing_ack():
            with transaction() as connection:
                yield connection
            error = sqlite3.OperationalError("INJECTED_PRIVATE_MESSAGE")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            error.sqlite_errorname = "SQLITE_BUSY"
            raise error
        with patch.object(self.store, "_transaction", side_effect=missing_ack), self.assertRaises(media.MediaStorageError) as caught:
            self.store.next_download("download", 1)
        self.assertEqual(caught.exception.claim, media.claim_identity(self.store.claimed_tasks()[0]))
        self.assertNotIn("INJECTED_PRIVATE_MESSAGE", json.dumps(media.safe_media_error(caught.exception)))
        self.assertIsNone(self.store.next_download("no-retry", 2))

    def test_failure_codes_and_diagnostics_use_only_controlled_values(self):
        self.store.add_refs("job", [ref("job", 1, "https://example.invalid/one.png")])
        task = self.store.next_download("download", 1)
        private = "INJECTED_CUSTOMER_URL_KEY_HINT_TRACE"
        for code in (private, "http_404", "https://example.invalid/private"):
            with self.assertRaisesRegex(media.MediaStoreError, "safe_error_code_required"):
                self.store.fail_download(task, code)
        self.assertEqual(media.safe_media_error(media.MediaStoreError(private)), {"error_code":"media_store_error"})
        error = sqlite3.OperationalError(private)
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        error.sqlite_errorname = "SQLITE_BUSY"
        self.assertEqual(media.safe_media_error(error), {"error_code":"sqlite_busy", "sqlite_errorcode":5, "sqlite_errorname":"SQLITE_BUSY"})
        for name in (private, "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_IOERR"):
            error.sqlite_errorname = name
            self.assertEqual(media.safe_media_error(error), {"error_code":"sqlite_busy", "sqlite_errorcode":5})
        descriptor = {"path":"https://example.invalid/"+private,"sha256":"0"*64,"bytes":1}
        self.assertIsNone(media.MediaStorageError(error, prepared_artifact=descriptor).prepared_artifact)

    def test_latest_reference_priority_skips_unreferenced_urls_and_uses_image_ties(self):
        references = [ref("job", 10, "https://example.invalid/a.png"),
                      ref("job", 40, "https://example.invalid/a.png"),
                      ref("job", 30, "https://example.invalid/b.png"),
                      ref("job", 40, "https://example.invalid/c.png")]
        # Keep the two equal-priority references distinct within the job.
        references[-1]["ref_key"] = media.sha(b"tied-c")
        self.store.add_refs("job", references)
        with contextlib.closing(self.store._connect()) as connection:
            connection.execute("INSERT INTO media_urls(url_key,source_url,state) VALUES(?,?,'pending')", ("0"*64,"synthetic:unreferenced"))
        newest = sorted({media.sha(row["source_url"].encode()) for row in references if row["send_time_ms"] == 1790784000040})
        tasks = [self.store.next_download("download-"+str(i), i+1) for i in range(3)]
        self.assertEqual([task["task_key"] for task in tasks[:2]], newest)
        self.assertIsNone(self.store.next_download("unreferenced", 10))
        images = {}
        for task, color in zip(tasks, ("red", "blue", "green")):
            Path(task["output_path"]).write_bytes(png(color))
            blob = self.store.record_download(task)
            images[task["task_key"]] = blob["image_sha256"]
        expected = sorted(images[key] for key in newest)
        first = self.store.next_ocr(PROFILE, "ocr-a", 20)
        second = self.store.next_ocr(PROFILE, "ocr-b", 21)
        self.assertEqual([first["image_sha256"],second["image_sha256"]], expected)
        self.store.fail_ocr(first, "invalid_ocr_result")
        self.store.record_ocr(second, result(second))
        last = self.store.next_ocr(PROFILE, "ocr-c", 22)
        self.assertNotIn(last["image_sha256"], expected)
        self.store.record_ocr(last, result(last))
        self.assertIsNone(self.store.next_ocr(PROFILE, "no-retry", 23))

    def test_image_priority_uses_newest_complete_url_and_ignores_failed_and_unreferenced_urls(self):
        refs = [ref("job", 10, "https://example.invalid/red-old.png"),
                ref("job", 50, "https://example.invalid/red-new.png"),
                ref("job", 30, "https://example.invalid/blue.png"),
                ref("job", 40, "https://example.invalid/green.png")]
        self.store.add_refs("job", refs)
        colors = {media.sha(row["source_url"].encode()): "red" if "red-" in row["source_url"]
                  else "blue" if "blue" in row["source_url"] else "green" for row in refs}
        for _ in refs:
            task = self.store.next_download("download", 1)
            Path(task["output_path"]).write_bytes(png(colors[task["url_key"]]))
            self.store.record_download(task)
        # A newer failed URL must not raise the priority of its shared image.
        failed = ref("job", 100, "https://example.invalid/failed-blue.png")
        self.store.add_refs("job", [failed])
        with contextlib.closing(self.store._connect()) as connection:
            connection.execute("UPDATE media_urls SET state='failed_review',image_sha=? WHERE url_key=?",
                (media.sha(png("blue")), media.sha(failed["source_url"].encode())))
            # A complete URL with no reference cannot make a blob eligible.
            blob = self.store._store_blob(png("yellow"))
            self.store._insert_blob(connection, blob)
            connection.execute("INSERT INTO media_urls(url_key,source_url,state,image_sha) VALUES(?,?,'complete',?)",
                ("0"*64, "synthetic:unreferenced", blob["image_sha256"]))
        expected = [media.sha(png(color)) for color in ("red", "green", "blue")]
        actual = []
        for _ in expected:
            task = self.store.next_ocr(PROFILE, "ocr", 2)
            actual.append(task["image_sha256"])
            self.store.record_ocr(task, result(task))
        self.assertEqual(actual, expected)
        self.assertIsNone(self.store.next_ocr(PROFILE, "ocr", 2))

    def test_current_pending_and_other_profile_failed_images_remain_eligible(self):
        self.store.add_refs("job", [ref("job", number, "https://example.invalid/"+color+".png")
                                    for number, color in enumerate(("red", "blue"))])
        for color in ("blue", "red"):
            self.download(png(color))
        value, current_id = media.profile_value(PROFILE)
        other_value, other_id = media.profile_value({**copy.deepcopy(PROFILE), "parameters": {"threads": 2}})
        with contextlib.closing(self.store._connect()) as connection:
            connection.executemany("INSERT INTO media_profiles VALUES(?,?)",
                [(current_id, media.encoded(value).decode()), (other_id, media.encoded(other_value).decode())])
            connection.execute("INSERT INTO media_ocr_tasks(image_sha,profile_id,state) VALUES(?,?,'pending')",
                (media.sha(png("red")), current_id))
            connection.execute("INSERT INTO media_ocr_tasks(image_sha,profile_id,state) VALUES(?,?,'failed_review')",
                (media.sha(png("blue")), other_id))
        tasks = [self.store.next_ocr(PROFILE, "ocr-"+str(i), i+1) for i in range(2)]
        self.assertEqual({task["image_sha256"] for task in tasks}, {media.sha(png(c)) for c in ("red", "blue")})
        self.assertTrue(all(task["profile_id"] == current_id for task in tasks))
        self.assertIsNone(self.store.next_ocr(PROFILE, "no-duplicate", 3))
        for task in tasks:
            self.store.record_ocr(task, result(task))
        with contextlib.closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT state FROM media_ocr_tasks WHERE profile_id=?", (other_id,)).fetchone()[0], "failed_review")

    def test_four_downloads_and_four_ocrs_share_one_ledger_without_duplicate_claims(self):
        colors = ("red", "blue", "green", "yellow", "purple", "orange", "cyan", "black")
        self.store.add_refs("job", [ref("job", number, "https://example.invalid/"+color+".png") for number,color in enumerate(colors)])
        data = {media.sha(("https://example.invalid/"+color+".png").encode()): png(color) for color in colors}
        for _ in range(4):
            task = self.store.next_download("seed", 1)
            Path(task["output_path"]).write_bytes(data[task["task_key"]])
            self.store.record_download(task)
        barrier = threading.Barrier(8)
        def work(number):
            barrier.wait(timeout=10)
            if number < 4:
                task = self.store.next_download("download-"+str(number), number+10)
                Path(task["output_path"]).write_bytes(data[task["task_key"]])
                self.store.record_download(task)
            else:
                task = self.store.next_ocr(PROFILE, "ocr-"+str(number), number+10)
                self.store.record_ocr(task, result(task))
            return task
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(work, range(8)))
        self.assertEqual(len({task["claim_id"] for task in tasks}), 8)
        self.assertEqual(len({task["task_key"] for task in tasks[:4]}), 4)
        self.assertEqual(len({task["image_sha256"] for task in tasks[4:]}), 4)
        self.assertEqual(self.store.claimed_tasks(), [])
        with contextlib.closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_urls WHERE state='complete'").fetchone()[0], 8)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_ocr_versions").fetchone()[0], 4)

    def test_candidate_indexes_reduce_work_on_representative_reference_fanout(self):
        old_download = """SELECT u.url_key,u.source_url,MAX(r.send_time_ms) priority FROM media_urls u
            JOIN media_refs r USING(url_key) WHERE u.state='pending' GROUP BY u.url_key
            ORDER BY priority DESC,u.url_key LIMIT 1"""
        old_ocr = """SELECT b.*,MAX(r.send_time_ms) priority FROM media_blobs b
            JOIN media_urls u ON u.image_sha=b.image_sha JOIN media_refs r USING(url_key)
            LEFT JOIN media_ocr_tasks t ON t.image_sha=b.image_sha AND t.profile_id=?
            WHERE u.state='complete' AND (t.state IS NULL OR t.state='pending')
            AND NOT EXISTS(SELECT 1 FROM media_ocr_tasks active WHERE active.image_sha=b.image_sha AND active.state='claimed')
            AND NOT EXISTS(SELECT 1 FROM media_ocr_versions v WHERE v.image_sha=b.image_sha)
            GROUP BY b.image_sha ORDER BY priority DESC,b.image_sha LIMIT 1"""
        with contextlib.closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            for number in range(600):
                image_sha, key = media.sha(("image-"+str(number//2)).encode()), media.sha(str(number).encode())
                connection.execute("INSERT OR IGNORE INTO media_blobs VALUES(?,?,1,'{}')", (image_sha,"synthetic-unused.image"))
                connection.execute("INSERT INTO media_urls(url_key,source_url,state,image_sha) VALUES(?,?,?,?)",
                    (key,"synthetic:"+str(number),"pending" if number%3 == 0 else "complete",image_sha))
                connection.executemany("INSERT INTO media_refs VALUES('fixture',?,?,?,?,'{}')",
                    ((media.sha((str(number)+":"+str(index)).encode()),key,"synthetic",100000+number*24+index) for index in range(24)))
            connection.commit()
            def measure(sql, parameters):
                steps = 0
                def progress():
                    nonlocal steps
                    steps += 100
                    return 0
                connection.set_progress_handler(progress,100)
                try:
                    row = dict(connection.execute(sql,parameters).fetchone())
                finally:
                    connection.set_progress_handler(None,0)
                return row,steps,[entry[3] for entry in connection.execute("EXPLAIN QUERY PLAN "+sql,parameters)]
            connection.executescript("DROP INDEX media_refs_url_priority; DROP INDEX media_urls_state_image; CREATE INDEX media_refs_url ON media_refs(url_key);")
            old = [measure(old_download,()),measure(old_ocr,("0"*64,))]
            # Isolate the index effect on the new SQL as well as the total improvement.
            without_cover = [measure(media.DOWNLOAD_CANDIDATE_SQL,()),measure(media.OCR_CANDIDATE_SQL,("0"*64,))]
            connection.executescript(media.SCHEMA)
            new = [measure(media.DOWNLOAD_CANDIDATE_SQL,()),measure(media.OCR_CANDIDATE_SQL,("0"*64,))]
            for (old_row,old_steps,_),(_,uncovered_steps,_),(new_row,new_steps,plan) in zip(old,without_cover,new):
                self.assertEqual(old_row,new_row)
                self.assertLess(new_steps,old_steps//2)
                self.assertLess(new_steps,uncovered_steps//2)
                self.assertTrue(any("COVERING INDEX media_refs_url_priority" in line for line in plan),plan)
                self.assertTrue(any("INDEX media_urls_state_image" in line for line in plan),plan)
                self.assertFalse(any("GROUP BY" in line for line in plan),plan)
            self.assertNotIn("media_refs_url",[row[1] for row in connection.execute("PRAGMA index_list(media_refs)")])


if __name__ == "__main__":
    unittest.main()
