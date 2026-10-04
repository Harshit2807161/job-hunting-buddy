"""Receipt-confirmed application history and a durable, private Sheets outbox.

This module never clicks Submit. Queue states and submit attempts are not proof.
An ambiguous append is reconciled by reads only, never blindly repeated.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import boards, queue

HEADERS = ["Company", "Role", "Location(s)", "Date applied", "Initial OA?",
           "Status last checked", "Verdict", "Link"]
CONFIG_NAME = "application-tracker.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmed_submissions (
 submission_key TEXT PRIMARY KEY, ats TEXT NOT NULL, application_url TEXT NOT NULL,
 job_json TEXT NOT NULL, confirmed_at TEXT NOT NULL, proof_json TEXT NOT NULL,
 recorded_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_sheet_delivery (
 submission_key TEXT NOT NULL, sink_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, updated_range TEXT, match_basis TEXT,
 last_error TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY(submission_key,sink_key)
);
"""


def initialize(conn):
    conn.executescript(SCHEMA)


def ats_identity(url):
    """Exact supported job identity, including posting/application URL variants."""
    identity = boards.job_identity(url)
    return (identity, boards.canonical_url(url)) if identity else None


def _private_json(path):
    path = Path(path)
    if not path.is_absolute():
        path = config.ROOT / path
    root = config.ROOT / "private"
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Tracking files must not be symlinks")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file() or resolved.stat().st_size > 1024*1024:
        raise ValueError("Tracking requires an existing private JSON file")
    resolved.chmod(0o600)
    content = resolved.read_bytes()
    return resolved, json.loads(content), hashlib.sha256(content).hexdigest()


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Submission confirmation needs an observed timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Submission timestamp must include its timezone")
    return parsed.astimezone(timezone.utc)


def _proof(job, receipt_path):
    path, receipt, digest = _private_json(receipt_path)
    identity = ats_identity(job.get("url"))
    if not identity or receipt.get("state") != "submitted" or ats_identity(receipt.get("url")) is None:
        raise ValueError("Only receipt-confirmed submissions may enter the tracker")
    if ats_identity(receipt["url"])[0] != identity[0]:
        raise ValueError("Submission receipt belongs to another ATS job")
    confirmed_at = _timestamp(receipt.get("confirmed_at")).isoformat()
    confirmation, body, source = (receipt.get(key, "") for key in ("confirmation", "body", "source"))
    if not all(isinstance(value, str) and value.strip() for value in (confirmation, body, source)):
        raise ValueError("Submission confirmation needs captured evidence and provenance")
    if confirmation not in body:
        raise ValueError("Captured evidence does not contain the reported confirmation")
    if source == "explicit_user_confirmation":
        evidence_path, evidence, evidence_digest = _private_json(receipt.get("user_evidence_path", ""))
        statement = evidence.get("content", "")
        positive = r"(?:done[,\s]+)?(?:submitted[.!]?|i (?:have |already )?(?:submitted|applied)(?:\b.*)|(?:it|the application) (?:was|is|has been) submitted(?:\b.*))"
        if (evidence.get("role") != "user" or not isinstance(statement, str)
                or statement not in body or "?" in statement or not re.fullmatch(positive, statement.strip(), re.I)
                or re.search(r"\b(?:not|never|haven't|didn't|failed)\b", statement, re.I)):
            raise ValueError("User-confirmed submission needs an actual affirmative user statement")
        kind = "explicit_user_confirmation"
        extra = {"user_evidence_path": str(evidence_path), "user_evidence_sha256": evidence_digest}
    else:
        positive = r"(?:your application (?:was successfully submitted|has been submitted successfully|has been received)|we(?: have|'ve)? received your application|thank you for applying)"
        if (not source.startswith("Live ") or "success page" not in source.lower()
                or not isinstance(receipt.get("target_id"), str) or not receipt["target_id"]
                or not re.search(positive, confirmation, re.I)):
            raise ValueError("A submit attempt or login screen is not a successful receipt")
        kind, extra = "live_success_page", {}
    return identity, confirmed_at, {"kind": kind, "receipt_path": str(path), "receipt_sha256": digest,
                                   "confirmation": confirmation, "source": source, **extra}


