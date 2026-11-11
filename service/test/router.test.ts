import { env, exports } from "cloudflare:workers";
import { describe, expect, it, vi } from "vitest";
import worker, { streamIdentities } from "../src/index";
import type {
  CreateResponse,
  IdentityAddRequest,
  IdentityAddResponse,
  IdentitySummary,
} from "../src/generated/protocol";
import type { IdentityListPage } from "../src/registry_do";
import {
  validateErrorResponse,
  validateOwnResponse,
  validateIdentityListResponse,
} from "../src/generated/validators.js";
import { version } from "../src/version";

const missingId = "t_00000000000000000000000000";

function summary(name: string): IdentitySummary {
  return {
    name,
    domains: ["acme"],
    scopes: { create: [], own: [], read: [] },
    operator: false,
    created_at: 1,
    last_seen_at: null,
  };
}

async function post(
  operation: string,
  body: unknown,
  token?: string,
  workerLabel: string | null = "router-worker",
): Promise<Response> {
  return exports.default.fetch(`https://tasklease/v1/${operation}`, {
    method: "POST",
    headers: {
      ...(token === undefined ? {} : { Authorization: `Bearer ${token}` }),
      ...(workerLabel === null ? {} : { "X-Worker": workerLabel }),
    },
    body: JSON.stringify(body),
  });
}

async function provision(
  overrides: Partial<IdentityAddRequest> = {},
): Promise<{ token: string; domain: string }> {
  const domain = `router/${crypto.randomUUID()}`;
  const response = await post(
    "identity.add",
    {
      name: `router-${crypto.randomUUID()}`,
      domains: [domain],
      scopes: { create: ["**"], own: ["**"], read: ["**"] },
      operator: false,
      ...overrides,
    },
    "test-admin-token",
  );
  expect(response.status).toBe(200);
  const result = await response.json<IdentityAddResponse>();
  return { token: result.token, domain: overrides.domains?.[0] ?? domain };
}

async function expectError(
  response: Response,
  status: number,
  code: string,
): Promise<void> {
  expect(response.status).toBe(status);
  const result: unknown = await response.json();
  expect(validateErrorResponse(result)).toBe(true);
  expect(result).toMatchObject({ ok: false, error: { code } });
}

