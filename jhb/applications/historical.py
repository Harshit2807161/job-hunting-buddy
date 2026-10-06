"""Read-only spreadsheet import and pre-browser duplicate protection.

Candidate-maintained history is evidence against reapplying, not a new employer
receipt. Incomplete legacy rows may hold a matching role for reconciliation;
they never become an exact ATS identity or acquire an invented submission date.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
HOLD_SCHEMA = """CREATE TABLE IF NOT EXISTS audited_history_holds (
 hold_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, entry_key TEXT NOT NULL,
 evidence_json TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'active', resolved_at REAL, resolution_reason TEXT
)"""
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
    text = _words(text)
    levels = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
    text = re.sub(r"\b(engineer|developer|scientist|analyst|researcher)\s+(i|ii|iii|iv|v)\b",
                  lambda match: match[1]+" "+levels[match[2]], text)
    # Role word order varies between feeds and hand-entered rows. Retain every
    # token (including level, specialty and cohort), rather than widening to a
    # company-wide or generic software-engineer match.
    return " ".join(sorted(text.split()))


def _locations(value):
    """Conservative (country, region, city) descriptions, not geocoding.

    Empty components remain unknown. A broad or ambiguous location cannot
    prove that an otherwise matching historical application is a different job.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value
        except ValueError:
            pass
    countries = {alias: canonical for canonical, aliases in {
        "us": ("us", "u s", "usa", "u s a", "united states", "united states of america"),
        "gb": ("uk", "u k", "gb", "united kingdom", "great britain"),
        "ca": ("canada",), "in": ("india",), "de": ("germany",), "fr": ("france",),
        "au": ("australia",), "ie": ("ireland",), "sg": ("singapore",),
    }.items() for alias in aliases}
    regions = dict(pair.split(":") for pair in (
        "al:alabama|ak:alaska|az:arizona|ar:arkansas|ca:california|co:colorado|ct:connecticut|de:delaware|"
        "dc:district of columbia|fl:florida|ga:georgia|hi:hawaii|id:idaho|il:illinois|in:indiana|ia:iowa|"
        "ks:kansas|ky:kentucky|la:louisiana|me:maine|md:maryland|ma:massachusetts|mi:michigan|mn:minnesota|"
        "ms:mississippi|mo:missouri|mt:montana|ne:nebraska|nv:nevada|nh:new hampshire|nj:new jersey|"
        "nm:new mexico|ny:new york|nc:north carolina|nd:north dakota|oh:ohio|ok:oklahoma|or:oregon|"
        "pa:pennsylvania|ri:rhode island|sc:south carolina|sd:south dakota|tn:tennessee|tx:texas|ut:utah|"
        "vt:vermont|va:virginia|wa:washington|wv:west virginia|wi:wisconsin|wy:wyoming").split("|"))
    regions.update({name: code for code, name in tuple(regions.items())})
    # Normalize both abbreviations and full names to the same region code.
    regions.update({code: code for code in tuple(regions) if len(code) == 2})
    cities = {"sf": "san francisco", "san francisco ca": "san francisco",
              "san francisco california": "san francisco", "nyc": "new york city", "new york": "new york city"}
    result = set()
    for item in value if isinstance(value, list) else [value]:
        for part in re.split(r"[;|/\n]", str(item or "")):
            part = re.sub(r"\b(?:remote|hybrid|on[- ]?site|multiple(?:\s+locations)?|various\s+locations|anywhere|worldwide|global)\b", "", part, flags=re.I)
            parts = [_words(p) for p in part.split(",") if _words(p)]
            if not parts:
                continue
            if all(p in countries for p in parts):
                result.update((countries[p], "", "") for p in parts)
                continue
            country = countries.get(parts[-1], "")
            if country:
                parts.pop()
            region = (regions.get(parts[-1], "") if parts and country in {"", "us"}
                      and (country or len(parts) > 1 or len(parts[-1]) > 2) else "")
            if region:
                country = "us"; parts.pop()
            # Unrecognized comma lists might name several cities. They cannot
            # safely supply evidence of a conflicting single locality.
            city = cities.get(parts[0], parts[0]) if len(parts) == 1 else ""
            if city in regions and len(city) == 2:
                city = ""  # A standalone CA, for example, has ambiguous scope.
            result.add((country, region, city))
    return result


