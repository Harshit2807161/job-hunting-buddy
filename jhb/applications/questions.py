"""Durable private question handoffs shared by application workers.

Answers are explicit user input. A question is employer scoped unless a user
deliberately promotes an ordinary profile fact to a shared booklet answer.
Authentication challenges belong to the browser handoff, never this ledger.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone

from . import booklet
from .queue import greenhouse_identity

_EDUCATION = re.compile(r"(?:school|degree|discipline|start_date|end_date|start-year|end-year)--\d+")
_SECRET = re.compile(
    r"\bpassword\b|passphrase|captcha|verification[_ ]*(?:code|token)|one[- ]time|two[- ]factor|"
    r"(?:security|authentication|recovery|backup|login|sign[- ]in)\s*(?:code|token)|"
    r"(?:api|access|secret)[_ -]?(?:key|token)|\botp\b", re.I)
_SAFE_SHARED = ("identity.", "links.", "preferences.application_city")


class QuestionChanged(ValueError):
    """A dashboard answer was based on a superseded question snapshot."""


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _locked(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Private destination must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lock = path.with_suffix(path.suffix + ".questions.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _scope(job):
    url = job.get("application_url") or job.get("url", "")
    identity = greenhouse_identity(url)
    if identity is not None:
        # Keep existing question IDs and explicitly approved answers stable.
        return {"region": identity[0], "board": identity[1]}
    from .boards import job_identity
    identity = job_identity(url)
    if identity is None:
        raise ValueError("Question handoffs require an exact resolved application job URL")
    if identity[0] == "linkedin":
        # LinkedIn's URL identifies a posting, not a stable employer tenant.
        return {"ats": "linkedin", "region": "global", "board": identity[-1]}
    return {"ats": identity[0], "region": "global", "board": "|".join(identity[1:-1])}


def _job_hash(job):
    value = job.get("dedupe_hash") or job.get("job_hash")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid job identity")
    return value


def _identity(scope, question, ref, country, kind, job_hash=None):
    # Most refs change across jobs. Repeated education rows are an exception:
    # asking for School twice must preserve which candidate record is missing.
    import json
    stable = {"scope": scope, "question": booklet.normalize(question),
              "education_ref": ref if _EDUCATION.fullmatch(ref or "") else None,
              "country_context": country, "kind": kind,
              "role_job": job_hash if kind == "role" else None}
    return "q_" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]


def _answer_revision(record):
    # Existing ledgers predate the dedicated revision field.
    return record.get("answer_revision") or next((event["at"] for event in reversed(record.get("history", []))
                                                   if event.get("event") == "answered"), None)


def collect(job: dict, result: dict, bookpath=booklet.DEFAULT_PATH, *, observed_book=None) -> list[dict]:
    """Record unknown required/meaningful optional questions and return pending ones.

    Result items carry question, answer_key, ref, required, country_context,
    choices and optionally kind='role'. Optional questions are supplied in
    ``optional_questions`` explicitly; blank optional controls are not guessed.
    ``missing`` defaults to required. Returning an existing pending record does
    not create another notification: its ID remains stable for UI/notification
    deduplication. Answers incompatible with a later form reopen for correction.
    """
    scope, job_hash = _scope(job), _job_hash(job)
    items = [(item, True) for item in result.get("missing", [])]
    items += [(item, False) for item in result.get("optional_questions", [])]
    items += [(item, False) for item in result.get("unknown_questions", [])]
    with _locked(bookpath):
        book = booklet.load(bookpath)
        ledger = book.setdefault("question_handoffs", {})
        touched = []
        for item, default_required in items:
            text = item.get("question", "")
            if not isinstance(text, str) or not text.strip():
                continue
            description = item.get("description", "")
            description = description if isinstance(description, str) else ""
            truncated = item.get("description_truncated") is True or len(description) > 4096
            description = description[:4096]
            key, ref = item.get("answer_key"), item.get("ref")
            if _SECRET.search(" ".join(str(part or "") for part in (text, key, ref))):
                continue
            text = text.strip()
            kind = item.get("kind", "role" if text == "Choose the SDE or ML resume variant" else "field")
            if kind not in {"field", "role"}:
                raise ValueError("Unsupported question kind")
            country = item.get("country_context")
            if country is not None:
                country = booklet.normalize(str(country))
            qid = _identity(scope, text, ref, country, kind, job_hash)
            stamp = _now()
            record = ledger.get(qid)
            required = bool(item.get("required", default_required))
            if record is None:
                record = {"id": qid, "question": text, "normalized_question": booklet.normalize(text),
                          "scope": scope, "country_context": country, "kind": kind,
                          "field_ref": ref if _EDUCATION.fullmatch(ref or "") else None,
                          "answer_key": key, "status": "pending", "created_at": stamp,
                          "updated_at": stamp, "contexts": {}, "history": []}
                ledger[qid] = record
            elif record["status"] in {"answered", "resolved"}:
                stored = book.get("custom_answers", {}).get(record.get("custom_answer_key"))
                earlier = (observed_book or {}).get("question_handoffs", {}).get(qid, {})
                newer_answer = observed_book is not None and record["status"] == "answered" and (
                    earlier.get("status") != "answered" or _answer_revision(earlier) != _answer_revision(record))
                incompatible = bool(item.get("reason")) and key is not None and (
                    key == record.get("custom_answer_key") or key == record.get("answer_key"))
                old_context = record.get("contexts", {}).get(job_hash, {})
                changed_description = (old_context.get("description", "") != description or
                                       (old_context.get("description_truncated") is True) != truncated)
                public_changed = False
                if stored and isinstance(stored.get("source"), dict) and "public_question_metadata_proofs" in stored["source"]:
                    from .question_metadata import public_response_allowed, public_response_context_matches
                    # A failed catalog inspection is technical, not a candidate
                    # correction. Native enrichment reports that failure before
                    # this path; a nonempty observed catalog can prove a change.
                    fresh_field = {"ref": ref, "label": text, "type": item.get("type", "text"),
                        "required": required, "country_context": country, "description": description,
                        "description_truncated": truncated, "choices": item.get("choices", [])}
                    public_changed = not public_response_context_matches(fresh_field, stored)
                    if item.get("choices"):
                        public_changed |= not public_response_allowed(fresh_field, stored)
                reopen = not newer_answer and (public_changed or changed_description or record["status"] == "resolved" or incompatible or (
                    stored and stored.get("status") == "declined" and required))
                if reopen and (public_changed or changed_description or not stored or stored["status"] != "declined" or required):
                    record["history"].append({"event": "reopened", "at": stamp,
                                              "reason": item.get("reason", "Approved answer did not fill this form")})
                    record["status"] = "pending"
                    record.pop("notified_at", None)
                    if stored:
                        stored["status"] = "needs_input"
                # Preserve newer explicit input while still attaching this job's
                # context, so its older run can queue a fresh pass.
            context = {"job_hash": job_hash, "url": job.get("application_url") or job["url"],
                       "source_url": job.get("source_url") or job.get("original_url") or job["url"],
                       "company": job.get("company", ""), "title": job.get("title", ""),
                       "required": required or record["contexts"].get(job_hash, {}).get("required", False),
                       "ref": ref, "answer_key": key, "reason": item.get("reason", ""),
                       "type": item.get("type", "text"), "choices": item.get("choices", []),
                       "description": description, "description_truncated": truncated}
            if result.get("selected_role") in {"sde", "ml"}:
                context["selected_role"] = result["selected_role"]
            from .question_routing import route
            context["routing"] = route(book, record, context, job=job)
            if record["contexts"].get(job_hash) != context:
                record["contexts"][job_hash] = context
                record["updated_at"] = stamp
            if qid not in touched:
                touched.append(qid)
        booklet.write_private(Path(bookpath), book)
        return [ledger[qid] for qid in touched if ledger[qid]["status"] == "pending"]


def reconcile(job, result, bookpath=booklet.DEFAULT_PATH):
    """Retire technical handoffs after retained-value verification fixes a field."""
    job_hash = _job_hash(job)
    filled_refs = {item.get("ref") for item in result.get("filled", []) if item.get("ref")}
    declined_refs = set(result.get("resolved_optional_refs", []))
    current = {item.get("ref"): booklet.normalize(item["question"])
               for group in ("missing", "optional_questions", "unknown_questions")
               for item in result.get(group, []) if item.get("ref") and item.get("question")}
    with _locked(bookpath):
        book = booklet.load(bookpath)
        for record in book.get("question_handoffs", {}).values():
            context = record["contexts"].get(job_hash)
            if not context or not context.get("ref") or (record["status"] != "pending" and
                    not (record["status"] == "answered" and context.get("routing") in {"known_answer_fill", "document_generation"})):
                continue
            ref = context["ref"]
            corrected_label = ref in current and current[ref] != record["normalized_question"]
            if ref in filled_refs or corrected_label or ref in declined_refs and not context.get("required"):
                context["resolved"] = True
                context["resolved_at"] = _now()
                context["resolution"] = ("Verified field filled" if ref in filled_refs else
                                         "Optional field explicitly declined or inapplicable" if ref in declined_refs else
                                         "Corrected field label has its own question")
                if record["status"] == "pending" and all(item.get("resolved") for item in record["contexts"].values()):
                    record["status"] = "resolved"
                    record["history"].append({"event": "resolved", "at": _now(), "reason": context["resolution"]})
        booklet.write_private(Path(bookpath), book)


def current_work(connection):
    """Load current private packets once for candidate inbox and recovery alike."""
    import json
    from .. import config
    from . import application_discard, historical
    from .authorized_submission import private_file
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone():
        return {}
    result = {}
    for row in connection.execute("SELECT job_hash,job_json,packet,state FROM applications WHERE state='waiting_input'"):
        try:
            job = json.loads(row["job_json"])
            if not row["packet"] or application_discard.discarded(config.ROOT, row["job_hash"]):
                continue
            if historical.match(connection, job):
                continue
            path = Path(row["packet"])
            if not path.is_absolute():
                path = config.ROOT / path
            path = private_file(path.with_name("packet.json"))
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            packet = json.loads(path.read_bytes())
            if not isinstance(packet, dict):
                continue
            result[row["job_hash"]] = {"row": dict(row), "job": job, "packet": packet}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result


def live_contexts(book, record, work):
    from .question_lifecycle import current_context
    return {key: context for key, context in record.get("contexts", {}).items()
            if key in work and current_context(book, record, context, work[key]["row"], work[key]["packet"])}


def pending(bookpath=booklet.DEFAULT_PATH, *, unnotified=False, connection=None) -> list[dict]:
    from .question_routing import candidate_contexts
    book = booklet.load(bookpath)
    if connection is not None:
        from .question_routing import CANDIDATE, route
        work = current_work(connection)
        result = []
        for record in book.get("question_handoffs", {}).values():
            if record.get("status") != "pending" or unnotified and record.get("notified_at"):
                continue
            contexts = {key: context for key, context in live_contexts(book, record, work).items()
                        if route(book, record, context, job=work[key]["job"]) == CANDIDATE}
            if contexts:
                result.append({**record, "contexts": contexts})
        return sorted(result, key=lambda q: (q["created_at"], q["id"]))
    return sorted(({**q, "contexts": contexts} for q in book.get("question_handoffs", {}).values()
                   if q["status"] == "pending" and (not unnotified or not q.get("notified_at"))
                   and (contexts := candidate_contexts(book, q))),
                  key=lambda q: (q["created_at"], q["id"]))


def mark_notified(question_ids: list[str], bookpath=booklet.DEFAULT_PATH, *, revisions=None):
    """Mark a successfully delivered aggregate handoff, never before delivery."""
    with _locked(bookpath):
        book = booklet.load(bookpath)
        ledger = book.get("question_handoffs", {})
        if any(qid not in ledger for qid in question_ids):
            raise ValueError("Unknown question ID")
        for qid in question_ids:
            if revisions is not None and sum(item.get("event") == "reopened" for item in ledger[qid]["history"]) != revisions[qid]:
                continue  # The candidate corrected/reopened this question during SMTP delivery.
            ledger[qid]["notified_at"] = _now()
        booklet.write_private(Path(bookpath), book)


def role_override(job_hash: str, bookpath=booklet.DEFAULT_PATH):
    """An explicit per-job choice takes precedence over title heuristics."""
    selection = booklet.load(bookpath).get("job_role_answers", {}).get(job_hash, {})
    if selection.get("status") == "verified" and selection.get("value") in {"sde", "ml"}:
        return selection["value"]
    return None


def notify_new(connection, bookpath=booklet.DEFAULT_PATH, *, send_email=False):
    """Deliver each new employer question once to the configured candidate inbox."""
    import json
    from .. import config, notify
    records = pending(bookpath, unnotified=True, connection=connection)
    if not records:
        return 0
    payload = {"questions": records, "submitted": False}
    booklet.write_private(config.ROOT / "private" / "notifications" / "new-questions.json", payload)
    if not send_email:
        return 0
    # Optional choices stay visible in the local booklet/outbox. Routine email
    # asks only genuinely new required candidate questions.
    records = [record for record in records if any(context["required"] for context in record["contexts"].values())]
    if not records:
        return 0
    hashes = {job_hash for record in records for job_hash in record["contexts"]}
    jobs = {}
    for job_hash in sorted(hashes):
        row = connection.execute("SELECT job_json FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
        if row:
            jobs[job_hash] = json.loads(row[0])
    if not jobs:
        return 0
    from .notices import deliver
    keyed = {f"question:{record['id']}:{sum(item.get('event') == 'reopened' for item in record['history'])}": record
             for record in records}
    def send(keys):
        selected = [keyed[key] for key in sorted(keys)]
        contexts = {job_hash for record in selected for job_hash in record["contexts"]}
        selected_jobs = [jobs[job_hash] for job_hash in sorted(contexts) if job_hash in jobs]
        lines = ["New application questions need your explicit answers. No application was submitted."]
        for record in selected:
            lines += ["", f"Required: {record['question']}", f"Question ID: {record['id']}",
                      f"Employer scope: {record['scope']['board']}"]
            choices = sorted({str(choice) for context in record["contexts"].values() for choice in context.get("choices", [])})
            if choices:
                lines.append("Observed choices: " + "; ".join(choices))
            lines.append(f"Answer locally: jhb-apply answer-question {record['id']}")
        lines.append("You can also reply with these question IDs and answers in the ongoing Codex conversation.")
        return notify.send(selected_jobs, subject_prefix="[application questions] ", details="\n".join(lines))
    sent = deliver(connection, keyed, "required_questions", send)
    mark_notified([keyed[key]["id"] for key in sent], bookpath,
                  revisions={keyed[key]["id"]: int(key.rsplit(":", 1)[1]) for key in sent})
    return len(sent)


def _validate_value(value):
    if value is None or isinstance(value, str) and not value.strip():
        raise ValueError("An explicit nonempty answer is required")
    if isinstance(value, list):
        if not value or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError("Selection answers require nonempty strings")
    elif not isinstance(value, (str, bool, int, float)):
        raise ValueError("Answer must be text, boolean, number, or selections")
    if isinstance(value, float):
        import math
        if not math.isfinite(value):
            raise ValueError("Answer number must be finite")


def answer(question_id: str, value, bookpath=booklet.DEFAULT_PATH, connection=None,
           *, promote=False, decline=False, expected_revision=None, before_save=None, after_save=None, context_job_hashes=None) -> list[str]:
    """Persist a user's answer and optionally resume unblocked waiting_input jobs.

    ``promote=True`` requires deliberate user choice and an ordinary known
    profile key. Consents, screening, eligibility and disclosure stay employer
    scoped. Browser credentials/challenge codes cannot enter the ledger.
    Repeated education answers bind to their field ref, never every School row.
    Returned IDs include all affected jobs; automatic queue resumption requires
    all required questions for that job to have answers and skips reviewed or
    submitted applications.
    """
    if not decline:
        _validate_value(value)
    with _locked(bookpath):
        book = booklet.load(bookpath)
        record = book.get("question_handoffs", {}).get(question_id)
        if record is None:
            raise ValueError("Unknown question ID")
        if expected_revision is not None and (record.get("status") != "pending" or
                                              record.get("updated_at") != expected_revision):
            raise QuestionChanged("Question changed; refresh before answering")
        if _SECRET.search(record["question"]):
            raise ValueError("Credentials and verification codes are browser handoffs")
        if decline and (record["kind"] != "field" or promote or
                        any(context["required"] for context in record["contexts"].values())):
            raise ValueError("Only optional field questions can be declined")
        if record["kind"] == "role" and (not isinstance(value, str) or value not in {"sde", "ml"}):
            raise ValueError("Choose sde or ml")
        if context_job_hashes is not None:
            from .question_routing import candidate_contexts
            allowed = candidate_contexts(book, record)
            if not allowed or any(key not in allowed for key in context_job_hashes):
                raise QuestionChanged("This field is agent work; refresh before answering")
        key = record.get("answer_key")
        if promote and (record["kind"] != "field" or key not in book["answers"]
                        or not any(key.startswith(prefix) for prefix in _SAFE_SHARED)
                        or record.get("field_ref") or
                        record["normalized_question"] not in booklet.ALIASES.get(key, [])):
            raise ValueError("This question cannot become a shared profile default")
        # Validate and serialize candidate edits against CLI/worker ledger writers
        # before revoking approvals or changing durable queue state.
        stamp = _now()
        if before_save is not None:
            before_save(record, stamp)
        source = {"provider": "explicit user question response", "question_id": question_id,
                  "scope": record["scope"], "answered_at": stamp,
                  "contexts": list(record["contexts"]) if context_job_hashes is None else list(context_job_hashes)}
        proofs = []
        for job_hash, context in record["contexts"].items():
            if context.get("resolved") or (context_job_hashes is not None and job_hash not in context_job_hashes):
                continue
            description = context.get("description", "")
            if isinstance(description, str) and description:
                proof = {"owned_description_sha256": hashlib.sha256(description.encode()).hexdigest(),
                         "owned_description_truncated": context.get("description_truncated") is True,
                         "field_ref": context.get("ref") if isinstance(context.get("ref"), str) else "",
                         "country_context": record.get("country_context") or ""}
                if proof not in proofs:
                    proofs.append(proof)
        from .question_metadata import description_digest
        public_proofs = []
        for job_hash, context in record["contexts"].items():
            if context.get("resolved") or (context_job_hashes is not None and job_hash not in context_job_hashes):
                continue
            metadata = context.get("public_question_metadata")
            if not isinstance(metadata, dict) or metadata.get("source") != "official_public_question_metadata":
                continue
            proof = {"source": "official_public_question_metadata", "field_ref": context.get("ref"),
                     "country_context": record.get("country_context") or "",
                     "description_sha256": description_digest(metadata.get("description", "")),
                     "choices_sha256": hashlib.sha256(json.dumps(context.get("choices", [])[:100], ensure_ascii=False).encode()).hexdigest(),
                     "required": context.get("required") is True, "observed_type": context.get("type"),
                     "metadata_sha256": metadata.get("sha256")}
            if proof not in public_proofs:
                public_proofs.append(proof)
        if public_proofs:
            source["public_question_metadata_proofs"] = public_proofs
        if proofs:
            source["owned_description_proofs"] = proofs
            if len(proofs) == 1:
                source.update(proofs[0])
        item = {**booklet.answer(None if decline else value, source,
                                "declined" if decline else "verified"), "user_override": True}
        if record["kind"] == "role":
            # Root's queue/worker uses this explicit selection when resuming.
            selections = book.setdefault("job_role_answers", {})
            for job_hash in record["contexts"]:
                old = selections.get(job_hash)
                if old:
                    book.setdefault("answer_history", {}).setdefault("job_role." + job_hash, []).append(old)
                selections[job_hash] = item
        elif promote:
            old = book["answers"].get(key)
            if old:
                book.setdefault("answer_history", {}).setdefault(key, []).append(old)
            book["answers"][key] = item
        else:
            custom_key = "custom." + question_id[2:]
            old = book.setdefault("custom_answers", {}).get(custom_key)
            if old:
                book.setdefault("answer_history", {}).setdefault(custom_key, []).append(old)
            book["custom_answers"][custom_key] = {**item, "question": record["question"],
                                                  "scope": record["scope"]}
            if context_job_hashes:
                book["custom_answers"][custom_key]["job_hashes"] = sorted(context_job_hashes)
            if record.get("field_ref"):
                book["custom_answers"][custom_key]["field_ref"] = record["field_ref"]
            if record.get("country_context"):
                book["custom_answers"][custom_key]["country_context"] = record["country_context"]
            record["custom_answer_key"] = custom_key
        record["status"], record["updated_at"] = "answered", stamp
        record["answer_revision"] = stamp
        record["history"].append({"event": "answered", "at": stamp, "promoted": promote})
        affected = sorted(record["contexts"] if context_job_hashes is None or not context_job_hashes else context_job_hashes)
        from .question_routing import candidate_contexts
        blocked = {job_hash for q in book["question_handoffs"].values() if q["status"] == "pending"
                   for job_hash, context in candidate_contexts(book, q).items() if context["required"]}
        booklet.write_private(Path(bookpath), book)
    if after_save is not None:
        after_save(affected)
    if connection is not None:
        import time
        # Restrict recovery to question handoffs. queue.resume is broader and
        # intentionally cannot be used blindly for a CAPTCHA/login failure.
        for job_hash in affected:
            if job_hash not in blocked:
                connection.execute("UPDATE applications SET state='queued',lease_until=NULL,attempts=0,"
                                   "updated_at=?,notified_at=NULL WHERE job_hash=? AND state='waiting_input'",
                                   (int(time.time()), job_hash))
        connection.commit()
    return affected
