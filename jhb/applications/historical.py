"""Read-only spreadsheet import and pre-browser duplicate protection.

Candidate-maintained history is evidence against reapplying, not a new employer
receipt. Incomplete legacy rows may hold a matching role for reconciliation;
they never become an exact ATS identity or acquire an invented submission date.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlunsplit

from .. import config
from . import boards, booklet, tracking

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheet_application_history (
 entry_key TEXT PRIMARY KEY, sink_key TEXT NOT NULL, row_number INTEGER NOT NULL,
 row_json TEXT NOT NULL, identity_json TEXT, url_key TEXT, company_key TEXT NOT NULL,
 snapshot_path TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, imported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sheet_history_identity ON sheet_application_history(identity_json);
CREATE INDEX IF NOT EXISTS sheet_history_url ON sheet_application_history(url_key);
CREATE INDEX IF NOT EXISTS sheet_history_company ON sheet_application_history(company_key);
CREATE TABLE IF NOT EXISTS sheet_history_imports (
 sink_key TEXT PRIMARY KEY, imported_at REAL NOT NULL, snapshot_path TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL, row_count INTEGER NOT NULL
);
"""
READ_TOOLS = {"GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_BATCH_GET"}
APPLIED_DATE = re.compile(r"(?:0?[1-9]|[12][0-9]|3[01])(?:st|nd|rd|th)?\s+"
                          r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(?:\s+[0-9]{4})?", re.I)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _words(value):
    return re.sub(r"[^\w+#]+", " ", str(value).casefold()).strip()


def _company(value):
    # This alias affects cautious legacy holds only, never exact confirmation.
    return _words(re.sub(r"\.(?:ai|com|io)\s*$", "", str(value), flags=re.I))


def _title(value, company):
    text = str(value).strip()
    text = re.sub(r"\s*\((?:remote|hybrid|on[ -]?site)\)\s*$", "", text, flags=re.I)
    parts = re.split(r"\s+[-–—]\s+", text)
    if len(parts) > 1 and _company(parts[-1]) == _company(company):
        text = " - ".join(parts[:-1])
    return _words(text)


def _locations(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value
        except ValueError:
            pass
    values = value if isinstance(value, list) else re.split(r"[;|]", str(value or ""))
    aliases = {"sf": "san francisco", "san francisco ca": "san francisco",
               "san francisco california": "san francisco", "nyc": "new york city"}
    # Remote alone describes work arrangement, not a conflicting country/city.
    unspecified = {"remote", "multiple locations", "remote multiple locations", "various locations"}
    return {aliases.get(_words(item), _words(item)) for item in values
            if _words(item) and _words(item) not in unspecified}


def _url_key(value):
    """Compare unresolved links cautiously, retaining all meaningful parameters."""
    parsed = boards._parts(value)
    if not parsed or parsed.fragment or not re.search(r"/(?:jobs?|careers?)/[^/]+", parsed.path, re.I):
        return None
    if re.search(r"/(?:search|login|signin|sign-in|jobs|careers)/?$", parsed.path, re.I):
        return None
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=40)
    except ValueError:
        return None
    tracking_keys = {"gh_src"}
    pairs = [(k, v) for k, v in pairs if not k.casefold().startswith("utm_") and k.casefold() not in tracking_keys]
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), urlencode(sorted(pairs)), ""))


