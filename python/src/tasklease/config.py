"""Per-user settings and credentials, with atomic deployment updates."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import tomli_w
from platformdirs import user_config_path

from tasklease.errors import ConfigError
from tasklease.fsio import atomic_replace
from tasklease.names import is_bearer_token, is_domain, is_segment, is_worker_label


def validate_url(value: str) -> str:
    """Validate a service origin and normalize its optional trailing slash."""
    if not isinstance(value, str):
        raise ConfigError("url must be an HTTP or HTTPS service origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not any(
                character.isspace() or ord(character) < 32 for character in value
            )
            and (port is None or 1 <= port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("url must be an HTTP or HTTPS service origin")
    return value.rstrip("/")


def _read_values(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as source:
            values: dict[str, object] = tomllib.load(source)
        return values
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ConfigError(f"Cannot parse configuration at {path}") from None
    except OSError:
        raise ConfigError(f"Cannot read configuration at {path}") from None


def _token(value: str, source: str) -> str:
    if not is_bearer_token(value):
        raise ConfigError(f"{source} must contain a nonempty bearer token")
    return value


@dataclass
class Config:
    """Settings from one config file and a snapshot of its environment."""

    path: Path
    values: dict[str, object]
    _environ: Mapping[str, str] = field(
        default_factory=lambda: dict(os.environ), repr=False
    )

    def __post_init__(self) -> None:
        self.path = self.path.expanduser().absolute()

    def _setting(self, key: str, environment: str) -> str | None:
        value: object = self._environ.get(environment, self.values.get(key))
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{key} must be a nonempty string")
        return value

    @property
    def url(self) -> str | None:
        value = self._setting("url", "TL_URL")
        return validate_url(value) if value is not None else None

    @property
    def domain(self) -> str | None:
        value = self._setting("domain", "TL_DOMAIN")
        if value is not None and not is_domain(value):
            raise ConfigError(
                "domain must be a valid domain name of at most 256 characters"
            )
        return value

    @property
    def agent_id(self) -> str | None:
        value = self._setting("agent_id", "TL_AGENT_ID")
        if value is not None and not is_segment(value):
            raise ConfigError(
                "agent_id must be a topic segment of at most 128 characters"
            )
        return value

    @property
    def worker_label(self) -> str:
        value = self._setting("worker_label", "TL_WORKER") or "cli"
        if not is_worker_label(value):
            raise ConfigError(
                "worker_label must contain 1 to 256 letters, digits, ., _ or -"
            )
        return value

    def token(self, admin: bool = False) -> str:
        """Read an identity credential, or the environment-only admin token."""
        environment = "TL_ADMIN_TOKEN" if admin else "TL_TOKEN"
        if environment in self._environ:
            return _token(self._environ[environment], environment)
        if admin:
            raise ConfigError("Set TL_ADMIN_TOKEN for identity administration")
        table = self.values.get("token", {})
        if not isinstance(table, dict):
            raise ConfigError("token must be a TOML table")
        token_table = cast(dict[str, object], table)
        filename = token_table.get("file", "token")
        if not isinstance(filename, str) or not filename:
            raise ConfigError("token.file must be a nonempty path string")
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = self.path.parent / path
        try:
            with path.open(encoding="utf-8") as source:
                if os.name == "posix" and os.fstat(source.fileno()).st_mode & 0o077:
                    raise ConfigError(
                        "Identity token file allows group or other access; "
                        "run chmod 600 on the identity token file "
                        "(token.file, or 'token' beside the config by default)"
                    )
                value = source.read().strip()
        except (OSError, UnicodeError):
            raise ConfigError(
                "Cannot read identity token; set TL_TOKEN or token.file"
            ) from None
        return _token(value, "token.file")


def load_config(
    path: Path | str | None = None, *, environ: Mapping[str, str] | None = None
) -> Config:
    """Select an explicit path, TL_CONFIG, or the platform's user config."""
    environment = dict(os.environ if environ is None else environ)
    if path is None:
        selected = environment.get("TL_CONFIG")
        if selected is not None:
            if not selected:
                raise ConfigError("TL_CONFIG must be a nonempty path")
            path = selected
        else:
            path = user_config_path("tasklease") / "config.toml"
    if isinstance(path, str) and not path:
        raise ConfigError("config path must be nonempty")
    resolved = Path(path).expanduser().absolute()
    return Config(resolved, _read_values(resolved), environment)


def save_deployment(
    config: Config,
    *,
    url: str,
    account_id: str,
    name: str,
    bucket: str,
    r2_credentials_pending: bool | None = None,
) -> None:
    """Merge deployment settings with the latest file and atomically save it.

    Environment overrides and credentials are never part of the serialized
    settings. Rereading avoids erasing settings edited since load_config.
    An omitted credential-pending flag preserves its latest saved value.
    """
    url = validate_url(url)
    deployment = {"account_id": account_id, "name": name, "bucket": bucket}
    if any(not isinstance(value, str) or not value for value in deployment.values()):
        raise ConfigError(
            "Deployment account_id, name and bucket must be nonempty strings"
        )
    values = _read_values(config.path)
    existing = values.get("cloudflare", {})
    if not isinstance(existing, dict):
        raise ConfigError("cloudflare must be a TOML table")
    cloudflare = cast(dict[str, object], existing).copy()
    cloudflare.update(deployment)
    if r2_credentials_pending is not None:
        cloudflare["r2_credentials_pending"] = r2_credentials_pending
    values.update(url=url, cloudflare=cloudflare)
    try:
        serialized = tomli_w.dumps(values)
    except (TypeError, ValueError):
        raise ConfigError(
            "Configuration contains values that cannot be saved"
        ) from None

    try:
        atomic_replace(config.path, serialized.encode("utf-8"))
    except OSError:
        raise ConfigError(f"Cannot save configuration at {config.path}") from None
    config.values = values
