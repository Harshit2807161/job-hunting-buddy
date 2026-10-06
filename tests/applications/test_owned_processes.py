"""Actual disposable process trees; never signal live browser/app workers."""
import os
import json
import signal
import subprocess
import sys
import time

import psutil
import pytest

from jhb.applications import monitor, owned_processes
from monitor_process_fixture import controlled_process_inventory
from test_monitor import setup, failure, repo


@pytest.mark.parametrize("leader_exits", [False, True])
def test_bounded_stops_new_session_child_and_preserves_unrelated_process(
        setup, monkeypatch, leader_exits, controlled_process_inventory):
    monitor.directory().mkdir()
    output = monitor.directory() / "nested-session.jsonl"
    original_tracker = owned_processes.OwnedProcesses
    controlled_process_inventory.pid_files.append(output)

    def tracker(process, *args):
        if leader_exits:
            # Force the orphan route rather than occasionally finding the
            # child through its still-alive parent before reparenting.
            process.wait(timeout=5)
        tracked = original_tracker(process, *args)
        if leader_exits:
            assert tracked.members == {}
        return tracked

    monkeypatch.setattr(owned_processes, "OwnedProcesses", tracker)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    script = ("import subprocess,sys,time; "
              "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
              "print(child.pid,flush=True);" + ("" if leader_exits else "time.sleep(30)"))
    try:
        result = monitor.bounded([sys.executable, "-c", script], monitor.directory()/"nested-session",
                                 monitor.authorization(), .5 if not leader_exits else 5)
        pid = int(output.read_text().strip())
        assert result["state"] == ("complete" if leader_exits else "timeout"), result
        assert unrelated.poll() is None
        assert {pid, unrelated.pid} <= controlled_process_inventory.enumerated
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
    tracker.members={};tracker.uid=os.getuid();tracker.started_at=100;tracker.token="synthetic";tracker.initial_errors=[]
    class PotentialOrphan:
        pid=43210
        def create_time(self): return 101
        def uids(self): return type("Uids", (), {"real":os.getuid()})()
        def status(self): return psutil.STATUS_RUNNING
        def environ(self): raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(owned_processes.psutil, "process_iter", lambda: [PotentialOrphan()])
    monkeypatch.setattr(owned_processes.psutil, "Process", lambda pid: PotentialOrphan())
    with pytest.raises(owned_processes.CleanupUnproven, match="ownership is unreadable"):
        tracker.scan(recover_orphans=True)


def test_uncertain_foreign_process_does_not_leave_known_repair_tree_running(setup, monkeypatch):
    monitor.directory().mkdir()
    actual_iter=psutil.process_iter
    actual_process=psutil.Process
    class UnreadableForeign:
        pid=987654321
        def create_time(self): return time.time()
        def uids(self): return type("Uids", (), {"real":os.getuid()})()
        def status(self): return psutil.STATUS_RUNNING
        def environ(self): raise psutil.AccessDenied(self.pid)
        def send_signal(self, sig): pytest.fail("foreign process must not be signaled")
    monkeypatch.setattr(owned_processes.psutil, "process_iter", lambda: iter([*actual_iter(),UnreadableForeign()]))
    monkeypatch.setattr(owned_processes, "_process", lambda pid: UnreadableForeign() if pid==987654321 else actual_process(pid))
    script=("import os,subprocess,sys,time,json;"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);"
            "print(json.dumps([os.getpid(),child.pid]),flush=True);time.sleep(30)")
    result=monitor.bounded([sys.executable,'-c',script],monitor.directory()/'uncertain-foreign',
                           monitor.authorization(),.5)
    assert result['state']=='failed' and result['error_kind']=='ProcessCleanupError'
    pids=json.loads((monitor.directory()/'uncertain-foreign.jsonl').read_text())
    for pid in pids:
        try:assert psutil.Process(pid).status()==psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:pass


