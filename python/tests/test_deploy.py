"""Deployment recovery against an executable fake npx and a mocked service."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import httpx
import pytest

from tasklease import __version__
from tasklease.cli import service as service_cli
from tasklease.cli.service import configure_parser
from tasklease.cloudflare import DeployError, DeployOptions, deploy, deployer, status
from tasklease.cloudflare.bundle import configure_bundle, stage_bundle
from tasklease.cloudflare.wrangler import (
    Wrangler,
    bucket_names,
    deployment_url,
    lifecycle_rules,
    secret_names,
)
from tasklease.config import Config, load_config

ACCOUNT = "a" * 32
URL = "https://tasklease.example.workers.dev"
ALL_SECRETS = ["ADMIN_TOKEN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
RULE = (
    "name:     tasklease-expire\nenabled:  Yes\n"
    "prefix:   (all prefixes)\naction:   Expire objects after 7 days\n"
)
FAKE_NPX = r"""
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[3:]
record = {"pin": sys.argv[1:3], "args": args,
          "account": os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
          "logs": [os.environ.get(k) for k in ["WRANGLER_LOG",
                   "WRANGLER_LOG_SANITIZE", "WRANGLER_WRITE_LOGS"]],
          "target_env": [k for k in ["CLOUDFLARE_ENV", "WRANGLER_CI_OVERRIDE_NAME"]
                         if k in os.environ],
          "tasklease_env": [k for k in os.environ if k.startswith("TL_")]}
state.setdefault("calls", []).append(record)
body = sys.stdin.read()
if body:
    record["stdin"] = json.loads(body)
if args[0] == "deploy":
    state["rendered"] = json.loads(pathlib.Path(args[2]).read_text())
    event = state.get("event", {"type":"deploy", "version":1,
             "worker_name":"tasklease", "targets":["tasklease.example.workers.dev"]})
    pathlib.Path(os.environ["WRANGLER_OUTPUT_FILE_PATH"]).write_text(json.dumps(event)+"\n")
if args[:2] == ["secret", "bulk"]:
    state["secrets"] = sorted(set(state.get("secrets", [])) | record["stdin"].keys())
state_path.write_text(json.dumps(state))
if args[:len(state.get("fail", ["never"]))] == state.get("fail", ["never"]):
    print(state.get("failure_text", "simulated failure"), file=sys.stderr)
    sys.exit(2)
if args == ["whoami", "--json"]:
    print(json.dumps(state.get("whoami", {"loggedIn":True, "authType":"OAuth Token",
          "accounts":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]})))
elif args == ["r2", "bucket", "list"]:
    print("Listing buckets...")
    if state.get("bucket", True):
        print("name:           tasklease-blobs\ncreation_date:  2026-11-07T00:00:00Z")
elif args[:4] == ["r2", "bucket", "lifecycle", "list"]:
    print(state.get("rules", "No lifecycle rules found."))
elif args[:2] == ["secret", "list"]:
    records = [{"name":n, "type":"secret_text"} for n in state.get("secrets",[])]
    print(json.dumps(records))
