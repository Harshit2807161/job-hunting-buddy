"""SQLite ledger.

RESEARCH.md: "Dedupe via sha256(company + title + external_id-or-url) with a
UNIQUE constraint doing double duty as the idempotency guard." The PRIMARY KEY
on dedupe_hash is that guard -- a re-poll of unchanged data is a no-op, so the
poller is safe to run every 15 minutes forever.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    dedupe_hash  TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    locations    TEXT,
    role_classes TEXT,
    date_posted  INTEGER,
    first_seen   INTEGER NOT NULL,
    notified_at  INTEGER,
    role_key     TEXT,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_notified ON jobs(notified_at);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);

CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT,
    rows_seen   INTEGER DEFAULT 0,
    rows_new    INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@dataclass
class Job:
    source: str
    source_id: str
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    role_classes: list[str] = field(default_factory=list)
    date_posted: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def dedupe_hash(self) -> str:
        key = f"{self.company.strip().lower()}|{self.title.strip().lower()}|{self.source_id or self.url}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


def connect(path=None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in (("role_key", "TEXT"),):
        if col not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # Index only after the column is guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_role_key ON jobs(role_key)")
    conn.commit()

    # Backfill role_key for rows written before the column existed.
    missing = conn.execute(
        "SELECT dedupe_hash, company, title FROM jobs WHERE role_key IS NULL").fetchall()
    if missing:
        conn.executemany(
            "UPDATE jobs SET role_key=? WHERE dedupe_hash=?",
            [(role_key(r["company"], r["title"]), r["dedupe_hash"]) for r in missing])
        conn.commit()


_CO_NOISE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|group|technologies"
    r"|technology|labs|systems|holdings|the)\b", re.I)


def role_key(company: str, title: str) -> str:
    """Identity of a ROLE, independent of which source found it.

    The same opening reaches us twice -- once from Simplify carrying the
    employer's ATS link, once from JobSpy carrying a linkedin.com/jobs/view/...
    wrapper. Different source_ids meant different dedupe_hashes, so one job
    produced two rows and two emails. This key collapses them for notification
    while both rows stay in the ledger.
    """
    co = re.sub(r"[^a-z0-9]", "", _CO_NOISE.sub("", (company or "").lower()))
    ti = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return hashlib.sha256(f"{co}|{ti}".encode()).hexdigest()


def get_meta(conn, k: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, k: str, v: str) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    conn.commit()


def is_first_run(conn) -> bool:
    return conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def upsert_jobs(conn, jobs: list[Job], mark_notified: bool = False) -> list[Job]:
    """Insert jobs; return only those that were genuinely new.

    `mark_notified=True` is seed mode: record everything as already-seen so the
    first run does not email 1,175 historical listings.
    """
    now = int(time.time())
    new: list[Job] = []
    for j in jobs:
        cur = conn.execute(
            """INSERT INTO jobs(dedupe_hash, source, source_id, company, title, url,
                                locations, role_classes, date_posted, first_seen,
                                notified_at, raw, role_key)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedupe_hash) DO NOTHING""",
            (j.dedupe_hash, j.source, j.source_id, j.company, j.title, j.url,
             json.dumps(j.locations), ",".join(j.role_classes), j.date_posted, now,
             now if mark_notified else None, json.dumps(j.raw)[:20000],
             role_key(j.company, j.title)),
        )
        if cur.rowcount:
            new.append(j)
    conn.commit()
    return new


def _source_rank(row: dict) -> tuple:
    """Lower sorts better. Prefer a direct employer ATS link over a job-board
    wrapper, so the email links straight to the application form."""
    src = row.get("source") or ""
    url = (row.get("url") or "").lower()
    board = any(b in url for b in ("linkedin.com", "indeed.com", "ziprecruiter",
                                   "glassdoor.com", "google.com"))
    return (0 if src == "simplify" else 1, 1 if board else 0, -(row.get("date_posted") or 0))


def pending_notification(conn) -> tuple[list[dict], list[str]]:
    """Return (rows_to_email, hashes_to_suppress).

    Simplify lists one row per LOCATION -- RTX "Software Engineer 1" appeared 13
    times, one per city -- which read as the same opening arriving over and over.
    Those are collapsed into a single card carrying every location and link.

    The same opening arriving from BOTH sources is left alone on purpose: the
    Simplify row carries the employer's ATS link and the JobSpy row the LinkedIn
    one, and both are worth having. Grouping is therefore per (role_key, source).
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE notified_at IS NULL ORDER BY date_posted DESC")]
    if not rows:
        return [], []

    # A role already emailed from this same source must not come back when a new
    # location for it shows up later.
    already = {(r["role_key"], r["source"]) for r in conn.execute(
        "SELECT DISTINCT role_key, source FROM jobs WHERE notified_at IS NOT NULL")}

    keep: dict[tuple, dict] = {}
    groups: dict[tuple, list[dict]] = {}
    suppress: list[str] = []

    for r in rows:
        key = (r.get("role_key"), r.get("source"))
        if key in already:
            suppress.append(r["dedupe_hash"])
            continue
        groups.setdefault(key, []).append(r)
        best = keep.get(key)
        if best is None or _source_rank(r) < _source_rank(best):
            keep[key] = r

    for key, best in keep.items():
        members = groups[key]
        for r in members:
            if r["dedupe_hash"] != best["dedupe_hash"]:
                suppress.append(r["dedupe_hash"])

        # Fold every location in the group onto the surviving card.
        locs: list[str] = []
        for r in members:
            try:
                for l in json.loads(r.get("locations") or "[]"):
                    if l not in locs:
                        locs.append(l)
            except Exception:
                pass
        best["locations"] = json.dumps(locs)
        best["dupe_count"] = len(members)

    out = sorted(keep.values(), key=lambda r: -(r.get("date_posted") or 0))
    return out, suppress


def mark_notified(conn, hashes: list[str]) -> None:
    now = int(time.time())
    conn.executemany("UPDATE jobs SET notified_at=? WHERE dedupe_hash=?",
                     [(now, h) for h in hashes])
    conn.commit()


def start_run(conn, source: str) -> int:
    cur = conn.execute("INSERT INTO poll_runs(source, started_at) VALUES(?,?)",
                       (source, int(time.time())))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, rows_seen=0, rows_new=0, error=None) -> None:
    conn.execute(
        """UPDATE poll_runs SET finished_at=?, status=?, rows_seen=?, rows_new=?,
                                error=? WHERE id=?""",
        (int(time.time()), status, rows_seen, rows_new, error, run_id),
    )
    conn.commit()


def stats(conn) -> dict:
    g = lambda q: conn.execute(q).fetchone()[0]
    return {
        "total_jobs": g("SELECT COUNT(*) FROM jobs"),
        "notified": g("SELECT COUNT(*) FROM jobs WHERE notified_at IS NOT NULL"),
        "pending": g("SELECT COUNT(*) FROM jobs WHERE notified_at IS NULL"),
        "swe": g("SELECT COUNT(*) FROM jobs WHERE role_classes LIKE '%swe%'"),
        "ml": g("SELECT COUNT(*) FROM jobs WHERE role_classes LIKE '%ml%'"),
        "runs": g("SELECT COUNT(*) FROM poll_runs"),
    }
