"""Executable interpretation of spec §§2–5, independent of either backend.

Payloads and grants are deliberately finite. Fresh IDs are supplied by the
observer, since their random values are unspecified; their freshness and their
effect on claim ordering are still checked. No production transition helpers
are imported here.
"""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Literal

WEEK = 7 * 24 * 60 * 60 * 1000
START = 2_000_000_000_000
TOPICS = ("work", "result")
KEYS = (
    "00000000-0000-7000-8000-000000000001",
    "00000000-0000-7000-8000-000000000002",
)
MISSING = "t_00000000000000000000000000"
type Operation = Literal[
    "create", "own", "renew", "read", "delete", "consume", "cancel"
]


@dataclass(frozen=True)
class Actor:
    name: str
    create: frozenset[str]
    own: frozenset[str]
    read: frozenset[str]


ACTORS = (
    Actor("requester", frozenset(TOPICS), frozenset(), frozenset({"work"})),
    Actor("worker-a", frozenset({"result"}), frozenset(TOPICS), frozenset()),
    Actor("worker-b", frozenset(TOPICS), frozenset(TOPICS), frozenset({"result"})),
)


@dataclass(frozen=True)
class Command:
    operation: Operation
    actor: int = 0
    topic: str = "work"
    task_id: str = MISSING
    worker: str = "main"
    hold: int = 1
    value: int = 0
    key: str | None = None
    cutoff: int | None = None
    targets: tuple[tuple[str, int], ...] = ()

    def body(self, domain: str) -> dict[str, object]:
        body: dict[str, object] = {"domain": domain, "topic": self.topic}
        if self.operation == "create":
            body["data"] = {"value": self.value}
            if self.key is not None:
                body["idempotency_key"] = self.key
        elif self.operation == "cancel":
            body["origin_id"] = self.task_id
        elif self.operation != "own":
            body["id"] = self.task_id
        if self.operation in {"own", "renew"}:
            body["hold_seconds"] = self.hold
        if self.operation == "consume":
            body["tasks"] = [
                {"topic": topic, "data": {"value": value}}
                for topic, value in self.targets
            ]
        if self.operation == "delete" and self.cutoff is not None:
            body["lease_before"] = self.cutoff
        return body


@dataclass(frozen=True)
class Task:
    task_id: str
    origin: str
    root: bool
    topic: str
    created: int
    lease: int
    creator: str
    value: int
    count: int = 0
    owner: str | None = None
    worker: str | None = None
    owned: int | None = None

    def wire(self, domain: str) -> dict[str, object]:
        return {
            "id": self.task_id,
            "origin_id": self.origin,
            "domain": domain,
            "topic": self.topic,
            "created_at": self.created,
            "lease_until": self.lease,
            "creator": self.creator,
            "data": {"value": self.value},
            "owned_count": self.count,
            "owner": None
            if self.owner is None
            else {"identity": self.owner, "worker": self.worker},
            "owned_at": self.owned,
        }

    def summary(self, domain: str, now: int) -> dict[str, object]:
        result = self.wire(domain)
        for field in ("domain", "topic", "data"):
            del result[field]
        result["state"] = "available" if self.lease <= now else "held"
        return result


def refused(code: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code}}


class BoardModel:
    def __init__(self, domain: str, actors: tuple[Actor, ...] = ACTORS) -> None:
        self.domain = domain
        self.actors = actors
        self.now = START
        self.tasks: dict[str, Task] = {}
        self.seen: set[str] = set()
        self.keys: dict[tuple[str, str], tuple[str, int]] = {}

    def _fresh(self, ids: Iterator[str]) -> str:
        task_id = next(ids)
        assert task_id not in self.seen, f"Reused fencing ID: {task_id}"
        self.seen.add(task_id)
        return task_id

    def _insert(
        self,
        ids: Iterator[str],
        actor: Actor,
        topic: str,
        value: int,
        origin: str | None = None,
    ) -> str:
        task_id = self._fresh(ids)
        self.tasks[task_id] = Task(
            task_id,
            origin or task_id,
            origin is None,
            topic,
            self.now,
            self.now - 5000,
            actor.name,
            value,
        )
        return task_id

    def apply(self, command: Command, fresh_ids: list[str]) -> dict[str, object]:
        """Compute the specified response and next state, binding fresh names."""
        ids = iter(fresh_ids)
        actor = self.actors[command.actor]
        op, topic = command.operation, command.topic
        if (
            (op == "create" and topic not in actor.create)
            or (op in {"own", "renew", "consume"} and topic not in actor.own)
            or (op == "read" and topic not in actor.read | actor.own)
            or (
                op == "consume"
                and any(target not in actor.create for target, _ in command.targets)
            )
        ):
            return refused("forbidden")
        if op == "create":
            key = (actor.name, command.key) if command.key is not None else None
            cached = self.keys.get(key) if key is not None else None
            if cached is not None and self.now - cached[1] <= WEEK:
                return {"ok": True, "id": cached[0]}
            task_id = self._insert(ids, actor, topic, command.value)
            if key is not None:
                self.keys[key] = (task_id, self.now)
            return {"ok": True, "id": task_id}
        if op == "own":
            available = [
                task
                for task in self.tasks.values()
                if task.topic == topic and task.lease <= self.now
            ]
            if not available:
                return refused("empty")
            old = min(available, key=lambda task: (task.created, task.task_id))
            task_id = self._fresh(ids)
            owned = replace(
                old,
                task_id=task_id,
                lease=self.now + command.hold * 1000,
                count=old.count + 1,
                owner=actor.name,
                worker=command.worker,
                owned=self.now,
            )
            del self.tasks[old.task_id]
            self.tasks[task_id] = owned
            return {"ok": True, "task": owned.wire(self.domain)}
        if op == "cancel":
            root = next(
                (
                    task
                    for task in self.tasks.values()
                    if task.root
                    and task.origin == command.task_id
                    and task.topic == topic
                ),
                None,
            )
            if root is None:
                return refused("not_found")
            if root.creator != actor.name:
                return refused("forbidden")
            del self.tasks[root.task_id]
            return {"ok": True}
        task = self.tasks.get(command.task_id)
        if task is None or task.topic != topic:
            return refused("not_found")
        if op == "read":
            return {"ok": True, "task": task.wire(self.domain)}
        if actor.name != task.owner and not (
            op == "delete" and task.owner is None and actor.name == task.creator
        ):
            return refused("forbidden")
        if op == "renew":
            task = replace(task, lease=self.now + command.hold * 1000)
            self.tasks[task.task_id] = task
            return {"ok": True, "task": task.wire(self.domain)}
        if op == "delete":
            if command.cutoff is not None and task.lease >= command.cutoff:
                return refused("forbidden")
            del self.tasks[task.task_id]
            return {"ok": True}
        assert op == "consume"
        del self.tasks[task.task_id]
        successors = [
            self._insert(ids, actor, target, value, task.origin)
            for target, value in command.targets
        ]
        return {"ok": True, "ids": successors}