def _locations_conflict(saved, current):
    left, right = _locations(saved), _locations(current)
    return bool(left and right and all(
        any(a and b and a != b for a, b in zip(old, new))
        for old in left for new in right))


def _url_key(value, *, allow_http=False):
    """Compare unresolved links cautiously, retaining all meaningful parameters."""
    parsed = boards._parts(value)
    scheme = "https"
    # Audited history keys never navigate. Validate HTTP source syntax through
    # the existing HTTPS parser without broadening any live-browser URL policy.
    if (parsed is None and allow_http and isinstance(value, str)
            and not any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        try:
            original = urlsplit(value)
            if (original.scheme == "http" and not original.username and not original.password
                    and original.port in {None, 80}):
                parsed = boards._parts(urlunsplit(("https", original.netloc.removesuffix(":80"),
                    original.path, original.query, original.fragment)))
                scheme = "http"
        except ValueError:
            pass
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
    return urlunsplit((scheme, parsed.hostname, parsed.path.rstrip("/"), urlencode(sorted(pairs)), ""))


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


def _snapshot(path, *, root=None):
    """Read evidence under the selected root without modifying its permissions."""
    root = Path(root) if root is not None else config.ROOT
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Unsafe history evidence")
    path = path.resolve(strict=True)
    if not path.is_relative_to((root / "private").resolve()) or path.stat().st_size > 1024*1024:
        raise ValueError("History evidence must remain private")
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict):
        raise ValueError("History snapshot must be an object")
    return snapshot, hashlib.sha256(raw).hexdigest()


def _hold_scope(url):
    identity = boards.job_identity(url)
    if identity:
        return "identity:"+_json(identity)
    key = _url_key(url, allow_http=True)
    return "source:"+key if key else None


def _hold_reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1200:
        raise ValueError("A bounded explicit audit reason is required")
    return value.strip()


def _hold_id(scope, entry_key, source_hash):
    return hashlib.sha256(_json([scope, entry_key, source_hash if scope.startswith("source:") else None]).encode()).hexdigest()


def _imported_evidence(conn, entry_key, *, root=None):
    if not re.fullmatch(r"[a-f0-9]{64}", str(entry_key)):
        raise ValueError("An exact imported history entry key is required")
    row = conn.execute("SELECT row_number,row_json,snapshot_path,snapshot_sha256,sink_key "
                       "FROM sheet_application_history WHERE entry_key=?", (entry_key,)).fetchone()
    if row is None:
        raise ValueError("The audited spreadsheet row was not imported")
    number, encoded, path, digest, sink = row
    values = json.loads(encoded)
    snapshot, actual = _snapshot(path, root=root)
    original = snapshot.get("rows", {}).get(str(number), [])
    original = original[:8]+[""]*max(0, 8-len(original))
    if (actual != digest or original != values or snapshot.get("source") != "configured_application_spreadsheet"
            or hashlib.sha256(_json([sink, number, values]).encode()).hexdigest() != entry_key):
        raise ValueError("Imported history evidence failed integrity verification")
    return {"entry_key": entry_key, "row_number": number, "row": values,
            "snapshot_path": path, "snapshot_sha256": digest}


