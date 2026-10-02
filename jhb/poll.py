"""Phase 1 entry point: poll -> diff -> filter -> ledger -> email.

Stages 01-03 of the RESEARCH.md pipeline, all owned by code. No model, no
browser, no automation surface -- and per the research, most of the available
edge, since interview odds are up to 8x higher inside four days.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import config, lock, notify, store
from .sources import jobspy_src, simplify

log = logging.getLogger("jhb")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("JobSpy").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def run_once(conn, *, use_jobspy: bool = True, dry_run: bool = False,
             force_seed: bool = False) -> dict:
    """One poll cycle across all sources. Returns a summary dict."""
    seeding = force_seed or store.is_first_run(conn)
    if seeding:
        log.info("SEED MODE -- recording current listings as already-seen (no email)")

    total_seen = total_new = 0
    for name, mod in (("simplify", simplify), ("jobspy", jobspy_src)):
        if name == "jobspy" and not use_jobspy:
            continue
        run_id = store.start_run(conn, name)
        try:
            jobs, status = mod.poll(conn)
            new = store.upsert_jobs(conn, jobs, mark_notified=seeding)
            total_seen += len(jobs)
            total_new += len(new)
            store.finish_run(conn, run_id, status, len(jobs), len(new))
            log.info("%-9s %-28s matched=%-5d new=%d", name, status, len(jobs), len(new))
        except Exception as e:
            store.finish_run(conn, run_id, "error", error=f"{type(e).__name__}: {e}")
            log.error("%-9s FAILED: %s: %s", name, type(e).__name__, e)

    pending, suppressed = store.pending_notification(conn)
    applications_queued = 0
    if os.environ.get("JHB_APPLICATIONS_ENABLED") == "1" and not seeding and not dry_run:
        from .applications import queue
        # Recover insertion→enqueue crashes from the ledger before notification collapsing.
        candidates = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE notified_at IS NULL")]
        applications_queued = queue.enqueue(conn, candidates)
        if applications_queued:
            log.info("queued %d Greenhouse application(s) for preparation", applications_queued)
    if suppressed:
        # Same role already emailed (other source, or another location row).
        store.mark_notified(conn, suppressed)
        log.info("collapsed %d duplicate row(s) of roles already covered", len(suppressed))
    emailed = 0

    if seeding:
        log.info("seed complete: %d listings recorded, 0 emails sent", total_seen)
    elif pending:
        if len(pending) > config.BURST_THRESHOLD:
            log.warning("burst guard: %d pending (> %d) -- sending ONE summary",
                        len(pending), config.BURST_THRESHOLD)
            shown = pending[:60]
            if len(pending) > len(shown):
                log.warning("email shows %d of %d; remainder stays pending for next cycle",
                            len(shown), len(pending))
            if notify.send(shown, subject_prefix=f"[{len(pending)} at once] ", dry_run=dry_run):
                if not dry_run:
                    store.mark_notified(conn, [j["dedupe_hash"] for j in shown])
                emailed = 1
        else:
            if notify.send(pending, dry_run=dry_run):
                if not dry_run:
                    store.mark_notified(conn, [j["dedupe_hash"] for j in pending])
                emailed = 1
            log.info("%s %d new opening(s)",
                     "would email" if dry_run else "emailed", len(pending))
    else:
        log.info("no new openings this cycle")

    return {"seen": total_seen, "new": total_new, "notified": len(pending) if not seeding else 0,
            "emails": emailed, "seeded": seeding, "applications_queued": applications_queued}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jhb.poll", description="Job opening poller")
    ap.add_argument("--watch", type=int, metavar="MIN",
                    help="loop every MIN minutes (default: one cycle, then exit)")
    ap.add_argument("--seed", action="store_true", help="force seed mode (record, do not email)")
    ap.add_argument("--dry-run", action="store_true", help="render email to file, do not send")
    ap.add_argument("--no-jobspy", action="store_true", help="Simplify only")
    ap.add_argument("--stats", action="store_true", help="print ledger stats and exit")
    ap.add_argument("--test-email", action="store_true", help="send one test email and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)
    config.load_dotenv()
    config.refresh_from_env()
    conn = store.connect()

    if args.stats:
        for k, v in store.stats(conn).items():
            print(f"  {k:12} {v}")
        recent = conn.execute(
            "SELECT source,started_at,status,rows_seen,rows_new FROM poll_runs "
            "ORDER BY id DESC LIMIT 8").fetchall()
        print("\n  recent runs:")
        for r in recent:
            print(f"    {time.strftime('%m-%d %H:%M', time.localtime(r['started_at']))} "
                  f"{r['source']:9} {str(r['status'])[:34]:34} seen={r['rows_seen']:<5} new={r['rows_new']}")
        return 0

    if args.test_email:
        sample = [{"title": "Software Engineer, New Grad", "company": "Example Corp",
                   "url": "https://job-boards.greenhouse.io/example/jobs/1234",
                   "locations": '["San Jose, CA"]', "role_classes": "swe",
                   "date_posted": int(time.time()), "source": "simplify"},
                  {"title": "Machine Learning Engineer", "company": "Example AI",
                   "url": "https://jobs.ashbyhq.com/example-ai/5678",
                   "locations": '["Remote in USA"]', "role_classes": "ml",
                   "date_posted": int(time.time()), "source": "jobspy:indeed"}]
        notify.send(sample, subject_prefix="[test] ", dry_run=args.dry_run)
        print(f"test email sent to {config.EMAIL_TO}" if not args.dry_run else "test preview written")
        return 0

    interval = args.watch
    while True:
        t0 = time.time()
        try:
            with lock.single_instance() as held:
                if not held:
                    log.warning("another poll cycle is already running; skipping")
                else:
                    run_once(conn, use_jobspy=not args.no_jobspy, dry_run=args.dry_run,
                             force_seed=args.seed)
        except KeyboardInterrupt:
            log.info("interrupted")
            return 130
        except Exception as e:
            log.exception("cycle failed: %s", e)
        if not interval:
            return 0
        sleep = max(30, interval * 60 - (time.time() - t0))
        log.info("sleeping %.0fs", sleep)
        try:
            time.sleep(sleep)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
