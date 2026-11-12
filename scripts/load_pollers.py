#!/usr/bin/env python3
"""Load a deployed tasklease service with many simulated workers.

Run from the repository root against a test deployment:
  TL_ADMIN_TOKEN=... uv run --project python python scripts/load_pollers.py \\
      --url https://NAME.SUBDOMAIN.workers.dev --workers 1000 --poll-seconds 30 \\
      --duration 240 --output /tmp/load.json

Each simulated worker has its own identity and token (round-robin over
--identities), so authentication exercises the identity registry as many
machines would. Workers poll one topic with Own at the real worker's jittered
interval (0.8-1.2x --poll-seconds). A producer creates tasks at --create-rate;
a worker that gets one holds it for --work-seconds, Consumes it and polls
again at once. Everything runs in a fresh domain `loadtest.<run id>`; at the
end the topic is drained and every identity is revoked.

One Python asyncio client saturates near 100 requests a second, after which
its own queue inflates latency; spread higher rates over --processes (about
one per 75 requests a second). Requests are billed to the deployment's
Cloudflare account. Latency includes the client's network path; the report
records the achieved rate, so a saturated client is visible.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import secrets
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

TOPIC = "work/load"
WINDOW_SECONDS = 10


@dataclass
class OpStats:
    latencies_ms: list[float] = field(default_factory=list)
    outcomes: Counter[str] = field(default_factory=Counter)

    def summary(self, seconds: float) -> dict[str, object]:
        values = sorted(self.latencies_ms)
        quantiles = (
            statistics.quantiles(values, n=100, method="inclusive")
            if len(values) > 1
            else values * 99
        )
        return {
            "requests": len(values),
            "per_second": round(len(values) / seconds, 2) if seconds else 0,
            "outcomes": dict(self.outcomes),
            "p50_ms": round(quantiles[49], 1) if values else None,
            "p95_ms": round(quantiles[94], 1) if values else None,
            "p99_ms": round(quantiles[98], 1) if values else None,
            "max_ms": round(values[-1], 1) if values else None,
        }


@dataclass
class Recorder:
    started: float = field(default_factory=time.time)
    ops: dict[str, OpStats] = field(default_factory=dict)
    windows: dict[int, Counter[str]] = field(default_factory=dict)

    def record(self, op: str, outcome: str, latency_ms: float) -> None:
        stats = self.ops.setdefault(op, OpStats())
        stats.latencies_ms.append(latency_ms)
        stats.outcomes[outcome] += 1
        window = int((time.time() - self.started) // WINDOW_SECONDS)
        counts = self.windows.setdefault(window, Counter())
        counts["requests"] += 1
        if not outcome.startswith(("200", "404 empty")):
            counts["errors"] += 1

    def merge(self, other: Recorder) -> None:
        for op, stats in other.ops.items():
            mine = self.ops.setdefault(op, OpStats())
            mine.latencies_ms.extend(stats.latencies_ms)
            mine.outcomes.update(stats.outcomes)
        for window, counts in other.windows.items():
            self.windows.setdefault(window, Counter()).update(counts)


class Service:
    def __init__(self, client: httpx.AsyncClient, recorder: Recorder) -> None:
        self.client = client
        self.recorder = recorder

    async def call(
        self, op: str, body: dict[str, object], token: str, worker: str = "load"
    ) -> tuple[str, dict[str, object] | None]:
        """Return the outcome label and the decoded success body, if any."""
        started = time.perf_counter()
        try:
            response = await self.client.post(
                f"/v1/{op}",
                json=body,
                headers={"Authorization": f"Bearer {token}", "X-Worker": worker},
            )
        except httpx.TimeoutException:
            outcome, value = "timeout", None
        except httpx.TransportError as exc:
            outcome, value = f"transport {type(exc).__name__}", None
        else:
            value = None
            try:
                decoded = response.json()
            except ValueError:
                decoded = None
            if response.status_code == 200 and isinstance(decoded, dict):
                outcome, value = "200", decoded
            else:
                error = decoded.get("error", {}) if isinstance(decoded, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
                outcome = f"{response.status_code} {code or 'non-json'}"
                # A refused wait was not held; report it apart from an empty.
                if isinstance(error, dict) and error.get("wait_refused") is True:
                    outcome += " wait_refused"
        self.recorder.record(op, outcome, (time.perf_counter() - started) * 1000)
        return outcome, value


async def add_identities(
    service: Service, admin: str, domain: str, run_id: str, count: int
) -> list[tuple[str, str]]:
    semaphore = asyncio.Semaphore(16)

    async def add(index: int) -> tuple[str, str]:
        name = f"load-{run_id}-{index}"
        async with semaphore:
            outcome, value = await service.call(
                "identity.add",
                {
                    "name": name,
                    "domains": [domain],
                    "operator": False,
                    "scopes": {"create": [], "own": [TOPIC], "read": []},
                },
                admin,
            )
        if value is None:
            raise RuntimeError(f"identity.add failed: {outcome}")
        return name, str(value["token"])

    return list(await asyncio.gather(*(add(i) for i in range(count))))


async def revoke(service: Service, admin: str, names: list[str]) -> int:
    semaphore = asyncio.Semaphore(16)
    failures = 0

    async def one(name: str) -> None:
        nonlocal failures
        async with semaphore:
            outcome, _ = await service.call("identity.revoke", {"name": name}, admin)
        if outcome != "200":
            failures += 1

    await asyncio.gather(*(one(name) for name in names))
    return failures


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def worker_loop(
    service: Service,
    domain: str,
    token: str,
    label: str,
    args: argparse.Namespace,
    stop: asyncio.Event,
) -> None:
    await wait_or_stop(stop, random.uniform(0, args.poll_seconds))
    while not stop.is_set():
        body: dict[str, object] = {
            "domain": domain,
            "topic": TOPIC,
            "hold_seconds": args.hold_seconds,
        }
        if args.wait_seconds:
            body["wait_seconds"] = args.wait_seconds
        started = time.monotonic()
        result = await own_or_stop(service, body, token, label, stop)
        if result is None:
            return  # Shutdown abandoned an idle long poll; nothing was claimed.
        outcome, value = result
        task = value.get("task") if value else None
        if isinstance(task, dict):
            await asyncio.sleep(args.work_seconds)
            await service.call(
                "consume",
                {"domain": domain, "topic": TOPIC, "id": task["id"], "tasks": []},
                token,
                label,
            )
            continue
        # As in WorkerLoop, time the service held an empty poll counts toward
        # the poll interval; a refused wait, a timeout or an error sleeps the
        # whole interval.
        idle = args.poll_seconds
        if outcome == "404 empty":
            idle = max(0.0, idle - (time.monotonic() - started))
        if idle > 0:
            await wait_or_stop(stop, idle * random.uniform(0.8, 1.2))


async def own_or_stop(
    service: Service,
    body: dict[str, object],
    token: str,
    label: str,
    stop: asyncio.Event,
) -> tuple[str, dict[str, object] | None] | None:
    """Own, abandoning the request if shutdown begins while it is held.

    Closing a held poll's connection cancels its Worker before any claim, so
    abandoning it cannot strand a task. A claim that already returned is
    finished normally, so the final drain sees every task.
    """
    own = asyncio.create_task(service.call("own", body, token, label))
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({own, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
    if own.done():
        return own.result()
    own.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await own
    return None


async def producer_loop(
    service: Service, domain: str, token: str, rate: float, stop: asyncio.Event
) -> None:
    if rate <= 0:
        return
    sequence = 0
    next_at = time.monotonic()
    pending: set[asyncio.Task[tuple[str, dict[str, object] | None]]] = set()
    while not stop.is_set():
        sequence += 1
        task = asyncio.create_task(
            service.call(
                "create",
                {"domain": domain, "topic": TOPIC, "data": {"n": sequence}},
                token,
                "producer",
            )
        )
        pending.add(task)
        task.add_done_callback(pending.discard)
        next_at += 1 / rate
        await wait_or_stop(stop, max(0.0, next_at - time.monotonic()))
    await asyncio.gather(*pending)


async def drain(service: Service, domain: str, token: str) -> int:
    drained = 0
    for _ in range(10_000):
        _, value = await service.call(
            "own", {"domain": domain, "topic": TOPIC, "hold_seconds": 60}, token
        )
        task = value.get("task") if value else None
        if not isinstance(task, dict):
            return drained
        await service.call(
            "consume",
            {"domain": domain, "topic": TOPIC, "id": task["id"], "tasks": []},
            token,
        )
        drained += 1
    return drained


def client(url: str, connections: int) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=connections, max_keepalive_connections=connections
    )
    # A held poll can outlast the fixed 60-second limit.
    return httpx.AsyncClient(base_url=url.rstrip("/"), limits=limits, timeout=120)


@dataclass(frozen=True)
class Shard:
    """One process's share of the simulated workers."""

    domain: str
    tokens: list[str]
    first_worker: int
    producer_token: str | None
    started: float


