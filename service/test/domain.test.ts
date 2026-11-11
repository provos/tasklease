import { env } from "cloudflare:workers";
import {
  evictDurableObject,
  runDurableObjectAlarm,
  runInDurableObject,
} from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import { DomainObject, ownWithWait, type RpcTask } from "../src/domain_do";
import type { Data, HourlyStats } from "../src/generated/protocol";
import { newTaskId } from "../src/ids";
import type { Caller, Result } from "../src/types";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const creator: Caller = {
  identity: "requester",
  worker: "requester-1",
  operator: false,
  scopes: { create: ["**"], own: [], read: ["**"] },
};
const worker: Caller = {
  identity: "worker",
  worker: "worker-1",
  operator: false,
  scopes: { create: ["result/**"], own: ["work/**"], read: ["work/**"] },
};
const operator: Caller = { ...creator, identity: "operator", operator: true };

function fixture(label: string) {
  const domain = `domain-tests/${label}/${crypto.randomUUID()}`;
  return { domain, stub: env.DOMAINS.getByName(domain) };
}

function success<T extends { ok: boolean }>(
  result: T,
): Extract<T, { ok: true }> {
  expect(result.ok).toBe(true);
  if (!result.ok) throw new Error(JSON.stringify(result));
  return result as Extract<T, { ok: true }>;
}

function error(result: Result<{ ok: true }>, code: string) {
  expect(result).toMatchObject({ ok: false, error: { code } });
}

