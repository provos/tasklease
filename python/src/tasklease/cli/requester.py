"""Durable requester commands and recovery after uncertain submissions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

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
    text,
)
from tasklease.client import Client, reject_nonfinite
from tasklease.errors import ConfigError, TaskleaseError
from tasklease.store import RequestRecord, Store


def agent_store(args: argparse.Namespace) -> Store:
    """Select a single agent without requiring any network configuration."""
    agent_id = configuration(args).agent_id
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
            "request_id": record.request_id,
            "task_id": record.task_id,
            "state": record.state,
        }
    )


def _report_recovery(store: Store, request_id: str) -> None:
    try:
        record = store.request(request_id)
    except (TaskleaseError, OSError, ValueError):
        return
    if record is None:
        return
    _status(record)
    if record.state == "submission_unknown":
        message(
            "Submission outcome is unknown. Resume with tl wait --request-id "
            f"{request_id}, or resend with tl request --resend {request_id}. "
            "Cancellation is unavailable until a task ID is saved."
        )
    else:
        message(
            f"Request {request_id} remains saved. "
            f"Resume with tl wait --request-id {request_id}."
        )


def _wait_for(store: Store, api: Client, request_id: str, timeout_seconds: int) -> int:
    reply = store.wait(api, request_id, timeout_seconds=timeout_seconds)
    if reply is None:
        output({"request_id": request_id, "timed_out": True})
        message(f"Timed out; request {request_id} remains saved for tl wait")
        return 3
    output({"request_id": request_id, "data": reply.data, "creator": reply.creator})
    return 0


def _request(args: argparse.Namespace) -> int:
    timeout = duration_seconds(text(args, "timeout"), name="Timeout")
    resend = optional_text(args, "resend")
    if resend is not None:
        if any(
            optional_text(args, name) is not None
            for name in ("domain", "topic", "reply_topic", "data", "request_id")
        ):
            raise ConfigError(
                "--resend uses the saved request; omit --domain, --topic, "
                "--reply-topic, --data and --request-id"
            )
        request_id = resend
        payload = None
        selected = topic = reply_topic = None
    else:
        selected = domain(args, configuration(args))
        topic = text(args, "topic")
        reply_topic = text(args, "reply_topic")
        payload = _data(text(args, "data"))
        supplied_id = optional_text(args, "request_id")
        request_id = uuid4().hex if supplied_id is None else supplied_id
    store = agent_store(args)
    with client(args) as api:
        try:
            if resend is not None:
                record = store.resend(api, request_id)
            else:
                assert selected is not None and topic is not None
                assert reply_topic is not None and payload is not None
                record = store.submit(
                    api,
                    selected,
                    topic,
                    reply_topic,
                    payload,
                    request_id=request_id,
                )
            if flag(args, "wait"):
                return _wait_for(store, api, request_id, timeout)
        except (TaskleaseError, OSError, ValueError, KeyboardInterrupt):
            _report_recovery(store, request_id)
            raise
        _status(record)
    return 0


def _wait(args: argparse.Namespace) -> int:
    timeout = duration_seconds(text(args, "timeout"), name="Timeout")
    store = agent_store(args)
    with client(args) as api:
        return _wait_for(store, api, text(args, "request_id"), timeout)


def _cancel(args: argparse.Namespace) -> int:
    store = agent_store(args)
    request_id = text(args, "request_id")
    with client(args) as api:
        result = store.cancel(api, request_id)
    output(
        {
            "request_id": request_id,
            "cancelled": result == "cancelled",
            "state": result,
        }
    )
    if result == "submission_unknown":
        message("Cannot cancel: the submission has no saved task ID; use tl wait")
        return 1
    if result == "not_found":
        message(
            "Cannot confirm cancellation: the task was already taken or removed. "
            "The request remains saved for tl wait."
        )
        return 1
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    request = subparsers.add_parser("request", help="Submit a durable agent request")
    connection_options(request)
    request.add_argument("--domain")
    request.add_argument("--topic")
    request.add_argument("--reply-topic")
    request.add_argument("--data", help="JSON object or @file")
    identifiers = request.add_mutually_exclusive_group()
    identifiers.add_argument("--request-id")
    identifiers.add_argument("--resend", metavar="REQUEST_ID")
    request.add_argument("--wait", action="store_true")
    request.add_argument("--timeout", default="6h", help="Wait duration, e.g. 6h")
    request.set_defaults(handler=_request)

    wait = subparsers.add_parser("wait", help="Wait for a saved request's reply")
    connection_options(wait)
    wait.add_argument("--request-id", required=True)
    wait.add_argument("--timeout", default="6h", help="Wait duration, e.g. 6h")
    wait.set_defaults(handler=_wait)

    cancel = subparsers.add_parser("cancel", help="Cancel a saved unowned request")
    connection_options(cancel)
    cancel.add_argument("--request-id", required=True)
    cancel.set_defaults(handler=_cancel)
