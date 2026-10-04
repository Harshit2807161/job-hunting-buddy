"""Durable candidate-email deduplication, coalescing, and bounded retries.

Delivery keys contain stable event identities, never answers or credentials.
All send attempts share a category lease so concurrent processes cannot mail
the same candidate event. An SMTP failure backs off the entire category, even
when another job discovers a new event during the next cron cycle.
"""
from __future__ import annotations

import time
import uuid

COALESCE_SECONDS = 300
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 6 * 3600
LEASE_SECONDS = 180


def initialize(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS application_notice_delivery ("
                 "notice_key TEXT PRIMARY KEY,category TEXT NOT NULL,delivered_at INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS application_notice_channels ("
                 "category TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,"
                 "next_attempt_at INTEGER NOT NULL DEFAULT 0,lease_until INTEGER,"
                 "lease_token TEXT,last_error TEXT)")
    conn.commit()


def deliver(conn, keys, category, callback, *, now=None):
    """Return successfully delivered keys; callback receives only unsent keys.

    An already delivered key is returned without sending, allowing callers to
    reconcile a crash after SMTP success but before updating their local record.
    SMTP offers no atomic receipt: an interrupted attempt is retried only after
    its persisted lease/backoff, never again within the next cron minute.
    """
    keys = set(keys)
    if not keys:
        return set()
    initialize(conn)
    now = int(time.time()) if now is None else int(now)
    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        for key in keys:
            conn.execute("INSERT OR IGNORE INTO application_notice_delivery(notice_key,category) VALUES(?,?)", (key, category))
        sent = {key for key in keys if conn.execute("SELECT delivered_at FROM application_notice_delivery WHERE notice_key=?", (key,)).fetchone()[0] is not None}
        unsent = keys - sent
        conn.execute("INSERT OR IGNORE INTO application_notice_channels(category) VALUES(?)", (category,))
        row = conn.execute("SELECT attempts,next_attempt_at,lease_until FROM application_notice_channels WHERE category=?", (category,)).fetchone()
        attempts, next_attempt, lease = row
        if not unsent or next_attempt > now or (lease is not None and lease > now):
            conn.commit()
            return sent
        attempts += 1
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 10))
        conn.execute("UPDATE application_notice_channels SET attempts=?,next_attempt_at=?,lease_until=?,lease_token=? WHERE category=?",
                     (attempts, now + delay, now + LEASE_SECONDS, token, category))
        conn.commit()  # Retry timing is durable before invoking SMTP.
    except BaseException:
        conn.rollback()
        raise
    error = None
    try:
        success = callback(unsent) is True
    except Exception as exc:
        success, error = False, type(exc).__name__
    with conn:
        # A stale sender cannot overwrite a newer process's delivery lease.
        current = conn.execute("SELECT lease_token FROM application_notice_channels WHERE category=?", (category,)).fetchone()
        if current[0] != token:
            return sent
        if success:
            conn.executemany("UPDATE application_notice_delivery SET delivered_at=? WHERE notice_key=?", ((now, key) for key in unsent))
            conn.execute("UPDATE application_notice_channels SET attempts=0,next_attempt_at=?,lease_until=NULL,lease_token=NULL,last_error=NULL WHERE category=?",
                         (now + COALESCE_SECONDS, category))
            sent.update(unsent)
        else:
            conn.execute("UPDATE application_notice_channels SET lease_until=NULL,lease_token=NULL,last_error=? WHERE category=?",
                         (error or "DeliveryFailed", category))
    return sent
