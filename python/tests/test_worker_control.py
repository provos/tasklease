"""Cross-platform stop files and actual POSIX first/second signal handling."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

import pytest

from tasklease.errors import ConfigError
from tasklease.worker_control import WorkerControl, request_stop, stop_path


def test_stop_filenames_are_portable_and_case_sensitive(tmp_path: Path) -> None:
    labels = ["CON", "Foo", "foo", ".", "..", "x" * 256]
    paths = [stop_path(label, tmp_path) for label in labels]
    assert len(set(paths)) == len(labels)
    for path in paths:
        assert path.parent == tmp_path / "workers"
        assert len(path.name) == 37
        assert path.name.endswith(".stop")
        assert set(path.stem) <= set("0123456789abcdef")


@pytest.mark.parametrize("label", ["", "x" * 257, "a/b", "a\\b", "space here", "é"])
def test_invalid_stop_label(tmp_path: Path, label: str) -> None:
    with pytest.raises(ConfigError):
        request_stop(label, tmp_path)
    assert not (tmp_path / "workers").exists()


def test_stop_request_is_empty_private_and_idempotent(
    tmp_path: Path,
) -> None:
    path = request_stop("CON", tmp_path)
    assert path.read_bytes() == b""
    original = path.stat().st_mtime_ns
    assert request_stop("CON", tmp_path) == path
    assert path.stat().st_mtime_ns == original
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_existing_stop_is_consumed_before_work(tmp_path: Path) -> None:
    path = request_stop("worker", tmp_path)
    with WorkerControl("worker", tmp_path) as control:
        assert control.stop.is_set()
        assert not path.exists()
    with WorkerControl("worker", tmp_path) as next_control:
        assert not next_control.stop.is_set()


def test_stop_file_is_observed_while_worker_is_active(tmp_path: Path) -> None:
    with WorkerControl("worker", tmp_path) as control:
        assert not control.stop.is_set()
        path = request_stop("worker", tmp_path)
        assert control.stop.wait(3)
        assert not path.exists()
    assert control._watcher is not None and not control._watcher.is_alive()


def test_signal_handlers_are_restored_on_exception(tmp_path: Path) -> None:
    numbers = [signal.SIGINT]
    if sys.platform != "win32":
        numbers.extend([signal.SIGHUP, signal.SIGTERM])
    previous = {number: signal.getsignal(number) for number in numbers}
    with pytest.raises(ValueError, match="handler failed"):
        with WorkerControl("worker", tmp_path):
            raise ValueError("handler failed")
    assert {number: signal.getsignal(number) for number in numbers} == previous


@pytest.mark.parametrize("raises", [False, True])
def test_second_signal_calls_cleanup_once_and_always_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    calls: list[str] = []

    def cleanup() -> None:
        calls.append("cleanup")
        if raises:
            raise RuntimeError("cleanup failed")

    def exit_now(code: int) -> NoReturn:
        raise SystemExit(code)

    monkeypatch.setattr("tasklease.worker_control.os._exit", exit_now)
    control = WorkerControl("worker", tmp_path, on_second_signal=cleanup)
    control._signal(signal.SIGINT, None)
    assert control.stop.is_set()
    assert not calls
    for _ in range(2):
        with pytest.raises(SystemExit) as caught:
            control._signal(signal.SIGINT, None)
        assert caught.value.code == 128 + signal.SIGINT
    assert calls == ["cleanup"]


def test_signal_during_cleanup_forces_exit_without_repeating_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    exits: list[int] = []

    def cleanup() -> None:
        calls.append("cleanup")
        control._signal(signal.SIGINT, None)

    def exit_now(code: int) -> NoReturn:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("tasklease.worker_control.os._exit", exit_now)
    control = WorkerControl("worker", tmp_path, on_second_signal=cleanup)
    control.stop.set()
    with pytest.raises(SystemExit):
        control._signal(signal.SIGINT, None)
    assert calls == ["cleanup"]
    assert exits and set(exits) == {128 + signal.SIGINT}


def test_unusable_stop_file_stops_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.unlink
    target = stop_path("worker", tmp_path)

    def denied(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise IsADirectoryError("Cannot consume stop file")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(IsADirectoryError, match="Cannot consume"):
        with WorkerControl("worker", tmp_path) as control:
            assert control.stop.is_set()


def test_creator_handle_permission_race_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = request_stop("worker", tmp_path)
    original = Path.unlink
    attempts = 0

    def denied_once(target: Path, missing_ok: bool = False) -> None:
        nonlocal attempts
        if target == path:
            attempts += 1
            if attempts == 1:
                raise PermissionError("Creator handle is still open")
        original(target, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied_once)
    with WorkerControl("worker", tmp_path) as control:
        assert not control.stop.is_set()
        assert control.stop.wait(3)
    assert attempts == 2
    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="Windows denies unlink of an open file")
def test_consumer_can_remove_marker_before_creator_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = stop_path("worker", tmp_path)
    original_open = os.open
    original_close = os.close
    marker_descriptor: int | None = None
    with WorkerControl("worker", tmp_path) as control:

        def opened(path: Path, flags: int, mode: int = 0o777) -> int:
            nonlocal marker_descriptor
            descriptor = original_open(path, flags, mode)
            if path == target:
                marker_descriptor = descriptor
            return descriptor

        def closed(descriptor: int) -> None:
            if descriptor == marker_descriptor:
                assert control.stop.wait(3)
                assert not target.exists()
            original_close(descriptor)

        monkeypatch.setattr("tasklease.worker_control.os.open", opened)
        monkeypatch.setattr("tasklease.worker_control.os.close", closed)
        assert request_stop("worker", tmp_path) == target
        assert control.stop.is_set()


def _wait_for(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if process.poll() is not None:
            pytest.fail(f"Child exited early: {process.communicate()[1]!r}")
        if time.monotonic() > deadline:
            pytest.fail(f"Child did not publish {path.name}")
        time.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals; Windows uses stop files")
@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("hook", [False, True])
def test_actual_first_signal_finishes_work_second_signal_exits(
    tmp_path: Path, immediate: bool, hook: bool
) -> None:
    script = """
import sys, time
from pathlib import Path
from tasklease.worker_control import WorkerControl
root = Path(sys.argv[1])
def cleanup():
    (root / 'cleanup').touch()
with WorkerControl(
    'child', root, on_second_signal=cleanup if sys.argv[2] == 'hook' else None
) as control:
    (root / 'ready').touch()
    control.stop.wait(30)
    (root / 'stopping').touch()
    while not (root / 'complete').exists():
        time.sleep(.01)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), "hook" if hook else "default"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(tmp_path / "ready", process)
        process.send_signal(signal.SIGTERM)
        _wait_for(tmp_path / "stopping", process)
        assert process.poll() is None
        assert not (tmp_path / "cleanup").exists()
        if immediate:
            process.send_signal(signal.SIGTERM)
            expected = 128 + signal.SIGTERM
        else:
            (tmp_path / "complete").touch()
            expected = 0
        _, errors = process.communicate(timeout=3)
        assert process.returncode == expected, errors
        assert (tmp_path / "cleanup").exists() == (immediate and hook)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
