"""Read, move and prune tasks using their current fenced handles."""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterator

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    domain,
    duration_seconds,
    flag,
    optional_text,
    output,
    text,
)
from tasklease.client import Client
from tasklease.errors import ConfigError, Forbidden, NotFound
from tasklease.generated.protocol import TasksListRequest, TaskSummary
from tasklease.names import is_topic

_DEFAULT_QUEUE_LIMIT = 500


def _tasks(args: argparse.Namespace) -> int:
    config = configuration(args)
    request: TasksListRequest = {
        "domain": domain(args, config),
        "topic": text(args, "topic"),
    }
    cursor = optional_text(args, "cursor")
    if cursor is not None:
        request["cursor"] = cursor
    with client(args, config=config) as api:
        output(api.tasks_list(request))
    return 0


def _read(args: argparse.Namespace) -> int:
    config = configuration(args)
    selected = domain(args, config)
    with client(args, config=config) as api:
        output(
            api.read(
                {
                    "domain": selected,
                    "topic": text(args, "topic"),
                    "id": text(args, "id"),
                }
            )
        )
    return 0


def _delete(args: argparse.Namespace) -> int:
    config = configuration(args)
    selected = domain(args, config)
    with client(args, config=config) as api:
        output(
            api.delete(
                {
                    "domain": selected,
                    "topic": text(args, "topic"),
                    "id": text(args, "id"),
                }
            )
        )
    return 0


def _pollers(args: argparse.Namespace) -> int:
    config = configuration(args)
    selected = domain(args, config)
    with client(args, config=config) as api:
        output(
            api.topic_pollers(
                {
                    "domain": selected,
                    "topic": text(args, "topic"),
                }
            )
        )
    return 0


def _move(args: argparse.Namespace) -> int:
    config = configuration(args)
    selected, topic, task_id = (
        domain(args, config),
        text(args, "topic"),
        text(args, "id"),
    )
    with client(args, config=config) as api:
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
    return duration_seconds(value, name="Age") * 1000


def _task_pages(
    api: Client, selected: str, topic: str
) -> Iterator[tuple[list[TaskSummary], bool]]:
    request: TasksListRequest = {"domain": selected, "topic": topic}
    previous_cursor: str | None = None
    while True:
        page = api.tasks_list(request)
        page_tasks = page["tasks"]
        cursor = page.get("cursor")
        has_more = cursor is not None and bool(page_tasks) and cursor != previous_cursor
        yield page_tasks, has_more
        if not has_more:
            return
        assert cursor is not None
        previous_cursor = cursor
        request["cursor"] = cursor


def _pages(api: Client, selected: str, topic: str) -> Iterator[TaskSummary]:
    for page_tasks, _ in _task_pages(api, selected, topic):
        yield from page_tasks


def _prune(args: argparse.Namespace) -> int:
    config = configuration(args)
    selected = domain(args, config)
    cutoff = time.time_ns() // 1_000_000 - _age(text(args, "lease_older_than"))
    deleted = skipped = 0
    with client(args, config=config) as api:
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


def _task_state(task: TaskSummary, now_ms: int) -> str:
    server_state = task.get("state")
    if server_state == "held":
        return "held"
    if server_state == "available":
        return (
            "lapsed"
            if (task["owner"] is not None or task["owned_count"] > 0)
            else "available"
        )
    if task["owner"] is None and task["owned_count"] == 0:
        return "available"
    return "held" if task["lease_until"] > now_ms else "lapsed"


def _queue(args: argparse.Namespace) -> int:
    topic = text(args, "topic")
    if not is_topic(topic):
        raise ConfigError("topic must be a valid topic name")
    limit: object = getattr(args, "limit", _DEFAULT_QUEUE_LIMIT)
    if type(limit) is not int or limit <= 0:
        raise ConfigError("limit must be a positive integer")
    config = configuration(args)
    selected = domain(args, config)
    items: list[TaskSummary] = []
    truncated = False
    with client(args, config=config) as api:
        for page_tasks, has_more in _task_pages(api, selected, topic):
            remaining = limit - len(items)
            if len(page_tasks) > remaining:
                items.extend(page_tasks[:remaining])
                truncated = True
                break
            items.extend(page_tasks)
            if len(items) >= limit:
                truncated = has_more
                break
        pollers = api.topic_pollers({"domain": selected, "topic": topic})["pollers"]
    now_ms = time.time_ns() // 1_000_000
    counts = {"available": 0, "held": 0, "lapsed": 0}
    enriched: list[dict[str, object]] = []
    for task in items:
        state = _task_state(task, now_ms)
        counts[state] += 1
        enriched.append(
            {
                **task,
                "state": state,
                "age_seconds": max(0, (now_ms - task["created_at"]) // 1000),
            }
        )
    output(
        {
            "domain": selected,
            "topic": topic,
            "counts": {**counts, "total": len(enriched)},
            "truncated": truncated,
            "tasks": enriched,
            "pollers": pollers,
        }
    )
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
    queue = subparsers.add_parser(
        "queue", help="Inspect a topic's queued/held tasks and recent pollers"
    )
    connection_options(queue)
    queue.add_argument("--domain", help="Domain (default: TL_DOMAIN or config.domain)")
    queue.add_argument("topic")
    queue.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_QUEUE_LIMIT,
        help=f"Maximum tasks to fetch, default {_DEFAULT_QUEUE_LIMIT}",
    )
    queue.set_defaults(handler=_queue)
    prune = subparsers.add_parser("prune", help="Remove abandoned tasks by age")
    connection_options(prune)
    prune.add_argument("domain")
    prune.add_argument("--topic", required=True, help="Topic glob")
    prune.add_argument("--lease-older-than", required=True)
    prune.add_argument(
        "--all", action="store_true", help="Also include never-owned tasks"
    )
    prune.set_defaults(handler=_prune)
