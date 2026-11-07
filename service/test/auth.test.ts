import { env, exports } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import { bearerToken, hashToken, IdentityCache, isAdmin } from "../src/auth";
import worker from "../src/index";
import type {
  Identity,
  IdentityAddRequest,
  IdentityAddResponse,
  IdentityListResponse,
  IdentityRotateResponse,
} from "../src/generated/protocol";
import {
  validateIdentityAddResponse,
  validateIdentityListResponse,
} from "../src/generated/validators.js";

const emptyScopes = { create: [], own: [], read: [] };
const identity: Identity = {
  name: "cache-test",
  domains: ["acme"],
  scopes: emptyScopes,
  operator: false,
};

function identityWithScope(length: number): Identity {
  return {
    ...identity,
    scopes: { ...emptyScopes, read: ["a".repeat(length)] },
  };
}

async function post(
  operation: string,
  body: unknown,
  token = "test-admin-token",
  workerLabel?: string,
): Promise<Response> {
  return exports.default.fetch(`https://tasklease/v1/${operation}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(workerLabel === undefined ? {} : { "X-Worker": workerLabel }),
    },
    body: JSON.stringify(body),
  });
}

async function add(
  overrides: Partial<IdentityAddRequest> = {},
): Promise<IdentityAddResponse> {
  const response = await post("identity.add", {
    name: `auth-${crypto.randomUUID()}`,
    domains: ["acme"],
    scopes: emptyScopes,
    operator: false,
    ...overrides,
  });
  expect(response.status).toBe(200);
  const result: unknown = await response.json();
  expect(validateIdentityAddResponse(result)).toBe(true);
  if (!validateIdentityAddResponse(result))
    throw new Error("Invalid add response");
  return result;
}

describe("authentication primitives", () => {
  it("hashes tokens and compares unequal-length admin inputs safely", async () => {
    expect(await hashToken("abc")).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(await isAdmin("a", "much-longer-secret")).toBe(false);
    expect(await isAdmin("same", "same")).toBe(true);
    expect(await isAdmin("", "")).toBe(false);
    expect(await isAdmin("secret", undefined)).toBe(false);
    expect(await isAdmin(null, "secret")).toBe(false);
  });

  it.each(["", "Basic secret", "Bearer", "Bearer a b", "Bearer  token"])(
    "rejects malformed authorization %s",
    (authorization) => {
      expect(
        bearerToken(
          new Request("https://tasklease", {
            headers: { Authorization: authorization },
          }),
        ),
      ).toBeNull();
    },
  );

  it("accepts the bearer scheme case-insensitively", () => {
    expect(
      bearerToken(
        new Request("https://tasklease", {
          headers: { Authorization: "bearer tl_token" },
        }),
      ),
    ).toBe("tl_token");
  });

  it("expires positive entries at 60 seconds, never caches unknown identities", async () => {
    let now = 100;
    const cache = new IdentityCache(() => now);
    const lookup = vi.fn(async (): Promise<Identity | null> => identity);
    expect(await cache.resolve("known", lookup)).toBe(identity);
    now += 59_999;
    lookup.mockResolvedValue(null);
    expect(await cache.resolve("known", lookup)).toBe(identity);
    expect(lookup).toHaveBeenCalledTimes(1);
    now++;
    expect(await cache.resolve("known", lookup)).toBeNull();
    expect(cache.size).toBe(0);
    expect(await cache.resolve("known", lookup)).toBeNull();
    expect(lookup).toHaveBeenCalledTimes(3);
    lookup.mockResolvedValue(identity);
    expect(await cache.resolve("known", lookup)).toBe(identity);
  });

  it("purges expired entries and bounds successful token churn", async () => {
    let now = 0;
    const cache = new IdentityCache(() => now);
    const lookup = async (): Promise<Identity> => identity;
    for (let i = 0; i < 4100; i++) await cache.resolve(String(i), lookup);
    expect(cache.size).toBe(4096);
    now = 60_000;
    await cache.resolve("fresh", lookup);
    expect(cache.size).toBe(1);
  });

  it("evicts enough differently sized identities to fit its byte budget", async () => {
    const cache = new IdentityCache(() => 0);
    const small = identityWithScope(250_000);
    const middle = identityWithScope(2_000_000);
    const tail = identityWithScope(500_000);
    const incoming = identityWithScope(2_000_000);
    await cache.resolve("small", async () => small);
    const smallBytes = cache.retainedBytes;
    await cache.resolve("middle", async () => middle);
    const middleBytes = cache.retainedBytes - smallBytes;
    await cache.resolve("tail", async () => tail);
    const before = cache.retainedBytes;
    const measurement = new IdentityCache(() => 0);
    await measurement.resolve("incoming", async () => incoming);

    expect(await cache.resolve("incoming", async () => incoming)).toBe(
      incoming,
    );
    expect(cache.size).toBe(2);
    expect(cache.retainedBytes).toBe(
      before - smallBytes - middleBytes + measurement.retainedBytes,
    );
    expect(cache.retainedBytes).toBeLessThanOrEqual(8 * 1024 * 1024);
    const lookup = vi.fn(async (): Promise<Identity | null> => null);
    expect(await cache.resolve("tail", lookup)).toBe(tail);
    expect(await cache.resolve("incoming", lookup)).toBe(incoming);
    expect(lookup).not.toHaveBeenCalled();
    expect(await cache.resolve("small", lookup)).toBeNull();
    expect(await cache.resolve("middle", lookup)).toBeNull();
    expect(lookup).toHaveBeenCalledTimes(2);
  });

  it("authenticates oversized identities without caching or evicting existing entries", async () => {
    const cache = new IdentityCache(() => 0);
    await cache.resolve("small", async () => identity);
    const before = cache.retainedBytes;
    const oversized = identityWithScope(5_000_000);
    const lookup = vi.fn(async (): Promise<Identity> => oversized);
    expect(await cache.resolve("oversized", lookup)).toBe(oversized);
    expect(await cache.resolve("oversized", lookup)).toBe(oversized);
    expect(lookup).toHaveBeenCalledTimes(2);
    expect(cache.size).toBe(1);
    expect(cache.retainedBytes).toBe(before);
    expect(await cache.resolve("small", lookup)).toBe(identity);
    expect(lookup).toHaveBeenCalledTimes(2);
  });

  it("reclaims each expired entry's full byte charge", async () => {
    let now = 0;
    const cache = new IdentityCache(() => now);
    await cache.resolve("first", async () => identityWithScope(1_000_000));
    const firstBytes = cache.retainedBytes;
    now = 30_000;
    const second = identityWithScope(1_500_000);
    await cache.resolve("second", async () => second);
    const secondBytes = cache.retainedBytes - firstBytes;
    now = 60_000;
    expect(await cache.resolve("missing", async () => null)).toBeNull();
    expect(cache.size).toBe(1);
    expect(cache.retainedBytes).toBe(secondBytes);
    now = 90_000;
    expect(await cache.resolve("missing", async () => null)).toBeNull();
    expect(cache.size).toBe(0);
    expect(cache.retainedBytes).toBe(0);
    await cache.resolve("fresh", async () => second);
    expect(cache.size).toBe(1);
    expect(cache.retainedBytes).toBeGreaterThan(0);
    expect(cache.retainedBytes).toBeLessThanOrEqual(8 * 1024 * 1024);
  });

  it("reclaims entries that expire while a lookup is pending", async () => {
    let now = 0;
    const cache = new IdentityCache(() => now);
    await cache.resolve("old", async () => identityWithScope(1_000_000));
    now = 59_999;
    await cache.resolve("new", async () => {
      now = 60_000;
      return identity;
    });
    const measurement = new IdentityCache(() => now);
    await measurement.resolve("new", async () => identity);
    expect(cache.size).toBe(1);
    expect(cache.retainedBytes).toBe(measurement.retainedBytes);
  });

  it("accounts once when concurrent misses replace the same cache hash", async () => {
    let now = 0;
    const cache = new IdentityCache(() => now);
    let completeFirst!: (value: Identity) => void;
    const first = cache.resolve(
      "same",
      () =>
        new Promise<Identity>((resolve) => {
          completeFirst = resolve;
        }),
    );
    now = 10;
    await cache.resolve("same", async () => identityWithScope(500_000));
    completeFirst(identity);
    expect(await first).toBe(identity);
    const measurement = new IdentityCache(() => now);
    await measurement.resolve("same", async () => identity);
    expect(cache.size).toBe(1);
    expect(cache.retainedBytes).toBe(measurement.retainedBytes);
    now = 60_000;
    expect(await cache.resolve("same", async () => null)).toBeNull();
    expect(cache.retainedBytes).toBe(0);
  });

  it("counts slow lookup time against the 60-second cache lifetime", async () => {
    let now = 0;
    const cache = new IdentityCache(() => now);
    expect(
      await cache.resolve("slow", async () => {
        now = 60_001;
        return identity;
      }),
    ).toBe(identity);
    expect(cache.size).toBe(0);
  });
});

describe("registry HTTP and persistence", () => {
  it("continues to accept and authenticate legal million-character scope globs", async () => {
    const scope = "a".repeat(1_000_000);
    const added = await add({ scopes: { ...emptyScopes, read: [scope] } });
    const response = await post("identity.whoami", {}, added.token, "worker");
    expect(response.status).toBe(200);
    const result = await response.json<Identity>();
    expect(result.scopes.read).toEqual([scope]);
  });

  it("creates one-time tokens, hashes storage, expands and deduplicates grants", async () => {
    const added = await add({
      scopes: {
        create: ["verify/linux/error", "reply/*", "reply/*"],
        own: ["verify/linux", "verify/linux"],
        read: ["**"],
      },
    });
    expect(added.token).toMatch(/^tl_[A-Za-z0-9_-]{43}$/);
    const listed = await post(
      "identity.list",
      {},
      "test-admin-token",
      "invalid worker",
    );
    const list: unknown = await listed.json();
    expect(validateIdentityListResponse(list)).toBe(true);
    if (!validateIdentityListResponse(list)) throw new Error("Invalid list");
    const record = list.identities.find((item) => item.name === added.name);
    expect(record?.scopes.create).toEqual(["verify/linux/error", "reply/*"]);
    expect(record?.last_seen_at).toBeNull();
    expect(JSON.stringify(list)).not.toContain(added.token);
    expect(JSON.stringify(list)).not.toContain("token_hash");
    await runInDurableObject(
      env.REGISTRY.getByName("registry"),
      async (_instance, state) => {
        const rows = state.storage.sql
          .exec<{ token_hash: string }>(
            "SELECT token_hash FROM identities WHERE name = ?",
            added.name,
          )
          .toArray();
        expect(rows).toEqual([{ token_hash: await hashToken(added.token) }]);
      },
    );
    expect(
      (
        await post("identity.add", {
          name: added.name,
          domains: ["acme"],
          scopes: emptyScopes,
          operator: false,
        })
      ).status,
    ).toBe(409);
  });

  it("rejects expanded create lists over 256 before writing, allows the boundary", async () => {
    const name = `expanded-${crypto.randomUUID()}`;
    const base = {
      name,
      domains: ["acme"],
      scopes: {
        create: Array.from({ length: 256 }, (_, i) => `topic-${i}`),
        own: ["work"],
        read: [],
      },
      operator: false,
    };
    expect((await post("identity.add", base)).status).toBe(400);
    expect((await post("identity.rotate", { name })).status).toBe(404);
    const added = await add({
      ...base,
      scopes: { ...base.scopes, create: base.scopes.create.slice(1) },
    });
    const result = await (
      await post("identity.whoami", {}, added.token, "worker")
    ).json<Identity>();
    expect(result.scopes.create).toHaveLength(256);
    expect(result.scopes.create).toContain("work/error");
  });

  it("accepts exactly 7 MiB of expanded lists and rejects one byte above without writing", async () => {
    const limit = 7 * 1024 * 1024;
    const domains = ["acme"];
    let read = ["r"];
    const overhead = (): number =>
      JSON.stringify(domains).length +
      JSON.stringify({ create: ["/error"], own: [""], read }).length;
    if ((limit - overhead()) % 2) read = ["rr"];
    const own = "a".repeat((limit - overhead()) / 2);
    const expanded = { create: [`${own}/error`], own: [own], read };
    expect(
      new TextEncoder().encode(JSON.stringify(domains)).byteLength +
        new TextEncoder().encode(JSON.stringify(expanded)).byteLength,
    ).toBe(limit);

    const request: IdentityAddRequest = {
      name: `list-boundary-${crypto.randomUUID()}`,
      domains,
      scopes: { create: [], own: [own], read },
      operator: false,
    };
    expect(
      new TextEncoder().encode(JSON.stringify(request)).byteLength,
    ).toBeLessThan(16 * 256 * 1024 + 64 * 1024);
    const accepted = await post("identity.add", request);
    expect(accepted.status).toBe(200);
    await runInDurableObject(
      env.REGISTRY.getByName("registry"),
      (_instance, state) => {
        expect(
          state.storage.sql
            .exec<{ bytes: number }>(
              "SELECT length(domains) + length(scopes) AS bytes FROM identities WHERE name = ?",
              request.name,
            )
            .one().bytes,
        ).toBe(limit);
      },
    );
    expect((await post("identity.revoke", { name: request.name })).status).toBe(
      200,
    );

    const rejected = {
      ...request,
      name: `above-boundary-${crypto.randomUUID()}`,
      scopes: { ...request.scopes, read: [`${read[0]}r`] },
    };
    const response = await post("identity.add", rejected);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      ok: false,
      error: { code: "invalid" },
    });
    expect(
      (await post("identity.rotate", { name: rejected.name })).status,
    ).toBe(404);
  });

  it("rejects a valid 4 MiB own glob whose expansion exceeds storage capacity", async () => {
    const name = `expanded-bytes-${crypto.randomUUID()}`;
    const request = {
      name,
      domains: ["acme"],
      scopes: { create: [], own: ["a".repeat(4 * 1024 * 1024)], read: [] },
      operator: false,
    };
    expect(
      new TextEncoder().encode(JSON.stringify(request)).byteLength,
    ).toBeLessThan(16 * 256 * 1024 + 64 * 1024);
    const response = await post("identity.add", request);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      ok: false,
      error: { code: "invalid" },
    });
    expect((await post("identity.rotate", { name })).status).toBe(404);
  });

  it("limits inputs and never treats admin credentials as an identity", async () => {
    expect(
      (await post("identity.whoami", {}, "test-admin-token", "worker")).status,
    ).toBe(401);
    expect((await post("identity.list", {}, "unknown")).status).toBe(401);
    const added = await add();
    expect((await post("identity.list", {}, added.token)).status).toBe(401);
    expect(
      (await post("identity.whoami", {}, added.token, "worker")).status,
    ).toBe(200);
    expect(
      (
        await post("identity.add", {
          name: `invalid-${crypto.randomUUID()}`,
          domains: Array(257).fill("acme"),
          scopes: emptyScopes,
          operator: false,
        })
      ).status,
    ).toBe(400);
    expect(
      (
        await post("identity.add", {
          name: `invalid-${crypto.randomUUID()}`,
          domains: ["acme"],
          scopes: { ...emptyScopes, read: Array(257).fill("**") },
          operator: false,
        })
      ).status,
    ).toBe(400);
    const withoutSecret: Env = { ...env };
    delete withoutSecret.ADMIN_TOKEN;
    const response = await worker.fetch(
      new Request("https://tasklease/v1/identity.list", {
        method: "POST",
        headers: { Authorization: "Bearer test-admin-token" },
        body: "{}",
      }),
      withoutSecret,
    );
    expect(response.status).toBe(401);
  });

  it("only updates last_seen_at on registry cache misses", async () => {
    const added = await add();
    await post("identity.whoami", {}, added.token, "worker");
    await runInDurableObject(
      env.REGISTRY.getByName("registry"),
      (_instance, state) => {
        state.storage.sql.exec(
          "UPDATE identities SET last_seen_at = 1 WHERE name = ?",
          added.name,
        );
      },
    );
    await post("identity.whoami", {}, added.token, "worker");
    const listed = await (
      await post("identity.list", {})
    ).json<IdentityListResponse>();
    expect(
      listed.identities.find((item) => item.name === added.name)?.last_seen_at,
    ).toBe(1);
  });

  it("revokes and rotates at the registry immediately and cached tokens within 60 seconds", async () => {
    const added = await add();
    let now = 0;
    const cache = new IdentityCache(() => now);
    const registry = env.REGISTRY.getByName("registry");
    const hash = await hashToken(added.token);
    const lookup = (value: string) => registry.lookup(value);
    expect(await cache.resolve(hash, lookup)).toMatchObject({
      name: added.name,
    });
    const rotated = await (
      await post("identity.rotate", { name: added.name })
    ).json<IdentityRotateResponse>();
    expect(rotated.token).not.toBe(added.token);
    expect(await registry.lookup(hash)).toBeNull();
    expect(await registry.lookup(await hashToken(rotated.token))).toMatchObject(
      { name: added.name },
    );
    now = 59_999;
    expect(await cache.resolve(hash, lookup)).toMatchObject({
      name: added.name,
    });
    now = 60_000;
    expect(await cache.resolve(hash, lookup)).toBeNull();
    expect(
      (await post("identity.whoami", {}, rotated.token, "worker")).status,
    ).toBe(200);
    expect((await post("identity.revoke", { name: added.name })).status).toBe(
      200,
    );
    expect(await registry.lookup(await hashToken(rotated.token))).toBeNull();
    expect((await post("identity.revoke", { name: added.name })).status).toBe(
      404,
    );
    expect((await post("identity.rotate", { name: added.name })).status).toBe(
      404,
    );
    const replacement = await add({ name: added.name });
    expect(replacement.token).not.toBe(rotated.token);
  });

  it("returns bounded internal pages with correct empty and row-count continuations", async () => {
    const registry = env.REGISTRY.getByName(
      `list-pages-${crypto.randomUUID()}`,
    );
    expect(await registry.listPage()).toEqual({ identities: [] });
    await runInDurableObject(registry, (_instance, state) => {
      state.storage.transactionSync(() => {
        for (let index = 0; index < 101; index++) {
          state.storage.sql.exec(
            `INSERT INTO identities (name, token_hash, domains, scopes, operator, created_at, last_seen_at)
             VALUES (?, ?, ?, ?, 0, ?, NULL)`,
            `entry-${String(index).padStart(3, "0")}`,
            String(index).padStart(64, "0"),
            JSON.stringify(["acme"]),
            JSON.stringify(emptyScopes),
            index,
          );
        }
      });
    });
    const first = await registry.listPage();
    expect(first.identities).toHaveLength(100);
    expect(first.afterName).toBe("entry-099");
    const second = await registry.listPage(first.afterName);
    expect(second.identities.map((item) => item.name)).toEqual(["entry-100"]);
    expect(second.afterName).toBeUndefined();
    expect(
      validateIdentityListResponse({ ok: true, identities: first.identities }),
    ).toBe(true);
    expect(
      validateIdentityListResponse({ ok: true, identities: second.identities }),
    ).toBe(true);
    expect(await registry.listPage("zzzz")).toEqual({ identities: [] });
  });

  it("streams an actual HTTP identity list larger than the 32 MiB RPC limit", async () => {
    const added: IdentityAddResponse[] = [];
    try {
      for (let index = 0; index < 5; index++) {
        added.push(
          await add({
            scopes: { create: [], own: ["a".repeat(3_500_000)], read: [] },
          }),
        );
      }
      const response = await post("identity.list", {});
      expect(response.status).toBe(200);
      expect(response.headers.get("Content-Type")).toBe("application/json");
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      const seen = new Set<string>();
      let bytes = 0;
      let prefix = "";
      let carry = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        const text = decoder.decode(value, { stream: true });
        prefix = (prefix + text).slice(0, 25);
        const scan = carry + text;
        for (const item of added) {
          if (scan.includes(`"name":"${item.name}"`)) seen.add(item.name);
          expect(scan.includes(item.token)).toBe(false);
        }
        expect(scan.includes('"token_hash":')).toBe(false);
        expect(scan.includes('"token":')).toBe(false);
        carry = scan.slice(-192);
      }
      carry += decoder.decode();
      expect(bytes).toBeGreaterThan(32 * 1024 * 1024);
      expect(prefix).toBe('{"ok":true,"identities":[');
      expect(carry.endsWith("]}")).toBe(true);
      expect([...seen].sort()).toEqual(added.map((item) => item.name).sort());
    } finally {
      for (const item of added) {
        expect(
          (await post("identity.revoke", { name: item.name })).status,
        ).toBe(200);
      }
    }
  });
});
