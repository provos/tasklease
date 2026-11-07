"""Read, move and prune tasks using their current fenced handles."""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Iterator

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    domain,
    flag,
    optional_text,
    output,
    text,
)
from tasklease.client import Client
from tasklease.errors import ConfigError, Forbidden, NotFound
from tasklease.generated.protocol import TasksListRequest, TaskSummary


def _tasks(args: argparse.Namespace) -> int:
    request: TasksListRequest = {
        "domain": domain(args, configuration(args)),
        "topic": text(args, "topic"),
    }
    cursor = optional_text(args, "cursor")
    if cursor is not None:
        request["cursor"] = cursor
    with client(args) as api:
        output(api.tasks_list(request))
    return 0


def _read(args: argparse.Namespace) -> int:
    with client(args) as api:
        output(
            api.read(
                {
                    "domain": domain(args, configuration(args)),
                    "topic": text(args, "topic"),
                    "id": text(args, "id"),
                }
            )
        )
    return 0


def _delete(args: argparse.Namespace) -> int:
    with client(args) as api:
        output(
            api.delete(
                {
                    "domain": domain(args, configuration(args)),
                    "topic": text(args, "topic"),
                    "id": text(args, "id"),
                }
            )
        )
    return 0


def _pollers(args: argparse.Namespace) -> int:
    with client(args) as api:
        output(
            api.topic_pollers(
                {
                    "domain": domain(args, configuration(args)),
                    "topic": text(args, "topic"),
                }
            )
        )
    return 0


def _move(args: argparse.Namespace) -> int:
    selected, topic, task_id = (
        domain(args, configuration(args)),
        text(args, "topic"),
        text(args, "id"),
    )
    with client(args) as api:
        task = api.read({"domain": selected, "topic": topic, "id": task_id})["task"]
        output(
            api.consume(
                {
                    "domain": selected,
                    "topic": topic,
                    "id": task["id"],
                    "tasks": [{"topic": text(args, "to"), "data": task["data"]}],
                }
            )
        )
    return 0


def _age(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(s|m|h|d)", value)
    if not match or int(match[1]) <= 0:
        raise ConfigError(
            "Age must be positive seconds, minutes, hours or days, e.g. 3d"
        )
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]] * 1000


def _pages(api: Client, selected: str, topic: str) -> Iterator[TaskSummary]:
    request: TasksListRequest = {"domain": selected, "topic": topic}
    while True:
        page = api.tasks_list(request)
        yield from page["tasks"]
        cursor = page.get("cursor")
        if cursor is None:
            return
        request["cursor"] = cursor


def _prune(args: argparse.Namespace) -> int:
    selected = domain(args, configuration(args))
    cutoff = time.time_ns() // 1_000_000 - _age(text(args, "lease_older_than"))
    deleted = skipped = 0
    with client(args) as api:
        topics = api.topics_list({"domain": selected, "glob": text(args, "topic")})[
            "topics"
        ]
        for topic in topics:
            for task in _pages(api, selected, topic["topic"]):
                if task["lease_until"] >= cutoff or (
                    task["owned_count"] == 0 and not flag(args, "all")
                ):
                    continue
                try:
                    api.delete(
                        {
                            "domain": selected,
                            "topic": topic["topic"],
                            "id": task["id"],
                            "lease_before": cutoff,
                        }
                    )
                except (NotFound, Forbidden):
                    skipped += 1
                else:
                    deleted += 1
    output({"ok": True, "deleted": deleted, "skipped": skipped})
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    for name, handler in (
        ("tasks", _tasks),
        ("read", _read),
        ("delete", _delete),
        ("pollers", _pollers),
        ("move", _move),
    ):
        parser = subparsers.add_parser(name)
        connection_options(parser)
        parser.add_argument("domain")
        parser.add_argument("topic")
        if name in {"read", "delete", "move"}:
            parser.add_argument("id")
        if name == "tasks":
            parser.add_argument("--cursor")
        if name == "move":
            parser.add_argument("--to", required=True)
        parser.set_defaults(handler=handler)
    prune = subparsers.add_parser("prune", help="Remove abandoned tasks by age")
    connection_options(prune)
    prune.add_argument("domain")
    prune.add_argument("--topic", required=True, help="Topic glob")
    prune.add_argument("--lease-older-than", required=True)
    prune.add_argument(
        "--all", action="store_true", help="Also include never-owned tasks"
    )
    prune.set_defaults(handler=_prune)
