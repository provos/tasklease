"""Requester commands through the real HTTP client and durable local store."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, NoReturn, cast

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
    origin_id: str = TASK_ID,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "task": {
                "id": task_id,
                "origin_id": origin_id,
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


def request_args(*, data: str = '{"input":"hello"}') -> list[str]:
    return [
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


@dataclass
class CLI:
    config: Path
    root: Path
    requests: list[httpx.Request]
    respond: Callable[[httpx.Request], httpx.Response] = unexpected

    def run(self, args: Sequence[str]) -> int:
        return main(["--config", str(self.config), *args])

    def record_path(self, origin_id: str = TASK_ID) -> Path:
        return self.root / "requests" / hashed(AGENT) / f"{origin_id}.json"


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


def test_request_creates_before_saving_and_preserves_global_flags(
    cli: CLI, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    data_file = tmp_path / "request.json"
    data_file.write_text(
        '{"input":{"items":[1,null,true]},"text":"héllo"}', encoding="utf-8"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert operation(request) == "create"
        assert not cli.record_path().exists()
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
    assert json.loads(captured.out) == {"origin_id": TASK_ID, "state": "pending"}
    assert cli.requests[0].url.host == "global.example"
    assert cli.requests[0].headers["x-worker"] == "requester-7"
    assert body(cli.requests[0]) == {
        "domain": DOMAIN,
        "topic": TOPIC,
        "data": {
            "input": {"items": [1, None, True]},
            "text": "héllo",
            "reply_topic": REPLY_TOPIC,
        },
    }
    assert (
        json.loads(cli.record_path().read_text())["data"]
        == body(cli.requests[0])["data"]
    )


def test_wait_drains_other_origins_and_reads_its_answer_after_restart(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = [
        reply(
            {"request_id": TASK_ID, "result": "unsolicited"},
            task_id=OTHER_ID,
            origin_id=OTHER_ID,
        ),
        reply({"answer": "done"}),
        empty(),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "own":
            return replies.pop(0)
        assert operation(request) == "delete"
        assert any(
            record.task_id == body(request)["id"] for record in Store(AGENT).inbox()
        )
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    assert cli.run([*request_args(), "--wait", "--timeout", "1s"]) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered == {
        "origin_id": TASK_ID,
        "data": {"answer": "done"},
        "creator": "linux-1",
    }
    assert {
        body(request)["id"]
        for request in cli.requests
        if operation(request) == "delete"
    } == {REPLY_ID, OTHER_ID}
    cli.requests.clear()
    cli.respond = unexpected
    assert cli.run(["wait", "--origin-id", TASK_ID]) == 0
    assert json.loads(capsys.readouterr().out) == delivered
    assert cli.requests == []
    assert cli.run(["inbox"]) == 0
    records = json.loads(capsys.readouterr().out)["replies"]
    assert len(records) == 2
    assert {record["origin_id"]: record["matched"] for record in records} == {
        TASK_ID: True,
        OTHER_ID: False,
    }


@pytest.mark.parametrize("failure", ["old_service", "protocol", "interrupt"])
def test_wait_failure_reports_the_saved_origin_and_recovery(
    cli: CLI, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "version":
            return httpx.Response(
                200, json={"service_version": "0.3.0", "protocol": "v1"}
            )
        assert operation(request) == "own"
        if failure == "old_service":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error": {"code": "invalid", "message": "Invalid request body"},
                },
            )
        if failure == "interrupt":
            raise KeyboardInterrupt
        return httpx.Response(200, content=b"invalid JSON")

    cli.respond = respond
    assert cli.run([*request_args(), "--wait"]) == (
        130 if failure == "interrupt" else 1
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"origin_id": TASK_ID, "state": "pending"}
    assert f"tl wait --origin-id {TASK_ID}" in captured.err
    assert Store(AGENT).request(TASK_ID) is not None


@pytest.mark.parametrize("interrupt", [False, True])
def test_uncertain_create_has_no_invented_origin_or_local_record(
    cli: CLI, capsys: pytest.CaptureFixture[str], interrupt: bool
) -> None:
    def lose(request: httpx.Request) -> NoReturn:
        if interrupt:
            raise KeyboardInterrupt
        raise httpx.ReadTimeout("reply lost", request=request)

    cli.respond = lose
    assert cli.run(request_args()) == (130 if interrupt else 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--resend" not in captured.err
    assert Store(AGENT).requests() == []
    assert len(cli.requests) == 1


def test_local_save_failure_reports_known_origin_and_stateless_cancel_hint(
    cli: CLI, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tasklease import fsio

    original = fsio.publish_new

    def publish(path: Path, data: bytes) -> Literal["created", "exists"]:
        if path == cli.record_path():
            raise OSError("private disk diagnostic")
        return original(path, data)

    monkeypatch.setattr(fsio, "publish_new", publish)
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert TASK_ID in captured.err
    assert (
        f"tl cancel --domain {DOMAIN} --topic {TOPIC} --origin-id {TASK_ID}"
        in captured.err
    )
    assert "private disk diagnostic" not in captured.err


def test_reply_read_failure_reports_wait_recovery_instead_of_failed_save(
    cli: CLI, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tasklease import fsio

    original = fsio.read_json
    reply_path = cli.root / "replies" / hashed(AGENT) / f"{TASK_ID}.json"

    def read(path: Path) -> object:
        if path == reply_path:
            raise PermissionError("private filesystem diagnostic")
        return original(path)

    monkeypatch.setattr(fsio, "read_json", read)
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 1
    captured = capsys.readouterr()
    assert "was saved" in captured.err
    assert f"tl wait --origin-id {TASK_ID}" in captured.err
    assert "tl cancel" not in captured.err and "private" not in captured.err
    assert cli.record_path().exists()
    assert len(cli.requests) == 1


@pytest.mark.parametrize(
    ("origin_id", "topic"), [("invalid", TOPIC), (TASK_ID, "invalid//topic")]
)
def test_direct_cancel_validates_coordinates_before_configuration_or_network(
    cli: CLI, capsys: pytest.CaptureFixture[str], origin_id: str, topic: str
) -> None:
    cli.config.write_text("invalid TOML =")
    assert cli.run(["cancel", "--origin-id", origin_id, "--topic", topic]) == 1
    assert "must be a valid" in capsys.readouterr().err
    assert cli.requests == [] and not cli.root.exists()


@pytest.mark.parametrize("not_found", [False, True])
def test_cancel_uses_saved_origin_and_retains_record_on_not_found(
    cli: CLI, capsys: pytest.CaptureFixture[str], not_found: bool
) -> None:
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 0
    capsys.readouterr()
    cli.config.write_text(
        f'url="https://changed.example"\ndomain="other"\nagent_id="{AGENT}"'
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert operation(request) == "cancel"
        assert body(request) == {"domain": DOMAIN, "topic": TOPIC, "origin_id": TASK_ID}
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
    assert cli.run(["cancel", "--origin-id", TASK_ID]) == int(not_found)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["cancelled"] is not not_found
    assert cli.record_path().exists() is not_found
    assert not cli.record_path().with_suffix(".task.json").exists()


@pytest.mark.parametrize("not_found", [False, True])
def test_direct_cancel_needs_no_agent_or_local_store(
    cli: CLI, capsys: pytest.CaptureFixture[str], not_found: bool
) -> None:
    cli.config.write_text(f'url="https://file.example"\ndomain="{DOMAIN}"')

    def respond(request: httpx.Request) -> httpx.Response:
        assert operation(request) == "cancel"
        assert body(request) == {"domain": DOMAIN, "topic": TOPIC, "origin_id": TASK_ID}
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
    assert cli.run(["cancel", "--origin-id", TASK_ID, "--topic", TOPIC]) == int(
        not_found
    )
    assert json.loads(capsys.readouterr().out)["origin_id"] == TASK_ID
    assert not cli.root.exists()


def test_cancel_domain_override_requires_topic(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["cancel", "--origin-id", TASK_ID, "--domain", DOMAIN]) == 1
    assert "--domain requires --topic" in capsys.readouterr().err
    assert cli.requests == []


@pytest.mark.parametrize("flag", ["--request-id", "--resend"])
def test_removed_submission_flags_are_rejected(cli: CLI, flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.run([*request_args(), flag, "legacy"])
    assert error.value.code == 2
    assert cli.requests == []


def test_timeout_preserves_request_for_a_later_wait(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr("tasklease.store.time.monotonic", lambda: now)
    monkeypatch.setattr("tasklease.store.time.sleep", sleep)
    cli.respond = lambda request: (
        httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "create"
        else empty()
    )
    assert cli.run([*request_args(), "--wait", "--timeout", "1s"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "origin_id": TASK_ID,
        "timed_out": True,
    }
    replies = [reply({"answer": "later"}), empty()]
    cli.respond = lambda request: (
        replies.pop(0)
        if operation(request) == "own"
        else httpx.Response(200, json={"ok": True})
    )
    cli.requests.clear()
    assert cli.run(["wait", "--origin-id", TASK_ID, "--timeout", "1s"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["answer"] == "later"
    assert all(operation(request) != "create" for request in cli.requests)
    assert cli.record_path().exists()


def test_all_agent_inbox_and_prune_need_no_configuration_or_credentials(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 2_000_000_000.0 - 8 * 86400
    monkeypatch.setattr("tasklease.store.time.time", lambda: now)

    def populate(agent: str, origin: str, *, unsolicited: bool = False) -> None:
        topic = f"reply/{agent}"
        replies = [reply({"answer": "ok"}, topic=topic, origin_id=origin)]
        if unsolicited:
            replies.append(
                reply(
                    {"message": "peer"},
                    task_id=OTHER_ID,
                    topic=topic,
                    origin_id=OTHER_ID,
                )
            )
        replies.append(empty())

        def respond(request: httpx.Request) -> httpx.Response:
            if operation(request) == "create":
                return httpx.Response(200, json={"ok": True, "id": origin})
            if operation(request) == "own":
                return replies.pop(0)
            return httpx.Response(200, json={"ok": True})

        with Client(
            "https://service.example",
            "tl_token",
            transport=httpx.MockTransport(respond),
        ) as api:
            store = Store(agent)
            store.submit(api, DOMAIN, TOPIC, topic, {})
            store.drain(api, DOMAIN, topic)

    populate(AGENT, TASK_ID, unsolicited=True)
    now += 8 * 86400
    populate("agent-b", REPLY_ID)
    cli.config.write_text("This is not valid TOML =")
    monkeypatch.delenv("TL_TOKEN")
    assert cli.run(["inbox", "--all"]) == 0
    records = json.loads(capsys.readouterr().out)["replies"]
    assert len(records) == 3 and any(not record["matched"] for record in records)
    assert cli.run(["inbox", "--all", "--prune"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pruned"] >= 3
    assert len(result["replies"]) == 1
    assert result["replies"][0]["agent_id"] == "agent-b"
    assert Store(AGENT).requests() == [] and Store(AGENT).inbox() == []
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
    corrupt = cli.root / "replies" / hashed(AGENT) / f"{OTHER_ID}.json"
    corrupt.write_text("{")
    cli.requests.clear()
    cli.respond = lambda request: reply({"answer": "new"}, origin_id=OTHER_ID)
    assert cli.run(["wait", "--origin-id", TASK_ID, "--timeout", "1s"]) == 1
    assert "local requester state" in capsys.readouterr().err
    assert [operation(request) for request in cli.requests] == ["own"]
    assert corrupt.read_text() == "{"


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


@pytest.mark.parametrize(
    "data",
    [
        "[]",
        "null",
        "{",
        '{"x":NaN}',
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


def test_wait_seconds_flag_controls_the_own_request(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    owns: list[dict[str, object]] = []
    replies = [reply({"answer": "done"}), empty()]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "own":
            owns.append(body(request))
            return replies.pop(0)
        assert operation(request) == "delete"
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    assert (
        cli.run([*request_args(), "--wait", "--timeout", "1m", "--wait-seconds", "12"])
        == 0
    )
    assert owns[0]["wait_seconds"] == 12
    assert json.loads(capsys.readouterr().out)["origin_id"] == TASK_ID


def test_config_wait_seconds_disables_the_requester_long_poll(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    # The opt-out for an older service, which rejects wait_seconds.
    with cli.config.open("a") as config:
        config.write('wait_seconds = "off"\n')
    owns: list[dict[str, object]] = []
    replies = [reply({"answer": "done"}), empty()]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation(request) == "own":
            owns.append(body(request))
            return replies.pop(0)
        assert operation(request) == "delete"
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    assert cli.run([*request_args(), "--wait", "--timeout", "1m"]) == 0
    assert "wait_seconds" not in owns[0]
    assert json.loads(capsys.readouterr().out)["origin_id"] == TASK_ID


def test_request_without_wait_ignores_the_wait_setting(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    with cli.config.open("a") as config:
        config.write("wait_seconds = 99\n")
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "id": TASK_ID})
    assert cli.run(request_args()) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "pending"
