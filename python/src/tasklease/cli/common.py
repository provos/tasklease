"""Shared command configuration and output."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from platformdirs import user_config_path

from tasklease.client import MAX_WAIT_SECONDS, Client
from tasklease.config import Config, load_config
from tasklease.errors import ConfigError
from tasklease.names import is_domain


def config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=(
            "Config file (default: TL_CONFIG or "
            f"{user_config_path('tasklease') / 'config.toml'})"
        ).replace("%", "%%"),  # argparse interpolates help with the % operator.
    )


def connection_options(parser: argparse.ArgumentParser) -> None:
    config_option(parser)
    parser.add_argument("--url", default=argparse.SUPPRESS)
    parser.add_argument("--worker", default=argparse.SUPPRESS)


def optional_text(args: argparse.Namespace, name: str) -> str | None:
    value: object = getattr(args, name, None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be text")
    return value


def text(args: argparse.Namespace, name: str) -> str:
    value = optional_text(args, name)
    if value is None:
        raise ConfigError(f"{name} is required")
    return value


def flag(args: argparse.Namespace, name: str) -> bool:
    value: object = getattr(args, name, False)
    return value is True


def wait_seconds(value: object) -> int:
    """Parse a long-poll wait; 0, `off` or `none` disables it."""
    if isinstance(value, str):
        if value.lower() in {"off", "none"}:
            return 0
        if re.fullmatch(r"[0-9]+", value):
            value = int(value)
    if type(value) is int and 0 <= value <= MAX_WAIT_SECONDS:
        return value
    raise ConfigError(
        f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}, or off"
    )


def requester_wait_seconds(args: argparse.Namespace) -> int:
    """`--wait-seconds`, else the config's top-level `wait_seconds`, else 30."""
    value: object = optional_text(args, "wait_seconds")
    if value is None:
        value = configuration(args).values.get("wait_seconds", MAX_WAIT_SECONDS)
    return wait_seconds(value)


def duration_seconds(value: str, *, name: str = "Duration") -> int:
    """Parse a positive duration expressed in seconds, minutes, hours or days."""
    match = re.fullmatch(r"([0-9]+)(s|m|h|d)", value)
    if not match or int(match[1]) <= 0:
        raise ConfigError(
            f"{name} must be positive seconds, minutes, hours or days, e.g. 3d"
        )
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def configuration(args: argparse.Namespace) -> Config:
    path: object = getattr(args, "config", None)
    if path is not None and not isinstance(path, (str, Path)):
        raise ConfigError("config must be a path")
    return load_config(path)


def domain(args: argparse.Namespace, config: Config) -> str:
    value = optional_text(args, "domain")
    if value is None:
        value = config.domain
    if value is None:
        raise ConfigError("Give a domain or set domain in configuration")
    if not is_domain(value):
        raise ConfigError(
            "domain must be a valid domain name of at most 256 characters"
        )
    return value


@contextmanager
def client(
    args: argparse.Namespace, *, admin: bool = False, config: Config | None = None
) -> Iterator[Client]:
    if config is None:
        config = configuration(args)
    url = optional_text(args, "url") or config.url
    if not url:
        raise ConfigError("Set the service URL in config.toml or use --url")
    worker = optional_text(args, "worker") or config.worker_label
    with Client(url, config.token(admin=admin), worker) as api:
        yield api


def output(value: object) -> None:
    # Escaping also preserves unusual wire JSON strings, including isolated
    # surrogate code units, without failing at the terminal's UTF-8 boundary.
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))


def message(value: str) -> None:
    print(value, file=sys.stderr)