"""


class Fake:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = root / "state.json"
        self.environment = {
            "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_STATE": str(self.path),
            "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
        }
        self.write({"secrets": ALL_SECRETS, "rules": RULE})
        if os.name == "nt":
            pytest.skip(
                "Executable fake npx uses POSIX shebang; OS acceptance is separate"
            )
        for name, source in (
            ("npx", FAKE_NPX),
            ("node", 'print("v24.13.1")\n'),
        ):
            target = root / name
            target.write_text(f"#!{sys.executable}\n" + source)
            target.chmod(0o700)

        def bundle(destination: Path) -> tuple[str, dict[str, object]]:
            (destination / "index.js").write_text("export default {}")
            return "4.158.0", {
                "main": "index.js",
                "vars": {"EXTRA": "preserve"},
                "durable_objects": {"bindings": [{"name": "DOMAINS"}]},
                "migrations": [{"tag": "v1"}],
                "r2_buckets": [{"binding": "BLOBS", "bucket_name": "placeholder"}],
            }

        monkeypatch.setattr(deployer, "stage_bundle", bundle)

    def read(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.path.read_text()))

    def write(self, changes: dict[str, object]) -> None:
        state = self.read() if self.path.exists() else {}
        state.update(changes)
        self.path.write_text(json.dumps(state))

    def calls(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.read().get("calls", []))

    def args(self) -> list[list[str]]:
        return [cast(list[str], call["args"]) for call in self.calls()]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


def config(tmp_path: Path, *, saved: bool = False) -> Config:
    values: dict[str, object] = {"domain": "my/domain", "custom": {"keep": [1, 2]}}
    if saved:
        values.update(
            url=URL,
            cloudflare={
                "account_id": ACCOUNT,
                "name": "tasklease",
                "bucket": "tasklease-blobs",
            },
        )
    path = tmp_path / "selected.toml"
    path.write_text('domain = "my/domain"\n[custom]\nkeep = [1, 2]\n')
    return Config(path, values, {})


class HeaderOnly(httpx.SyncByteStream):
    closed = False

    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("Admin confirmation must not buffer identity.list")

    def close(self) -> None:
        self.closed = True


class Service:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payload = b""
        self.confirmations: list[int] = [200]
        self.streams: list[HeaderOnly] = []
        self.fail: str | None = None
        self.cleanup_fails = False
        self.blob_failure = False
        self.wrong_download = False
        self.version = __version__

    def request(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(operation)
        if request.url.host == "bucket.r2.example":
            assert "authorization" not in request.headers
            if self.blob_failure:
                return httpx.Response(403)
            if request.method == "PUT":
                self.payload = request.content
                assert request.headers["content-length"] == "16"
                return httpx.Response(200)
            return httpx.Response(
                200, content=b"wrong" if self.wrong_download else self.payload
            )
        if operation == "version":
            return httpx.Response(
                200, json={"service_version": self.version, "protocol": "v1"}
            )
        if operation == "identity.list":
            code = (
                self.confirmations.pop(0)
                if len(self.confirmations) > 1
                else self.confirmations[0]
            )
            stream = HeaderOnly()
            self.streams.append(stream)
            return httpx.Response(code, stream=stream)
        if operation == self.fail or (
            operation == "identity.revoke" and self.cleanup_fails
        ):
            return httpx.Response(500)
        body = json.loads(request.content)
        if operation == "identity.add":
            return httpx.Response(
                200, json={"ok": True, "name": body["name"], "token": "identity-token"}
            )
        if operation == "create":
            return httpx.Response(200, json={"ok": True, "id": "created-id"})
        if operation == "own":
            return httpx.Response(200, json={"ok": True, "task": {"id": "owned-id"}})
        if operation == "consume":
            assert body["id"] == "owned-id" and body["tasks"] == []
            return httpx.Response(200, json={"ok": True, "ids": []})
        if operation == "blob.put_url":
            assert body["size"] == 16
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "key": "probe-key",
                    "url": "https://bucket.r2.example/object?signature=secret",
                    "expires_at": 1000,
                },
            )
        if operation == "blob.get_url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "url": "https://bucket.r2.example/object?signature=secret",
                    "expires_at": 1000,
                },
            )
        if operation == "identity.revoke":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"Unexpected operation {operation}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.request)


def test_upgrade_pin_environment_config_and_probe(fake: Fake, tmp_path: Path) -> None:
    service = Service()
    fake.environment["TL_ADMIN_TOKEN"] = "existing-admin"
    fake.environment.update(
        WRANGLER_LOG="debug", WRANGLER_LOG_SANITIZE="false", WRANGLER_WRITE_LOGS="true"
    )
    selected = config(tmp_path, saved=True)
    result = deploy(
        DeployOptions(),
        selected,
        environ=fake.environment,
        transport=service.transport(),
        retry_seconds=0,
    )
    assert result["verified"] is True
    assert len(service.payload) == 16
    assert service.calls[-1] == "identity.revoke"
    calls = fake.calls()
    assert all(call["pin"] == ["--yes", "wrangler@4.158.0"] for call in calls)
    assert all(call["account"] == ACCOUNT for call in calls)
    assert all(call["tasklease_env"] == [] for call in calls)
    assert all(call["logs"] == ["log", "true", "false"] for call in calls)
    assert not any(args[:2] == ["secret", "bulk"] for args in fake.args())
    rendered = cast(dict[str, object], fake.read()["rendered"])
    assert rendered["no_bundle"] is True and rendered["workers_dev"] is True
    assert rendered["vars"] == {
        "EXTRA": "preserve",
        "R2_ACCOUNT_ID": ACCOUNT,
        "R2_BUCKET_NAME": "tasklease-blobs",
    }
    saved = tomllib.loads(selected.path.read_text())
    assert saved["custom"] == {"keep": [1, 2]} and saved["domain"] == "my/domain"
    assert saved["cloudflare"]["account_id"] == ACCOUNT
    assert "existing-admin" not in selected.path.read_text()


def test_bootstrap_missing_keys_prints_before_bulk_and_saves_target(
    fake: Fake, tmp_path: Path
) -> None:
    fake.write({"bucket": False, "secrets": []})
    service = Service()
    messages: list[str] = []

    def report(message: str) -> None:
        if "ADMIN TOKEN" in message:
            assert not any(args[:2] == ["secret", "bulk"] for args in fake.args())
        messages.append(message)

    selected = config(tmp_path)
    with pytest.raises(DeployError, match="R2 signing credentials") as caught:
        deploy(
            DeployOptions(),
            selected,
            environ=fake.environment,
            transport=service.transport(),
            report=report,
            retry_seconds=0,
        )
    assert caught.value.exit_code == 4
    assert sum("tla_" in message for message in messages) == 1
    assert any(
        "confirmed" == message.removeprefix("Admin token ").removesuffix(".")
        for message in messages
    )
    assert all(stream.closed for stream in service.streams)
    assert "cloudflare" in tomllib.loads(selected.path.read_text())
    assert "tla_" not in selected.path.read_text()


@pytest.mark.parametrize("confirmation", [200, 401])
def test_ambiguous_rotation_confirms_even_when_bulk_fails(
    fake: Fake, tmp_path: Path, confirmation: int
) -> None:
    fake.write({"fail": ["secret", "bulk"]})
    service = Service()
    service.confirmations = [confirmation]
    messages: list[str] = []
    with pytest.raises(DeployError):
        deploy(
            DeployOptions(rotate_admin_token=True),
            config(tmp_path, saved=True),
            environ=fake.environment,
            transport=service.transport(),
            report=messages.append,
            retry_seconds=0,
        )
    assert "identity.list" in service.calls
    assert any("Admin token confirmed." == message for message in messages) == (
        confirmation == 200
    )
    if confirmation == 401:
        assert any("unconfirmed; retain it" in message for message in messages)
        assert not any(
            "discard" in message or "old token still" in message for message in messages
        )


def test_r2_pair_updates_without_admin_rotation(fake: Fake, tmp_path: Path) -> None:
    options = DeployOptions(
        r2_access_key_id="new-id", r2_secret_access_key="new-secret"
    )
    result = deploy(
        options,
        config(tmp_path, saved=True),
        environ=fake.environment,
        transport=Service().transport(),
        retry_seconds=0,
    )
    bulk = [
        call
        for call in fake.calls()
        if cast(list[str], call["args"])[:2] == ["secret", "bulk"]
    ]
    assert bulk[0]["stdin"] == {
        "R2_ACCESS_KEY_ID": "new-id",
        "R2_SECRET_ACCESS_KEY": "new-secret",
    }
    assert result["verified"] is False


@pytest.mark.parametrize(
    "change", [DeployOptions(bucket="other-bucket"), DeployOptions(account_id="b" * 32)]
)
def test_target_change_requires_new_keys_before_remote_calls(
    fake: Fake, tmp_path: Path, change: DeployOptions
) -> None:
    with pytest.raises(DeployError, match="new pair") as caught:
        deploy(
            change,
            config(tmp_path, saved=True),
            environ=fake.environment,
        )
    assert caught.value.exit_code == 4 and fake.calls() == []


def test_dry_run_changes_nothing_and_inspects_known_worker(
    fake: Fake, tmp_path: Path
) -> None:
    selected = config(tmp_path, saved=True)
    original = selected.path.read_bytes()
    result = deploy(
        DeployOptions(dry_run=True, rotate_admin_token=True),
        selected,
        environ=fake.environment,
        transport=Service().transport(),
    )
    assert result["dry_run"] is True and result["secrets"] == sorted(ALL_SECRETS)
    assert selected.path.read_bytes() == original
    assert [call["args"] for call in fake.calls()] == [
        ["whoami", "--json"],
        ["r2", "bucket", "list"],
        ["r2", "bucket", "lifecycle", "list", "tasklease-blobs"],
        ["secret", "list", "--name", "tasklease", "--format", "json"],
    ]


def test_lifecycle_replacement_preserves_unrelated_rules(
    fake: Fake, tmp_path: Path
) -> None:
    fake.write(
        {
            "rules": RULE.replace("7 days", "30 days")
            + "\n"
            + RULE.replace("tasklease-expire", "other-rule")
        }
    )
    deploy(
        DeployOptions(),
        config(tmp_path, saved=True),
        environ=fake.environment,
        transport=Service().transport(),
        retry_seconds=0,
    )
    mutations = [
        cast(list[str], call["args"])
        for call in fake.calls()
        if "remove" in cast(list[str], call["args"])
        or "add" in cast(list[str], call["args"])
    ]
    assert mutations == [
        [
            "r2",
            "bucket",
            "lifecycle",
            "remove",
            "tasklease-blobs",
            "--name",
            "tasklease-expire",
        ],
        [
            "r2",
            "bucket",
            "lifecycle",
            "add",
            "tasklease-blobs",
            "tasklease-expire",
            "--expire-days",
            "7",
            "--force",
        ],
    ]


@pytest.mark.parametrize(
    "fail", [["r2", "bucket", "list"], ["deploy"], ["secret", "list"]]
)
def test_failed_command_stops_and_redacts(
    fake: Fake, tmp_path: Path, fail: list[str]
) -> None:
    fake.environment["TL_ADMIN_TOKEN"] = "super-secret"
    fake.write({"fail": fail, "failure_text": "oops super-secret"})
    with pytest.raises(DeployError) as caught:
        deploy(
            DeployOptions(),
            config(tmp_path, saved=True),
            environ=fake.environment,
            transport=Service().transport(),
        )
    assert "super-secret" not in str(caught.value) and "[redacted]" in str(caught.value)
    assert cast(list[str], fake.calls()[-1]["args"])[: len(fail)] == fail


def test_probe_cleanup_does_not_mask_original(fake: Fake, tmp_path: Path) -> None:
    fake.environment["TL_ADMIN_TOKEN"] = "existing-admin"
    service = Service()
    service.fail = "consume"
    service.cleanup_fails = True
    messages: list[str] = []
    with pytest.raises(DeployError, match="Durable Objects task flow"):
        deploy(
            DeployOptions(),
            config(tmp_path, saved=True),
            environ=fake.environment,
            transport=service.transport(),
            report=messages.append,
            retry_seconds=0,
        )
    assert service.calls[-1] == "identity.revoke"
    assert any("cleanup also failed" in message for message in messages)


def test_status_needs_no_node_or_cloudflare_auth(fake: Fake, tmp_path: Path) -> None:
    result = status(
        config(tmp_path, saved=True),
        environ={"PATH": ""},
        transport=Service().transport(),
        report=lambda _: None,
    )
    assert (
        result["service_version"] == __version__ and result["upgrade_needed"] is False
    )
    assert result["cloudflare"] == {"available": False} and fake.calls() == []


def test_pinned_label_parsers_reject_incomplete_records() -> None:
    assert bucket_names("Listing buckets...\nname: example\ncreation_date: today") == {
        "example"
    }
    assert lifecycle_rules(RULE)[0]["action"] == "Expire objects after 7 days"
    assert secret_names('[{"name":"ADMIN_TOKEN","type":"secret_text"}]') == {
        "ADMIN_TOKEN"
    }
    for output in ("name: example", "random output"):
        with pytest.raises(DeployError):
            bucket_names(output)


@pytest.mark.parametrize("targets", [["tasklease.example.workers.dev"], [URL]])
def test_deploy_target_normalizes_only_typed_event(
    tmp_path: Path, targets: list[str]
) -> None:
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps({"type": "other", "targets": ["evil.workers.dev"]})
        + "\n"
        + json.dumps(
            {
                "type": "deploy",
                "version": 1,
                "worker_name": "tasklease",
                "targets": targets,
            }
        )
        + "\n"
    )
    assert deployment_url(path, "tasklease", None) == URL
    path.write_text(
        json.dumps(
            {
                "type": "deploy",
                "version": 1,
                "worker_name": "tasklease",
                "targets": [URL, "tasklease.other.workers.dev"],
            }
        )
    )
    with pytest.raises(DeployError) as caught:
        deployment_url(path, "tasklease", None)
    assert caught.value.exit_code == 4


def test_service_parser_preserves_global_selected_config() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--url")
    configure_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "--config",
            "/tmp/isolated.toml",
            "--url",
            URL,
            "service",
            "deploy",
            "--dry-run",
        ]
    )
    assert args.config == Path("/tmp/isolated.toml") and args.url == URL


@pytest.mark.parametrize(
    "options",
    [
        DeployOptions(r2_access_key_id="one-key"),
        DeployOptions(name="INVALID"),
        DeployOptions(bucket="no"),
        DeployOptions(account_id="bad-account"),
        DeployOptions(url="https://other.example.workers.dev"),
    ],
)
def test_invalid_inputs_stop_before_resources(
    fake: Fake, tmp_path: Path, options: DeployOptions
) -> None:
    with pytest.raises(DeployError) as caught:
        deploy(options, config(tmp_path), environ=fake.environment)
    assert caught.value.exit_code == 4 and fake.calls() == []


def test_node_and_explicit_api_token_account_requirements(
    fake: Fake, tmp_path: Path
) -> None:
    for environment in (
        {"PATH": ""},
        {"PATH": fake.environment["PATH"], "CLOUDFLARE_API_TOKEN": "api-token"},
    ):
        with pytest.raises(DeployError) as caught:
            deploy(DeployOptions(), config(tmp_path), environ=environment)
        assert caught.value.exit_code == 4
    assert fake.calls() == []


def test_account_discovery_and_multiple_account_stop(
    fake: Fake, tmp_path: Path
) -> None:
    del fake.environment["CLOUDFLARE_ACCOUNT_ID"]
    deploy(DeployOptions(dry_run=True), config(tmp_path), environ=fake.environment)
    assert all(call["account"] == ACCOUNT for call in fake.calls()[1:])
    fake.write(
        {
            "calls": [],
            "whoami": {
                "loggedIn": True,
                "accounts": [{"id": ACCOUNT}, {"id": "b" * 32}],
            },
        }
    )
    with pytest.raises(DeployError, match="more than one") as caught:
        deploy(DeployOptions(dry_run=True), config(tmp_path), environ=fake.environment)
    assert caught.value.exit_code == 4
    assert fake.args() == [["whoami", "--json"]]


@pytest.mark.parametrize(
    ("failure", "changes"),
    [
        (["whoami"], {}),
        (["r2", "bucket", "create"], {"bucket": False}),
        (["r2", "bucket", "lifecycle", "list"], {}),
        (
            ["r2", "bucket", "lifecycle", "remove"],
            {"rules": RULE.replace("7 days", "30 days")},
        ),
        (
            ["r2", "bucket", "lifecycle", "add"],
            {"rules": "There are no lifecycle rules for bucket 'tasklease-blobs'."},
        ),
        (["secret", "bulk"], {}),
    ],
)
def test_other_command_failures_stop_at_the_failed_step(
    fake: Fake, tmp_path: Path, failure: list[str], changes: dict[str, object]
) -> None:
    fake.write({**changes, "fail": failure})
    with pytest.raises(DeployError):
        deploy(
            DeployOptions(r2_access_key_id="id", r2_secret_access_key="secret"),
            config(tmp_path),
            environ=fake.environment,
            transport=Service().transport(),
            retry_seconds=0,
        )
    assert fake.args()[-1][: len(failure)] == failure


def test_dry_run_unknown_worker_does_not_try_secret_list(
    fake: Fake, tmp_path: Path
) -> None:
    messages: list[str] = []
    result = deploy(
        DeployOptions(dry_run=True),
        config(tmp_path),
        environ=fake.environment,
        report=messages.append,
    )
    assert result["secrets"] is None
    assert not any(args[:2] == ["secret", "list"] for args in fake.args())
    assert any("existence is unknown" in message for message in messages)


def test_admin_propagation_and_unreachable_outcomes(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service()
    service.confirmations = [401, 200]
    monkeypatch.setattr("tasklease.cloudflare.deployer.time.sleep", lambda _: None)
    deploy(
        DeployOptions(rotate_admin_token=True),
        config(tmp_path),
        environ=fake.environment,
        transport=service.transport(),
        retry_seconds=1,
    )
    assert service.calls.count("identity.list") == 2

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    messages: list[str] = []
    with pytest.raises(DeployError, match="unconfirmed"):
        deploy(
            DeployOptions(rotate_admin_token=True),
            config(tmp_path),
            environ=fake.environment,
            transport=httpx.MockTransport(unavailable),
            report=messages.append,
            retry_seconds=0,
        )
    assert any("retain it" in message for message in messages)


@pytest.mark.parametrize(
    "failure", ["version", "identity.add", "upload", "download", "cleanup"]
)
def test_http_verification_failures_keep_checkpoint_and_clean_up(
    fake: Fake, tmp_path: Path, failure: str
) -> None:
    fake.environment["TL_ADMIN_TOKEN"] = "admin"
    service = Service()
    if failure == "version":
        service.version = "old"
    elif failure == "upload":
        service.blob_failure = True
    elif failure == "download":
        service.wrong_download = True
    elif failure == "cleanup":
        service.cleanup_fails = True
    else:
        service.fail = failure
    selected = config(tmp_path)
    with pytest.raises(DeployError) as caught:
        deploy(
            DeployOptions(),
            selected,
            environ=fake.environment,
            transport=service.transport(),
            retry_seconds=0,
        )
    assert "signature=secret" not in str(caught.value)
    assert "cloudflare" in tomllib.loads(selected.path.read_text())
    if failure != "version":
        assert service.calls[-1] == "identity.revoke"


def test_status_reads_resources_without_mutation(fake: Fake, tmp_path: Path) -> None:
    result = status(
        config(tmp_path, saved=True),
        environ=fake.environment,
        transport=Service().transport(),
    )
    assert result["cloudflare"] == {
        "bucket_exists": True,
        "lifecycle": lifecycle_rules(RULE),
        "secrets": sorted(ALL_SECRETS),
    }
    assert all(
        args[0] != "deploy" and args[:2] != ["secret", "bulk"] for args in fake.args()
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://[",
        "https://tasklease.example.workers.dev/path",
        "https://tasklease.example.workers.dev.evil",
        "http://tasklease.example.workers.dev",
        "https://user@tasklease.example.workers.dev",
    ],
)
def test_invalid_url_override_is_input_error(tmp_path: Path, target: str) -> None:
    with pytest.raises(DeployError) as caught:
        deployment_url(tmp_path / "no-output", "tasklease", target)
    assert caught.value.exit_code == 4


@pytest.mark.parametrize("position", ["global", "leaf"])
def test_status_url_override_reaches_http_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    selected = config(tmp_path, saved=True)
    override = "https://other.example.workers.dev"
    observed: list[str | None] = []

    def inspect(config: Config, *, environ: dict[str, str]) -> dict[str, object]:
        observed.append(config.url)
        assert environ["TL_URL"] == override
        return {"url": config.url}

    monkeypatch.setattr(service_cli, "status", inspect)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--url")
    configure_parser(parser.add_subparsers())
    arguments = ["--config", str(selected.path)]
    if position == "global":
        arguments += ["--url", override]
    arguments += ["service", "status"]
    if position == "leaf":
        arguments += ["--url", override]
    args = parser.parse_args(arguments)
    assert args.handler(args) == 0
    assert observed == [override]


def test_status_override_does_not_inspect_another_workers_saved_resources(
    fake: Fake, tmp_path: Path
) -> None:
    selected = config(tmp_path, saved=True)
    selected._environ = {"TL_URL": "https://other.example.workers.dev"}
    result = status(selected, environ=fake.environment, transport=Service().transport())
    assert result["url"] == "https://other.example.workers.dev"
    assert "cloudflare" not in result and fake.calls() == []


@pytest.mark.parametrize("changed_field", ["account_id", "bucket"])
def test_failed_target_credential_update_requires_pair_after_reload(
    fake: Fake, tmp_path: Path, changed_field: str
) -> None:
    selected = config(tmp_path, saved=True)
    changed = (
        DeployOptions(
            account_id="b" * 32,
            r2_access_key_id="new-id",
            r2_secret_access_key="new-secret",
        )
        if changed_field == "account_id"
        else DeployOptions(
            bucket="other-bucket",
            r2_access_key_id="new-id",
            r2_secret_access_key="new-secret",
        )
    )
    fake.write({"fail": ["secret", "bulk"]})
    with pytest.raises(DeployError, match="secret bulk"):
        deploy(
            changed,
            selected,
            environ=fake.environment,
            transport=Service().transport(),
            retry_seconds=0,
        )
    saved = tomllib.loads(selected.path.read_text())
    assert saved["cloudflare"]["r2_credentials_pending"] is True
    assert saved["cloudflare"][changed_field] == getattr(changed, changed_field)

    # A new process sees only the new target and the pending flag. Existing
    # secret names cannot prove that the values were updated for this target.
    fake.write({"fail": ["never"], "calls": []})
    del fake.environment["CLOUDFLARE_ACCOUNT_ID"]
    original = selected.path.read_bytes()
    with pytest.raises(DeployError, match="credential update is pending") as caught:
        deploy(
            DeployOptions(),
            load_config(selected.path, environ={}),
            environ=fake.environment,
            transport=Service().transport(),
            retry_seconds=0,
        )
    assert caught.value.exit_code == 4
    assert fake.calls() == [] and selected.path.read_bytes() == original

    result = deploy(
        DeployOptions(r2_access_key_id="retry-id", r2_secret_access_key="retry-secret"),
        load_config(selected.path, environ={}),
        environ=fake.environment,
        transport=Service().transport(),
        retry_seconds=0,
    )
    assert result[changed_field] == getattr(changed, changed_field)
    assert (
        tomllib.loads(selected.path.read_text())["cloudflare"]["r2_credentials_pending"]
        is False
    )
    assert any(args[:2] == ["secret", "bulk"] for args in fake.args())


def test_interrupt_during_secret_write_leaves_credentials_pending(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = Wrangler.run

    def interrupt(
        runner: Wrangler,
        args: Sequence[str],
        *,
        secrets: Mapping[str, str] | None = None,
        output_file: Path | None = None,
        timeout: float = 180,
    ) -> str:
        if args[:2] == ["secret", "bulk"]:
            raise KeyboardInterrupt
        return original_run(
            runner, args, secrets=secrets, output_file=output_file, timeout=timeout
        )

    monkeypatch.setattr(Wrangler, "run", interrupt)
    selected = config(tmp_path, saved=True)
    with pytest.raises(KeyboardInterrupt):
        deploy(
            DeployOptions(r2_access_key_id="id", r2_secret_access_key="secret"),
            selected,
            environ=fake.environment,
            transport=Service().transport(),
            retry_seconds=0,
        )
    assert (
        tomllib.loads(selected.path.read_text())["cloudflare"]["r2_credentials_pending"]
        is True
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Native Windows process-tree acceptance is external"
)
@pytest.mark.parametrize("interrupted", [False, True])
def test_timeout_and_interrupt_stop_real_launcher_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    ready = tmp_path / "child-ready"
    late = tmp_path / "late-write"
    child = tmp_path / "child.py"
    child.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text('ready')\n"
        "time.sleep(0.8)\nPath(sys.argv[2]).write_text('mutation')\n"
    )
    launcher = tmp_path / "npx"
    launcher.write_text(
        f"#!{sys.executable}\nimport subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,{str(child)!r},{str(ready)!r},{str(late)!r}])\n"
        "time.sleep(30)\n"
    )
    launcher.chmod(0o700)
    runner = Wrangler("4.158.0", tmp_path, {"PATH": str(tmp_path)})
    if interrupted:
        original = subprocess.Popen.communicate
        first = True

        def communicate(
            process: subprocess.Popen[str],
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            nonlocal first
            if first:
                first = False
                try:
                    original(process, input=input, timeout=0.3)
                except subprocess.TimeoutExpired:
                    raise KeyboardInterrupt from None
                raise AssertionError("Launcher should still be running")
            return original(process, input=input, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
        with pytest.raises(KeyboardInterrupt):
            runner.run(["deploy"], timeout=0.3)
    else:
        with pytest.raises(DeployError, match="timed out"):
            runner.run(["deploy"], timeout=0.3)
    assert ready.exists(), "The descendant must have started before cancellation"
    time.sleep(0.9)
    assert not late.exists(), "No descendant may mutate after the failure returns"


def test_inherited_wrangler_target_variables_are_not_forwarded(fake: Fake) -> None:
    inherited = {
        **fake.environment,
        "CLOUDFLARE_ENV": "staging",
        "WRANGLER_CI_OVERRIDE_NAME": "other-worker",
    }
    runner = Wrangler("4.158.0", fake.path.parent, inherited)
    runner.run(["whoami", "--json"])
    assert fake.calls()[-1]["target_env"] == []
    assert fake.calls()[-1]["account"] == ACCOUNT
    assert inherited["CLOUDFLARE_ENV"] == "staging"
    assert inherited["WRANGLER_CI_OVERRIDE_NAME"] == "other-worker"


@pytest.mark.skipif(os.name == "nt", reason="Local CLI launcher uses a POSIX shebang")
def test_pinned_local_wrangler_cannot_redirect_packaged_target(tmp_path: Path) -> None:
    """Exercise actual normalization and upload preparation without a remote API."""
    package = Path(__file__).resolve().parents[2] / "node_modules" / "wrangler"
    node = shutil.which("node")
    if node is None or not (package / "package.json").is_file():
        pytest.skip("Pinned repository Wrangler and Node are required")
    project = tmp_path / "project"
    project.mkdir()
    pin, template = stage_bundle(project)
    assert json.loads((package / "package.json").read_text())["version"] == pin
    configure_bundle(
        project,
        template,
        account_id=ACCOUNT,
        name="tasklease-review",
        bucket="tasklease-review-blobs",
    )
    module = package / "wrangler-dist" / "cli.js"
    cli = package / "bin" / "wrangler.js"
    config_path = project / "wrangler.jsonc"
    runner = Wrangler(
        pin,
        project,
        {
            "PATH": os.environ.get("PATH", ""),
            "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
            "CLOUDFLARE_ENV": "staging",
            "WRANGLER_CI_OVERRIDE_NAME": "other-worker",
            "WRANGLER_HIDE_BANNER": "true",
            "WRANGLER_SEND_ERROR_REPORTS": "false",
            "WRANGLER_NO_SKILLS_UPDATE_PROMPTS": "true",
        },
    )
    # Fail closed even if a future pinned CLI adds a network check to dry-run.
    no_network = project / "no-network.cjs"
    no_network.write_text(
        "require('node:net').Socket.prototype.connect = function() { "
        "throw new Error('Network disabled in local target regression'); };\n"
    )
    runner.environ["NODE_OPTIONS"] = f"--require={no_network}"
    normalization = (
        "const {unstable_readConfig}=require(process.argv[1]);"
        "console.log(JSON.stringify({name:unstable_readConfig("
        "{config:process.argv[2]},{hideWarnings:true}).name}));"
    )

    def normalize(environment: Mapping[str, str]) -> str:
        result = subprocess.run(
            [node, "-e", normalization, str(module), str(config_path)],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        record = cast(dict[str, str], json.loads(result.stdout))
        return record["name"]

    unsafe_environment = {
        **runner.environ,
        "CLOUDFLARE_ENV": "staging",
        "WRANGLER_CI_OVERRIDE_NAME": "other-worker",
        "WRANGLER_OUTPUT_FILE_PATH": str(project / "unsafe.ndjson"),
    }
    assert normalize(unsafe_environment) == "tasklease-review-staging"
    subprocess.run(
        [node, str(cli), "deploy", "--dry-run", "--config", str(config_path)],
        cwd=project,
        env=unsafe_environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    unsafe_event = json.loads((project / "unsafe.ndjson").read_text().splitlines()[-1])
    assert unsafe_event["worker_name"] == "other-worker"
    assert normalize(runner.environ) == "tasklease-review"

    # Execute the installed pin directly in place of npx to avoid npm/network.
    launcher = project / "local-npx"
    launcher.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        f"os.execv({node!r}, [{node!r},{str(cli)!r},*sys.argv[3:]])\n"
    )
    launcher.chmod(0o700)
    runner.executable = str(launcher)
    output = project / "safe.ndjson"
    runner.run(
        ["deploy", "--dry-run", "--config", str(config_path)], output_file=output
    )
    safe_event = json.loads(output.read_text().splitlines()[-1])
    assert safe_event["worker_name"] == "tasklease-review"
