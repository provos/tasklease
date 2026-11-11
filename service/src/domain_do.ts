import { DurableObject } from "cloudflare:workers";
import { failure } from "./errors";
import type {
  ConsumeRequest,
  ConsumeResponse,
  CreateRequest,
  CreateResponse,
  DeleteRequest,
  DeleteResponse,
  HourlyStats,
  MonitorSnapshotRequest,
  OwnRequest,
  OwnResponse,
  ReadRequest,
  ReadResponse,
  RenewRequest,
  RenewResponse,
  Task,
  TaskSummary,
  TasksListRequest,
  TasksListResponse,
  TopicDeclareRequest,
  TopicDeclareResponse,
  TopicPollersRequest,
  TopicPollersResponse,
  TopicsListRequest,
  TopicsListResponse,
} from "./generated/protocol";
import { decodeCursor, encodeCursor, newTaskId } from "./ids";
import { allows, matches } from "./scopes";
import type { Caller, Result } from "./types";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const PAGE_SIZE = 500;
const SNAPSHOT_CHUNK_BYTES = 64 * 1024;
const SNAPSHOT_TTL = 5 * 60 * 1000;
const SNAPSHOT_LIMIT = 2;
const encoder = new TextEncoder();

// Workers' RPC type mapping recursively rewrites JSON unions. Keep the transport
// boundary finite; the Worker validates and types the JSON payload as Data.
export type RpcTask = Omit<Task, "data"> & { data: object };
type RpcTaskResponse<T extends { ok: true; task: Task }> = Omit<T, "task"> & {
  task: RpcTask;
};

type TaskRow = {
  id: string;
  topic: string;
  created_at: number;
  lease_until: number;
  owned_count: number;
  creator: string;
  owner_identity: string | null;
  owner_worker: string | null;
  owned_at: number | null;
  data: string;
};

type TaskMetadataRow = Omit<TaskRow, "data">;
const metadataColumns = `id, topic, created_at, lease_until, owned_count, creator,
  owner_identity, owner_worker, owned_at`;

type PollerRow = {
  identity: string;
  worker: string;
  last_polled_at: number;
  last_owned_at: number | null;
};

type TopicRow = {
  topic: string;
  available: number;
  held: number;
  oldest_available_at: number | null;
  declared: number;
};

type Snapshot = { id: string; expiresAt: number; chunks: number };

/** How stale a poller row's last_polled_at may get before an empty poll writes it. */
const POLLER_REFRESH_MS = 60_000;

/**
 * Retained waiters per topic. Beyond this, long polls answer empty immediately
 * so clients fall back to their own poll sleep instead of growing the object.
 */
const MAX_WAITERS_PER_TOPIC = 10_000;

/** An in-memory long-poll waiter. Lost waiters are safe: the client retries. */
type Waiter = {
  wake: () => void;
  // setTimeout returns a number in Workers; null means no timer is armed.
  timer: ReturnType<typeof setTimeout> | null;
  /** A wake hint is outstanding and the waiter is about to retry. */
  pending: boolean;
  done: boolean;
};

/** Arm a waiter's wake-up; it settles on a hint or when `delay` elapses. */
function pause(waiter: Waiter, delay: number): Promise<void> {
  // A hint that arrived during the preceding attempt is consumed at once.
  if (waiter.pending) {
    waiter.pending = false;
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    waiter.wake = () => {
      waiter.pending = false;
      resolve();
    };
    waiter.timer = setTimeout(resolve, delay);
  });
}

const pollerQuery = `SELECT identity, worker, last_polled_at, last_owned_at FROM pollers
  WHERE topic = ? AND last_polled_at >= ? ORDER BY identity, worker`;

