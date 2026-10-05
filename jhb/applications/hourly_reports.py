"""Hourly receipt-based candidate progress emails, independent of browser work."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config, notify, store
from . import booklet, notices, overnight


REPORT_SCOPE = "hourly progress reporting only"
REPORT_WINDOW = "progress-report-window.json"


def load_report_window(path=None, *, now=None):
    """Finite report consent has no application or submission privilege."""
    now = time.time() if now is None else now
    try:
        path = Path(path or config.ROOT / "private" / REPORT_WINDOW)
        if not path.is_absolute():
            path = config.ROOT / path
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            return None
        path = path.resolve(strict=True)
        if not path.is_relative_to((config.ROOT / "private").resolve()) or path.stat().st_size > 65536:
            return None
        raw = path.read_bytes()
        window = json.loads(raw)
        start, expiry = overnight._timestamp(window["authorized_at"]), overnight._timestamp(window["expires_at"])
        content = window.get("content", "")
        if (window.get("scope") != REPORT_SCOPE or window.get("role") != "user" or window.get("status") != "verified"
                or window.get("enabled") is not True or window.get("submission_authority") is not False
                or window.get("frequency_seconds") != 3600 or not window.get("source")
                or not isinstance(content, str) or not re.search(r"\b(?:hourly|every\s+hour)\b", content, re.I)
                or not re.search(r"\b(?:email(?:s|ing)?|reports?|updates?|progress)\b", content, re.I)
                or re.search(r"\b(?:do not|don't|never|stop|cancel|disable)\s+(?:send|sending|email|emailing|report|reporting|updates?)\b", content, re.I)
                or not start <= now < expiry or not 0 < expiry-start <= 86400):
            return None
        return {**window, "authorization_id": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def report(conn, auth=None, *, now=None, sender=None):
    if os.environ.get("JHB_HOURLY_PROGRESS_EMAIL") != "1":
        return {"state": "disabled"}
    now = int(time.time()) if now is None else int(now)
    auth = auth or load_report_window(now=now)
    if not auth:
        return {"state": "authorization_ended"}
    start = overnight._timestamp(auth["authorized_at"])
    expiry = overnight._timestamp(auth["expires_at"])
    if not start <= now < expiry:
        return {"state": "authorization_ended"}
    bucket = int((now-start)//3600)
    if bucket < 1:
        return {"state": "not_due"}
    conn.execute("CREATE TABLE IF NOT EXISTS application_hourly_reports (authorization_id TEXT NOT NULL,"
                 "bucket INTEGER NOT NULL,start_at INTEGER NOT NULL,end_at INTEGER NOT NULL,"
                 "summary_json TEXT NOT NULL,PRIMARY KEY(authorization_id,bucket))")
    notices.initialize(conn)
    key = f"hourly-progress:{auth['authorization_id']}:{bucket}"
    existing = conn.execute("SELECT summary_json FROM application_hourly_reports WHERE authorization_id=? AND bucket=?",
                            (auth["authorization_id"], bucket)).fetchone()
    if existing:
        summary = json.loads(existing[0])
    else:
        previous = conn.execute("SELECT MAX(r.end_at) FROM application_hourly_reports r JOIN application_notice_delivery d "
                                "ON d.notice_key=('hourly-progress:' || r.authorization_id || ':' || r.bucket) "
                                "WHERE r.authorization_id=? AND d.delivered_at IS NOT NULL", (auth["authorization_id"],)).fetchone()[0]
        lower = previous or start
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = list(conn.execute("SELECT submission_key,job_json,confirmed_at FROM confirmed_submissions")) if "confirmed_submissions" in tables else []
        confirmed = []
        total = 0
        for row in events:
            try:
                moment = overnight._timestamp(row[2])
            except (ValueError, TypeError):
                continue
            if start <= moment < now:
                total += 1
            if lower <= moment < now:
                confirmed.append({"submission_key": row[0], "job": json.loads(row[1])})
        delivered = 0
        if "submission_sheet_delivery" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(submission_sheet_delivery)")}
            if "state" in columns:
                delivered = sum(conn.execute("SELECT 1 FROM submission_sheet_delivery WHERE submission_key=? AND state='synced'",
                                             (event["submission_key"],)).fetchone() is not None for event in confirmed)
        app_states = dict(conn.execute("SELECT state,COUNT(*) FROM applications GROUP BY state").fetchall()) if "applications" in tables else {}
        summary = {"start_at": lower, "end_at": now, "confirmed_this_period": len(confirmed),
                   "confirmed_in_window": total, "spreadsheet_verified_this_period": delivered,
                   "applications": [event["job"] for event in confirmed], "application_states": app_states}
        conn.execute("INSERT OR IGNORE INTO application_hourly_reports VALUES(?,?,?,?,?)",
                     (auth["authorization_id"], bucket, int(lower), now, json.dumps(summary)))
        conn.commit()
    def send(keys):
        policy = "Applications follow the active review policy shown on the dashboard; independent checks remain required.\n"
        details = (f"Confirmed submissions since the previous update: {summary['confirmed_this_period']}\n"
                   f"Confirmed submissions in this run: {summary['confirmed_in_window']}\n"
                   f"Verified spreadsheet entries for this period: {summary['spreadsheet_verified_this_period']}\n"
                   f"Current application states: {json.dumps(summary['application_states'], sort_keys=True)}\n"
                   + policy +
                   "Only positive submission receipts are counted. Drafts and uncertain attempts are excluded.")
        when = datetime.fromtimestamp(summary['end_at'], ZoneInfo('America/Los_Angeles')).strftime('%b %d %H:%M %Z')
        return (sender or notify.send)(summary["applications"], details=details,
                    subject_override=f"Job hunting update ({when}): {summary['confirmed_this_period']} submitted; {summary['confirmed_in_window']} total this run")
    delivered = notices.deliver(conn, [key], "hourly_progress", send, now=now)
    booklet.write_private(config.ROOT / "private" / "hourly-reports" / f"{auth['authorization_id']}-{bucket}.json",
                          {**summary, "delivered": key in delivered})
    return {"state": "delivered" if key in delivered else "pending", "confirmed": summary["confirmed_this_period"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    config.load_dotenv(); config.refresh_from_env()
    while True:
        conn = store.connect()
        try:
            result = report(conn)
        finally:
            conn.close()
        print(json.dumps(result), flush=True)
        if not args.watch or result["state"] in {"disabled", "authorization_ended"}:
            break
        time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
