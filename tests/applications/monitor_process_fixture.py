"""Explicit process inventories for positive disposable-process integration tests."""
import subprocess
from types import SimpleNamespace

import psutil
import pytest

from jhb.applications import owned_processes


@pytest.fixture
def controlled_process_inventory(monkeypatch):
    """Keep native ownership checks/signals, excluding unrelated desktop churn.

    This is deliberately opt-in. Negative unreadable-foreign, PID-reuse and
    unknown-ownership tests must exercise their own inventories unchanged.
    PID files identify candidates to enumerate; they never register ownership.
    A reparented child still needs its real birth time and inherited token.
    """
    original_popen, original_iter = subprocess.Popen, psutil.process_iter
    inventory = SimpleNamespace(spawned=[], pid_files=[], enumerated=set())

    def spawn(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        inventory.spawned.append(process)
        return process

    def processes():
        pids = {process.pid for process in inventory.spawned}
        for path in inventory.pid_files:
            recorded = path.read_text().strip() if path.exists() else ""
            if recorded:
                pids.add(int(recorded))
        for process in original_iter():
            if process.pid in pids:
                inventory.enumerated.add(process.pid)
                yield process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(owned_processes.psutil, "process_iter", processes)
    return inventory
