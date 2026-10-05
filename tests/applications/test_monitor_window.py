"""Review-mode supervision never creates a submission grant or sends email."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from jhb import config
from jhb.applications import booklet, hourly_reports, monitor, monitor_window, overnight
from test_monitor import setup, failure, repo, runner


def preparation_window(setup, **changes):
    root, _, now = setup
    window = {
        "role": "user", "status": "verified", "enabled": True,
        "scope": monitor_window.SCOPE, "submission_authority": False,
        "repair_authority": True, "source": "synthetic explicit finite preparation consent",
        "content": "Monitor preparation failures and fix technical issues for the next hour",
        "authorized_at": datetime.fromtimestamp(now-60, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(now+3600, timezone.utc).isoformat(),
        **changes,
    }
    path = root / "private" / monitor_window.WINDOW_NAME
    booklet.write_private(path, window)
    return path


def report_window(setup):
    root, _, now = setup
    path = root / "private" / hourly_reports.REPORT_WINDOW
    booklet.write_private(path, {
        "role": "user", "status": "verified", "enabled": True,
        "scope": hourly_reports.REPORT_SCOPE, "submission_authority": False,
        "frequency_seconds": 3600, "source": "synthetic hourly email consent",
        "content": "Keep emailing me every hour during the next hour",
        "authorized_at": datetime.fromtimestamp(now-60, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(now+3600, timezone.utc).isoformat(),
    })
    return path


def review_mode(setup, monkeypatch):
    _, submit_path, _ = setup
    booklet.write_private(submit_path, {**json.loads(submit_path.read_text()), "enabled": False})
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "0")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")


def test_preparation_repair_survives_revoked_submission_consent_without_changing_it(setup, monkeypatch):
    path = preparation_window(setup)
    review_mode(setup, monkeypatch)
    before = setup[1].read_bytes()
    failure(setup)
    monkeypatch.setattr(hourly_reports, "report", lambda *a, **k: pytest.fail("monitor must not send email"))
    # A window authorizes only this supervisor. The actual submit loader rejects it.
    assert overnight.load_authorization(path) is None
    calls = []
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
    assert len(calls) == 4 and "grants no submission authority" in calls[0][1]
    health = json.loads((monitor.directory() / "health.json").read_text())
    assert health["monitoring_kind"] == "preparation_repair"
    assert health["state"] == "validated"
    assert health["repair_authority"] is True and health["submission_authority"] is False
    assert health["application_states"] == {"failed": 1}
    assert setup[1].read_bytes() == before and overnight.load_authorization() is None
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "healthy"


def test_report_window_observes_failures_in_review_mode_but_cannot_start_repair(setup, monkeypatch):
    report_window(setup)
    review_mode(setup, monkeypatch)
    failure(setup)
    forbidden = lambda *a, **k: pytest.fail("report consent must not repair or email")
    monkeypatch.setattr(hourly_reports, "report", forbidden)
    result = monitor.once(run=forbidden, inspect_repository=repo)
    assert result == {"state": "observing", "repairs": 0, "technical_issues": 1}
    health = json.loads((monitor.directory() / "health.json").read_text())
    assert health["monitoring_kind"] == "report_observation"
    assert health["state"] == "observing"
    assert health["repair_authority"] is False
    monkeypatch.setattr(monitor.subprocess, "Popen", forbidden)
    assert monitor.bounded(["unused"], monitor.directory() / "no-repair", monitor.authorization(), 1)["state"] == "authorization_ended"
    assert monitor.validate(monitor.authorization(), forbidden)["state"] == "authorization_ended"
    assert not (monitor.directory() / "repair-pending.json").exists()


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"role": "assistant"}, {"status": "pending"}, {"source": ""},
    {"scope": "submit applications"}, {"submission_authority": True}, {"repair_authority": False},
    {"content": "Send hourly emails"}, {"content": "Stop monitoring and repair issues"},
    {"authorized_at": "2099-01-01T00:00:00+00:00"},
    {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": "2099-01-01T00:00:00+00:00"},
    {"expires_at": "2026-10-05T00:00:00"},
])
def test_invalid_preparation_window_does_not_fall_back_to_report_or_submission(setup, changes):
    report_window(setup)
    preparation_window(setup, **changes)
    assert monitor.authorization() is None
    assert monitor.once(run=lambda *a, **k: pytest.fail("invalid repair consent"))["state"] == "authorization_ended"


@pytest.mark.parametrize("gate", ["monitor", "ci", "symlink", "outside", "large"])
def test_new_window_keeps_environment_and_private_path_guards(setup, monkeypatch, gate):
    path = preparation_window(setup)
    if gate == "monitor":
        monkeypatch.delenv("JHB_OVERNIGHT_MONITOR_ENABLED")
    elif gate == "ci":
        monkeypatch.setenv("CI", "true")
    else:
        value = path.read_bytes()
        if gate == "symlink":
            path.unlink()
            target = setup[0] / "private" / "other.json"
            target.write_bytes(value)
            path.symlink_to(target)
        elif gate == "outside":
            path = setup[0] / "public.json"
            path.write_bytes(value)
        else:
            path.write_bytes(value + b" " * 65536)
    assert monitor.authorization(path) is None


@pytest.mark.parametrize("changed", ["submission_authority", "approval_file", "approval_state", "manual_packet", "application_state"])
def test_preparation_repair_cannot_change_approvals_drafts_or_revoked_submit_grant(setup, monkeypatch, changed):
    root, submit_path, _ = setup
    preparation_window(setup)
    review_mode(setup, monkeypatch)
    packet = failure(setup)
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("CREATE TABLE application_approvals(approval_id TEXT,state TEXT)")
    def mutate():
        if changed == "submission_authority":
            booklet.write_private(submit_path, {**json.loads(submit_path.read_text()), "enabled": True})
        elif changed == "approval_file":
            booklet.write_private(root / "private" / "application-approvals" / "synthetic.json", {"approved": True})
        elif changed == "manual_packet":
            booklet.write_private(packet, {"state": "waiting_review", "filled": [{"value": "replaced manual answer"}]})
        else:
            with sqlite3.connect(config.DB_PATH) as conn:
                if changed == "approval_state":
                    conn.execute("INSERT INTO application_approvals VALUES ('synthetic','approved')")
                else:
                    conn.execute("UPDATE applications SET state='waiting_review'")
    assert monitor.once(run=runner([], mutate=mutate), inspect_repository=repo)["state"] == "quarantined"
    assert (monitor.directory() / "repair-pending.json").exists()


def test_new_window_respects_worker_locks_revocation_and_existing_quarantine(setup, monkeypatch):
    path = preparation_window(setup)
    review_mode(setup, monkeypatch)
    failure(setup)
    with monitor._lock(config.ROOT / "private" / "approved-worker.lock") as owned:
        assert owned
        assert monitor.once(run=lambda *a, **k: pytest.fail("must wait"), inspect_repository=repo)["state"] == "approved_worker_busy"
    def revoke():
        booklet.write_private(path, {**json.loads(path.read_text()), "enabled": False})
    calls = []
    assert monitor.once(run=runner(calls, mutate=revoke), inspect_repository=repo)["state"] == "quarantined"
    assert len(calls) == 1  # No validation may start after revocation.
    assert monitor.once()["state"] == "authorization_ended"
    report_window(setup)
    assert monitor.authorization() is None
    assert (monitor.directory() / "repair-pending.json").exists()


def test_explicit_pause_disables_repairs_even_with_valid_window(setup):
    preparation_window(setup)
    failure(setup)
    auth = monitor.authorization()
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"paused": True})
    result = monitor.once(run=lambda *a, **k: pytest.fail("paused repair"), inspect_repository=repo)
    assert result["state"] == "paused"
    assert json.loads((monitor.directory() / "health.json").read_text())["state"] == "paused"
    assert monitor.validate(auth, lambda *a, **k: pytest.fail("paused validation"))["state"] == "authorization_ended"


def test_window_renewal_does_not_reset_quarantine_or_repair_history(setup):
    preparation_window(setup)
    monitor.once()
    state = (monitor.directory() / "state.json").read_bytes()
    booklet.write_private(monitor.directory() / "repair-pending.json", {"state": "quarantined"})
    preparation_window(setup, source="new explicit finite consent")
    assert monitor.once()["state"] == "authorization_changed"
    assert (monitor.directory() / "state.json").read_bytes() == state
    assert (monitor.directory() / "repair-pending.json").exists()
