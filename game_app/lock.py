"""ENG-F05: one worker per installation (07 §1/§11).

The product runs as a single local process.  Two workers on one installation
would both claim the execution slot and both advance the same task queue, so
the second one must be refused instead of silently doubling the writer.

The lock is an OS file lock on ``<managed root>/application.lock``:

* it is held by the file handle, so a crashed process releases it with the
  handle and no stale-lock cleanup is needed;
* it is non-blocking: a busy lock is a stable storage error, not a hang;
* the error message names no absolute path (ENG-V06 / 07 §11).

Only *worker* commands take the lock.  ``task run`` and ``--wait`` advance the
execution slot; reads, registrations and short control requests must still
complete while a long task runs (07 §3 "短请求可完成").
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import IO

from game_app.errors import ContractError, ErrorCode

LOCK_FILE = "application.lock"
LOCKED_MESSAGE = (
    "another game-app worker already holds this installation's application"
    " lock; only one worker may run at a time"
)


def _lock_exclusive(handle: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            # ``msvcrt.locking`` needs a byte to lock; the content is irrelevant.
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl  # pragma: no cover - POSIX only

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl  # pragma: no cover - POSIX only

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ApplicationLock:
    """Exclusive, non-blocking ownership of one installation's worker slot."""

    def __init__(self, root: str | Path, *, name: str = LOCK_FILE) -> None:
        self.root = Path(root)
        self.path = self.root / name
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "ApplicationLock":
        if self._handle is not None:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock_exclusive(handle)
        except OSError as error:
            handle.close()
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED, LOCKED_MESSAGE
            ) from error
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:  # pragma: no cover - closing the handle also releases
            pass
        finally:
            handle.close()

    def __enter__(self) -> "ApplicationLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["ApplicationLock", "LOCK_FILE", "LOCKED_MESSAGE"]
