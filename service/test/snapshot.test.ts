import { env, exports } from "cloudflare:workers";
import {
  evictDurableObject,
  runDurableObjectAlarm,
  runInDurableObject,
} from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import type {
  IdentityAddResponse,
  MonitorSnapshotResponse,
  Scopes,
} from "../src/generated/protocol";
import { validateMonitorSnapshotResponse } from "../src/generated/validators.js";
import type { Caller } from "../src/types";
import { dropCompletionLatencyColumns } from "./migrations";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const operator: Caller = {
  identity: "snapshot-operator",
  worker: "snapshot-worker",
  operator: true,
  scopes: { create: [], read: [], own: [] },
};

function fixture(label: string) {
  const domain = `snapshot/${label}/${crypto.randomUUID()}`;
  return { domain, stub: env.DOMAINS.getByName(domain) };
}

async function snapshot(response: Response): Promise<MonitorSnapshotResponse> {
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  if (!validateMonitorSnapshotResponse(body)) {
    throw new Error(JSON.stringify(validateMonitorSnapshotResponse.errors));
  }
  return body;
}

function post(
  operation: string,
  token: string,
  body: unknown,
): Promise<Response> {
  return exports.default.fetch(`http://tasklease/v1/${operation}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "X-Worker": "snapshot-http" },
    body: JSON.stringify(body),
  });
}

it("projects paired completion latency into the Consume hour only", async () => {
  const { domain, stub } = fixture("completion-latency");
  const topic = "work";
  const hour = Math.ceil(Date.now() / HOUR) * HOUR + DAY;
  await runInDurableObject(stub, async (instance) => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(hour - 180_000);
    try {
      expect(
        await instance.create({ domain, topic, data: {} }, operator),
      ).toMatchObject({ ok: true });
      clock.mockReturnValue(hour - 120_000);
      const first = await instance.own(
        { domain, topic, hold_seconds: 600 },
        operator,
      );
      if (!first.ok) throw new Error("Could not claim the task");

      clock.mockReturnValue(hour + 30_000);
      const moved = await instance.consume(
        { domain, topic, id: first.task.id, tasks: [{ topic, data: {} }] },
        operator,
      );
      const successor = moved.ok ? moved.ids[0] : undefined;
      if (!successor) throw new Error("Could not consume the task");
      // An operator can consume an unclaimed task. Its successor starts a new interval.
      const forwarded = await instance.consume(
        { domain, topic, id: successor, tasks: [{ topic, data: {} }] },
        operator,
      );
      expect(forwarded.ok).toBe(true);

      clock.mockReturnValue(hour + 60_000);
      const second = await instance.own(
        { domain, topic, hold_seconds: 600 },
        operator,
      );
      if (!second.ok) throw new Error("Could not claim the successor");
      clock.mockReturnValue(hour + 120_000);
      expect(
        await instance.consume(
          { domain, topic, id: second.task.id, tasks: [] },
          operator,
        ),
      ).toMatchObject({ ok: true });

      const result = await snapshot(
        await instance.monitorSnapshot({ domain, hours: 2 }, operator),
      );
      expect(result.stats).toHaveLength(2);
      expect(result.stats[0]).toMatchObject({
        hour: hour - HOUR,
        wait_count: 1,
        consumed: 0,
      });
      expect(result.stats[0]).not.toHaveProperty("completion_latency");
      expect(result.stats[1]).toMatchObject({
        hour,
        wait_count: 1,
        consumed: 3,
        completion_latency: {
          wait_ms_sum: 90_000,
          after_claim_ms_sum: 210_000,
          count: 2,
        },
      });
    } finally {
      clock.mockRestore();
    }
  });
});

async function identity(
  domain: string,
  scopes: Scopes,
  privileged = false,
): Promise<string> {
  const response = await post("identity.add", "test-admin-token", {
    name: `snapshot-${crypto.randomUUID()}`,
    domains: [domain],
    scopes,
    operator: privileged,
  });
  expect(response.status).toBe(200);
  return (await response.json<IdentityAddResponse>()).token;
}

describe("monitor snapshots", () => {
  it("filters by read OR own, including historical statistics independently of live topics", async () => {
    const { domain, stub } = fixture("scopes");
    await runInDurableObject(stub, async (instance, state) => {
      for (const topic of ["read/queue", "own/queue", "private/queue"]) {
        await instance.create(
          { domain, topic, data: { secret: true } },
          operator,
        );
      }
      for (const topic of ["own/history", "private/history"]) {
        state.storage.sql.exec(
          `INSERT INTO stats_hourly (topic, hour, consumed, completion_wait_ms_sum,
             completion_after_claim_ms_sum, completion_count) VALUES (?, ?, 1, 1000, 2000, 1)`,
          topic,
          Math.floor(Date.now() / HOUR) * HOUR,
        );
      }
    });
    const token = await identity(domain, {
      create: ["private/**"],
      read: ["read/**"],
      own: ["own/**"],
    });
    const result = await snapshot(
      await post("monitor.snapshot", token, { domain }),
    );
    expect(result.topics.map((row) => row.topic)).toEqual([
      "own/queue",
      "read/queue",
    ]);
    expect(result.stats.map((row) => row.topic)).toEqual([
      "own/history",
      "own/queue",
      "read/queue",
    ]);
    expect(result.stats[0]?.completion_latency).toEqual({
      wait_ms_sum: 1000,
      after_claim_ms_sum: 2000,
      count: 1,
    });
    expect(JSON.stringify(result)).not.toContain("secret");
    expect(result.topics.every((row) => !Object.hasOwn(row, "truncated"))).toBe(
      true,
    );
    const empty = await identity(domain, { create: ["**"], read: [], own: [] });
    expect(
      await snapshot(await post("monitor.snapshot", empty, { domain })),
    ).toMatchObject({ topics: [], stats: [] });
    const privileged = await identity(
      domain,
      { create: [], read: [], own: [] },
      true,
    );
    expect(
      (await snapshot(await post("monitor.snapshot", privileged, { domain })))
        .topics,
    ).toHaveLength(3);
    expect(
      (
        await post("monitor.snapshot", privileged, {
          domain: domain + "/other",
        })
      ).status,
    ).toBe(403);
  });

  it("returns exactly the first 100 ordered metadata summaries and a consistent pre-mutation projection", async () => {
    const { domain, stub } = fixture("metadata");
    let expected: string[] = [];
    await runInDurableObject(stub, async (instance) => {
      for (let index = 0; index < 101; index++) {
        await instance.create(
          { domain, topic: "work", data: { secret: "x".repeat(250_000) } },
          operator,
        );
      }
      await instance.own({ domain, topic: "work", hold_seconds: 60 }, operator);
      const listed = await instance.tasksList(
        { domain, topic: "work" },
        operator,
      );
      if (!listed.ok) throw new Error("Could not list test tasks");
      expected = listed.tasks.slice(0, 100).map((row) => row.id);
      await instance.topicDeclare(
        { domain, topic: "declared/empty", declared: true },
        operator,
      );
    });
    const response = await stub.monitorSnapshot({ domain }, operator);
    // Mutation after the RPC returns cannot change the spooled projection.
    await stub.create({ domain, topic: "later", data: {} }, operator);
    const result = await snapshot(response);
    expect(result.topics.map((row) => row.topic)).toEqual([
      "declared/empty",
      "work",
    ]);
    const work = result.topics[1]!;
    expect(work).toMatchObject({ available: 100, held: 1, truncated: true });
    expect(work.tasks.map((row) => row.id)).toEqual(expected);
    expect(work.tasks.every((row) => !Object.hasOwn(row, "data"))).toBe(true);
    expect(work.pollers).toMatchObject([
      { identity: operator.identity, worker: operator.worker },
    ]);
    expect(result.topics[0]).toMatchObject({
      declared: true,
      tasks: [],
      pollers: [],
    });
    expect(result.stats.map((row) => row.topic)).toEqual(["work"]);
    await stub.delete({ domain, topic: "work", id: expected[0]! }, operator);
    const hundred = (
      await snapshot(await stub.monitorSnapshot({ domain }, operator))
    ).topics.find((row) => row.topic === "work")!;
    expect(hundred.tasks).toHaveLength(100);
    expect(Object.hasOwn(hundred, "truncated")).toBe(false);
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
          .one(),
      ).toEqual({ count: 0 });
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshot_chunks")
          .one(),
      ).toEqual({ count: 0 });
    });
  });

  it("uses the current hour plus N-1 prior buckets and excludes future/stale rows", async () => {
    const { domain, stub } = fixture("hours");
    await runInDurableObject(stub, async (instance, state) => {
      const hour = Math.floor(Date.now() / HOUR) * HOUR;
      const now = hour + HOUR / 2;
      const clock = vi.spyOn(Date, "now").mockReturnValue(now);
      try {
        for (const delta of [-720, -719, -24, -23, 0, 1]) {
          state.storage.sql.exec(
            "INSERT INTO stats_hourly (topic, hour, created) VALUES (?, ?, 1)",
            "history",
            hour + delta * HOUR,
          );
        }
        for (const [topic, age] of [
          ["poller/current", 7 * DAY],
          ["poller/stale", 7 * DAY + 1],
        ] as const) {
          state.storage.sql.exec(
            "INSERT INTO pollers (topic, identity, worker, last_polled_at) VALUES (?, ?, ?, ?)",
            topic,
            operator.identity,
            operator.worker,
            now - age,
          );
        }
        const one = await snapshot(
          await instance.monitorSnapshot({ domain, hours: 1 }, operator),
        );
        expect(one.generated_at).toBe(now);
        expect(one.stats.map((row) => row.hour)).toEqual([hour]);
        expect(one.topics.map((row) => row.topic)).toEqual(["poller/current"]);
        expect(one.topics[0]!.pollers[0]!.last_owned_at).toBeNull();
        const defaults = await snapshot(
          await instance.monitorSnapshot({ domain }, operator),
        );
        expect(defaults.stats.map((row) => row.hour)).toEqual([
          hour - 23 * HOUR,
          hour,
        ]);
        const maximum = await snapshot(
          await instance.monitorSnapshot({ domain, hours: 720 }, operator),
        );
        expect(maximum.stats.map((row) => row.hour)).toEqual([
          hour - 719 * HOUR,
          hour - 24 * HOUR,
          hour - 23 * HOUR,
          hour,
        ]);
      } finally {
        clock.mockRestore();
      }
    });
  });

  it.each([0, 721, 1.5, "24", null])(
    "rejects invalid hours %s",
    async (hours) => {
      const { domain } = fixture("invalid");
      const token = await identity(domain, {
        create: [],
        read: ["**"],
        own: [],
      });
      const response = await post("monitor.snapshot", token, { domain, hours });
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({
        ok: false,
        error: { code: "invalid" },
      });
    },
  );

  it("bounds active projections, frees cancellation capacity, and expires abandoned streams", async () => {
    const { domain, stub } = fixture("lifetime");
    await runInDurableObject(stub, async (instance, state) => {
      const first = await instance.monitorSnapshot({ domain }, operator);
      const second = await instance.monitorSnapshot({ domain }, operator);
      expect(
        (await instance.monitorSnapshot({ domain }, operator)).status,
      ).toBe(503);
      await first.body!.cancel();
      const replacement = await instance.monitorSnapshot({ domain }, operator);
      expect(replacement.status).toBe(200);
      await replacement.body!.cancel();
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
          .one(),
      ).toEqual({ count: 1 });
      expect(await state.storage.getAlarm()).toBeLessThanOrEqual(
        Date.now() + 5 * 60_000,
      );
      const clock = vi
        .spyOn(Date, "now")
        .mockReturnValue(Date.now() + 5 * 60_000 + 1);
      try {
        await instance.alarm();
        expect(
          state.storage.sql
            .exec("SELECT COUNT(*) AS count FROM monitor_snapshot_chunks")
            .one(),
        ).toEqual({ count: 0 });
        expect(
          state.storage.sql
            .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
            .one(),
        ).toEqual({ count: 0 });
        await expect(second.text()).rejects.toThrow("Internal server error");
        expect(await state.storage.getAlarm()).toBe(Date.now() + DAY);
      } finally {
        clock.mockRestore();
      }
    });
    expect(await runDurableObjectAlarm(stub)).toBe(true);
  });

  it("terminates incomplete chunk sequences and cleans their projection", async () => {
    const { domain, stub } = fixture("incomplete");
    await runInDurableObject(stub, async (instance, state) => {
      const response = await instance.monitorSnapshot({ domain }, operator);
      state.storage.sql.exec("DELETE FROM monitor_snapshot_chunks");
      await expect(response.text()).rejects.toThrow("Internal server error");
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
          .one(),
      ).toEqual({ count: 0 });
    });
  });

  it("upgrades version 2 without changing task state and discards orphaned projections on eviction", async () => {
    const { domain, stub } = fixture("migration");
    const created = await stub.create(
      { domain, topic: "work", data: { retained: true } },
      operator,
    );
    if (!created.ok) throw new Error("Could not create test task");
    const before = await stub.read(
      { domain, topic: "work", id: created.id },
      operator,
    );
    await runInDurableObject(stub, (_instance, state) => {
      dropCompletionLatencyColumns(state.storage.sql);
      state.storage.sql.exec(
        "DROP TABLE create_keys; DROP TABLE monitor_snapshot_chunks; DROP TABLE monitor_snapshots; DROP INDEX tasks_root_origin; ALTER TABLE tasks DROP COLUMN root; ALTER TABLE tasks DROP COLUMN origin_id; UPDATE schema_version SET version = 2",
      );
    });
    await evictDurableObject(stub);
    expect(
      await stub.read({ domain, topic: "work", id: created.id }, operator),
    ).toEqual(before);
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql.exec("SELECT version FROM schema_version").one(),
      ).toEqual({ version: 6 });
      state.storage.sql.exec(
        "INSERT INTO monitor_snapshots (id, expires_at) VALUES ('orphan', ?)",
        Date.now() + DAY,
      );
      state.storage.sql.exec(
        "INSERT INTO monitor_snapshot_chunks (snapshot_id, seq, data) VALUES ('orphan', 0, ?)",
        new ArrayBuffer(1),
      );
    });
    await evictDurableObject(stub);
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
          .one(),
      ).toEqual({ count: 0 });
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshot_chunks")
          .one(),
      ).toEqual({ count: 0 });
    });
  });

  it("streams a consistent valid snapshot beyond the 32 MiB RPC object limit", async () => {
    const { domain, stub } = fixture("large");
    const hour = Math.floor(Date.now() / HOUR) * HOUR;
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        `WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x < 999)
         INSERT INTO stats_hourly (topic, hour, created)
         SELECT 'history/' || printf('%0120d', a.x * 110 + b.x), ?, 1
         FROM n a CROSS JOIN n b WHERE b.x < 110`,
        hour,
      );
    });
    const response = await stub.monitorSnapshot({ domain }, operator);
    expect(response.status).toBe(200);
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let bytes = 0;
    let count = 0;
    let pending = "";
    let header = true;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      pending += decoder.decode(value, { stream: true });
      if (header) {
        const start = pending.indexOf('"topics":[],"stats":[');
        if (start < 0) continue;
        pending = pending.slice(start + '"topics":[],"stats":['.length);
        header = false;
      }
      for (;;) {
        if (pending.startsWith(",")) pending = pending.slice(1);
        if (!pending.startsWith("{")) break;
        const end = pending.indexOf("}");
        if (end < 0) break;
        const row: unknown = JSON.parse(pending.slice(0, end + 1));
        if (
          typeof row !== "object" ||
          row === null ||
          !("topic" in row) ||
          !("created" in row) ||
          row.created !== 1
        ) {
          throw new Error("Invalid streamed statistics row");
        }
        count++;
        pending = pending.slice(end + 1);
      }
    }
    expect(bytes).toBeGreaterThan(32 * 1024 * 1024);
    expect(count).toBe(110_000);
    expect(pending + decoder.decode()).toBe("]}");
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshot_chunks")
          .one(),
      ).toEqual({ count: 0 });
      expect(
        state.storage.sql
          .exec("SELECT COUNT(*) AS count FROM monitor_snapshots")
          .one(),
      ).toEqual({ count: 0 });
    });
  }, 60_000);
});
