"""Real publication races, persistence barriers and process-crash recovery."""

from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import stat
import sys
import time
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier, Event
from pathlib import Path

import pytest

from tasklease import fsio

_PAYLOAD_SIZE = 256 * 1024


def _payload(index: int) -> bytes:
    return bytes([index]) * _PAYLOAD_SIZE + b"complete"


def _publish_racer(
    path: str, index: int, barrier: Barrier, results: Queue[tuple[int, str]]
) -> None:
    barrier.wait(timeout=30)
    results.put((index, fsio.publish_new(Path(path), _payload(index))))


def _paused_publisher(path: str, visible: Event, finish: Event) -> None:
    synchronize = fsio._sync_publication

    def pause(path: Path) -> None:
        visible.set()
        if not finish.wait(timeout=30):
            raise TimeoutError("Publisher was not released")
        synchronize(path)

    fsio._sync_publication = pause
    fsio.publish_new(Path(path), _payload(31))


def _crash_publisher(path: str, point: str) -> None:
    if point == "prepared":
        synchronize = os.fsync

        def crash_after_file_sync(descriptor: int) -> None:
            synchronize(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                os._exit(23)

        patch = pytest.MonkeyPatch()
        patch.setattr(os, "fsync", crash_after_file_sync)
    else:

        def crash_after_publish(path: Path) -> None:
            os._exit(23)

        fsio._sync_publication = crash_after_publish
    fsio.publish_new(Path(path), _payload(29))


def test_spawned_publishers_have_one_winner_and_only_whole_bytes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    count = 6
    barrier = context.Barrier(count)
    results: Queue[tuple[int, str]] = context.Queue()
    path = tmp_path / "new" / "parents" / "reply.json"
    processes = [
        context.Process(target=_publish_racer, args=(str(path), i, barrier, results))
        for i in range(count)
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()
        results.join_thread()
    winners = [index for index, result in outcomes if result == "created"]
    assert len(winners) == 1
    assert sorted(result for _, result in outcomes) == ["created"] + ["exists"] * 5
    assert path.read_bytes() == _payload(winners[0])
    assert list(path.parent.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


def test_loser_finishes_barrier_while_winner_is_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = multiprocessing.get_context("spawn")
    visible, finish = context.Event(), context.Event()
    path = tmp_path / "reply.json"
    process = context.Process(
        target=_paused_publisher, args=(str(path), visible, finish)
    )
    process.start()
    try:
        assert visible.wait(timeout=30)
        assert path.read_bytes() == _payload(31)
        synchronized: list[Path] = []
        synchronize = fsio._sync_publication

        def record_sync(destination: Path) -> None:
            synchronize(destination)
            synchronized.append(destination)

        monkeypatch.setattr(fsio, "_sync_publication", record_sync)
        assert fsio.publish_new(path, b"duplicate") == "exists"
        assert synchronized == [path]
        assert process.is_alive()
        assert path.read_bytes() == _payload(31)
    finally:
        finish.set()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("point", ["prepared", "published"])
def test_process_crash_leaves_absent_or_complete_file_and_retry_recovers(
    tmp_path: Path, point: str
) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "reply.json"
    process = context.Process(target=_crash_publisher, args=(str(path), point))
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)
    assert process.exitcode == 23
    if point == "prepared":
        assert not path.exists()
        assert fsio.publish_new(path, b"recovered") == "created"
        assert path.read_bytes() == b"recovered"
    else:
        assert path.read_bytes() == _payload(29)
        assert fsio.publish_new(path, b"duplicate") == "exists"
        assert path.read_bytes() == _payload(29)
    removed = fsio.cleanup_temporary_files(tmp_path, before=time.time() + 60)
    assert removed == (0 if os.name == "nt" and point == "published" else 1)
    assert list(tmp_path.iterdir()) == [path]


def test_existing_destination_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    assert fsio.publish_new(path, b"original") == "created"
    before = path.stat()
    assert fsio.publish_new(path, b"replacement") == "exists"
    assert path.read_bytes() == b"original"
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_replace_exposes_only_complete_new_bytes(tmp_path: Path) -> None:
    path = tmp_path / "new" / "settings.toml"
    fsio.atomic_replace(path, b"old")
    fsio.atomic_replace(path, _payload(7))
    assert path.read_bytes() == _payload(7)
    assert list(path.parent.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("replace", [False, True])
def test_failed_file_flush_cleans_temp_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"original")
    synchronize = os.fsync

    def fail_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("file flush failed")
        synchronize(descriptor)

    monkeypatch.setattr(os, "fsync", fail_file_sync)
    operation = fsio.atomic_replace if replace else fsio.publish_new
    with pytest.raises(OSError, match="file flush failed"):
        operation(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("replace", [False, True])
def test_failed_publish_cleans_temp_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"original")

    def fail_publish(source: Path, destination: Path) -> None:
        raise OSError("publish failed")

    def fail_move(source: Path, destination: Path, *, replace: bool) -> None:
        raise OSError("publish failed")

    if os.name == "nt":
        monkeypatch.setattr(fsio, "_move_windows", fail_move)
    else:
        monkeypatch.setattr(os, "replace" if replace else "link", fail_publish)
    operation = fsio.atomic_replace if replace else fsio.publish_new
    with pytest.raises(OSError, match="publish failed"):
        operation(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("existing", [False, True])
def test_failed_publication_barrier_never_returns_success_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    path = tmp_path / "record.json"
    if existing:
        fsio.publish_new(path, b"original")

    def fail_barrier(destination: Path) -> None:
        raise OSError("namespace flush failed")

    with monkeypatch.context() as patch:
        patch.setattr(fsio, "_sync_publication", fail_barrier)
        with pytest.raises(OSError, match="namespace flush failed"):
            fsio.publish_new(path, b"new")
    assert path.read_bytes() == (b"original" if existing else b"new")
    assert list(tmp_path.iterdir()) == [path]
    assert fsio.publish_new(path, b"duplicate") == "exists"


def test_ensure_directory_flushes_existing_ancestor_chain_and_preserves_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o750)
    original_mode = stat.S_IMODE(existing.stat().st_mode)
    path = existing / "new" / "leaf"
    synchronized: list[Path] = []
    synchronize = fsio._sync_directory

    def record_sync(directory: Path) -> None:
        synchronize(directory)
        synchronized.append(directory)

    monkeypatch.setattr(fsio, "_sync_directory", record_sync)
    fsio.ensure_directory(path)
    assert synchronized == list(reversed(path.parents))
    assert stat.S_IMODE(existing.stat().st_mode) == original_mode
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    synchronized.clear()
    fsio.ensure_directory(path)
    assert synchronized == list(reversed(path.parents))


def test_parent_file_and_destination_directory_fail(tmp_path: Path) -> None:
    parent = tmp_path / "file"
    parent.write_bytes(b"original")
    with pytest.raises(NotADirectoryError):
        fsio.publish_new(parent / "record.json", b"new")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(OSError):
        fsio.publish_new(directory, b"new")
    assert not list(directory.iterdir())
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["directory", "file"]


def test_read_json_returns_none_only_for_missing_and_preserves_failures(
    tmp_path: Path,
) -> None:
    path = tmp_path / "record.json"
    assert fsio.read_json(path) is None
    path.write_text('{"request_id":"Foo","data":[1,true]}', encoding="utf-8")
    assert fsio.read_json(path) == {"request_id": "Foo", "data": [1, True]}
    path.write_text("null", encoding="utf-8")
    with pytest.raises(ValueError, match="must not be null"):
        fsio.read_json(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        fsio.read_json(path)
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        fsio.read_json(path)
    with pytest.raises(OSError):
        fsio.read_json(tmp_path)


@pytest.mark.parametrize(
    "number",
    ["NaN", "Infinity", "-Infinity", "1e999", "-1e999", "1.7976931348623159e308"],
)
def test_read_json_rejects_nested_nonfinite_numbers(
    tmp_path: Path, number: str
) -> None:
    path = tmp_path / "record.json"
    path.write_text('{"data":{"nested":[0,' + number + "]}}", encoding="utf-8")
    with pytest.raises(ValueError, match="finite numbers"):
        fsio.read_json(path)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("1.7976931348623157e308", sys.float_info.max),
        ("-1.7976931348623157e308", -sys.float_info.max),
        ("0", 0),
        ("-17", -17),
        ("1208925819614629174706176", 2**80),
        ("1.25", 1.25),
        ("true", True),
        ("[null,false]", [None, False]),
        (r'"\ud800"', "\ud800"),
        (
            r'{"data":{"escaped":"\udfff","nested":[null,2]}}',
            {"data": {"escaped": "\udfff", "nested": [None, 2]}},
        ),
    ],
)
def test_read_json_preserves_finite_numbers_and_other_valid_values(
    tmp_path: Path, contents: str, expected: object
) -> None:
    path = tmp_path / "record.json"
    path.write_text(contents, encoding="utf-8")
    assert fsio.read_json(path) == expected


def test_stale_temp_cleanup_is_nonrecursive_and_ignores_other_entries(
    tmp_path: Path,
) -> None:
    old = tmp_path / ".record.json.random.tmp"
    old.write_bytes(b"abandoned")
    os.utime(old, (1, 1))
    fresh = tmp_path / ".record.json.fresh.tmp"
    fresh.write_bytes(b"active")
    visible = tmp_path / "record.json"
    visible.write_bytes(b"kept")
    other = tmp_path / "other.tmp"
    other.write_bytes(b"kept")
    directory = tmp_path / ".directory.tmp"
    directory.mkdir()
    nested = directory / ".nested.random.tmp"
    nested.write_bytes(b"kept")
    assert fsio.cleanup_temporary_files(tmp_path, before=2) == 1
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        ".directory.tmp",
        ".record.json.fresh.tmp",
        "other.tmp",
        "record.json",
    ]
    assert nested.read_bytes() == b"kept"
    assert fsio.cleanup_temporary_files(tmp_path / "missing", before=2) == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX supports unprivileged symlinks")
