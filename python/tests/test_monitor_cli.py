"""Monitor CLI lifetime and connection configuration without opening a browser."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest

from tasklease.cli import main
from tasklease.client import Client


@pytest.mark.parametrize("open_browser", [False, True])
def test_monitor_prints_loopback_url_and_closes_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    open_browser: bool,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('url="https://file.example"\nworker_label="file-worker"\n')
    monkeypatch.setenv("TL_TOKEN", "tl_monitor-secret")
    events: list[str] = []
    opened: list[str] = []

    class Server:
        url = "http://127.0.0.1:54321"

        def __init__(self, api: Client, *, port: int, state_root: Path | None) -> None:
            assert port == 1234 and state_root == tmp_path / "data"
            assert api._base_url == "https://override.example"
            assert api._worker_label == "monitor-test"
            events.append("created")

        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            events.append("closed")

        def serve_forever(self, *, poll_interval: float) -> None:
            assert poll_interval > 0
            events.append("serving")
            raise KeyboardInterrupt

    def factory(url: str, token: str, worker_label: str = "cli") -> Client:
        return Client(
            url,
            token,
            worker_label,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )

    monkeypatch.setattr("tasklease.cli.common.Client", factory)
    monkeypatch.setattr("tasklease.cli.monitor.MonitorServer", Server)
    monkeypatch.setattr("tasklease.cli.monitor.webbrowser.open", opened.append)
    args = [
        "--config",
        str(config),
        "--url",
        "https://override.example",
        "--worker",
        "monitor-test",
        "monitor",
        "--port",
        "1234",
        "--state-dir",
        str(tmp_path / "data"),
    ]
    if not open_browser:
        args.append("--no-browser")
    assert main(args) == 130
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"url": Server.url}
    assert "Ctrl-C" in captured.err and "Interrupted" in captured.err
    assert "tl_monitor-secret" not in captured.out + captured.err
    assert events == ["created", "serving", "closed"]
    assert opened == ([Server.url] if open_browser else [])


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_invalid_port_precedes_connection_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], port: str
) -> None:
    assert main(["monitor", "--config", str(tmp_path / "missing"), "--port", port]) == 1
    assert "port" in capsys.readouterr().err


def test_actual_headless_cli_reports_free_port_before_exiting(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        TL_TOKEN="tl_monitor-process-fake-token", TL_URL="https://service.example"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tasklease",
            "monitor",
            "--no-browser",
            "--config",
            str(tmp_path / "missing"),
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    output = process.stdout
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(output.readline()), daemon=True)
    reader.start()
    try:
        try:
            line = lines.get(timeout=5)
        except queue.Empty:
            pytest.fail("The running monitor did not flush its local URL")
        assert process.poll() is None
        url = json.loads(line)["url"]
        assert url.startswith("http://127.0.0.1:")
        with httpx.Client(trust_env=False) as browser:
            response = browser.get(url)
        assert response.status_code == 200
        assert "tl_monitor-process-fake-token" not in response.text
    finally:
        process.terminate()
        process.communicate(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
