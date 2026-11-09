/**
 * Operator health derived from one snapshot: worker and machine state, topic
 * health, alerts and the state of this machine's pending requests.
 *
 * Everything here is computed from data the monitor already receives. Times
 * are measured against each domain's `generated_at`, so a slow refresh does
 * not age the board.
 */
import type {
  HourlyStats,
  MonitorSnapshotResponse,
  Poller,
  TaskSummary,
} from "../../service/src/generated/protocol.ts";
import { duration } from "./format.ts";
import type { LocalRequest, Snapshot } from "./model.ts";

const MINUTE = 60_000;
/** Workers poll every 30 s (±20 %) when idle; three missed polls is silent. */
export const LIVE_MS = 3 * MINUTE;
/** Ten minutes without a poll, while not busy, is down. */
export const DOWN_MS = 10 * MINUTE;
/** After a day, a worker is retired rather than down: listed, never alerted. */
export const INACTIVE_MS = 24 * 60 * MINUTE;
/** A queue is aging when its oldest task waits this long and 3× the usual wait. */
export const AGING_MS = 10 * MINUTE;
/** An unconfirmed submission this old needs `tl request --resend` or a check. */
export const UNKNOWN_SUBMISSION_MS = 5 * MINUTE;

export type Level = "critical" | "warning" | "ok" | "idle";
export type Role = "work" | "error" | "reply" | "other";
export type WorkerState =
  "busy" | "lapsed" | "live" | "waiting" | "silent" | "down" | "inactive";

const SEVERITY: Record<Level, number> = {
  critical: 0,
  warning: 1,
  ok: 2,
  idle: 3,
};

export function bySeverity(a: Level, b: Level): number {
  return SEVERITY[a] - SEVERITY[b];
}

export interface WorkerHealth {
  domain: string;
  identity: string;
  worker: string;
  topics: string[];
  state: WorkerState;
  lastPolledAt: number;
  /** The held task that makes the worker busy or lapsed. */
  task: (TaskSummary & { topic: string }) | null;
  generatedAt: number;
}

export interface MachineHealth {
  identity: string;
  level: Level;
  state: "busy" | "live" | "silent" | "down" | "inactive";
  workers: WorkerHealth[];
  lastPolledAt: number;
  generatedAt: number;
}

export interface TopicHealth {
  key: string;
  domain: string;
  topic: string;
  role: Role;
  level: Level;
  reasons: string[];
  available: number;
  held: number;
  oldestAvailableAt: number | null;
  /** Workers live, busy or waiting for capacity on this topic. */
  serving: number;
  /** Workers seen in the last day. */
  seen: number;
  averageWaitMs: number | null;
  tasks: TaskSummary[];
  truncated: boolean;
  generatedAt: number;
}

export interface Alert {
  key: string;
  level: "critical" | "warning";
  subject: string;
  detail: string;
}

export interface RequestHealth {
  request: LocalRequest;
  level: Level;
  status: string;
  summary: string;
}

export interface Health {
  topics: TopicHealth[];
  /** Reply and undeclared topics: shown apart, never counted as work. */
  otherTopics: TopicHealth[];
  machines: MachineHealth[];
  alerts: Alert[];
  requests: RequestHealth[];
  /** Domains without declared topics, where machines cannot be tracked. */
  undeclaredDomains: string[];
  waiting: number;
  held: number;
}

const key = (...parts: string[]): string => JSON.stringify(parts);

function role(
  topic: string,
  declared: boolean,
  replyTopics: ReadonlySet<string>,
): Role {
  if (topic.endsWith("/error")) return "error";
  if (replyTopics.has(topic)) return "reply";
  return declared ? "work" : "other";
}

function heldBy(
  domain: MonitorSnapshotResponse,
): Map<string, TaskSummary & { topic: string }> {
  const result = new Map<string, TaskSummary & { topic: string }>();
  for (const topic of domain.topics) {
    for (const task of topic.tasks) {
      if (task.state !== "held" || !task.owner) continue;
      const owner = key(task.owner.identity, task.owner.worker);
      const current = result.get(owner);
      // Prefer a live lease, so a stale task cannot hide a busy worker.
      if (!current || task.lease_until > current.lease_until)
        result.set(owner, { ...task, topic: topic.topic });
    }
  }
  return result;
}

