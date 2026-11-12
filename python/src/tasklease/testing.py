"""An offline, thread-safe tasklease service for application tests.

``FakeBoard`` serves real :class:`tasklease.client.Client` instances through
HTTPX mock transports. Its clock is in Unix milliseconds. Task IDs have random
ULID suffixes, so tasks created in the same millisecond have no insertion order.
Identity administration, operator identities and monitor snapshots are omitted.
Blob URLs are opaque local capabilities, not AWS signatures or R2 emulation.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import copy
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, NotRequired, cast, get_origin, get_type_hints
from urllib.parse import quote

import httpx

from tasklease import __version__
from tasklease.client import MAX_WAIT_SECONDS, Client, reject_nonfinite
from tasklease.errors import API_ERRORS
from tasklease.generated import protocol as p
from tasklease.names import (
    is_bearer_token,
    is_domain,
    is_task_id,
    is_topic,
    is_topic_glob,
    is_worker_label,
    topic_matches,
)

_MAX_DATA = 256 * 1024
_MAX_BODY = 16 * _MAX_DATA + 64 * 1024
_MAX_BLOB = 2 * 1024**3
_WEEK = 7 * 86400 * 1000
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_IDENTITY = re.compile(r"[a-z0-9-]{1,64}")
_BLOB_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_KEY_SUFFIX = re.compile(
    r"(\d{4}-\d{2}-\d{2})/(" + _ULID + r")/([A-Za-z0-9._-]{1,128})"
)


def _blob_day(now: int) -> str:
    # Gregorian dates repeat every 400 years (146097 days). Keep datetime
    # inside its year range even at the end of ULID's 48-bit clock range.
    cycles, day = divmod(now // 86_400_000, 146097)
    date = datetime.fromtimestamp(day * 86400, UTC)
    year = date.year + cycles * 400
    prefix = f"{year:04d}" if year <= 9999 else f"+{year:06d}"
    # The Worker takes the first ten characters of Date.toISOString().
    return f"{prefix}-{date.month:02d}-{date.day:02d}"[:10]


_OPERATIONS = {
    "identity.whoami",
    "create",
    "own",
    "renew",
    "read",
    "delete",
    "consume",
    "tasks.list",
    "topics.list",
    "topic.pollers",
    "blob.put_url",
    "blob.get_url",
}


def _request_fields() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    fields: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for operation, request in get_type_hints(p.OperationRequests).items():
        if operation not in _OPERATIONS:
            continue
        # Postponed annotations make TypedDict.__optional_keys__ unreliable.
        annotations = get_type_hints(request, include_extras=True)
        optional = frozenset(
            name
            for name, annotation in annotations.items()
            if get_origin(annotation) is NotRequired
        )
        fields[operation] = (frozenset(annotations) - optional, optional)
    return fields


_FIELDS = _request_fields()


def _integer(value: object) -> bool:
    # JSON Schema's integer type accepts a number such as 1.0, but not true.
    return type(value) is int or (
        type(value) is float and math.isfinite(value) and value.is_integer()
    )


def _json_value(value: object) -> bool:
    pending = [value]
    while pending:
        member = pending.pop()
        if member is None or isinstance(member, (str, bool, int)):
            continue
        if isinstance(member, float):
            if not math.isfinite(member):
                return False
        elif isinstance(member, list):
            pending.extend(member)
        elif isinstance(member, dict):
            if not all(isinstance(key, str) for key in member):
                return False
            pending.extend(member.values())
        else:
            return False
    return True


def _data(value: object) -> bool:
    return isinstance(value, dict) and _json_value(value)


def _number_text(value: int | float) -> str:
    number = float(value)
    if number == 0:
        return "0"
    shortest = repr(number).removesuffix(".0")
    if "e" not in shortest:
        return shortest
    if 1e-6 <= abs(number) < 1e21:
        return format(Decimal(shortest), "f")
    mantissa, exponent = shortest.split("e")
    power = int(exponent)
    return f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _json_text(value: object) -> str:
    """Stack-safe compact JSON with JSON.stringify's number formatting."""
    chunks: list[str] = []
    # The boolean distinguishes literal punctuation from JSON string values.
    pending: list[tuple[bool, object]] = [(False, value)]
    while pending:
        literal, member = pending.pop()
        if literal:
            chunks.append(cast(str, member))
        elif member is None:
            chunks.append("null")
        elif isinstance(member, bool):
            chunks.append("true" if member else "false")
        elif isinstance(member, str):
            chunks.append(json.dumps(member, ensure_ascii=False))
        elif isinstance(member, (int, float)):
            chunks.append(_number_text(member))
        elif isinstance(member, list):
            pending.append((True, "]"))
            for index in range(len(member) - 1, -1, -1):
                pending.append((False, member[index]))
                if index:
                    pending.append((True, ","))
            chunks.append("[")
        elif isinstance(member, dict):
            pending.append((True, "}"))
            for index, (key, item) in reversed(list(enumerate(member.items()))):
                pending.extend(((False, item), (True, ":"), (False, key)))
                if index:
                    pending.append((True, ","))
            chunks.append("{")
        else:
            raise TypeError("Not a JSON value")
    return "".join(chunks)


