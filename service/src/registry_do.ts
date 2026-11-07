import { DurableObject } from "cloudflare:workers";
import { hashToken } from "./auth";
import { failure } from "./errors";
import type {
  Identity,
  IdentityAddRequest,
  IdentityAddResponse,
  IdentityRevokeRequest,
  IdentityRevokeResponse,
  IdentityRotateRequest,
  IdentityRotateResponse,
  IdentitySummary,
  Scopes,
} from "./generated/protocol";
import type { Result } from "./types";

const MAX_IDENTITY_LIST_BYTES = 7 * 1024 * 1024;
const MAX_LIST_PAGE_BYTES = 8 * 1024 * 1024;
const MAX_LIST_PAGE_IDENTITIES = 100;
const encoder = new TextEncoder();

type IdentityRow = {
  name: string;
  domains: string;
  scopes: string;
  operator: number;
  created_at: number;
  last_seen_at: number | null;
};

export interface IdentityListPage {
  identities: IdentitySummary[];
  afterName?: string;
}

function publicIdentity(row: IdentityRow): IdentitySummary {
  return {
    name: row.name,
    domains: JSON.parse(row.domains) as string[],
    scopes: JSON.parse(row.scopes) as Scopes,
    operator: Boolean(row.operator),
    created_at: row.created_at,
    last_seen_at: row.last_seen_at,
  };
}

function newToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `tl_${btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "")}`;
}

export class RegistryObject extends DurableObject<Env> {
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
        if (version > 2) throw new Error("Unsupported registry schema version");
        if (version < 2) {
          sql.exec(`CREATE TABLE identities (
            name TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            domains TEXT NOT NULL,
            scopes TEXT NOT NULL,
            operator INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER
          )`);
          sql.exec("UPDATE schema_version SET version = 2");
        }
      });
    });
  }

  lookup(tokenHash: string): Identity | null {
    return this.ctx.storage.transactionSync(() => {
      const row = this.ctx.storage.sql
        .exec<IdentityRow>(
          `UPDATE identities SET last_seen_at = ? WHERE token_hash = ?
         RETURNING name, domains, scopes, operator, created_at, last_seen_at`,
          Date.now(),
          tokenHash,
        )
        .toArray()[0];
      if (!row) return null;
      const { name, domains, scopes, operator } = publicIdentity(row);
      return { name, domains, scopes, operator };
    });
  }

  async add(request: IdentityAddRequest): Promise<Result<IdentityAddResponse>> {
    const create = [
      ...new Set([
        ...request.scopes.create,
        ...request.scopes.own.map((glob) => `${glob}/error`),
      ]),
    ];
    if (create.length > 256)
      return failure("invalid", "Expanded create scope exceeds 256 grants");
    const domains = JSON.stringify(request.domains);
    const scopes = JSON.stringify({ ...request.scopes, create });
    // Expansion can nearly double the request. Leave room for the remaining
    // columns and SQLite record overhead below the platform's 8 MiB row limit.
    if (
      encoder.encode(domains).byteLength + encoder.encode(scopes).byteLength >
      MAX_IDENTITY_LIST_BYTES
    ) {
      return failure("invalid", "Expanded identity lists exceed 7 MiB");
    }
    const token = newToken();
    const hash = await hashToken(token);
    return this.ctx.storage.transactionSync(() => {
      const sql = this.ctx.storage.sql;
      if (
        sql
          .exec("SELECT name FROM identities WHERE name = ?", request.name)
          .toArray().length
      ) {
        return failure("exists");
      }
      sql.exec(
        `INSERT INTO identities (name, token_hash, domains, scopes, operator, created_at, last_seen_at)
         VALUES (?, ?, ?, ?, ?, ?, NULL)`,
        request.name,
        hash,
        domains,
        scopes,
        Number(request.operator),
        Date.now(),
      );
      return { ok: true, name: request.name, token };
    });
  }

  revoke(request: IdentityRevokeRequest): Result<IdentityRevokeResponse> {
    return this.ctx.storage.transactionSync(() => {
      const rows = this.ctx.storage.sql
        .exec(
          "DELETE FROM identities WHERE name = ? RETURNING name",
          request.name,
        )
        .toArray();
      return rows.length ? { ok: true } : failure("not_found");
    });
  }

  async rotate(
    request: IdentityRotateRequest,
  ): Promise<Result<IdentityRotateResponse>> {
    const token = newToken();
    const hash = await hashToken(token);
    return this.ctx.storage.transactionSync(() => {
      const rows = this.ctx.storage.sql
        .exec(
          "UPDATE identities SET token_hash = ? WHERE name = ? RETURNING name",
          hash,
          request.name,
        )
        .toArray();
      return rows.length ? { ok: true, token } : failure("not_found");
    });
  }

  listPage(afterName = ""): IdentityListPage {
    return this.ctx.storage.transactionSync(() => {
      let bytes = 256;
      let lastName = afterName;
      let count = 0;
      let more = false;
      // Read only small metadata for the overflow row. Payloads stay in SQLite
      // until they fit the page, well below the 32 MiB RPC result limit.
      const rows = this.ctx.storage.sql.exec<{
        name: string;
        list_bytes: number;
      }>(
        `SELECT name, length(CAST(domains AS BLOB)) + length(CAST(scopes AS BLOB)) AS list_bytes
         FROM identities WHERE name > ? ORDER BY name LIMIT ?`,
        afterName,
        MAX_LIST_PAGE_IDENTITIES + 1,
      );
      for (const row of rows) {
        // Public metadata, array/record overhead and punctuation fit in 512 bytes
        // per record; domains and scopes were already serialized when added.
        const rowBytes = row.list_bytes + 512;
        if (
          count >= MAX_LIST_PAGE_IDENTITIES ||
          bytes + rowBytes > MAX_LIST_PAGE_BYTES
        ) {
          if (count === 0)
            throw new Error("Identity exceeds registry page limit");
          more = true;
          break;
        }
        count++;
        bytes += rowBytes;
        lastName = row.name;
      }
      const identities =
        count === 0
          ? []
          : this.ctx.storage.sql
              .exec<IdentityRow>(
                `SELECT name, domains, scopes, operator, created_at, last_seen_at FROM identities
         WHERE name > ? AND name <= ? ORDER BY name LIMIT ?`,
                afterName,
                lastName,
                MAX_LIST_PAGE_IDENTITIES,
              )
              .toArray()
              .map(publicIdentity);
      return more ? { identities, afterName: lastName } : { identities };
    });
  }
}