function pollState(quietMs: number): WorkerState {
  if (quietMs <= LIVE_MS) return "live";
  if (quietMs > INACTIVE_MS) return "inactive";
  return quietMs > DOWN_MS ? "down" : "silent";
}

function workerStates(
  domain: MonitorSnapshotResponse,
  workTopics: ReadonlySet<string>,
): WorkerHealth[] {
  const now = domain.generated_at;
  const held = heldBy(domain);
  const pollers = new Map<string, { poller: Poller; topics: string[] }>();
  for (const topic of domain.topics) {
    if (!workTopics.has(topic.topic)) continue;
    for (const poller of topic.pollers) {
      const id = key(poller.identity, poller.worker);
      const entry = pollers.get(id);
      if (!entry) pollers.set(id, { poller, topics: [topic.topic] });
      else {
        entry.topics.push(topic.topic);
        if (poller.last_polled_at > entry.poller.last_polled_at)
          entry.poller = poller;
      }
    }
  }
  const workers = [...pollers].map(([id, { poller, topics }]): WorkerHealth => {
    const task = held.get(id) ?? null;
    const state: WorkerState = task
      ? task.lease_until > now
        ? "busy"
        : "lapsed"
      : pollState(now - poller.last_polled_at);
    return {
      domain: domain.domain,
      identity: poller.identity,
      worker: poller.worker,
      topics: topics.sort(),
      state,
      lastPolledAt: poller.last_polled_at,
      task,
      generatedAt: now,
    };
  });
  // Workers on one machine share its capacity: while a sibling is busy, a
  // quiet worker is waiting for capacity, not failing.
  const busyMachines = new Set(
    workers.filter((w) => w.state === "busy").map((w) => w.identity),
  );
  for (const worker of workers) {
    if (
      (worker.state === "silent" || worker.state === "down") &&
      busyMachines.has(worker.identity)
    )
      worker.state = "waiting";
  }
  return workers;
}

function machines(workers: WorkerHealth[]): MachineHealth[] {
  const groups = new Map<string, WorkerHealth[]>();
  for (const worker of workers) {
    const group = groups.get(worker.identity) ?? [];
    group.push(worker);
    groups.set(worker.identity, group);
  }
  return [...groups]
    .map(([identity, members]): MachineHealth => {
      // A lapsed lease flags the worker; whether the machine is up still
      // depends on when it last polled.
      const effective = (w: WorkerHealth): WorkerState =>
        w.state === "lapsed"
          ? pollState(w.generatedAt - w.lastPolledAt)
          : w.state;
      const has = (...states: WorkerState[]) =>
        members.some((w) => states.includes(effective(w)));
      const state = has("busy")
        ? "busy"
        : has("live", "waiting")
          ? "live"
          : has("silent")
            ? "silent"
            : has("down")
              ? "down"
              : "inactive";
      const level: Level =
        state === "down"
          ? "critical"
          : state === "silent" || members.some((w) => w.state === "lapsed")
            ? "warning"
            : state === "inactive"
              ? "idle"
              : "ok";
      const order: WorkerState[] = [
        "lapsed",
        "down",
        "silent",
        "busy",
        "live",
        "waiting",
        "inactive",
      ];
      members.sort(
        (a, b) =>
          order.indexOf(a.state) - order.indexOf(b.state) ||
          a.worker.localeCompare(b.worker),
      );
      return {
        identity,
        level,
        state,
        workers: members,
        lastPolledAt: Math.max(...members.map((w) => w.lastPolledAt)),
        generatedAt: Math.max(...members.map((w) => w.generatedAt)),
      };
    })
    .sort(
      (a, b) =>
        bySeverity(a.level, b.level) || a.identity.localeCompare(b.identity),
    );
}

