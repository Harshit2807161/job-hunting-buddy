"""Private, immutable observations about attempts; never candidate facts or policy.

Call once after the final packet is persisted, including handoffs and failures.
The caller owns the attempt token: reuse it for a replay of the same observation,
and use a new token when actual preparation resumes. No browser actions occur here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .. import config
from . import boards, booklet

SCHEMA_VERSION = 1
STAGES = {"classification", "preparation", "review", "submission", "tracking"}
OUTCOMES = {"waiting_review", "waiting_input", "waiting_login", "waiting_captcha",
            "failed", "retry", "skipped", "unsupported", "submitted", "submission_uncertain",
            "complete", "filtered", "in_progress", "unknown"}
TECHNICAL_KINDS = {
    "TimeoutError", "TimeoutExpired", "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError", "FileNotFoundError", "ImportError",
    "ModuleNotFoundError", "SyntaxError", "IndentationError", "NameError", "TypeError",
    "AttributeError", "browser_transport", "browser_mechanics", "browser_capture",
    "planner_transport", "document_generation", "narrative_generation",
}
OPERATIONS = {"open", "observe", "fill", "describe", "upload", "screenshot", "planner",
              "classification", "runtime"}
RECOMMENDATIONS = {"preserve_terminal_evidence", "candidate_input", "verification_handoff",
                   "resume_known_tasks", "bounded_technical_repair", "wait_for_capacity",
                   "review_adapter", "review_complete_packet", "inspect_incomplete_inventory",
                   "no_action", "inspect_unclassified_failure"}


def directory():
    return config.ROOT / "private" / "attempt-feedback"


def private_path(value):
    path = Path(value)
    if not path.is_absolute():
        path = config.ROOT / path
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Unsafe feedback evidence path")
    path = path.resolve()
    if not path.is_relative_to((config.ROOT / "private").resolve()):
        raise ValueError("Feedback evidence must remain private")
    return path


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _enum(value, choices, fallback):
    return value if isinstance(value, str) and value in choices else fallback


def _flag(value):
    return value if type(value) is bool else None


def _recommendation(record):
    if record["terminal_started"] is not False or record["submitted"]:
        return "preserve_terminal_evidence"
    if record["verification_required"]:
        return "verification_handoff"
    if record["required_unknown_count"]:
        return "candidate_input"
    if record["outcome"] == "unsupported":
        return "review_adapter"
    if record["error_kind"] == "browser_capacity":
        return "wait_for_capacity"
    if (record["stage"] == "preparation" and record["outcome"] in {"failed", "retry"}
            and record["retryable"] and record["error_kind"] in TECHNICAL_KINDS):
        return "bounded_technical_repair"
    if record["known_field_task_count"]:
        return "resume_known_tasks"
    if record["outcome"] == "waiting_review":
        return ("review_complete_packet" if record["inventory_complete"]
                and record["screenshot_status"] == "verified" else "inspect_incomplete_inventory")
    return "inspect_unclassified_failure" if record["outcome"] in {"failed", "retry"} else "no_action"


def build(job, result, *, attempt_token, stage="preparation", packet_path=None, evidence_paths=()):
    """Reduce untrusted packet data to enums, counts, hashes and private paths.

    This is diagnostic evidence, not an independent audit or submission receipt.
    `verified_answer_count` reports the packet's retained inventory statuses; it
    grants neither approval nor permission to infer a new candidate fact.
    """
    job_hash = job.get("dedupe_hash")
    if not isinstance(job_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", job_hash):
        raise ValueError("Feedback requires an exact job hash")
    if not isinstance(attempt_token, str) or not 1 <= len(attempt_token) <= 256 or stage not in STAGES:
        raise ValueError("Invalid attempt token or stage")
    if not isinstance(result, dict):
        raise ValueError("Invalid attempt outcome")
    board = boards.board_type(job.get("url"))
    inventory = result.get("review_inventory")
    inventory = inventory if isinstance(inventory, dict) else {}
    fields = _rows(inventory.get("fields"))
    missing = _rows(result.get("missing"))
    tasks = _rows(result.get("agent_tasks"))
    task_refs = {row["ref"] for row in tasks if isinstance(row.get("ref"), str)}
    # Known field tasks are not new factual questions. Match only actual refs;
    # similar labels in repeated work/education rows are not interchangeable.
    unknown = [row for row in missing if row.get("required") is not False and row.get("ref") not in task_refs]
    events = _rows(result.get("events"))
    operation = next((_enum(row.get("operation"), OPERATIONS, "runtime") for row in reversed(events)
                      if row.get("event") == "technical_failure"), "runtime")
    capture = result.get("capture")
    capture = capture if isinstance(capture, dict) else {}
    submitted = result.get("submitted") is True or result.get("state") == "submitted"
    terminal = _flag(result.get("runtime_click_started", result.get("click_started")))
    # The preparation adapter is guarded from terminal actions. Explicit
    # contrary markers always win; submission/review stages default unknown.
    if (stage in {"classification", "preparation"} and terminal is None and not submitted
            and "runtime_click_started" not in result and "click_started" not in result):
        terminal = False
    refs = []
    for raw in ([packet_path] if packet_path is not None else []) + list(evidence_paths):
        path = private_path(raw)
        relative = str(path.relative_to(config.ROOT.resolve()))
        if len(relative) > 1024 or any(ord(char) < 32 for char in relative):
            raise ValueError("Unsafe feedback evidence name")
        refs.append(relative)
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": hashlib.sha256(json.dumps([job_hash, stage, attempt_token]).encode()).hexdigest(),
        "job_hash": job_hash, "board": board, "stage": stage,
        "recorded_at": int(time.time()), "recorded_at_ns": time.time_ns(),
        "outcome": _enum(result.get("state"), OUTCOMES, "unknown"),
        "error_kind": _enum(result.get("error_kind"), TECHNICAL_KINDS | {"browser_capacity"}, "unclassified"),
        "operation": operation, "retryable": result.get("retryable") is True,
        "required_unknown_count": len(unknown), "known_field_task_count": len(tasks),
        "inventory_count": len(fields),
        "verified_answer_count": sum(row.get("status") == "answered" for row in fields),
        "required_blank_count": sum(row.get("required") is True and row.get("status") != "answered" for row in fields),
        "optional_blank_count": sum(row.get("required") is False and row.get("status") != "answered" for row in fields),
        "inventory_complete": inventory.get("complete") is True and bool(fields)
                              and all(row.get("status") == "answered" for row in fields if row.get("required") is True),
        "screenshot_status": "verified" if capture.get("verified") is True else "failed" if capture else "unavailable",
        "mutation_started": _flag(result.get("mutation_started")),
        "terminal_started": terminal, "submitted": submitted,
        "verification_required": bool(result.get("verification")) or result.get("state") in {"waiting_login", "waiting_captcha"},
        "evidence_paths": sorted(set(refs))[:20],
    }
    # Preserve only fixed runtime enums, never arbitrary exception/page text.
    from .cli_browser import MECHANICAL_DIAGNOSTICS
    detail = next((row.get("mechanical_error") for row in reversed(events)
                   if row.get("event") == "technical_failure"), None)
    if isinstance(detail, str) and detail in MECHANICAL_DIAGNOSTICS:
        record["mechanical_error"] = detail
    record["recovery_recommendation"] = _recommendation(record)
    return record


def record_attempt(job, result, *, attempt_token, stage="preparation", packet_path=None, evidence_paths=()):
    record = build(job, result, attempt_token=attempt_token, stage=stage,
                   packet_path=packet_path, evidence_paths=evidence_paths)
    base = private_path(directory() / record["job_hash"])
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    path = private_path(base / (record["attempt_id"] + ".json"))
    fd = os.open(private_path(base / ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.exists():
            old = json.loads(path.read_text())
            old.pop("recorded_at", None)
            old.pop("recorded_at_ns", None)
            comparable = {key: value for key, value in record.items() if key not in {"recorded_at", "recorded_at_ns"}}
            if old != comparable:
                raise ValueError("Attempt feedback already exists with different evidence")
        else:
            booklet.write_private(path, record)
    finally:
        os.close(fd)
    return path


def records(*, since=0):
    """Read bounded private summaries; malformed/manual records never drive repair."""
    base = private_path(directory())
    if not base.exists():
        return []
    found = []
    for path in base.glob("*/*.json"):
        try:
            path = private_path(path)
            if path.stat().st_size > 32_000:
                continue
            row = json.loads(path.read_text())
            if (not isinstance(row, dict) or row.get("schema_version") != SCHEMA_VERSION
                    or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("job_hash", "")))
                    or path.parent.name != row["job_hash"] or path.stem != row.get("attempt_id")
                    or not re.fullmatch(r"[a-f0-9]{64}", path.stem)
                    or type(row.get("recorded_at")) is not int or row["recorded_at"] < since
                    or type(row.get("recorded_at_ns")) is not int
                    or row.get("board") not in set(boards.ADAPTERS) | {"unknown"}
                    or row.get("stage") not in STAGES or row.get("outcome") not in OUTCOMES
                    or row.get("error_kind") not in TECHNICAL_KINDS | {"browser_capacity", "unclassified"}
                    or row.get("operation") not in OPERATIONS
                    or any(type(row.get(key)) is not int or not 0 <= row[key] <= 100_000 for key in (
                        "required_unknown_count", "known_field_task_count", "inventory_count", "verified_answer_count",
                        "required_blank_count", "optional_blank_count"))
                    or any(type(row.get(key)) is not bool for key in (
                        "retryable", "inventory_complete", "submitted", "verification_required"))
                    or (row.get("terminal_started") is not None and type(row.get("terminal_started")) is not bool)
                    or row.get("recovery_recommendation") not in RECOMMENDATIONS
                    or row["recovery_recommendation"] != _recommendation(row)):
                continue
            # Return only the sanitized schema, never unrecognized additions.
            found.append({key: row[key] for key in (
                "attempt_id", "job_hash", "board", "stage", "recorded_at", "recorded_at_ns", "outcome", "error_kind", "operation",
                "retryable", "required_unknown_count", "known_field_task_count", "inventory_count",
                "verified_answer_count", "required_blank_count", "optional_blank_count", "inventory_complete",
                "submitted", "verification_required", "terminal_started", "recovery_recommendation")}
                | {"feedback_path": str(path.relative_to(config.ROOT.resolve()))})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(found, key=lambda row: (row["recorded_at_ns"], row["attempt_id"]))


def summarize(rows):
    return {"attempts": len(rows), "boards": dict(Counter(row["board"] for row in rows)),
            "outcomes": dict(Counter(row["outcome"] for row in rows)),
            "recommendations": dict(Counter(row["recovery_recommendation"] for row in rows))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--attempt-token", required=True)
    parser.add_argument("--stage", choices=sorted(STAGES), default="preparation")
    args = parser.parse_args(argv)
    path = private_path(args.packet)
    value = json.loads(path.read_text())
    feedback = record_attempt(value["job"], value, attempt_token=args.attempt_token,
                              stage=args.stage, packet_path=path)
    print(json.dumps({"state": "recorded", "attempt_id": feedback.stem}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
