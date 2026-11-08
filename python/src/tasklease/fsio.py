"""Atomic publication and filesystem persistence for the local stores.

These barriers target common local filesystems that implement their platform's
flush operations. They do not promise that hardware, network filesystems, or
Windows directory creation survives every possible power failure.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast


def _sync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def ensure_directory(path: Path) -> None:
    """Create private directories and flush every POSIX parent entry.

    Existing directories also need a barrier: another process may have just
    created them. Start at the filesystem root rather than assuming an existing
    ancestor is already durable. The tree must not be concurrently renamed or
    removed. Existing directory permissions are preserved.
    """
    path = Path(os.path.abspath(path))
    for directory in (*reversed(path.parents), path):
        if directory.parent == directory:
            continue
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise NotADirectoryError(
                    errno.ENOTDIR, "Expected a directory", str(directory)
                ) from None
        _sync_directory(directory.parent)


@contextmanager
def _prepared_file(path: Path, data: bytes) -> Iterator[Path]:
    ensure_directory(path.parent)
    descriptor, filename = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _move_windows(source: Path, destination: Path, *, replace: bool) -> None:
    """Move on one volume, requesting write-through and optional replacement."""
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "Windows publication is unavailable")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    function = library.MoveFileExW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    function.restype = ctypes.c_int
    move = cast(Callable[[str, str, int], int], function)
    # MOVEFILE_WRITE_THROUGH; never enable the copy/delete cross-volume fallback.
    flags = 0x8 | (0x1 if replace else 0)
    if not move(str(source), str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def _sync_publication(path: Path) -> None:
    # A second publisher can observe the name before the first publisher gets
    # here. It must finish the same barrier before returning "exists".
    _sync_directory(path.parent)
    # Windows file flushes include cached metadata. Darwin's full flush must
    # follow the directory flush, so the drive-cache barrier also covers the
    # newly published namespace entry. Opening also refuses an existing directory.
    with path.open("r+b" if os.name == "nt" else "rb") as source:
        os.fsync(source.fileno())
        if sys.platform == "darwin":
            import fcntl

            fcntl.fcntl(source.fileno(), fcntl.F_FULLFSYNC)


def publish_new(path: Path, data: bytes) -> Literal["created", "exists"]:
    """Publish complete bytes once, flushing before either successful result.

    Published files must be immutable, and callers must not concurrently remove
    or rename their directories. The "exists" barrier makes duplicate reply
    acknowledgement safe when another publisher has not yet finished flushing.
    Windows uses its documented file flush and write-through move operations;
    these do not establish a universal ancestor-namespace power-loss guarantee.
    """
    with _prepared_file(path, data) as temporary:
        result: Literal["created", "exists"] = "created"
        try:
            if os.name == "nt":
                _move_windows(temporary, path, replace=False)
            else:
                os.link(temporary, path)
        except FileExistsError:
            result = "exists"
        _sync_publication(path)
        return result


def atomic_replace(path: Path, data: bytes) -> None:
    """Atomically replace a file using the same persistence barriers."""
    with _prepared_file(path, data) as temporary:
        if os.name == "nt":
            _move_windows(temporary, path, replace=True)
        else:
            os.replace(temporary, path)
        _sync_publication(path)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Stored JSON must contain finite numbers")
    return number


def read_json(path: Path) -> object | None:
    """Read UTF-8 JSON; missing files return None and JSON null is rejected."""
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value: object = json.loads(
        contents, parse_float=_finite_float, parse_constant=_finite_float
    )
    if value is None:
        raise ValueError(f"Stored JSON must not be null: {path}")
    return value


def cleanup_temporary_files(directory: Path, *, before: float) -> int:
    """Remove regular dot-prefixed .tmp files older than a cutoff, nonrecursively.

    A process killed while writing may leave these behind. A live publication
    whose old temp is removed fails safely without acknowledging a server task.
    """
    removed = 0
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not entry.name.startswith(".") or not entry.name.endswith(".tmp"):
            continue
        try:
            metadata = entry.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < before:
                entry.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    if removed:
        _sync_directory(directory)
    return removed
