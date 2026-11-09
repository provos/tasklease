/**
 * Deterministic mock snapshots for developing and testing the dashboard.
 *
 * Each scenario mirrors what `tl monitor` returns from `/api/snapshot`: one
 * service snapshot per domain plus this machine's local requester records.
 * Values are seeded, so a scenario looks the same on every load; timestamps
 * are relative to the request time, so ages read naturally.
 */
import type {
  HourlyStats,
  MonitorSnapshotResponse,
  Poller,
  SnapshotTopic,
  TaskSummary,
} from "../../../service/src/generated/protocol.ts";
import {
  HOUR,
  type LocalReply,
  type LocalRequest,
  type Snapshot,
} from "../model.ts";

/** Scenarios with a snapshot body. `unreachable` is an HTTP failure instead. */
export const SNAPSHOT_SCENARIOS = [
  "busy",
  "building",
  "empty",
  "degraded",
  "large",
] as const;
export type SnapshotScenario = (typeof SNAPSHOT_SCENARIOS)[number];
export const SCENARIOS = [...SNAPSHOT_SCENARIOS, "unreachable"] as const;
export type Scenario = (typeof SCENARIOS)[number];

export function isScenario(value: string): value is Scenario {
  return (SCENARIOS as readonly string[]).includes(value);
}

const MINUTE = 60_000;
const DOMAIN = "github.com/provos/fluxshoteditor";
const ULID = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/** Mulberry32: small, fast and seedable. */
function seeded(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296;
  };
}

class Builder {
  private readonly random: () => number;

  constructor(
    readonly now: number,
    seed: number,
  ) {
    this.random = seeded(seed);
  }

  integer(low: number, high: number): number {
    return low + Math.floor(this.random() * (high - low + 1));
  }

  chance(probability: number): boolean {
    return this.random() < probability;
  }

  taskId(): string {
    let id = "t_0";
    for (let i = 0; i < 25; i++) id += ULID[this.integer(0, 31)];
    return id;
  }

  requestId(): string {
    let id = "";
    for (let i = 0; i < 32; i++) id += this.integer(0, 15).toString(16);
    return id;
  }

  held(
    identity: string,
    worker: string,
    minutesAgo: number,
    ownedCount = 1,
    leaseLeftMinutes: number | null = null,
  ): TaskSummary {
    const ownedAt = this.now - minutesAgo * MINUTE;
    return {
      id: this.taskId(),
      created_at: ownedAt - 40_000,
      // Without renewal the 10-minute lease runs out; a renewed lease has time left.
      lease_until:
        leaseLeftMinutes === null
          ? ownedAt + 10 * MINUTE
          : this.now + leaseLeftMinutes * MINUTE,
      owned_count: ownedCount,
      creator: "agent-claude-a1",
      owner: { identity, worker },
      owned_at: ownedAt,
      state: "held",
    };
  }

  available(minutesAgo: number, creator = "agent-claude-a1"): TaskSummary {
    return {
      id: this.taskId(),
      created_at: this.now - minutesAgo * MINUTE,
      lease_until: 0,
      owned_count: 0,
      creator,
      owner: null,
      owned_at: null,
      state: "available",
    };
  }

  poller(
    identity: string,
    worker: string,
    polledSecondsAgo: number,
    ownedMinutesAgo: number | null,
  ): Poller {
    return {
      identity,
      worker,
      last_polled_at: this.now - polledSecondsAgo * 1000,
      last_owned_at:
        ownedMinutesAgo === null ? null : this.now - ownedMinutesAgo * MINUTE,
    };
  }