// Reuse the task aggregate for live topic names instead of scanning tasks twice.
const topicQuery = `WITH topics AS (
  SELECT topic FROM counts UNION SELECT topic FROM pollers WHERE last_polled_at >= ?
  UNION SELECT topic FROM declared_topics
), counts AS (
  SELECT topic, SUM(lease_until <= ?) AS available, SUM(lease_until > ?) AS held,
    MIN(CASE WHEN lease_until <= ? THEN created_at END) AS oldest_available_at
  FROM tasks GROUP BY topic
)
SELECT topics.topic, COALESCE(available, 0) AS available, COALESCE(held, 0) AS held,
  oldest_available_at, declared_topics.topic IS NOT NULL AS declared
FROM topics LEFT JOIN counts ON counts.topic = topics.topic
LEFT JOIN declared_topics ON declared_topics.topic = topics.topic ORDER BY topics.topic`;

/** Persist a projection without retaining its aggregate bytes in the isolate. */
class SnapshotWriter {
  private readonly buffer = new Uint8Array(SNAPSHOT_CHUNK_BYTES);
  private used = 0;
  private chunks = 0;

  constructor(
    private readonly sql: SqlStorage,
    private readonly id: string,
  ) {}

  write(value: string): void {
    const bytes = encoder.encode(value);
    for (let offset = 0; offset < bytes.length;) {
      const count = Math.min(
        bytes.length - offset,
        this.buffer.length - this.used,
      );
      this.buffer.set(bytes.subarray(offset, offset + count), this.used);
      this.used += count;
      offset += count;
      if (this.used === this.buffer.length) this.flush();
    }
  }

  finish(): number {
    this.flush();
    return this.chunks;
  }

  private flush(): void {
    if (this.used === 0) return;
    this.sql.exec(
      "INSERT INTO monitor_snapshot_chunks (snapshot_id, seq, data) VALUES (?, ?, ?)",
      this.id,
      this.chunks++,
      this.buffer.slice(0, this.used).buffer,
    );
    this.used = 0;
  }
}

type StatsIncrement = Partial<Omit<HourlyStats, "topic" | "hour">>;
type StatsRow = { [K in keyof HourlyStats]: HourlyStats[K] };

// Each entry upgrades the previous version. Version 1 is the foundation's
// schema_version table, and intentionally has no application tables.
const migrations = [
  `CREATE TABLE tasks (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL,
    created_at INTEGER NOT NULL, lease_until INTEGER NOT NULL,
    owned_count INTEGER NOT NULL, creator TEXT NOT NULL,
    owner_identity TEXT, owner_worker TEXT, owned_at INTEGER,
    data TEXT NOT NULL
  );
  CREATE INDEX tasks_pick ON tasks (topic, created_at, id, lease_until);
  CREATE TABLE pollers (
    topic TEXT NOT NULL, identity TEXT NOT NULL, worker TEXT NOT NULL,
    last_polled_at INTEGER NOT NULL, last_owned_at INTEGER,
    PRIMARY KEY (topic, identity, worker)
  );
  CREATE INDEX pollers_expiry ON pollers (last_polled_at);
  CREATE TABLE declared_topics (
    topic TEXT PRIMARY KEY, declared_by TEXT NOT NULL, declared_at INTEGER NOT NULL
  );
  CREATE TABLE stats_hourly (
    topic TEXT NOT NULL, hour INTEGER NOT NULL,
    created INTEGER NOT NULL DEFAULT 0, owned INTEGER NOT NULL DEFAULT 0,
    reowned INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
    wait_ms_sum INTEGER NOT NULL DEFAULT 0, wait_ms_max INTEGER NOT NULL DEFAULT 0,
    wait_count INTEGER NOT NULL DEFAULT 0,
    run_ms_sum INTEGER NOT NULL DEFAULT 0, run_ms_max INTEGER NOT NULL DEFAULT 0,
    run_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (topic, hour)
  );
  CREATE INDEX stats_expiry ON stats_hourly (hour);`,
  `CREATE TABLE monitor_snapshots (
    id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
  );
  CREATE TABLE monitor_snapshot_chunks (
    snapshot_id TEXT NOT NULL, seq INTEGER NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY (snapshot_id, seq)
  );
  CREATE INDEX monitor_snapshots_expiry ON monitor_snapshots (expires_at);`,
];