def test_scan_error_still_stops_positively_owned_leader(setup, monkeypatch):
    monitor.directory().mkdir()
    original_scan=owned_processes.OwnedProcesses.scan
    def uncertain(self, *, recover_orphans=False, errors=None, deadline=None):
        if errors is None:
            raise owned_processes.CleanupUnproven('Synthetic live scan uncertainty')
        return original_scan(self,recover_orphans=recover_orphans,errors=errors,deadline=deadline)
    monkeypatch.setattr(owned_processes.OwnedProcesses,'scan',uncertain)
    original_remember=owned_processes.OwnedProcesses.__init__
    seen=[]
    def record(self,process,*args):
        original_remember(self,process,*args)
        seen.append((process.pid,self.leader))
    monkeypatch.setattr(owned_processes.OwnedProcesses,'__init__',record)
    result=monitor.bounded([sys.executable,'-c','import time;time.sleep(30)'],monitor.directory()/'scan-error',
                           monitor.authorization(),5)
    assert result['state']=='failed' and result['error_kind']=='CleanupUnproven'
    assert len(seen)==1 and seen[0][1] is not None
    with pytest.raises(psutil.NoSuchProcess):psutil.Process(seen[0][0])


def test_expired_scan_does_not_read_additional_process_environments(monkeypatch):
    tracker=object.__new__(owned_processes.OwnedProcesses)
    tracker.members={};tracker.initial_errors=[]
    class UnreadProcess:
        pid=43210
        def create_time(self):pytest.fail('scan deadline must precede process reads')
    monkeypatch.setattr(owned_processes.psutil,'process_iter',lambda:[UnreadProcess()])
    with pytest.raises(owned_processes.CleanupUnproven,match='deadline'):
        tracker.scan(recover_orphans=True,deadline=time.monotonic()-1)


def test_orphan_scan_refreshes_cached_process_birth_before_age_filter(monkeypatch):
    tracker=object.__new__(owned_processes.OwnedProcesses)
    tracker.members={};tracker.initial_errors=[];tracker.uid=os.getuid();tracker.started_at=100;tracker.token='synthetic-owned'
    class Cached:
        pid=43210
        def create_time(self):return 1
    class Fresh(Cached):
        def create_time(self):return 101
        def uids(self):return type('Uids',(),{'real':os.getuid()})()
        def status(self):return psutil.STATUS_RUNNING
        def environ(self):return {owned_processes.TOKEN_ENV:'synthetic-owned'}
    monkeypatch.setattr(owned_processes.psutil,'process_iter',lambda:[Cached()])
    monkeypatch.setattr(owned_processes.psutil,'Process',lambda pid:Fresh())
    tracker.scan(recover_orphans=True)
    assert tracker.members=={43210:101}


def test_tracker_constructor_failure_stops_direct_child_and_keeps_quarantine(setup, monkeypatch):
    failure(setup)
    children=[]
    def broken_tracker(process,*args):
        assert process.poll() is None
        children.append(process)
        raise RuntimeError('Synthetic tracker initialization failed')
    monkeypatch.setattr(owned_processes,'OwnedProcesses',broken_tracker)
    commands=[]
    def run(command,prefix,auth,timeout,**kwargs):
        commands.append(command)
        kwargs.pop('input_text',None)
        return monitor.bounded([sys.executable,'-c','import time;time.sleep(30)'],prefix,auth,timeout,**kwargs)
    result=monitor.once(run=run,inspect_repository=repo)
    assert result['state']=='quarantined'
    assert len(commands)==len(children)==1  # Validation must not start.
    assert children[0].poll() is not None
    with pytest.raises(psutil.NoSuchProcess):psutil.Process(children[0].pid)
    pending=json.loads((monitor.directory()/'repair-pending.json').read_text())
    assert pending['repair']['error_kind']=='ProcessCleanupError'
    assert pending['validation']['state']=='not_run'


def test_unreadable_leader_birth_still_stops_direct_popen_child(setup, monkeypatch):
    monitor.directory().mkdir()
    actual_popen=subprocess.Popen;actual_process=owned_processes._process;children=[]
    def remember(*args,**kwargs):
        child=actual_popen(*args,**kwargs);children.append(child);return child
    class UnreadableBirth:
        def __init__(self,pid):self.pid=pid
        def create_time(self):raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(monitor.subprocess,'Popen',remember)
    monkeypatch.setattr(owned_processes,'_process',lambda pid:UnreadableBirth(pid) if children and pid==children[0].pid else actual_process(pid))
    result=monitor.bounded([sys.executable,'-c','import time;time.sleep(30)'],monitor.directory()/'birth-error',
                           monitor.authorization(),5)
    assert result['state']=='failed' and result['error_kind']=='ProcessCleanupError'
    assert len(children)==1 and children[0].poll() is not None
    with pytest.raises(psutil.NoSuchProcess):psutil.Process(children[0].pid)