async def load_shard(shard: Shard, args: argparse.Namespace) -> Recorder:
    recorder = Recorder(started=shard.started)
    stop = asyncio.Event()
    async with client(args.url, args.connections) as http:
        service = Service(http, recorder)
        workers = [
            asyncio.create_task(
                worker_loop(
                    service,
                    shard.domain,
                    token,
                    f"w{shard.first_worker + i}",
                    args,
                    stop,
                )
            )
            for i, token in enumerate(shard.tokens)
        ]
        producer = asyncio.create_task(
            producer_loop(
                service,
                shard.domain,
                shard.producer_token or "",
                args.create_rate if shard.producer_token else 0,
                stop,
            )
        )
        await asyncio.sleep(shard.started + args.duration - time.time())
        stop.set()
        # Workers abandon only idle long polls; work in progress and pending
        # Creates finish, and any loop failure still fails the run.
        await asyncio.gather(producer, *workers)
    return recorder


def run_shard(shard: Shard, args: argparse.Namespace) -> Recorder:
    return asyncio.run(load_shard(shard, args))


async def set_up(
    args: argparse.Namespace, admin: str, domain: str, run_id: str, setup: Recorder
) -> tuple[list[tuple[str, str]], str, str]:
    async with client(args.url, 32) as http:
        service = Service(http, setup)
        print(f"Domain {domain}: adding {args.identities} identities", flush=True)
        identities = await add_identities(
            service, admin, domain, run_id, args.identities
        )
        producer_name = f"load-{run_id}-producer"
        outcome, created = await service.call(
            "identity.add",
            {
                "name": producer_name,
                "domains": [domain],
                "operator": False,
                "scopes": {"create": [TOPIC], "own": [], "read": []},
            },
            admin,
        )
    if created is None:
        raise RuntimeError(f"Could not add the producer identity: {outcome}")
    return identities, producer_name, str(created["token"])