describe("DomainObject task transactions", () => {
  it("claims by creation time and ID while skipping held tasks with different expiry order", async () => {
    const { domain, stub } = fixture("claim-order");
    const topic = "work/linux";
    const now = Date.now();
    const rows = [
      { id: newTaskId(1000), created: now - 4000, lease: now + DAY },
      { id: newTaskId(1001), created: now - 3000, lease: now - 1 },
      { id: newTaskId(1002), created: now - 3000, lease: now - 1000 },
      { id: newTaskId(1003), created: now - 2000, lease: now - 2000 },
    ];
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.transactionSync(() => {
        for (const row of rows) {
          state.storage.sql.exec(
            `INSERT INTO tasks (id, topic, created_at, lease_until, owned_count, creator,
             owner_identity, owner_worker, owned_at, data)
             VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)`,
            row.id,
            topic,
            row.created,
            row.lease,
            creator.identity,
            worker.identity,
            worker.worker,
            row.created,
            JSON.stringify({ original: row.id }),
          );
        }
      });
    });
    expect(success(await stub.topicsList({ domain }, creator)).topics).toEqual([
      {
        topic,
        available: 3,
        held: 1,
        oldest_available_at: now - 3000,
        declared: false,
      },
    ]);
    for (const expected of rows.slice(1)) {
      const task = success(
        await stub.own({ domain, topic, hold_seconds: 60 }, worker),
      ).task;
      expect(task.data).toEqual({ original: expected.id });
      expect(task.created_at).toBe(expected.created);
      expect(task.id).not.toBe(expected.id);
    }
    error(await stub.own({ domain, topic, hold_seconds: 60 }, worker), "empty");
    expect(success(await stub.topicsList({ domain }, creator)).topics).toEqual([
      {
        topic,
        available: 0,
        held: 4,
        oldest_available_at: null,
        declared: false,
      },
    ]);
  });

  it("checks a prune cutoff atomically after Renew keeps the task ID", async () => {
    const { domain, stub } = fixture("conditional-delete");
    const topic = "work/linux";
    await stub.create({ domain, topic, data: { work: true } }, creator);
    const held = success(
      await stub.own({ domain, topic, hold_seconds: 60 }, worker),
    ).task;
    const cutoff = Date.now() - DAY;
    const setLease = async (value: number) => {
      await runInDurableObject(stub, (_instance, state) => {
        state.storage.sql.exec(
          "UPDATE tasks SET lease_until = ? WHERE id = ?",
          value,
          held.id,
        );
      });
    };
    await setLease(cutoff - 1);
    const renewed = success(
      await stub.renew(
        { domain, topic, id: held.id, hold_seconds: 60 },
        worker,
      ),
    ).task;
    expect(renewed.id).toBe(held.id);
    error(
      await stub.delete(
        { domain, topic, id: held.id, lease_before: cutoff },
        operator,
      ),
      "forbidden",
    );
    expect(
      success(await stub.read({ domain, topic, id: held.id }, operator)).task
        .lease_until,
    ).toBe(renewed.lease_until);
    await setLease(cutoff);
    error(
      await stub.delete(
        { domain, topic, id: held.id, lease_before: cutoff },
        operator,
      ),
      "forbidden",
    );
    await setLease(cutoff - 1);
    success(
      await stub.delete(
        { domain, topic, id: held.id, lease_before: cutoff },
        operator,
      ),
    );
    error(
      await stub.read({ domain, topic, id: held.id }, operator),
      "not_found",
    );
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql
          .exec("SELECT SUM(deleted) AS count FROM stats_hourly")
          .one(),
      ).toEqual({ count: 1 });
    });
  });

  it("claims each available task exactly once under concurrent Owns", async () => {
    const { domain, stub } = fixture("concurrent-own");
    const originals = await Promise.all(
      Array.from({ length: 12 }, (_, index) =>
        stub.create(
          {
            domain,
            topic: "work/linux",
            data: { index, nested: { value: true } },
          },
          creator,
        ),
      ),
    );
    const originalIds = originals.map((result) => success(result).id);
    const results = await Promise.all(
      Array.from(
        { length: 36 },
        async (_, index): Promise<Result<{ ok: true; task: RpcTask }>> =>
          await stub.own(
            { domain, topic: "work/linux", hold_seconds: 60 },
            { ...worker, worker: `slot-${index}` },
          ),
      ),
    );
    const claimed = results
      .filter((result) => result.ok)
      .map((result) => success(result).task);
    expect(claimed).toHaveLength(12);
    expect(new Set(claimed.map((task) => task.id)).size).toBe(12);
    expect(new Set(claimed.map((task) => (task.data as Data).index)).size).toBe(
      12,
    );
    expect(
      results.filter((result) => !result.ok && result.error.code === "empty"),
    ).toHaveLength(24);
    for (const task of claimed) {
      expect(originalIds).not.toContain(task.id);
      expect(task).toMatchObject({
        domain,
        topic: "work/linux",
        creator: creator.identity,
        owned_count: 1,
        owner: { identity: worker.identity },
        data: { nested: { value: true } },
      });
      expect(task.lease_until - task.owned_at!).toBe(60_000);
    }
    const topics = success(await stub.topicsList({ domain }, creator)).topics;
    expect(topics).toEqual([
      {
        topic: "work/linux",
        available: 0,
        held: 12,
        oldest_available_at: null,
        declared: false,
      },
    ]);
    await runInDurableObject(stub, (_instance, state) => {
      const [stats] = state.storage.sql
        .exec(
          "SELECT SUM(created) AS created, SUM(owned) AS owned, SUM(wait_count) AS wait_count FROM stats_hourly",
        )
        .toArray();
      expect(stats).toEqual({
        created: 12,
        owned: 12,
        wait_count: 12,
      });
    });
  });

  it("preserves immutable fields across rotation and fences every old-ID operation", async () => {
    const { domain, stub } = fixture("fencing");
    const topic = "work/linux";
    const originalId = success(
      await stub.create(
        { domain, topic, data: { unicode: "λ", nested: [1, null] } },
        creator,
      ),
    ).id;
    const original = success(
      await stub.read({ domain, topic, id: originalId }, creator),
    ).task;
    expect(original).toMatchObject({
      owner: null,
      owned_at: null,
      owned_count: 0,
    });
    expect(original.lease_until).toBe(original.created_at - 5000);
    const first = success(
      await stub.own({ domain, topic, hold_seconds: 1 }, worker),
    ).task;
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET lease_until = 0 WHERE id = ?",
        first.id,
      );
    });
    // Expiry alone does not revoke a live fencing token.
    const renewed = success(
      await stub.renew(
        { domain, topic, id: first.id, hold_seconds: 30 },
        { ...worker, worker: "another-slot" },
      ),
    ).task;
    expect(renewed).toEqual({ ...first, lease_until: renewed.lease_until });
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET lease_until = 0 WHERE id = ?",
        first.id,
      );
    });
    const secondWorker = { ...worker, identity: "second-worker" };
    const second = success(
      await stub.own({ domain, topic, hold_seconds: 60 }, secondWorker),
    ).task;
    expect(second.id).not.toBe(first.id);
    expect(second).toMatchObject({
      data: original.data,
      creator: original.creator,
      created_at: original.created_at,
      owned_count: 2,
      owner: { identity: "second-worker" },
    });
    for (const id of [originalId, first.id]) {
      error(await stub.read({ domain, topic, id }, worker), "not_found");
      error(
        await stub.renew({ domain, topic, id, hold_seconds: 1 }, worker),
        "not_found",
      );
      error(
        await stub.consume({ domain, topic, id, tasks: [] }, worker),
        "not_found",
      );
      error(await stub.delete({ domain, topic, id }, operator), "not_found");
    }
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET lease_until = 0 WHERE id = ?",
        second.id,
      );
    });
    success(
      await stub.consume(
        { domain, topic, id: second.id, tasks: [] },
        secondWorker,
      ),
    );
  });

  it("enforces identity holder rules, operator override, and exact topic equality", async () => {
    const { domain, stub } = fixture("holder-rules");
    const topic = "work/linux";
    const id = success(
      await stub.create({ domain, topic, data: {} }, creator),
    ).id;
    error(await stub.delete({ domain, topic, id }, worker), "forbidden");
    error(
      await stub.renew({ domain, topic, id, hold_seconds: 1 }, creator),
      "forbidden",
    );
    error(
      await stub.consume({ domain, topic, id, tasks: [] }, creator),
      "forbidden",
    );
    for (const caller of [creator, operator]) {
      const wrong = { domain, topic: "work/windows", id };
      error(await stub.read(wrong, caller), "not_found");
      error(
        await stub.renew({ ...wrong, hold_seconds: 1 }, caller),
        "not_found",
      );
      error(await stub.delete(wrong, caller), "not_found");
      error(await stub.consume({ ...wrong, tasks: [] }, caller), "not_found");
    }
    success(await stub.delete({ domain, topic, id }, creator));
    success(await stub.create({ domain, topic, data: {} }, creator));
    const held = success(
      await stub.own({ domain, topic, hold_seconds: 60 }, worker),
    ).task;
    error(
      await stub.renew(
        { domain, topic, id: held.id, hold_seconds: 1 },
        creator,
      ),
      "forbidden",
    );
    error(
      await stub.delete({ domain, topic, id: held.id }, creator),
      "forbidden",
    );
    error(
      await stub.consume({ domain, topic, id: held.id, tasks: [] }, creator),
      "forbidden",
    );
    success(
      await stub.renew(
        { domain, topic, id: held.id, hold_seconds: 1 },
        operator,
      ),
    );
    success(
      await stub.delete(
        { domain, topic, id: held.id },
        { ...worker, worker: "another-slot" },
      ),
    );
    const untouched = success(
      await stub.create({ domain, topic, data: {} }, creator),
    ).id;
    success(
      await stub.consume({ domain, topic, id: untouched, tasks: [] }, operator),
    );
  });

  it("validates every Consume target before writing and rolls back SQL failures", async () => {
    const { domain, stub } = fixture("consume-atomic");
    const topic = "work/linux";
    success(
      await stub.create({ domain, topic, data: { source: true } }, creator),
    );
    const held = success(
      await stub.own({ domain, topic, hold_seconds: 60 }, worker),
    ).task;
    const request = { domain, topic, id: held.id };
    error(
      await stub.consume(
        {
          ...request,
          tasks: [
            { topic: "result/allowed", data: {} },
            { topic: "private/denied", data: {} },
          ],
        },
        worker,
      ),
      "forbidden",
    );
    expect(success(await stub.read(request, creator)).task).toEqual(held);
    expect(
      success(
        await stub.tasksList({ domain, topic: "result/allowed" }, creator),
      ).tasks,
    ).toEqual([]);
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        `CREATE TRIGGER fail_target BEFORE INSERT ON tasks WHEN NEW.topic = 'result/fail' BEGIN SELECT RAISE(ABORT, 'test insertion failure'); END`,
      );
    });
    await runInDurableObject(stub, async (instance: DomainObject) => {
      await expect(
        instance.consume(
          {
            ...request,
            tasks: [
              { topic: "result/allowed", data: {} },
              { topic: "result/fail", data: {} },
            ],
          },
          worker,
        ),
      ).rejects.toThrow("test insertion failure");
    });
    expect(success(await stub.read(request, creator)).task).toEqual(held);
    expect(
      success(
        await stub.tasksList({ domain, topic: "result/allowed" }, creator),
      ).tasks,
    ).toEqual([]);
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec("DROP TRIGGER fail_target");
      expect(
        state.storage.sql
          .exec(
            "SELECT SUM(created) AS created, SUM(consumed) AS consumed FROM stats_hourly",
          )
          .one(),
      ).toEqual({ created: 1, consumed: 0 });
    });
    const targets = [
      { topic: "result/first", data: { value: 1 } },
      { topic: "result/second", data: { value: 2 } },
    ] satisfies Parameters<typeof stub.consume>[0]["tasks"];
    const consumed = success(
      await stub.consume({ ...request, tasks: targets }, worker),
    );
    expect(consumed.ids).toHaveLength(2);
    for (const [index, target] of targets.entries()) {
      const task = success(
        await stub.read(
          { domain, topic: target.topic, id: consumed.ids[index]! },
          creator,
        ),
      ).task;
      expect(task).toMatchObject({
        topic: target.topic,
        data: target.data,
        creator: worker.identity,
        owned_count: 0,
        owner: null,
        owned_at: null,
      });
      expect(task.lease_until).toBe(task.created_at - 5000);
    }
    error(
      await stub.consume({ ...request, tasks: targets }, worker),
      "not_found",
    );
  });

  it("allows exactly one winner when Consume races Own after expiry", async () => {
    const { domain, stub } = fixture("consume-own-race");
    const topic = "work/linux";
    success(await stub.create({ domain, topic, data: {} }, creator));
    const held = success(
      await stub.own({ domain, topic, hold_seconds: 1 }, worker),
    ).task;
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET lease_until = 0 WHERE id = ?",
        held.id,
      );
    });
    const results = await Promise.all([
      stub.consume(
        {
          domain,
          topic,
          id: held.id,
          tasks: [{ topic: "result/done", data: {} }],
        },
        worker,
      ),
      stub.own(
        { domain, topic, hold_seconds: 60 },
        { ...worker, identity: "competitor" },
      ),
    ]);
    expect(results.filter((result) => result.ok)).toHaveLength(1);
    error(
      await stub.read({ domain, topic, id: held.id }, creator),
      "not_found",
    );
    const remaining = success(await stub.tasksList({ domain, topic }, creator))
      .tasks.length;
    const delivered = success(
      await stub.tasksList({ domain, topic: "result/done" }, creator),
    ).tasks.length;
    expect(remaining + delivered).toBe(1);
  });

  it("pages summaries without data while tasks rotate and disappear", async () => {
    const { domain, stub } = fixture("paging");
    const topic = "work/linux";
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.transactionSync(() => {
        for (let index = 0; index < 502; index++) {
          state.storage.sql.exec(
            `INSERT INTO tasks (id, topic, created_at, lease_until, owned_count, creator, owner_identity, owner_worker, owned_at, data) VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?)`,
            newTaskId(1000 + index),
            topic,
            1000 + index,
            index < 500 ? Date.now() + DAY : 0,
            creator.identity,
            '{"large":"not returned"}',
          );
        }
      });
    });
    const first = success(await stub.tasksList({ domain, topic }, creator));
    expect(first.tasks).toHaveLength(500);
    expect(first.cursor).toBeTypeOf("string");
    expect(first.tasks[0]).toMatchObject({ state: "held", created_at: 1000 });
    expect(first.tasks.every((task) => !("data" in task))).toBe(true);
    const rotated = success(
      await stub.own({ domain, topic, hold_seconds: 60 }, worker),
    ).task;
    expect(rotated.created_at).toBe(1500);
    const second = success(
      await stub.tasksList({ domain, topic, cursor: first.cursor! }, creator),
    );
    expect(second.tasks.map((task) => task.created_at)).toEqual([1500, 1501]);
    expect(second.tasks[0]!.id).toBe(rotated.id);
    expect(second.cursor).toBeUndefined();
    success(
      await stub.consume({ domain, topic, id: rotated.id, tasks: [] }, worker),
    );
    const afterConsume = success(
      await stub.tasksList({ domain, topic, cursor: first.cursor! }, creator),
    );
    expect(afterConsume.tasks.map((task) => task.created_at)).toEqual([1501]);
    error(
      await stub.tasksList({ domain, topic, cursor: "not-a-cursor" }, creator),
      "invalid",
    );
    const exact = success(
      await stub.tasksList({ domain, topic: "empty" }, creator),
    );
    expect(exact).toEqual({ ok: true, tasks: [] });
  });

  it("lists only readable live or declared topics and retains empty-poll history", async () => {
    const { domain, stub } = fixture("topic-pollers");
    const topic = "work/linux";
    error(await stub.own({ domain, topic, hold_seconds: 60 }, worker), "empty");
    const emptyPoll = success(
      await stub.topicPollers({ domain, topic }, creator),
    ).pollers;
    expect(emptyPoll).toHaveLength(1);
    expect(emptyPoll[0]).toMatchObject({
      identity: worker.identity,
      worker: worker.worker,
      last_owned_at: null,
    });
    success(await stub.create({ domain, topic, data: {} }, creator));
    success(await stub.own({ domain, topic, hold_seconds: 60 }, worker));
    const ownedPoll = success(
      await stub.topicPollers({ domain, topic }, creator),
    ).pollers[0]!;
    expect(ownedPoll.last_owned_at).toBeTypeOf("number");
    error(await stub.own({ domain, topic, hold_seconds: 60 }, worker), "empty");
    error(
      await stub.own(
        { domain, topic, hold_seconds: 60 },
        { ...worker, worker: "slot-2" },
      ),
      "empty",
    );
    const pollers = success(
      await stub.topicPollers({ domain, topic }, creator),
    ).pollers;
    expect(pollers).toHaveLength(2);
    expect(
      pollers.find((row) => row.worker === worker.worker)!.last_owned_at,
    ).toBe(ownedPoll.last_owned_at);
    error(
      await stub.topicDeclare(
        { domain, topic: "declared/empty", declared: true },
        creator,
      ),
      "forbidden",
    );
    success(
      await stub.topicDeclare(
        { domain, topic: "declared/empty", declared: true },
        operator,
      ),
    );
    success(
      await stub.create({ domain, topic: "secret/topic", data: {} }, creator),
    );
    const taskOnlyTopic = "work/task-only";
    const taskOnlyId = success(
      await stub.create({ domain, topic: taskOnlyTopic, data: {} }, creator),
    ).id;
    const taskOnly = success(
      await stub.read(
        { domain, topic: taskOnlyTopic, id: taskOnlyId },
        creator,
      ),
    ).task;
    error(
      await stub.own({ domain, topic: "poller/only", hold_seconds: 1 }, worker),
      "empty",
    );
    const reader = {
      ...worker,
      scopes: {
        ...worker.scopes,
        read: ["work/**", "declared/**", "poller/**"],
      },
    };
    const topics = success(await stub.topicsList({ domain }, reader)).topics;
    expect(topics.map((row) => row.topic)).toEqual([
      "declared/empty",
      "poller/only",
      "work/linux",
      taskOnlyTopic,
    ]);
    expect(topics[0]).toEqual({
      topic: "declared/empty",
      available: 0,
      held: 0,
      oldest_available_at: null,
      declared: true,
    });
    expect(topics[3]).toEqual({
      topic: taskOnlyTopic,
      available: 1,
      held: 0,
      oldest_available_at: taskOnly.created_at,
      declared: false,
    });
    expect(
      success(
        await stub.topicsList({ domain, glob: "work/*" }, reader),
      ).topics.map((row) => row.topic),
    ).toEqual([topic, taskOnlyTopic]);
    expect(
      success(
        await stub.topicsList(
          { domain },
          { ...reader, scopes: { ...reader.scopes, read: [] } },
        ),
      ).topics,
    ).toEqual([]);
    expect(
      success(await stub.topicsList({ domain }, operator)).topics,
    ).toHaveLength(5);
    success(
      await stub.topicDeclare(
        { domain, topic: "declared/empty", declared: false },
        operator,
      ),
    );
    expect(
      success(await stub.topicsList({ domain, glob: "declared/**" }, operator))
        .topics,
    ).toEqual([]);
    success(
      await stub.delete(
        { domain, topic: taskOnlyTopic, id: taskOnlyId },
        creator,
      ),
    );
    expect(
      success(
        await stub.topicsList({ domain, glob: "work/**" }, reader),
      ).topics.map((row) => row.topic),
    ).toEqual([topic]);
  });

  it("records exact hourly counters and first-wait/current-run durations", async () => {
    const { domain, stub } = fixture("statistics");
    await runInDurableObject(stub, async (instance: DomainObject, state) => {
      const base = Math.ceil(Date.now() / HOUR) * HOUR + 10 * DAY;
      const clock = vi.spyOn(Date, "now").mockReturnValue(base);
      try {
        const topic = "work/linux";
        const original = success(
          await instance.create({ domain, topic, data: {} }, creator),
        ).id;
        clock.mockReturnValue(base + 2000);
        const first = success(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
        ).task;
        clock.mockReturnValue(base + 4000);
        const second = success(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
        ).task;
        clock.mockReturnValue(base + 5000);
        success(
          await instance.consume(
            {
              domain,
              topic,
              id: second.id,
              tasks: [
                { topic: "result/done", data: {} },
                { topic: "result/done", data: {} },
              ],
            },
            worker,
          ),
        );
        error(
          await instance.consume(
            { domain, topic, id: second.id, tasks: [] },
            worker,
          ),
          "not_found",
        );
        error(
          await instance.renew(
            { domain, topic, id: first.id, hold_seconds: 1 },
            worker,
          ),
          "not_found",
        );
        error(
          await instance.delete({ domain, topic, id: original }, operator),
          "not_found",
        );
        clock.mockReturnValue(base + 6000);
        success(await instance.create({ domain, topic, data: {} }, creator));
        clock.mockReturnValue(base + 9000);
        const third = success(
          await instance.own({ domain, topic, hold_seconds: 60 }, worker),
        ).task;
        clock.mockReturnValue(base + 11_000);
        success(
          await instance.consume(
            { domain, topic, id: third.id, tasks: [] },
            worker,
          ),
        );
        error(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
          "empty",
        );
        const deleted = success(
          await instance.create({ domain, topic, data: {} }, creator),
        ).id;
        success(await instance.delete({ domain, topic, id: deleted }, creator));
        const rows = state.storage.sql
          .exec("SELECT * FROM stats_hourly ORDER BY topic")
          .toArray();
        expect(rows).toEqual([
          {
            topic: "result/done",
            hour: base,
            created: 2,
            owned: 0,
            reowned: 0,
            consumed: 0,
            deleted: 0,
            wait_ms_sum: 0,
            wait_ms_max: 0,
            wait_count: 0,
            run_ms_sum: 0,
            run_ms_max: 0,
            run_count: 0,
          },
          {
            topic,
            hour: base,
            created: 3,
            owned: 3,
            reowned: 1,
            consumed: 2,
            deleted: 1,
            wait_ms_sum: 5000,
            wait_ms_max: 3000,
            wait_count: 2,
            run_ms_sum: 3000,
            run_ms_max: 2000,
            run_count: 2,
          },
        ] satisfies HourlyStats[]);
        clock.mockReturnValue(base + HOUR);
        error(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
          "empty",
        );
        // Empty polls are not counted, so a new hour gets no row of its own.
        expect(
          state.storage.sql
            .exec(
              "SELECT hour FROM stats_hourly WHERE topic = ? ORDER BY hour",
              topic,
            )
            .toArray(),
        ).toEqual([{ hour: base }]);
      } finally {
        clock.mockRestore();
      }
    });
  });

  it("refreshes an idle poller's row at most once a minute", async () => {
    const { domain, stub } = fixture("poller-refresh");
    const topic = "work/linux";
    await runInDurableObject(stub, async (instance: DomainObject, state) => {
      const base = 1_800_000_000_000;
      const clock = vi.spyOn(Date, "now").mockReturnValue(base);
      const poller = () =>
        state.storage.sql
          .exec(
            "SELECT last_polled_at, last_owned_at FROM pollers WHERE topic = ?",
            topic,
          )
          .toArray();
      const poll = () =>
        instance.own({ domain, topic, hold_seconds: 60 }, worker);
      try {
        error(await poll(), "empty");
        expect(poller()).toEqual([
          { last_polled_at: base, last_owned_at: null },
        ]);
        clock.mockReturnValue(base + 59_999);
        error(await poll(), "empty");
        expect(poller()).toEqual([
          { last_polled_at: base, last_owned_at: null },
        ]);
        clock.mockReturnValue(base + 60_000);
        error(await poll(), "empty");
        expect(poller()).toEqual([
          { last_polled_at: base + 60_000, last_owned_at: null },
        ]);
        // Taking a task always records the poll and the Own.
        clock.mockReturnValue(base + 60_001);
        success(await instance.create({ domain, topic, data: {} }, creator));
        success(await poll());
        expect(poller()).toEqual([
          { last_polled_at: base + 60_001, last_owned_at: base + 60_001 },
        ]);
        expect(
          state.storage.sql
            .exec("SELECT COUNT(*) AS rows FROM stats_hourly")
            .one(),
        ).toEqual({ rows: 1 });
      } finally {
        clock.mockRestore();
      }
    });
  });

  it("checks for the cleanup alarm only on an object's first operation", async () => {
    const { domain, stub } = fixture("alarm-once");
    const topic = "work/linux";
    await runInDurableObject(stub, async (instance: DomainObject, state) => {
      const getAlarm = vi.spyOn(state.storage, "getAlarm");
      try {
        error(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
          "empty",
        );
        error(
          await instance.own({ domain, topic, hold_seconds: 1 }, worker),
          "empty",
        );
        success(await instance.create({ domain, topic, data: {} }, creator));
        expect(getAlarm).toHaveBeenCalledTimes(1);
        expect(await state.storage.getAlarm()).not.toBeNull();
      } finally {
        getAlarm.mockRestore();
      }
    });
    await evictDurableObject(stub);
    await runInDurableObject(stub, async (instance: DomainObject, state) => {
      const getAlarm = vi.spyOn(state.storage, "getAlarm");
      try {
        error(
          await instance.own(
            { domain, topic: "work/other", hold_seconds: 1 },
            worker,
          ),
          "empty",
        );
        expect(getAlarm).toHaveBeenCalledTimes(1);
      } finally {
        getAlarm.mockRestore();
      }
    });
  });

  it("expires only stale pollers/statistics, preserves tasks, and reschedules daily", async () => {
    const { domain, stub } = fixture("alarm");
    await runInDurableObject(stub, async (_instance, state) => {
      expect(await state.storage.getAlarm()).toBeNull();
    });
    const task = success(
      await stub.create({ domain, topic: "work/live", data: {} }, creator),
    ).id;
    error(
      await stub.own(
        { domain, topic: "poller/stale", hold_seconds: 1 },
        worker,
      ),
      "empty",
    );
    error(
      await stub.own(
        { domain, topic: "poller/recent", hold_seconds: 1 },
        worker,
      ),
      "empty",
    );
    success(
      await stub.topicDeclare(
        { domain, topic: "declared/empty", declared: true },
        operator,
      ),
    );
    await runInDurableObject(stub, async (_instance, state) => {
      expect(await state.storage.getAlarm()).toBeGreaterThan(Date.now());
      state.storage.sql.exec(
        "UPDATE pollers SET last_polled_at = ? WHERE topic = ?",
        Date.now() - 8 * DAY,
        "poller/stale",
      );
      state.storage.sql.exec(
        "UPDATE stats_hourly SET hour = ? WHERE topic = ?",
        Date.now() - 31 * DAY,
        "poller/stale",
      );
    });
    expect(
      success(
        await stub.topicPollers({ domain, topic: "poller/stale" }, creator),
      ).pollers,
    ).toEqual([]);
    expect(
      success(
        await stub.topicsList({ domain, glob: "poller/**" }, operator),
      ).topics.map((row) => row.topic),
    ).toEqual(["poller/recent"]);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    await runInDurableObject(stub, async (_instance, state) => {
      expect(
        state.storage.sql
          .exec("SELECT topic FROM pollers ORDER BY topic")
          .toArray(),
      ).toEqual([{ topic: "poller/recent" }]);
      expect(
        state.storage.sql
          .exec("SELECT topic FROM stats_hourly WHERE topic = 'poller/stale'")
          .toArray(),
      ).toEqual([]);
      const next = await state.storage.getAlarm();
      expect(next).toBeGreaterThanOrEqual(Date.now() + DAY - 1000);
      expect(next).toBeLessThanOrEqual(Date.now() + DAY);
    });
    expect(
      success(
        await stub.read({ domain, topic: "work/live", id: task }, creator),
      ).task.id,
    ).toBe(task);
    expect(
      success(await stub.topicsList({ domain, glob: "declared/**" }, operator))
        .topics[0]!.declared,
    ).toBe(true);
  });

  it("upgrades the phase-1 schema and reconstructs persistent state after eviction", async () => {
    const { domain, stub } = fixture("migration");
    await runInDurableObject(stub, (_instance, state) => {
      state.storage.sql.exec(
        `DROP TABLE tasks; DROP TABLE pollers; DROP TABLE declared_topics; DROP TABLE stats_hourly; DROP TABLE monitor_snapshot_chunks; DROP TABLE monitor_snapshots; UPDATE schema_version SET version = 1; CREATE TABLE phase_one_marker (value TEXT); INSERT INTO phase_one_marker VALUES ('retained')`,
      );
    });
    await evictDurableObject(stub);
    const created = success(
      await stub.create(
        { domain, topic: "work/linux", data: { persist: true } },
        creator,
      ),
    );
    await runInDurableObject(stub, (_instance, state) => {
      expect(
        state.storage.sql.exec("SELECT version FROM schema_version").one(),
      ).toEqual({ version: 3 });
      expect(
        state.storage.sql.exec("SELECT value FROM phase_one_marker").one(),
      ).toEqual({ value: "retained" });
    });
    const before = success(
      await stub.read({ domain, topic: "work/linux", id: created.id }, creator),
    ).task;
    await evictDurableObject(stub);
    expect(
      success(
        await stub.read(
          { domain, topic: "work/linux", id: created.id },
          creator,
        ),
      ).task,
    ).toEqual(before);
    expect(
      success(
        await env.DOMAINS.getByName(`${domain}/separate`).tasksList(
          { domain: `${domain}/separate`, topic: "work/linux" },
          creator,
        ),
      ).tasks,
    ).toEqual([]);
  });
});

