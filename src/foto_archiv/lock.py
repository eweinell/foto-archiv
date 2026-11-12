"""Exklusive Sperre je Index: verhindert, dass zwei Läufe gleichzeitig schreiben."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class LockedError(Exception):
    pass


class IndexLock:
    def __init__(self, index_path: Path):
        self.path = Path(index_path).with_suffix(".lock")
        self._fh = None

    def acquire(self) -> "IndexLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockedError(
                f"Ein anderer foto-archiv-Lauf arbeitet bereits mit diesem Index ({self.path}). "
                "Bitte warten, bis er beendet ist."
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
