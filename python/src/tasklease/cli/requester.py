"""Requester commands correlated by server-carried origins."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    domain,
    duration_seconds,
    flag,
    message,
    optional_text,
    output,
    requester_wait_seconds,
    text,
)
from tasklease.client import Client, reject_nonfinite
from tasklease.config import Config
from tasklease.errors import ConfigError, NotFound, SubmissionError, TaskleaseError
from tasklease.names import is_task_id, is_topic
from tasklease.store import CancelResult, RequestRecord, Store


def agent_store(args: argparse.Namespace, *, config: Config | None = None) -> Store:
    """Select a single agent without requiring any network configuration."""
    if config is None:
        config = configuration(args)
    agent_id = config.agent_id
    if agent_id is None:
        raise ConfigError("Set agent_id in config.toml or TL_AGENT_ID")
    return Store(agent_id)


def _data(value: str) -> dict[str, object]:
    if value.startswith("@"):
        if len(value) == 1:
            raise ConfigError("--data @file requires a file path")
        try:
            value = Path(value[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ConfigError("Cannot read --data file") from None
    try:
        decoded: object = json.loads(value, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError("--data must contain a valid JSON object") from None
    if not isinstance(decoded, dict):
        raise ConfigError("--data must contain a JSON object")
    return cast(dict[str, object], decoded)


def _status(record: RequestRecord) -> None:
    output(
        {
            "origin_id": record.origin_id,
            "state": record.state,
        }
    )


def _report_recovery(store: Store, origin_id: str) -> None:
    try:
        record = store.request(origin_id)
    except (TaskleaseError, OSError, ValueError):
        return
    if record is None:
        return
    _status(record)
    message(
        f"Request {origin_id} remains saved. "
        f"Resume with tl wait --origin-id {origin_id}."
    )


def _wait_for(
    store: Store,
    api: Client,
    origin_id: str,
    timeout_seconds: int,
    wait_seconds: int,
) -> int:
    reply = store.wait(
        api, origin_id, timeout_seconds=timeout_seconds, wait_seconds=wait_seconds
    )
    if reply is None:
        output({"origin_id": origin_id, "timed_out": True})
        message(f"Timed out; request {origin_id} remains saved for tl wait")
        return 3
    output({"origin_id": origin_id, "data": reply.data, "creator": reply.creator})
    return 0


def _request(args: argparse.Namespace) -> int:
    timeout = duration_seconds(text(args, "timeout"), name="Timeout")
    config = configuration(args)
    # Validated before Create, but only when this request will wait.
    wait = requester_wait_seconds(args, config=config) if flag(args, "wait") else 0
    selected = domain(args, config)
    topic = text(args, "topic")
    reply_topic = text(args, "reply_topic")
    payload = _data(text(args, "data"))
    store = agent_store(args, config=config)
    with client(args, config=config) as api:
        try:
            record = store.submit(api, selected, topic, reply_topic, payload)
        except SubmissionError as error:
            message(
                f"Cancel the created request with tl cancel --domain {selected} "
                f"--topic {topic} --origin-id {error.origin_id}."
            )
            raise
        try:
            if flag(args, "wait"):
                return _wait_for(store, api, record.origin_id, timeout, wait)
        except (TaskleaseError, OSError, ValueError, KeyboardInterrupt):
            _report_recovery(store, record.origin_id)
            raise
        _status(record)
    return 0


def _wait(args: argparse.Namespace) -> int:
    timeout = duration_seconds(text(args, "timeout"), name="Timeout")
    config = configuration(args)
    wait = requester_wait_seconds(args, config=config)
    store = agent_store(args, config=config)
    with client(args, config=config) as api:
        return _wait_for(store, api, text(args, "origin_id"), timeout, wait)


def _cancel(args: argparse.Namespace) -> int:
    origin_id = text(args, "origin_id")
    topic = optional_text(args, "topic")
    if not is_task_id(origin_id):
        raise ConfigError("origin_id must be a valid task ID")
    if topic is not None and not is_topic(topic):
        raise ConfigError("topic must be a valid topic name")
    if topic is None and optional_text(args, "domain") is not None:
        raise ConfigError("--domain requires --topic for direct cancellation")
    config = configuration(args)
    selected = domain(args, config) if topic is not None else None
    store = agent_store(args, config=config) if topic is None else None
    result: CancelResult
    with client(args, config=config) as api:
        if store is not None:
            result = store.cancel(api, origin_id)
        else:
            assert topic is not None and selected is not None
            try:
                api.cancel({"domain": selected, "topic": topic, "origin_id": origin_id})
            except NotFound:
                result = "not_found"
            else:
                result = "cancelled"
    output(
        {"origin_id": origin_id, "cancelled": result == "cancelled", "state": result}
    )
    if result == "not_found":
        hint = " The request remains saved for tl wait." if store is not None else ""
        message(
            "Cannot confirm cancellation: the first task finished, was removed, "
            f"or moved to a later stage.{hint}"
        )
        return 1
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    request = subparsers.add_parser("request", help="Submit a durable agent request")
    connection_options(request)
    request.add_argument("--domain")
    request.add_argument("--topic", required=True)
    request.add_argument("--reply-topic", required=True)
    request.add_argument("--data", required=True, help="JSON object or @file")
    request.add_argument("--wait", action="store_true")
    request.add_argument("--timeout", default="6h", help="Wait duration, e.g. 6h")
    request.add_argument(
        "--wait-seconds",
        help="Long-poll seconds per Own, default 30 or config wait_seconds; "
        "0 or off disables",
    )
    request.set_defaults(handler=_request)

    wait = subparsers.add_parser("wait", help="Wait for a saved request's reply")
    connection_options(wait)
    wait.add_argument("--origin-id", required=True)
    wait.add_argument("--timeout", default="6h", help="Wait duration, e.g. 6h")
    wait.add_argument(
        "--wait-seconds",
        help="Long-poll seconds per Own, default 30 or config wait_seconds; "
        "0 or off disables",
    )
    wait.set_defaults(handler=_wait)

    cancel = subparsers.add_parser(
        "cancel", help="Cancel a queued or running first-stage request"
    )
    connection_options(cancel)
    cancel.add_argument("--origin-id", required=True)
    cancel.add_argument(
        "--topic", help="Cancel directly without a local request record"
    )
    cancel.add_argument("--domain")
    cancel.set_defaults(handler=_cancel)
