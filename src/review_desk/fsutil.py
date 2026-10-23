"""Atomic JSON writes: temp file in the same directory, fsync, os.replace. A failed write leaves the old file and no temp."""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path


class NotRegularError(OSError):
    """The path is a FIFO, device, directory or the like."""


def read_regular(path, max_bytes: int | None = None) -> bytes:
    """Read a regular file without ever blocking on one that turns into a FIFO or device after a check on its path.
    Open non-blocking (that returns at once even for a FIFO), look at the open descriptor, and only then read."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularError(errno.EINVAL, "not a regular file", str(path))
        if max_bytes is not None and st.st_size > max_bytes:
            raise OSError(errno.EFBIG, "file too large", str(path))
        f = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with f:
        return f.read()


def atomic_write_json(path: Path, obj) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
