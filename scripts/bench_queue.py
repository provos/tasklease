#!/usr/bin/env python3
"""Disposable SQLite microbenchmark for tasklease's current domain queries.

Run from the repository:
  python3 scripts/bench_queue.py --output /tmp/tasklease-queue-bench-results.json

Reads the actual schema and SQL from domain_do.ts; changes no repository files
unless --output explicitly names a repository path.
Uses one file-backed WAL database per size and removes it after measurement.
This measures local SQLite, including local commits for Own, rather than HTTP,
Workers CPU, authentication, JSON serialization, or replicated durability.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def extract(pattern: str, source: str) -> str:
    match = re.search(pattern, source, re.S)
    if match is None:
        raise ValueError(f"Cannot find production SQL: {pattern}")
    return match.group(1)


def measure(action: Callable[[], object], samples: int) -> dict[str, int | float]:
    for _ in range(3):
        action()
    values = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        action()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(values)
    return {
        "samples": samples,
        "p50_ms": round(statistics.median(values), 4),
        "p95_ms": round(ordered[math.ceil(0.95 * samples) - 1], 4),
        "max_ms": round(max(values), 4),
    }


def run_size(
    size: int, args: argparse.Namespace, sql: dict[str, str], payload: str
) -> dict[str, object]:
    topic = "work/bench"
    now = 1_800_000_000_000
    future = now + 600_000
    hour = now // 3_600_000 * 3_600_000
    with tempfile.TemporaryDirectory(prefix="tasklease-queue-bench-") as directory:
        path = Path(directory) / "tasks.sqlite"
        db = sqlite3.connect(path, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-65536")
        db.executescript(sql["schema"])
        started = time.perf_counter()
        db.execute("BEGIN")
        db.executemany(
            sql["insert"],
            (
                (
                    f"t_{index:026d}",
                    topic,
                    now - size + index,
                    0,
                    0,
                    "requester",
                    None,
                    None,
                    None,
                    payload,
                )
                for index in range(size)
            ),
        )
        db.execute("COMMIT")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        seed_seconds = time.perf_counter() - started
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == size
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        cases: dict[str, object] = {}
        database_mib = round(page_count * page_size / 2**20, 2)
        result: dict[str, object] = {
            "tasks": size,
            "payload_bytes": len(payload.encode()),
            "database_mib": database_mib,
            "seed_seconds": round(seed_seconds, 3),
            "query_plan": [
                list(row)
                for row in db.execute("EXPLAIN QUERY PLAN " + sql["pick"], (topic, now))
            ],
            "cases": cases,
        }
        print(
            f"Seeded {size:,} tasks in {seed_seconds:.2f}s; "
            f"database {database_mib:.2f} MiB",
            flush=True,
        )

        # A successful Own commits the same SQL writes as the service, then an
        # untimed transaction restores the original task for the next sample.
        def own_once() -> float:
            started = time.perf_counter_ns()
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(sql["pick"], (topic, now)).fetchone()
            db.execute(
                sql["poller"], (topic, "worker", "worker-1", now, now if row else None)
            )
            if row:
                db.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
                db.execute(
                    sql["insert"],
                    (
                        "t_ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
                        topic,
                        row["created_at"],
                        future,
                        row["owned_count"] + 1,
                        row["creator"],
                        "worker",
                        "worker-1",
                        now,
                        row["data"],
                    ),
                )
                first = row["owned_count"] == 0
                wait = now - row["created_at"] if first else 0
                stats = (0, 1, int(not first), 0, 0, 0, wait, wait, int(first), 0, 0, 0)
            else:
                stats = (0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
            db.execute(sql["stats"], (topic, hour, *stats))
            db.execute("COMMIT")
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if row:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM tasks WHERE id = ?", ("t_ZZZZZZZZZZZZZZZZZZZZZZZZZZ",)
                )
                db.execute(sql["insert"], tuple(row))
                db.execute("COMMIT")
            return elapsed

        def measure_own() -> dict[str, int | float]:
            for _ in range(3):
                own_once()
            values = [own_once() for _ in range(args.samples)]
            ordered = sorted(values)
            return {
                "samples": args.samples,
                "p50_ms": round(statistics.median(values), 4),
                "p95_ms": round(ordered[math.ceil(0.95 * args.samples) - 1], 4),
                "max_ms": round(max(values), 4),
            }

        for name in ("available_head", "held_prefix", "available_tail", "all_held"):
            skipped = 0
            if name == "held_prefix":
                skipped = min(args.held_prefix, size - 1)
                db.execute(
                    "UPDATE tasks SET lease_until = ? WHERE created_at < ?",
                    (future, now - size + skipped),
                )
            elif name == "available_tail":
                skipped = size - 1
                db.execute("UPDATE tasks SET lease_until = ?", (future,))
                db.execute(
                    "UPDATE tasks SET lease_until = 0 WHERE id = ?",
                    (f"t_{size - 1:026d}",),
                )
            elif name == "all_held":
                skipped = size
                db.execute("UPDATE tasks SET lease_until = ?", (future,))
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            picked = db.execute(sql["pick"], (topic, now)).fetchone()
            assert (picked is None) == (name == "all_held")
            if picked:
                assert picked["id"] == f"t_{skipped:026d}"
            pick_metrics = measure(
                lambda: db.execute(sql["pick"], (topic, now)).fetchone(), args.samples
            )
            own_metrics = measure_own()
            case = {
                "held_rows_before_candidate": skipped,
                "pick": pick_metrics,
                "own_sql_transaction": own_metrics,
            }
            assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == size
            cases[name] = case
            print(
                f"  {name}: pick p95={pick_metrics['p95_ms']:.4f}ms; "
                f"Own SQL p95={own_metrics['p95_ms']:.4f}ms",
                flush=True,
            )

        topic_args = (now - 7 * 86_400_000, now, now, now)
        summary = db.execute(sql["topics"], topic_args).fetchall()
        assert len(summary) == 1 and summary[0]["held"] == size
        topics_metrics = measure(
            lambda: db.execute(sql["topics"], topic_args).fetchall(), args.samples
        )
        result["topics_list"] = topics_metrics
        result["empty_topic_pick"] = measure(
            lambda: db.execute(sql["pick"], ("work/empty", now)).fetchone(),
            args.samples,
        )
        print(f"  topics.list p95={topics_metrics['p95_ms']:.4f}ms", flush=True)
        db.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[100, 10_000, 100_000, 1_000_000]
    )
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--held-prefix", type=int, default=32)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        min(args.sizes) < 1
        or args.samples < 1
        or args.payload_bytes < 12
        or args.held_prefix < 0
    ):
        parser.error("sizes/samples must be positive, payload >= 12, held prefix >= 0")
    source_path = args.repo / "service/src/domain_do.ts"
    source = source_path.read_text()
    sql = {
        "schema": extract(r"const migrations = \[\s*`([^`]+)`", source),
        "pick": extract(r"async own\(.*?`(SELECT \* FROM tasks[^`]+)`", source),
        "insert": extract(r"private insertRow\(.*?`(INSERT INTO tasks[^`]+)`", source),
        "poller": extract(r"async own\(.*?`(INSERT INTO pollers[^`]+)`", source),
        "stats": extract(
            r"private recordStats\(.*?`(INSERT INTO stats_hourly[^`]+)`", source
        ),
        "topics": extract(r"const topicQuery = `([^`]+)`", source),
    }
    payload = json.dumps(
        {"value": "x" * (args.payload_bytes - 12)}, separators=(",", ":")
    )
    assert len(payload.encode()) == args.payload_bytes
    results: list[dict[str, object]] = []
    report: dict[str, object] = {
        "method": (
            "Local SQLite microbenchmark; warm queries; WAL; synchronous FULL; "
            "64 MiB page cache; one topic"
        ),
        "exclusions": (
            "No HTTP, auth, JS runtime, JSON serialization, replication, or "
            "concurrency; seed time is bulk insert, not API throughput"
        ),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "source": str(source_path.relative_to(args.repo)),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "sql": sql,
        "results": results,
    }
    for size in args.sizes:
        results.append(run_size(size, args, sql, payload))
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Results: {args.output}", flush=True)


if __name__ == "__main__":
    main()