function averageWait(rows: HourlyStats[]): number | null {
  let sum = 0;
  let count = 0;
  for (const row of rows) {
    sum += row.wait_ms_sum;
    count += row.wait_count;
  }
  return count ? sum / count : null;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function topicLevel(
  topic: Omit<TopicHealth, "level" | "reasons">,
  lapsed: number,
): { level: Level; reasons: string[] } {
  const oldest =
    topic.oldestAvailableAt === null
      ? null
      : topic.generatedAt - topic.oldestAvailableAt;
  const waiting = topic.available + topic.held;
  if (topic.role === "error") {
    return waiting
      ? {
          level: "critical",
          reasons: [
            `${plural(waiting, "failed task")} to inspect` +
              (oldest === null ? "" : `, oldest ${duration(oldest)}`),
          ],
        }
      : { level: "idle", reasons: [] };
  }
  if (topic.role !== "work") return { level: "idle", reasons: [] };
  const reasons: string[] = [];
  let level: Level = waiting || topic.serving ? "ok" : "idle";
  if (topic.available && !topic.serving) {
    level = "critical";
    reasons.push("no live worker");
  } else if (
    oldest !== null &&
    oldest > Math.max(AGING_MS, 3 * (topic.averageWaitMs ?? 0))
  ) {
    level = "warning";
    reasons.push(
      `oldest waiting ${duration(oldest)}` +
        (topic.averageWaitMs === null
          ? ""
          : ` (usual ${duration(topic.averageWaitMs)})`),
    );
  }
  if (lapsed) {
    if (level !== "critical") level = "warning";
    reasons.push(`lease lapsed on ${plural(lapsed, "task")}`);
  }
  return { level, reasons };
}

/** A one-line summary of request data, such as `pr 1031 · sha 3497557d`. */
export function summarize(data: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [name, value] of Object.entries(data)) {
    if (typeof value === "string") {
      const text = value.length > 60 ? `${value.slice(0, 57)}…` : value;
      parts.push(/\s/.test(text) ? `${name} “${text}”` : `${name} ${text}`);
    } else if (typeof value === "number" || typeof value === "boolean")
      parts.push(`${name} ${String(value)}`);
  }
  const text = parts.join(" · ");
  if (!text) return "{…}";
  return text.length > 100 ? `${text.slice(0, 99)}…` : text;
}

function requestHealth(
  request: LocalRequest,
  topics: ReadonlyMap<string, TopicHealth>,
  domains: ReadonlySet<string>,
  now: number,
): RequestHealth {
  const summary = summarize(request.data);
  const topic = topics.get(key(request.domain, request.topic));
  if (request.state === "submission_unknown" || request.task_id === null) {
    const old = now - request.created_at > UNKNOWN_SUBMISSION_MS;
    return {
      request,
      summary,
      level: old ? "warning" : "ok",
      status: `Submission unconfirmed for ${duration(now - request.created_at)}`,
    };
  }
  if (!domains.has(request.domain))
    return {
      request,
      summary,
      level: "idle",
      status: "Domain not on this board",
    };
  const task = topic?.tasks.find((t) => t.id === request.task_id);
  if (!topic || !task) {
    return {
      request,
      summary,
      level: "ok",
      status: topic?.truncated
        ? "Queued beyond the first 100 tasks"
        : "Not on the board: finished, reply on its way",
    };
  }
  const generated = topic.generatedAt;
  if (task.state === "held" && task.owner) {
    const by = `${task.owner.worker} on ${task.owner.identity}`;
    return task.lease_until > generated
      ? {
          request,
          summary,
          level: "ok",
          status: `Running on ${by}, lease ${duration(task.lease_until - generated)} left`,
        }
      : {
          request,
          summary,
          level: "warning",
          status: `Lease lapsed ${duration(generated - task.lease_until)} ago (${by})`,
        };
  }
  const queue = topic.tasks
    .filter((t) => t.state === "available")
    .sort((a, b) => a.created_at - b.created_at);
  const position = queue.findIndex((t) => t.id === task.id) + 1;
  const total = topic.truncated ? `${topic.available}` : `${queue.length}`;
  const queued = `Queued ${position} of ${total}`;
  return topic.serving
    ? {
        request,
        summary,
        level: topic.level === "warning" ? "warning" : "ok",
        status: queued,
      }
    : {
        request,
        summary,
        level: "critical",
        status: `${queued}, no live worker`,
      };
}