async def tear_down(
    args: argparse.Namespace,
    admin: str,
    domain: str,
    names: list[str],
    drain_token: str,
    setup: Recorder,
) -> tuple[int, int]:
    async with client(args.url, 32) as http:
        service = Service(http, setup)
        try:
            drained = await drain(service, domain, drain_token)
        finally:
            failures = await revoke(service, admin, names)
    return drained, failures


def run(args: argparse.Namespace, admin: str) -> dict[str, object]:
    run_id = secrets.token_hex(4)
    domain = f"loadtest.{run_id}"
    setup = Recorder()
    identities, producer_name, producer_token = asyncio.run(
        set_up(args, admin, domain, run_id, setup)
    )
    names = [name for name, _ in identities] + [producer_name]
    tokens = [identities[i % len(identities)][1] for i in range(args.workers)]
    recorder = Recorder(started=time.time() + 2)
    try:
        cycle = max(args.poll_seconds, args.wait_seconds)
        print(
            f"Running {args.workers} workers in {args.processes} processes for "
            f"{args.duration}s (offered ~{args.workers / cycle:.0f} idle polls/s)",
            flush=True,
        )
        shards = [
            Shard(
                domain=domain,
                tokens=tokens[index :: args.processes],
                first_worker=index,
                producer_token=producer_token if index == 0 else None,
                started=recorder.started,
            )
            for index in range(args.processes)
        ]
        with ProcessPoolExecutor(max_workers=args.processes) as pool:
            for result in pool.map(run_shard, shards, [args] * len(shards)):
                recorder.merge(result)
        elapsed = time.time() - recorder.started
    finally:
        drained, failures = asyncio.run(
            tear_down(args, admin, domain, names, identities[0][1], setup)
        )
    windows = [
        {
            "start_s": index * WINDOW_SECONDS,
            "requests_per_s": round(counts["requests"] / WINDOW_SECONDS, 1),
            "errors": counts["errors"],
        }
        for index, counts in sorted(recorder.windows.items())
        if index >= 0
    ]
    return {
        "url": args.url,
        "domain": domain,
        "config": {
            key: getattr(args, key)
            for key in (
                "workers",
                "identities",
                "poll_seconds",
                "wait_seconds",
                "duration",
                "create_rate",
                "work_seconds",
                "hold_seconds",
                "connections",
                "processes",
            )
        },
        "offered_idle_polls_per_s": round(
            args.workers / max(args.poll_seconds, args.wait_seconds), 1
        ),
        "elapsed_s": round(elapsed, 1),
        "operations": {
            op: stats.summary(elapsed) for op, stats in sorted(recorder.ops.items())
        },
        "windows": windows,
        "drained_after_run": drained,
        "setup": {op: stats.summary(1) for op, stats in sorted(setup.ops.items())},
        "revoke_failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--workers", type=int, default=1000)
    parser.add_argument("--identities", type=int, default=0, help="default: workers")
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Own long-poll seconds; 0 keeps immediate polling",
    )
    parser.add_argument("--duration", type=float, default=240)
    parser.add_argument("--create-rate", type=float, default=2)
    parser.add_argument("--work-seconds", type=float, default=2)
    parser.add_argument("--hold-seconds", type=int, default=60)
    parser.add_argument("--connections", type=int, default=100, help="per process")
    parser.add_argument(
        "--processes",
        type=int,
        default=1,
        help="client processes; one asyncio client saturates near 100 requests/s",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.identities:
        args.identities = args.workers
    if (
        args.workers < 1
        or args.identities < 1
        or args.poll_seconds <= 0
        or not 0 <= args.wait_seconds <= 30
        or not 1 <= args.processes <= args.workers
    ):
        parser.error(
            "workers, identities and poll seconds must be positive, "
            "--wait-seconds between 0 and 30, and processes between 1 and the "
            "worker count"
        )
    admin = os.environ.get("TL_ADMIN_TOKEN")
    if not admin:
        parser.error("set TL_ADMIN_TOKEN to the deployment's admin token")
    report = run(args, admin)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    operations = report["operations"]
    if isinstance(operations, dict):
        for op, summary in operations.items():
            print(op, json.dumps(summary), flush=True)
    print(f"Results: {args.output}", flush=True)


if __name__ == "__main__":
    main()