def record_confirmed(conn, job, receipt_path, *, config_path=None, executor=None):
    identity, confirmed_at, proof = _proof(job, receipt_path)
    if not all(isinstance(job.get(key), str) and job[key].strip() for key in ("company", "title")):
        raise ValueError("Confirmed submission needs the verified company and role")
    initialize(conn)
    queue.initialize(conn)
    key = hashlib.sha256(json.dumps(identity[0], separators=(",", ":")).encode()).hexdigest()
    row_job = {k: job.get(k) for k in ("company", "title", "locations", "location", "dedupe_hash")}
    row_job["url"] = identity[1]
    with conn:
        conn.execute("INSERT OR IGNORE INTO confirmed_submissions VALUES(?,?,?,?,?,?,?)",
                     (key, identity[0][0], identity[1], json.dumps(row_job), confirmed_at, json.dumps(proof), int(time.time())))
        application_hash = boards.application_hash(job["url"])
        if application_hash:
            terminal_job = {**row_job, "dedupe_hash": application_hash}
            conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'submitted',?,?) "
                         "ON CONFLICT(job_hash) DO UPDATE SET state='submitted',lease_until=NULL,updated_at=excluded.updated_at,packet=excluded.packet,notified_at=NULL",
                         (application_hash, json.dumps(terminal_job), int(time.time()), proof["receipt_path"]))
    # Confirmation is durable even when connection/auth/Sheets delivery fails.
    try:
        tracking = sync_pending(conn, config_path=config_path, executor=executor)
    except Exception as exc:
        tracking = {"state": "pending", "reason": type(exc).__name__}
    return {"submission_key": key, "state": "submitted", "tracking": tracking}


def confirmed_application(conn, url):
    """Revalidate prior proof; an exact but damaged confirmation blocks replay."""
    identity = ats_identity(url)
    if identity is None or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='confirmed_submissions'").fetchone():
        return None
    key = hashlib.sha256(json.dumps(identity[0], separators=(",", ":")).encode()).hexdigest()
    rows = conn.execute("SELECT * FROM confirmed_submissions WHERE submission_key=? OR application_url=?",
                        (key, identity[1])).fetchall()
    unverified = None
    for row in rows:
        try:
            stored = ats_identity(row["application_url"])
            if stored is None or stored[0] != identity[0]:
                continue
            unverified = {"job": {"url": identity[1], "dedupe_hash": boards.application_hash(url),
                                  "historical_confirmation": {"state": "unverified", "kind": "saved_receipt_integrity"}},
                          "receipt_path": None, "verified": False}
            job, saved = json.loads(row["job_json"]), json.loads(row["proof_json"])
            checked, confirmed_at, proof = _proof(job, saved["receipt_path"])
            if (checked[0] != identity[0] or ats_identity(row["application_url"])[0] != identity[0]
                    or confirmed_at != row["confirmed_at"] or proof["receipt_sha256"] != saved.get("receipt_sha256")):
                continue
            return {"job": {**job, "url": checked[1], "dedupe_hash": boards.application_hash(url)},
                    "receipt_path": proof["receipt_path"], "verified": True}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return unverified


def _restore_confirmation(conn, confirmation):
    job = confirmation["job"]
    state = "submitted" if confirmation["verified"] else "submission_uncertain"
    return conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(job_hash) DO UPDATE SET state=excluded.state,lease_until=NULL,updated_at=excluded.updated_at,"
                        "packet=excluded.packet WHERE applications.state<>'submitted' AND (excluded.state='submitted' OR applications.state<>'submission_uncertain')",
                        (job["dedupe_hash"], json.dumps(job), state, int(time.time()), confirmation["receipt_path"])).rowcount


