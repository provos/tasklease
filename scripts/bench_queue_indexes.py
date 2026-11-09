#!/usr/bin/env python3
"""Compare queue indexes and topic queries in disposable SQLite databases.

Run from any directory:
  python3 /path/to/tasklease/scripts/bench_queue_indexes.py --output /tmp/indexes.json

Reads the service's schema and queries, then measures explicit index variants
and both topic-name sources (tasks and their aggregate). The held prefix varies
by changing the query clock against expiries that decrease with creation order.
This deliberately exposes the sort needed by an index that orders by expiry.

Queries run after warmup with a 64 MiB SQLite page cache and file-backed WAL.
Renewal samples roll back 64 updates and exclude durability costs. This measures
local SQL, not the HTTP service or deployed throughput. Temporary databases are
removed on exit. --maintained-totals includes the experimental counter query;
its total is seeded once, so counter-maintenance writes are not measured.
"""

import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from functools import partial
from pathlib import Path

from bench_queue import load_sql, measure

PROBE = """SELECT 1 FROM tasks INDEXED BY tasks_expiry
WHERE topic = ? AND lease_until <= ? LIMIT 1"""
NARROW_PICK = """SELECT * FROM tasks WHERE id = (
  SELECT id FROM tasks WHERE topic = ? AND lease_until <= ?
  ORDER BY created_at, id LIMIT 1
)"""
TOTALS_QUERY = """WITH held_counts AS MATERIALIZED (
  SELECT topic, total,
    CASE WHEN EXISTS (
      SELECT 1 FROM tasks INDEXED BY tasks_expiry
      WHERE topic = c.topic AND lease_until <= ? LIMIT 1
    ) THEN (
      SELECT COUNT(*) FROM tasks INDEXED BY tasks_expiry
      WHERE topic = c.topic AND lease_until > ?
    ) ELSE total END AS held
  FROM task_counts c
)
SELECT topic, total - held AS available, held,
  CASE WHEN held = total THEN NULL ELSE (
    SELECT created_at FROM tasks INDEXED BY tasks_pick
    WHERE topic = h.topic AND lease_until <= ?
    ORDER BY created_at, id LIMIT 1
  ) END AS oldest_available_at
FROM held_counts h"""


def variants(pick: str) -> dict[str, tuple[list[str], str]]:
    return {
        "creation_order": (
            ["CREATE INDEX tasks_pick ON tasks(topic, created_at, id)"],
            pick,
        ),
        "expiry_appended": (
            ["CREATE INDEX tasks_pick ON tasks(topic, created_at, id, lease_until)"],
            pick,
        ),
        "expiry_after_topic": (
            ["CREATE INDEX tasks_pick ON tasks(topic, lease_until, created_at, id)"],
            pick,
        ),
        "expiry_after_topic_narrow_pick": (
            ["CREATE INDEX tasks_pick ON tasks(topic, lease_until, created_at, id)"],
            NARROW_PICK,
        ),
        "expiry_appended_with_probe": (
            [
                "CREATE INDEX tasks_pick ON tasks(topic, created_at, id, lease_until)",
                "CREATE INDEX tasks_expiry ON tasks(topic, lease_until)",
            ],
            pick.replace("FROM tasks", "FROM tasks INDEXED BY tasks_pick"),
        ),
    }


def pick_task(
    db: sqlite3.Connection, query: str, topic: str, now: int, probe: bool
) -> sqlite3.Row | None:
    if probe and db.execute(PROBE, (topic, now)).fetchone() is None:
        return None
    row: sqlite3.Row | None = db.execute(query, (topic, now)).fetchone()
    return row


def fetch_rows(
    db: sqlite3.Connection, query: str, params: tuple[object, ...]
) -> list[sqlite3.Row]:
    return db.execute(query, params).fetchall()


def renewal_batch(db: sqlite3.Connection, ids: list[str]) -> None:
    db.execute("BEGIN")
    db.executemany(
        "UPDATE tasks SET lease_until = lease_until + 1 WHERE id = ?",
        ((task_id,) for task_id in ids),
    )
    db.execute("ROLLBACK")


