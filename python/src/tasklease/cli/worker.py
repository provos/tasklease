"""Run a generic worker or request a graceful local stop."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable
from pathlib import Path
from threading import TIMEOUT_MAX
from typing import cast

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    duration_seconds,
    flag,
    optional_text,
    output,
)
from tasklease.errors import ConfigError
from tasklease.worker import (
    EchoHandler,
    Handler,
    UnlimitedCapacity,
    WorkerLoop,
    validate_work_topic,
)
from tasklease.worker_control import WorkerControl, request_stop


def _table(values: dict[str, object]) -> dict[str, object]:
    value = values.get("worker", {})
    if not isinstance(value, dict):
        raise ConfigError("worker must be a TOML table")
    return cast(dict[str, object], value)


def _setting(
    args: argparse.Namespace, settings: dict[str, object], name: str, default: object
) -> object:
    selected: object = getattr(
        args, "worker_handler" if name == "handler" else name, None
    )
    return settings.get(name, default) if selected is None else selected


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"worker {name} must be a nonempty string")
    return value


def _seconds(value: object, name: str) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str):
        return duration_seconds(value, name=name)
    raise ConfigError(f"worker {name} must be positive seconds or a duration")


def _handler(value: str) -> Handler[object, object]:
    if value == "echo":
        return cast(Handler[object, object], EchoHandler())
    module_name, separator, factory_name = value.partition(":")
    if not separator or not module_name or not factory_name.isidentifier():
        raise ConfigError("worker handler must be echo or module:factory")
    try:
        module = importlib.import_module(module_name)
        factory: object = getattr(module, factory_name)
        if not callable(factory):
            raise TypeError("Factory is not callable")
        instance = cast(Callable[[], object], factory)()
    except Exception:
        raise ConfigError(f"Cannot load worker handler {value}") from None
    if not all(
        callable(getattr(instance, name, None)) for name in ("validate", "run", "reply")
    ):
        raise ConfigError("worker handler must provide validate, run and reply")
    return cast(Handler[object, object], instance)


def _root(args: argparse.Namespace) -> Path | None:
    value: object = getattr(args, "state_dir", None)
    if value is not None and not isinstance(value, Path):
        raise ConfigError("state-dir must be a path")
    return value


def _stop(args: argparse.Namespace) -> int:
    label = optional_text(args, "worker") or configuration(args).worker_label
    path = request_stop(label, _root(args))
    output({"ok": True, "worker": label, "stop_file": str(path)})
    return 0


def _run(args: argparse.Namespace) -> int:
    config = configuration(args)
    settings = _table(config.values)
    selected = _text(optional_text(args, "domain") or config.domain, "domain")
    topic = validate_work_topic(_text(_setting(args, settings, "topic", None), "topic"))
    lease = _seconds(_setting(args, settings, "lease", 600), "lease")
    renew = _seconds(_setting(args, settings, "renew_every", 180), "renew_every")
    poll = _seconds(_setting(args, settings, "poll_interval", 30), "poll_interval")
    if poll > TIMEOUT_MAX / 1.2:
        raise ConfigError("worker poll_interval exceeds this platform's timer limit")
    maximum = _setting(args, settings, "max_owned", 3)
    if type(maximum) is not int or maximum <= 0:
        raise ConfigError("worker max_owned must be a positive integer")
    if lease > 86400 or renew >= lease:
        raise ConfigError(
            "worker lease must be at most 86400s and renew_every below lease"
        )
    handler = _handler(_text(_setting(args, settings, "handler", "echo"), "handler"))
    label = optional_text(args, "worker") or config.worker_label
    with WorkerControl(label, _root(args)) as control:
        if control.stop.is_set():
            return 0
        with client(args) as api:
            capacity = UnlimitedCapacity()
            loop = WorkerLoop(
                api,
                selected,
                topic,
                capacity,
                handler,
                lease=lease,
                renew_every=renew,
                max_owned=maximum,
                poll_interval=poll,
                stop=control.stop,
            )
            if flag(args, "once"):
                with capacity.acquire():
                    result = loop.step()
                output({"status": result})
            else:
                loop.run()
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("worker", help="Run an echo or plugin worker")
    connection_options(parser)
    parser.add_argument("--domain")
    parser.add_argument("--topic")
    parser.add_argument(
        "--handler", dest="worker_handler", help="echo or module:factory"
    )
    parser.add_argument("--lease", help="Lease duration, default 600s")
    parser.add_argument("--renew-every", help="Renew interval, default 180s")
    parser.add_argument("--max-owned", type=int)
    parser.add_argument("--poll-interval", help="Idle poll duration, default 30s")
    parser.add_argument("--once", action="store_true", help="Own at most one task")
    parser.add_argument("--state-dir", type=Path)
    parser.set_defaults(handler=_run)
    commands = parser.add_subparsers(dest="worker_command")
    stop = commands.add_parser("stop", help="Request a graceful stop without a token")
    connection_options(stop)
    stop.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
    stop.set_defaults(handler=_stop)
