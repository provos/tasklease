"""Configuration selection, lazy validation and credential-free updates."""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tasklease.config import Config, load_config, save_deployment
from tasklease.errors import ConfigError


def test_explicit_config_then_environment_then_platform_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit.toml"
    environment = tmp_path / "environment.toml"
    default = tmp_path / "default" / "config.toml"
    monkeypatch.setattr("tasklease.config.user_config_path", lambda app: default.parent)
    assert (
        load_config(explicit, environ={"TL_CONFIG": str(environment)}).path == explicit
    )
    assert load_config(environ={"TL_CONFIG": str(environment)}).path == environment
    assert load_config(environ={}).path == default
    assert not default.exists()


def test_relative_config_path_is_resolved_before_token_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings" / "config.toml").write_text('[token]\nfile = "credential"\n')
    (tmp_path / "settings" / "credential").write_text("tl_file-token\n")
    monkeypatch.chdir(tmp_path)
    config = load_config("settings/config.toml", environ={})
    monkeypatch.chdir(tmp_path.parent)
    assert config.token() == "tl_file-token"


def test_environment_overrides_file_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'url="https://file.example"\ndomain="file"\nagent_id="file"\nworker_label="file"\n'
    )
    config = load_config(
        path,
        environ={
            "TL_URL": "https://env.example/",
            "TL_DOMAIN": "acme/project",
            "TL_AGENT_ID": "agent-1",
            "TL_WORKER": "worker-1",
        },
    )
    assert config.url == "https://env.example"
    assert config.domain == "acme/project"
    assert config.agent_id == "agent-1"
    assert config.worker_label == "worker-1"


