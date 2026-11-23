"""Immutable requester records and replies keyed by server-carried origins.

Create chooses each request's origin before its local record is published.
Replies remain independently readable when their request record is absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import sys
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from os import stat_result
from pathlib import Path
from random import uniform
from threading import Event
from typing import Literal, cast

from platformdirs import user_data_path

from tasklease import fsio
from tasklease.client import MAX_WAIT_SECONDS, Client
from tasklease.errors import (
    Empty,
    NotFound,
    ReplyStateError,
    StoreError,
    SubmissionError,
    TransportError,
)
from tasklease.generated import protocol as p
from tasklease.names import (
    MAX_OWN_TOPICS,
    is_domain,
    is_idempotency_key,
    is_segment,
    is_task_id,
    is_topic,
)

type RequestState = Literal["pending", "answered"]
type CancelResult = Literal["cancelled", "not_found"]

_HASH = re.compile(r"[0-9a-f]{32}\Z")
_DUPLICATE = re.compile(r"t_[0-7][0-9A-HJKMNP-TV-Z]{25}-[0-9a-f]{64}\Z")
_RETENTION_SECONDS = 7 * 86400
_BATCH_SIZE = 100
# Seconds a long poll leaves for its answer before the overall deadline.
_WAIT_MARGIN = 5.0
# Seconds of consecutive transport failures a wait rides out before raising.
_TRANSPORT_PATIENCE = 300.0


def default_root() -> Path:
    """The per-user data directory, independently of configuration location.

    On Windows, prefer the legacy doubled folder whenever it already exists and
    never auto-move it, so concurrent processes cannot split worker locks, stop
    files or local reply stores across two directories.
    """
    preferred = user_data_path("tasklease", appauthor=False)
    if sys.platform == "win32":
        legacy = user_data_path("tasklease")
        if legacy != preferred:
            try:
                use_legacy = legacy.is_dir()
            except OSError:
                raise StoreError("Cannot inspect local requester state") from None
            if use_legacy:
                return legacy
    return preferred


def hash_id(value: str) -> str:
    """A portable, case-sensitive filename for an original UTF-8 identifier."""
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    except UnicodeError:
        raise StoreError("Identifiers must contain valid UTF-8 text") from None


def _name(value: object, *, domain: bool = False) -> str:
    if not (is_domain(value) if domain else is_topic(value)):
        raise StoreError("Invalid domain or topic in requester state")
    return cast(str, value)


def _topics(reply_topic: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(reply_topic, str):
        return (_name(reply_topic),)
    if isinstance(reply_topic, (bytes, bytearray)) or not isinstance(
        reply_topic, Sequence
    ):
        raise StoreError("reply_topic must be a topic name or sequence of topic names")
    if not 1 <= len(reply_topic) <= MAX_OWN_TOPICS:
        raise StoreError(
            f"reply_topic must contain 1 to {MAX_OWN_TOPICS} distinct topic names"
        )
    topics = tuple(_name(topic) for topic in reply_topic)
    if len(set(topics)) != len(topics):
        raise StoreError(
            f"reply_topic must contain 1 to {MAX_OWN_TOPICS} distinct topic names"
        )
    return topics


def _check_wait(wait_seconds: object) -> None:
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise StoreError(
            f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}"
        )


def _check_timeout(timeout_seconds: float | None) -> None:
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds < 0
    ):
        raise StoreError("timeout must be a nonnegative finite number of seconds")


def _check_poll_intervals(
    poll_interval: float, max_poll_interval: float, wait_seconds: int
) -> None:
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise StoreError("poll interval must be a positive finite number of seconds")
    if not math.isfinite(max_poll_interval) or max_poll_interval <= 0:
        raise StoreError(
            "maximum poll interval must be a positive finite number of seconds"
        )
    _check_wait(wait_seconds)


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


def _stat(path: Path) -> stat_result | None:
    try:
        return path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StoreError("Cannot inspect local requester state") from None


def _exists(path: Path) -> bool:
    return _stat(path) is not None


def _is_file(path: Path) -> bool:
    result = _stat(path)
    return result is not None and stat.S_ISREG(result.st_mode)


def _records(
    directory: Path, *, duplicate: bool = False
) -> Iterator[tuple[Path, dict[str, object]]]:
    for path in _files(directory):
        if path.name == "agent.json" or path.name.startswith("."):
            continue
        if path.suffix.casefold() != ".json" and path.name.casefold() != "unmatched":
            continue
        valid_name = (
            bool(_DUPLICATE.fullmatch(path.stem))
            if duplicate
            else is_task_id(path.stem)
        )
        if path.suffix != ".json" or not valid_name:
            raise StoreError(
                "Requester record has an invalid storage name; "
                "archive/reset legacy Store directories before using tasklease 0.5.0"
            )
        value = _read(path)
        if value is not None:
            yield path, value


@dataclass(frozen=True)
class RequestRecord:
    """A created request, with state derived from its saved reply."""

    agent_id: str
    origin_id: str
    domain: str
    topic: str
    reply_topic: str
    created_at: int
    data: dict[str, object]
    state: RequestState = "pending"

    def _intent(self) -> dict[str, object]:
        return {
            "origin_id": self.origin_id,
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
            state=self.state,
        )

    def to_dict(self) -> dict[str, object]:
        return self.as_dict()


@dataclass(frozen=True)
class DrainResult:
    """What one :meth:`Store.poll` saved, and how to schedule the next.

    Every entry in ``replies`` was durably published locally before its server
    copy was deleted (save-then-delete), so a crash never loses a delivered
    reply.
    """

    saved: int
    """Replies durably saved by this poll."""
    poll_again: bool
    """True to poll again at once: replies arrived, or the service held the
    wait. False after an immediate empty, including a refused wait
    (``Empty.wait_refused``): sleep the caller's interval first."""
    replies: tuple[ReplyRecord, ...] = field(default=(), compare=False)
    """Replies durably saved during this poll, in arrival order."""