def import_sheet(conn, *, config_path=None, executor=None, now=None, refresh_seconds=900):
    """GET the configured existing sheet; never append, submit or send email."""
    now = time.time() if now is None else now
    if executor is None and os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
        return {"state": "disabled", "imported": 0, "reason": "CI"}
    try:
        settings = tracking._load_config(config_path)
        if settings is None:
            return {"state": "disabled", "imported": 0}
        conn.executescript(SCHEMA)
        sink = hashlib.sha256(_json([settings["spreadsheet_id"], settings["sheet_name"]]).encode()).hexdigest()
        last = conn.execute("SELECT imported_at,snapshot_path,snapshot_sha256,row_count FROM sheet_history_imports WHERE sink_key=?", (sink,)).fetchone()
        if last and 0 <= now-last[0] < refresh_seconds:
            try:
                _, _, digest = tracking._private_json(last[1])
                if digest == last[2]:
                    return {"state": "cached", "imported": 0, "rows": last[3]}
            except (OSError, ValueError, TypeError):
                pass  # Refresh damaged cached evidence through an actual GET.
        lock = config.ROOT / "private" / "application-tracker.lock"
        if lock.is_symlink() or any(parent.is_symlink() for parent in lock.parents):
            raise ValueError("Unsafe tracker lock")
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"state": "busy", "imported": 0}
            execute = executor or tracking.ComposioSheets(settings.get("account"))
            def read(slug, payload):
                if slug not in READ_TOOLS:
                    raise ValueError("History import cannot write to Sheets")
                return execute(slug, payload)
            rows = tracking._read_sheet(read, settings)
            snapshot = {"schema_version": 1, "source": "configured_application_spreadsheet",
                        "spreadsheet_id": settings["spreadsheet_id"], "sheet_name": settings["sheet_name"],
                        "captured_at": now, "rows": {str(k): v for k, v in rows.items()}}
            if len(_json(snapshot).encode()) > 700_000:
                raise ValueError("History snapshot exceeds bounded private evidence size")
            path = config.ROOT / "private" / "tracker-history" / (hashlib.sha256(_json(snapshot).encode()).hexdigest()+".json")
            booklet.write_private(path, snapshot)
            _, _, digest = tracking._private_json(path)
            imported = exact = legacy = 0
            with conn:
                for number, values in rows.items():
                    if number == 1:
                        continue
                    row = list(values[:8])+[""]*max(0, 8-len(values))
                    # A planned role without an application date is not history.
                    if not all(isinstance(row[i], str) and row[i].strip() for i in (0, 1, 3)):
                        continue
                    if not APPLIED_DATE.fullmatch(row[3].strip()):
                        continue  # Never infer a year or evaluate date formulas.
                    url = tracking._link(row[7])
                    identity = boards.job_identity(url)
                    key = hashlib.sha256(_json([sink, number, row]).encode()).hexdigest()
                    imported += conn.execute("INSERT OR IGNORE INTO sheet_application_history VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (key, sink, number, _json(row), _json(identity) if identity else None,
                         _url_key(url), _company(row[0]), str(path), digest, now)).rowcount
                    conn.execute("UPDATE sheet_application_history SET snapshot_path=?,snapshot_sha256=?,imported_at=? WHERE entry_key=?",
                                 (str(path), digest, now, key))
                    exact += bool(identity)
                    legacy += not bool(identity)
                conn.execute("INSERT INTO sheet_history_imports VALUES(?,?,?,?,?) ON CONFLICT(sink_key) DO UPDATE SET "
                             "imported_at=excluded.imported_at,snapshot_path=excluded.snapshot_path,"
                             "snapshot_sha256=excluded.snapshot_sha256,row_count=excluded.row_count",
                             (sink, now, str(path), digest, exact+legacy))
            return {"state": "imported", "imported": imported, "rows": exact+legacy,
                    "exact_identity_rows": exact, "legacy_rows": legacy, "snapshot_path": str(path)}
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"state": "pending", "imported": 0, "reason": type(exc).__name__}


def match(conn, job):
    """Return a pre-browser exclusion/hold; never claim a confirmed submission."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_application_history'").fetchone():
        return None
    urls = [job.get(key) for key in ("url", "source_url", "application_url") if isinstance(job.get(key), str)]
    identities = {_json(identity) for url in urls if (identity := boards.job_identity(url))}
    ats_identities = {item for item in identities if json.loads(item)[0] != "linkedin"}
    url_keys = {key for url in urls if (key := _url_key(url))}
    company = _company(job.get("company", ""))
    clauses, args = ["company_key=?"], [company]
    for column, keys in (("identity_json", identities), ("url_key", url_keys)):
        if keys:
            clauses.append(column+" IN ("+",".join("?" for _ in keys)+")")
            args.extend(sorted(keys))
    records = conn.execute("SELECT row_number,row_json,identity_json,url_key,snapshot_path,snapshot_sha256 FROM "
                           "sheet_application_history WHERE "+" OR ".join(clauses)+" ORDER BY imported_at DESC", args).fetchall()
    held = None
    for number, encoded, identity, url_key, path, digest in records:
        exact = identity in identities if identity else False
        candidate = exact
        try:
            row = json.loads(encoded)
            linked = bool(url_key and url_key in url_keys)
            # Different exact ATS jobs stay distinct, even with identical titles.
            conflicting = identity and not exact and ats_identities and json.loads(identity)[0] != "linkedin"
            legacy = (not conflicting and company and _company(row[0]) == company
                      and _title(row[1], row[0]) == _title(job.get("title", ""), job.get("company", "")))
            old_locations = _locations(row[2])
            current_locations = _locations(job.get("locations") or job.get("location"))
            if legacy and old_locations and current_locations and old_locations.isdisjoint(current_locations):
                legacy = False
            if not (exact or linked or legacy):
                continue
            candidate = True
            _, snapshot, actual = tracking._private_json(path)
            original = snapshot.get("rows", {}).get(str(number), [])
            original = original[:8]+[""]*max(0, 8-len(original))
            if actual != digest or original != row or snapshot.get("source") != "configured_application_spreadsheet":
                raise ValueError("Saved history evidence changed")
            decision = {"state": "previously_applied" if exact else "possible_prior_application",
                        "disposition": "exclude" if exact else "hold", "submission_confirmed": False,
                        "match_kind": "exact_job_identity" if exact else "same_unresolved_url" if linked else "legacy_company_role_location",
                        "row_number": number, "company": row[0], "title": row[1],
                        "location_raw": row[2], "applied_date_raw": row[3],
                        "evidence_path": path, "evidence_sha256": digest,
                        "reason": "Existing application spreadsheet records this exact job" if exact else
                            "Existing application history may match; reconcile identity before preparing another application"}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            if not candidate:
                continue
            decision = {"state": "history_integrity_handoff", "disposition": "hold", "submission_confirmed": False,
                        "match_kind": "exact_identity_unverified_evidence" if exact else "legacy_unverified_evidence", "row_number": number,
                        "reason": "Saved matching history evidence requires reconciliation before reapplying"}
        if exact:
            return decision
        held = held or decision
    return held
