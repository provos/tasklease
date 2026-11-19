import { describe, expect, it } from "vitest";
import fixture from "../../schema/examples/monitor.snapshot/valid/response.json";
import {
  HOUR,
  decodeSnapshot,
  hourBuckets,
  statsTopics,
  topicLatencies,
  topics,
  type Snapshot,
} from "./model";

function snapshot(): Snapshot {
  return decodeSnapshot({
    generated_at: fixture.body.generated_at,
    domains: [structuredClone(fixture.body)],
    local: { requests: [], replies: [] },
  });
}

describe("snapshot dashboard model", () => {
  it.each(
    ["task", "request", "reply"].flatMap((location) =>
      [undefined, null, "invalid", 42].map((origin) => ({ location, origin })),
    ),
  )("rejects invalid origin $origin in $location", ({ location, origin }) => {
    const value = snapshot();
    const task: Record<string, unknown> = {
      ...value.domains[0].topics[0].tasks[0],
      duplicate: false,
    };
    if (origin === undefined) delete task.origin_id;
    else task.origin_id = origin;
    const domain = value.domains[0];
    const invalid =
      location === "task"
        ? {
            ...value,
            domains: [
              { ...domain, topics: [{ ...domain.topics[0], tasks: [task] }] },
            ],
          }
        : {
            ...value,
            local: {
              requests: location === "request" ? [task] : [],
              replies: location === "reply" ? [task] : [],
            },
          };
    expect(() => decodeSnapshot(invalid)).toThrow("invalid snapshot");
  });

  it("carries schema-fixture topic metadata into its domain", () => {
    const queues = topics(snapshot());
    expect(queues).toHaveLength(1);
    expect(queues[0]).toMatchObject({
      domain: "acme/pipeline",
      topic: "work/linux",
      available: 0,
      held: 1,
    });
  });

  it("includes exactly N buckets ending in the current partial hour", () => {
    const current = fixture.body.stats[0].hour;
    const rows = [
      { ...fixture.body.stats[0], hour: current - 3 * HOUR, created: 99 },
      { ...fixture.body.stats[0], hour: current - 2 * HOUR, created: 4 },
      { ...fixture.body.stats[0], created: 7 },
      { ...fixture.body.stats[0], hour: current + HOUR, created: 99 },
    ];
    const result = hourBuckets(rows, fixture.body.generated_at, 3);

    expect(result.map((row) => row.hour)).toEqual([
      current - 2 * HOUR,
      current - HOUR,
      current,
    ]);
    expect(result.map((row) => row.created)).toEqual([4, 0, 7]);
    expect(result[1]).toMatchObject({
      created: 0,
      consumed: 0,
      wait_count: 0,
      run_count: 0,
    });
  });

  it("retains the exact 720-hour boundary without including a 721st bucket", () => {
    const current = fixture.body.stats[0].hour;
    const result = hourBuckets(
      [{ ...fixture.body.stats[0], hour: current - 719 * HOUR, created: 1 }],
      fixture.body.generated_at,
      720,
    );

    expect(result).toHaveLength(720);
    expect(result[0]).toMatchObject({ hour: current - 719 * HOUR, created: 1 });
    expect(result[719].hour).toBe(current);
  });

  it("uses a domain's capture time when aggregate collection crosses an hour", () => {
    const value = snapshot();
    const current = fixture.body.stats[0].hour;
    value.domains[0].generated_at = current + HOUR - 1;
    value.generated_at = current + HOUR + 1;
    const [selected] = statsTopics(value);
    const result = hourBuckets(selected.rows, selected.generated_at, 1);

    expect(selected.generated_at).toBe(current + HOUR - 1);
    expect(result[0].hour).toBe(current);
  });

  it("keeps historical-only topics and current topics without statistics", () => {
    const value = snapshot();
    value.domains[0].stats[0].topic = "finished/historical";
    const result = statsTopics(value);

    expect(result.map((group) => group.topic)).toEqual([
      "work/linux",
      "finished/historical",
    ]);
    expect(result[0].rows).toEqual([]);
    expect(result[1].rows).toEqual(value.domains[0].stats);
    expect(result[0].key).not.toBe(result[1].key);
  });

  it("does not confuse identical topic names in separate domains", () => {
    const value = snapshot();
    value.domains.push({
      ...structuredClone(value.domains[0]),
      domain: "other",
    });
    const result = statsTopics(value);

    expect(result).toHaveLength(2);
    expect(new Set(result.map((group) => group.key)).size).toBe(2);
    expect(result.map((group) => group.domain)).toEqual([
      "acme/pipeline",
      "other",
    ]);
  });

  it("weights paired completion samples and keeps unmeasured topics with live backlog", () => {
    const value = snapshot();
    const domain = value.domains[0];
    const live = domain.topics[0];
    live.available = 125;
    live.truncated = true;
    live.oldest_available_at = domain.generated_at - 50_000;
    const base = domain.stats[0];
    domain.stats = [
      { ...base, consumed: 6 },
      {
        ...base,
        topic: "history/slow",
        consumed: 1,
        completion_latency: {
          wait_ms_sum: 400,
          after_claim_ms_sum: 200,
          count: 1,
        },
      },
      {
        ...base,
        topic: "history/slow",
        consumed: 4,
        completion_latency: {
          wait_ms_sum: 600,
          after_claim_ms_sum: 400,
          count: 4,
        },
      },
      {
        ...base,
        topic: "history/instant",
        consumed: 1,
        completion_latency: { wait_ms_sum: 0, after_claim_ms_sum: 0, count: 1 },
      },
    ];
    expect(topicLatencies(statsTopics(value))).toMatchObject([
      {
        topic: "history/slow",
        waiting: 0,
        oldestWaitingMs: null,
        consumed: 5,
        completion: { count: 5, waitMs: 200, afterClaimMs: 120 },
      },
      {
        topic: "history/instant",
        consumed: 1,
        completion: { count: 1, waitMs: 0, afterClaimMs: 0 },
      },
      {
        topic: "work/linux",
        waiting: 125,
        oldestWaitingMs: 50_000,
        consumed: 6,
        completion: null,
      },
    ]);
  });

  it("accepts an empty monitor envelope and reports a malformed envelope", () => {
    expect(
      decodeSnapshot({
        generated_at: 1,
        domains: [],
        local: { requests: [], replies: [] },
      }),
    ).toMatchObject({ domains: [], local: { requests: [], replies: [] } });
    expect(() =>
      decodeSnapshot({ generated_at: 1, domains: [], local: {} }),
    ).toThrow("invalid snapshot");
    expect(() => decodeSnapshot({ ...snapshot(), errors: [42] })).toThrow(
      "invalid snapshot",
    );
    expect(topics(null)).toEqual([]);
    expect(statsTopics(null)).toEqual([]);
  });
});