export function health(snapshot: Snapshot): Health {
  const replyTopics = new Set([
    ...snapshot.local.requests.map((r) => r.reply_topic),
    ...snapshot.local.replies.map((r) => r.topic),
  ]);
  const allTopics: TopicHealth[] = [];
  const allWorkers: WorkerHealth[] = [];
  const undeclaredDomains: string[] = [];
  for (const domain of snapshot.domains) {
    const roles = new Map(
      domain.topics.map((t) => [
        t.topic,
        role(t.topic, t.declared, replyTopics),
      ]),
    );
    const workTopics = new Set(
      [...roles].filter(([, r]) => r === "work").map(([t]) => t),
    );
    if (!domain.topics.some((t) => t.declared))
      undeclaredDomains.push(domain.domain);
    const workers = workerStates(domain, workTopics);
    allWorkers.push(...workers);
    const states = new Map(
      workers.map((w) => [key(w.identity, w.worker), w.state]),
    );
    for (const topic of domain.topics) {
      const pollerStates = topic.pollers.map(
        (p) => states.get(key(p.identity, p.worker)) ?? "inactive",
      );
      const base = {
        key: key(domain.domain, topic.topic),
        domain: domain.domain,
        topic: topic.topic,
        role: roles.get(topic.topic) ?? "other",
        available: topic.available,
        held: topic.held,
        oldestAvailableAt: topic.oldest_available_at,
        serving: pollerStates.filter((s) =>
          ["busy", "live", "waiting"].includes(s),
        ).length,
        seen: pollerStates.filter((s) => s !== "inactive").length,
        averageWaitMs: averageWait(
          domain.stats.filter((row) => row.topic === topic.topic),
        ),
        tasks: topic.tasks,
        truncated: topic.truncated === true,
        generatedAt: domain.generated_at,
      };
      const lapsed = topic.tasks.filter(
        (t) => t.state === "held" && t.lease_until <= domain.generated_at,
      ).length;
      allTopics.push({ ...base, ...topicLevel(base, lapsed) });
    }
  }
  const age = (t: TopicHealth) =>
    t.oldestAvailableAt === null ? 0 : t.generatedAt - t.oldestAvailableAt;
  const sorted = allTopics.sort(
    (a, b) =>
      bySeverity(a.level, b.level) ||
      Number(b.role === "error") - Number(a.role === "error") ||
      age(b) - age(a) ||
      a.topic.localeCompare(b.topic),
  );
  const primary = sorted.filter((t) => t.role === "work" || t.role === "error");
  const machineList = machines(allWorkers);
  const byKey = new Map(allTopics.map((t) => [t.key, t]));
  const requests = snapshot.local.requests
    .filter((r) => r.state !== "answered")
    .map((r) =>
      requestHealth(
        r,
        byKey,
        new Set(snapshot.domains.map((d) => d.domain)),
        snapshot.generated_at,
      ),
    )
    .sort(
      (a, b) =>
        bySeverity(a.level, b.level) ||
        a.request.created_at - b.request.created_at,
    );
  const alerts: Alert[] = [];
  for (const machine of machineList) {
    if (machine.state !== "down" && machine.state !== "silent") continue;
    const quiet = machine.generatedAt - machine.lastPolledAt;
    alerts.push({
      key: key("machine", machine.identity),
      level: machine.state === "down" ? "critical" : "warning",
      subject: `${machine.identity} ${machine.state}`,
      detail: `${plural(machine.workers.length, "worker")} quiet for ${duration(quiet)}: ${machine.workers
        .map((w) => w.worker)
        .join(", ")}`,
    });
  }
  for (const topic of primary) {
    if (topic.level !== "critical" && topic.level !== "warning") continue;
    alerts.push({
      key: topic.key,
      level: topic.level,
      subject:
        snapshot.domains.length > 1
          ? `${topic.domain} ${topic.topic}`
          : topic.topic,
      detail:
        topic.role === "work" && topic.available
          ? `${topic.reasons.join("; ")} · ${plural(topic.available, "task")} waiting`
          : topic.reasons.join("; "),
    });
  }
  for (const request of requests) {
    if (
      request.request.state !== "submission_unknown" ||
      request.level !== "warning"
    )
      continue;
    alerts.push({
      key: key("request", request.request.agent_id, request.request.request_id),
      level: "warning",
      subject: `${request.request.agent_id} request to ${request.request.topic}`,
      detail: `${request.status}; check it with tl wait or retry with tl request --resend`,
    });
  }
  alerts.sort((a, b) => bySeverity(a.level, b.level));
  const work = allTopics.filter((t) => t.role === "work");
  return {
    topics: primary,
    otherTopics: sorted.filter((t) => t.role === "reply" || t.role === "other"),
    machines: machineList,
    alerts,
    requests,
    undeclaredDomains,
    waiting: work.reduce((total, t) => total + t.available, 0),
    held: work.reduce((total, t) => total + t.held, 0),
  };
}
