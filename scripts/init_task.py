"""Initialize one new unified task from frozen month manifests; no external calls."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
from source_manifest import source_manifest

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts/unified"))
from message_export import context_key
from task_queue import TaskQueue


class InitError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode("utf-8")


def initialize(task_root, manifests, node, opencli_script, ocr_python):
    root = Path(task_root).resolve()
    if root.exists() or root.is_relative_to(SKILL_ROOT):
        raise InitError("task_root_must_be_new_and_outside_skill")
    runtime = {}
    for key, value in (("node", node), ("opencli_script", opencli_script), ("ocr_python", ocr_python)):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise InitError("runtime_requires_existing_absolute_files")
        runtime[key] = str(path.resolve())
    inputs, seen, owners, common = [], set(), {"codex": 0, "kimi": 0}, None
    for filename in manifests:
        path = Path(filename).resolve()
        data = path.read_bytes()
        manifest = json.loads(data)
        scope = {key: manifest[key] for key in ("start", "end", "timezone", "expected_nick")}
        if manifest.get("schema") != 2 or manifest.get("scope") != "month" or scope["timezone"] != "Asia/Shanghai" or not isinstance(scope["expected_nick"], str) or not scope["expected_nick"].strip():
            raise InitError("invalid_month_manifest")
        if common is not None and scope != common:
            raise InitError("manifest_scope_or_identity_differs")
        common = scope
        if not isinstance(manifest.get("items"), list):
            raise InitError("manifest_items_must_be_array")
        for item in manifest["items"]:
            key = context_key(scope, item)
            if key in seen or item.get("lane") not in owners:
                raise InitError("duplicate_context_or_invalid_owner")
            day = item["latest_seen_day"]
            if not scope["start"] <= day < scope["end"] or dt.date.fromisoformat(day).isoformat() != day:
                raise InitError("priority_day_outside_scope")
            seen.add(key)
            owners[item["lane"]] += 1
        inputs.append({"original_path": str(path), "sha256": sha(data), "data": data})
    if not seen or abs(owners["codex"]-owners["kimi"]) > 1:
        raise InitError("nonempty_evenly_assigned_manifest_required")
    files = source_manifest(SKILL_ROOT)
    sync = {"schema": 1, "version": "unified-v4", "master": str(SKILL_ROOT), "files": files}
    # All input validation finishes before creating the new task directory.
    root.mkdir(parents=True, exist_ok=False)
    copied = []
    for index, source in enumerate(inputs, 1):
        relative = "month-manifest.json" if len(inputs) == 1 else f"inputs/{index:03d}/month-manifest.json"
        path = root / relative
        write(path, source["data"])
        copied.append({"path": relative, "original_path": source["original_path"], "sha256": source["sha256"]})
    write(root / "skill-sync.json", json_bytes(sync))
    write(root / "codex-runtime.json", json_bytes(runtime))
    summary = TaskQueue(root / "unified/state.sqlite3").initialize([root / source["path"] for source in copied])
    receipt = {"schema": 1, "status": "initialized", "pipeline": "unified", "task_root": str(root),
        "scope": common, "manifests": copied, "queue": summary,
        "skill_sync_sha256": sha((root / "skill-sync.json").read_bytes()),
        "runtime_sha256": sha((root / "codex-runtime.json").read_bytes()),
        "external_calls": 0, "browser_created": False, "ocr_profile_created": False,
        "enumeration_completeness_asserted": False}
    write(root / "task-init.json", json_bytes(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, action="append", type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--opencli-script", required=True, type=Path)
    parser.add_argument("--ocr-python", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = initialize(args.task_root, args.manifest, args.node, args.opencli_script, args.ocr_python)
    except (InitError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 2
    print(json.dumps({"status": receipt["status"], "jobs": receipt["queue"]["jobs"], "owners": receipt["queue"]["owners"], "external_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
