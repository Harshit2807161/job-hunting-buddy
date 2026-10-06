"""Exhausted history must not pause preparation for an unclaimable repair."""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from jhb import config
from jhb.applications import monitor, queue
from test_monitor import setup, failure, feedback_attempt, repo, runner


def health():
    return json.loads((monitor.directory() / "health.json").read_text())


def test_repair_budget_matches_normal_preparation_claim_budget():
    assert monitor.MAX_PREPARATION_ATTEMPTS == queue.claim.__kwdefaults__["max_attempts"] == 3


@pytest.mark.parametrize("changes,reason", [
    ({"state": "failed", "attempts": 3}, "retry_budget_exhausted"),
    ({"state": "retry", "attempts": 3}, "retry_budget_exhausted"),
    ({"state": "failed", "attempts": 1}, "not_claimable_retry"),
    ({"attempts": -1}, "missing_queue_evidence"),
    ({"attempts": None}, "missing_queue_evidence"),
    ({"available_at": None}, "missing_queue_evidence"),
    ({"available_at": "future"}, "missing_queue_evidence"),
    ({"available_at": 9_999_999_999}, "retry_backoff"),
    ({"lease_until": 9_999_999_999}, "retained_lease"),
])
def test_nonclaimable_packet_is_preserved_as_diagnostic_without_repair(setup, changes, reason):
    packet = failure(setup, **changes)
    packet_before = packet.read_bytes()
    with sqlite3.connect(config.DB_PATH) as connection:
        queue_before = connection.execute("SELECT * FROM applications").fetchall()
    assert monitor.once(run=lambda *a, **k: pytest.fail("nonclaimable repair"),
                        inspect_repository=repo)["state"] == "healthy"
    assert health()["technical_issues"] == []
    diagnostic, = health()["deferred_application_issues"]
    assert diagnostic["defer_reason"] == reason
    assert str(packet.relative_to(config.ROOT)) in diagnostic["evidence"]
    assert diagnostic["job_hashes"] == [f"{1:064x}"]
    assert not (monitor.directory() / "repair-pending.json").exists()
    assert monitor._state()["issues"] == {}
    assert packet.read_bytes() == packet_before
    with sqlite3.connect(config.DB_PATH) as connection:
        assert connection.execute("SELECT * FROM applications").fetchall() == queue_before


def test_missing_legacy_queue_columns_do_not_imply_available_retry_budget(setup):
    failure(setup)
    with sqlite3.connect(config.DB_PATH) as connection:
        connection.execute("ALTER TABLE applications DROP COLUMN attempts")
    assert monitor.once(run=lambda *a, **k: pytest.fail("queue proof missing"),
                        inspect_repository=repo)["state"] == "healthy"
    assert health()["deferred_application_issues"][0]["defer_reason"] == "missing_queue_evidence"


def test_exhausted_feedback_without_packet_cannot_trigger_repair(setup):
    _, _, now = setup
    path = feedback_attempt()
    with sqlite3.connect(config.DB_PATH) as connection:
        connection.execute("INSERT INTO applications(job_hash,state,attempts,updated_at) VALUES (?,'failed',3,?)",
                           ("b" * 64, now))
    assert monitor.once(run=lambda *a, **k: pytest.fail("feedback cannot renew attempts"),
                        inspect_repository=repo)["state"] == "healthy"
    diagnostic, = health()["deferred_application_issues"]
    assert diagnostic["defer_reason"] == "retry_budget_exhausted"
    assert str(path.relative_to(config.ROOT)) in diagnostic["evidence"]
    assert health()["attempt_feedback"]["attempts"] == 1


@pytest.mark.parametrize("same_fingerprint", [True, False])
def test_due_retry_precedes_exhausted_history_and_does_not_inherit_its_evidence(setup, same_fingerprint):
    exhausted = failure(setup, number=1, state="failed", attempts=3, kind="TimeoutError")
    active = failure(setup, number=2, attempts=2,
                     kind="TimeoutError" if same_fingerprint else "browser_mechanics")
    calls = []
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
    assert len(calls) == 4
    assert str(active.relative_to(config.ROOT)) in calls[0][1]
    assert str(exhausted.relative_to(config.ROOT)) not in calls[0][1]
    assert f"{1:064x}" not in calls[0][1]
    assert len(health()["deferred_application_issues"]) == 1
    assert health()["technical_issues"][0]["job_hashes"] == [f"{2:064x}"]


