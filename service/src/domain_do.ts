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

type StatsIncrement = Partial<Omit<HourlyStats, "topic" | "hour">>;

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
  CREATE INDEX tasks_pick ON tasks (topic, created_at, id);
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
    reowned INTEGER NOT NULL DEFAULT 0, empty_polls INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
    wait_ms_sum INTEGER NOT NULL DEFAULT 0, wait_ms_max INTEGER NOT NULL DEFAULT 0,
    wait_count INTEGER NOT NULL DEFAULT 0,
    run_ms_sum INTEGER NOT NULL DEFAULT 0, run_ms_max INTEGER NOT NULL DEFAULT 0,
    run_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (topic, hour)
  );
  CREATE INDEX stats_expiry ON stats_hourly (hour);`,
];

export class DomainObject extends DurableObject<Env> {
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
      });
    });
  }

  async create(
    request: CreateRequest,
    caller: Caller,
  ): Promise<Result<CreateResponse>> {
    return this.operation((now) => {
      const id = this.insertTask(
        request.topic,
        JSON.stringify(request.data),
        caller,
        now,
      );
      return { ok: true, id };
    });
  }

  async own(
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
      this.ctx.storage.sql.exec(
        `INSERT INTO pollers (topic, identity, worker, last_polled_at, last_owned_at)
         VALUES (?, ?, ?, ?, ?) ON CONFLICT(topic, identity, worker) DO UPDATE SET
         last_polled_at = excluded.last_polled_at,
         last_owned_at = COALESCE(excluded.last_owned_at, pollers.last_owned_at)`,
        request.topic,
        caller.identity,
        caller.worker,
        now,
        row ? now : null,
      );
      if (!row) {
        this.recordStats(request.topic, now, { empty_polls: 1 });
        return failure("empty");
      }
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

  async renew(
    request: RenewRequest,
    caller: Caller,
  ): Promise<Result<RpcTaskResponse<RenewResponse>>> {
    return this.operation((now) => {
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
    return this.operation((now) => {
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
  }

  async topicsList(
    request: TopicsListRequest,
    caller: Caller,
  ): Promise<Result<TopicsListResponse>> {
    return this.operation((now) => {
      const topics = this.ctx.storage.sql
        .exec<{
          topic: string;
          available: number;
          held: number;
          oldest_available_at: number | null;
          declared: number;
        }>(
          `WITH topics AS (
             SELECT topic FROM tasks UNION SELECT topic FROM pollers WHERE last_polled_at >= ?
             UNION SELECT topic FROM declared_topics
           ), counts AS (
             SELECT topic, SUM(lease_until <= ?) AS available, SUM(lease_until > ?) AS held,
               MIN(CASE WHEN lease_until <= ? THEN created_at END) AS oldest_available_at
             FROM tasks GROUP BY topic
           )
           SELECT topics.topic, COALESCE(available, 0) AS available, COALESCE(held, 0) AS held,
             oldest_available_at, declared_topics.topic IS NOT NULL AS declared
           FROM topics LEFT JOIN counts ON counts.topic = topics.topic
           LEFT JOIN declared_topics ON declared_topics.topic = topics.topic ORDER BY topics.topic`,
          now - 7 * DAY,
          now,
          now,
          now,
        )
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
        .exec<PollerRow>(
          `SELECT identity, worker, last_polled_at, last_owned_at FROM pollers
           WHERE topic = ? AND last_polled_at >= ? ORDER BY identity, worker`,
          request.topic,
          now - 7 * DAY,
        )
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

  async alarm(): Promise<void> {
    const now = Date.now();
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec(
        "DELETE FROM pollers WHERE last_polled_at < ?",
        now - 7 * DAY,
      );
      this.ctx.storage.sql.exec(
        "DELETE FROM stats_hourly WHERE hour < ?",
        now - 30 * DAY,
      );
    });
    await this.ctx.storage.setAlarm(now + DAY);
  }

  /** No await may separate the reads, checks, and writes of an operation. */
  private async operation<T extends { ok: true }>(
    body: (now: number) => Result<T>,
  ): Promise<Result<T>> {
    const now = Date.now();
    const result = this.ctx.storage.transactionSync(() => body(now));
    // Alarms use the asynchronous storage API, after the operation commits.
    // Refusals leave storage unchanged; empty Own still records activity.
    if (result.ok || result.error.code === "empty") {
      if ((await this.ctx.storage.getAlarm()) === null) {
        await this.ctx.storage.setAlarm(now + DAY);
      }
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
        topic, hour, created, owned, reowned, empty_polls, consumed, deleted,
        wait_ms_sum, wait_ms_max, wait_count, run_ms_sum, run_ms_max, run_count
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(topic, hour) DO UPDATE SET
        created = created + excluded.created, owned = owned + excluded.owned,
        reowned = reowned + excluded.reowned, empty_polls = empty_polls + excluded.empty_polls,
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
      increment.empty_polls ?? 0,
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