def record_hold(conn, job, *, entry_key, reason, company_alias_reason=None, root=None):
    """Record one explicit audit, never fuzzy matching or submission authority.

    ATS aliases bind their canonical identity, independent of a discovery-row
    dedupe hash. Unrecognized sources need both an exact job URL and source hash.
    """
    reason = _hold_reason(reason)
    url = job.get("application_url") or job.get("url") or job.get("canonical_url")
    scope = _hold_scope(url)
    source_hash = job.get("dedupe_hash")
    if not scope or not re.fullmatch(r"[a-f0-9]{64}", str(source_hash)):
        raise ValueError("Audited holds require an exact job URL and hash")
    evidence = _imported_evidence(conn, entry_key, root=root)
    identity, old_identity = boards.job_identity(url), boards.job_identity(tracking._link(evidence["row"][7]))
    if identity and old_identity and identity[0] != "linkedin" and old_identity[0] != "linkedin" and identity != old_identity:
        raise ValueError("Distinct explicit ATS identities cannot be an ambiguous history hold")
    company = _company(job.get("company", ""))
    if not company:
        raise ValueError("The audited employer must be identified")
    if company != _company(evidence["row"][0]):
        company_alias_reason = _hold_reason(company_alias_reason)
    elif company_alias_reason is not None:
        company_alias_reason = _hold_reason(company_alias_reason)
    evidence.update(scope_key=scope, job_url=url, source_job_hash=source_hash,
                    canonical_job_hash=boards.application_hash(url), company_key=company,
                    company_alias_reason=company_alias_reason)
    hold_id = _hold_id(scope, entry_key, source_hash)
    with conn:
        conn.execute(HOLD_SCHEMA)
        previous = conn.execute("SELECT state FROM audited_history_holds WHERE hold_id=?", (hold_id,)).fetchone()
        if previous and previous[0] != "active":
            raise ValueError("An explicitly resolved audit must not be silently reopened")
        conn.execute("INSERT OR IGNORE INTO audited_history_holds "
                     "(hold_id,scope_key,entry_key,evidence_json,reason,created_at) VALUES(?,?,?,?,?,?)",
                     (hold_id, scope, entry_key, _json(evidence), reason, time.time()))
    return {"hold_id": hold_id, "state": "active", "scope_key": scope, "entry_key": entry_key,
            "submission_confirmed": False}


def release_hold(conn, hold_id, *, reason):
    """Explicitly resolve this audit only; other independent history still applies."""
    reason = _hold_reason(reason)
    with conn:
        changed = conn.execute("UPDATE audited_history_holds SET state='released',resolved_at=?,resolution_reason=? "
                               "WHERE hold_id=? AND state='active'", (time.time(), reason, hold_id)).rowcount
    if not changed:
        raise ValueError("No active audited hold matches this resolution")
    return {"hold_id": hold_id, "state": "released", "submission_confirmed": False}


def _audited_hold(conn, job, *, root=None):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='audited_history_holds'").fetchone():
        return None
    scopes = {_hold_scope(job.get(key)) for key in ("url", "source_url", "application_url", "canonical_url") if job.get(key)} - {None}
    if not scopes:
        return None
    records = conn.execute("SELECT hold_id,scope_key,entry_key,evidence_json,reason FROM audited_history_holds "
                           "WHERE state='active' AND scope_key IN ("+",".join("?" for _ in scopes)+") ORDER BY created_at",
                           tuple(sorted(scopes))).fetchall()
    for hold_id, scope, entry_key, encoded, reason in records:
        try:
            evidence = json.loads(encoded)
            if hold_id != _hold_id(scope, entry_key, evidence["source_job_hash"]):
                raise ValueError("Audited history binding changed")
            if scope.startswith("source:") and evidence["source_job_hash"] not in {
                    job.get("dedupe_hash"), job.get("source_job_hash")}:
                continue  # Unknown boards require the audited source row too.
            current = _imported_evidence(conn, entry_key, root=root)
            snapshot, digest = _snapshot(evidence["snapshot_path"], root=root)
            original = snapshot.get("rows", {}).get(str(evidence["row_number"]), [])
            original = original[:8]+[""]*max(0, 8-len(original))
            if (evidence["scope_key"] != scope or _hold_scope(evidence["job_url"]) != scope
                    or current["row"] != evidence["row"] or current["row_number"] != evidence["row_number"]
                    or digest != evidence["snapshot_sha256"]
                    or original != evidence["row"]):
                raise ValueError("Audited history evidence changed")
            return {"state": "possible_prior_application", "disposition": "hold", "submission_confirmed": False,
                    "match_kind": "audited_history_hold", "hold_id": hold_id, "row_number": evidence["row_number"],
                    "company": evidence["row"][0], "title": evidence["row"][1], "location_raw": evidence["row"][2],
                    "applied_date_raw": evidence["row"][3], "evidence_path": evidence["snapshot_path"],
                    "evidence_sha256": digest, "reason": reason}
        except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.DatabaseError):
            return {"state": "history_integrity_handoff", "disposition": "hold", "submission_confirmed": False,
                    "match_kind": "audited_history_unverified_evidence", "hold_id": hold_id,
                    "reason": "An audited history hold requires explicit evidence reconciliation"}
    return None


