"""Exact tested-tree reuse after diagnostics, without live services or Codex."""
import pytest

from jhb.applications import booklet, monitor
from test_monitor import failure, repo, runner, setup


def seed_validation(snapshot):
    monitor._write(monitor.directory() / "state.json", {
        "issues": {}, "log_cursors": {}, "validated_repository": snapshot,
    })


def latest_validation():
    state = monitor._state()
    return next(iter(state["issues"].values()))["validation"]


@pytest.mark.parametrize("dirty", [False, True])
def test_successful_diagnostic_reuses_only_exact_previously_tested_snapshot(setup, dirty):
    failure(setup)
    snapshot = repo(dirty=dirty, tree="tested-untracked-and-tracked-content")
    seed_validation(snapshot)
    auth = monitor.authorization()
    protected = monitor.protected_data(auth)
    calls = []
    assert monitor.once(run=runner(calls), inspect_repository=lambda: snapshot)["state"] == "validated"
    assert len(calls) == 1 and calls[0][0][0] == "codex"
    assert latest_validation() == {
        "state": "complete", "mode": "reused_validated_repository", "repository": snapshot,
    }
    assert monitor.authorization() == auth
    assert monitor.protected_data(auth) == protected
    assert not (monitor.directory() / "repair-pending.json").exists()


@pytest.mark.parametrize("baseline", [None, {}, {"head": "abc123"}, repo(tree="other-tested-tree")])
def test_unknown_or_different_validation_baseline_runs_full_checks(setup, baseline):
    failure(setup)
    seed_validation(baseline)
    calls = []
    assert monitor.once(run=runner(calls), inspect_repository=repo)["state"] == "validated"
    assert len(calls) == 4
    assert calls[2][0][-3:] == ["-m", "pytest", "-q"]
    assert "mode" not in latest_validation()


def test_changed_code_after_diagnostic_still_runs_full_checks(setup):
    failure(setup)
    seed_validation(repo())
    calls = []
    edited = False
    def mutate():
        nonlocal edited
        edited = True
    def inspect():
        return repo(dirty=edited, tree="new-code" if edited else "clean")
    assert monitor.once(run=runner(calls, mutate=mutate), inspect_repository=inspect)["state"] == "validated"
    assert len(calls) == 4 and "mode" not in latest_validation()
    assert monitor._state()["validated_repository"] == inspect()


def test_changed_protected_candidate_data_never_uses_cached_validation(setup):
    failure(setup)
    seed_validation(repo())
    calls = []
    def mutate():
        booklet.write_private(setup[0] / "private" / "answer-booklet.json",
                              {"answers": {"synthetic": "changed"}})
    assert monitor.once(run=runner(calls, mutate=mutate), inspect_repository=repo)["state"] == "quarantined"
    assert len(calls) == 4 and "mode" not in latest_validation()
    assert (monitor.directory() / "repair-pending.json").exists()


@pytest.mark.parametrize("change", ["revoked", "paused", "same_id_changed_authority"])
def test_changed_active_authority_cannot_reuse_validation(setup, monkeypatch, change):
    failure(setup)
    seed_validation(repo())
    calls = []
    original = monitor.authorization()
    changed = False
    def current(*args):
        if not changed:
            return original
        if change == "revoked":
            return None
        return {**original, "repair_authority": False} if change == "paused" else {
            **original, "content": "Changed authority with a synthetic reused ID"}
    def mutate():
        nonlocal changed
        changed = True
    monkeypatch.setattr(monitor, "authorization", current)
    outcome = monitor.once(run=runner(calls, mutate=mutate), inspect_repository=repo)
    assert "mode" not in latest_validation()
    assert outcome["state"] == "quarantined"
    if change == "same_id_changed_authority":
        # Even a synthetic loader reusing an ID cannot pass the final check.
        assert len(calls) == 4
    else:
        assert len(calls) == 1


def test_failed_child_never_reuses_previously_tested_snapshot(setup):
    failure(setup)
    seed_validation(repo())
    calls = []
    assert monitor.once(run=runner(calls, fail=lambda command: True), inspect_repository=repo)["state"] == "quarantined"
    assert len(calls) == 1 and latest_validation() == {"state": "not_run"}


@pytest.mark.parametrize("late_change", ["repository", "authority", "protected"])
def test_reuse_is_rechecked_before_releasing_gate(setup, monkeypatch, late_change):
    failure(setup)
    seed_validation(repo())
    calls = []
    inspections = 0
    original = monitor.authorization()
    original_protected = monitor.protected_data
    def inspect():
        nonlocal inspections
        inspections += 1
        if late_change == "repository" and inspections >= 3:
            return repo(tree="late-edit")
        return repo()
    monkeypatch.setattr(monitor, "authorization", lambda *args: (
        {**original, "repair_authority": False}
        if late_change == "authority" and inspections >= 3 else original))
    monkeypatch.setattr(monitor, "protected_data", lambda *args: (
        "late-private-edit" if late_change == "protected" and inspections >= 3
        else original_protected(*args)))
    assert monitor.once(run=runner(calls), inspect_repository=inspect)["state"] == "quarantined"
    assert len(calls) == 1
    assert latest_validation()["mode"] == "reused_validated_repository"
    assert (monitor.directory() / "repair-pending.json").exists()
