"""Inspect, watch and prune the machine's durable local reply stores."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from threading import TIMEOUT_MAX
from typing import cast

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    domain,
    duration_seconds,
    flag,
    optional_text,
    output,
    requester_wait_seconds,
    state_dir,
    text,
)
from tasklease.cli.requester import agent_store
from tasklease.config import Config
from tasklease.errors import ConfigError, TaskleaseError
from tasklease.names import MAX_OWN_TOPICS, is_topic
from tasklease.store import Store
from tasklease.worker_control import WorkerControl, request_stop


def _inbox(args: argparse.Namespace) -> int:
    stores = (
        [Store(agent_id) for agent_id in Store.agents()]
        if flag(args, "all")
        else [agent_store(args)]
    )
    pruned = sum(store.prune() for store in stores) if flag(args, "prune") else None
    result: dict[str, object] = {
        "replies": [reply.to_dict() for store in stores for reply in store.inbox()]
    }
    if pruned is not None:
        result["pruned"] = pruned
    output(result)
    return 0


def _topics(args: argparse.Namespace) -> list[str]:
    raw: object = getattr(args, "topic", None)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OWN_TOPICS:
        raise ConfigError(
            f"--topic must specify 1 to {MAX_OWN_TOPICS} distinct topic names"
        )
    topics = cast(list[object], raw)
    result: list[str] = []
    for item in topics:
        if not isinstance(item, str) or not is_topic(item):
            raise ConfigError("topic must be a valid topic name")
        result.append(item)
    if len(set(result)) != len(result):
        raise ConfigError(
            f"--topic must specify 1 to {MAX_OWN_TOPICS} distinct topic names"
        )
    return result


def _watcher_label(args: argparse.Namespace, config: Config) -> str:
    return optional_text(args, "worker") or config.agent_id or config.worker_label


def _stop(args: argparse.Namespace) -> int:
    config = configuration(args)
    label = _watcher_label(args, config)
    path = request_stop(label, state_dir(args), namespace="watchers")
    output({"ok": True, "watcher": label, "stop_file": str(path)})
    return 0


def _watch(args: argparse.Namespace) -> int:
    timeout_text = optional_text(args, "timeout")
    timeout = (
        None if timeout_text is None else duration_seconds(timeout_text, name="Timeout")
    )
    poll_interval = duration_seconds(text(args, "poll_interval"), name="Poll interval")
    if poll_interval > TIMEOUT_MAX / 1.2:
        raise ConfigError("poll_interval exceeds this platform's timer limit")
    topics = _topics(args)
    stop_root = state_dir(args)
    config = configuration(args)
    selected = domain(args, config)
    wait = requester_wait_seconds(args, config=config)
    store = agent_store(args, config=config)
    watcher_label = _watcher_label(args, config)
    once = flag(args, "once")
    with WorkerControl(watcher_label, stop_root, namespace="watchers") as control:
        if control.stop.is_set():
            if once:
                output({"replies": [], "stopped": True})
            return 0
        with client(args, config=config) as api:
            records = store.watch(
                api,
                selected,
                topics,
                timeout_seconds=timeout,
                poll_interval=poll_interval,
                wait_seconds=wait,
                stop=control.stop,
                once=once,
            )
            if once:
                replies: list[dict[str, object]] = []
                try:
                    for record in records:
                        replies.append(record.to_dict())
                except TaskleaseError:
                    if replies:
                        output({"replies": replies})
                        sys.stdout.flush()
                    raise
                if replies:
                    output({"replies": replies})
                    return 0
                if control.stop.is_set():
                    output({"replies": [], "stopped": True})
                    return 0
                output({"replies": [], "timed_out": True})
                return 3
            for record in records:
                output(record.to_dict())
                sys.stdout.flush()
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("inbox", help="List replies saved on this machine")
    connection_options(parser)
    parser.add_argument("--all", action="store_true", help="Include every local agent")
    parser.add_argument(
        "--prune", action="store_true", help="Remove records over 7 days"
    )
    parser.set_defaults(handler=_inbox)
    commands = parser.add_subparsers(dest="inbox_command")
    watch = commands.add_parser(
        "watch", help="Watch 1 to 16 inbox topics and save replies locally"
    )
    connection_options(watch)
    watch.add_argument("--domain", help="Domain (default: TL_DOMAIN or config.domain)")
    watch.add_argument(
        "--topic",
        action="append",
        required=True,
        help="Inbox topic to drain (repeat for up to 16 topics)",
    )
    mode = watch.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Exit after the first non-empty arrival batch, printing JSON",
    )
    mode.add_argument(
        "--stream",
        action="store_true",
        help="Stream replies continuously as NDJSON (default)",
    )
    watch.add_argument("--timeout", help="Optional watch duration, e.g. 1h")
    watch.add_argument(
        "--wait-seconds",
        help="Long-poll seconds per Own, default 30 or config wait_seconds; "
        "0 or off disables",
    )
    watch.add_argument(
        "--poll-interval",
        default="1s",
        help="Initial backoff when long-polling is disabled or refused, default 1s",
    )
    watch.add_argument(
        "--state-dir",
        type=Path,
        help="Directory for inbox watcher stop files (default: store root)",
    )
    watch.set_defaults(handler=_watch)
    stop = commands.add_parser(
        "stop", help="Request a graceful inbox watch stop without a token"
    )
    connection_options(stop)
    stop.add_argument(
        "--state-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="Directory for inbox watcher stop files (default: store root)",
    )
    stop.set_defaults(handler=_stop)