def match(conn, job, *, root=None):
    """Return a pre-browser exclusion/hold; never claim a confirmed submission."""
    urls = [job.get(key) for key in ("url", "source_url", "application_url") if isinstance(job.get(key), str)]
    # Receipt logging is durable before the final Sheet append. That interval
    # must not let a confirmed exact job return through Phase 1 as a new lead.
    for url in urls:
        confirmation = tracking.confirmed_application(conn, url)
        if confirmation:
            verified = confirmation["verified"]
            return {"state": "previously_applied" if verified else "history_integrity_handoff",
                    "disposition": "exclude" if verified else "hold", "submission_confirmed": False,
                    "existing_receipt_verified": verified, "match_kind": "existing_submission_record",
                    "evidence_path": confirmation.get("receipt_path"),
                    "reason": "An existing exact-job submission record prevents reapplication"}
    if held := _audited_hold(conn, job, root=root):
        return held
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_application_history'").fetchone():
        return None
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
            if legacy and _locations_conflict(row[2], job.get("locations") or job.get("location")):
                legacy = False
            if not (exact or linked or legacy):
                continue
            candidate = True
            snapshot, actual = _snapshot(path, root=root)
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


def cached_ready(conn, *, config_path=None):
    """Prove a prior complete read exists when a refresh temporarily fails."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_history_imports'").fetchone():
        return False
    try:
        settings = tracking._load_config(config_path)
        if settings is None:
            return False
        sink = hashlib.sha256(_json([settings["spreadsheet_id"], settings["sheet_name"]]).encode()).hexdigest()
    except (OSError, ValueError, TypeError):
        return False
    for path, digest in conn.execute("SELECT snapshot_path,snapshot_sha256 FROM sheet_history_imports WHERE sink_key=?", (sink,)):
        try:
            snapshot, current = _snapshot(path)
            if current == digest and snapshot.get("source") == "configured_application_spreadsheet":
                return True
        except (OSError, ValueError, TypeError):
            pass
    return False


def cached_match(job, *, root=None):
    """Read-only guard for direct preparation and submission entry points."""
    root = Path(root) if root is not None else config.ROOT
    path = root / "data" / "jobs.sqlite3"
    if not path.exists():
        return None
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("History database must not be a symlink")
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        return match(conn, job, root=root)


async def refresh_sheet(conn, **kwargs):
    """Keep the pipeline heartbeat responsive during bounded Composio GETs."""
    database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not database:
        return import_sheet(conn, **kwargs)  # Injected in-memory fixture executors.
    def refresh():
        with sqlite3.connect(Path(database).resolve().as_uri()+"?mode=rw", uri=True, timeout=10) as other:
            return import_sheet(other, **kwargs)
    return await asyncio.to_thread(refresh)


def refresh_before_submit(job, *, connection=None, executor=None):
    """Re-read manual applications immediately before the terminal browser call.

    Preparation may use the short cache, but a person can add a spreadsheet row
    while a draft is being filled or independently reviewed. A configured sheet
    that cannot be refreshed defers submission; it is never treated as empty.
    """
    owned = None
    try:
        if tracking._load_config() is None:
            return {"state": "disabled"}
        if executor is None and os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            return {"state": "pending", "reason": "CI"}
        if connection is None:
            path = config.ROOT / "data" / "jobs.sqlite3"
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError("Unsafe application history database")
            owned = sqlite3.connect(path.resolve().as_uri()+"?mode=rw", uri=True, timeout=10)
            owned.row_factory = sqlite3.Row
            connection = owned
        result = import_sheet(connection, executor=executor, refresh_seconds=0)
        if result.get("state") != "imported":
            return {"state": "pending", "reason": result.get("state", "invalid_refresh")}
        previous = match(connection, job)
        return {"state": "blocked", "match": previous} if previous else {"state": "clear"}
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
        return {"state": "pending", "reason": "history_refresh_failed"}
    finally:
        if owned is not None:
            owned.close()
