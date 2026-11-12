import pytest

from foto_archiv.lock import IndexLock, LockedError


def test_lock_exclusive(tmp_path):
    a = IndexLock(tmp_path / "index.sqlite").acquire()
    with pytest.raises(LockedError):
        IndexLock(tmp_path / "index.sqlite").acquire()
    a.release()
    b = IndexLock(tmp_path / "index.sqlite").acquire()
    b.release()
