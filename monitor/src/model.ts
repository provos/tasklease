import type {
  Data,
  HourlyStats,
  MonitorSnapshotResponse,
  SnapshotTopic,
} from "../../service/src/generated/protocol.ts";

export type { HourlyStats };

export interface LocalRequest {
  agent_id: string;
  origin_id: string;
  domain: string;
  topic: string;
  reply_topic: string;
  created_at: number;
  data: Data;
  state: "pending" | "answered";
}

export interface LocalReply {
  agent_id: string;
  task_id: string;
  topic: string;
  creator: string;
  created_at: number;
  received_at: number;
  data: Data;
  origin_id: string;
  matched: boolean;
  duplicate: boolean;
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

export interface StatsTopic {
  key: string;
  domain: string;
  generated_at: number;
  topic: string;
  rows: HourlyStats[];
  available: number;
  oldest_available_at: number | null;
}

export interface TopicLatency {
  key: string;
  domain: string;
  topic: string;
  waiting: number;
  oldestWaitingMs: number | null;
  consumed: number;
  completion: { count: number; waitMs: number; afterClaimMs: number } | null;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function origin(value: unknown): boolean {
  return (
    typeof value === "string" && /^t_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(value)
  );
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
        domain.topics.every(
          (topic: unknown) =>
            record(topic) &&
            Array.isArray(topic.tasks) &&
            topic.tasks.every(
              (task: unknown) => record(task) && origin(task.origin_id),
            ),
        ) &&
        Array.isArray(domain.stats),
    ) ||
    !record(value.local) ||
    !Array.isArray(value.local.requests) ||
    !value.local.requests.every(
      (request: unknown) => record(request) && origin(request.origin_id),
    ) ||
    !Array.isArray(value.local.replies) ||
    !value.local.replies.every(
      (reply: unknown) =>
        record(reply) &&
        origin(reply.origin_id) &&
        typeof reply.duplicate === "boolean",
    ) ||
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

export function statsTopics(snapshot: Snapshot | null): StatsTopic[] {
  return (
    snapshot?.domains.flatMap((domain) => {
      const groups = new Map<string, StatsTopic>();
      function group(topic: string): StatsTopic {
        const existing = groups.get(topic);
        if (existing) return existing;
        const entry: StatsTopic = {
          key: JSON.stringify([domain.domain, topic]),
          domain: domain.domain,
          generated_at: domain.generated_at,
          topic,
          rows: [],
          available: 0,
          oldest_available_at: null,
        };
        groups.set(topic, entry);
        return entry;
      }
      for (const topic of domain.topics) {
        const entry = group(topic.topic);
        entry.available = topic.available;
        entry.oldest_available_at = topic.oldest_available_at;
      }
      for (const row of domain.stats) group(row.topic).rows.push(row);
      return [...groups.values()];
    }) ?? []
  );
}

export function topicLatencies(topics: StatsTopic[]): TopicLatency[] {
  return topics
    .map((topic): TopicLatency => {
      let waitSum = 0;
      let afterClaimSum = 0;
      let count = 0;
      let consumed = 0;
      for (const row of topic.rows) {
        consumed += row.consumed;
        const sample = row.completion_latency;
        if (!sample) continue;
        waitSum += sample.wait_ms_sum;
        afterClaimSum += sample.after_claim_ms_sum;
        count += sample.count;
      }
      return {
        key: topic.key,
        domain: topic.domain,
        topic: topic.topic,
        waiting: topic.available,
        oldestWaitingMs:
          topic.oldest_available_at === null
            ? null
            : Math.max(0, topic.generated_at - topic.oldest_available_at),
        consumed,
        completion: count
          ? {
              count,
              waitMs: waitSum / count,
              afterClaimMs: afterClaimSum / count,
            }
          : null,
      };
    })
    .sort((a, b) => {
      if (!a.completion && !b.completion) {
        return (
          (b.oldestWaitingMs ?? -1) - (a.oldestWaitingMs ?? -1) ||
          b.waiting - a.waiting
        );
      }
      return (
        (b.completion?.waitMs ?? -1) - (a.completion?.waitMs ?? -1) ||
        (b.completion?.afterClaimMs ?? -1) - (a.completion?.afterClaimMs ?? -1)
      );
    });
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
