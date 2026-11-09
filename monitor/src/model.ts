import type {
  Data,
  HourlyStats,
  MonitorSnapshotResponse,
  Poller,
  SnapshotTopic,
} from "../../service/src/generated/protocol.ts";

export type { HourlyStats };

export interface LocalRequest {
  agent_id: string;
  request_id: string;
  domain: string;
  topic: string;
  reply_topic: string;
  created_at: number;
  data: Data;
  task_id: string | null;
  state: "submission_unknown" | "pending" | "answered";
}

export interface LocalReply {
  agent_id: string;
  task_id: string;
  topic: string;
  creator: string;
  created_at: number;
  received_at: number;
  data: Data;
  request_id: string | null;
}

export interface Snapshot {
  generated_at: number;
  domains: MonitorSnapshotResponse[];
  local: { requests: LocalRequest[]; replies: LocalReply[] };
  errors?: string[];
}

export interface TopicView extends SnapshotTopic {
  domain: string;
}

export interface WorkerView extends Poller {
  domain: string;
  topic: string;
}

export interface StatsTopic {
  key: string;
  domain: string;
  generated_at: number;
  topic: string;
  rows: HourlyStats[];
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function decodeSnapshot(value: unknown): Snapshot {
  if (
    !record(value) ||
    !Number.isFinite(value.generated_at) ||
    !Array.isArray(value.domains) ||
    !value.domains.every(
      (domain: unknown) =>
        record(domain) &&
        typeof domain.domain === "string" &&
        Array.isArray(domain.topics) &&
        Array.isArray(domain.stats),
    ) ||
    !record(value.local) ||
    !Array.isArray(value.local.requests) ||
    !Array.isArray(value.local.replies) ||
    (value.errors !== undefined &&
      (!Array.isArray(value.errors) ||
        !value.errors.every((error: unknown) => typeof error === "string")))
  ) {
    throw new Error("The monitor returned an invalid snapshot.");
  }
  return value as unknown as Snapshot;
}

export function topics(snapshot: Snapshot | null): TopicView[] {
  return (
    snapshot?.domains.flatMap((domain) =>
      domain.topics.map((topic) => ({ ...topic, domain: domain.domain })),
    ) ?? []
  );
}

export function workers(topics: TopicView[]): WorkerView[] {
  return topics
    .flatMap((topic) =>
      topic.pollers.map((poller) => ({
        ...poller,
        domain: topic.domain,
        topic: topic.topic,
      })),
    )
    .sort((a, b) => b.last_polled_at - a.last_polled_at);
}

export function statsTopics(snapshot: Snapshot | null): StatsTopic[] {
  return (
    snapshot?.domains.flatMap((domain) => {
      const groups = new Map<string, HourlyStats[]>();
      for (const topic of domain.topics) groups.set(topic.topic, []);
      for (const row of domain.stats) {
        const rows = groups.get(row.topic);
        if (rows) rows.push(row);
        else groups.set(row.topic, [row]);
      }
      return [...groups].map(([topic, rows]) => ({
        key: JSON.stringify([domain.domain, topic]),
        domain: domain.domain,
        generated_at: domain.generated_at,
        topic,
        rows,
      }));
    }) ?? []
  );
}

export const HOUR = 3_600_000;

export function hourBuckets(
  rows: HourlyStats[],
  generatedAt: number,
  hours: number,
): HourlyStats[] {
  const current = Math.floor(generatedAt / HOUR) * HOUR;
  const indexed = new Map(rows.map((row) => [row.hour, row]));
  return Array.from({ length: hours }, (_, index) => {
    const hour = current - (hours - index - 1) * HOUR;
    return (
      indexed.get(hour) ?? {
        topic: "",
        hour,
        created: 0,
        owned: 0,
        reowned: 0,
        empty_polls: 0,
        consumed: 0,
        deleted: 0,
        wait_ms_sum: 0,
        wait_ms_max: 0,
        wait_count: 0,
        run_ms_sum: 0,
        run_ms_max: 0,
        run_count: 0,
      }
    );
  });
}