  /** Hourly rows, newest first, with a daytime peak and occasional re-owns. */
  stats(
    topic: string,
    hours: number,
    rate: number,
    waitMinutes: number,
    runMinutes: number,
  ): HourlyStats[] {
    const current = Math.floor(this.now / HOUR) * HOUR;
    return Array.from({ length: hours }, (_, index) => {
      const daytime =
        0.3 + 0.7 * Math.max(0, 1 - Math.abs((index % 24) - 6) / 9);
      const created = Math.max(
        0,
        Math.round(rate * daytime + this.integer(-1, 1)),
      );
      const consumed = Math.max(0, created - (this.chance(0.3) ? 1 : 0));
      const owned = consumed + (this.chance(0.2) ? 1 : 0);
      const wait = waitMinutes * MINUTE * (0.6 + this.random() * 0.8);
      const run = runMinutes * MINUTE * (0.7 + this.random() * 0.6);
      return {
        topic,
        hour: current - index * HOUR,
        created,
        owned,
        reowned: this.chance(0.06) ? 1 : 0,
        consumed,
        deleted: this.chance(0.03) ? 1 : 0,
        wait_ms_sum: Math.round(owned * wait),
        wait_ms_max: owned ? Math.round(wait * 2) : 0,
        wait_count: owned,
        run_ms_sum: Math.round(consumed * run),
        run_ms_max: consumed ? Math.round(run * 1.6) : 0,
        run_count: consumed,
      };
    });
  }
}

function topic(
  name: string,
  declared: boolean,
  tasks: TaskSummary[],
  pollers: Poller[],
): SnapshotTopic {
  const waiting = tasks
    .filter((task) => task.state === "available")
    .map((task) => task.created_at);
  return {
    topic: name,
    available: waiting.length,
    held: tasks.length - waiting.length,
    oldest_available_at: waiting.length ? Math.min(...waiting) : null,
    declared,
    pollers,
    tasks,
  };
}

function domainSnapshot(
  domain: string,
  now: number,
  topics: SnapshotTopic[],
  stats: HourlyStats[],
): MonitorSnapshotResponse {
  return { ok: true, domain, generated_at: now, topics, stats };
}

function request(
  b: Builder,
  agent: string,
  topicName: string,
  minutesAgo: number,
  data: LocalRequest["data"],
  state: LocalRequest["state"] = "pending",
  task: TaskSummary | null = null,
): LocalRequest {
  return {
    agent_id: agent,
    request_id: b.requestId(),
    domain: DOMAIN,
    topic: topicName,
    reply_topic: `${topicName.startsWith("advise") ? "advised" : "verified"}/${agent}`,
    created_at: task ? task.created_at - 2_000 : b.now - minutesAgo * MINUTE,
    data,
    task_id:
      state === "submission_unknown" ? null : task ? task.id : b.taskId(),
    state,
  };
}

function reply(
  b: Builder,
  agent: string,
  creator: string,
  minutesAgo: number,
  data: LocalReply["data"],
  requestId: string | null,
): LocalReply {
  const createdAt = b.now - minutesAgo * MINUTE;
  return {
    agent_id: agent,
    task_id: b.taskId(),
    topic: `${creator === "adviser" ? "advised" : "verified"}/${agent}`,
    creator,
    created_at: createdAt,
    received_at: createdAt + 4_000,
    data,
    request_id: requestId,
  };
}

const PR = { pr: 1031, sha: "3497557d", tier: "pr" };

/**
 * A FluxShot-like board with an incident: windows-1 is down, so
 * verify/windows has no live worker; a verify/macos lease has lapsed; and a
 * failed task waits in verify/linux/error.
 */
