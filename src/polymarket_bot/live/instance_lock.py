"""Prevents two live-start processes from running at once. See
live/RUNBOOK.md's "-10." section -- added after two separate live-start
processes were found running simultaneously against the same real account
in one session (different Python installs, started manually).

An OS-level exclusive file lock, not a PID file with a liveness check: a PID
file can go stale if a process crashes without cleanup, and "is this PID
still alive" is unreliable cross-platform (os.kill(pid, 0) on Windows
actually calls TerminateProcess for non-CTRL signals, it doesn't just
probe). An OS-level lock is released automatically by the OS the instant
the holding process exits for ANY reason -- normal exit, crash, or kill -9
-- so no stale-lock cleanup logic is ever needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .. import config, storage
from ..logger import get_logger
from ..models import utcnow_iso

logger = get_logger("live.instance_lock")

LOCK_FILE = config.LIVE_TRADES_DIR / "live_bot.lock"


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """Exclusive, OS-enforced lock held for the entire live-start
    run_forever() lifetime. Released automatically on process exit
    (including a crash) by the OS, or explicitly on a clean __exit__."""

    def __init__(self, lock_path: Optional[Path] = None):
        self._lock_path = lock_path or LOCK_FILE
        self._fh = None

    def __enter__(self) -> "InstanceLock":
        storage.ensure_dir(self._lock_path.parent)
        self._fh = open(self._lock_path, "a+b")
        try:
            # Append mode starts the file position at EOF once the file
            # already has content from a previous run -- msvcrt.locking()
            # locks a byte range starting from the CURRENT position, so
            # without this seek, every restart after the first would lock a
            # different, non-overlapping range and never actually contend.
            self._fh.seek(0)
            _try_lock(self._fh)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise AlreadyRunningError(
                "Another live-start process appears to already be running "
                f"(exclusive lock already held on {self._lock_path}). "
                "Running two at once can race on the same account's "
                "orders/ledger -- stop the other one first."
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()} {utcnow_iso()}".encode("utf-8"))
        self._fh.flush()
        logger.info("Acquired live-start instance lock (pid %d).", os.getpid())
        return self

    def __exit__(self, *exc_info) -> None:
        # No explicit unlock call -- both platforms release the lock on
        # close() (msvcrt: released "by closing the file"; POSIX flock(2):
        # released "when all such file descriptors have been closed"). A
        # manual unlock would have to reproduce the exact same seek/nbytes
        # as the lock call or itself risk an OSError -- close() alone is
        # simpler and removes that whole class of bug.
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _try_lock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