describe("HTTP routing and validation", () => {
  it("serves only the public version route without credentials", async () => {
    const response = await exports.default.fetch(
      "https://tasklease/v1/version",
    );
    expect(await response.json()).toEqual({
      service_version: version,
      protocol: "v1",
    });
    await expectError(
      await post("identity.whoami", {}),
      401,
      "unauthenticated",
    );
    await expectError(
      await post("monitor.snapshot", {}),
      401,
      "unauthenticated",
    );
    await expectError(await post("version", {}), 400, "invalid");
    await expectError(await post("constructor", {}), 400, "invalid");
    await expectError(
      await exports.default.fetch("https://tasklease/v1/create"),
      400,
      "invalid",
    );
  });

  it.each([null, "", "two workers", "slash/worker", "a".repeat(257)])(
    "rejects X-Worker %s",
    async (label) => {
      const { token } = await provision();
      await expectError(
        await post("identity.whoami", {}, token, label),
        400,
        "invalid",
      );
    },
  );

  it("accepts the 256-character worker-label boundary", async () => {
    const { token } = await provision();
    expect(
      (await post("identity.whoami", {}, token, "a".repeat(256))).status,
    ).toBe(200);
  });

  it("rejects malformed JSON, extra fields, invalid names and nonintegral holds", async () => {
    const { token, domain } = await provision();
    const malformed = await exports.default.fetch(
      "https://tasklease/v1/create",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "X-Worker": "worker" },
        body: "{",
      },
    );
    await expectError(malformed, 400, "invalid");
    for (const body of [
      null,
      [],
      { domain, topic: "work", data: {}, extra: true },
      { domain, topic: "Work", data: {} },
      { domain, topic: "x".repeat(129), data: {} },
      { domain: "bad/domain/", topic: "work", data: {} },
      { domain, topic: "work", data: [] },
    ]) {
      await expectError(await post("create", body, token), 400, "invalid");
    }
    for (const hold_seconds of [0, 86_401, 1.5])
      await expectError(
        await post("own", { domain, topic: "work", hold_seconds }, token),
        400,
        "invalid",
      );
    for (const wait_seconds of [-1, 31, 1.5, true, "5"])
      await expectError(
        await post(
          "own",
          { domain, topic: "work", hold_seconds: 60, wait_seconds },
          token,
        ),
        400,
        "invalid",
      );
    expect(
      (
        await post(
          "create",
          { domain, topic: `${"x".repeat(128)}/error`, data: {} },
          token,
        )
      ).status,
    ).toBe(200);
  });

  it("maps maximum task count and blob size to too_large before schema rejection", async () => {
    const { token, domain } = await provision();
    await expectError(
      await post(
        "consume",
        {
          domain,
          topic: "work",
          id: missingId,
          tasks: Array.from({ length: 17 }, () => ({
            topic: "reply",
            data: {},
          })),
        },
        token,
      ),
      413,
      "too_large",
    );
    await expectError(
      await post(
        "blob.put_url",
        { domain, name: "blob", size: 2 ** 31 + 1, sha256: "a".repeat(64) },
        token,
      ),
      413,
      "too_large",
    );
    await expectError(
      await post(
        "blob.put_url",
        { domain, name: "blob", size: 0, sha256: "a".repeat(64) },
        token,
      ),
      400,
      "invalid",
    );
  });

  it("measures compact serialized data in UTF-8 bytes at the exact boundary", async () => {
    const { token, domain } = await provision();
    const exact = {
      value: "a".repeat(256 * 1024 - JSON.stringify({ value: "" }).length),
    };
    expect(
      (await post("create", { domain, topic: "work", data: exact }, token))
        .status,
    ).toBe(200);
    await expectError(
      await post(
        "create",
        { domain, topic: "work", data: { value: `${exact.value}a` } },
        token,
      ),
      413,
      "too_large",
    );
    await expectError(
      await post(
        "create",
        { domain, topic: "work", data: { value: "é".repeat(140_000) } },
        token,
      ),
      413,
      "too_large",
    );
  });

  it("bounds streamed request bodies and cancels after crossing the limit", async () => {
    const { token, domain } = await provision();
    let reads = 0;
    let canceled = false;
    const body = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          reads++;
          controller.enqueue(new Uint8Array(64 * 1024).fill(32));
        },
        cancel() {
          canceled = true;
        },
      },
      { highWaterMark: 0 },
    );
    const response = await worker.fetch(
      new Request("https://tasklease/v1/create", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "X-Worker": "worker" },
        body,
      }),
      env,
    );
    await expectError(response, 413, "too_large");
    expect(canceled).toBe(true);
    expect(reads).toBeLessThanOrEqual(66);
    expect((await post("topics.list", { domain }, token)).status).toBe(200);
  });

  it("refuses too-deep data as invalid instead of leaking a validator failure", async () => {
    const { token, domain } = await provision();
    const raw = `{"domain":${JSON.stringify(domain)},"topic":"work","data":${'{"nested":'.repeat(5000)}0${"}".repeat(5000)}}`;
    const response = await exports.default.fetch(
      "https://tasklease/v1/create",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "X-Worker": "worker" },
        body: raw,
      },
    );
    await expectError(response, 400, "invalid");
  });
});