def restore_confirmed_applications(conn, *, limit=1000):
    """Migrate historical confirmations into the queue without Sheets or browser IO.

    Exact enqueue/claim checks also protect jobs beyond this bounded startup scan.
    Missing or changed private receipt evidence never fabricates a submission.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='confirmed_submissions'").fetchone():
        return 0
    restored = 0
    rows = conn.execute("SELECT application_url FROM confirmed_submissions ORDER BY recorded_at DESC LIMIT ?", (limit,)).fetchall()
    for row in rows:
        confirmation = confirmed_application(conn, row["application_url"])
        if confirmation:
            restored += _restore_confirmation(conn, confirmation)
    conn.commit()
    return restored


def _load_config(path=None):
    selected = path or os.environ.get("JHB_TRACKER_CONFIG") or config.ROOT / "private" / CONFIG_NAME
    if not Path(selected).is_absolute():
        selected = config.ROOT / selected
    if not Path(selected).exists():
        return None
    _, settings, _ = _private_json(selected)
    if settings.get("enabled") is not True:
        return None
    if (settings.get("headers") != HEADERS or settings.get("date_style") != "ordinal_day_short_month"
            or not isinstance(settings.get("sheet_name"), str) or not settings["sheet_name"]
            or not re.fullmatch(r"[A-Za-z0-9_-]+", settings.get("spreadsheet_id", ""))):
        raise ValueError("Tracker configuration must match the observed existing sheet")
    ZoneInfo(settings["timezone"])
    return settings


class ComposioSheets:
    """Published CLI, fixed tool slugs, JSON stdin; no credentials in CI/argv."""
    def __init__(self, account=None, executable="composio", timeout=45):
        self.account, self.executable, self.timeout = account, executable, timeout

    def __call__(self, slug, payload):
        if slug not in {"GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_BATCH_GET", "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND"}:
            raise ValueError("Unsupported tracker tool")
        command = [self.executable, "execute", slug, "-d", "-"]
        if self.account:
            command += ["--account", self.account]
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, timeout=self.timeout)
        if result.returncode:
            raise RuntimeError("Composio Sheets request failed")
        response = json.loads(result.stdout)
        if (response.get("successful") is True and response.get("error") is None
                and isinstance(response.get("data"), dict) and "results" not in response):
            return response["data"]
        results = response.get("results", [])
        if (response.get("successful") is not True or len(results) != 1 or results[0].get("slug") != slug
                or results[0].get("successful") is not True or results[0].get("error") is not None
                or not isinstance(results[0].get("data"), dict)):
            raise RuntimeError("Composio Sheets response was not successful")
        return results[0]["data"]


def _column(number):
    text = ""
    while number:
        number, remainder = divmod(number-1, 26)
        text = chr(65+remainder)+text
    return text


def _tab(name):
    return "'"+name.replace("'", "''")+"'"


def _read_sheet(execute, settings):
    metadata = execute("GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": settings["spreadsheet_id"],
        "fields": "properties.timeZone,sheets.properties(title,gridProperties)"})
    if metadata.get("spreadsheetId", settings["spreadsheet_id"]) != settings["spreadsheet_id"]:
        raise ValueError("Tracker metadata returned another spreadsheet")
    sheets = [s["properties"] for s in metadata.get("sheets", []) if s.get("properties", {}).get("title") == settings["sheet_name"]]
    if len(sheets) != 1 or metadata.get("properties", {}).get("timeZone") != settings["timezone"]:
        raise ValueError("Tracker tab or timezone differs from its verified configuration")
    grid = sheets[0].get("gridProperties", {})
    count, columns = grid.get("rowCount"), grid.get("columnCount")
    if not isinstance(count, int) or count < 1 or not isinstance(columns, int) or columns < len(HEADERS):
        raise ValueError("Tracker grid metadata is invalid")
    rows = {}
    for start in range(1, count+1, 10000):
        result = execute("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": settings["spreadsheet_id"],
            "ranges": [f"{_tab(settings['sheet_name'])}!A{start}:{_column(columns)}{min(count,start+9999)}"],
            "majorDimension": "ROWS", "valueRenderOption": "FORMULA"})
        ranges = result.get("valueRanges", [])
        if len(ranges) != 1 or not isinstance(ranges[0].get("values", []), list):
            raise ValueError("Tracker read did not return the requested rows")
        returned = _parse_range(ranges[0].get("range", ""))
        if returned != (settings["sheet_name"], "A", start, _column(columns), min(count,start+9999)):
            raise ValueError("Tracker read did not cover the complete requested grid")
        for offset, values in enumerate(ranges[0].get("values", [])):
            if values:
                rows[start+offset] = values
    if rows.get(1, [])[:len(HEADERS)] != HEADERS:
        raise ValueError("Tracker headers changed; no application rows were written")
    return rows


def _date(value, zone):
    local = _timestamp(value).astimezone(ZoneInfo(zone))
    suffix = "th" if 10 < local.day % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(local.day % 10, "th")
    month = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")[local.month-1]
    return f"{local.day}{suffix} {month}"


def _sheet_row(event, settings):
    job = json.loads(event["job_json"])
    locations = job.get("locations") or job.get("location") or ""
    if isinstance(locations, str):
        try:
            parsed = json.loads(locations)
            if isinstance(parsed, list):
                locations = parsed
        except ValueError:
            pass
    if isinstance(locations, list):
        locations = "; ".join(str(value) for value in locations)
    return [job["company"], job["title"], str(locations), _date(event["confirmed_at"], settings["timezone"]),
            "", "", "", event["application_url"]]


def _normal(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _link(value):
    text = str(value).strip()
    formula = re.match(r'=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*[,;]', text, re.I)
    return formula[1].replace('""', '"') if formula else text


def _existing(rows, wanted):
    key = ats_identity(wanted[7])[0]
    for number, row in rows.items():
        if number == 1:
            continue
        current = list(row)+[""]*max(0, len(HEADERS)-len(row))
        identity = ats_identity(_link(current[7]))
        if identity and identity[0] == key:
            return number, "ats_identity"
        # Older candidate rows contain source-only LinkedIn posting links,
        # which never meant a receipt-confirmed ATS identity.
        legacy = not identity or identity[0][0] == "linkedin"
        if legacy and all(_normal(current[i]) == _normal(wanted[i]) for i in (0, 1, 3)):
            return number, "legacy_company_role_date"
    return None


def _parse_range(value):
    match = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)(\d+):([A-Z]+)(\d+)", value or "")
    if not match:
        return None
    return (match[1].replace("''", "'") if match[1] else match[2], match[3], int(match[4]), match[5], int(match[6]))


def _aligned_range(value, sheet):
    parsed = _parse_range(value)
    if not parsed or parsed[0] != sheet or parsed[1] != "A" or parsed[3] != "H" or parsed[2] != parsed[4] or parsed[2] < 2:
        raise ValueError("Append placement was not the expected complete tracker row")
    return parsed[2]


def sync_pending(conn, *, config_path=None, executor=None):
    initialize(conn)
    try:
        settings = _load_config(config_path)
    except Exception as exc:
        return {"state": "configuration_error", "reason": type(exc).__name__}
    if not settings:
        return {"state": "disabled", "synced": 0}
    sink = hashlib.sha256(json.dumps([settings["spreadsheet_id"], settings["sheet_name"]]).encode()).hexdigest()
    path = config.ROOT / "private" / "application-tracker.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        return {"state": "failed", "reason": "UnsafeTrackerLock"}
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return {"state": "busy", "synced": 0}
    try:
        execute = executor or ComposioSheets(settings.get("account"))
        summary = {"state": "complete", "synced": 0, "uncertain": 0, "failed": 0}
        for event in conn.execute("SELECT * FROM confirmed_submissions ORDER BY recorded_at").fetchall():
            key = event["submission_key"]
            conn.execute("INSERT OR IGNORE INTO submission_sheet_delivery(submission_key,sink_key,updated_at) VALUES(?,?,?)", (key, sink, int(time.time())))
            conn.commit()
            delivery = conn.execute("SELECT * FROM submission_sheet_delivery WHERE submission_key=? AND sink_key=?", (key, sink)).fetchone()
            if delivery["state"] == "synced":
                continue
            wrote = delivery["state"] in {"writing", "uncertain"}
            try:
                proof = json.loads(event["proof_json"])
                _, _, digest = _private_json(proof["receipt_path"])
                if digest != proof["receipt_sha256"]:
                    raise ValueError("Submission proof changed after confirmation")
                if proof.get("kind") == "explicit_user_confirmation":
                    _, _, user_digest = _private_json(proof["user_evidence_path"])
                    if user_digest != proof["user_evidence_sha256"]:
                        raise ValueError("User confirmation changed after recording")
                wanted = _sheet_row(event, settings)
                rows = _read_sheet(execute, settings)
                found = _existing(rows, wanted)
                if found:
                    number, basis = found
                elif wrote:
                    summary["uncertain"] += 1
                    continue  # Read-only reconciliation, never repeat ambiguous append.
                else:
                    conn.execute("UPDATE submission_sheet_delivery SET state='writing',attempts=attempts+1,updated_at=? WHERE submission_key=? AND sink_key=?", (int(time.time()), key, sink))
                    conn.commit()
                    wrote = True
                    response = execute("GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND", {
                        "spreadsheetId": settings["spreadsheet_id"], "range": f"{_tab(settings['sheet_name'])}!A:H",
                        "majorDimension": "ROWS", "valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS",
                        "includeValuesInResponse": True, "values": [wanted]})
                    number = _aligned_range(response.get("updates", {}).get("updatedRange"), settings["sheet_name"])
                    rows = _read_sheet(execute, settings)
                    actual = list(rows.get(number, []))[:len(HEADERS)]
                    actual += [""]*(len(HEADERS)-len(actual))
                    if actual != wanted:
                        raise ValueError("Appended tracker row was not verified by readback")
                    basis = "appended_verified"
                conn.execute("UPDATE submission_sheet_delivery SET state='synced',updated_range=?,match_basis=?,last_error=NULL,updated_at=? WHERE submission_key=? AND sink_key=?",
                             (f"{_tab(settings['sheet_name'])}!A{number}:H{number}", basis, int(time.time()), key, sink))
                conn.commit()
                summary["synced"] += 1
            except Exception as exc:
                state = "uncertain" if wrote else "pending"
                conn.execute("UPDATE submission_sheet_delivery SET state=?,last_error=?,updated_at=? WHERE submission_key=? AND sink_key=?",
                             (state, type(exc).__name__, int(time.time()), key, sink))
                conn.commit()
                summary["uncertain" if wrote else "failed"] += 1
        if summary["uncertain"] or summary["failed"]:
            summary["state"] = "pending"
        return summary
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
