"""Per-user settings and credentials, with atomic deployment updates."""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable, Mapping
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
    profile: str | None = None

    def __post_init__(self) -> None:
        self.path = self.path.expanduser().absolute()
        if self.profile is not None:
            if not isinstance(self.profile, str) or not self.profile:
                raise ConfigError("profile must be a nonempty string")
            self._profile_table()

    def _profile_table(self) -> dict[str, object]:
        if self.profile is None:
            return {}
        profiles = self.values.get("profiles")
        if profiles is not None and not isinstance(profiles, dict):
            raise ConfigError("profiles must be a TOML table")
        selected: object = (
            cast(dict[str, object], profiles).get(self.profile)
            if isinstance(profiles, dict)
            else None
        )
        if selected is None:
            raise ConfigError(f"Profile {self.profile!r} is not defined in {self.path}")
        if not isinstance(selected, dict):
            raise ConfigError(f"Profile {self.profile!r} must be a TOML table")
        return cast(dict[str, object], selected)

    def setting(self, key: str, default: object = None) -> object:
        """Read a setting from the active profile, falling back to top level."""
        profile = self._profile_table()
        if key in profile:
            return profile[key]
        return self.values.get(key, default)

    def table_layers(self, key: str) -> tuple[dict[str, object], dict[str, object]]:
        """Read a top-level TOML table and its optional active-profile override."""
        base = self.values.get(key)
        if base is not None and not isinstance(base, dict):
            raise ConfigError(f"{key} must be a TOML table")
        base_table = dict(cast(dict[str, object], base)) if base is not None else {}
        profile = self._profile_table()
        if key not in profile:
            return base_table, {}
        override = profile[key]
        if not isinstance(override, dict):
            raise ConfigError(f"{key} must be a TOML table")
        return base_table, dict(cast(dict[str, object], override))

    def table(self, key: str) -> dict[str, object]:
        """Read a TOML table, merging profile overrides onto top-level entries."""
        base_table, override_table = self.table_layers(key)
        base_table.update(override_table)
        return base_table

    def _setting(self, key: str, environment: str) -> str | None:
        value: object = (
            self._environ[environment]
            if environment in self._environ
            else self.setting(key)
        )
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
        token_table = self.table("token")
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

    def token_loader(self, admin: bool = False) -> Callable[[], str] | None:
        """Return a reloader for file-backed identity credentials, else None."""
        if admin or "TL_TOKEN" in self._environ:
            return None
        return lambda: self.token(admin=False)


def default_config_path() -> Path:
    """Platform-native user configuration path without a doubled Windows folder."""
    return user_config_path("tasklease", appauthor=False) / "config.toml"


def _resolve_default_config_path() -> Path:
    preferred = default_config_path()
    if sys.platform == "win32":
        legacy = user_config_path("tasklease") / "config.toml"
        if legacy != preferred:
            try:
                use_legacy = not preferred.exists() and legacy.is_file()
            except OSError:
                raise ConfigError(
                    f"Cannot inspect default configuration path {preferred}"
                ) from None
            if use_legacy:
                print(
                    f"Using legacy config path {legacy}; move config.toml and token to "
                    f"{preferred.parent}",
                    file=sys.stderr,
                )
                return legacy
    return preferred


def load_config(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    profile: str | None = None,
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
            path = _resolve_default_config_path()
    if isinstance(path, str) and not path:
        raise ConfigError("config path must be nonempty")
    if profile is None:
        selected_profile = environment.get("TL_PROFILE")
        if selected_profile is not None:
            if not selected_profile:
                raise ConfigError("TL_PROFILE must be a nonempty profile name")
            profile = selected_profile
    elif not profile:
        raise ConfigError("profile must be a nonempty string")
    resolved = Path(path).expanduser().absolute()
    return Config(resolved, _read_values(resolved), environment, profile=profile)


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
