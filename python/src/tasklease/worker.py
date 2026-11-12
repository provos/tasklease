"""Capacity-aware workers with conservative local leases and fenced completion.

Profiles provide a handler and a capacity context. ``step`` is also usable
under an externally held machine lock; only ``run`` acquires capacity itself.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from random import uniform
from threading import TIMEOUT_MAX, Condition, Event, Thread
from typing import Literal, Protocol, cast

from tasklease.client import MAX_WAIT_SECONDS, Client
from tasklease.errors import (
    Empty,
    Forbidden,
    Invalid,
    NotFound,
    TaskleaseError,
    TooLarge,
    TransportError,
)
from tasklease.generated import protocol as p
from tasklease.names import error_topic, is_domain, is_topic

_ERROR_LIMIT = 128 * 1024
# A hand-back after stop is best effort; this caps each of its HTTP phases.
_HAND_BACK_TIMEOUT = 10.0


class Capacity(Protocol):
    def acquire(self) -> AbstractContextManager[None]: ...


class UnlimitedCapacity:
    """A worker with no additional local scheduling constraint."""

    def acquire(self) -> AbstractContextManager[None]:
        return nullcontext()


@dataclass(frozen=True)
class Rejection:
    """A task the handler recognizes but cannot execute."""

    message: str


class Handler[Request, Outcome](Protocol):
    def validate(self, task: p.Task) -> Request | Rejection: ...

    def run(self, request: Request, lost: Event) -> Outcome: ...

    def reply(self, task: p.Task, outcome: Outcome) -> list[p.NewTask]: ...


def _topic(topic: object) -> str:
    if not is_topic(topic):
        raise Invalid("Invalid topic name")
    return topic


def validate_work_topic(topic: str) -> str:
    """Reject malformed and manual-only error topics before polling."""
    _topic(topic)
    if topic.endswith("/error"):
        raise Invalid("Automated workers cannot own /error topics")
    return topic


def _reply_topic(task: p.Task) -> str | None:
    value = task["data"].get("reply_topic")
    try:
        return _topic(value)
    except Invalid:
        return None


def _correlation(task: p.Task) -> p.Data:
    value = task["data"].get("request_id")
    return {"request_id": value} if isinstance(value, str) else {}


class EchoHandler:
    """An example handler that echoes a small message and request identifier."""

    def validate(self, task: p.Task) -> str | Rejection:
        message = task["data"].get("message", "")
        if not isinstance(message, str):
            return Rejection("message must be text")
        try:
            if len(message.encode("utf-8")) <= 4096:
                return message
        except UnicodeError:
            pass
        return Rejection("message must contain at most 4096 UTF-8 bytes")

    def run(self, request: str, lost: Event) -> p.Data:
        return {"status": "ok", "message": request}

    def reply(self, task: p.Task, outcome: p.Data) -> list[p.NewTask]:
        topic = _reply_topic(task)
        if topic is None:
            raise Invalid("Missing or invalid reply_topic")
        return [{"topic": topic, "data": {**_correlation(task), **outcome}}]


def _error(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"[:2048]
    except Exception:
        return type(error).__name__


def _json_text(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return json.dumps({"unserializable": type(value).__name__})


def bounded_error(original: object, outcome: object, error: str) -> p.Data:
    """Keep JSON-text prefixes within 128 KiB including their extra escaping."""
    original_text, outcome_text = _json_text(original), _json_text(outcome)
    # The SDK sends UTF-8 JSON. Prefix strings are already ASCII JSON text;
    # error messages need the same treatment for isolated surrogate code units.
    error = error[:2048].encode("utf-8", errors="backslashreplace").decode("utf-8")

    def payload(length: int) -> p.Data:
        return {
            "original": original_text[:length],
            "outcome": outcome_text[:length],
            "error": error,
            "truncated": True,
        }

    low, high = 0, min(max(len(original_text), len(outcome_text)), _ERROR_LIMIT)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_json_text(payload(middle)).encode("ascii")) <= _ERROR_LIMIT:
            low = middle
        else:
            high = middle - 1
    return payload(low)


class _Lease:
    """Two bounded-lifetime threads: I/O never blocks the deadline watchdog.

    Renewal is joined before completion and before returning local capacity.
    A custom transport ignoring its timeout can delay that join, but cannot
    keep the handler's lost event unset or accumulate background renewals.
    """

    def __init__(
        self,
        client: Client,
        task: p.Task,
        domain: str,
        topic: str,
        seconds: int,
        renew_every: float,
        sent: float,
    ) -> None:
        self.client = client
        self.task = task
        self.domain = domain
        self.topic = topic
        self.seconds = seconds
        self.renew_every = renew_every
        self.deadline = sent + seconds
        self.lost = Event()
        self._condition = Condition()
        self._stop_renew = Event()
        self._stop_watchdog = False
        self._renewer = Thread(
            target=self._renew, name="tasklease-renewer", daemon=True
        )
        self._watcher = Thread(
            target=self._watch, name="tasklease-deadline", daemon=True
        )

    def start(self) -> None:
        self._watcher.start()
        self._renewer.start()

    def remaining(self) -> float:
        with self._condition:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.lost.set()
            return 0 if self.lost.is_set() else remaining

    def _watch(self) -> None:
        with self._condition:
            while not self._stop_watchdog:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    self.lost.set()
                    return
                self._condition.wait(remaining)

    def _renew(self) -> None:
        # The lease counts from the Own's send time, so a slow answer leaves
        # less than a full interval: renew by half the remaining time at most.
        delay = min(self.renew_every, max(0.01, self.remaining() / 2))
        while not self._stop_renew.wait(delay):
            remaining = self.remaining()
            if remaining <= 0:
                return
            sent = time.monotonic()
            try:
                result = self.client.renew(
                    {
                        "domain": self.domain,
                        "topic": self.topic,
                        "id": self.task["id"],
                        "hold_seconds": self.seconds,
                    },
                    timeout=remaining,
                )
            except TransportError:
                delay = min(1.0, self.renew_every, max(0.01, self.remaining() / 2))
                continue
            except TaskleaseError:
                self.lost.set()
                return
            if result["task"].get("id") != self.task["id"]:
                self.lost.set()
                return
            with self._condition:
                if self.lost.is_set() or time.monotonic() >= self.deadline:
                    self.lost.set()
                    return
                self.deadline = sent + self.seconds
                self._condition.notify_all()
            delay = self.renew_every

    def stop_renewing(self) -> None:
        self._stop_renew.set()
        self._renewer.join()

    def close(self) -> None:
        self.stop_renewing()
        with self._condition:
            self._stop_watchdog = True
            self._condition.notify_all()
        self._watcher.join()


class WorkerLoop[Request, Outcome]:
    """Execute and finish one task at a time under a renewable local lease."""

    def __init__(
        self,
        client: Client,
        domain: str,
        topic: str,
        capacity: Capacity,
        handler: Handler[Request, Outcome],
        *,
        lease: int = 600,
        renew_every: float = 180,
        max_owned: int = 3,
        poll_interval: float = 30,
        wait_seconds: int = 30,
        stop: Event | None = None,
    ) -> None:
        if not is_domain(domain):
            raise Invalid("Invalid domain name")
        validate_work_topic(topic)
        if type(lease) is not int or not 1 <= lease <= 86400:
            raise Invalid("lease must be an integer from 1 to 86400 seconds")
        if type(max_owned) is not int or max_owned <= 0:
            raise Invalid("max_owned must be a positive integer")
        if (
            type(renew_every) not in {int, float}
            or not 0 < renew_every < lease
            or not math.isfinite(renew_every)
        ):
            raise Invalid("renew_every must be positive and less than lease")
        if (
            type(poll_interval) not in {int, float}
            or not 0 < poll_interval <= TIMEOUT_MAX / 1.2
            or not math.isfinite(poll_interval)
        ):
            raise Invalid("poll_interval must be positive and fit the platform timeout")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise Invalid(
                f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}"
            )
        self.client = client
        self.domain = domain
        self.topic = topic
        self.capacity = capacity
        self.handler = handler
        self.lease = lease
        self.renew_every = renew_every
        self.max_owned = max_owned
        self.poll_interval = poll_interval
        self.wait_seconds = wait_seconds
        # Set by step(): how long the last empty Own took. A held long poll
        # counts toward the idle poll interval.
        self._empty_seconds = 0.0
        self.stop = stop if stop is not None else Event()

    def _consume(self, task: p.Task, tasks: list[p.NewTask], lease: _Lease) -> bool:
        """Return false only after the local lease was lost."""
        while (remaining := lease.remaining()) > 0:
            try:
                self.client.consume(
                    {
                        "domain": self.domain,
                        "topic": self.topic,
                        "id": task["id"],
                        "tasks": tasks,
                    },
                    timeout=remaining,
                )
                return True
            except NotFound:
                return True
            except TransportError:
                lease.lost.wait(min(0.1, remaining))
        return False

    def _fallback(
        self, task: p.Task, outcome: object, error: str, lease: _Lease
    ) -> None:
        target = error_topic(self.topic)
        for bounded in (False, True):
            data = (
                bounded_error(task["data"], outcome, error)
                if bounded
                else cast(
                    p.Data,
                    {"original": task["data"], "outcome": outcome, "error": error},
                )
            )
            try:
                self._consume(task, [{"topic": target, "data": data}], lease)
                return
            except Forbidden:
                lease.lost.set()
                return
            except (TooLarge, Invalid):
                if bounded:
                    raise

    def _finish(
        self, task: p.Task, outcome: Outcome | p.Data, failed: bool, lease: _Lease
    ) -> None:
        if lease.remaining() <= 0:
            return
        reply_topic = _reply_topic(task)
        try:
            if not failed:
                # A pipeline stage's successors need no reply topic.
                replies = self.handler.reply(task, cast(Outcome, outcome))
            elif reply_topic is None:
                # No one asked for a reply: the error topic shows the failure.
                self._fallback(
                    task, outcome, "Failed, and the task has no reply_topic", lease
                )
                return
            else:
                replies = [
                    {
                        "topic": reply_topic,
                        "data": {**_correlation(task), **cast(p.Data, outcome)},
                    }
                ]
            if not isinstance(replies, list) or len(replies) > 16:
                raise Invalid("Handler reply must contain at most 16 tasks")
            for reply in replies:
                if not isinstance(reply, dict) or set(reply) != {"topic", "data"}:
                    raise Invalid("Handler reply must contain NewTask objects")
                _topic(reply["topic"])
                if not isinstance(reply["data"], dict):
                    raise Invalid("Handler reply data must be an object")
            self._consume(task, replies, lease)
        except Exception as error:
            self._fallback(task, outcome, _error(error), lease)

    def step(self, *, wait_seconds: int = 0) -> Literal["worked", "empty"]:
        """Own at most one task; callers may already hold external capacity.

        By default the Own answers at once, so a caller holding a lock that
        other work needs never keeps it through an idle wait. A positive
        ``wait_seconds`` long-polls; ``run()`` passes the loop's
        ``wait_seconds`` when its capacity is unlimited. A task that arrives
        after ``stop`` was set is handed back unrun and the step reports
        ``"empty"``.
        """
        if self.stop.is_set():
            return "empty"
        wait = self._effective_wait(wait_seconds)
        sent = time.monotonic()
        claim: p.OwnRequest = {
            "domain": self.domain,
            "topic": self.topic,
            "hold_seconds": self.lease,
        }
        if wait:
            claim["wait_seconds"] = wait
        try:
            task = self.client.own(claim, timeout=float(self.lease))["task"]
        except Empty as error:
            # A refused wait was not held, however long it took: a deployment
            # ends every held poll at once, and the whole fleet must not
            # reconnect at the same instant.
            self._empty_seconds = 0.0 if error.wait_refused else time.monotonic() - sent
            return "empty"
        if self.stop.is_set():
            # A stop that arrived during the Own must not start new work.
            self._hand_back(task)
            self._empty_seconds = time.monotonic() - sent
            return "empty"
        # The service may claim at any point during the wait, so the local
        # lease conservatively starts at the send time. _effective_wait keeps
        # the wait short enough that the first renewal still comes in time.
        lease = _Lease(
            self.client,
            task,
            self.domain,
            self.topic,
            self.lease,
            self.renew_every,
            sent,
        )
        if lease.remaining() <= 0:
            return "worked"
        lease.start()
        try:
            failed = True
            outcome: Outcome | p.Data
            if task["owned_count"] > self.max_owned:
                outcome = {"status": "abandoned", "error": "Maximum Own count exceeded"}
            else:
                try:
                    request = self.handler.validate(task)
                    if isinstance(request, Rejection):
                        outcome = {
                            "status": "rejected",
                            "error": request.message[:2048],
                        }
                    elif lease.remaining() > 0:
                        outcome = self.handler.run(request, lease.lost)
                        failed = False
                    else:
                        return "worked"
                except Exception as error:
                    outcome = {"status": "failed", "error": _error(error)}
            lease.stop_renewing()
            self._finish(task, outcome, failed, lease)
        finally:
            lease.close()
        return "worked"

    def _hand_back(self, task: p.Task) -> None:
        """Shorten an unstarted task's lease so another worker can own it soon.

        Renew keeps the ID and the raised ``owned_count``. If it fails, the
        task simply becomes available again when its lease expires.
        """
        try:
            self.client.renew(
                {
                    "domain": self.domain,
                    "topic": self.topic,
                    "id": task["id"],
                    "hold_seconds": 1,
                },
                timeout=_HAND_BACK_TIMEOUT,
            )
        except TaskleaseError:
            pass

    def _effective_wait(self, wait: int) -> int:
        """Bound a long poll so a task claimed at its end keeps a usable lease.

        The local lease counts from the send time, so a wait shortens the first
        lease window. Half the gap between renew_every and the lease leaves the
        first renewal that much margin before the conservative deadline.
        """
        if type(wait) is not int or not 0 <= wait <= MAX_WAIT_SECONDS:
            raise Invalid(
                f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}"
            )
        return max(0, min(wait, int((self.lease - self.renew_every) // 2)))

    def run(self) -> None:
        """Poll until stopped, acquiring local capacity before every Own.

        Only an unlimited capacity long-polls. A real capacity is held across
        each Own, so a wait would keep it from other work while idle; such a
        loop polls at once and sleeps outside the capacity instead.
        """
        wait = self.wait_seconds if isinstance(self.capacity, UnlimitedCapacity) else 0
        while not self.stop.is_set():
            try:
                with self.capacity.acquire():
                    if self.stop.is_set():
                        return
                    result = self.step(wait_seconds=wait)
            except TransportError:
                # A transport failure is not a completed wait: back off.
                self.stop.wait(self.poll_interval * uniform(0.8, 1.2))
                continue
            # Time the service held an empty poll counts toward the poll
            # interval: the default 30-second wait and interval poll again at
            # once, a longer interval keeps its cadence, and an immediate empty
            # (wait disabled or refused) sleeps the whole interval.
            if result == "empty":
                idle = max(0.0, self.poll_interval - self._empty_seconds)
                if idle > 0:
                    self.stop.wait(idle * uniform(0.8, 1.2))