describe("HTTP authorization", () => {
  it("enforces operation scopes and domain grants before any state change", async () => {
    const domain = `scope/${crypto.randomUUID()}`;
    const { token } = await provision({
      domains: [domain],
      scopes: { create: ["work/*"], own: ["work/linux"], read: [] },
    });
    await expectError(
      await post(
        "create",
        { domain, topic: "work/linux/deeper", data: {} },
        token,
      ),
      403,
      "forbidden",
    );
    // Automatically provisioned error grants apply only to an owned topic.
    expect(
      (
        await post(
          "create",
          { domain, topic: "work/linux/error", data: {} },
          token,
        )
      ).status,
    ).toBe(200);
  });

  it("allows read through own scope, filters topics by read alone, and hides stored topic mismatches", async () => {
    const { token, domain } = await provision({
      scopes: { create: ["work", "other"], own: ["work"], read: [] },
    });
    const created = await (
      await post(
        "create",
        { domain, topic: "work", data: { secret: "task-data" } },
        token,
      )
    ).json<CreateResponse>();
    expect(
      (await post("read", { domain, topic: "work", id: created.id }, token))
        .status,
    ).toBe(200);
    expect(
      (await post("tasks.list", { domain, topic: "work" }, token)).status,
    ).toBe(200);
    expect(
      (await post("topic.pollers", { domain, topic: "work" }, token)).status,
    ).toBe(200);
    expect(await (await post("topics.list", { domain }, token)).json()).toEqual(
      { ok: true, topics: [] },
    );
    await expectError(
      await post("own", { domain, topic: "other", hold_seconds: 10 }, token),
      403,
      "forbidden",
    );
    await expectError(
      await post(
        "read",
        { domain: `${domain}/other`, topic: "work", id: created.id },
        token,
      ),
      403,
      "forbidden",
    );
    await expectError(
      await post(
        "topic.declare",
        { domain, topic: "work", declared: true },
        token,
      ),
      403,
      "forbidden",
    );
    const operator = await provision({
      domains: [domain],
      scopes: { create: [], own: [], read: [] },
      operator: true,
    });
    await expectError(
      await post(
        "read",
        { domain, topic: "other", id: created.id },
        operator.token,
      ),
      404,
      "not_found",
    );
    await expectError(
      await post(
        "own",
        { domain, topic: "empty", hold_seconds: 10 },
        operator.token,
      ),
      404,
      "empty",
    );
    expect(
      (
        await post(
          "topic.declare",
          { domain, topic: "declared", declared: true },
          operator.token,
        )
      ).status,
    ).toBe(200);
    await expectError(
      await post(
        "topic.declare",
        { domain: `${domain}/outside`, topic: "declared", declared: true },
        operator.token,
      ),
      403,
      "forbidden",
    );
  });

  it("uses holder rules for deletion without requiring a create, own, or read scope", async () => {
    const { token, domain } = await provision({
      scopes: { create: ["work"], own: [], read: [] },
    });
    const created = await (
      await post("create", { domain, topic: "work", data: {} }, token)
    ).json<CreateResponse>();
    expect(
      (await post("delete", { domain, topic: "work", id: created.id }, token))
        .status,
    ).toBe(200);
    const another = await (
      await post("create", { domain, topic: "work", data: {} }, token)
    ).json<CreateResponse>();
    const stranger = await provision({
      domains: [domain],
      scopes: { create: [], own: ["work"], read: [] },
    });
    await expectError(
      await post(
        "delete",
        { domain, topic: "work", id: another.id },
        stranger.token,
      ),
      403,
      "forbidden",
    );
    const ownedResponse = await post(
      "own",
      { domain, topic: "work", hold_seconds: 30 },
      stranger.token,
    );
    const owned: unknown = await ownedResponse.json();
    expect(validateOwnResponse(owned)).toBe(true);
    if (!validateOwnResponse(owned)) throw new Error("Invalid own response");
    await expectError(
      await post("delete", { domain, topic: "work", id: owned.task.id }, token),
      403,
      "forbidden",
    );
    expect(
      (
        await post(
          "delete",
          { domain, topic: "work", id: owned.task.id },
          stranger.token,
        )
      ).status,
    ).toBe(200);
  });

  it("requires domain-level blob capability before signing", async () => {
    const { token, domain } = await provision({
      scopes: { create: [], own: [], read: [] },
    });
    await expectError(
      await post(
        "blob.put_url",
        { domain, name: "blob", size: 1, sha256: "a".repeat(64) },
        token,
      ),
      403,
      "forbidden",
    );
    await expectError(
      await post("blob.get_url", { domain, key: "unrelated/invalid" }, token),
      403,
      "forbidden",
    );
    const creator = await provision({
      domains: [domain],
      scopes: { create: ["work"], own: [], read: [] },
    });
    await expectError(
      await post(
        "blob.get_url",
        { domain, key: "unrelated/invalid" },
        creator.token,
      ),
      403,
      "forbidden",
    );
  });

  it("returns one controlled backend failure and logs only request metadata", async () => {
    const { token, domain } = await provision();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const backend = vi
      .spyOn(env.DOMAINS, "getByName")
      .mockImplementation(() => {
        throw new Error("private-backend-error");
      });
    try {
      const request = new Request("https://tasklease/v1/create", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "X-Worker": "worker" },
        body: JSON.stringify({
          domain,
          topic: "work",
          data: { secret: "private-task-data" },
        }),
      });
      const response = await worker.fetch(request, env);
      expect(response.status).toBe(500);
      expect(await response.text()).toBe("Internal server error");
      expect(backend).toHaveBeenCalledTimes(1);
      expect(log).toHaveBeenCalledWith(
        expect.objectContaining({
          operation: "create",
          domain,
          topic: "work",
          result: "internal_error",
        }),
      );
      const output = JSON.stringify(log.mock.calls);
      expect(output).not.toContain(token);
      expect(output).not.toContain("private-task-data");
      expect(output).not.toContain("private-backend-error");
    } finally {
      backend.mockRestore();
      log.mockRestore();
    }
  });
});

