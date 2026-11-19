"""Single-instance guard using an OS file lock, plus a PID file for the scripts.

The OS releases the lock automatically if the process dies, so a crash never
leaves the bot "locked out".
"""

from __future__ import annotations

import contextlib
import os
from typing import IO

from .paths import LOCK_FILE as LOCK_PATH
from .paths import PID_FILE as PID_PATH
from .paths import ensure_runtime_dir

_handle: IO[str] | None = None


class AlreadyRunningError(RuntimeError):
    """Another bot instance already holds the lock."""


def _lock(fileobj: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fileobj: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)


def acquire() -> None:
    """Take the lock or raise AlreadyRunningError."""
    global _handle
    ensure_runtime_dir()
    fileobj = open(LOCK_PATH, "a+", encoding="utf-8")  # noqa: SIM115 - held for process lifetime
    try:
        _lock(fileobj)
    except OSError as exc:
        fileobj.close()
        raise AlreadyRunningError("Another instance of the bot is already running.") from exc
    _handle = fileobj
    with open(PID_PATH, "w", encoding="utf-8") as pid_file:
        pid_file.write(str(os.getpid()))


def release() -> None:
    global _handle
    with contextlib.suppress(OSError):
        with open(PID_PATH, encoding="utf-8") as pid_file:
            owner = pid_file.read().strip()
        # Remove only after closing: Windows can't delete an open file.
        if owner == str(os.getpid()):
            os.remove(PID_PATH)
    if _handle is not None:
        with contextlib.suppress(OSError):
            _unlock(_handle)
        _handle.close()
        _handle = None
