"""Generic workers use actual CLI parsing, client envelopes and local controls."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import httpx
import pytest

from tasklease.cli import main
from tasklease.client import Client
from tasklease.generated.protocol import Data
from tasklease.worker import EchoHandler
from tasklease.worker_control import request_stop

DOMAIN = "acme/pipeline"
TOPIC = "work/echo"
REPLY = "reply/agent"
TASK_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _unexpected(request: httpx.Request) -> NoReturn:
    pytest.fail(f"Unexpected network request: {request.url.path}")


def _body(request: httpx.Request) -> dict[str, object]:
    value: object = json.loads(request.content)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _owned(data: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "task": {
                "id": TASK_ID,
                "domain": DOMAIN,
                "topic": TOPIC,
                "created_at": 2_000_000_000_000,
                "lease_until": 2_000_000_600_000,
                "owned_count": 1,
                "creator": "agent",
                "owner": {"identity": "machine", "worker": "echo-worker"},
                "owned_at": 2_000_000_000_000,
                "data": data
                if data is not None
                else {
                    "request_id": "request-1",
                    "reply_topic": REPLY,
                    "message": "hello",
                    "unused_large_input": "do not copy this",
                },
            },
        },
    )


def _empty() -> httpx.Response:
    return httpx.Response(
        404, json={"ok": False, "error": {"code": "empty", "message": "Empty"}}
    )


@dataclass
class CLI:
    config: Path
    state: Path
    requests: list[httpx.Request]
    respond: Callable[[httpx.Request], httpx.Response] = _unexpected

    def run(self, args: Sequence[str]) -> int:
        return main(
            [
                "--config",
                str(self.config),
                "worker",
                "--state-dir",
                str(self.state),
                *args,
            ]
        )


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CLI:
    for key in (
        "TL_CONFIG",
        "TL_URL",
        "TL_DOMAIN",
        "TL_AGENT_ID",
        "TL_WORKER",
        "TL_TOKEN",
        "TL_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TL_TOKEN", "tl_worker-test-secret")
    config = tmp_path / "config.toml"
    config.write_text(
        f'url="https://service.example"\ndomain="{DOMAIN}"\n'
        'worker_label="echo-worker"\n'
    )
    command = CLI(config, tmp_path / "state", [])

    def record(request: httpx.Request) -> httpx.Response:
        command.requests.append(request)
        return command.respond(request)

    def factory(url: str, token: str, worker_label: str = "cli") -> Client:
        return Client(url, token, worker_label, transport=httpx.MockTransport(record))

    monkeypatch.setattr("tasklease.cli.common.Client", factory)
    return command


def test_once_polls_immediately_unless_a_wait_is_configured(cli: CLI) -> None:
    # A one-shot step usually runs under an outer machine lock, which an idle
    # long poll would keep from other work.
    cli.respond = lambda request: _empty()
    assert cli.run(["--topic", TOPIC, "--once"]) == 0
    assert "wait_seconds" not in _body(cli.requests[0])


def test_echo_once_uses_capacity_and_small_correlated_reply(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/own":
            assert _body(request) == {
                "domain": DOMAIN,
                "topic": TOPIC,
                "hold_seconds": 600,
            }
            return _owned()
        assert request.url.path == "/v1/consume"
        assert _body(request) == {
            "domain": DOMAIN,
            "topic": TOPIC,
            "id": TASK_ID,
            "tasks": [
                {
                    "topic": REPLY,
                    "data": {
                        "request_id": "request-1",
                        "status": "ok",
                        "message": "hello",
                    },
                }
            ],
        }
        return httpx.Response(200, json={"ok": True, "ids": [TASK_ID]})

    cli.respond = response
    assert cli.run(["--topic", TOPIC, "--once"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "worked"}
    assert all(request.headers["X-Worker"] == "echo-worker" for request in cli.requests)
    assert "tl_worker-test-secret" not in captured.out + captured.err


def test_empty_once_returns_without_idle_sleep(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.respond = lambda request: _empty()
    assert cli.run(["--topic", TOPIC, "--once", "--poll-interval", "1h"]) == 0
    assert len(cli.requests) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "empty"}


def test_settings_and_cli_overrides(cli: CLI) -> None:
    cli.config.write_text(
        f'url="https://service.example"\ndomain="{DOMAIN}"\n'
        '[worker]\ntopic="work/config"\nhandler="invalid-factory"\n'
        'lease="20m"\nrenew_every=120\nmax_owned=4\npoll_interval="1m"\n'
    )
    cli.respond = lambda request: _empty()
    assert (
        cli.run(["--handler", "echo", "--topic", TOPIC, "--lease", "10m", "--once"])
        == 0
    )
    assert _body(cli.requests[0]) == {
        "domain": DOMAIN,
        "topic": TOPIC,
        "hold_seconds": 600,
    }


def test_config_handler_and_topic_used(cli: CLI) -> None:
    with cli.config.open("a") as config:
        config.write('[worker]\ntopic="work/config"\nhandler="echo"\nlease=700\n')
    cli.respond = lambda request: _empty()
    assert cli.run(["--once"]) == 0
    assert _body(cli.requests[0])["topic"] == "work/config"
    assert _body(cli.requests[0])["hold_seconds"] == 700


def test_standard_domain_environment_and_cli_precedence(
    cli: CLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TL_DOMAIN", "from/environment")
    cli.respond = lambda request: _empty()
    assert cli.run(["--topic", TOPIC, "--once"]) == 0
    assert _body(cli.requests[-1])["domain"] == "from/environment"
    assert cli.run(["--topic", TOPIC, "--domain", "from/flag", "--once"]) == 0
    assert _body(cli.requests[-1])["domain"] == "from/flag"


@pytest.mark.parametrize(
    "topic", ["work/error", "error/error", "invalid topic", "a/", "x" * 129]
)
def test_invalid_or_error_topics_refused_before_client(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, topic: str
) -> None:
    monkeypatch.delenv("TL_TOKEN")
    assert cli.run(["--topic", topic, "--once"]) == 1
    assert cli.requests == []


@pytest.mark.parametrize(
    "setting",
    [
        'lease="600"',
        "lease=false",
        "lease=-1",
        "lease=86401",
        "renew_every=600",
        "max_owned=0",
        "max_owned=true",
        'poll_interval="0s"',
        'poll_interval="' + "9" * 400 + 's"',
        "topic=false",
    ],
)
def test_invalid_settings_refused_before_client(cli: CLI, setting: str) -> None:
    with cli.config.open("a") as config:
        config.write(
            f'[worker]\ntopic="{TOPIC}"\n{setting}\n'
            if not setting.startswith("topic=")
            else f"[worker]\n{setting}\n"
        )
    assert cli.run(["--once"]) == 1
    assert cli.requests == []


@pytest.mark.parametrize("handler", ["missing", "absent_module:create", "sys:version"])
def test_invalid_handler_refused_before_client(cli: CLI, handler: str) -> None:
    assert cli.run(["--topic", TOPIC, "--handler", handler, "--once"]) == 1
    assert cli.requests == []


def test_local_plugin_factory(
    cli: CLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "local_worker_plugin.py").write_text(
        "from tasklease.worker import EchoHandler\n"
        "def create():\n    return EchoHandler()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    cli.respond = lambda request: _empty()
    assert (
        cli.run(["--topic", TOPIC, "--handler", "local_worker_plugin:create", "--once"])
        == 0
    )
    assert len(cli.requests) == 1


def test_plugin_interface_is_checked(
    cli: CLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad_worker_plugin.py").write_text(
        "def create():\n    return object()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert (
        cli.run(["--topic", TOPIC, "--handler", "bad_worker_plugin:create", "--once"])
        == 1
    )
    assert cli.requests == []


def test_stop_command_is_offline_and_preserves_case(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TL_TOKEN")
    cli.config.write_text('url=false\ndomain=false\nworker_label="CON"\nworker=false\n')
    assert cli.run(["stop"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is True and response["worker"] == "CON"
    assert Path(response["stop_file"]).parent == cli.state / "workers"
    assert cli.requests == []
    assert cli.run(["stop", "--worker", "Foo"]) == 0
    other = json.loads(capsys.readouterr().out)
    assert other["stop_file"] != response["stop_file"]


def test_existing_stop_skips_polling_and_credentials(
    cli: CLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TL_TOKEN")
    request_stop("echo-worker", cli.state)
    assert cli.run(["--topic", TOPIC, "--once"]) == 0
    assert cli.requests == []


def test_stop_file_wakes_idle_worker(cli: CLI) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        request_stop("echo-worker", cli.state)
        return _empty()

    cli.respond = response
    assert cli.run(["--topic", TOPIC, "--poll-interval", "1h"]) == 0
    assert len(cli.requests) == 1
    assert not any(thread.name == "tasklease-stop" for thread in threading.enumerate())


def test_active_handler_finishes_before_file_stop_exits(
    cli: CLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    finish = threading.Event()
    stop_written = threading.Event()
    observed = threading.Event()

    class SlowEcho(EchoHandler):
        def run(self, request: str, lost: threading.Event) -> Data:
            started.set()
            assert finish.wait(3)
            assert not lost.is_set()
            return super().run(request, lost)

    monkeypatch.setattr("tasklease.cli.worker._handler", lambda name: SlowEcho())

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/own":
            return _owned()
        assert request.url.path == "/v1/consume"
        assert stop_written.is_set()
        return httpx.Response(200, json={"ok": True, "ids": [TASK_ID]})

    def stop_active() -> None:
        if not started.wait(3):
            finish.set()
            return
        path = request_stop("echo-worker", cli.state)
        stop_written.set()
        # The watcher consumes the request while handler.run remains active.
        deadline = threading.Event()
        for _ in range(300):
            if not path.exists():
                observed.set()
                break
            deadline.wait(0.01)
        finish.set()

    cli.respond = response
    stopper = threading.Thread(target=stop_active)
    stopper.start()
    try:
        assert cli.run(["--topic", TOPIC]) == 0
    finally:
        finish.set()
        stopper.join(timeout=3)
    assert not stopper.is_alive()
    assert observed.is_set()
    assert [request.url.path for request in cli.requests] == ["/v1/own", "/v1/consume"]


def test_wait_seconds_can_be_disabled_and_configured(cli: CLI) -> None:
    cli.respond = lambda request: _empty()
    assert cli.run(["--topic", TOPIC, "--once", "--wait-seconds", "off"]) == 0
    assert "wait_seconds" not in _body(cli.requests[0])
    assert cli.run(["--topic", TOPIC, "--once", "--wait-seconds", "12"]) == 0
    assert _body(cli.requests[1])["wait_seconds"] == 12


def test_wait_seconds_setting_and_validation(cli: CLI) -> None:
    with cli.config.open("a") as config:
        config.write("[worker]\nwait_seconds=5\n")
    cli.respond = lambda request: _empty()
    assert cli.run(["--topic", TOPIC, "--once"]) == 0
    assert _body(cli.requests[0])["wait_seconds"] == 5
    for value in ("-1", "31", "1.5", "\u00b3"):
        assert cli.run(["--topic", TOPIC, "--once", "--wait-seconds", value]) == 1


def test_worker_names_an_old_service_and_the_opt_out(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/version":
            return httpx.Response(
                200, json={"service_version": "0.3.0", "protocol": "v1"}
            )
        if "wait_seconds" in _body(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error": {"code": "invalid", "message": "Invalid request body"},
                },
            )
        return _empty()

    cli.respond = respond
    assert cli.run(["--topic", TOPIC]) == 1
    error = capsys.readouterr().err
    assert "wait_seconds" in error and "version 0.3.0" in error
    assert "[worker] wait_seconds" in error
    # The documented opt-out restores the old request.
    assert cli.run(["--topic", TOPIC, "--once", "--wait-seconds", "0"]) == 0
