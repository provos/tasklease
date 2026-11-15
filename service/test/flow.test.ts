import { exports } from "cloudflare:workers";
import type { ValidateFunction } from "ajv";
import { expect, it } from "vitest";
import {
  validateConsumeResponse,
  validateCreateResponse,
  validateDeleteResponse,
  validateErrorResponse,
  validateIdentityAddResponse,
  validateOwnResponse,
  validateReadResponse,
  validateRenewResponse,
} from "../src/generated/validators.js";
import type { Scopes } from "../src/generated/protocol";

function post(
  operation: string,
  token: string,
  body: object,
): Promise<Response> {
  return exports.default.fetch(`http://tasklease/v1/${operation}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Worker": "integration",
    },
    body: JSON.stringify(body),
  });
}

async function success<T>(
  response: Response,
  validate: ValidateFunction<T>,
): Promise<T> {
  expect(response.status).toBe(200);
  const body: unknown = await response.json();
  if (!validate(body)) {
    throw new Error(
      `Response violates its schema: ${JSON.stringify(validate.errors)}`,
    );
  }
  return body;
}

async function identity(domain: string, scopes: Scopes): Promise<string> {
  const result = await success(
    await post("identity.add", "test-admin-token", {
      name: `flow-${crypto.randomUUID()}`,
      domains: [domain],
      scopes,
      operator: false,
    }),
    validateIdentityAddResponse,
  );
  return result.token;
}

it("delivers a reply through authenticated HTTP operations with fenced IDs", async () => {
  const domain = `flow/${crypto.randomUUID()}`;
  const requester = await identity(domain, {
    create: ["work"],
    own: ["answers"],
    read: ["work"],
  });
  const worker = await identity(domain, {
    create: ["answers"],
    own: ["work"],
    read: [],
  });
  const created = await success(
    await post("create", requester, {
      domain,
      topic: "work",
      data: { reply_topic: "answers" },
    }),
    validateCreateResponse,
  );
  const owned = await success(
    await post("own", worker, { domain, topic: "work", hold_seconds: 60 }),
    validateOwnResponse,
  );
  expect(owned.task.id).not.toBe(created.id);
  expect(owned.task.origin_id).toBe(created.id);
  expect(owned.task.owned_count).toBe(1);
  const stale = await post("delete", requester, {
    domain,
    topic: "work",
    id: created.id,
  });
  expect(stale.status).toBe(404);
  const renewed = await success(
    await post("renew", worker, {
      domain,
      topic: "work",
      id: owned.task.id,
      hold_seconds: 120,
    }),
    validateRenewResponse,
  );
  expect(renewed.task.id).toBe(owned.task.id);
  expect(renewed.task.origin_id).toBe(created.id);
  expect(renewed.task.created_at).toBe(owned.task.created_at);
  expect(renewed.task.creator).toBe(owned.task.creator);
  const consumed = await success(
    await post("consume", worker, {
      domain,
      topic: "work",
      id: owned.task.id,
      tasks: [{ topic: "answers", data: { result: "done" } }],
    }),
    validateConsumeResponse,
  );
  expect(consumed.ids).toHaveLength(1);
  const reply = await success(
    await post("own", requester, {
      domain,
      topic: "answers",
      hold_seconds: 60,
    }),
    validateOwnResponse,
  );
  expect(reply.task.data).toEqual({ result: "done" });
  expect(reply.task.origin_id).toBe(created.id);
  expect(reply.task.creator).toBe(owned.task.owner?.identity);
  await success(
    await post("delete", requester, {
      domain,
      topic: "answers",
      id: reply.task.id,
    }),
    validateDeleteResponse,
  );
});

it("keeps a confused-deputy reply on the attacker's origin even when a genuine worker posts it", async () => {
  const domain = `origin/${crypto.randomUUID()}`;
  const requester = await identity(domain, {
    create: ["work"],
    own: ["answers"],
    read: ["work"],
  });
  const attacker = await identity(domain, {
    create: ["bounce"],
    own: [],
    read: [],
  });
  const worker = await identity(domain, {
    create: ["answers"],
    own: ["bounce"],
    read: [],
  });
  const victim = await success(
    await post("create", requester, { domain, topic: "work", data: {} }),
    validateCreateResponse,
  );
  const forged = await post("create", attacker, {
    domain,
    topic: "bounce",
    origin_id: victim.id,
    data: {},
  });
  expect(forged.status).toBe(400);
  const bounced = await success(
    await post("create", attacker, {
      domain,
      topic: "bounce",
      data: {
        request_id: victim.id,
        origin_id: victim.id,
        reply_topic: "answers",
      },
    }),
    validateCreateResponse,
  );
  const held = await success(
    await post("own", worker, { domain, topic: "bounce", hold_seconds: 60 }),
    validateOwnResponse,
  );
  await success(
    await post("consume", worker, {
      domain,
      topic: "bounce",
      id: held.task.id,
      tasks: [
        {
          topic: "answers",
          data: { request_id: victim.id, origin_id: victim.id },
        },
      ],
    }),
    validateConsumeResponse,
  );
  const reply = await success(
    await post("own", requester, {
      domain,
      topic: "answers",
      hold_seconds: 60,
    }),
    validateOwnResponse,
  );
  expect(reply.task.origin_id).toBe(bounced.id);
  expect(reply.task.origin_id).not.toBe(victim.id);
  expect(reply.task.creator).toBe(held.task.owner?.identity);
  expect(
    (
      await success(
        await post("read", requester, { domain, topic: "work", id: victim.id }),
        validateReadResponse,
      )
    ).task.origin_id,
  ).toBe(victim.id);
});

it("refuses the entire Consume before retiring its source or posting any target", async () => {
  const domain = `atomic/${crypto.randomUUID()}`;
  const token = await identity(domain, {
    create: ["work", "allowed"],
    own: ["work", "allowed"],
    read: [],
  });
  await success(
    await post("create", token, {
      domain,
      topic: "work",
      data: { original: true },
    }),
    validateCreateResponse,
  );
  const owned = await success(
    await post("own", token, { domain, topic: "work", hold_seconds: 60 }),
    validateOwnResponse,
  );
  const refused = await post("consume", token, {
    domain,
    topic: "work",
    id: owned.task.id,
    tasks: [
      { topic: "allowed", data: {} },
      { topic: "forbidden", data: {} },
    ],
  });
  expect(refused.status).toBe(403);
  const failure: unknown = await refused.json();
  expect(validateErrorResponse(failure)).toBe(true);
  await success(
    await post("read", token, { domain, topic: "work", id: owned.task.id }),
    validateReadResponse,
  );
  const empty = await post("own", token, {
    domain,
    topic: "allowed",
    hold_seconds: 60,
  });
  expect(empty.status).toBe(404);
  expect(await empty.json()).toMatchObject({
    ok: false,
    error: { code: "empty" },
  });
});
