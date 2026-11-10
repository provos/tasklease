"""Operator commands through their actual typed HTTP client boundary."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import httpx
import pytest

from tasklease import __version__
from tasklease.cli import main
from tasklease.client import Client
from tasklease.generated.protocol import TaskSummary

IDENTITY_TOKEN = "tl_identity-secret"
ADMIN_TOKEN = "tla_admin-secret"
NOW = 2_000_000_000_000
DAY = 86_400_000
CUTOFF = NOW - 3 * DAY
SCHEMA = Path(__file__).resolve().parents[2] / "schema"


def body(request: httpx.Request) -> dict[str, object]:
    value: object = json.loads(request.content)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def operation(request: httpx.Request) -> str:
    return request.url.path.rsplit("/", 1)[-1]


def example(name: str) -> dict[str, object]:
    value: object = json.loads(
        (SCHEMA / "examples" / name / "valid" / "response.json").read_text()
    )
    assert isinstance(value, dict)
    response: object = value["body"]
    assert isinstance(response, dict)
    return cast(dict[str, object], response)


def unexpected_request(request: httpx.Request) -> NoReturn:
    pytest.fail(f"Unexpected {operation(request)} request")


@dataclass
class CLI:
    config: Path
    requests: list[httpx.Request]
    respond: Callable[[httpx.Request], httpx.Response] = unexpected_request

    def run(self, args: Sequence[str]) -> int:
        return main(["--config", str(self.config), *args])


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
        "TL_R2_ACCESS_KEY_ID",
        "TL_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TL_TOKEN", IDENTITY_TOKEN)
    monkeypatch.setenv("TL_ADMIN_TOKEN", ADMIN_TOKEN)
    config = tmp_path / "config.toml"
    config.write_text(
        'url="https://file.example"\ndomain="default-only"\nagent_id=false\n'
        '[token]\nfile="credential"\n'
    )
    command = CLI(config, [])

    def record(request: httpx.Request) -> httpx.Response:
        command.requests.append(request)
        return command.respond(request)

    def factory(url: str, token: str, worker_label: str = "cli") -> Client:
        return Client(url, token, worker_label, transport=httpx.MockTransport(record))

    monkeypatch.setattr("tasklease.cli.common.Client", factory)
    return command


@pytest.mark.parametrize(
    ("args", "wire", "expected_body"),
    [
        (["identity", "list"], "identity.list", {}),
        (["identity", "revoke", "linux-1"], "identity.revoke", {"name": "linux-1"}),
        (["identity", "rotate", "linux-1"], "identity.rotate", {"name": "linux-1"}),
    ],
)
def test_identity_administration_uses_only_admin_environment_token(
    cli: CLI,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    wire: str,
    expected_body: dict[str, object],
) -> None:
    response = example(wire)
    cli.respond = lambda request: httpx.Response(200, json=response)
    assert cli.run(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert ADMIN_TOKEN not in captured.err
    assert IDENTITY_TOKEN not in captured.err
    assert len(cli.requests) == 1
    assert operation(cli.requests[0]) == wire
    assert body(cli.requests[0]) == expected_body
    assert cli.requests[0].headers["authorization"] == f"Bearer {ADMIN_TOKEN}"


def test_identity_add_reports_automatic_error_grants_on_stderr(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.respond = lambda request: httpx.Response(
        200, json={"ok": True, "name": "linux-1", "token": "tl_new-identity"}
    )
    assert (
        cli.run(
            [
                "identity",
                "add",
                "linux-1",
                "--domain",
                "acme/a",
                "--domain",
                "acme/b",
                "--own",
                "verify/linux,verify/linux,verify/windows",
                "--create",
                "result/*,verify/windows/error",
                "--read",
                "**",
                "--operator",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": True,
        "name": "linux-1",
        "token": "tl_new-identity",
    }
    assert "Added create grants: verify/linux/error" in captured.err
    assert "verify/windows/error" not in captured.err
    assert "shown only once" in captured.err
    assert "tl_new-identity" not in captured.err
    assert body(cli.requests[0]) == {
        "name": "linux-1",
        "domains": ["acme/a", "acme/b"],
        "scopes": {
            "own": ["verify/linux", "verify/linux", "verify/windows"],
            "create": ["result/*", "verify/windows/error"],
            "read": ["**"],
        },
        "operator": True,
    }
    assert cli.requests[0].headers["authorization"] == f"Bearer {ADMIN_TOKEN}"


def test_whoami_uses_identity_token_and_outputs_only_json(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    response = example("identity.whoami")
    cli.respond = lambda request: httpx.Response(200, json=response)
    assert cli.run(["whoami"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert captured.err == ""
    assert cli.requests[0].headers["authorization"] == f"Bearer {IDENTITY_TOKEN}"


@pytest.mark.parametrize(
    ("environment", "selected", "expected"),
    [
        (None, None, "default-only"),
        ("acme/environment", None, "acme/environment"),
        ("acme/environment", "acme/override", "acme/override"),
    ],
)
@pytest.mark.parametrize("remove", [False, True])
def test_declare_uses_configured_domain_or_explicit_override_and_identity_token(
    cli: CLI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: str | None,
    selected: str | None,
    expected: str,
    remove: bool,
) -> None:
    if environment:
        monkeypatch.setenv("TL_DOMAIN", environment)
    cli.respond = lambda request: httpx.Response(200, json={"ok": True})
    args = ["topic", "declare", "verify/linux"]
    if selected:
        args.extend(["--domain", selected])
    if remove:
        args.append("--remove")
    assert cli.run(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}
    assert captured.err == ""
    assert len(cli.requests) == 1
    assert operation(cli.requests[0]) == "topic.declare"
    assert body(cli.requests[0]) == {
        "domain": expected,
        "topic": "verify/linux",
        "declared": not remove,
    }
    assert cli.requests[0].headers["authorization"] == f"Bearer {IDENTITY_TOKEN}"


def test_declare_missing_domain_fails_before_network(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.config.write_text('url="https://file.example"\n')
    assert cli.run(["topic", "declare", "verify/linux"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "domain" in captured.err
    assert not cli.requests


def test_cli_help_shows_platform_config_fallback(
    cli: CLI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    directory = Path(tmp_path.anchor) / "User config"
    monkeypatch.setattr("tasklease.cli.common.user_config_path", lambda app: directory)
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert str(directory / "config.toml") in " ".join(captured.out.split())
    assert "TL_CONFIG" in captured.out
    assert captured.err == "" and not cli.requests


def test_identity_token_file_is_used_for_ordinary_commands_but_never_admin(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TL_TOKEN")
    monkeypatch.delenv("TL_ADMIN_TOKEN")
    cli.config.with_name("credential").write_text("tl_disk-token\n")
    cli.config.with_name("credential").chmod(0o600)
    cli.respond = lambda request: httpx.Response(200, json=example("identity.whoami"))
    assert cli.run(["whoami"]) == 0
    assert cli.requests[0].headers["authorization"] == "Bearer tl_disk-token"
    capsys.readouterr()
    assert cli.run(["identity", "list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TL_ADMIN_TOKEN" in captured.err
    assert len(cli.requests) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX token permission bits")
def test_insecure_token_file_reports_fix_without_stdout_secret_or_request(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TL_TOKEN")
    token = cli.config.with_name("credential")
    token.write_text("tl_insecure-do-not-print")
    token.chmod(0o644)
    assert cli.run(["whoami"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "chmod 600" in captured.err
    assert "tl_insecure-do-not-print" not in captured.err
    assert "Traceback" not in captured.err
    assert not cli.requests


@pytest.mark.parametrize("leaf_override", [False, True])
def test_global_connection_options_and_leaf_overrides(
    cli: CLI,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    leaf_override: bool,
) -> None:
    global_config = tmp_path / "global.toml"
    leaf_config = tmp_path / "leaf.toml"
    global_config.write_text(
        'url="https://global-file.example"\nworker_label="global"\n'
    )
    leaf_config.write_text('url="https://leaf-file.example"\nworker_label="leaf"\n')
    cli.respond = lambda request: httpx.Response(200, json=example("identity.whoami"))
    args = ["--config", str(global_config), "--url", "https://global.example", "whoami"]
    if leaf_override:
        args.extend(["--config", str(leaf_config), "--url", "https://leaf.example"])
    assert main(args) == 0
    capsys.readouterr()
    request = cli.requests[0]
    assert request.url.host == ("leaf.example" if leaf_override else "global.example")
    assert request.headers["x-worker"] == ("leaf" if leaf_override else "global")


def test_topics_without_domain_visits_all_identity_domains_and_ignores_default(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TL_DOMAIN", "Invalid default is irrelevant")
    domains = ["acme/a", "acme/b"]

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "identity.whoami":
            return httpx.Response(
                200, json={**example("identity.whoami"), "domains": domains}
            )
        assert operation(request) == "topics.list"
        return httpx.Response(200, json={"ok": True, "topics": []})

    cli.respond = respond
    assert cli.run(["topics", "--glob", "verify/**"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == [
        {"domain": name, "ok": True, "topics": []} for name in domains
    ]
    assert [operation(request) for request in cli.requests] == [
        "identity.whoami",
        "topics.list",
        "topics.list",
    ]
    assert [body(request) for request in cli.requests[1:]] == [
        {"domain": name, "glob": "verify/**"} for name in domains
    ]


def test_topics_explicit_domain_avoids_whoami_and_narrows_query(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.respond = lambda request: httpx.Response(200, json={"ok": True, "topics": []})
    assert cli.run(["topics", "--domain", "acme/selected"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [
        {"domain": "acme/selected", "ok": True, "topics": []}
    ]
    assert len(cli.requests) == 1
    assert operation(cli.requests[0]) == "topics.list"
    assert body(cli.requests[0]) == {"domain": "acme/selected"}


def test_move_reads_then_atomically_consumes_one_copy(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    response = example("read")
    task_value = response["task"]
    assert isinstance(task_value, dict)
    task = cast(dict[str, object], task_value)
    task.update(id="t_live", data={"nested": {"result": "é"}, "count": 2})

    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "read":
            return httpx.Response(200, json=response)
        assert operation(request) == "consume"
        return httpx.Response(200, json={"ok": True, "ids": ["t_followup"]})

    cli.respond = respond
    assert (
        cli.run(
            ["move", "acme", "verify/linux/error", "t_live", "--to", "verify/linux"]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"ok": True, "ids": ["t_followup"]}
    assert [operation(request) for request in cli.requests] == ["read", "consume"]
    assert body(cli.requests[1]) == {
        "domain": "acme",
        "topic": "verify/linux/error",
        "id": "t_live",
        "tasks": [{"topic": "verify/linux", "data": task["data"]}],
    }


def test_move_consume_failure_leaves_no_create_or_delete(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if operation(request) == "read":
            return httpx.Response(200, json=example("read"))
        return httpx.Response(
            403,
            json={
                "ok": False,
                "error": {"code": "forbidden", "message": "not the holder"},
            },
        )

    cli.respond = respond
    assert cli.run(["move", "acme", "work/error", "t_live", "--to", "work"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not the holder" in captured.err
    assert [operation(request) for request in cli.requests] == ["read", "consume"]


def test_tasks_passes_cursor_and_returns_one_page_as_json(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    response = {"ok": True, "tasks": [], "cursor": "next-page"}
    cli.respond = lambda request: httpx.Response(200, json=response)
    assert cli.run(["tasks", "acme", "work", "--cursor", "previous-page"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == response
    assert len(cli.requests) == 1
    assert body(cli.requests[0]) == {
        "domain": "acme",
        "topic": "work",
        "cursor": "previous-page",
    }


def task_summary(identifier: str, *, lease: int, owned_count: int = 1) -> TaskSummary:
    return {
        "id": identifier,
        "created_at": 0,
        "lease_until": lease,
        "owned_count": owned_count,
        "creator": "creator",
        "owner": {"identity": "owner", "worker": "worker"} if owned_count else None,
        "owned_at": 1 if owned_count else None,
        "state": "available",
    }


@pytest.mark.parametrize("include_never_owned", [False, True])
def test_prune_walks_all_pages_and_topics_with_atomic_lease_guard(
    cli: CLI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    include_never_owned: bool,
) -> None:
    monkeypatch.setattr("tasklease.cli.tasks.time.time_ns", lambda: NOW * 1_000_000)
    pages = {
        ("work/a", None): {
            "ok": True,
            "tasks": [
                task_summary("t_delete", lease=CUTOFF - 1),
                task_summary("t_stale", lease=CUTOFF - 1),
            ],
            "cursor": "page-two",
        },
        ("work/a", "page-two"): {
            "ok": True,
            "tasks": [
                task_summary("t_forbidden", lease=CUTOFF - 1),
                task_summary("t_renewed", lease=CUTOFF - 1),
            ],
        },
        ("work/b", None): {
            "ok": True,
            "tasks": [
                task_summary("t_never", lease=CUTOFF - 1, owned_count=0),
                task_summary("t_current", lease=CUTOFF + 1),
                task_summary("t_boundary", lease=CUTOFF),
            ],
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = body(request)
        if operation(request) == "topics.list":
            assert payload == {"domain": "acme", "glob": "work/**"}
            return httpx.Response(
                200,
                json={"ok": True, "topics": [{"topic": "work/a"}, {"topic": "work/b"}]},
            )
        if operation(request) == "tasks.list":
            topic, cursor = payload["topic"], payload.get("cursor")
            assert isinstance(topic, str)
            assert cursor is None or isinstance(cursor, str)
            return httpx.Response(200, json=pages[topic, cursor])
        assert operation(request) == "delete"
        assert payload["lease_before"] == CUTOFF
        code = "not_found" if payload["id"] == "t_stale" else "forbidden"
        if payload["id"] in {"t_stale", "t_forbidden", "t_renewed"}:
            return httpx.Response(
                404 if code == "not_found" else 403,
                json={
                    "ok": False,
                    "error": {"code": code, "message": "changed while pruning"},
                },
            )
        return httpx.Response(200, json={"ok": True})

    cli.respond = respond
    args = ["prune", "acme", "--topic", "work/**", "--lease-older-than", "3d"]
    if include_never_owned:
        args.append("--all")
    assert cli.run(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True,
        "deleted": 2 if include_never_owned else 1,
        "skipped": 3,
    }
    deleted = [
        body(request)["id"]
        for request in cli.requests
        if operation(request) == "delete"
    ]
    assert deleted == [
        "t_delete",
        "t_stale",
        "t_forbidden",
        "t_renewed",
        *(["t_never"] if include_never_owned else []),
    ]
    listings = [
        body(request) for request in cli.requests if operation(request) == "tasks.list"
    ]
    assert listings == [
        {"domain": "acme", "topic": "work/a"},
        {"domain": "acme", "topic": "work/a", "cursor": "page-two"},
        {"domain": "acme", "topic": "work/b"},
    ]


@pytest.mark.parametrize("age", ["0s", "12", "1.5h", "-1d"])
def test_prune_rejects_invalid_age_without_network(
    cli: CLI, capsys: pytest.CaptureFixture[str], age: str
) -> None:
    assert (
        cli.run(["prune", "acme", "--topic", "work", f"--lease-older-than={age}"]) == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Age must be positive" in captured.err
    assert cli.requests == []


@pytest.mark.parametrize("status", [403, 500])
def test_service_errors_return_one_with_json_stdout_empty(
    cli: CLI, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    cli.respond = lambda request: httpx.Response(
        status,
        json={"ok": False, "error": {"code": "forbidden", "message": "scope refused"}},
    )
    assert cli.run(["whoami"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "scope refused" in captured.err if status == 403 else "HTTP 500" in captured.err
    )
    assert "Traceback" not in captured.err
    assert IDENTITY_TOKEN not in captured.err
    assert len(cli.requests) == 1


def test_missing_service_url_has_actionable_message_without_network(
    cli: CLI, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.config.unlink()
    assert cli.run(["whoami"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "URL" in captured.err and "--url" in captured.err
    assert cli.requests == []


@pytest.mark.parametrize(
    "args", [["--help"], ["--version"], ["service", "deploy", "--help"]]
)
def test_help_and_version_need_no_config_credentials_or_node(
    cli: CLI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    cli.config.write_text("malformed = [")
    monkeypatch.delenv("TL_TOKEN")
    monkeypatch.delenv("TL_ADMIN_TOKEN")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(SystemExit) as caught:
        cli.run(args)
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if args == ["--version"]:
        assert captured.out.strip() == f"tl {__version__}"
    elif args[0] == "service":
        assert "Node.js" in captured.out
        assert "TL_R2_SECRET_ACCESS_KEY" in captured.out
        assert "TL_ADMIN_TOKEN" in captured.out
    else:
        assert "A leased task board" in captured.out
    assert cli.requests == []


def test_deploy_without_node_explains_how_to_proceed(
    cli: CLI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "")
    assert cli.run(["service", "deploy", "--dry-run"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Install Node.js" in captured.err
    assert "npx" in captured.err
    assert cli.requests == []