function busy(hours: number, now: number): Snapshot {
  const b = new Builder(now, 7);
  const linuxRun = b.held("linux-1", "verify-a", 6);
  const windowsQueued = b.available(18);
  const topics = [
    topic(
      "verify/linux",
      true,
      [
        linuxRun,
        b.held("linux-1", "verify-b", 2, 2),
        b.available(3),
        b.available(1, "agent-codex-b7"),
      ],
      [
        b.poller("linux-1", "verify-a", 5, 6),
        b.poller("linux-1", "verify-b", 12, 2),
      ],
    ),
    topic(
      "verify/macos",
      true,
      [b.held("mac-1", "verify-a", 11)],
      [b.poller("mac-1", "verify-a", 8, 11)],
    ),
    topic(
      "verify/windows",
      true,
      [windowsQueued, b.available(9)],
      [b.poller("windows-1", "verify-a", 1_900, 47)],
    ),
    topic(
      "build/windows",
      true,
      [],
      [b.poller("windows-1", "build-a", 1_900, 180)],
    ),
    topic(
      "advise",
      true,
      [b.held("adviser", "opus", 4, 1, 6)],
      [b.poller("adviser", "opus", 20, 4)],
    ),
    topic("verified/agent-claude-a1", false, [b.available(1, "linux-1")], []),
    topic("verify/linux/error", false, [b.available(95, "linux-1")], []),
  ];
  const stats = [
    ...b.stats("verify/linux", hours, 6, 4, 9),
    ...b.stats("verify/macos", hours, 3, 6, 14),
    ...b.stats("verify/windows", hours, 2, 15, 22),
    ...b.stats("build/windows", hours, 1, 3, 35),
    ...b.stats("advise", hours, 2, 1, 6),
  ];
  const answered = b.requestId();
  return {
    generated_at: now,
    domains: [domainSnapshot(DOMAIN, now, topics, stats)],
    local: {
      requests: [
        request(
          b,
          "agent-claude-a1",
          "verify/linux",
          6,
          PR,
          "pending",
          linuxRun,
        ),
        request(
          b,
          "agent-claude-a1",
          "verify/windows",
          18,
          PR,
          "pending",
          windowsQueued,
        ),
        request(
          b,
          "agent-codex-b7",
          "advise",
          7,
          { question: "Is a 30-minute solve-lane hold OK for the G-9 batch?" },
          "submission_unknown",
        ),
      ],
      replies: [
        reply(
          b,
          "agent-claude-a1",
          "mac-1",
          31,
          {
            request_id: answered,
            result: "pass",
            tiers: { T0: "pass", T1: "pass", T3: "pass", T4: "pass" },
            report: "r2://reports/e93474db-macos.json",
          },
          answered,
        ),
        reply(
          b,
          "agent-codex-b7",
          "adviser",
          180,
          {
            answer:
              "Approved: hold the lock for up to 45 minutes for this measurement only.",
          },
          null,
        ),
      ],
    },
  };
}

/**
 * A healthy board during a long Windows build: windows-1 has not polled for
 * 35 minutes because build-a holds a renewed lease and verify-a waits for the
 * machine's capacity. Nothing should alert.
 */
function building(hours: number, now: number): Snapshot {
  const b = new Builder(now, 13);
  const build = b.held("windows-1", "build-a", 35, 1, 7);
  const queued = b.available(4);
  const linux = b.held("linux-1", "verify-a", 3, 1, 8);
  const topics = [
    topic(
      "verify/linux",
      true,
      [linux],
      [
        b.poller("linux-1", "verify-a", 180, 3),
        b.poller("linux-1", "verify-b", 14, 40),
      ],
    ),
    topic("verify/macos", true, [], [b.poller("mac-1", "verify-a", 22, 25)]),
    topic(
      "verify/windows",
      true,
      [queued],
      [b.poller("windows-1", "verify-a", 2_100, 60)],
    ),
    topic(
      "build/windows",
      true,
      [build],
      [b.poller("windows-1", "build-a", 2_100, 35)],
    ),
    topic("advise", true, [], [b.poller("adviser", "opus", 9, 50)]),
  ];
  const stats = [
    ...b.stats("verify/linux", hours, 6, 4, 9),
    ...b.stats("verify/macos", hours, 3, 6, 14),
    ...b.stats("verify/windows", hours, 2, 15, 22),
    ...b.stats("build/windows", hours, 1, 3, 35),
    ...b.stats("advise", hours, 2, 1, 6),
  ];
  return {
    generated_at: now,
    domains: [domainSnapshot(DOMAIN, now, topics, stats)],
    local: {
      requests: [
        request(
          b,
          "agent-claude-a1",
          "build/windows",
          35,
          { pr: 1032, sha: "9f1c2e7a", target: "installer" },
          "pending",
          build,
        ),
        request(
          b,
          "agent-claude-a1",
          "verify/windows",
          4,
          { pr: 1032, sha: "9f1c2e7a", tier: "pr" },
          "pending",
          queued,
        ),
        request(
          b,
          "agent-codex-b7",
          "verify/linux",
          3,
          { pr: 1033, sha: "51d0aa3c", tier: "pr" },
          "pending",
          linux,
        ),
      ],
      replies: [],
    },
  };
}

