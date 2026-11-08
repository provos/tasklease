"""Requester commands through the real HTTP client and durable local store."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NoReturn, cast

import httpx
import pytest

from tasklease.cli import main
from tasklease.client import Client
from tasklease.store import Store

DOMAIN = "acme/project"
AGENT = "agent-a"
TOPIC = "work/linux"
REPLY_TOPIC = "reply/agent-a"
TASK_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"
REPLY_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAW"
OTHER_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAX"


def hashed(value: str) -> str:
    return sha256(value.encode()).hexdigest()[:32]


def body(request: httpx.Request) -> dict[str, object]:
    decoded: object = json.loads(request.content)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def operation(request: httpx.Request) -> str:
    return request.url.path.rsplit("/", 1)[-1]


def unexpected(request: httpx.Request) -> NoReturn:
    pytest.fail(f"Unexpected {operation(request)} request")


def empty() -> httpx.Response:
    return httpx.Response(
        404, json={"ok": False, "error": {"code": "empty", "message": "No replies"}}
    )


def reply(
    data: dict[str, object],
    *,
    task_id: str = REPLY_ID,
    topic: str = REPLY_TOPIC,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "task": {
                "id": task_id,
                "domain": DOMAIN,
                "topic": topic,
                "created_at": 2_000_000_000_000,
                "lease_until": 2_000_000_060_000,
                "owned_count": 1,
                "creator": "linux-1",
                "owner": {"identity": AGENT, "worker": "cli"},
                "owned_at": 2_000_000_000_000,
                "data": data,
            },
        },
    )


def request_args(
    request_id: str | None = "request-1", *, data: str = '{"input":"hello"}'
) -> list[str]:
    args = [
        "request",
        "--domain",
        DOMAIN,
        "--topic",
        TOPIC,
        "--reply-topic",
        REPLY_TOPIC,
        "--data",
        data,
    ]
    if request_id is not None:
        args.extend(["--request-id", request_id])
    return args


@dataclass
class CLI:
    config: Path
    root: Path
    requests: list[httpx.Request]
    respond: Callable[[httpx.Request], httpx.Response] = unexpected

    def run(self, args: Sequence[str]) -> int:
        return main(["--config", str(self.config), *args])

    def record_path(self, request_id: str = "request-1") -> Path:
        return self.root / "requests" / hashed(AGENT) / f"{hashed(request_id)}.json"


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CLI:
    for name in (
        "TL_CONFIG",
        "TL_URL",
        "TL_DOMAIN",
        "TL_AGENT_ID",
        "TL_WORKER",
        "TL_TOKEN",
        "TL_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TL_TOKEN", "tl_test-token")
    config = tmp_path / "config.toml"
    config.write_text(f'url="https://file.example"\nagent_id="{AGENT}"\n')
    command = CLI(config, tmp_path / "data", [])

    def data_path(appname: str) -> Path:
        assert appname == "tasklease"
        return command.root

    def record(request: httpx.Request) -> httpx.Response:
        command.requests.append(request)
        return command.respond(request)

    def factory(url: str, token: str, worker_label: str = "cli") -> Client:
        return Client(url, token, worker_label, transport=httpx.MockTransport(record))

    monkeypatch.setattr("tasklease.store.user_data_path", data_path)
    monkeypatch.setattr("tasklease.cli.common.Client", factory)
    return command


def test_request_publishes_complete_record_before_create_and_preserves_global_flags(
    cli: CLI, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    data_file = tmp_path / "request.json"
    data_file.write_text(
        '{"input":{"items":[1,null,true]},"text":"héllo"}', encoding="utf-8"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert operation(request) == "create"
        saved = cast(
            dict[str, object], json.loads(cli.record_path().read_text(encoding="utf-8"))
        )
        assert saved["data"] == body(request)["data"]
        assert saved["domain"] == DOMAIN
        assert saved["topic"] == TOPIC
        assert saved["reply_topic"] == REPLY_TOPIC
        assert not cli.record_path().with_suffix(".task.json").exists()
        return httpx.Response(200, json={"ok": True, "id": TASK_ID})

    cli.respond = respond
    assert (
        cli.run(
            [
                "--url",
                "https://global.example",
                "--worker",
                "requester-7",
                *request_args(data=f"@{data_file}"),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "request_id": "request-1",
        "task_id": TASK_ID,
        "state": "pending",
    }
    assert cli.requests[0].url.host == "global.example"
    assert cli.requests[0].headers["x-worker"] == "requester-7"
    assert body(cli.requests[0]) == {
        "domain": DOMAIN,
        "topic": TOPIC,
        "data": {
            "input": {"items": [1, None, True]},
            "text": "héllo",
            "request_id": "request-1",
            "reply_topic": REPLY_TOPIC,
        },
    }


def test_non_utf8_data_file_is_rejected_before_publication(
    cli: CLI, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    data_file = tmp_path / "request-cp1252.json"
    data_file.write_bytes(b'{"message":"h\xe9llo"}')
    assert cli.run(request_args(data=f"@{data_file}")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cannot read --data file" in captured.err
    assert cli.requests == []
    assert not cli.record_path().exists()


def test_request_wait_drains_unmatched_replies_and_wait_survives_restart(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = [
        reply({"request_id": 12, "problem": "no correlation"}, task_id=OTHER_ID),
        reply({"request_id": "request-1", "answer": "done"}),
        empty(),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "own":
            assert body(request) == {
                "domain": DOMAIN,
                "topic": REPLY_TOPIC,
                "hold_seconds": 60,
            }
            return replies.pop(0)
        assert operation(request) == "delete"
        saved = Store(AGENT).inbox()
        assert any(record.task_id == body(request)["id"] for record in saved)
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    assert cli.run([*request_args(), "--wait", "--timeout", "1s"]) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered == {
        "request_id": "request-1",
        "data": {"request_id": "request-1", "answer": "done"},
        "creator": "linux-1",
    }
    assert all(response.status_code == 404 for response in replies)
    assert {
        body(request)["id"]
        for request in cli.requests
        if operation(request) == "delete"
    } == {REPLY_ID, OTHER_ID}
    cli.requests.clear()
    cli.respond = unexpected
    assert cli.run(["wait", "--request-id", "request-1"]) == 0
    assert json.loads(capsys.readouterr().out) == delivered
    assert cli.requests == []
    assert cli.record_path().exists()
    assert cli.run(["inbox"]) == 0
    inbox = cast(dict[str, object], json.loads(capsys.readouterr().out))
    records = inbox["replies"]
    assert isinstance(records, list)
    assert len(records) == 2
    assert any(record["data"]["request_id"] == 12 for record in records)


@pytest.mark.parametrize("failure", ["transport", "protocol", "interrupt"])
@pytest.mark.parametrize("resend", [False, True])
def test_wait_failure_prints_saved_generated_id_and_recovery(
    cli: CLI, capsys: pytest.CaptureFixture[str], failure: str, resend: bool
) -> None:
    request_id: str | None = None
    if resend:

        def lose_create(request: httpx.Request) -> NoReturn:
            raise httpx.ReadError("Create response lost", request=request)

        cli.respond = lose_create
        assert cli.run(request_args(None)) == 1
        request_id = json.loads(capsys.readouterr().out)["request_id"]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        assert operation(request) == "own"
        if failure == "transport":
            raise httpx.ReadError("Wait failed", request=request)
        if failure == "interrupt":
            raise KeyboardInterrupt
        return httpx.Response(200, content=b"invalid JSON")

    cli.respond = respond
    args = ["request", "--resend", request_id] if request_id else request_args(None)
    assert cli.run([*args, "--wait"]) == (130 if failure == "interrupt" else 1)
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    saved = Store(AGENT).requests()
    assert len(saved) == 1 and saved[0].state == "pending"
    assert status == {
        "request_id": saved[0].request_id,
        "task_id": TASK_ID,
        "state": "pending",
    }
    assert f"tl wait --request-id {saved[0].request_id}" in captured.err
    assert "Submission outcome is unknown" not in captured.err
    assert "--resend" not in captured.err


@pytest.mark.parametrize("interrupt", [False, True])
def test_uncertain_create_prints_generated_id_and_resend_uses_exact_saved_payload(
    cli: CLI, capsys: pytest.CaptureFixture[str], interrupt: bool
) -> None:
    def lose_response(request: httpx.Request) -> NoReturn:
        assert operation(request) == "create"
        if interrupt:
            raise KeyboardInterrupt
        raise httpx.ReadTimeout("reply lost", request=request)

    cli.respond = lose_response
    assert cli.run(request_args(None)) == (130 if interrupt else 1)
    captured = capsys.readouterr()
    unknown = cast(dict[str, object], json.loads(captured.out))
    request_id = unknown["request_id"]
    assert isinstance(request_id, str)
    assert len(request_id) == 32
    assert unknown == {
        "request_id": request_id,
        "task_id": None,
        "state": "submission_unknown",
    }
    assert "tl wait" in captured.err
    assert "--resend" in captured.err
    assert "Cancellation is unavailable" in captured.err
    saved = cli.record_path(request_id).read_bytes()
    original_wire = cli.requests[0].content

    cli.requests.clear()
    cli.respond = unexpected
    assert cli.run(["cancel", "--request-id", request_id]) == 1
    assert "no saved task ID" in capsys.readouterr().err
    assert cli.requests == []

    cli.config.write_text(
        f'url="https://new.example"\ndomain="other"\nagent_id="{AGENT}"'
    )
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(["request", "--resend", request_id]) == 0
    assert cli.requests[0].content == original_wire
    assert cli.record_path(request_id).read_bytes() == saved
    assert json.loads(capsys.readouterr().out)["state"] == "pending"
    cli.requests.clear()
    assert cli.run(["request", "--resend", request_id]) == 1
    assert "pending" in capsys.readouterr().err.lower()
    assert cli.requests == []


@pytest.mark.parametrize("not_found", [False, True])
def test_cancel_uses_saved_task_and_retains_record_when_task_was_taken_or_removed(
    cli: CLI, capsys: pytest.CaptureFixture[str], not_found: bool
) -> None:
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 0
    capsys.readouterr()
    cli.requests.clear()
    cli.config.write_text(
        f'url="https://changed.example"\ndomain="other"\nagent_id="{AGENT}"'
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert operation(request) == "delete"
        assert body(request) == {"domain": DOMAIN, "topic": TOPIC, "id": TASK_ID}
        if not_found:
            return httpx.Response(
                404,
                json={
                    "ok": False,
                    "error": {"code": "not_found", "message": "Task absent"},
                },
            )
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    assert cli.run(["cancel", "--request-id", "request-1"]) == int(not_found)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["cancelled"] is not not_found
    assert cli.record_path().exists() is not_found
    assert cli.record_path().with_suffix(".task.json").exists() is not_found
    if not_found:
        assert "taken or removed" in captured.err
    else:
        assert captured.err == ""


@pytest.mark.parametrize(
    "data",
    [
        "[]",
        "null",
        "{",
        '{"x":NaN}',
        '{"request_id":"other"}',
        '{"reply_topic":"other"}',
    ],
)
def test_invalid_data_or_correlation_conflicts_never_create(
    cli: CLI, capsys: pytest.CaptureFixture[str], data: str
) -> None:
    assert cli.run(request_args(data=data)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert cli.requests == []
    assert not cli.record_path().exists()


@pytest.mark.parametrize("timeout", ["0s", "1", "1.5h", "-2d"])
def test_invalid_timeout_never_creates_or_publishes(
    cli: CLI, capsys: pytest.CaptureFixture[str], timeout: str
) -> None:
    assert cli.run([*request_args(), f"--timeout={timeout}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Timeout must be positive" in captured.err
    assert cli.requests == []
    assert not cli.record_path().exists()


def test_request_agent_environment_and_domain_configuration(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.config.write_text(f'url="https://file.example"\ndomain="{DOMAIN}"\n')
    args = request_args()
    del args[1:3]
    assert cli.run(args) == 1
    assert "TL_AGENT_ID" in capsys.readouterr().err
    monkeypatch.setenv("TL_AGENT_ID", AGENT)
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(args) == 0
    assert body(cli.requests[0])["domain"] == DOMAIN
    assert json.loads(capsys.readouterr().out)["state"] == "pending"


def test_empty_supplied_request_id_is_rejected_without_replacement(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(request_args("")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "request_id" in captured.err
    assert cli.requests == []
    assert Store(AGENT).requests() == []


def test_resend_rejects_payload_options_without_network(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["request", "--resend", "saved", "--data", "{} "]) == 1
    assert "uses the saved request" in capsys.readouterr().err
    assert cli.requests == []


@pytest.mark.parametrize("unknown", [False, True])
def test_timeout_preserves_request_for_later_wait_without_real_sleeps(
    cli: CLI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unknown: bool,
) -> None:
    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        assert 0 < seconds <= 1
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr("tasklease.store.time.monotonic", monotonic)
    monkeypatch.setattr("tasklease.store.time.sleep", sleep)

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            if unknown:
                raise httpx.ReadTimeout("lost Create response", request=request)
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        assert operation(request) == "own"
        assert body(request)["domain"] == DOMAIN
        assert body(request)["topic"] == REPLY_TOPIC
        return empty()

    cli.respond = respond
    if unknown:
        assert cli.run(request_args()) == 1
        capsys.readouterr()
        cli.config.write_text(
            f'url="https://file.example"\ndomain="changed"\nagent_id="{AGENT}"'
        )
        args = ["wait", "--request-id", "request-1", "--timeout", "1s"]
    else:
        args = [*request_args(), "--wait", "--timeout", "1s"]
    assert cli.run(args) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"request_id": "request-1", "timed_out": True}
    assert "request-1" in captured.err
    assert sleeps
    record = Store(AGENT).request("request-1")
    assert record is not None
    assert record.state == ("submission_unknown" if unknown else "pending")
    assert all(operation(request) in {"create", "own"} for request in cli.requests)

    replies = [reply({"request_id": "request-1", "answer": "later"}), empty()]

    def later(request: httpx.Request) -> httpx.Response:
        if operation(request) == "own":
            return replies.pop(0)
        assert operation(request) == "delete"
        return httpx.Response(200, json={"ok": True})

    cli.requests.clear()
    cli.respond = later
    assert cli.run(["wait", "--request-id", "request-1", "--timeout", "1s"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["answer"] == "later"
    assert all(operation(request) != "create" for request in cli.requests)
    assert cli.record_path().exists()


def test_all_agent_inbox_and_prune_need_no_configuration_or_credentials(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 2_000_000_000.0 - 8 * 86400
    monkeypatch.setattr("tasklease.store.time.time", lambda: now)

    def populate(agent_id: str, request_id: str, *, unmatched: bool = False) -> None:
        topic = f"reply/{agent_id}"
        replies = [reply({"request_id": request_id, "answer": "ok"}, topic=topic)]
        if unmatched:
            replies.append(
                reply({"problem": "missing ID"}, task_id=OTHER_ID, topic=topic)
            )
        replies.append(empty())

        def respond(request: httpx.Request) -> httpx.Response:
            if operation(request) == "create":
                return httpx.Response(200, json={"ok": True, "id": TASK_ID})
            if operation(request) == "own":
                return replies.pop(0)
            assert operation(request) == "delete"
            return httpx.Response(200, json={"ok": True})

        with Client(
            "https://service.example",
            "tl_token",
            transport=httpx.MockTransport(respond),
        ) as api:
            store = Store(agent_id)
            store.submit(api, DOMAIN, TOPIC, topic, {"input": "hello"}, request_id)
            store.drain(api, DOMAIN, topic)
        assert not replies

    populate(AGENT, "old", unmatched=True)
    now += 8 * 86400
    populate("agent-b", "fresh")
    cli.config.write_text("This is not valid TOML =")
    monkeypatch.delenv("TL_TOKEN")
    monkeypatch.setenv("TL_AGENT_ID", "invalid/agent")
    monkeypatch.setenv("TL_URL", "invalid URL")
    assert cli.run(["inbox", "--all"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    records = json.loads(captured.out)["replies"]
    assert len(records) == 3
    assert {record["agent_id"] for record in records} == {AGENT, "agent-b"}
    assert any(record["request_id"] is None for record in records)
    assert cli.requests == []

    assert cli.run(["inbox", "--all", "--prune"]) == 0
    pruned = json.loads(capsys.readouterr().out)
    assert pruned["pruned"] >= 4
    assert len(pruned["replies"]) == 1
    assert pruned["replies"][0]["agent_id"] == "agent-b"
    assert Store(AGENT).requests() == []
    assert Store(AGENT).inbox() == []
    assert Store("agent-b").request("fresh") is not None
    assert cli.requests == []


def test_corrupt_existing_reply_is_never_acknowledged(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 0
    capsys.readouterr()
    with Client(
        "https://service.example",
        "tl_token",
        transport=httpx.MockTransport(lambda request: empty()),
    ) as api:
        Store(AGENT).drain(api, DOMAIN, REPLY_TOPIC)
    corrupt = cli.root / "replies" / hashed(AGENT) / f"{hashed('other')}.json"
    corrupt.write_text("{")
    cli.requests.clear()
    cli.respond = lambda request: reply({"request_id": "other", "answer": "new"})
    assert cli.run(["wait", "--request-id", "request-1", "--timeout", "1s"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "local requester state" in captured.err
    assert [operation(request) for request in cli.requests] == ["own"]
    assert corrupt.read_text() == "{"
    cli.requests.clear()
    assert cli.run(["inbox"]) == 1
    assert capsys.readouterr().out == ""
    assert cli.requests == []
