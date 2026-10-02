"""Local profile, queue, browser worker, and review commands."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from .. import config, store
from . import booklet, queue


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jhb-apply")
    ap.add_argument("--booklet", type=Path, default=booklet.DEFAULT_PATH)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-profile")
    init.add_argument("--source", type=Path, required=True)
    sub.add_parser("missing")
    edit = sub.add_parser("answer")
    edit.add_argument("key")
    edit.add_argument("--role", choices=["sde", "ml"])
    edit.add_argument("--decline", action="store_true")
    custom = sub.add_parser("custom-answer")
    custom.add_argument("question")
    work = sub.add_parser("worker")
    work.add_argument("--if-enabled", action="store_true")
    work.add_argument("--planner", choices=["codex", "deterministic"], default="codex")
    work.add_argument("--headless", action="store_true")
    work.add_argument("--interactive", action="store_true")
    work.add_argument("--review-seconds", type=int, default=0)
    sub.add_parser("status")
    pipeline = sub.add_parser("pipeline")
    pipeline.add_argument("--if-enabled", action="store_true")
    pipeline.add_argument("--planner", choices=["codex", "deterministic"], default="codex")
    pipeline.add_argument("--source-limit", type=int)
    pipeline.add_argument("--application-limit", type=int)
    pipeline.add_argument("--concurrency", type=int)
    source = sub.add_parser("classify")
    source.add_argument("url")
    source.add_argument("--timeout", type=int, default=90)
    sub.add_parser("source-status")
    source_resume = sub.add_parser("source-resume")
    source_resume.add_argument("source_job_hash")
    sub.add_parser("questions")
    respond = sub.add_parser("answer-question")
    respond.add_argument("question_id")
    respond.add_argument("--decline", action="store_true")
    respond.add_argument("--promote", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("job_hash")
    review = sub.add_parser("review")
    review.add_argument("job_hash")
    review.add_argument("--role", choices=["sde", "ml"])
    review.add_argument("--planner", choices=["codex", "deterministic"], default="codex")
    probe = sub.add_parser("probe")
    probe.add_argument("url")
    probe.add_argument("--headless", action="store_true")
    demo = sub.add_parser("demo")
    demo.add_argument("--planner", choices=["codex", "deterministic"], default="deterministic")
    demo.add_argument("--headed", action="store_true")
    letter = sub.add_parser("cover-letter")
    letter.add_argument("--role", choices=["sde", "ml"], required=True)
    letter.add_argument("--replacements", type=Path, required=True)
    letter.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)
    config.load_dotenv()
    config.refresh_from_env()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    if args.command == "init-profile":
        book = booklet.bootstrap(args.source, args.booklet)
        print(f"Private booklet created: {args.booklet}; {len(booklet.missing(book))} shared answers need input")
        return 0
    if args.command == "missing":
        for key in booklet.missing(booklet.load(args.booklet)): print(key)
        return 0
    if args.command == "custom-answer":
        raise SystemExit("Unscoped custom answers are retired. Use questions and answer-question to retain employer context.")
    if args.command == "answer":
        value = input("Answer (JSON true/false for yes/no or checkboxes; otherwise text): ")
        if value in {"true", "false"}: value = value == "true"
        from .questions import _locked
        with _locked(args.booklet):
            booklet.set_answer(args.booklet, args.key, value, role=args.role, decline=args.decline)
        print("Private answer saved")
        return 0
    if args.command == "demo":
        from .demo import run_demo
        return run_demo(planner=args.planner, headless=not args.headed)
    if args.command == "probe":
        from .probe import probe_url
        print(asyncio.run(probe_url(args.url, headless=args.headless)))
        return 0
    if args.command == "classify":
        from .greenhouse_source import resolve_job
        print(json.dumps(asyncio.run(resolve_job(args.url, timeout=args.timeout)), ensure_ascii=False))
        return 0
    if args.command == "questions":
        from .questions import pending
        for question in pending(args.booklet):
            print(json.dumps(question, ensure_ascii=False))
        return 0
    if args.command == "cover-letter":
        from .cover_letter import compile_letter
        book = booklet.load(args.booklet)
        template = Path(book["roles"][args.role]["documents.cover_template"]["value"])
        result = compile_letter(template, json.loads(args.replacements.read_text()), args.output)
        print(f"One-page PDF written: {result}. Visually review before approving for upload.")
        return 0
    if args.command in {"worker", "pipeline"} and args.if_enabled and os.environ.get("JHB_APPLICATIONS_ENABLED") != "1":
        return 0
    conn = store.connect()
    queue.initialize(conn)
    try:
        if args.command == "status":
            for row in conn.execute("SELECT job_hash,state,attempts,packet FROM applications ORDER BY updated_at DESC"):
                print(json.dumps(dict(row)))
        elif args.command == "resume":
            queue.resume(conn, args.job_hash)
        elif args.command == "source-status":
            from . import source_queue
            source_queue.initialize(conn)
            for row in conn.execute("SELECT source_job_hash,state,board,application_url,attempts,evidence_path "
                                    "FROM application_sources ORDER BY updated_at DESC LIMIT 100"):
                print(json.dumps(dict(row)))
        elif args.command == "source-resume":
            from . import source_queue
            source_queue.initialize(conn)
            source_queue.resume(conn, args.source_job_hash)
        elif args.command == "answer-question":
            from .questions import answer
            value = None
            if not args.decline:
                raw = input("Answer (text, true/false, number, or JSON selections): ")
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
            affected = answer(args.question_id, value, args.booklet, conn,
                              promote=args.promote, decline=args.decline)
            print(f"Private answer saved; {len(affected)} application context(s) updated")
        elif args.command == "pipeline":
            from .pipeline import limits_from_env, run_cycle
            limits = limits_from_env()
            for name in ("source_limit", "application_limit", "concurrency"):
                if getattr(args, name) is not None:
                    limits[name] = getattr(args, name)
            summary = run_cycle(conn, args.booklet, planner_name=args.planner,
                                send_email=os.environ.get("JHB_APPLICATION_EMAIL") == "1", **limits)
            print(json.dumps(summary))
        elif args.command == "review":
            from .worker import run_job
            row = conn.execute("SELECT job_json,state,packet FROM applications WHERE job_hash=?", (args.job_hash,)).fetchone()
            if not row: raise ValueError("Application ID not found")
            if row["state"] == "submitted":
                print(f"Already submitted; saved review packet: {row['packet']}")
                return 0
            result, packet = asyncio.run(run_job(json.loads(row[0]), booklet.load(args.booklet),
                     planner_name=args.planner, interactive=True, role=args.role))
            print(f"{result['state']}: {packet}")
        elif args.command == "worker":
            from .worker import drain_once, notify_pending
            # Retry notification delivery independently of browser preparation.
            send_email = os.environ.get("JHB_APPLICATION_EMAIL") == "1"
            notify_pending(conn, send_email=send_email)
            result = drain_once(conn, args.booklet, planner_name=args.planner, headless=args.headless,
                                interactive=args.interactive, review_seconds=args.review_seconds)
            notify_pending(conn, send_email=send_email)
            print(result["state"] if result else "No queued applications")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
