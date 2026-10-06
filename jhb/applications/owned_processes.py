"""Bound a repair command's descendants even when tools start new sessions.

An inherited random token recovers a short-lived parent's orphaned children.
Signals still bind a PID's original creation time, never a name or bare group ID.
"""
from __future__ import annotations

import os
import signal
import time

import psutil

TOKEN_ENV = "JHB_REPAIR_PROCESS_TOKEN"


class CleanupUnproven(RuntimeError):
    pass


class OwnedProcesses:
    def __init__(self, process, token, started_at):
        self.process, self.token, self.started_at = process, token, started_at
        self.uid = os.getuid()
        self.members = {}
        self.leader = None
        try:
            leader = psutil.Process(process.pid)
            self.leader = self._remember(leader)
        except psutil.NoSuchProcess:
            # A very short command may already have exited. The unguessable
            # inherited token still identifies its otherwise orphaned children.
            if process.poll() is None:
                raise CleanupUnproven("Repair process identity is unavailable")

    def _remember(self, process):
        try:
            birth = process.create_time()
            if process.uids().real != self.uid or birth < self.started_at - 1:
                raise CleanupUnproven("Repair descendant ownership changed")
            old = self.members.get(process.pid)
            if old is not None and old != birth:
                raise CleanupUnproven("Repair descendant PID was reused")
            self.members[process.pid] = birth
            return birth
        except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
            raise CleanupUnproven("Repair descendant identity is unreadable") from exc

    def _current(self, pid, birth):
        try:
            process = psutil.Process(pid)
            if process.create_time() != birth:
                raise CleanupUnproven("Repair descendant PID was reused")
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            return process
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied as exc:
            raise CleanupUnproven("Repair descendant state is unreadable") from exc

    def scan(self, *, recover_orphans=False):
        """Capture descendants before their parent exits; keep their birth IDs."""
        for pid, birth in list(self.members.items()):
            process = self._current(pid, birth)
            if process is None:
                continue
            try:
                for child in process.children(recursive=True):
                    try:
                        self._remember(child)
                    except psutil.NoSuchProcess:
                        pass
            except psutil.NoSuchProcess:
                continue  # The recorded parent can exit between these reads.
            except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
                raise CleanupUnproven("Repair descendant tree is unreadable") from exc
        if recover_orphans:
            # Only inspect same-user processes born during this exact command.
            # Never log their environment or treat a generic process name as proof.
            for process in psutil.process_iter():
                try:
                    if (process.pid in self.members or process.pid == os.getpid()
                            or process.create_time() < self.started_at - 1
                            or process.uids().real != self.uid
                            or process.status() == psutil.STATUS_ZOMBIE):
                        continue
                    if process.environ().get(TOKEN_ENV) == self.token:
                        self._remember(process)
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
                    raise CleanupUnproven("Potential repair orphan ownership is unreadable") from exc

    def _signal(self, pid, birth, sig):
        process = self._current(pid, birth)
        if process is None:
            return
        try:
            # psutil performs its own reuse check too. Individual signals avoid
            # affecting unrelated members that joined a numeric process group.
            process.send_signal(sig)
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, PermissionError) as exc:
            raise CleanupUnproven("Owned repair process could not be stopped") from exc

    def cleanup(self, *, timeout=5):
        deadline = time.monotonic() + timeout
        stopped = set()
        self.scan(recover_orphans=True)
        # Pause the command leader first, then each discovered live descendant.
        # Re-scan after freezing to include descendants spawned during the scan.
        while time.monotonic() < deadline:
            previous = set(self.members.items())
            for pid, birth in list(self.members.items()):
                if (pid, birth) not in stopped:
                    self._signal(pid, birth, signal.SIGSTOP)
                    stopped.add((pid, birth))
            self.scan(recover_orphans=True)
            if set(self.members.items()) == previous:
                break
        else:
            raise CleanupUnproven("Repair descendant tree did not stabilize")
        # Descendants first. SIGKILL ends a stopped process without resuming a
        # shell long enough to launch another command. The parent reaps its child.
        ordered = sorted(self.members.items(), key=lambda row: row[0] == self.process.pid)
        for pid, birth in ordered:
            self._signal(pid, birth, signal.SIGKILL)
        try:
            self.process.wait(timeout=max(.01, deadline-time.monotonic()))
        except Exception as exc:
            raise CleanupUnproven("Repair leader could not be reaped") from exc
        while time.monotonic() < deadline:
            self.scan(recover_orphans=True)
            live = [(pid, birth) for pid, birth in self.members.items() if self._current(pid, birth)]
            if not live:
                return
            # Newly proven orphans may have reparented during stopping. Their
            # exact identities remain owned; never signal an unknown process.
            for pid, birth in live:
                self._signal(pid, birth, signal.SIGKILL)
            time.sleep(.05)
        raise CleanupUnproven("Live repair descendants remain")
