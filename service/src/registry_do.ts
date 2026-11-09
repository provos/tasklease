import { DurableObject } from "cloudflare:workers";
import { hashToken } from "./auth";
import { failure } from "./errors";
import { base64url } from "./ids";
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

const MAX_GLOB_LENGTH = 256;
// A legal identity serializes to at most about 265 KB (256 domains and three
// lists of 256 globs, each at most 256 characters), so a page stays near
// 17 MB, well below the 32 MiB RPC message limit.
const LIST_PAGE_IDENTITIES = 64;

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
  return `tl_${base64url(String.fromCharCode(...bytes))}`;
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
    if (create.some((glob) => glob.length > MAX_GLOB_LENGTH))
      return failure("invalid", "An error grant exceeds 256 characters");
    const domains = JSON.stringify(request.domains);
    const scopes = JSON.stringify({ ...request.scopes, create });
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
    const rows = this.ctx.storage.sql
      .exec<IdentityRow>(
        `SELECT name, domains, scopes, operator, created_at, last_seen_at FROM identities
         WHERE name > ? ORDER BY name LIMIT ?`,
        afterName,
        LIST_PAGE_IDENTITIES + 1,
      )
      .toArray();
    const identities = rows.slice(0, LIST_PAGE_IDENTITIES).map(publicIdentity);
    return rows.length > LIST_PAGE_IDENTITIES
      ? { identities, afterName: identities.at(-1)!.name }
      : { identities };
  }
}