describe("streamed identity lists", () => {
  it.each([{ identities: [] }, { identities: [summary("small")] }])(
    "uses one RPC for a complete small page",
    async ({ identities }) => {
      const lookup = vi.fn(async (): Promise<IdentityListPage> => ({
        identities,
      }));
      const response = await streamIdentities(lookup);
      expect(response.headers.get("Content-Type")).toBe("application/json");
      const result: unknown = await response.json();
      expect(validateIdentityListResponse(result)).toBe(true);
      expect(result).toEqual({ ok: true, identities });
      expect(lookup).toHaveBeenCalledTimes(1);
    },
  );

  it("streams continuation pages into the unchanged response envelope", async () => {
    const lookup = vi.fn(
      async (afterName?: string): Promise<IdentityListPage> => {
        if (afterName === undefined)
          return { identities: [summary("a"), summary("b")], afterName: "b" };
        if (afterName === "b")
          return { identities: [summary("c"), summary("d")], afterName: "d" };
        return { identities: [summary("e")] };
      },
    );
    const result: unknown = await (await streamIdentities(lookup)).json();
    expect(validateIdentityListResponse(result)).toBe(true);
    expect(result).toEqual({
      ok: true,
      identities: ["a", "b", "c", "d", "e"].map(summary),
    });
    expect(lookup.mock.calls).toEqual([[], ["b"], ["d"]]);
  });

  it("does not fetch continuation pages before demand or after cancellation", async () => {
    const lookup = vi.fn(async (): Promise<IdentityListPage> => ({
      identities: [summary("a")],
      afterName: "a",
    }));
    const response = await streamIdentities(lookup);
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    expect(lookup).toHaveBeenCalledTimes(1);
    expect(decoder.decode((await reader.read()).value)).toBe(
      '{"ok":true,"identities":[',
    );
    expect(lookup).toHaveBeenCalledTimes(1);
    expect(JSON.parse(decoder.decode((await reader.read()).value))).toEqual(
      summary("a"),
    );
    expect(lookup).toHaveBeenCalledTimes(1);
    await reader.cancel();
    expect(lookup).toHaveBeenCalledTimes(1);
  });

  it("discards an in-flight continuation when the reader cancels", async () => {
    let complete!: (value: IdentityListPage) => void;
    let notifyStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      notifyStarted = resolve;
    });
    const continuation = new Promise<IdentityListPage>((resolve) => {
      complete = resolve;
    });
    const lookup = vi.fn(
      async (afterName?: string): Promise<IdentityListPage> => {
        if (afterName === undefined)
          return { identities: [summary("a")], afterName: "a" };
        notifyStarted();
        return continuation;
      },
    );
    const reader = (await streamIdentities(lookup)).body!.getReader();
    await reader.read();
    await reader.read();
    const pending = reader.read();
    await started;
    await reader.cancel();
    complete({ identities: [summary("b")], afterName: "b" });
    expect((await pending).done).toBe(true);
    expect(lookup).toHaveBeenCalledTimes(2);
  });

  it("terminates a failed continuation without retrying or leaking its error", async () => {
    const lookup = vi.fn(
      async (afterName?: string): Promise<IdentityListPage> => {
        if (afterName === undefined)
          return { identities: [summary("a")], afterName: "a" };
        throw new Error("private-backend-error");
      },
    );
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      const reader = (await streamIdentities(lookup)).body!.getReader();
      await reader.read();
      await reader.read();
      await expect(reader.read()).rejects.toThrow("Internal server error");
      expect(lookup).toHaveBeenCalledTimes(2);
      expect(JSON.stringify(log.mock.calls)).not.toContain(
        "private-backend-error",
      );
    } finally {
      log.mockRestore();
    }
  });
});