def _json_bytes(value: object) -> bytes:
    # JSON.stringify escapes lone surrogates; normal non-ASCII text stays UTF-8.
    return _json_text(value).encode("utf-8", errors="backslashreplace")


def _data_size(value: object) -> int:
    return len(_json_bytes(value))


def _new_ulid(now: int) -> str:
    value = (now << 80) | secrets.randbits(80)
    result = ""
    for _ in range(26):
        result = _ALPHABET[value & 31] + result
        value >>= 5
    return result


def _failure(code: p.ErrorCode, message: str | None = None) -> httpx.Response:
    return httpx.Response(
        API_ERRORS[code].status_code,
        json={"ok": False, "error": {"code": code, "message": message or code}},
    )


def _refused() -> httpx.Response:
    """An empty long poll that was answered without holding its wait."""
    return httpx.Response(
        404,
        json={
            "ok": False,
            "error": {"code": "empty", "message": "empty", "wait_refused": True},
        },
    )


def _success(body: object) -> httpx.Response:
    # ASCII escaping can represent lone surrogates accepted by JSON.parse.
    return httpx.Response(
        200,
        content=_json_bytes(body),
        headers={"Content-Type": "application/json"},
    )


def _valid_request(operation: str, body: dict[str, object]) -> bool:
    required, optional = _FIELDS[operation]
    if not required <= body.keys() or body.keys() - required - optional:
        return False
    if "domain" in body and not is_domain(body["domain"]):
        return False
    if "topic" in body and not is_topic(body["topic"]):
        return False
    if "id" in body and not is_task_id(body["id"]):
        return False
    if "hold_seconds" in body:
        hold = body["hold_seconds"]
        if not _integer(hold) or not 1 <= cast(int, hold) <= 86400:
            return False
    if "wait_seconds" in body:
        wait = body["wait_seconds"]
        if not _integer(wait) or not 0 <= cast(int, wait) <= MAX_WAIT_SECONDS:
            return False
    if "lease_before" in body and not _integer(body["lease_before"]):
        return False
    if "data" in body and not _data(body["data"]):
        return False
    if "tasks" in body:
        tasks = body["tasks"]
        if not isinstance(tasks, list) or len(tasks) > 16:
            return False
        for target in tasks:
            if (
                not isinstance(target, dict)
                or target.keys() != {"topic", "data"}
                or not is_topic(target["topic"])
                or not _data(target["data"])
            ):
                return False
    if "glob" in body and not is_topic_glob(body["glob"]):
        return False
    for field in ("cursor", "key"):
        if field in body and (not isinstance(body[field], str) or not body[field]):
            return False
    if operation == "blob.put_url":
        name, size, sha256 = body["name"], body["size"], body["sha256"]
        if (
            not isinstance(name, str)
            or _BLOB_NAME.fullmatch(name) is None
            or name in {".", ".."}
            or not _integer(size)
            or not 1 <= cast(int, size) <= _MAX_BLOB
            or not isinstance(sha256, str)
            or re.fullmatch(r"[a-fA-F0-9]{64}", sha256) is None
        ):
            return False
    return True


@dataclass(frozen=True)
class _BlobGrant:
    key: str
    method: Literal["GET", "PUT"]
    expires_at: int
    size: int | None = None


