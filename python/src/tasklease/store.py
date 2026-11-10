"""Durable, immutable requester intents and a concurrent local reply inbox.

Publication is always no-replace. A lost network response can therefore leave
an unknown submission or an already saved reply, but never erase its evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from random import uniform
from typing import Literal, cast
from uuid import uuid4

from platformdirs import user_data_path

from tasklease import fsio
from tasklease.client import Client
from tasklease.errors import Empty, NotFound, StoreError, TransportError
from tasklease.generated import protocol as p
from tasklease.names import is_domain, is_request_id, is_segment, is_task_id, is_topic

type RequestState = Literal["submission_unknown", "pending", "answered"]
type CancelResult = Literal["cancelled", "not_found", "submission_unknown"]

_HASH = re.compile(r"[0-9a-f]{32}\Z")
_RETENTION_SECONDS = 7 * 86400
_BATCH_SIZE = 100


def default_root() -> Path:
    """The per-user data directory, independently of configuration location."""
    return user_data_path("tasklease")


def hash_id(value: str) -> str:
    """A portable, case-sensitive filename for an original UTF-8 identifier."""
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    except UnicodeError:
        raise StoreError("Identifiers must contain valid UTF-8 text") from None


def _request_id(value: object) -> str:
    if not is_request_id(value):
        raise StoreError("request_id must contain 1 to 128 valid UTF-8 characters")
    return value


def _name(value: object, *, domain: bool = False) -> str:
    if not (is_domain(value) if domain else is_topic(value)):
        raise StoreError("Invalid domain or topic in requester state")
    return cast(str, value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise StoreError("Invalid timestamp in requester state")
    return value


def _task_id(value: object) -> str:
    if not is_task_id(value):
        raise StoreError("Invalid task ID in requester state")
    return value


def _encode(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StoreError("Requester state must contain valid JSON values") from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise StoreError("Invalid object in requester state")
    return cast(dict[str, object], value)


def _read(path: Path) -> dict[str, object] | None:
    try:
        value = fsio.read_json(path)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise StoreError("Cannot read local requester state") from None
    return None if value is None else _object(value)


def _publish(path: Path, value: dict[str, object]) -> Literal["created", "exists"]:
    try:
        return fsio.publish_new(path, _encode(value))
    except OSError:
        raise StoreError("Cannot publish local requester state") from None


def _files(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        raise StoreError("Cannot list local requester state") from None


def _remove(path: Path) -> int:
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    except OSError:
        raise StoreError("Cannot remove local requester state") from None
    return 1


@dataclass(frozen=True)
class RequestRecord:
    """An original intent, with state derived from its companion files."""

    agent_id: str
    request_id: str
    domain: str
    topic: str
    reply_topic: str
    created_at: int
    data: dict[str, object]
    task_id: str | None = None
    state: RequestState = "submission_unknown"

    def _intent(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "domain": self.domain,
            "topic": self.topic,
            "reply_topic": self.reply_topic,
            "created_at": self.created_at,
            "data": self.data,
        }

    def as_dict(self) -> dict[str, object]:
        return dict(
            self._intent(),
            agent_id=self.agent_id,
            task_id=self.task_id,
            state=self.state,
        )

    def to_dict(self) -> dict[str, object]:
        return self.as_dict()


@dataclass(frozen=True)
class ReplyRecord:
    """The first durably published reply for a correlation ID."""

    agent_id: str
    task_id: str
    topic: str
    creator: str
    created_at: int
    received_at: int
    data: dict[str, object]
    request_id: str | None

    def _saved(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "topic": self.topic,
            "creator": self.creator,
            "created_at": self.created_at,
            "received_at": self.received_at,
            "data": self.data,
        }

    def as_dict(self) -> dict[str, object]:
        return dict(self._saved(), agent_id=self.agent_id, request_id=self.request_id)

    def to_dict(self) -> dict[str, object]:
        return self.as_dict()


class Store:
    """One agent's requester state; independent processes need no drain lock.

    ``initialize=False`` locates an existing agent without publishing metadata.
    It is useful to readers such as the monitor; they must use read methods.
    """

    def __init__(
        self, agent_id: str, root: Path | None = None, *, initialize: bool = True
    ) -> None:
        if not is_segment(agent_id):
            raise StoreError("agent_id must be a topic segment of 1 to 128 characters")
        self.agent_id = agent_id
        self.root = (default_root() if root is None else root).expanduser().absolute()
        self.request_directory = self.root / "requests" / hash_id(agent_id)
        self.reply_directory = self.root / "replies" / hash_id(agent_id)
        self.unmatched_directory = self.reply_directory / "unmatched"
        found_metadata = False
        for directory in (self.request_directory, self.reply_directory):
            path = directory / "agent.json"
            metadata = _read(path)
            if initialize and metadata is None:
                _publish(path, {"agent_id": agent_id})
                metadata = _read(path)
            if not initialize and metadata is None and not directory.exists():
                continue
            if metadata != {"agent_id": agent_id}:
                raise StoreError("Agent metadata does not match its storage directory")
            found_metadata = True
        if not found_metadata:
            raise StoreError("No existing metadata for this agent")

    def _request_path(self, request_id: str) -> Path:
        return self.request_directory / f"{hash_id(_request_id(request_id))}.json"

    def _task_path(self, request_id: str) -> Path:
        return self.request_directory / f"{hash_id(_request_id(request_id))}.task.json"

    def _reply_path(self, request_id: str) -> Path:
        return self.reply_directory / f"{hash_id(_request_id(request_id))}.json"

    def _parse_intent(self, value: dict[str, object]) -> RequestRecord:
        request_id = _request_id(value.get("request_id"))
        reply_topic = _name(value.get("reply_topic"))
        data = _object(value.get("data"))
        if (
            data.get("request_id") != request_id
            or data.get("reply_topic") != reply_topic
        ):
            raise StoreError("Request payload does not match its saved coordinates")
        return RequestRecord(
            self.agent_id,
            request_id,
            _name(value.get("domain"), domain=True),
            _name(value.get("topic")),
            reply_topic,
            _integer(value.get("created_at")),
            data,
        )

    def _parse_reply(self, value: dict[str, object]) -> ReplyRecord:
        data = _object(value.get("data"))
        correlation = data.get("request_id")
        request_id = correlation if is_request_id(correlation) else None
        creator = value.get("creator")
        if not isinstance(creator, str) or not creator:
            raise StoreError("Invalid reply creator in requester state")
        return ReplyRecord(
            self.agent_id,
            _task_id(value.get("task_id")),
            _name(value.get("topic")),
            creator,
            _integer(value.get("created_at")),
            _integer(value.get("received_at")),
            data,
            request_id,
        )

    def reply(self, request_id: str) -> ReplyRecord | None:
        value = _read(self._reply_path(request_id))
        if value is None:
            return None
        result = self._parse_reply(value)
        if result.request_id != request_id:
            raise StoreError("Reply correlation does not match its storage name")
        return result

    def request(self, request_id: str) -> RequestRecord | None:
        value = _read(self._request_path(request_id))
        if value is None:
            return None
        record = self._parse_intent(value)
        if record.request_id != request_id:
            raise StoreError("Request correlation does not match its storage name")
        return self._request_state(record)

    def _request_state(self, record: RequestRecord) -> RequestRecord:
        sidecar = _read(self._task_path(record.request_id))
        task_id = None if sidecar is None else _task_id(sidecar.get("task_id"))
        state: RequestState = "submission_unknown" if task_id is None else "pending"
        if self.reply(record.request_id) is not None:
            state = "answered"
        return replace(record, task_id=task_id, state=state)

    def _required_request(self, request_id: str) -> RequestRecord:
        record = self.request(request_id)
        if record is None:
            raise StoreError("No saved request for this request_id")
        return record

    def _send_intent(self, client: Client, record: RequestRecord) -> RequestRecord:
        response = client.create(
            {
                "domain": record.domain,
                "topic": record.topic,
                "data": cast(p.Data, record.data),
            }
        )
        task_id = _task_id(response["id"])
        path = self._task_path(record.request_id)
        _publish(path, {"task_id": task_id})
        saved = self._required_request(record.request_id)
        if saved.task_id is None:
            raise StoreError("Saved task ID disappeared after publication")
        return saved

    def submit(
        self,
        client: Client,
        domain: str,
        topic: str,
        reply_topic: str,
        data: Mapping[str, object],
        request_id: str | None = None,
    ) -> RequestRecord:
        """Publish the exact intent before Create; duplicate IDs never repost."""
        request_id = _request_id(uuid4().hex if request_id is None else request_id)
        domain, topic, reply_topic = (
            _name(domain, domain=True),
            _name(topic),
            _name(reply_topic),
        )
        payload = _object(dict(data))
        for key, expected in (("request_id", request_id), ("reply_topic", reply_topic)):
            if key in payload and payload[key] != expected:
                raise StoreError("Request identifying fields conflict with the command")
            payload[key] = expected
        # Round-trip once so caller mutation cannot change the saved/sent payload.
        payload = _object(json.loads(_encode(payload)))
        record = RequestRecord(
            self.agent_id,
            request_id,
            domain,
            topic,
            reply_topic,
            int(time.time() * 1000),
            payload,
        )
        if self.request(request_id) is None and (
            self.reply(request_id) is not None
            or _read(self._task_path(request_id)) is not None
        ):
            raise StoreError("request_id already has saved reply or task metadata")
        path = self._request_path(request_id)
        if _publish(path, record._intent()) == "exists":
            saved = self._required_request(request_id)
            if (saved.domain, saved.topic, saved.reply_topic) != (
                domain,
                topic,
                reply_topic,
            ) or _encode(saved.data) != _encode(payload):
                raise StoreError("request_id already belongs to a different request")
            return saved
        return self._send_intent(client, record)

    def resend(self, client: Client, request_id: str) -> RequestRecord:
        """Explicitly retry an unknown submission using only its saved payload."""
        record = self._required_request(request_id)
        if record.state == "answered":
            return record
        if record.state != "submission_unknown":
            raise StoreError("Request already has a known pending task; wait for it")
        return self._send_intent(client, record)

    def cancel(self, client: Client, request_id: str) -> CancelResult:
        record = self._required_request(request_id)
        if record.task_id is None:
            return "submission_unknown"
        try:
            client.delete(
                {"domain": record.domain, "topic": record.topic, "id": record.task_id}
            )
        except NotFound:
            return "not_found"
        _remove(self._request_path(request_id))
        _remove(self._task_path(request_id))
        return "cancelled"

    def _save_reply(self, task: p.Task, domain: str, reply_topic: str) -> ReplyRecord:
        if task.get("domain") != domain or task.get("topic") != reply_topic:
            raise StoreError("Owned reply does not match the requested topic")
        record = self._parse_reply(
            {
                "task_id": task.get("id"),
                "topic": task.get("topic"),
                "creator": task.get("creator"),
                "created_at": task.get("created_at"),
                "received_at": int(time.time() * 1000),
                "data": task.get("data"),
            }
        )
        path = (
            self._reply_path(record.request_id)
            if record.request_id is not None
            else self.unmatched_directory / f"{record.task_id}.json"
        )
        if _publish(path, record._saved()) == "created":
            return record
        # An existing file is safe to acknowledge only when it is readable and
        # really represents this correlation (including the unlikely hash collision).
        existing = _read(path)
        if existing is None:
            raise StoreError("Saved reply disappeared after publication")
        previous = self._parse_reply(existing)
        if previous.request_id != record.request_id or (
            record.request_id is None and previous.task_id != record.task_id
        ):
            raise StoreError("Reply correlation does not match its storage name")
        return previous

    def _drain(
        self,
        client: Client,
        domain: str,
        reply_topic: str,
        *,
        deadline: float | None = None,
        request_id: str | None = None,
    ) -> int:
        count = 0
        for _ in range(_BATCH_SIZE):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                response = client.own(
                    {"domain": domain, "topic": reply_topic, "hold_seconds": 60},
                    timeout=remaining,
                )
            except Empty:
                break
            task = response["task"]
            saved = self._save_reply(task, domain, reply_topic)
            count += 1
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                client.delete(
                    {"domain": domain, "topic": reply_topic, "id": task["id"]},
                    timeout=remaining,
                )
            except NotFound:
                pass  # The durable local reply already survives ID rotation.
            if request_id is not None and saved.request_id == request_id:
                break
        return count

    def drain(self, client: Client, domain: str, reply_topic: str) -> int:
        """Drain a bounded batch, durably saving every reply before Delete."""
        domain, reply_topic = _name(domain, domain=True), _name(reply_topic)
        self._automatic_prune()
        return self._drain(client, domain, reply_topic)

    def wait(
        self,
        client: Client,
        request_id: str,
        timeout_seconds: float,
        poll_interval: float = 1.0,
        max_poll_interval: float = 30.0,
    ) -> ReplyRecord | None:
        """Return a local reply, or None at a monotonic timeout; never repost.

        Polls start at ``poll_interval`` and back off by half each round up to
        ``max_poll_interval``: a quick reply is seen within about a second,
        while a multi-hour wait makes a few hundred requests, not thousands.
        """
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise StoreError("timeout must be a nonnegative finite number of seconds")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise StoreError(
                "poll interval must be a positive finite number of seconds"
            )
        if not math.isfinite(max_poll_interval) or max_poll_interval <= 0:
            raise StoreError(
                "maximum poll interval must be a positive finite number of seconds"
            )
        delay = poll_interval
        deadline = time.monotonic() + timeout_seconds
        reply = self.reply(request_id)
        if reply is not None:
            return reply
        record = self._required_request(request_id)
        self._automatic_prune()
        while time.monotonic() < deadline:
            try:
                self._drain(
                    client,
                    record.domain,
                    record.reply_topic,
                    deadline=deadline,
                    request_id=request_id,
                )
            except TransportError:
                reply = self.reply(request_id)
                if reply is not None:
                    return reply
                if time.monotonic() >= deadline:
                    return None
                raise
            reply = self.reply(request_id)
            if reply is not None:
                return reply
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, delay * uniform(0.8, 1.2)))
            delay = min(delay * 1.5, max(poll_interval, max_poll_interval))
        return None

    def inbox(self) -> list[ReplyRecord]:
        result: list[ReplyRecord] = []
        for directory in (self.reply_directory, self.unmatched_directory):
            for path in _files(directory):
                if (
                    path.name == "agent.json"
                    or not path.name.endswith(".json")
                    or path.name.startswith(".")
                ):
                    continue
                value = _read(path)
                if value is None:
                    continue
                record = self._parse_reply(value)
                expected = (
                    f"{record.task_id}.json"
                    if record.request_id is None
                    else f"{hash_id(record.request_id)}.json"
                )
                if path.name != expected or (directory == self.unmatched_directory) != (
                    record.request_id is None
                ):
                    raise StoreError(
                        "Reply correlation does not match its storage name"
                    )
                result.append(record)
        return sorted(result, key=lambda record: (record.received_at, record.task_id))

    def requests(self) -> list[RequestRecord]:
        return sorted(
            (self._request_state(record) for record in self._intents()),
            key=lambda record: (record.created_at, record.request_id),
        )

    def _intents(self) -> list[RequestRecord]:
        result: list[RequestRecord] = []
        for path in _files(self.request_directory):
            if _HASH.fullmatch(path.stem) is None or path.suffix != ".json":
                continue
            value = _read(path)
            if value is None:
                continue
            record = self._parse_intent(value)
            if path != self._request_path(record.request_id):
                raise StoreError("Request correlation does not match its storage name")
            result.append(record)
        return result

    def prune(self) -> int:
        """Remove expired records and orphaned publication temporaries."""
        now = time.time()
        cutoff = int((now - _RETENTION_SECONDS) * 1000)
        count = 0
        for record in self._intents():
            if record.created_at < cutoff:
                count += _remove(self._request_path(record.request_id))
                count += _remove(self._task_path(record.request_id))
        for path in _files(self.request_directory):
            if not path.name.endswith(".task.json"):
                continue
            stem = path.name.removesuffix(".task.json")
            if _HASH.fullmatch(stem) is None:
                continue
            if _read(self.request_directory / f"{stem}.json") is not None:
                continue
            try:
                expired = path.stat().st_mtime < now - _RETENTION_SECONDS
            except FileNotFoundError:
                continue
            except OSError:
                raise StoreError(
                    "Cannot inspect orphaned local task metadata"
                ) from None
            if expired:
                value = _read(path)
                if value is not None:
                    _task_id(value.get("task_id"))
                    count += _remove(path)
        for reply in self.inbox():
            if reply.received_at < cutoff:
                path = (
                    self._reply_path(reply.request_id)
                    if reply.request_id is not None
                    else self.unmatched_directory / f"{reply.task_id}.json"
                )
                count += _remove(path)
        for directory in (
            self.request_directory,
            self.reply_directory,
            self.unmatched_directory,
        ):
            try:
                count += fsio.cleanup_temporary_files(directory, before=now - 86400)
            except OSError:
                raise StoreError("Cannot prune local publication temporaries") from None
        for path in _files(self.reply_directory):
            if path.name.startswith(".prune-") and path.suffix == ".json":
                value = _read(path)
                if value is not None and _integer(value.get("created_at")) < cutoff:
                    count += _remove(path)
        return count

    def _automatic_prune(self) -> None:
        now = time.time()
        # The immutable daily claim elects one process without a drain lock.
        day = int(now // 86400)
        path = self.reply_directory / f".prune-{day}.json"
        # Checking first skips a full publication on every call after the first.
        if (
            not path.exists()
            and _publish(path, {"created_at": int(now * 1000)}) == "created"
        ):
            self.prune()

    @classmethod
    def agents(cls, root: Path | None = None) -> list[str]:
        """Find agent IDs through metadata, without a client or configuration."""
        base = default_root() if root is None else root
        agents: set[str] = set()
        for kind in ("requests", "replies"):
            for directory in _files(base / kind):
                if _HASH.fullmatch(directory.name) is None:
                    continue
                value = _read(directory / "agent.json")
                if value is None:
                    raise StoreError(
                        "Agent metadata is missing from a storage directory"
                    )
                agent_id = value.get("agent_id")
                if not is_segment(agent_id) or hash_id(agent_id) != directory.name:
                    raise StoreError(
                        "Agent metadata does not match its storage directory"
                    )
                agents.add(agent_id)
        return sorted(agents)

    @classmethod
    def prune_all(cls, root: Path | None = None) -> int:
        return sum(cls(agent_id, root).prune() for agent_id in cls.agents(root))
