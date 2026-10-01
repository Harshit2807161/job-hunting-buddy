"""Single-instance lock for a poll cycle.

Task Scheduler's MultipleInstances=IgnoreNew only stops the scheduler from
overlapping itself. A manual `python -m jhb.poll --once` can still run at the
same moment as a scheduled fire -- both would read the same `pending` rows
before either called mark_notified, and both would email them.
"""

from __future__ import annotations

import contextlib
import os
import time

from . import config

LOCK_PATH = config.DATA_DIR / "poll.lock"
STALE_AFTER = 25 * 60  # a cycle that has run this long is dead, not working


@contextlib.contextmanager
def single_instance():
    """Yield True if this process holds the lock, False if another cycle owns it."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age > STALE_AFTER:
            LOCK_PATH.unlink(missing_ok=True)   # previous run died mid-cycle

    # O_EXCL is atomic: whoever creates the file first owns the lock.
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False          # someone else owns it -- must NOT touch their file
        return

    try:
        os.write(fd, f"{os.getpid()} {int(time.time())}".encode())
        os.close(fd)
        yield True
    finally:
        # Only the owner removes the lock.
        with contextlib.suppress(OSError):
            LOCK_PATH.unlink(missing_ok=True)