def test_far_future_retry_only_becomes_repairable_inside_bounded_horizon(setup, monkeypatch):
    _, _, now = setup
    failure(setup, attempts=2, available_at=now + 601)
    calls = []
    monkeypatch.setattr(monitor.time, "time", lambda: now)
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "healthy"
    assert health()["deferred_application_issues"][0]["defer_reason"] == "retry_backoff"
    monkeypatch.setattr(monitor.time, "time", lambda: now + 1)
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
    assert len(calls) == 4
    assert health()["deferred_application_issues"] == []


@pytest.mark.parametrize("seconds_until_due", [1, 299, 300, 600])
def test_repair_during_backoff_never_claims_early_or_changes_queue_timing(setup, monkeypatch, seconds_until_due):
    _, _, now = setup
    failure(setup, attempts=2, available_at=now + seconds_until_due)
    monkeypatch.setattr(monitor.time, "time", lambda: now)
    with sqlite3.connect(config.DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        # The queue's real SELECT requires its full schema even when no retry
        # is due. Exercise normal claim rather than mocking its timing guard.
        connection.execute("ALTER TABLE applications ADD COLUMN job_json TEXT NOT NULL DEFAULT '{}'")
        queue.initialize(connection)
        before = dict(connection.execute("SELECT * FROM applications").fetchone())
        assert queue.claim(connection) is None
        calls = []
        assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
        assert len(calls) == 4
        assert queue.claim(connection) is None
        assert dict(connection.execute("SELECT * FROM applications").fetchone()) == before
        assert before["attempts"] == 2
        assert before["available_at"] == now + seconds_until_due


def change_when_worker_locked(monkeypatch, change):
    original = monitor._lock
    changed = False

    @contextmanager
    def locked(path):
        nonlocal changed
        with original(path) as owned:
            if owned and path.name == "application-worker.lock" and not changed:
                changed = True
                change()
            yield owned

    monkeypatch.setattr(monitor, "_lock", locked)


@pytest.mark.parametrize("update", [
    "state='waiting_review'", "state='submitted'", "state='discarded'",
    "state='failed',attempts=3", "available_at=9999999999", "lease_until=9999999999",
])
def test_queue_is_rechecked_under_worker_locks_before_creating_repair_gate(setup, monkeypatch, update):
    failure(setup)

    def change():
        with sqlite3.connect(config.DB_PATH) as connection:
            connection.execute(f"UPDATE applications SET {update}")

    change_when_worker_locked(monkeypatch, change)
    assert monitor.once(run=lambda *a, **k: pytest.fail("stale queue repaired"),
                        inspect_repository=repo)["state"] == "healthy"
    assert not (monitor.directory() / "repair-pending.json").exists()
    assert monitor._state()["issues"] == {}
    assert health()["technical_issues"] == []


def test_recheck_removes_recovered_job_from_shared_fingerprint_prompt(setup, monkeypatch):
    recovered = failure(setup, number=1)
    active = failure(setup, number=2, attempts=2)

    def change():
        with sqlite3.connect(config.DB_PATH) as connection:
            connection.execute("UPDATE applications SET state='waiting_review' WHERE job_hash=?", (f"{1:064x}",))

    change_when_worker_locked(monkeypatch, change)
    calls = []
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
    assert str(active.relative_to(config.ROOT)) in calls[0][1]
    assert str(recovered.relative_to(config.ROOT)) not in calls[0][1]
    assert f"{1:064x}" not in calls[0][1]


def test_recheck_does_not_consume_incremental_log_cursor_twice(setup, monkeypatch):
    failure(setup)
    original = monitor._logs
    reads = []

    def logs(state, issues):
        reads.append(1)
        return original(state, issues)

    monkeypatch.setattr(monitor, "_logs", logs)
    assert monitor.once(run=runner([]), inspect_repository=repo)["state"] == "validated"
    assert reads == [1]
