import { env, exports } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { validateCreateRequest } from "../src/generated/validators.js";

describe("runtime foundations", () => {
  it("runs the generated standalone validators inside Workers", () => {
    expect(
      validateCreateRequest({
        domain: "acme/pipeline",
        topic: "work/linux",
        data: { nested: [null, { text: "hello", values: [1, true] }] },
      }),
    ).toBe(true);
    expect(
      validateCreateRequest({ domain: "acme\n", topic: "work", data: {} }),
    ).toBe(false);
  });

  it("serves its version without credentials", async () => {
    const response = await exports.default.fetch("http://tasklease/v1/version");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      service_version: "0.1.0",
      protocol: "v1",
    });
  });

  it("initializes both Durable Object classes with real SQLite storage", async () => {
    const objects = [
      env.DOMAINS.getByName("foundation/domain"),
      env.REGISTRY.getByName("foundation/registry"),
    ];
    for (const stub of objects) {
      await runInDurableObject(stub, async (_instance, state) => {
        expect(
          state.storage.sql
            .exec<{ version: number }>("SELECT version FROM schema_version")
            .toArray(),
        ).toEqual([{ version: 1 }]);
        expect(state.storage.sql.databaseSize).toBeGreaterThan(0);
      });
    }
  });
});
