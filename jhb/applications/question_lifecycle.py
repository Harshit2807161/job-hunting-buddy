"""Read-only membership of a handoff in the application's current packet.

The ledger is history, not a work queue. Callers load the authoritative current
packet and supply durable history/discard blocking; no browser or writes occur.
"""
from __future__ import annotations

import json

from . import boards, booklet


def current_context(book, record, context, row, packet, *,
                    allowed_states=("waiting_input",), blocked=False):
    """Whether this exact unresolved question still belongs to current work.

    ``waiting_review`` is opt-in solely for explicit edits of a current optional
    blank. A saved review's old ledger entries are never new candidate blockers.
    Queued/running contexts may be projected as agent work, not candidate input.
    """
    if not all(isinstance(value, dict) for value in (book, record, context, packet)) or row is None:
        return False
    row = dict(row)
    key, state = row.get("job_hash"), row.get("state")
    if blocked or state not in allowed_states or context.get("resolved") or packet.get("submitted"):
        return False
    try:
        job = row.get("job") or json.loads(row.get("job_json") or "{}")
    except (ValueError, TypeError):
        return False
    pjob = packet.get("job", {})
    if not isinstance(job, dict) or not isinstance(pjob, dict):
        return False
    identity = boards.job_identity(job.get("application_url") or job.get("url"))
    if (not identity or job.get("dedupe_hash") != key
            or boards.application_hash(job.get("application_url") or job.get("url")) != key
            or context.get("job_hash") != key or boards.job_identity(context.get("url")) != identity
            or pjob.get("dedupe_hash") != key
            or boards.job_identity(pjob.get("application_url") or pjob.get("url")) != identity
            or booklet.job_excluded(book, {"dedupe_hash": key})):
        return False
    for manual in book.get("manual_application_records", {}).values():
        if (isinstance(manual, dict) and boards.job_identity(manual.get("url")) == identity
                and manual.get("state") in {"submitted", "submission_uncertain", "skipped", "declined", "discarded"}):
            return False
    # A running/queued repair may retain its preceding blocked packet. A
    # waiting-input row may not borrow an old review or login packet.
    expected_packet_states = {"waiting_input"} if state == "waiting_input" else {
        "waiting_input", "queued", "running", "retry", "failed"}
    review_edit = state == "waiting_review"
    if review_edit:
        expected_packet_states = {"waiting_review"}
    if packet.get("state") not in expected_packet_states:
        return False
    ref = context.get("ref")
    if not ref and record.get("kind") != "role":
        return False
    if ref and (any(isinstance(item, dict) and item.get("ref") == ref for item in packet.get("filled", []))
                or ref in packet.get("resolved_optional_refs", [])):
        return False
    inventory = packet.get("review_inventory", {})
    fields = inventory.get("fields", []) if isinstance(inventory, dict) else []
    if any(isinstance(item, dict) and item.get("ref") == ref and item.get("status") in {"answered", "declined"}
           for item in fields):
        return False
    pending = [(item, True) for item in packet.get("missing", [])]
    pending += [(item, False) for group in ("optional_questions", "unknown_questions") for item in packet.get(group, [])]
    if review_edit:
        pending += [(item, False) for item in fields if isinstance(item, dict)
                    and item.get("status") == "blank" and not item.get("required")]
    for item, default_required in pending:
        if not isinstance(item, dict):
            continue
        required = bool(item.get("required", default_required))
        if (item.get("ref") != ref
                or booklet.normalize(str(item.get("question") or "")) != booklet.normalize(str(record.get("question") or ""))
                or item.get("type", "text") != context.get("type", "text")
                or required != bool(context.get("required"))
                or review_edit and required):
            continue
        description = item.get("description") or ""
        if not isinstance(description, str):
            continue
        if (description[:4096] != (context.get("description") or "")
                or (item.get("description_truncated") is True or len(description) > 4096)
                != (context.get("description_truncated") is True)):
            continue
        if item.get("choices") and context.get("choices") and item["choices"] != context["choices"]:
            continue
        return True
    return False
