"""Generated lifecycle histories checked against an independent spec model."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import replace
from random import Random
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    rule,
    run_state_machine_as_test,
)

from conformance.lifecycle_model import (
    ACTORS,
    KEYS,
    MISSING,
    START,
    TOPICS,
    WEEK,
    BoardModel,
    Command,
    Operation,
)
from conformance.support import ADMIN_TOKEN, LostResponse, ServiceTarget
from tasklease.client import Client
from tasklease.testing import FakeBoard


class RecordingTransport(httpx.BaseTransport):
    """Observe the committed response before LostResponse discards it."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.body: dict[str, object] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        response.read()
        self.body = cast(dict[str, object], response.json())
        return response

    def close(self) -> None:
        self.inner.close()


class ModelTarget:
    def __init__(self, url: str = "", admin: Client | None = None) -> None:
        with ExitStack() as cleanup:
            self.model = BoardModel("model/" + uuid4().hex)
            self.target = ServiceTarget(
                self.model.domain,
                url,
                board=FakeBoard(self.model.domain, clock=lambda: self.model.now)
                if admin is None
                else None,
                admin=admin,
            )
            cleanup.callback(self.target.close)
            self.http = cleanup.enter_context(self.target.http())
            self.tokens = []
            actors = []
            for actor in ACTORS:
                name, token = self.target.identity(
                    create=sorted(actor.create),
                    own=sorted(actor.own),
                    read=list(TOPICS),
                )
                actors.append(replace(actor, name=name))
                self.tokens.append(token)
            self.model.actors = tuple(actors)
            _, self.observer_token = self.target.identity(
                create=(), own=(), read=(*TOPICS, "denied")
            )
            if admin is None:
                cleanup.enter_context(
                    patch("tasklease.testing.secrets.randbits", Random(0).getrandbits)
                )
            self.references: dict[str, str] = {MISSING: "work"}
            self.origins: dict[str, str] = {MISSING: "work"}
            self.commands: list[Command] = []
            self.set_time(START, reset=True)
            self.cleanup = cleanup.pop_all()

    def close(self) -> None:
        self.cleanup.close()

    def set_time(self, now: int, *, reset: bool = False) -> None:
        assert now >= self.model.now
        if self.target.board is None:
            response = self.http.post(
                self.target.url + "/__model/clock",
                json={"domain": self.model.domain, "now": now, "reset": reset},
            )
            response.raise_for_status()
        self.model.now = now

    def execute(self, command: Command, *, lost: bool = False) -> dict[str, object]:
        url = self.target.url + "/v1/" + command.operation
        headers = {
            "Authorization": "Bearer " + self.tokens[command.actor],
            "X-Worker": command.worker,
        }
        if lost:
            recording = RecordingTransport(self.target.transport())
            with httpx.Client(
                transport=LostResponse(recording, command.operation), trust_env=False
            ) as http:
                with pytest.raises(httpx.ReadError, match="Lost committed response"):
                    http.post(
                        url, json=command.body(self.model.domain), headers=headers
                    )
            actual = recording.body
        else:
            response = self.http.post(
                url, json=command.body(self.model.domain), headers=headers
            )
            actual = cast(dict[str, object], response.json())
            code = (
                "ok" if actual["ok"] else cast(dict[str, str], actual["error"])["code"]
            )
            assert (
                response.status_code
                == {"ok": 200, "forbidden": 403, "not_found": 404, "empty": 404}[code]
            )
        fresh: list[str] = []
        if actual["ok"]:
            if command.operation == "create":
                fresh = [cast(str, actual["id"])]
            elif command.operation == "own":
                fresh = [cast(dict[str, str], actual["task"])["id"]]
            elif command.operation == "consume":
                fresh = cast(list[str], actual["ids"])
        expected = self.model.apply(command, fresh)
        if not actual["ok"]:
            actual = {
                "ok": False,
                "error": {"code": cast(dict[str, str], actual["error"])["code"]},
            }
        assert actual == expected, command
        for task in self.model.tasks.values():
            self.references[task.task_id] = task.topic
            if task.root:
                self.origins[task.origin] = task.topic
        self.commands.append(command)
        self.check_tasks()
        return actual

    def check_tasks(self) -> None:
        headers = {
            "Authorization": "Bearer " + self.observer_token,
            "X-Worker": "observer",
        }
        for topic in (*TOPICS, "denied"):
            tasks = sorted(
                (task for task in self.model.tasks.values() if task.topic == topic),
                key=lambda task: (task.created, task.task_id),
            )
            response = self.http.post(
                self.target.url + "/v1/tasks.list",
                json={"domain": self.model.domain, "topic": topic},
                headers=headers,
            )
            assert response.json() == {
                "ok": True,
                "tasks": [
                    task.summary(self.model.domain, self.model.now) for task in tasks
                ],
            }
            for task in tasks:
                response = self.http.post(
                    self.target.url + "/v1/read",
                    json={
                        "domain": self.model.domain,
                        "topic": topic,
                        "id": task.task_id,
                    },
                    headers=headers,
                )
                assert response.json() == {
                    "ok": True,
                    "task": task.wire(self.model.domain),
                }


@pytest.fixture
def model_target_factory(
    service_backend: str, request: pytest.FixtureRequest
) -> Iterator[Callable[[], ModelTarget]]:
    url = ""
    with ExitStack() as cleanup:
        admin = None
        if service_backend == "worker":
            url = cast(str, request.getfixturevalue("model_worker_url"))
            admin = cleanup.enter_context(
                Client(url, ADMIN_TOKEN, transport=httpx.HTTPTransport(trust_env=False))
            )

        def create() -> ModelTarget:
            return ModelTarget(url, admin)

        yield create