def test_temp_cleanup_does_not_follow_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"kept")
    link = tmp_path / ".symlink.random.tmp"
    link.symlink_to(target)
    assert fsio.cleanup_temporary_files(tmp_path, before=time.time() + 60) == 0
    assert link.is_symlink()
    assert target.read_bytes() == b"kept"


def test_windows_move_requests_write_through_without_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, int]] = []

    class Move:
        argtypes: list[object]
        restype: object

        def __call__(self, source: str, destination: str, flags: int) -> int:
            calls.append((source, destination, flags))
            return 1

    class Library:
        MoveFileExW = Move()

    def load_library(name: str, *, use_last_error: bool) -> Library:
        assert name == "kernel32"
        assert use_last_error is True
        return Library()

    source, destination = tmp_path / "temporary", tmp_path / "final"
    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    fsio._move_windows(source, destination, replace=False)
    fsio._move_windows(source, destination, replace=True)
    assert calls == [
        (str(source), str(destination), 0x8),
        (str(source), str(destination), 9),
    ]


def test_darwin_full_flush_follows_namespace_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("Darwin flush uses POSIX fcntl")
    import fcntl

    path = tmp_path / "record.json"
    path.write_bytes(b"whole")
    events: list[str] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fsio, "_sync_directory", lambda directory: events.append("dir"))
    monkeypatch.setattr(os, "fsync", lambda descriptor: events.append("file"))
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)

    def full_flush(descriptor: int, operation: int) -> int:
        assert operation == 51
        events.append("full")
        return 0

    monkeypatch.setattr(fcntl, "fcntl", full_flush)
    fsio._sync_publication(path)
    assert events == ["dir", "file", "full"]


def test_darwin_full_flush_failure_is_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("Darwin flush uses POSIX fcntl")
    import fcntl

    path = tmp_path / "record.json"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)

    def fail_flush(descriptor: int, operation: int) -> int:
        raise OSError("full flush unsupported")

    monkeypatch.setattr(fcntl, "fcntl", fail_flush)
    with pytest.raises(OSError, match="full flush unsupported"):
        fsio.publish_new(path, b"whole")
    assert path.read_bytes() == b"whole"
    assert list(tmp_path.iterdir()) == [path]
