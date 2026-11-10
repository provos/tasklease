"""Local worker stop requests, independent of service credentials."""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType, TracebackType
from typing import Self

from tasklease.errors import ConfigError
from tasklease.fsio import ensure_directory
from tasklease.names import is_worker_label
from tasklease.store import default_root, hash_id


def stop_path(worker_label: str, root: Path | None = None) -> Path:
    """Use portable filenames while preserving the label's case-sensitive identity."""
    if not is_worker_label(worker_label):
        raise ConfigError(
            "worker label must contain 1 to 256 letters, digits, ., _ or -"
        )
    base = default_root() if root is None else root
    return base.expanduser().absolute() / "workers" / f"{hash_id(worker_label)}.stop"


def request_stop(worker_label: str, root: Path | None = None) -> Path:
    """Create a transient marker; no credential or process lookup is needed.

    Existence is the complete signal. Unlike durable task records, a marker
    may be consumed immediately, even before this call returns. It carries no
    payload and needs no post-publication flush: power loss also stops workers.
    """
    path = stop_path(worker_label, root)
    ensure_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path
    os.close(descriptor)
    return path


class WorkerControl:
    """Finish active work on the first stop request; a second signal exits now.

    Stop files are consumed once, including a request already present at
    startup. A unique worker label identifies one running worker on a machine.
    Signal handlers are installed only on the main thread and restored on exit.

    ``on_second_signal`` runs once before forced exit, on the signal-handling
    thread. Use it for fast, nonblocking cleanup such as terminating a child
    process. It must not wait for work to finish. Exceptions still force exit;
    another signal during cleanup exits immediately without calling it again.
    """

    def __init__(
        self,
        worker_label: str,
        root: Path | None = None,
        *,
        on_second_signal: Callable[[], None] | None = None,
    ) -> None:
        self.path = stop_path(worker_label, root)
        self.stop = threading.Event()
        self._closed = threading.Event()
        self._signals: dict[int, signal._HANDLER] = {}
        self._watcher: threading.Thread | None = None
        self._error: OSError | None = None
        self._on_second_signal = on_second_signal
        self._forcing_exit = False

    def _signal(self, number: int, frame: FrameType | None) -> None:
        if self.stop.is_set():
            # User explicitly requested immediate termination. In particular,
            # do not wait for an uncooperative handler or networking thread.
            try:
                if not self._forcing_exit:
                    self._forcing_exit = True
                    if self._on_second_signal is not None:
                        self._on_second_signal()
            finally:
                os._exit(128 + number)
        self.stop.set()

    def _consume_stop(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except PermissionError:
            # Windows can deny removal until the creator closes its handle.
            # The next pass retries; no signal has been consumed yet.
            return
        except OSError as error:
            self._error = error
        self.stop.set()

    def _watch(self) -> None:
        while not self._closed.wait(0.1):
            self._consume_stop()
            if self.stop.is_set():
                return

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            numbers = [signal.SIGINT]
            if sys.platform != "win32":
                numbers.extend([signal.SIGHUP, signal.SIGTERM])
            for number in numbers:
                self._signals[number] = signal.signal(number, self._signal)
        self._consume_stop()
        self._watcher = threading.Thread(
            target=self._watch, name="tasklease-stop", daemon=True
        )
        self._watcher.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed.set()
        if self._watcher is not None:
            self._watcher.join()
        for number, handler in self._signals.items():
            signal.signal(number, handler)
        if exc_type is None and self._error is not None:
            raise self._error
