import { DurableObject } from "cloudflare:workers";

export class DomainObject extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    void ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
      );
      ctx.storage.sql.exec(
        "INSERT INTO schema_version (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
      );
    });
  }
}