@dataclass(frozen=True)
class ReplyRecord:
    """A selected reply or an archived conflicting observation for an origin."""

    agent_id: str
    task_id: str
    origin_id: str
    topic: str
    creator: str
    created_at: int
    received_at: int
    data: dict[str, object]
    matched: bool = False
    duplicate: bool = False

    def _content(self) -> dict[str, object]:
        return {
            "topic": self.topic,
            "creator": self.creator,
            "created_at": self.created_at,
            "data": self.data,
        }

    def _saved(self) -> dict[str, object]:
        return {
            **self._content(),
            "origin_id": self.origin_id,
            "task_id": self.task_id,
            "received_at": self.received_at,
        }

    def as_dict(self) -> dict[str, object]:
        return dict(
            self._saved(),
            agent_id=self.agent_id,
            matched=self.matched,
            duplicate=self.duplicate,
        )

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
        self.duplicate_directory = self.reply_directory / "duplicates"
        found_metadata = False
        for directory in (self.request_directory, self.reply_directory):
            path = directory / "agent.json"
            metadata = _read(path)
            if initialize and metadata is None:
                _publish(path, {"agent_id": agent_id})
                metadata = _read(path)
            if not initialize and metadata is None and not _exists(directory):
                continue
            if metadata != {"agent_id": agent_id}:
                raise StoreError("Agent metadata does not match its storage directory")
            found_metadata = True
        if not found_metadata:
            raise StoreError("No existing metadata for this agent")

    def _request_path(self, origin_id: str) -> Path:
        return self.request_directory / f"{_task_id(origin_id)}.json"

    def _reply_path(self, origin_id: str) -> Path:
        return self.reply_directory / f"{_task_id(origin_id)}.json"

    def _duplicate_path(self, origin_id: str, content: bytes) -> Path:
        fingerprint = hashlib.sha256(content).hexdigest()
        return self.duplicate_directory / f"{origin_id}-{fingerprint}.json"

    def _parse_intent(self, value: dict[str, object]) -> RequestRecord:
        origin_id = _task_id(value.get("origin_id"))
        reply_topic = _name(value.get("reply_topic"))
        data = _object(value.get("data"))
        if data.get("reply_topic") != reply_topic:
            raise StoreError("Request payload does not match its saved coordinates")
        return RequestRecord(
            self.agent_id,
            origin_id,
            _name(value.get("domain"), domain=True),
            _name(value.get("topic")),
            reply_topic,
            _integer(value.get("created_at")),
            data,
        )

    def _parse_reply(
        self, value: dict[str, object], *, duplicate: bool = False
    ) -> ReplyRecord:
        data = _object(value.get("data"))
        origin_id = _task_id(value.get("origin_id"))
        creator = value.get("creator")
        if not isinstance(creator, str) or not creator:
            raise StoreError("Invalid reply creator in requester state")
        return ReplyRecord(
            agent_id=self.agent_id,
            task_id=_task_id(value.get("task_id")),
            origin_id=origin_id,
            topic=_name(value.get("topic")),
            creator=creator,
            created_at=_integer(value.get("created_at")),
            received_at=_integer(value.get("received_at")),
            data=data,
            matched=_is_file(self._request_path(origin_id)),
            duplicate=duplicate,
        )

    def reply(self, origin_id: str) -> ReplyRecord | None:
        value = _read(self._reply_path(origin_id))
        if value is None:
            return None
        result = self._parse_reply(value)
        if result.origin_id != origin_id:
            raise StoreError("Reply correlation does not match its storage name")
        return result

    def request(self, origin_id: str) -> RequestRecord | None:
        record = self._request_intent(origin_id)
        return None if record is None else self._request_state(record)

    def _request_intent(self, origin_id: str) -> RequestRecord | None:
        value = _read(self._request_path(origin_id))
        if value is None:
            return None
        record = self._parse_intent(value)
        if record.origin_id != origin_id:
            raise StoreError("Request correlation does not match its storage name")
        return record

    def _request_state(self, record: RequestRecord) -> RequestRecord:
        state: RequestState = (
            "answered" if self.reply(record.origin_id) is not None else "pending"
        )
        return replace(record, state=state)

    def _required_request(self, origin_id: str) -> RequestRecord:
        record = self.request(origin_id)
        if record is None:
            raise StoreError("No saved request for this origin_id")
        return record

    def submit(
        self,
        client: Client,
        domain: str,
        topic: str,
        reply_topic: str,
        data: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> RequestRecord:
        """Create a new origin, then durably publish its local request record.

        Reuse an idempotency_key within seven days to recover an uncertain Create.
        Without a key, retrying creates another origin. Keyed replays preserve
        the first saved intent and its timestamp.
        Intent persistence failures raise SubmissionError with the known origin.
        Later reply inspection failures raise ReplyStateError for that saved origin.
        """
        domain, topic, reply_topic = (
            _name(domain, domain=True),
            _name(topic),
            _name(reply_topic),
        )
        if idempotency_key is not None and not is_idempotency_key(idempotency_key):
            raise StoreError("idempotency_key must be a canonical lowercase UUIDv7")
        payload = _object(dict(data))
        if "reply_topic" in payload and payload["reply_topic"] != reply_topic:
            raise StoreError("Request reply_topic conflicts with the command")
        payload["reply_topic"] = reply_topic
        # Round-trip once so caller mutation cannot change the saved/sent payload.
        payload = _object(json.loads(_encode(payload)))
        created_at = int(time.time() * 1000)
        request: p.CreateRequest = {
            "domain": domain,
            "topic": topic,
            "data": cast(p.Data, payload),
        }
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        response = client.create(request)
        origin_id = _task_id(response["id"])
        record = RequestRecord(
            self.agent_id, origin_id, domain, topic, reply_topic, created_at, payload
        )
        try:
            result = _publish(self._request_path(origin_id), record._intent())
            saved = self._request_intent(origin_id)
            if saved is None:
                raise StoreError("Saved request disappeared after publication")
            if saved.domain != domain:
                raise StoreError("Saved request belongs to another domain")
            if (
                result == "exists"
                and idempotency_key is None
                and _encode(saved._intent()) != _encode(record._intent())
            ):
                raise StoreError("Origin already belongs to another saved request")
        except StoreError as error:
            raise SubmissionError(origin_id) from error
        try:
            return self._request_state(saved)
        except StoreError:
            raise ReplyStateError(origin_id) from None

    def cancel(self, client: Client, origin_id: str) -> CancelResult:
        """Cancel the initial task, even after Own has rotated its fencing ID."""
        record = self._required_request(origin_id)
        try:
            client.cancel(
                {"domain": record.domain, "topic": record.topic, "origin_id": origin_id}
            )
        except NotFound:
            return "not_found"
        _remove(self._request_path(origin_id))
        return "cancelled"

    def _save_reply(
        self, task: p.Task, domain: str, reply_topic: str | Collection[str]
    ) -> ReplyRecord:
        allowed = {reply_topic} if isinstance(reply_topic, str) else set(reply_topic)
        topic = task.get("topic")
        if (
            task.get("domain") != domain
            or not isinstance(topic, str)
            or topic not in allowed
        ):
            raise StoreError("Owned reply does not match the requested topic")
        record = self._parse_reply(
            {
                "task_id": task.get("id"),
                "origin_id": task.get("origin_id"),
                "topic": task.get("topic"),
                "creator": task.get("creator"),
                "created_at": task.get("created_at"),
                "received_at": int(time.time() * 1000),
                "data": task.get("data"),
            }
        )
        path = self._reply_path(record.origin_id)
        if _publish(path, record._saved()) == "created":
            return record
        # A duplicate is safe to acknowledge only after validating the saved reply.
        previous = self.reply(record.origin_id)
        if previous is None:
            raise StoreError("Saved reply disappeared after publication")
        content = _encode(record._content())
        if _encode(previous._content()) != content:
            archive = self._duplicate_path(record.origin_id, content)
            if _publish(archive, record._saved()) == "created":
                return replace(record, duplicate=True)
            value = _read(archive)
            if value is None:
                raise StoreError("Archived reply disappeared after publication")
            existing = self._parse_reply(value, duplicate=True)
            if (
                existing.origin_id != record.origin_id
                or _encode(existing._content()) != content
            ):
                raise StoreError(
                    "Archived reply content does not match its storage name"
                )
            return existing
        return previous

    def _drain(
        self,
        client: Client,
        domain: str,
        reply_topics: tuple[str, ...],
        *,
        deadline: float | None = None,
        origin_id: str | None = None,
        wait_seconds: int = 0,
        stop: Event | None = None,
        saved_out: list[ReplyRecord] | None = None,
    ) -> tuple[list[ReplyRecord], bool]:
        """Save replies locally before deleting them; report whether to poll again.

        Each reply is durably published to disk before its server copy is
        deleted, so a crash or lost delete never loses a message. Polling again
        at once is right after replies arrived or after the service held a long
        poll. An immediate empty, including a wait the service refused
        (``Empty.wait_refused``), calls for the caller's backoff instead.
        """
        records: list[ReplyRecord] = [] if saved_out is None else saved_out
        initial = len(records)
        for index in range(_BATCH_SIZE):
            if stop is not None and stop.is_set():
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            # Only the first Own in a drain may wait: a reply that existed
            # before the call must not be delayed behind a long poll.
            wait = 0 if index else wait_seconds
            if remaining is not None and wait:
                # Leave the response a margin inside the deadline, which also
                # caps the read timeout: a reply claimed at the end of a wait
                # must still arrive rather than stay held unseen.
                wait = max(0, min(wait, math.floor(remaining - _WAIT_MARGIN)))
            claim: p.OwnRequest = {"domain": domain, "hold_seconds": 60}
            if len(reply_topics) == 1:
                claim["topic"] = reply_topics[0]
            else:
                claim["topics"] = list(reply_topics)
            if wait:
                claim["wait_seconds"] = wait
            sent = time.monotonic()
            try:
                response = client.own(claim, timeout=remaining)
            except Empty as error:
                # The service's hint marks a refused wait. Requiring half the
                # wait to have passed also guards against a server that answers
                # a wait at once without holding it, which must not spin.
                held = (
                    wait > 0
                    and not error.wait_refused
                    and time.monotonic() - sent >= wait / 2
                )
                return records, len(records) > initial or held
            task = response["task"]
            saved = self._save_reply(task, domain, reply_topics)
            records.append(saved)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                client.delete(
                    {"domain": domain, "topic": task["topic"], "id": task["id"]},
                    timeout=remaining,
                )
            except NotFound:
                pass  # The durable local reply already survives ID rotation.
            if origin_id is not None and saved.origin_id == origin_id:
                break
        return records, len(records) > initial

    def _poll_batches(
        self,
        client: Client,
        domain: str,
        topics: tuple[str, ...],
        *,
        deadline: float | None,
        poll_interval: float,
        max_poll_interval: float,
        wait_seconds: int,
        origin_id: str | None = None,
        stop: Event | None = None,
    ) -> Iterator[list[ReplyRecord]]:
        """Shared drain, transport-patience and backoff loop for wait and watch."""
        delay = poll_interval
        failing_since: float | None = None
        last_prune_day = int(time.time() // 86400)
        self._automatic_prune()
        while (stop is None or not stop.is_set()) and (
            deadline is None or time.monotonic() < deadline
        ):
            today = int(time.time() // 86400)
            if today != last_prune_day:
                self._automatic_prune()
                last_prune_day = today
            attempted = time.monotonic()
            batch: list[ReplyRecord] = []
            try:
                _, again = self._drain(
                    client,
                    domain,
                    topics,
                    deadline=deadline,
                    origin_id=origin_id,
                    wait_seconds=wait_seconds,
                    stop=stop,
                    saved_out=batch,
                )
            except TransportError:
                # Capture failure timestamp before yielding so consumer work
                # never eats into _TRANSPORT_PATIENCE. Any saved progress starts
                # a fresh outage window.
                failed_at = time.monotonic()
                if batch:
                    failing_since = failed_at
                elif failing_since is None:
                    failing_since = attempted
                yield batch
                if stop is not None and stop.is_set():
                    return
                if deadline is not None and (
                    failed_at >= deadline or time.monotonic() >= deadline
                ):
                    return
                if failed_at - failing_since >= _TRANSPORT_PATIENCE:
                    raise
                again = False
            else:
                failing_since = None
                yield batch
            if stop is not None and stop.is_set():
                return
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            # A held long poll or arriving batch resets backoff; an immediate
            # empty (including wait_refused) or transport failure backs off.
            if again:
                if batch or wait_seconds:
                    delay = poll_interval
                continue
            sleep_for = delay * uniform(0.8, 1.2)
            if remaining is not None:
                sleep_for = min(remaining, sleep_for)
            if stop is not None:
                stop.wait(sleep_for)
            else:
                time.sleep(sleep_for)
            delay = min(delay * 1.5, max(poll_interval, max_poll_interval))

    def drain(
        self, client: Client, domain: str, reply_topic: str | Sequence[str]
    ) -> int:
        """Drain a bounded batch from 1 to 16 topics, saving locally before Delete."""
        domain, topics = _name(domain, domain=True), _topics(reply_topic)
        self._automatic_prune()
        return len(self._drain(client, domain, topics)[0])

    def poll(
        self,
        client: Client,
        domain: str,
        reply_topic: str | Sequence[str],
        *,
        wait_seconds: int = MAX_WAIT_SECONDS,
    ) -> DrainResult:
        """Drain like :meth:`drain`, holding the first Own on the server.

        Accepts a single topic or 1 to 16 distinct topics (multi-topic requires
        service 0.6.0 or later). Every reply is durably published locally before
        its server copy is deleted. The first Own waits up to ``wait_seconds``
        for a task (zero does not wait), and the rest of the batch never waits.
        The result says whether to poll again at once or sleep first, so a
        refused wait (the topic's waiter limit, a deployment) does not spin.
        """
        _check_wait(wait_seconds)
        domain, topics = _name(domain, domain=True), _topics(reply_topic)
        self._automatic_prune()
        records, again = self._drain(client, domain, topics, wait_seconds=wait_seconds)
        return DrainResult(len(records), again, tuple(records))

    def watch(
        self,
        client: Client,
        domain: str,
        reply_topic: str | Sequence[str],
        *,
        timeout_seconds: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 30.0,
        wait_seconds: int = MAX_WAIT_SECONDS,
        stop: Event | None = None,
        once: bool = False,
    ) -> Iterator[ReplyRecord]:
        """Yield replies from 1 to 16 topics as they arrive, saving before Delete.

        With ``once=True``, yields the first non-empty arrival batch and stops.
        Identical redeliveries after a lost Delete are yielded at least once;
        conflicting replies are archived and yielded with ``duplicate=True``.
        Transport errors back off and retry for five minutes before raising,
        matching :meth:`wait`. A set ``stop`` event halts draining between
        messages and ends sleeps promptly.
        """
        _check_timeout(timeout_seconds)
        _check_poll_intervals(poll_interval, max_poll_interval, wait_seconds)
        domain, topics = _name(domain, domain=True), _topics(reply_topic)
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        for batch in self._poll_batches(
            client,
            domain,
            topics,
            deadline=deadline,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
            wait_seconds=wait_seconds,
            stop=stop,
        ):
            if batch:
                yield from batch
                if once:
                    return

    def wait(
        self,
        client: Client,
        origin_id: str,
        timeout_seconds: float,
        poll_interval: float = 1.0,
        max_poll_interval: float = 30.0,
        wait_seconds: int = MAX_WAIT_SECONDS,
    ) -> ReplyRecord | None:
        """Return a local reply, or None at a monotonic timeout; never repost.

        With ``wait_seconds``, Own holds each poll for that long, so a reply is
        seen within about a second without backoff. Zero restores the original
        ``poll_interval``-to-``max_poll_interval`` exponential backoff. A
        ``TransportError`` backs off the same way and is retried, so a service
        deployment does not end the wait; failures lasting five minutes raise.
        """
        _check_timeout(timeout_seconds)
        _check_poll_intervals(poll_interval, max_poll_interval, wait_seconds)
        deadline = time.monotonic() + timeout_seconds
        reply = self.reply(origin_id)
        if reply is not None:
            return reply
        record = self._required_request(origin_id)
        for _ in self._poll_batches(
            client,
            record.domain,
            (record.reply_topic,),
            deadline=deadline,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
            wait_seconds=wait_seconds,
            origin_id=origin_id,
        ):
            reply = self.reply(origin_id)
            if reply is not None:
                return reply
        return None

    def inbox(self) -> list[ReplyRecord]:
        result: list[ReplyRecord] = []
        for directory, duplicate in (
            (self.reply_directory, False),
            (self.duplicate_directory, True),
        ):
            for path, value in _records(directory, duplicate=duplicate):
                record = self._parse_reply(value, duplicate=duplicate)
                expected = (
                    self._duplicate_path(record.origin_id, _encode(record._content()))
                    if duplicate
                    else self._reply_path(record.origin_id)
                )
                if path != expected:
                    raise StoreError(
                        "Reply correlation does not match its storage name"
                    )
                result.append(record)
        return sorted(result, key=lambda record: (record.received_at, record.task_id))

    def requests(self) -> list[RequestRecord]:
        return sorted(
            (self._request_state(record) for record in self._intents()),
            key=lambda record: (record.created_at, record.origin_id),
        )

    def _intents(self) -> list[RequestRecord]:
        result: list[RequestRecord] = []
        for path, value in _records(self.request_directory):
            record = self._parse_intent(value)
            if path != self._request_path(record.origin_id):
                raise StoreError("Request correlation does not match its storage name")
            result.append(record)
        return result

    def prune(self) -> int:
        """Remove expired records and orphaned publication temporaries."""
        now = time.time()
        cutoff = int((now - _RETENTION_SECONDS) * 1000)
        count = 0
        for reply in self.inbox():
            if reply.received_at < cutoff:
                path = (
                    self._duplicate_path(reply.origin_id, _encode(reply._content()))
                    if reply.duplicate
                    else self._reply_path(reply.origin_id)
                )
                count += _remove(path)
        for record in self._intents():
            if record.created_at < cutoff and not _exists(
                self._reply_path(record.origin_id)
            ):
                count += _remove(self._request_path(record.origin_id))
        for directory in (
            self.request_directory,
            self.reply_directory,
            self.duplicate_directory,
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
            not _exists(path)
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