def run_size(
    size: int, args: argparse.Namespace, sql: dict[str, str]
) -> dict[str, object]:
    topic = "work/bench"
    base = 1_800_000_000_000
    held_prefix = min(args.held_prefix, size - 1)
    clocks = {
        "available_head": base,
        "held_prefix": base - held_prefix,
        "available_tail": base - size + 1,
        "all_held": base - size,
    }
    expected = {
        "available_head": 0,
        "held_prefix": held_prefix,
        "available_tail": size - 1,
    }
    legacy_topics = sql["topics"].replace(
        "SELECT topic FROM counts UNION", "SELECT topic FROM tasks UNION", 1
    )
    reused_topics = sql["topics"].replace(
        "SELECT topic FROM tasks UNION", "SELECT topic FROM counts UNION", 1
    )
    if legacy_topics == reused_topics:
        raise ValueError(
            "Cannot locate the live topic-name source in the service query"
        )
    variant_results: dict[str, object] = {}
    output: dict[str, object] = {
        "tasks": size,
        "payload_bytes": args.payload_bytes,
        "held_prefix": held_prefix,
        "variants": variant_results,
    }
    with (
        tempfile.TemporaryDirectory(prefix="tasklease-index-bench-") as directory,
        closing(
            sqlite3.connect(Path(directory) / "tasks.sqlite", isolation_level=None)
        ) as db,
    ):
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-65536")
        db.executescript(sql["schema"])
        db.execute("DROP INDEX tasks_pick")
        payload = json.dumps(
            {"value": "x" * (args.payload_bytes - 12)}, separators=(",", ":")
        )
        started = time.perf_counter()
        db.execute("BEGIN")
        db.executemany(
            sql["insert"],
            (
                (
                    f"t_{i:026d}",
                    topic,
                    base - 2 * size + i,
                    base - i,
                    1,
                    "requester",
                    "worker",
                    "worker-1",
                    base - 2 * size,
                    payload,
                )
                for i in range(size)
            ),
        )
        db.execute("COMMIT")
        if args.maintained_totals:
            db.execute(
                "CREATE TABLE task_counts("
                "topic TEXT PRIMARY KEY, total INTEGER NOT NULL)"
            )
            db.execute("INSERT INTO task_counts VALUES (?, ?)", (topic, size))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(
            f"Seeded {size:,} tasks in {time.perf_counter() - started:.2f}s", flush=True
        )

        for name, (indexes, query) in variants(sql["pick"]).items():
            if name not in args.variants:
                continue
            db.execute("DROP INDEX IF EXISTS tasks_pick")
            db.execute("DROP INDEX IF EXISTS tasks_expiry")
            pages_before = (
                db.execute("PRAGMA page_count").fetchone()[0]
                - db.execute("PRAGMA freelist_count").fetchone()[0]
            )
            for ddl in indexes:
                db.execute(ddl)
            index_pages = (
                db.execute("PRAGMA page_count").fetchone()[0]
                - db.execute("PRAGMA freelist_count").fetchone()[0]
                - pages_before
            )
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cases: dict[str, object] = {}
            result: dict[str, object] = {
                "indexes": indexes,
                "pick_sql": query,
                "pick_plan": [
                    list(row)
                    for row in db.execute("EXPLAIN QUERY PLAN " + query, (topic, base))
                ],
                "index_mib": round(
                    index_pages * db.execute("PRAGMA page_size").fetchone()[0] / 2**20,
                    3,
                ),
                "index_size_method": "Active pages added when creating the indexes",
                "cases": cases,
            }
            print(f"  {name}", flush=True)
            for case, now in clocks.items():
                pick = partial(
                    pick_task,
                    db,
                    query,
                    topic,
                    now,
                    name == "expiry_appended_with_probe",
                )
                value = pick()
                assert (value is None) == (case == "all_held")
                if value:
                    assert value["id"] == f"t_{expected[case]:026d}"
                pick_metrics = measure(pick, args.samples)
                case_result: dict[str, object] = {"pick": pick_metrics}
                params = (now - 7 * 86_400_000, now, now, now)
                original = db.execute(legacy_topics, params).fetchall()
                updated = db.execute(reused_topics, params).fetchall()
                assert [tuple(row) for row in original] == [
                    tuple(row) for row in updated
                ]
                for label, topic_query in (
                    ("topics_from_tasks", legacy_topics),
                    ("topics_from_counts", reused_topics),
                ):
                    case_result[label] = measure(
                        partial(fetch_rows, db, topic_query, params), args.samples
                    )
                if args.maintained_totals and name == "expiry_appended_with_probe":
                    totals = db.execute(TOTALS_QUERY, (now, now, now)).fetchall()
                    assert len(totals) == 1
                    for key in ("topic", "available", "held", "oldest_available_at"):
                        assert totals[0][key] == original[0][key]
                    case_result["maintained_totals"] = measure(
                        partial(fetch_rows, db, TOTALS_QUERY, (now, now, now)),
                        args.samples,
                    )
                cases[case] = case_result
                print(
                    f"    {case}: pick p50={pick_metrics['p50_ms']:.4f}ms", flush=True
                )

            ids = [f"t_{i * max(1, size // 64):026d}" for i in range(min(64, size))]

            result["renew_rollback"] = {
                "updates": len(ids),
                "timing": measure(partial(renewal_batch, db, ids), args.samples),
            }
            variant_results[name] = result
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--held-prefix", type=int, default=32)
    parser.add_argument(
        "--variants", nargs="+", choices=list(variants("")), default=list(variants(""))
    )
    parser.add_argument("--maintained-totals", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        min(args.sizes) < 1
        or args.samples < 1
        or args.payload_bytes < 12
        or args.held_prefix < 0
    ):
        parser.error("sizes/samples must be positive, payload >= 12, held prefix >= 0")
    source, sql = load_sql(args.repo)
    results: list[dict[str, object]] = []
    report: dict[str, object] = {
        "sqlite": sqlite3.sqlite_version,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "method": (
            "Local SQLite; 64 MiB page cache; file-backed WAL; 3 warmups; "
            "one topic; expiry decreases as creation time increases"
        ),
        "exclusions": "No HTTP, auth, JSON serialization, replication, or concurrency",
        "renewal_method": "Up to 64 updates, rolled back; excludes durability costs",
        "sql": sql,
        "maintained_totals_sql": TOTALS_QUERY if args.maintained_totals else None,
        "results": results,
    }
    for size in args.sizes:
        results.append(run_size(size, args, sql))
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Results: {args.output}", flush=True)


if __name__ == "__main__":
    main()
