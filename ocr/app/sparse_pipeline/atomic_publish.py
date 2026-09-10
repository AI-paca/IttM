from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class AtomicPublishUnavailableError(RuntimeError):
    """Raised when the host cannot guarantee no-replace publication."""


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish ``source`` while refusing every existing path.

    Plain POSIX ``rename`` may replace an empty destination directory.  Linux
    ``renameat2(RENAME_NOREPLACE)`` closes that race and is required here;
    unsupported hosts fail closed instead of weakening artifact immutability.
    """

    if not isinstance(source, Path) or not isinstance(destination, Path):
        raise TypeError("atomic publication paths must be pathlib.Path values")
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise AtomicPublishUnavailableError("renameat2(RENAME_NOREPLACE) is required for atomic publication")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    source_bytes = os.fsencode(os.path.abspath(source))
    destination_bytes = os.fsencode(os.path.abspath(destination))
    if (
        renameat2(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise AtomicPublishUnavailableError("host filesystem cannot guarantee no-replace publication")
    raise OSError(error_number, os.strerror(error_number), destination)


__all__ = ["AtomicPublishUnavailableError", "rename_no_replace"]
