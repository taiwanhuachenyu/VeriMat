"""Cross-platform primitives so the trusted runtime behaves identically on POSIX and Windows.

Three POSIX facilities the runtime depends on have no direct Windows equivalent. They are
provided here once instead of being worked around at each call site:

* advisory whole-file locks -- ``fcntl.flock`` on POSIX, ``LockFileEx`` on Windows. Both
  distinguish shared from exclusive and support a non-blocking acquire that raises
  ``BlockingIOError``, so callers need no platform branch.
* durable directory metadata -- ``fsync`` on a directory descriptor. Windows exposes no
  user-mode equivalent; see ``fsync_directory``.
* private, non-inheritable, symlink-refusing appends for audit logs.

The traffic also runs the other way: Windows caps a path at 260 characters where POSIX does not,
so ``extended_path`` and ``sqlite_readonly_uri`` give the content-addressed stores one place to
opt out of that limit instead of every caller discovering it as a ``FileNotFoundError``.

``platform_report`` states which guarantees are actually enforced on the running host, so a
reproducibility manifest can record the difference rather than silently assume POSIX.
"""
from __future__ import annotations

import contextlib
import errno
import os
import sys
import urllib.parse
from pathlib import Path
from typing import IO, Any, Iterator, Union

WINDOWS = sys.platform == "win32"

Lockable = Union[int, IO[Any]]

__all__ = [
    "WINDOWS",
    "DIRECTORY_FSYNC_SUPPORTED",
    "FILE_MODE_ENFORCED",
    "exclusive_lock",
    "shared_lock",
    "lock_exclusive",
    "lock_shared",
    "unlock",
    "fsync_directory",
    "fsync_file",
    "is_group_or_world_accessible",
    "create_private_file",
    "open_append_nofollow",
    "extended_path",
    "sqlite_readonly_uri",
    "platform_report",
]


def _descriptor(target: Lockable) -> int:
    return target if isinstance(target, int) else target.fileno()


if not WINDOWS:
    import fcntl

    DIRECTORY_FSYNC_SUPPORTED = True
    FILE_MODE_ENFORCED = True

    def lock_exclusive(target: Lockable, *, blocking: bool = True) -> None:
        fcntl.flock(_descriptor(target), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def lock_shared(target: Lockable, *, blocking: bool = True) -> None:
        fcntl.flock(_descriptor(target), fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))

    def unlock(target: Lockable) -> None:
        fcntl.flock(_descriptor(target), fcntl.LOCK_UN)

    def fsync_directory(path: str | os.PathLike[str]) -> None:
        """Flush a directory entry so a rename into it survives a crash.

        ``O_DIRECTORY`` is what makes a non-directory an error here. Without it Linux flushes a
        regular file without complaint -- ``fsync`` accepts a read-only descriptor -- and the
        caller walks away believing it holds a rename barrier that was never taken; the Windows
        branch raises on the same mistake, so the guarantee would differ by platform. Letting the
        kernel refuse during the open also beats a preceding ``stat``, which can describe a
        different inode than the one that ends up flushed, and which would not stop
        ``os.open`` from blocking on a fifo.

        The flag is read directly rather than through ``getattr``: it is POSIX.1-2008 and present
        wherever CPython builds a posix module, and a host that genuinely lacks it cannot make
        this guarantee at all. Failing at import is the honest outcome there.
        """
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    _APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
    _CREATE_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC

else:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    DIRECTORY_FSYNC_SUPPORTED = False
    FILE_MODE_ENFORCED = False

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997
    _WHOLE_FILE = 0xFFFFFFFF

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    def _apply(target: Lockable, flags: int) -> None:
        handle = msvcrt.get_osfhandle(_descriptor(target))
        overlapped = _Overlapped()
        if _kernel32.LockFileEx(
            handle, flags, 0, _WHOLE_FILE, _WHOLE_FILE, ctypes.byref(overlapped)
        ):
            return
        code = ctypes.get_last_error()
        if code in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise BlockingIOError(errno.EWOULDBLOCK, "file is locked by another process")
        raise ctypes.WinError(code)

    def lock_exclusive(target: Lockable, *, blocking: bool = True) -> None:
        flags = _LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        _apply(target, flags)

    def lock_shared(target: Lockable, *, blocking: bool = True) -> None:
        _apply(target, 0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)

    def unlock(target: Lockable) -> None:
        handle = msvcrt.get_osfhandle(_descriptor(target))
        overlapped = _Overlapped()
        if not _kernel32.UnlockFileEx(
            handle, 0, _WHOLE_FILE, _WHOLE_FILE, ctypes.byref(overlapped)
        ):
            code = ctypes.get_last_error()
            if code != _ERROR_LOCK_VIOLATION:
                raise ctypes.WinError(code)

    def fsync_directory(path: str | os.PathLike[str]) -> None:
        """No-op: Windows exposes no user-mode directory flush.

        NTFS journals the metadata of ``os.replace``/``os.link`` before the call returns, so a
        rename that has been observed is already recoverable. The POSIX pattern of fsyncing the
        parent descriptor has no counterpart -- ``FlushFileBuffers`` requires write access, which
        a directory handle cannot be granted. ``DIRECTORY_FSYNC_SUPPORTED`` reports this so the
        weaker guarantee reaches the run manifest instead of being assumed away.

        The type check is not decoration: it keeps the misuse that ``O_DIRECTORY`` catches on
        POSIX an error here too, with the same ``ENOTDIR``, so a caller cannot depend on a
        platform quietly accepting a file. It goes through ``extended_path`` because a bare
        ``is_dir`` swallows the ``OSError`` a path over 260 characters raises and answers False,
        which would turn the content-addressed store's own directories into spurious failures.
        """
        if not extended_path(path).is_dir():
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path))

    _APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_BINARY | os.O_NOINHERIT
    _CREATE_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_BINARY | os.O_NOINHERIT