@dataclass(frozen=True)
class BlobPutGrant:
    """An issued PUT capability's signed fields; its URL is the snapshot key.

    ``expires_at`` uses Unix milliseconds, like the board's injected clock.
    """

    key: str
    size: int
    expires_at: int


class FakeBoard:
    """One-domain service with explicit test identities and an injectable clock.

    ``tasks`` snapshots map live task IDs to complete protocol task records.
    ``pollers`` snapshots map ``(topic, identity, worker)`` to poller records;
    old entries can remain stored but API queries hide entries older than a week.
    ``blobs`` snapshots map object keys to immutable bytes. Mutating any snapshot
    never changes the board; ``put_grants`` also prevents snapshot mutation.
    Close clients returned by ``client``/``blob_http``.
    """

    base_url = "https://api.tasklease.invalid"
    blob_base_url = "https://blobs.tasklease.invalid"

    def __init__(
        self,
        domain: str,
        *,
        clock: Callable[[], int] | None = None,
        max_waiters: int = 10_000,
    ) -> None:
        if not is_domain(domain):
            raise ValueError("Invalid domain")
        if type(max_waiters) is not int or max_waiters < 0:
            raise ValueError("max_waiters must be a nonnegative integer")
        self.domain = domain
        # Long polls held per topic, as on the service. Beyond it, Own answers
        # at once with wait_refused; 0 refuses every wait.
        self._max_waiters = max_waiters
        self._waiters: dict[str, int] = {}
        self._clock = (
            clock if clock is not None else lambda: time.time_ns() // 1_000_000
        )
        self._lock = threading.RLock()
        # Waiters park on this condition without holding _lock, so Create and
        # Consume from other threads can wake them.
        self._changed = threading.Condition(self._lock)
        self._identities: dict[str, p.Identity] = {}
        self._tasks: dict[str, p.Task] = {}
        self._pollers: dict[tuple[str, str, str], p.Poller] = {}
        self._blobs: dict[str, bytes] = {}
        self._grants: dict[str, _BlobGrant] = {}

    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int or not 0 <= now < 2**48:
            raise ValueError(
                "clock must return Unix milliseconds in ULID's 48-bit range"
            )
        return now

    @property
    def tasks(self) -> Mapping[str, p.Task]:
        """A deep snapshot of live tasks, keyed by their current fencing IDs."""
        with self._lock:
            return {
                task_id: cast(p.Task, json.loads(_json_bytes(task)))
                for task_id, task in self._tasks.items()
            }

    @property
    def pollers(self) -> Mapping[tuple[str, str, str], p.Poller]:
        """A snapshot keyed by (topic, identity, worker)."""
        with self._lock:
            return copy.deepcopy(self._pollers)

    @property
    def blobs(self) -> Mapping[str, bytes]:
        """A snapshot of stored object bytes, keyed by blob key."""
        with self._lock:
            return self._blobs.copy()

    @property
    def put_grants(self) -> Mapping[str, BlobPutGrant]:
        """A read-only snapshot of unexpired signed PUT grants, keyed by URL."""
        with self._lock:
            now = self._now()
            return MappingProxyType(
                {
                    url: BlobPutGrant(
                        grant.key, cast(int, grant.size), grant.expires_at
                    )
                    for url, grant in self._grants.items()
                    if grant.method == "PUT" and grant.expires_at > now
                }
            )

    def overwrite_blob(self, key: str, content: bytes) -> None:
        """Replace an existing object's bytes to inject corruption in a test.

        Unknown keys raise KeyError. Signed grants and their sizes are unchanged.
        """
        if not isinstance(content, bytes):
            raise TypeError("content must be immutable bytes")
        with self._lock:
            if key not in self._blobs:
                raise KeyError(key)
            self._blobs[key] = content

    def add_identity(
        self,
        name: str,
        token: str,
        *,
        create: Iterable[str] = (),
        own: Iterable[str] = (),
        read: Iterable[str] = (),
    ) -> None:
        """Provision an identity; invalid or duplicate test setup raises ValueError.

        Every own glob grants create on ``<glob>/error``, exactly as Identity Add
        does. Expanded scope lengths/counts have the same protocol limits.
        """
        scopes: p.Scopes = {
            "create": list(create),
            "own": list(own),
            "read": list(read),
        }
        if (
            _IDENTITY.fullmatch(name) is None
            or not is_bearer_token(token)
            or any(
                len(grants) > 256 or any(not is_topic_glob(grant) for grant in grants)
                for grants in (scopes["create"], scopes["own"], scopes["read"])
            )
        ):
            raise ValueError("Invalid identity or scope")
        expanded = list(
            dict.fromkeys([*scopes["create"], *(g + "/error" for g in scopes["own"])])
        )
        if len(expanded) > 256 or any(len(glob) > 256 for glob in expanded):
            raise ValueError("Expanded create scope exceeds protocol limits")
        scopes["create"] = expanded
        with self._lock:
            if token in self._identities or any(
                identity["name"] == name for identity in self._identities.values()
            ):
                raise ValueError("Identity name and token must be unique")
            self._identities[token] = {
                "name": name,
                "domains": [self.domain],
                "scopes": scopes,
                "operator": False,
            }

    def client(self, token: str, worker_label: str = "test") -> Client:
        """Create a real protocol client without opening a socket."""
        return Client(self.base_url, token, worker_label, transport=self.transport())

    def transport(self) -> httpx.MockTransport:
        """A fresh API-only transport, suitable for raw HTTP and fault wrappers."""
        return httpx.MockTransport(self._handle_api)

    def blob_http(self) -> httpx.Client:
        """Create a separate, credential-free client for local presigned URLs."""
        return httpx.Client(
            transport=self.blob_transport(),
            follow_redirects=False,
            trust_env=False,
        )

    def blob_transport(self) -> httpx.MockTransport:
        """A fresh blob-only transport for clients and streaming fault wrappers."""
        return httpx.MockTransport(self._handle_blob)

    def expire(self, task_id: str) -> None:
        """End a live task's lease now; an unknown fencing ID raises KeyError."""
        with self._lock:
            self._tasks[task_id]["lease_until"] = self._now()

    def _handle_api(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if str(request.url).split("/v1/", 1)[0] != self.base_url:
                return _failure("invalid", "Unknown operation")
            if request.method == "GET" and request.url.path == "/v1/version":
                return _success({"service_version": __version__, "protocol": "v1"})
            operation = request.url.path.removeprefix("/v1/")
            if request.method != "POST" or operation not in _FIELDS:
                return _failure("invalid", "Unknown operation")
            bearer = re.fullmatch(
                r"Bearer ([^\s]+)", request.headers.get("Authorization", ""), re.I
            )
            identity = self._identities.get(bearer[1]) if bearer else None
            if identity is None:
                return _failure("unauthenticated")
            content = request.read()
            length = request.headers.get("Content-Length", "")
            if len(content) > _MAX_BODY or (
                re.fullmatch(r"[0-9]+", length) is not None
                and float(length) > _MAX_BODY
            ):
                return _failure("too_large", "Invalid request body")
            try:
                value: object = json.loads(
                    content.decode("utf-8-sig"),
                    parse_constant=reject_nonfinite,
                    parse_int=float,
                    parse_float=float,
                )
                if not isinstance(value, dict):
                    return _failure("invalid", "Invalid request body")
                body = cast(dict[str, object], value)
                if operation == "consume" and isinstance(body.get("tasks"), list):
                    if len(cast(list[object], body["tasks"])) > 16:
                        return _failure("too_large", "Consume accepts at most 16 tasks")
                size = body.get("size")
                if (
                    operation == "blob.put_url"
                    and type(size) in (int, float)
                    and cast(float, size) > _MAX_BLOB
                ):
                    return _failure("too_large", "Blob exceeds 2 GiB")
                if not _valid_request(operation, body):
                    return _failure("invalid", "Invalid request body")
            except (ValueError, UnicodeError, RecursionError):
                return _failure("invalid", "Invalid request body")
            for field in ("hold_seconds", "wait_seconds", "lease_before", "size"):
                if field in body:
                    body[field] = int(cast(int, body[field]))
            worker = request.headers.get("X-Worker")
            if not is_worker_label(worker):
                return _failure("invalid", "Invalid X-Worker header")
            if operation == "identity.whoami":
                return _success({"ok": True, **identity})
            if body["domain"] != self.domain:
                return _failure("forbidden", "Domain access denied")
            if not self._authorized(operation, body, identity["scopes"]):
                return _failure("forbidden", "Scope access denied")
            if operation == "create" and _data_size(body["data"]) > _MAX_DATA:
                return _failure("too_large", "Task data exceeds 256 KiB")
            if operation == "consume" and any(
                _data_size(target["data"]) > _MAX_DATA
                for target in cast(list[p.NewTask], body["tasks"])
            ):
                return _failure("too_large", "Task data exceeds 256 KiB")
            wait = cast(int, body.get("wait_seconds", 0)) if operation == "own" else 0
            if not wait:
                return self._operate(operation, body, identity, worker, self._now())
        # Long-poll outside the lock; the clock may be injected, so bound the
        # wait by both wall time and the board's own clock.
        return self._wait_own(body, identity, worker, wait)

    @staticmethod
    def _allows(
        scopes: p.Scopes, kind: Literal["create", "own", "read"], topic: str
    ) -> bool:
        return any(topic_matches(glob, topic) for glob in scopes[kind])

    def _authorized(
        self, operation: str, body: dict[str, object], scopes: p.Scopes
    ) -> bool:
        if operation in {"delete", "topics.list"}:
            return True
        if operation == "blob.put_url":
            return bool(scopes["create"])
        if operation == "blob.get_url":
            return bool(scopes["read"] or scopes["own"])
        topic = cast(str, body["topic"])
        if operation == "create":
            return self._allows(scopes, "create", topic)
        if operation in {"own", "renew", "consume"}:
            return self._allows(scopes, "own", topic) and (
                operation != "consume"
                or all(
                    self._allows(scopes, "create", target["topic"])
                    for target in cast(list[p.NewTask], body["tasks"])
                )
            )
        return self._allows(scopes, "read", topic) or self._allows(scopes, "own", topic)

    def _insert(self, topic: str, data: p.Data, identity: str, now: int) -> str:
        task_id = "t_" + _new_ulid(now)
        self._tasks[task_id] = {
            "id": task_id,
            "domain": self.domain,
            "topic": topic,
            "created_at": now,
            "lease_until": now - 5000,
            "owned_count": 0,
            "creator": identity,
            "owner": None,
            "owned_at": None,
            # Request JSON decoding already detached this data from the caller.
            "data": data,
        }
        return task_id

    def _wait_own(
        self, body: dict[str, object], caller: p.Identity, worker: str, wait: int
    ) -> httpx.Response:
        """Repeat the real atomic claim until it succeeds or the wait ends.

        The lock is released between attempts, so Create and Consume on other
        threads can make progress and notify this waiter. A topic already
        holding ``max_waiters`` waiters answers at once with ``wait_refused``.
        """
        topic = cast(str, body["topic"])
        with self._changed:
            response = self._operate("own", body, caller, worker, self._now())
            if response.status_code != 404:
                return response
            if self._waiters.get(topic, 0) >= self._max_waiters:
                return _refused()
            self._waiters[topic] = self._waiters.get(topic, 0) + 1
        try:
            # Whichever clock has advanced furthest measures elapsed wait time:
            # an injected clock lets a test jump the deadline, while a frozen
            # one still terminates on wall time.
            start_wall, start_clock = time.monotonic(), self._now()
            while True:
                with self._changed:
                    elapsed = max(
                        time.monotonic() - start_wall,
                        (self._now() - start_clock) / 1000,
                    )
                    remaining = wait - elapsed
                    if remaining <= 0:
                        return _failure("empty")
                    # Short waits also notice lease expiry on an injected clock.
                    self._changed.wait(min(remaining, 0.05))
                    response = self._operate("own", body, caller, worker, self._now())
                    if response.status_code != 404:
                        return response
        finally:
            with self._changed:
                self._waiters[topic] -= 1
                if not self._waiters[topic]:
                    del self._waiters[topic]

    def _wake(self) -> None:
        """Notify blocked Own callers; callers must hold the lock."""
        self._changed.notify_all()

    def _operate(
        self,
        operation: str,
        body: dict[str, object],
        caller: p.Identity,
        worker: str,
        now: int,
    ) -> httpx.Response:
        identity = caller["name"]
        if operation.startswith("blob."):
            return self._blob_url(operation, body, now)
        topic = cast(str, body.get("topic", ""))
        if operation == "create":
            task_id = self._insert(topic, cast(p.Data, body["data"]), identity, now)
            self._wake()
            return _success({"ok": True, "id": task_id})
        if operation == "own":
            available = [
                task
                for task in self._tasks.values()
                if task["topic"] == topic and task["lease_until"] <= now
            ]
            old = min(
                available,
                key=lambda task: (task["created_at"], task["id"]),
                default=None,
            )
            key = (topic, identity, worker)
            poller = self._pollers.get(key)
            if (
                old is not None
                or poller is None
                or poller["last_polled_at"] <= now - 60_000
            ):
                self._pollers[key] = {
                    "identity": identity,
                    "worker": worker,
                    "last_polled_at": now,
                    "last_owned_at": now
                    if old is not None
                    else (poller["last_owned_at"] if poller else None),
                }
            if old is None:
                return _failure("empty")
            task_id = "t_" + _new_ulid(now)
            owned: p.Task = {
                **old,
                "id": task_id,
                "lease_until": now + cast(int, body["hold_seconds"]) * 1000,
                "owned_count": old["owned_count"] + 1,
                "owner": {"identity": identity, "worker": worker},
                "owned_at": now,
            }
            del self._tasks[old["id"]]
            self._tasks[task_id] = owned
            return _success({"ok": True, "task": owned})
        if operation == "topic.pollers":
            pollers = [
                poller
                for (name, _, _), poller in sorted(self._pollers.items())
                if name == topic and poller["last_polled_at"] >= now - _WEEK
            ]
            return _success({"ok": True, "pollers": pollers})
        if operation == "topics.list":
            return self._topics_list(body, caller["scopes"], now)
        if operation == "tasks.list":
            return self._tasks_list(body, now)
        task = self._tasks.get(cast(str, body["id"]))
        if task is None or task["topic"] != topic:
            return _failure("not_found")
        if operation == "read":
            return _success({"ok": True, "task": task})
        holder = task["owner"] is not None and task["owner"]["identity"] == identity
        if not holder and not (
            operation == "delete"
            and task["owner"] is None
            and task["creator"] == identity
        ):
            return _failure("forbidden")
        if operation == "renew":
            task["lease_until"] = now + cast(int, body["hold_seconds"]) * 1000
            # A renewed lease moves the next expiry; recheck it.
            self._wake()
            return _success({"ok": True, "task": task})
        if operation == "delete":
            if "lease_before" in body and task["lease_until"] >= cast(
                int, body["lease_before"]
            ):
                return _failure(
                    "forbidden", "Task lease is newer than the deletion cutoff"
                )
            del self._tasks[task["id"]]
            return _success({"ok": True})
        # Every successor is validated and authorized before changing any state.
        targets = cast(list[p.NewTask], body["tasks"])
        ids = [
            self._insert(target["topic"], target["data"], identity, now)
            for target in targets
        ]
        del self._tasks[task["id"]]
        self._wake()
        return _success({"ok": True, "ids": ids})

    def _topics_list(
        self, body: dict[str, object], scopes: p.Scopes, now: int
    ) -> httpx.Response:
        grouped: dict[str, list[p.Task]] = {}
        for task in self._tasks.values():
            grouped.setdefault(task["topic"], []).append(task)
        names = grouped.keys() | {
            topic
            for (topic, _, _), poller in self._pollers.items()
            if poller["last_polled_at"] >= now - _WEEK
        }
        topics: list[p.TopicSummary] = []
        for topic in sorted(names):
            if not self._allows(scopes, "read", topic) or (
                "glob" in body and not topic_matches(cast(str, body["glob"]), topic)
            ):
                continue
            tasks = grouped.get(topic, [])
            available = [task for task in tasks if task["lease_until"] <= now]
            topics.append(
                {
                    "topic": topic,
                    "available": len(available),
                    "held": len(tasks) - len(available),
                    "oldest_available_at": min(
                        (task["created_at"] for task in available), default=None
                    ),
                    "declared": False,
                }
            )
        return _success({"ok": True, "topics": topics})

    def _tasks_list(self, body: dict[str, object], now: int) -> httpx.Response:
        after: tuple[int, str] | None = None
        if "cursor" in body:
            cursor = cast(str, body["cursor"])
            try:
                if len(cursor) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
                    raise ValueError
                payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                if base64.urlsafe_b64encode(payload).decode().rstrip("=") != cursor:
                    raise ValueError
                version, timestamp, task_id = payload.decode("ascii").split(":")
                if (
                    version != "v1"
                    or re.fullmatch(r"0|[1-9][0-9]*", timestamp) is None
                    or int(timestamp) > 2**53 - 1
                    or not is_task_id(task_id)
                ):
                    raise ValueError
                after = (int(timestamp), task_id)
            except (ValueError, UnicodeError, binascii.Error):
                return _failure("invalid", "Malformed cursor")
        tasks = sorted(
            (task for task in self._tasks.values() if task["topic"] == body["topic"]),
            key=lambda task: (task["created_at"], task["id"]),
        )
        if after is not None:
            tasks = [task for task in tasks if (task["created_at"], task["id"]) > after]
        page = tasks[:500]
        summaries: list[p.TaskSummary] = [
            {
                "id": task["id"],
                "created_at": task["created_at"],
                "lease_until": task["lease_until"],
                "owned_count": task["owned_count"],
                "creator": task["creator"],
                "owner": task["owner"],
                "owned_at": task["owned_at"],
                "state": "available" if task["lease_until"] <= now else "held",
            }
            for task in page
        ]
        result: p.TasksListResponse = {"ok": True, "tasks": summaries}
        if len(tasks) > 500:
            last = page[-1]
            payload = f"v1:{last['created_at']}:{last['id']}".encode()
            result["cursor"] = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return _success(result)

    def _blob_url(
        self, operation: str, body: dict[str, object], now: int
    ) -> httpx.Response:
        size: int | None = None
        if operation == "blob.put_url":
            day = _blob_day(now)
            prefix = self.domain.replace("/", "%2F")
            key = f"{prefix}/{day}/{_new_ulid(now)}/{body['name']}"
            size = cast(int, body["size"])
            method: Literal["GET", "PUT"] = "PUT"
        else:
            key = cast(str, body["key"])
            prefix = self.domain.replace("/", "%2F") + "/"
            match = (
                _KEY_SUFFIX.fullmatch(key.removeprefix(prefix))
                if key.startswith(prefix)
                else None
            )
            if match is None or match[3] in {".", ".."}:
                return _failure(
                    "invalid",
                    "Blob key does not belong to the domain or has invalid shape",
                )
            try:
                year, month, day_number = map(int, match[1].split("-"))
                if not 1 <= day_number <= calendar.monthrange(year, month)[1]:
                    raise ValueError
            except ValueError:
                return _failure(
                    "invalid",
                    "Blob key does not belong to the domain or has invalid shape",
                )
            if key not in self._blobs:
                return _failure("not_found", "Blob not found")
            method = "GET"
        expires_at = now // 1000 * 1000 + 3600 * 1000
        self._grants = {
            url: grant for url, grant in self._grants.items() if grant.expires_at > now
        }
        signature = secrets.token_hex(32)
        url = f"{self.blob_base_url}/{quote(key, safe='/')}?X-Amz-Signature={signature}"
        self._grants[url] = _BlobGrant(key, method, expires_at, size)
        result: dict[str, object] = {"ok": True, "url": url, "expires_at": expires_at}
        if method == "PUT":
            result["key"] = key
        return _success(result)

    def _handle_blob(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            grant = self._grants.get(str(request.url))
            if (
                grant is None
                or grant.expires_at <= self._now()
                or request.method != grant.method
            ):
                return httpx.Response(403, text="Invalid or expired blob URL")
            if grant.method == "GET":
                content = self._blobs.get(grant.key)
                return (
                    httpx.Response(404)
                    if content is None
                    else httpx.Response(200, content=content)
                )
            if request.headers.get("Content-Length") != str(grant.size):
                return httpx.Response(
                    403, text="Content-Length does not match signed size"
                )
            content = request.read()
            if len(content) != grant.size:
                return httpx.Response(
                    400, text="Body length does not match Content-Length"
                )
            self._blobs[grant.key] = content
            return httpx.Response(200)