def test_missing_optional_values_and_default_worker(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml", environ={})
    assert config.url is None
    assert config.domain is None
    assert config.agent_id is None
    assert config.worker_label == "cli"


@pytest.mark.parametrize(
    ("key", "value", "read"),
    [
        ("url", 12, lambda c: c.url),
        ("url", "https://user:secret@example.com", lambda c: c.url),
        ("url", "https://example.com/path", lambda c: c.url),
        ("url", "https://example.com?secret=value", lambda c: c.url),
        ("url", "https://example.com/#fragment", lambda c: c.url),
        ("url", "https://bad host.example", lambda c: c.url),
        ("url", "https://example.com:99999", lambda c: c.url),
        ("url", "file:///tmp/service", lambda c: c.url),
        ("domain", ".", lambda c: c.domain),
        ("domain", "..", lambda c: c.domain),
        ("domain", "Acme", lambda c: c.domain),
        ("domain", "a" * 257, lambda c: c.domain),
        ("agent_id", "a/b", lambda c: c.agent_id),
        ("agent_id", "A", lambda c: c.agent_id),
        ("agent_id", False, lambda c: c.agent_id),
        ("worker_label", "bad worker", lambda c: c.worker_label),
        ("worker_label", "a" * 257, lambda c: c.worker_label),
    ],
)
def test_settings_are_validated_only_when_used(
    tmp_path: Path, key: str, value: object, read: Callable[[Config], object]
) -> None:
    config = Config(tmp_path / "config.toml", {key: value}, {})
    assert config.path.name == "config.toml"
    with pytest.raises(ConfigError):
        read(config)


def test_invalid_agent_does_not_block_operator_configuration(tmp_path: Path) -> None:
    config = Config(
        tmp_path / "config.toml", {"url": "http://127.0.0.1:8787", "agent_id": 42}, {}
    )
    assert config.url == "http://127.0.0.1:8787"
    assert config.worker_label == "cli"


def test_identity_token_environment_and_relative_or_absolute_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    token = tmp_path / "credential"
    token.write_text("tl_disk-token\n")
    config = Config(
        path, {"token": {"file": "credential"}}, {"TL_TOKEN": "tl_env-token"}
    )
    assert config.token() == "tl_env-token"
    assert (
        Config(path, {"token": {"file": "credential"}}, {}).token() == "tl_disk-token"
    )
    assert Config(path, {"token": {"file": str(token)}}, {}).token() == "tl_disk-token"
    (tmp_path / "token").write_text("tl_default-token")
    assert Config(path, {}, {}).token() == "tl_default-token"


def test_admin_token_comes_only_from_its_environment_variable(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    (tmp_path / "token").write_text("tl_disk-token")
    values: dict[str, object] = {
        "admin_token": "admin-on-disk",
        "token": {"admin": "admin-on-disk"},
    }
    config = Config(path, values, {"TL_TOKEN": "tl_identity"})
    with pytest.raises(ConfigError, match="TL_ADMIN_TOKEN"):
        config.token(admin=True)
    config = Config(path, values, {"TL_ADMIN_TOKEN": "tla_admin"})
    assert config.token(admin=True) == "tla_admin"
    assert config.token() == "tl_disk-token"


@pytest.mark.parametrize("value", ["", "has spaces", "has\nnewline", "nonascii-é"])
def test_invalid_environment_tokens_are_controlled(tmp_path: Path, value: str) -> None:
    config = Config(tmp_path / "config.toml", {}, {"TL_TOKEN": value})
    with pytest.raises(ConfigError) as caught:
        config.token()
    if value:
        assert value not in str(caught.value)


def test_unreadable_or_invalid_token_files_are_controlled(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.toml", {}, {})
    with pytest.raises(ConfigError, match="Cannot read identity token"):
        config.token()
    (tmp_path / "token").write_bytes(b"\xff")
    with pytest.raises(ConfigError):
        config.token()
    (tmp_path / "token").write_text("contains spaces")
    with pytest.raises(ConfigError):
        config.token()


@pytest.mark.parametrize(
    "values", [{"token": "secret"}, {"token": {"file": 3}}, {"token": {"file": ""}}]
)
def test_invalid_token_config_is_controlled(
    tmp_path: Path, values: dict[str, object]
) -> None:
    with pytest.raises(ConfigError):
        Config(tmp_path / "config.toml", values, {}).token()


def test_deployment_update_preserves_latest_unrelated_toml_and_never_saves_environment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = (
        'domain = "acme/project"\nagent_id = "agent-1"\nworker_label = "worker-1"\n'
        "released = 2026-11-07T12:00:00Z\n"
        '[token]\nfile = "credential"\n'
        '[cloudflare]\ncustom = [1, 2]\nname = "old-worker"\n'
        '[profiles.verify]\ncommands = ["build", "test"]\n'
    )
    path.write_text(original)
    config = load_config(
        path, environ={"TL_TOKEN": "secret-identity", "TL_ADMIN_TOKEN": "secret-admin"}
    )
    path.write_text(original + "added_after_load = true\n")
    save_deployment(
        config,
        url="https://tasks.example/",
        account_id="account",
        name="new-worker",
        bucket="blobs",
    )
    with path.open("rb") as source:
        values = tomllib.load(source)
    assert values == {
        "url": "https://tasks.example",
        "domain": "acme/project",
        "agent_id": "agent-1",
        "worker_label": "worker-1",
        "released": datetime(2026, 11, 7, 12, tzinfo=UTC),
        "token": {"file": "credential"},
        "cloudflare": {
            "custom": [1, 2],
            "account_id": "account",
            "name": "new-worker",
            "bucket": "blobs",
        },
        "profiles": {
            "verify": {"commands": ["build", "test"], "added_after_load": True}
        },
    }
    assert config.url == "https://tasks.example"
    assert "secret-identity" not in path.read_text()
    assert "secret-admin" not in path.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(file.name for file in tmp_path.iterdir()) == ["config.toml"]


def test_save_creates_a_missing_private_config(tmp_path: Path) -> None:
    config = load_config(tmp_path / "new" / "config.toml", environ={})
    save_deployment(
        config,
        url="https://tasks.example",
        account_id="id",
        name="worker",
        bucket="blobs",
    )
    assert config.path.is_file()
    assert config.url == "https://tasks.example"
    if os.name == "posix":
        assert stat.S_IMODE(config.path.stat().st_mode) == 0o600


def test_failed_replace_leaves_original_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = 'domain = "acme"\n'
    path.write_text(original)
    config = load_config(path, environ={})

    def fail_replace(destination: Path, data: bytes) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr("tasklease.config.atomic_replace", fail_replace)
    with pytest.raises(ConfigError, match="Cannot save"):
        save_deployment(
            config,
            url="https://tasks.example",
            account_id="id",
            name="worker",
            bucket="blobs",
        )
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]
    assert config.values == {"domain": "acme"}


def test_malformed_toml_and_non_table_deployment_are_controlled(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="Cannot parse"):
        load_config(path, environ={})
    path.write_text("invalid = [")
    with pytest.raises(ConfigError, match="Cannot parse"):
        load_config(path, environ={})
    path.write_text('cloudflare = "value"\n')
    config = load_config(path, environ={})
    with pytest.raises(ConfigError, match="TOML table"):
        save_deployment(
            config,
            url="https://tasks.example",
            account_id="id",
            name="worker",
            bucket="blobs",
        )
    assert path.read_text() == 'cloudflare = "value"\n'


def test_empty_config_selection_is_controlled(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config("", environ={})
    with pytest.raises(ConfigError):
        load_config(environ={"TL_CONFIG": ""})


def test_save_deployment_preserves_or_explicitly_updates_pending_marker(
    tmp_path: Path,
) -> None:
    config = load_config(tmp_path / "config.toml", environ={})
    for pending in (True, None, False):
        save_deployment(
            config,
            url="https://tasks.example",
            account_id="id",
            name="worker",
            bucket="blobs",
            r2_credentials_pending=pending,
        )
        assert tomllib.loads(config.path.read_text())["cloudflare"][
            "r2_credentials_pending"
        ] is (pending is not False)