function internals(instance: DomainObject) {
  return instance as unknown as {
    waiting: Map<string, unknown>;
    maxWaitersPerTopic: number;
  };
}

describe("long-poll Own", () => {
  const poll = (domain: string, topic: string) => ({
    domain,
    topic,
    hold_seconds: 60,
    wait_seconds: 30,
  });

  it("returns a waiting task as soon as Create commits", async () => {
    const { domain, stub } = fixture("wait-create");
    const topic = "work/waited";
    const waiting = ownWithWait(stub, poll(domain, topic), worker);
    // Give the waiter a turn to register and park before creating the task.
    await new Promise((resolve) => setTimeout(resolve, 50));
    const created = success(
      await stub.create({ domain, topic, data: { request: 1 } }, creator),
    ).id;
    const claimed = success(await waiting).task;
    expect(claimed.id).not.toBe(created);
    expect(claimed.data).toEqual({ request: 1 });
    expect(claimed.owner).toEqual({ identity: "worker", worker: "worker-1" });
  });

  it("returns empty at the deadline without claiming anything", async () => {
    const { domain, stub } = fixture("wait-timeout");
    const started = Date.now();
    // A completed wait carries no refusal hint.
    expect(
      await ownWithWait(
        stub,
        { ...poll(domain, "work/idle"), wait_seconds: 1 },
        worker,
      ),
    ).toEqual({ ok: false, error: { code: "empty", message: "empty" } });
    expect(Date.now() - started).toBeLessThan(5_000);
  });

  it("keeps omission and zero immediate", async () => {
    const { domain, stub } = fixture("wait-immediate");
    const topic = "work/none";
    const started = Date.now();
    error(await stub.own({ domain, topic, hold_seconds: 60 }, worker), "empty");
    error(
      await stub.own(
        { domain, topic, hold_seconds: 60, wait_seconds: 0 },
        worker,
      ),
      "empty",
    );
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("wakes waiters for a Consume successor", async () => {
    const { domain, stub } = fixture("wait-consume");
    const source = success(
      await stub.create({ domain, topic: "work/source", data: {} }, creator),
    ).id;
    const held = success(
      await stub.own(
        { domain, topic: "work/source", hold_seconds: 60 },
        worker,
      ),
    ).task;
    expect(held.id).not.toBe(source);
    const waiting = ownWithWait(stub, poll(domain, "result/next"), worker);
    await new Promise((resolve) => setTimeout(resolve, 50));
    success(
      await stub.consume(
        {
          domain,
          topic: "work/source",
          id: held.id,
          tasks: [{ topic: "result/next", data: { step: 2 } }],
        },
        worker,
      ),
    );
    expect(success(await waiting).task.data).toEqual({ step: 2 });
  });

  it("wakes at the next lease expiry", async () => {
    const { domain, stub } = fixture("wait-lease");
    const topic = "work/lease";
    const task = success(
      await stub.create({ domain, topic, data: {} }, creator),
    ).id;
    success(await stub.own({ domain, topic, hold_seconds: 1 }, worker));
    const other: Caller = { ...worker, identity: "worker-two", worker: "w2" };
    const claimed = success(
      await ownWithWait(
        stub,
        { ...poll(domain, topic), wait_seconds: 5 },
        other,
      ),
    ).task;
    expect(claimed.owner).toEqual({ identity: "worker-two", worker: "w2" });
    // The original ID is fenced by the new owner's claim.
    error(
      await stub.renew({ domain, topic, id: task, hold_seconds: 60 }, worker),
      "not_found",
    );
  });

  it("does not wake waiters on an unrelated topic", async () => {
    const { domain, stub } = fixture("wait-isolation");
    const waiting = ownWithWait(
      stub,
      { ...poll(domain, "work/quiet"), wait_seconds: 1 },
      worker,
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    success(
      await stub.create({ domain, topic: "work/noisy", data: {} }, creator),
    );
    error(await waiting, "empty");
    // The unrelated task is still claimable.
    expect(
      success(
        await stub.own(
          { domain, topic: "work/noisy", hold_seconds: 60 },
          worker,
        ),
      ).task.topic,
    ).toBe("work/noisy");
  });

  it("claims each task once for competing waiters", async () => {
    const { domain, stub } = fixture("wait-compete");
    const topic = "work/compete";
    const waiting = [0, 1, 2].map((index) =>
      ownWithWait(
        stub,
        { ...poll(domain, topic), wait_seconds: 2 },
        { ...worker, worker: `worker-${index}` },
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    success(await stub.create({ domain, topic, data: { n: 1 } }, creator));
    const results = await Promise.all(waiting);
    const tasks = results.filter((result) => result.ok);
    expect(tasks).toHaveLength(1);
    expect(results.filter((result) => !result.ok)).toHaveLength(2);
  });

  it("re-registers no waiter after a completed wait", async () => {
    const { domain, stub } = fixture("wait-cleanup");
    error(
      await ownWithWait(
        stub,
        { ...poll(domain, "work/clean"), wait_seconds: 1 },
        worker,
      ),
      "empty",
    );
    await runInDurableObject(stub, (instance: DomainObject) => {
      expect(internals(instance).waiting.size).toBe(0);
    });
  });

  it("never claims from a parked call; a hint answers retry", async () => {
    const { domain, stub } = fixture("wait-no-ghost");
    const topic = "work/ghost";
    // A Worker whose client disconnected never makes the follow-up attempt.
    const parked = stub.ownOrPark(poll(domain, topic), worker, 5_000);
    await new Promise((resolve) => setTimeout(resolve, 50));
    success(await stub.create({ domain, topic, data: { n: 1 } }, creator));
    expect(await parked).toEqual({ retry: true });
    const [summary] = success(
      await stub.topicsList({ domain }, creator),
    ).topics;
    expect(summary).toMatchObject({ topic, available: 1, held: 0 });
  });

  it("attempts the claim and records the poll when the topic is full", async () => {
    const { domain, stub } = fixture("wait-full");
    const topic = "work/full";
    await runInDurableObject(stub, (instance: DomainObject) => {
      internals(instance).maxWaitersPerTopic = 0;
    });
    const started = Date.now();
    expect(await ownWithWait(stub, poll(domain, topic), worker)).toEqual({
      ok: false,
      error: { code: "empty", message: "empty", wait_refused: true },
    });
    expect(Date.now() - started).toBeLessThan(1_000);
    // Only a refused wait carries the hint; an immediate poll does not.
    expect(await stub.own({ domain, topic, hold_seconds: 60 }, worker)).toEqual(
      { ok: false, error: { code: "empty", message: "empty" } },
    );
    expect(
      success(await stub.topicPollers({ domain, topic }, creator)).pollers,
    ).toHaveLength(1);
    success(await stub.create({ domain, topic, data: { n: 1 } }, creator));
    expect(
      success(await ownWithWait(stub, poll(domain, topic), worker)).task.data,
    ).toEqual({ n: 1 });
  });

  it("wakes one waiter per task at a lease expiry", async () => {
    const { domain, stub } = fixture("wait-herd");
    const topic = "work/herd";
    success(await stub.create({ domain, topic, data: {} }, creator));
    success(await stub.own({ domain, topic, hold_seconds: 1 }, worker));
    const results = await Promise.all(
      [0, 1, 2].map((index) =>
        stub.ownOrPark(
          poll(domain, topic),
          { ...worker, worker: `herd-${index}` },
          2_500,
        ),
      ),
    );
    expect(results.filter((result) => "retry" in result)).toHaveLength(1);
    expect(
      results.filter((result) => !("retry" in result) && !result.ok),
    ).toHaveLength(2);
  });
});
