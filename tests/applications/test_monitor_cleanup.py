"""Synthetic permission-error boundaries for owned subprocess cleanup."""
import errno
import json
import signal
import subprocess
from types import SimpleNamespace

import pytest

from jhb.applications import monitor
from test_monitor import setup, failure, repo


class ReapedProcess:
    pid = 54321
    returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        return self.returncode


def permission(signals, *, denied=signal.SIGKILL, code=errno.EPERM):
    def killpg(pid, sig):
        assert pid == ReapedProcess.pid
        signals.append(sig)
        if sig == denied:
            raise PermissionError(code, "synthetic denied signal")
    return killpg


def snapshot(monkeypatch, text, returncode=0):
    def run(command, **kwargs):
        assert command == ["/bin/ps", "-axo", "pid=,pgid=,stat="]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 2, "check": False}
        return SimpleNamespace(returncode=returncode, stdout=text)
    monkeypatch.setattr(monitor.subprocess, "run", run)


@pytest.mark.parametrize("members", ["1 1 Ss\n", "1 1 Ss\n54322 54321 Z+\n"])
def test_eperm_accepted_only_with_reaped_leader_and_dead_or_absent_group(monkeypatch, members):
    signals = []
    monkeypatch.setattr(monitor.os, "killpg", permission(signals))
    snapshot(monkeypatch, members)
    monitor._signal_owned_group(ReapedProcess(), signal.SIGKILL)
    assert signals == [signal.SIGKILL]


@pytest.mark.parametrize("members", [
    "1 1 Ss\n54322 54321 S\n",  # Live descendant after leader exit.
    "54321 54321 R\n",  # Reused leader/group identity fails closed too.
    "1 1 Ss\nmalformed row\n", "", "1 1 Ss\n54322 invalid Z\n",
])
def test_eperm_not_accepted_for_live_or_unverifiable_members(monkeypatch, members):
    monkeypatch.setattr(monitor.os, "killpg", permission([]))
    snapshot(monkeypatch, members)
    with pytest.raises(PermissionError):
        monitor._signal_owned_group(ReapedProcess(), signal.SIGKILL)


@pytest.mark.parametrize("cause", ["leader_alive", "ps_failed", "ps_timeout", "other_errno"])
def test_other_failures_never_become_proof_of_cleanup(monkeypatch, cause):
    process = ReapedProcess()
    if cause == "leader_alive":
        process.returncode = None
    monkeypatch.setattr(monitor.os, "killpg", permission([], code=errno.EACCES if cause == "other_errno" else errno.EPERM))
    snapshot(monkeypatch, "1 1 Ss\n", returncode=1 if cause == "ps_failed" else 0)
    if cause == "ps_timeout":
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("ps", 2)
        monkeypatch.setattr(monitor.subprocess, "run", timeout)
    with pytest.raises(PermissionError):
        monitor._signal_owned_group(process, signal.SIGKILL)


@pytest.mark.parametrize("members,expected", [("1 1 Ss\n54322 54321 Z\n", "complete"),
                                               ("1 1 Ss\n54322 54321 S\n", "failed")])
def test_bounded_cleanup_records_failure_instead_of_success_or_exception(setup, monkeypatch, members, expected):
    monitor.directory().mkdir()
    signals = []
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *args, **kwargs: ReapedProcess())
    monkeypatch.setattr(monitor.os, "killpg", permission(signals))
    snapshot(monkeypatch, members)
    result = monitor.bounded(["synthetic"], monitor.directory() / "cleanup", monitor.authorization(), 5)
    assert result["state"] == expected
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    if expected == "failed":
        assert result["error_kind"] == "ProcessCleanupError"
        assert result["cleanup_errors"] == ["PermissionError"]


def test_unproven_cleanup_preserves_repair_quarantine_and_skips_validation(setup, monkeypatch):
    failure(setup)
    signals = []
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *args, **kwargs: ReapedProcess())
    # Both denied signals still get attempted. Neither can be excused by a live group.
    def denied(pid, sig):
        signals.append(sig)
        raise PermissionError(errno.EPERM, "synthetic denied signal")
    monkeypatch.setattr(monitor.os, "killpg", denied)
    snapshot(monkeypatch, "1 1 Ss\n54322 54321 S\n")
    calls = []
    def run(command, prefix, auth, timeout, **kwargs):
        calls.append(command)
        # Codex stdin is irrelevant to the synthetic cleanup check.
        kwargs.pop("input_text", None)
        return monitor.bounded(command, prefix, auth, timeout, **kwargs)
    result = monitor.once(run=run, inspect_repository=repo)
    assert result["state"] == "quarantined"
    assert len(calls) == 1
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    pending = monitor.directory() / "repair-pending.json"
    assert pending.exists()
    evidence = json.loads(pending.read_text())
    assert evidence["repair"]["error_kind"] == "ProcessCleanupError"
    assert evidence["validation"]["state"] == "not_run"
