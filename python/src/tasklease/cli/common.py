"""Shared command configuration and output."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tasklease.client import Client
from tasklease.config import Config, load_config
from tasklease.errors import ConfigError


def connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=argparse.SUPPRESS)
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


def configuration(args: argparse.Namespace) -> Config:
    path: object = getattr(args, "config", None)
    if path is not None and not isinstance(path, (str, Path)):
        raise ConfigError("config must be a path")
    return load_config(path)


def domain(args: argparse.Namespace, config: Config) -> str:
    value = optional_text(args, "domain") or config.domain
    if not value:
        raise ConfigError("Give a domain or set domain in configuration")
    return value


@contextmanager
def client(args: argparse.Namespace, *, admin: bool = False) -> Iterator[Client]:
    config = configuration(args)
    url = optional_text(args, "url") or config.url
    if not url:
        raise ConfigError("Set the service URL in config.toml or use --url")
    worker = optional_text(args, "worker") or config.worker_label
    with Client(url, config.token(admin=admin), worker) as api:
        yield api


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def message(value: str) -> None:
    print(value, file=sys.stderr)