class LifecycleMachine(RuleBasedStateMachine):
    def __init__(self, factory: Callable[[], ModelTarget]) -> None:
        super().__init__()
        self.target = factory()

    def teardown(self) -> None:
        self.target.close()

    @initialize()
    def held_root(self) -> None:
        self.target.execute(Command("create", key=KEYS[0]))
        self.target.execute(Command("own", actor=1))

    @rule(
        actor=st.integers(0, 2),
        topic=st.sampled_from((*TOPICS, "denied")),
        key=st.sampled_from((None, *KEYS)),
        value=st.integers(0, 3),
        lost=st.booleans(),
    )
    def create(
        self, actor: int, topic: str, key: str | None, value: int, lost: bool
    ) -> None:
        self.target.execute(
            Command("create", actor, topic, value=value, key=key), lost=lost
        )

    @rule(
        actor=st.integers(0, 2),
        topic=st.sampled_from(TOPICS),
        hold=st.sampled_from((1, 2, 5)),
        lost=st.booleans(),
    )
    def own(self, actor: int, topic: str, hold: int, lost: bool) -> None:
        self.target.execute(Command("own", actor, topic, hold=hold), lost=lost)

    @rule(
        data=st.data(),
        operation=st.sampled_from(("read", "renew", "delete", "consume", "cancel")),
    )
    def mutate(self, data: st.DataObject, operation: Operation) -> None:
        refs = self.target.origins if operation == "cancel" else self.target.references
        task_id = data.draw(st.sampled_from(tuple(refs)))
        topic = data.draw(st.sampled_from((refs[task_id], "work", "result")))
        task = self.target.model.tasks.get(task_id)
        actors = list(range(len(ACTORS)))
        if task is not None:
            actors.extend(
                i
                for i, actor in enumerate(self.target.model.actors)
                if actor.name in {task.owner, task.creator}
            )
        actor = data.draw(st.sampled_from(actors))
        cutoff = data.draw(
            st.sampled_from(
                (
                    None,
                    self.target.model.now,
                    *((task.lease - 1, task.lease, task.lease + 1) if task else ()),
                )
            )
        )
        targets = data.draw(
            st.lists(
                st.tuples(st.sampled_from((*TOPICS, "denied")), st.integers(0, 3)),
                max_size=2,
            )
        )
        self.target.execute(
            Command(
                operation,
                actor,
                topic,
                task_id,
                worker=data.draw(st.sampled_from(("main", "other"))),
                hold=data.draw(st.sampled_from((1, 2, 5))),
                cutoff=cutoff,
                targets=tuple(targets),
            ),
            lost=data.draw(st.booleans()),
        )

    @rule(data=st.data())
    def advance(self, data: st.DataObject) -> None:
        model = self.target.model
        boundaries = [task.lease for task in model.tasks.values()]
        boundaries.extend(accepted + WEEK for _, accepted in model.keys.values())
        candidates = {model.now + delta for delta in (0, 1, 999, 1000, 1001, WEEK)}
        candidates.update(
            boundary + delta
            for boundary in boundaries
            for delta in (-1, 0, 1)
            if boundary + delta >= model.now
        )
        self.target.set_time(data.draw(st.sampled_from(sorted(candidates))))
        self.target.check_tasks()

    @rule(data=st.data())
    def retry(self, data: st.DataObject) -> None:
        self.target.execute(data.draw(st.sampled_from(self.target.commands)))


def test_generated_lifecycle(model_target_factory: Callable[[], ModelTarget]) -> None:
    class Machine(LifecycleMachine):
        def __init__(self) -> None:
            super().__init__(model_target_factory)

    run_state_machine_as_test(
        Machine,
        settings=settings(max_examples=20, stateful_step_count=30, deadline=None),
    )


def test_reproducible_equal_time_order(
    model_target_factory: Callable[[], ModelTarget],
) -> None:
    histories = []
    for _ in range(2):
        target = model_target_factory()
        try:
            ids = [
                target.execute(Command("create", value=value))["id"]
                for value in range(3)
            ]
            claims = [
                cast(dict[str, object], target.execute(Command("own", actor=2))["task"])
                for _ in range(3)
            ]
            histories.append((ids, [(task["id"], task["data"]) for task in claims]))
        finally:
            target.close()
    assert histories[0] == histories[1]


def test_lifecycle_boundaries(model_target_factory: Callable[[], ModelTarget]) -> None:
    target = model_target_factory()
    try:
        created = target.execute(Command("create", key=KEYS[0]), lost=True)
        origin = cast(str, created["id"])
        held = target.execute(Command("own", actor=1))
        old_id = cast(dict[str, str], held["task"])["id"]
        target.set_time(START + 1000)
        target.execute(Command("renew", actor=1, task_id=old_id, worker="other"))
        target.set_time(START + 2000)
        target.execute(Command("own", actor=2))
        for operation in ("renew", "consume", "delete"):
            target.execute(Command(operation, actor=1, task_id=old_id))
        target.execute(Command("cancel", task_id=origin))
        target.set_time(START + WEEK)
        target.execute(Command("create", topic="result", key=KEYS[0], value=3))
        target.set_time(START + WEEK + 1)
        target.execute(Command("create", key=KEYS[0]))
        held = target.execute(Command("own", actor=2))
        current = cast(dict[str, str], held["task"])["id"]
        target.execute(
            Command(
                "consume",
                actor=2,
                task_id=current,
                targets=(("work", 1), ("result", 2)),
            ),
            lost=True,
        )
        target.execute(
            Command("cancel", task_id=next(iter(target.model.keys.values()))[0])
        )
    finally:
        target.close()
