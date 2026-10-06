"""Actual disposable process trees; never signal live browser/app workers."""
import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from jhb.applications import monitor, owned_processes
from test_monitor import setup


@pytest.mark.parametrize("leader_exits", [False, True])
def test_bounded_stops_new_session_child_and_preserves_unrelated_process(setup, leader_exits):
    monitor.directory().mkdir()
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    script = ("import subprocess,sys,time; "
              "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
              "print(child.pid,flush=True);" + ("" if leader_exits else "time.sleep(30)"))
    try:
        result = monitor.bounded([sys.executable, "-c", script], monitor.directory()/"nested-session",
                                 monitor.authorization(), .5 if not leader_exits else 5)
        pid = int((monitor.directory()/"nested-session.jsonl").read_text().strip())
        assert result["state"] == ("complete" if leader_exits else "timeout"), result
        assert unrelated.poll() is None
        try:
            child = psutil.Process(pid)
            assert child.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        unrelated.kill(); unrelated.wait(timeout=3)


def test_pid_reuse_never_signals_the_replacement(monkeypatch):
    tracker = object.__new__(owned_processes.OwnedProcesses)
    class Replacement:
        def create_time(self): return 200
        def send_signal(self, sig): pytest.fail("must not signal reused PID")
    monkeypatch.setattr(owned_processes.psutil, "Process", lambda pid: Replacement())
    with pytest.raises(owned_processes.CleanupUnproven, match="PID was reused"):
        tracker._signal(43210, 100, signal.SIGKILL)


def test_disappeared_child_is_already_clean_not_an_error(monkeypatch):
    tracker = object.__new__(owned_processes.OwnedProcesses)
    def gone(pid): raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(owned_processes.psutil, "Process", gone)
    assert tracker._signal(43210, 100, signal.SIGKILL) is None


def test_unreadable_possible_orphan_keeps_cleanup_unproven(monkeypatch):
    tracker = object.__new__(owned_processes.OwnedProcesses)
    tracker.members={};tracker.uid=os.getuid();tracker.started_at=100;tracker.token="synthetic"
    class PotentialOrphan:
        pid=43210
        def create_time(self): return 101
        def uids(self): return type("Uids", (), {"real":os.getuid()})()
        def status(self): return psutil.STATUS_RUNNING
        def environ(self): raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(owned_processes.psutil, "process_iter", lambda: [PotentialOrphan()])
    with pytest.raises(owned_processes.CleanupUnproven, match="ownership is unreadable"):
        tracker.scan(recover_orphans=True)