@contextlib.contextmanager
def exclusive_lock(target: Lockable, *, blocking: bool = True) -> Iterator[None]:
    lock_exclusive(target, blocking=blocking)
    try:
        yield
    finally:
        unlock(target)


@contextlib.contextmanager
def shared_lock(target: Lockable, *, blocking: bool = True) -> Iterator[None]:
    lock_shared(target, blocking=blocking)
    try:
        yield
    finally:
        unlock(target)


def fsync_file(path: str | os.PathLike[str]) -> None:
    """Flush an already-closed file to stable storage.

    The file is reopened for update rather than for reading: ``FlushFileBuffers`` needs write
    access, so Windows fails ``os.fsync`` on a read-only descriptor with ``EBADF``.
    """
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def is_group_or_world_accessible(path: str | os.PathLike[str]) -> bool:
    """Whether users other than the owner can read ``path``, as far as the platform can say.

    CPython synthesises Windows mode bits from the read-only attribute alone and never consults
    the ACL, so the question is unanswerable there and the answer is ``False`` by declaration.
    ``FILE_MODE_ENFORCED`` records that the check is inactive so a run manifest discloses the
    weaker guarantee instead of implying a POSIX one.
    """
    if not FILE_MODE_ENFORCED:
        return False
    return bool(Path(path).stat().st_mode & 0o077)


def create_private_file(path: str | os.PathLike[str]) -> None:
    """Create ``path`` if absent and restrict it to the owner as far as the platform allows."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"refusing to create through a symbolic link: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, _CREATE_FLAGS, 0o600)
    try:
        if not WINDOWS:
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def open_append_nofollow(path: str | os.PathLike[str]) -> int:
    """Open ``path`` for appending, refusing to traverse a symbolic link.

    POSIX enforces this atomically with ``O_NOFOLLOW``. Windows has no such flag, so the check is
    a pre-open test and therefore racy against an attacker who can create links inside the log
    directory; that directory is expected to be owner-controlled.
    """
    if WINDOWS and Path(path).is_symlink():
        raise OSError(errno.ELOOP, "refusing to append through a symbolic link", str(path))
    return os.open(path, _APPEND_FLAGS)


_EXTENDED_PREFIX = "\\\\?\\"
_DEVICE_PREFIX = "\\\\.\\"
_UNC_PREFIX = "\\\\?\\UNC\\"
_UNC_ROOT = "\\\\"


def extended_path(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` in a form the host can open regardless of its length.

    Windows applies the 260-character ``MAX_PATH`` limit to every path reaching the Win32 API
    unless it carries the extended-length prefix, and the registry opt-out is off by default.
    This runtime nests a 64-character tenant digest, a shard and a 64-character content digest
    under a caller-supplied root, so an ordinary temporary directory pushes the result past the
    limit and every open fails with ``FileNotFoundError``.

    The prefix also disables path normalisation, so the argument must already be absolute with no
    ``.`` or ``..`` component: ``abspath`` is applied first, on POSIX as well, so a relative root
    resolves identically on both platforms and the prefix is the only difference.

    Idempotent, because a doubly prefixed path is rejected by every API: a path already carrying
    the extended-length or device prefix is returned untouched.
    """
    text = os.fspath(path)
    if not WINDOWS:
        return Path(os.path.abspath(text))
    if text.startswith(_EXTENDED_PREFIX) or text.startswith(_DEVICE_PREFIX):
        return Path(text)
    absolute = os.path.abspath(text)
    if absolute.startswith(_UNC_ROOT):
        return Path(_UNC_PREFIX + absolute[len(_UNC_ROOT):])
    return Path(_EXTENDED_PREFIX + absolute)


def sqlite_readonly_uri(path: str | os.PathLike[str]) -> str:
    """Build a read-only SQLite URI for ``path``, which may carry the long-path prefix.

    SQLite reads ``?`` as the start of the URI query string and a leading ``//`` as an authority,
    and the prefix contains both, so an interpolated Windows path either loses ``mode=ro`` or is
    rejected outright.  Percent-encoding the path resolves it; SQLite decodes the escapes before
    opening the file.  ``/`` and ``:`` are left literal so a POSIX URI is byte-identical to the
    plain interpolation this replaces.
    """
    return f"file:{urllib.parse.quote(str(extended_path(path)), safe=':/')}?mode=ro"


def platform_report() -> dict[str, Any]:
    """Machine-readable statement of the durability and privacy guarantees actually in force."""
    return {
        "platform": sys.platform,
        "advisory_file_locking": "LockFileEx" if WINDOWS else "fcntl.flock",
        "shared_locks_supported": True,
        "directory_fsync_supported": DIRECTORY_FSYNC_SUPPORTED,
        "owner_only_file_mode_enforced": FILE_MODE_ENFORCED,
        "symlink_refusal": "pre-open check" if WINDOWS else "atomic (O_NOFOLLOW)",
        "path_length_limit": 260 if WINDOWS else None,
        "path_length_limit_bypassed": "extended-length prefix" if WINDOWS else "not applicable",
    }