export class DomainObject extends DurableObject<Env> {
  // The cleanup alarm reschedules itself and is never deleted, so once seen
  // it stays set for this object's lifetime.
  private alarmSet = false;
  // Long-poll waiters are transient: they hold no task state, and an evicted
  // object simply drops them while callers retry their bounded waits.
  private readonly waiters = new Map<string, Waiter[]>();
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    void ctx.blockConcurrencyWhile(async () => {
      ctx.storage.transactionSync(() => {
        const sql = ctx.storage.sql;
        sql.exec(
          "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
        );
        sql.exec(
          "INSERT INTO schema_version (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
        );
        const { version } = sql
          .exec<{ version: number }>("SELECT version FROM schema_version")
          .one();
        if (version < 1 || version > migrations.length + 1) {
          throw new Error(`Unsupported domain schema version: ${version}`);
        }
        for (let current = version; current <= migrations.length; current++) {
          sql.exec(migrations[current - 1]!);
          sql.exec("UPDATE schema_version SET version = ?", current + 1);
        }
        // Response streams do not survive object eviction. Discard their
        // immutable projections when a new incarnation starts.
        sql.exec("DELETE FROM monitor_snapshot_chunks");
        sql.exec("DELETE FROM monitor_snapshots");
      });
    });
  }

  async create(
    request: CreateRequest,
    caller: Caller,
  ): Promise<Result<CreateResponse>> {
    const result = await this.operation((now) => {
      const id = this.insertTask(
        request.topic,
        JSON.stringify(request.data),
        caller,
        now,
      );
      return { ok: true, id };
    });
    // A new task is available immediately (lease_until is in the past).
    if (result.ok) this.notify(request.topic);
    return result;
  }

  /** One atomic claim attempt. The transaction is the only availability authority. */
  private async attemptOwn(
    request: OwnRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<OwnResponse>>> {
    return this.operation((now) => {
      const row = this.ctx.storage.sql
        .exec<TaskRow>(
          `SELECT * FROM tasks WHERE topic = ? AND lease_until <= ?
           ORDER BY created_at, id LIMIT 1`,
          request.topic,
          now,
        )
        .toArray()[0];
      // An empty poll writes nothing unless the poller row is a minute old, so
      // idle workers do not commit storage on every poll.
      this.ctx.storage.sql.exec(
        `INSERT INTO pollers (topic, identity, worker, last_polled_at, last_owned_at)
         VALUES (?, ?, ?, ?, ?) ON CONFLICT(topic, identity, worker) DO UPDATE SET
         last_polled_at = excluded.last_polled_at,
         last_owned_at = COALESCE(excluded.last_owned_at, pollers.last_owned_at)
         WHERE excluded.last_owned_at IS NOT NULL
           OR pollers.last_polled_at <= excluded.last_polled_at - ?`,
        request.topic,
        caller.identity,
        caller.worker,
        now,
        row ? now : null,
        POLLER_REFRESH_MS,
      );
      if (!row) return failure("empty");
      const next: TaskRow = {
        ...row,
        id: newTaskId(now),
        lease_until: now + request.hold_seconds * 1000,
        owned_count: row.owned_count + 1,
        owner_identity: caller.identity,
        owner_worker: caller.worker,
        owned_at: now,
      };
      this.ctx.storage.sql.exec("DELETE FROM tasks WHERE id = ?", row.id);
      this.insertRow(next);
      const wait = now - row.created_at;
      this.recordStats(request.topic, now, {
        owned: 1,
        reowned: row.owned_count > 0 ? 1 : 0,
        ...(row.owned_count === 0
          ? { wait_ms_sum: wait, wait_ms_max: wait, wait_count: 1 }
          : {}),
      });
      return { ok: true, task: task(next, request.domain) };
    });
  }

  /** The earliest future lease on a topic, or undefined if none can expire. */
  private nextLease(topic: string, now: number): number | undefined {
    return (
      this.ctx.storage.sql
        .exec<{ lease_until: number | null }>(
          "SELECT MIN(lease_until) AS lease_until FROM tasks WHERE topic = ? AND lease_until > ?",
          topic,
          now,
        )
        .one().lease_until ?? undefined
    );
  }

  /**
   * Wait for a claimable task. The SQLite transaction stays authoritative: a
   * notification only wakes a waiter, which then retries it from scratch.
   */
  private async awaitTask(
    request: OwnRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<OwnResponse>>> {
    const topic = request.topic;
    let queue = this.waiters.get(topic);
    if (!queue) {
      queue = [];
      this.waiters.set(topic, queue);
    }
    // Backpressure: a full topic answers empty at once, so clients fall back
    // to their own poll sleep rather than the object retaining more waiters.
    if (queue.length >= MAX_WAITERS_PER_TOPIC) return failure("empty");
    const waiter: Waiter = {
      wake: () => {},
      timer: null,
      pending: false,
      done: false,
    };
    queue.push(waiter);
    const deadline = Date.now() + request.wait_seconds! * 1000;
    try {
      for (;;) {
        // Retry after registration, so a Create that committed between an
        // earlier empty attempt and this waiter becoming visible is seen.
        const result = await this.attemptOwn(request, caller);
        if (result.ok) return result;
        const now = Date.now();
        if (now >= deadline) return failure("empty");
        const lease = this.nextLease(topic, now);
        const next = lease === undefined ? deadline : Math.min(deadline, lease);
        // The pause ends at the wait deadline or at an earlier lease expiry;
        // only the former ends the poll, so a lease wake retries the claim.
        await pause(waiter, Math.max(0, next - now));
        if (Date.now() >= deadline) return failure("empty");
      }
    } finally {
      waiter.done = true;
      clearTimeout(waiter.timer);
      const index = queue.indexOf(waiter);
      if (index >= 0) queue.splice(index, 1);
      if (queue.length === 0 && this.waiters.get(topic) === queue) {
        this.waiters.delete(topic);
      }
    }
  }

  async own(
    request: OwnRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<OwnResponse>>> {
    if (!request.wait_seconds) return this.attemptOwn(request, caller);
    return this.awaitTask(request, caller);
  }

  /**
   * Wake waiters for `count` newly available tasks. One hint per task keeps a
   * burst from waking every waiter; losers wait again for the next hint.
   */
  private notify(topic: string, count = 1): void {
    const queue = this.waiters.get(topic);
    if (!queue) return;
    for (const waiter of queue) {
      if (count === 0) return;
      if (waiter.done || waiter.pending) continue;
      waiter.pending = true;
      waiter.wake();
      count--;
    }
  }

  async renew(
    request: RenewRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<RenewResponse>>> {
    const result = await this.operation((now) => {
      const row = this.findTask(request.topic, request.id);
      if (!row) return failure("not_found");
      if (!caller.operator && row.owner_identity !== caller.identity) {
        return failure("forbidden");
      }
      row.lease_until = now + request.hold_seconds * 1000;
      this.ctx.storage.sql.exec(
        "UPDATE tasks SET lease_until = ? WHERE id = ? AND topic = ?",
        row.lease_until,
        request.id,
        request.topic,
      );
      return { ok: true, task: task(row, request.domain) };
    });
    // A renewed lease moves the next wake-up later; let waiting claims
    // recompute their expiry timer instead of sleeping until the old one.
    if (result.ok) this.notify(request.topic);
    return result;
  }

  async read(
    request: ReadRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<ReadResponse>>> {
    void caller; // Domain and request-topic grants were checked by the Worker.
    return this.operation(() => {
      const row = this.findTask(request.topic, request.id);
      return row
        ? { ok: true, task: task(row, request.domain) }
        : failure("not_found");
    });
  }

  async delete(
    request: DeleteRequest,
    caller: Caller,
  ): Promise<Result<DeleteResponse>> {
    return this.operation((now) => {
      const row = this.findTask(request.topic, request.id);
      if (!row) return failure("not_found");
      if (
        !caller.operator &&
        row.owner_identity !== caller.identity &&
        !(row.owner_identity === null && row.creator === caller.identity)
      ) {
        return failure("forbidden");
      }
      if (
        request.lease_before !== undefined &&
        row.lease_until >= request.lease_before
      ) {
        return failure(
          "forbidden",
          "Task lease is newer than the deletion cutoff",
        );
      }
      this.ctx.storage.sql.exec("DELETE FROM tasks WHERE id = ?", row.id);
      this.recordStats(row.topic, now, { deleted: 1 });
      return { ok: true };
    });
  }

  async consume(
    request: ConsumeRequest,
    caller: Caller,
  ): Promise<Result<ConsumeResponse>> {
    const result = await this.operation((now) => {
      const row = this.findTask(request.topic, request.id);
      if (!row) return failure("not_found");
      if (!caller.operator && row.owner_identity !== caller.identity) {
        return failure("forbidden");
      }
      // Check every target before retiring the source or posting any successor.
      if (
        !caller.operator &&
        request.tasks.some(
          (target) => !allows(caller.scopes, "create", target.topic),
        )
      ) {
        return failure("forbidden");
      }
      this.ctx.storage.sql.exec("DELETE FROM tasks WHERE id = ?", row.id);
      const ids = request.tasks.map((target) =>
        this.insertTask(target.topic, JSON.stringify(target.data), caller, now),
      ) as ConsumeResponse["ids"];
      const run = row.owned_at === null ? null : now - row.owned_at;
      this.recordStats(row.topic, now, {
        consumed: 1,
        ...(run === null
          ? {}
          : { run_ms_sum: run, run_ms_max: run, run_count: 1 }),
      });
      return { ok: true, ids };
    });
    // Successors are available immediately; wake one waiter per successor topic.
    if (result.ok) {
      const perTopic = new Map<string, number>();
      for (const target of request.tasks) {
        perTopic.set(target.topic, (perTopic.get(target.topic) ?? 0) + 1);
      }
      for (const [topic, count] of perTopic) this.notify(topic, count);
    }
    return result;
  }

  async topicsList(
    request: TopicsListRequest,
    caller: Caller,
  ): Promise<Result<TopicsListResponse>> {
    return this.operation((now) => {
      const topics = this.ctx.storage.sql
        .exec<TopicRow>(topicQuery, now - 7 * DAY, now, now, now)
        .toArray()
        .filter(
          (row) =>
            (caller.operator || allows(caller.scopes, "read", row.topic)) &&
            (request.glob === undefined || matches(request.glob, row.topic)),
        )
        .map((row) => ({ ...row, declared: row.declared === 1 }));
      return { ok: true, topics };
    });
  }

  async tasksList(
    request: TasksListRequest,
    caller: Caller,
  ): Promise<Result<TasksListResponse>> {
    void caller;
    return this.operation((now) => {
      const cursor =
        request.cursor === undefined ? undefined : decodeCursor(request.cursor);
      if (cursor === null) return failure("invalid", "Malformed cursor");
      const rows = cursor
        ? this.ctx.storage.sql
            .exec<TaskMetadataRow>(
              `SELECT ${metadataColumns} FROM tasks WHERE topic = ? AND (created_at, id) > (?, ?)
               ORDER BY created_at, id LIMIT ?`,
              request.topic,
              cursor.created_at,
              cursor.id,
              PAGE_SIZE + 1,
            )
            .toArray()
        : this.ctx.storage.sql
            .exec<TaskMetadataRow>(
              `SELECT ${metadataColumns} FROM tasks WHERE topic = ? ORDER BY created_at, id LIMIT ?`,
              request.topic,
              PAGE_SIZE + 1,
            )
            .toArray();
      const page = rows.slice(0, PAGE_SIZE);
      const last = page[page.length - 1];
      return {
        ok: true,
        tasks: page.map((row) => summary(row, now)),
        ...(rows.length > PAGE_SIZE && last
          ? { cursor: encodeCursor(last.created_at, last.id) }
          : {}),
      };
    });
  }

  async topicPollers(
    request: TopicPollersRequest,
    caller: Caller,
  ): Promise<Result<TopicPollersResponse>> {
    void caller;
    return this.operation((now) => ({
      ok: true,
      pollers: this.ctx.storage.sql
        .exec<PollerRow>(pollerQuery, request.topic, now - 7 * DAY)
        .toArray(),
    }));
  }

  async topicDeclare(
    request: TopicDeclareRequest,
    caller: Caller,
  ): Promise<Result<TopicDeclareResponse>> {
    return this.operation((now) => {
      if (!caller.operator) return failure("forbidden");
      if (request.declared) {
        this.ctx.storage.sql.exec(
          `INSERT INTO declared_topics (topic, declared_by, declared_at) VALUES (?, ?, ?)
           ON CONFLICT(topic) DO NOTHING`,
          request.topic,
          caller.identity,
          now,
        );
      } else {
        this.ctx.storage.sql.exec(
          "DELETE FROM declared_topics WHERE topic = ?",
          request.topic,
        );
      }
      return { ok: true };
    });
  }

  async monitorSnapshot(
    request: MonitorSnapshotRequest,
    caller: Caller,
  ): Promise<Response> {
    const now = Date.now();
    const snapshot = this.ctx.storage.transactionSync(() => {
      this.expireSnapshots(now);
      const count = this.ctx.storage.sql
        .exec<{ count: number }>(
          "SELECT COUNT(*) AS count FROM monitor_snapshots",
        )
        .one().count;
      if (count >= SNAPSHOT_LIMIT) return null;
      const id = crypto.randomUUID();
      const expiresAt = now + SNAPSHOT_TTL;
      this.ctx.storage.sql.exec(
        "INSERT INTO monitor_snapshots (id, expires_at) VALUES (?, ?)",
        id,
        expiresAt,
      );
      const writer = new SnapshotWriter(this.ctx.storage.sql, id);
      this.writeSnapshot(writer, request, caller, now);
      return { id, expiresAt, chunks: writer.finish() };
    });
    if (snapshot === null) {
      // A capacity failure is operational, like an unavailable backend. Never
      // return a successful but silently incomplete snapshot.
      return new Response("Snapshot capacity is busy", {
        status: 503,
        headers: { "Retry-After": "5", "Cache-Control": "no-store" },
      });
    }
    try {
      const alarm = await this.ctx.storage.getAlarm();
      if (alarm === null || alarm > snapshot.expiresAt) {
        await this.ctx.storage.setAlarm(snapshot.expiresAt);
      }
    } catch (error) {
      this.discardSnapshot(snapshot.id);
      throw error;
    }
    return this.snapshotResponse(snapshot);
  }

  private writeSnapshot(
    writer: SnapshotWriter,
    request: MonitorSnapshotRequest,
    caller: Caller,
    now: number,
  ): void {
    const visible = (topic: string) =>
      caller.operator ||
      allows(caller.scopes, "read", topic) ||
      allows(caller.scopes, "own", topic);
    const sql = this.ctx.storage.sql;
    writer.write(
      `{"ok":true,"domain":${JSON.stringify(request.domain)},"generated_at":${now},"topics":[`,
    );
    let comma = false;
    for (const row of sql.exec<TopicRow>(
      topicQuery,
      now - 7 * DAY,
      now,
      now,
      now,
    )) {
      if (!visible(row.topic)) continue;
      if (comma) writer.write(",");
      comma = true;
      const topic = { ...row, declared: row.declared === 1 };
      writer.write(JSON.stringify(topic).slice(0, -1) + ',"pollers":[');
      let pollerComma = false;
      for (const poller of sql.exec<PollerRow>(
        pollerQuery,
        row.topic,
        now - 7 * DAY,
      )) {
        if (pollerComma) writer.write(",");
        pollerComma = true;
        writer.write(JSON.stringify(poller));
      }
      const tasks = sql
        .exec<TaskMetadataRow>(
          `SELECT ${metadataColumns} FROM tasks WHERE topic = ?
         ORDER BY created_at, id LIMIT 101`,
          row.topic,
        )
        .toArray();
      writer.write('],"tasks":');
      writer.write(
        JSON.stringify(tasks.slice(0, 100).map((task) => summary(task, now))),
      );
      writer.write(tasks.length > 100 ? ',"truncated":true}' : "}");
    }
    writer.write('],"stats":[');
    comma = false;
    const hour = Math.floor(now / HOUR) * HOUR;
    const since = hour - ((request.hours ?? 24) - 1) * HOUR;
    for (const row of sql.exec<StatsRow>(
      `SELECT * FROM stats_hourly WHERE hour >= ? AND hour <= ? ORDER BY topic, hour`,
      since,
      hour,
    )) {
      if (!visible(row.topic)) continue;
      if (comma) writer.write(",");
      comma = true;
      writer.write(JSON.stringify(row));
    }
    writer.write("]}");
  }

  private snapshotResponse(snapshot: Snapshot): Response {
    let sequence = 0;
    let closed = false;
    const cleanup = () => {
      if (closed) return;
      closed = true;
      this.discardSnapshot(snapshot.id);
    };
    const body = new ReadableStream(
      {
        type: "bytes",
        pull: (controller) => {
          if (closed) return;
          try {
            if (Date.now() >= snapshot.expiresAt)
              throw new Error("Expired snapshot");
            const row = this.ctx.storage.sql
              .exec<{ seq: number; data: ArrayBuffer }>(
                `SELECT seq, data FROM monitor_snapshot_chunks
                 WHERE snapshot_id = ? AND seq = ?`,
                snapshot.id,
                sequence,
              )
              .toArray()[0];
            if (
              !row ||
              row.seq !== sequence ||
              !(row.data instanceof ArrayBuffer) ||
              row.data.byteLength === 0 ||
              row.data.byteLength > SNAPSHOT_CHUNK_BYTES
            ) {
              throw new Error("Incomplete snapshot");
            }
            controller.enqueue(new Uint8Array(row.data));
            if (++sequence === snapshot.chunks) {
              cleanup();
              controller.close();
            }
          } catch {
            try {
              cleanup();
            } catch {
              // Expiry also cleans up abandoned projections after storage errors.
            }
            console.log({
              event: "stream_error",
              operation: "monitor.snapshot",
            });
            controller.error(new Error("Internal server error"));
          }
        },
        cancel: () => cleanup(),
      },
      { highWaterMark: 0 },
    );
    return new Response(body, {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      },
    });
  }

  private discardSnapshot(id: string): void {
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec(
        "DELETE FROM monitor_snapshot_chunks WHERE snapshot_id = ?",
        id,
      );
      this.ctx.storage.sql.exec(
        "DELETE FROM monitor_snapshots WHERE id = ?",
        id,
      );
    });
  }

  private expireSnapshots(now: number): void {
    this.ctx.storage.sql.exec(
      `DELETE FROM monitor_snapshot_chunks WHERE snapshot_id IN
       (SELECT id FROM monitor_snapshots WHERE expires_at <= ?)`,
      now,
    );
    this.ctx.storage.sql.exec(
      "DELETE FROM monitor_snapshots WHERE expires_at <= ?",
      now,
    );
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    this.ctx.storage.transactionSync(() => {
      this.expireSnapshots(now);
      this.ctx.storage.sql.exec(
        "DELETE FROM pollers WHERE last_polled_at < ?",
        now - 7 * DAY,
      );
      this.ctx.storage.sql.exec(
        "DELETE FROM stats_hourly WHERE hour < ?",
        now - 30 * DAY,
      );
    });
    const snapshot = this.ctx.storage.sql
      .exec<{ expires_at: number | null }>(
        "SELECT MIN(expires_at) AS expires_at FROM monitor_snapshots",
      )
      .one().expires_at;
    await this.ctx.storage.setAlarm(Math.min(now + DAY, snapshot ?? Infinity));
  }

  /** No await may separate the reads, checks, and writes of an operation. */
  private async operation<T extends { ok: true }>(
    body: (now: number) => Result<T>,
  ): Promise<Result<T>> {
    const now = Date.now();
    const result = this.ctx.storage.transactionSync(() => body(now));
    // Alarms use the asynchronous storage API, after the operation commits.
    // Refusals leave storage unchanged; empty Own still records activity.
    if (!this.alarmSet && (result.ok || result.error.code === "empty")) {
      if ((await this.ctx.storage.getAlarm()) === null) {
        await this.ctx.storage.setAlarm(now + DAY);
      }
      this.alarmSet = true;
    }
    return result;
  }

  private findTask(topic: string, id: string): TaskRow | undefined {
    return this.ctx.storage.sql
      .exec<TaskRow>(
        "SELECT * FROM tasks WHERE id = ? AND topic = ?",
        id,
        topic,
      )
      .toArray()[0];
  }

  private insertTask(
    topic: string,
    data: string,
    caller: Caller,
    now: number,
  ): string {
    const id = newTaskId(now);
    this.insertRow({
      id,
      topic,
      data,
      created_at: now,
      lease_until: now - 5000,
      owned_count: 0,
      creator: caller.identity,
      owner_identity: null,
      owner_worker: null,
      owned_at: null,
    });
    this.recordStats(topic, now, { created: 1 });
    return id;
  }

  private insertRow(row: TaskRow): void {
    this.ctx.storage.sql.exec(
      `INSERT INTO tasks (id, topic, created_at, lease_until, owned_count, creator,
       owner_identity, owner_worker, owned_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      row.id,
      row.topic,
      row.created_at,
      row.lease_until,
      row.owned_count,
      row.creator,
      row.owner_identity,
      row.owner_worker,
      row.owned_at,
      row.data,
    );
  }

  private recordStats(
    topic: string,
    now: number,
    increment: StatsIncrement,
  ): void {
    this.ctx.storage.sql.exec(
      `INSERT INTO stats_hourly (
        topic, hour, created, owned, reowned, consumed, deleted,
        wait_ms_sum, wait_ms_max, wait_count, run_ms_sum, run_ms_max, run_count
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(topic, hour) DO UPDATE SET
        created = created + excluded.created, owned = owned + excluded.owned,
        reowned = reowned + excluded.reowned,
        consumed = consumed + excluded.consumed, deleted = deleted + excluded.deleted,
        wait_ms_sum = wait_ms_sum + excluded.wait_ms_sum,
        wait_ms_max = MAX(wait_ms_max, excluded.wait_ms_max), wait_count = wait_count + excluded.wait_count,
        run_ms_sum = run_ms_sum + excluded.run_ms_sum,
        run_ms_max = MAX(run_ms_max, excluded.run_ms_max), run_count = run_count + excluded.run_count`,
      topic,
      Math.floor(now / HOUR) * HOUR,
      increment.created ?? 0,
      increment.owned ?? 0,
      increment.reowned ?? 0,
      increment.consumed ?? 0,
      increment.deleted ?? 0,
      increment.wait_ms_sum ?? 0,
      increment.wait_ms_max ?? 0,
      increment.wait_count ?? 0,
      increment.run_ms_sum ?? 0,
      increment.run_ms_max ?? 0,
      increment.run_count ?? 0,
    );
  }
}

function taskFields(row: TaskMetadataRow): Omit<TaskSummary, "state"> {
  return {
    id: row.id,
    created_at: row.created_at,
    lease_until: row.lease_until,
    owned_count: row.owned_count,
    creator: row.creator,
    owner:
      row.owner_identity === null
        ? null
        : { identity: row.owner_identity, worker: row.owner_worker! },
    owned_at: row.owned_at,
  };
}

function summary(row: TaskMetadataRow, now: number): TaskSummary {
  return {
    ...taskFields(row),
    state: row.lease_until <= now ? "available" : "held",
  };
}

function task(row: TaskRow, domain: string): Task {
  return {
    ...taskFields(row),
    domain,
    topic: row.topic,
    data: JSON.parse(row.data) as Task["data"],
  };
}
