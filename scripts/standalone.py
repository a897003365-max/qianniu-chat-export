"""Standalone authorized history export. No old task, session, owner split or OCR required."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from urllib.parse import quote
from source_manifest import source_manifest

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE / "unified"))
import qianniu_export as q
from message_export import collect_messages, context_key, ItemConflict, MessageExportError
from timed_bridge import TimedBrowser, BridgeError

HISTORY_URL = "https://market.m.taobao.com/app/qn/im-history-search/index.html"
LOGIN_URL = "https://login.taobao.com/member/login.jhtml?redirectURL=" + quote(HISTORY_URL, safe="")
CONVERSATIONS = "mtop.taobao.wireless.amp2.paas.conversation.list"


class StandaloneError(RuntimeError):
    pass


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def resolve_runtime(node=None, opencli_script=None):
    node = node or os.environ.get("QIANNIU_NODE") or shutil.which("node")
    explicit = opencli_script or os.environ.get("QIANNIU_OPENCLI_SCRIPT")
    if explicit:
        script = Path(explicit)
    else:
        command = shutil.which("opencli") or shutil.which("opencli.cmd") or shutil.which("opencli.ps1")
        bases = [Path(command).parent] if command else []
        if os.environ.get("APPDATA"):
            bases.append(Path(os.environ["APPDATA"]) / "npm")
        candidates = [base / "node_modules/@jackwener/opencli/dist/src/main.js" for base in bases]
        script = next((p for p in candidates if p.is_file()), None)
    if not node or not Path(node).is_file():
        raise StandaloneError("node_missing")
    if script is None or not script.is_file():
        raise StandaloneError("opencli_missing")
    return {"node": str(Path(node).resolve()), "opencli_script": str(script.resolve()), "ocr_python": None}


def doctor(args):
    runtime = resolve_runtime(args.node, args.opencli_script)
    version = subprocess.run([runtime["node"], "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=15, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if version.returncode or not version.stdout.strip().startswith("v"):
        raise StandaloneError("node_not_runnable")
    return {"status": "ready_for_browser_probe", "python": sys.executable, "python_version": list(sys.version_info[:3]),
            **runtime, "node_version": version.stdout.strip(), "pillow_available": importlib.util.find_spec("PIL") is not None,
            "channel": "official_history_h5_via_opencli", "native_im_bridge": "not_established",
            "browser_login_verified": False, "message_access_verified": False, "network_calls": 0,
            "next": "probe --output NEW_DIRECTORY, or collect --start YYYY-MM-DD --end YYYY-MM-DD --output NEW_DIRECTORY"}


def account(value):
    if value is None:
        return None
    nick = value.removeprefix("cntaobao").strip()
    if not nick or any(ord(c) < 32 for c in nick):
        raise StandaloneError("invalid_account_filter")
    return nick


def freeze_sources(root):
    files = source_manifest(SKILL)
    q.atomic_json(root / "skill-sync.json", {"schema": 1, "version": "standalone-v5", "master": str(SKILL), "files": files})


def failure_details(error):
    code = error.code if isinstance(error, BridgeError) else str(error) if isinstance(error, StandaloneError) else type(error).__name__
    hints = {
        "node_missing": "安装现有环境需要的 Node，或显式传 --node；不需要旧采集任务。",
        "opencli_missing": "提供已安装 OpenCLI 的 main.js 路径：--opencli-script。",
        "auth_required": "在同一 Chrome 打开 manual_entry_url 的淘宝登录入口，登录授权账号。若自动跳转天猫商家中心，再明确执行一次新目录的有界 collect 核验；只刷新聊天页不会刷新已过期的登录。",
        "verification_required": "在 Chrome 官方聊天记录页查询一次并完成验证。停止本轮请求，完成后再明确执行新采集。",
        "target_mismatch": "新标签发生跳转。打开官方聊天记录页确认登录与页面，再使用新的输出目录执行。",
        "mtop_not_ready": "官方页面尚未提供 MTOP。先在 Chrome 确认页面已加载，再明确执行新采集。",
        "cli_failed": "检查 Chrome 是否运行、OpenCLI 官方扩展是否连接；原 session 过期无需复用，新命令会创建新 session。",
        "identity_mismatch": "当前浏览器账号与 --expected-nick 不符；确认正确账号后再采集。",
        "staff_outside_shop": "客服账号与当前登录账号不属于同一店铺前缀；请核对完整客服账号。",
    }
    return {"error_code": code, "error_type": type(error).__name__,
            "action": hints.get(code, "保留本轮回执，核对错误与运行条件后再明确执行；不自动重试。"),
            "manual_entry_url": LOGIN_URL if code == "auth_required" else HISTORY_URL}


def message_failure(root, key, error):
    code, action = (
        ("message_conflict", "核对失败原始页中同一消息的不同版本，保留全部版本；本轮不重试该上下文。")
        if isinstance(error, ItemConflict) else
        ("message_validation_failed", "核对失败证据中的消息结构、时间范围与游标链；修复前不重试该上下文。")
        if isinstance(error, MessageExportError) else
        ("source_api_or_payload_error", "核对失败证据中的来源返回码或消息字段；先处理来源错误再明确采集。")
    )
    files = sorted((root / "unified/messages" / key / "failures").glob("*.json"))
    return {"job_key": key, "error_type": type(error).__name__, "error_code": code, "action": action,
            "failure_artifacts": [q.artifact(root, path) for path in files]}


def start(args):
    if args.command == "collect":
        dates = q.days(args.start, args.end)
        staff, customer = account(args.staff), account(args.customer)
    else:
        dates, staff, customer = [], None, None
    runtime = resolve_runtime(args.node, args.opencli_script)
    root = args.output.resolve()
    if root.exists() or root.is_relative_to(SKILL):
        raise StandaloneError("output_must_be_new_and_outside_skill")
    root.mkdir(parents=True, exist_ok=False)
    freeze_sources(root)
    q.atomic_json(root / "codex-runtime.json", runtime)
    report = {"schema": 1, "kind": "standalone_history", "command": args.command, "status": "started",
              "created_at": dt.datetime.now(q.TZ).isoformat(), "output": str(root),
              "channel": "official_history_h5_via_opencli", "native_im_bridge": "not_established",
              "message_access_verified": False, "enumeration_complete": False, "jobs": [], "failures": []}
    q.atomic_json(root / "run.json", report)
    try:
        browser = TimedBrowser.open({**runtime, "task_root": str(root)}, "single-" + uuid.uuid4().hex,
                                    "standalone", args.expected_nick, q,
                                    bind_current_identity=args.expected_nick is None, allow_discovery=args.command == "collect")
        q.atomic_json(root / "browser-runtime.json", {"pid": os.getpid(), "runtime_summary": browser.runtime_summary})
        report["identity_verified"] = True
        report["expected_nick"] = browser.expected_nick
        if args.command == "probe":
            report["status"] = "identity_verified"
            report["next"] = "collect proves message access; identity success alone does not"
        else:
            nick = account(browser.expected_nick)
            if staff and staff.split(":", 1)[0] != nick.split(":", 1)[0]:
                raise StandaloneError("staff_outside_shop")
            query_nick = "cntaobao" + (staff or nick)
            scope = {"start": args.start, "end": args.end, "timezone": "Asia/Shanghai", "expected_nick": browser.expected_nick}
            report.update(scope=scope, filters={"staff": staff, "customer": customer, "cid": args.cid},
                          max_pages=None if args.all_messages else args.max_pages)
            items, capped = {}, []
            for day in dates:
                request = {"selfWwNick": "cntaobao" + staff if staff else None,
                           "targetWwNick": "cntaobao" + customer if customer else None,
                           "beginDate": day.replace("-", ""), "endDate": day.replace("-", "")}
                response = browser.request(CONVERSATIONS, request)
                path = root / "discovery" / (day + ".json")
                q.atomic_json(path, {"api": CONVERSATIONS, "request": request, "response": response})
                daily = q.conversation_items(response, day)
                if len(daily) >= 500:
                    capped.append(day)
                for row in daily:
                    item = {**row, "query_user_nick": query_nick, "latest_seen_day": day}
                    items.setdefault((query_nick, row["cid"], row["user_id"]), item)
            ordered = sorted(items.values(), key=lambda x: x["latest_seen_day"], reverse=True)
            q.atomic_json(root / "conversations.json", {"scope": scope, "filters": report["filters"], "items": ordered,
                                                       "capped_days": capped, "enumeration_complete": False})
            selected = [row for row in ordered if not args.cid or row["cid"] == args.cid]
            if args.cid and not selected:
                raise StandaloneError("cid_not_in_authorized_discovery")
            if not args.all_conversations:
                selected = selected[:args.max_conversations]
            report.update(discovered_contexts=len(ordered), selected_contexts=len(selected), capped_days=capped,
                          all_discovered_contexts_selected=len(selected) == len(ordered))
            for item in selected:
                try:
                    receipt = collect_messages(root / "unified", scope, item, browser, q,
                                               max_pages=None if args.all_messages else args.max_pages)
                except (ItemConflict, MessageExportError, q.ExportError) as error:
                    report["failures"].append(message_failure(root, context_key(scope, item), error))
                    q.atomic_json(root / "run.json", report)
                    continue
                directory = root / "unified/messages" / receipt["job_key"]
                filename = "message-receipt.json" if receipt["status"] == "complete" else "sample-receipt.json"
                report["jobs"].append({"job_key": receipt["job_key"], "status": receipt["status"],
                                       "counts": receipt["counts"], "receipt": q.artifact(root, directory / filename)})
                report["message_access_verified"] = True
                q.atomic_json(root / "run.json", report)
            report["status"] = "needs_attention" if report["failures"] else "empty_result" if not selected else "partial" if any(
                job["status"] != "complete" for job in report["jobs"]) or not report["all_discovered_contexts_selected"] else "collected"
            report["counts"] = {name: sum(job["counts"][name] for job in report["jobs"])
                                for name in ("messages", "image_refs", "pages")}
            if report["failures"]:
                report.update(error_code="message_items_failed", action="部分上下文失败；按 failures 中的原因、操作建议和证据路径核对，成功结果已保留。",
                              manual_entry_url=HISTORY_URL)
    except Exception as error:
        report.update(status="needs_attention", **failure_details(error))
    report["finished_at"] = dt.datetime.now(q.TZ).isoformat()
    q.atomic_json(root / "run.json", report)
    return report


def media_stage(args):
    # Media processing is explicit and separate; messages remain usable without these dependencies.
    root = args.output.resolve()
    report = q.read_json(root / "run.json")
    if report.get("kind") != "standalone_history" or report.get("command") != "collect" or Path(report["output"]).resolve() != root:
        raise StandaloneError("not_a_standalone_collection")
    import pipeline
    from media_store import MediaStore
    pipeline.load_core(root)
    store = MediaStore(root / "unified", root / "unified/state.sqlite3")
    for job in report["jobs"]:
        q.verify_artifacts(root, [job["receipt"]])
        receipt = q.read_json(q.checked_path(root, job["receipt"]["path"]))
        q.verify_artifacts(root / "unified", receipt["artifacts"])
        refs_path = root / "unified/messages" / job["job_key"] / "image_refs.jsonl"
        store.add_refs(job["job_key"], [json.loads(line) for line in refs_path.read_text(encoding="utf-8").splitlines()])
    if args.stage == "ocr":
        if args.ocr_python is None or not args.ocr_python.is_file():
            raise StandaloneError("ocr_python_required")
        runtime = q.read_json(root / "codex-runtime.json")
        runtime["ocr_python"] = str(args.ocr_python.resolve())
        profile = pipeline.make_profile(runtime, HERE)
        profile_path = root / "unified/ocr-profile.json"
        if profile_path.exists() and q.read_json(profile_path) != profile:
            raise StandaloneError("ocr_profile_changed")
        q.atomic_json(root / "codex-runtime.json", runtime)
        q.atomic_json(profile_path, profile)
    return pipeline.run_stage(root, args.stage, None, args.workers, args.limit)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "probe", "collect"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--node")
        cmd.add_argument("--opencli-script")
        if name != "doctor":
            cmd.add_argument("--output", required=True, type=Path, help="New output directory; never an old task")
            cmd.add_argument("--expected-nick", help="Optional exact login identity; omission binds current browser login")
        if name == "collect":
            cmd.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD, Asia/Shanghai")
            cmd.add_argument("--end", required=True, help="Exclusive YYYY-MM-DD, Asia/Shanghai")
            cmd.add_argument("--staff", help="Full staff account, same shop as current login")
            cmd.add_argument("--customer", help="Exact customer account filter")
            cmd.add_argument("--cid", help="Select a CID present in this run's discovery")
            group = cmd.add_mutually_exclusive_group()
            group.add_argument("--max-conversations", type=positive, default=1)
            group.add_argument("--all-conversations", action="store_true")
            pages = cmd.add_mutually_exclusive_group()
            pages.add_argument("--max-pages", type=positive, default=1, help="Total pages per selected context; API page size 100")
            pages.add_argument("--all-messages", action="store_true")
    media = commands.add_parser("media", help="Explicit downloads/OCR for one standalone collection")
    media.add_argument("--output", type=Path, required=True)
    media.add_argument("--stage", choices=("downloads", "ocr"), required=True)
    media.add_argument("--ocr-python", type=Path)
    media.add_argument("--workers", type=positive, default=1)
    media.add_argument("--limit", type=positive)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise StandaloneError("python_311_required")
        result = doctor(args) if args.command == "doctor" else media_stage(args) if args.command == "media" else start(args)
    except Exception as error:
        result = {"status": "needs_attention", **failure_details(error)}
    # Logs contain counts and actionable states, never customer messages or account credentials.
    if args.command == "doctor":
        safe = result
    else:
        safe = {k: result[k] for k in ("status", "state", "counts", "discovered_contexts", "selected_contexts",
                "message_access_verified", "error_code", "error_type", "action", "manual_entry_url", "completed", "failed_review", "failures") if k in result}
        safe["output"] = str(args.output.resolve())
    print(json.dumps(safe, ensure_ascii=False))
    return 2 if result.get("status") == "needs_attention" or result.get("state", "drained") != "drained" else 0


if __name__ == "__main__":
    raise SystemExit(main())