/** A fresh deployment: one domain, nothing posted, no local records. */
function empty(_hours: number, now: number): Snapshot {
  return {
    generated_at: now,
    domains: [domainSnapshot(DOMAIN, now, [], [])],
    local: { requests: [], replies: [] },
  };
}

/** The busy board, with one domain unreachable and local state unreadable. */
function degraded(hours: number, now: number): Snapshot {
  return {
    ...busy(hours, now),
    errors: [
      "Snapshot unavailable for domain github.com/provos/tasklease",
      "Local requester state could not be read",
    ],
  };
}

/** Stress limits: many topics and domains, a truncated queue, long names. */
function large(hours: number, now: number): Snapshot {
  const b = new Builder(now, 11);
  const longTopic = `verify/${"x".repeat(121)}`;
  const domains = [
    "github.com/provos/fluxshoteditor",
    "github.com/provos/tasklease",
    "acme/pipeline",
  ];
  const snapshots = domains.map((domain, d) => {
    const topics: SnapshotTopic[] = [];
    const stats: HourlyStats[] = [];
    for (let i = 0; i < 14; i++) {
      const name =
        d === 0 && i === 0
          ? longTopic
          : `work/shard-${String(i).padStart(2, "0")}`;
      const tasks = Array.from({ length: b.integer(0, 6) }, (_, t) =>
        t % 3 === 0
          ? b.held(
              `host-${i}`,
              `worker-${t}`,
              b.integer(1, 30),
              b.integer(1, 4),
              b.integer(1, 9),
            )
          : b.available(b.integer(1, 600)),
      );
      topics.push(
        topic(name, i % 2 === 0, tasks, [
          b.poller(
            `host-${i}`,
            "worker-0",
            b.integer(1, 4_000),
            b.integer(1, 300),
          ),
        ]),
      );
      stats.push(
        ...b.stats(
          name,
          hours,
          b.integer(1, 12),
          b.integer(1, 20),
          b.integer(2, 60),
        ),
      );
    }
    if (d === 0) {
      const flood = topic(
        "verify/flood",
        true,
        Array.from({ length: 100 }, (_, t) => b.available(600 - t)),
        [],
      );
      // The service sends at most 100 tasks; counts cover the whole queue.
      topics.push({ ...flood, available: 1_250, truncated: true });
    }
    return domainSnapshot(domain, now, topics, stats);
  });
  return {
    generated_at: now,
    domains: snapshots,
    local: {
      requests: Array.from({ length: 20 }, (_, i) =>
        request(
          b,
          `agent-${i % 4}`,
          i === 0 ? longTopic : `work/shard-${String(i % 14).padStart(2, "0")}`,
          i * 7,
          { index: i },
          i % 9 === 0 ? "submission_unknown" : "pending",
        ),
      ),
      replies: Array.from({ length: 33 }, (_, i) =>
        reply(
          b,
          `agent-${i % 4}`,
          `host-${i % 14}`,
          i * 11,
          { index: i, result: i % 5 === 0 ? "fail" : "pass" },
          i % 6 === 0 ? null : b.requestId(),
        ),
      ),
    },
  };
}

const builders: Record<
  SnapshotScenario,
  (hours: number, now: number) => Snapshot
> = { busy, building, empty, degraded, large };

export function mockSnapshot(
  scenario: SnapshotScenario,
  hours: number,
  now: number,
): Snapshot {
  return builders[scenario](hours, now);
}
