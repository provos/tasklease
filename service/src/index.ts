import type { ValidateFunction } from "ajv";
import { authenticate, bearerToken, isAdmin } from "./auth";
import { blobGetUrl, blobPutUrl } from "./blobs";
import { failure, respond } from "./errors";
import type {
  ErrorResponse,
  Identity,
  OperationRequests,
} from "./generated/protocol";
import * as validators from "./generated/validators.js";
import type { IdentityListPage } from "./registry_do";
import { allows } from "./scopes";
import type { Caller } from "./types";
import { version } from "./version";

export { DomainObject } from "./domain_do";
export { RegistryObject } from "./registry_do";

type Operation = keyof OperationRequests;
type Outcome = { ok: true } | ErrorResponse | Response;
type Handler = (
  body: unknown,
  request: Request,
  env: Env,
  identity: Identity | null,
) => Promise<Outcome>;
interface Route {
  admin: boolean;
  run: Handler;
}
type Run<T> = (
  body: T,
  caller: Caller,
  env: Env,
  identity: Identity,
) => Outcome | Promise<Outcome>;

const MAX_DATA_BYTES = 256 * 1024;
// The largest compact Consume is 16 maximum-sized data values plus metadata.
// Bound the wire representation too, including whitespace and JSON escapes.
const MAX_BODY_BYTES = 16 * MAX_DATA_BYTES + 64 * 1024;
const encoder = new TextEncoder();

class BodyTooLarge extends Error {}

async function readBody(request: Request): Promise<unknown> {
  const length = request.headers.get("Content-Length");
  if (length && /^\d+$/.test(length) && Number(length) > MAX_BODY_BYTES) {
    await request.body?.cancel();
    throw new BodyTooLarge();
  }
  const reader = request.body?.getReader();
  if (!reader) return JSON.parse("");
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new BodyTooLarge();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(
    new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes),
  ) as unknown;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function preflight(operation: Operation, body: unknown): ErrorResponse | null {
  if (!record(body)) return null;
  if (
    operation === "consume" &&
    Array.isArray(body.tasks) &&
    body.tasks.length > 16
  ) {
    return failure("too_large", "Consume accepts at most 16 tasks");
  }
  if (
    operation === "blob.put_url" &&
    typeof body.size === "number" &&
    body.size > 2 ** 31
  ) {
    return failure("too_large", "Blob exceeds 2 GiB");
  }
  return null;
}

function validate<T>(validator: ValidateFunction<T>, body: unknown): body is T {
  try {
    return validator(body);
  } catch (error) {
    // Deeply nested untrusted JSON can exceed a generated validator's stack.
    if (error instanceof RangeError) return false;
    throw error;
  }
}

function adminRoute<T>(
  validator: ValidateFunction<T>,
  run: (body: T, env: Env) => Outcome | Promise<Outcome>,
): Route {
  return {
    admin: true,
    async run(body, _request, env) {
      if (!validate(validator, body))
        return failure("invalid", "Invalid request body");
      return run(body, env);
    },
  };
}

function identityRoute<T>(validator: ValidateFunction<T>, run: Run<T>): Route {
  return {
    admin: false,
    async run(body, request, env, identity) {
      if (!validate(validator, body))
        return failure("invalid", "Invalid request body");
      const worker = request.headers.get("X-Worker");
      if (!worker || worker.length > 256 || !/^[A-Za-z0-9._-]+$/.test(worker)) {
        return failure("invalid", "Invalid X-Worker header");
      }
      if (!identity) return failure("unauthenticated");
      return run(
        body,
        {
          identity: identity.name,
          worker,
          operator: identity.operator,
          scopes: identity.scopes,
        },
        env,
        identity,
      );
    },
  };
}

function domainRoute<T extends { domain: string }>(
  validator: ValidateFunction<T>,
  authorized: (body: T, caller: Caller) => boolean,
  run: Run<T>,
): Route {
  return identityRoute(validator, (body, caller, env, identity) => {
    if (!identity.domains.includes(body.domain))
      return failure("forbidden", "Domain access denied");
    if (!caller.operator && !authorized(body, caller))
      return failure("forbidden", "Scope access denied");
    return run(body, caller, env, identity);
  });
}

function dataTooLarge(data: object): boolean {
  return encoder.encode(JSON.stringify(data)).byteLength > MAX_DATA_BYTES;
}

/** Internal paging keeps the public response unchanged and bounds retained data. */
export async function streamIdentities(
  listPage: (afterName?: string) => Promise<IdentityListPage>,
): Promise<Response> {
  // Resolve the first page before sending headers, so initial RPC failures use
  // the router's controlled 500 response. Small lists need only this one call.
  let page: IdentityListPage | null = await listPage();
  let index = 0;
  let opening = true;
  let comma = false;
  let canceled = false;
  const body = new ReadableStream<Uint8Array>(
    {
      async pull(controller) {
        if (canceled) return;
        try {
          if (opening) {
            opening = false;
            controller.enqueue(encoder.encode('{"ok":true,"identities":['));
            return;
          }
          if (
            page &&
            index === page.identities.length &&
            page.afterName !== undefined
          ) {
            const afterName = page.afterName;
            page = null;
            index = 0;
            const next = await listPage(afterName);
            if (canceled) return;
            page = next;
          }
          if (page && index < page.identities.length) {
            const identity = page.identities[index++]!;
            controller.enqueue(
              encoder.encode(`${comma ? "," : ""}${JSON.stringify(identity)}`),
            );
            comma = true;
            return;
          }
          page = null;
          controller.enqueue(encoder.encode("]}"));
          controller.close();
        } catch {
          page = null;
          if (canceled) return;
          // Headers may already be sent. Terminate a failed stream without
          // leaking backend details or retrying an RPC.
          console.log({
            event: "stream_error",
            operation: "identity.list",
            result: "internal_error",
          });
          controller.error(new Error("Internal server error"));
        }
      },
      cancel() {
        canceled = true;
        page = null;
      },
    },
    { highWaterMark: 0 },
  );
  return new Response(body, {
    headers: { "Content-Type": "application/json" },
  });
}

const canRead = (body: { topic: string }, caller: Caller): boolean =>
  allows(caller.scopes, "read", body.topic) ||
  allows(caller.scopes, "own", body.topic);

const routes: Record<Operation, Route> = {
  "identity.add": adminRoute(
    validators.validateIdentityAddRequest,
    (body, env) => env.REGISTRY.getByName("registry").add(body),
  ),
  "identity.revoke": adminRoute(
    validators.validateIdentityRevokeRequest,
    (body, env) => env.REGISTRY.getByName("registry").revoke(body),
  ),
  "identity.rotate": adminRoute(
    validators.validateIdentityRotateRequest,
    (body, env) => env.REGISTRY.getByName("registry").rotate(body),
  ),
  "identity.list": adminRoute(
    validators.validateIdentityListRequest,
    (_body, env) => {
      const registry = env.REGISTRY.getByName("registry");
      return streamIdentities((afterName) => registry.listPage(afterName));
    },
  ),
  "identity.whoami": identityRoute(
    validators.validateIdentityWhoamiRequest,
    (_body, _caller, _env, identity) => ({ ok: true, ...identity }),
  ),
  create: domainRoute(
    validators.validateCreateRequest,
    (body, caller) => allows(caller.scopes, "create", body.topic),
    (body, caller, env) => {
      if (dataTooLarge(body.data))
        return failure("too_large", "Task data exceeds 256 KiB");
      return env.DOMAINS.getByName(body.domain).create(body, caller);
    },
  ),
  own: domainRoute(
    validators.validateOwnRequest,
    (body, caller) => allows(caller.scopes, "own", body.topic),
    (body, caller, env) => env.DOMAINS.getByName(body.domain).own(body, caller),
  ),
  renew: domainRoute(
    validators.validateRenewRequest,
    (body, caller) => allows(caller.scopes, "own", body.topic),
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).renew(body, caller),
  ),
  read: domainRoute(
    validators.validateReadRequest,
    canRead,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).read(body, caller),
  ),
  // The holder rule is evaluated against the stored task, without a topic scope requirement.
  delete: domainRoute(
    validators.validateDeleteRequest,
    () => true,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).delete(body, caller),
  ),
  consume: domainRoute(
    validators.validateConsumeRequest,
    (body, caller) =>
      allows(caller.scopes, "own", body.topic) &&
      body.tasks.every((task) => allows(caller.scopes, "create", task.topic)),
    (body, caller, env) => {
      if (body.tasks.some((task) => dataTooLarge(task.data)))
        return failure("too_large", "Task data exceeds 256 KiB");
      return env.DOMAINS.getByName(body.domain).consume(body, caller);
    },
  ),
  "topics.list": domainRoute(
    validators.validateTopicsListRequest,
    () => true,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).topicsList(body, caller),
  ),
  "monitor.snapshot": domainRoute(
    validators.validateMonitorSnapshotRequest,
    () => true,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).monitorSnapshot(body, caller),
  ),
  "tasks.list": domainRoute(
    validators.validateTasksListRequest,
    canRead,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).tasksList(body, caller),
  ),
  "topic.pollers": domainRoute(
    validators.validateTopicPollersRequest,
    canRead,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).topicPollers(body, caller),
  ),
  "topic.declare": domainRoute(
    validators.validateTopicDeclareRequest,
    () => false,
    (body, caller, env) =>
      env.DOMAINS.getByName(body.domain).topicDeclare(body, caller),
  ),
  "blob.put_url": domainRoute(
    validators.validateBlobPutUrlRequest,
    (_body, caller) => caller.scopes.create.length > 0,
    (body, _caller, env) => blobPutUrl(body, env),
  ),
  "blob.get_url": domainRoute(
    validators.validateBlobGetUrlRequest,
    (_body, caller) =>
      caller.scopes.read.length > 0 || caller.scopes.own.length > 0,
    (body, _caller, env) => blobGetUrl(body, env),
  ),
};

function isOperation(value: string): value is Operation {
  return Object.hasOwn(routes, value);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const started = Date.now();
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/v1/version") {
      return Response.json({ service_version: version, protocol: "v1" });
    }
    const name = url.pathname.startsWith("/v1/") ? url.pathname.slice(4) : "";
    if (request.method !== "POST" || !isOperation(name))
      return respond(failure("invalid", "Unknown operation"));
    let identity: Identity | null = null;
    let body: unknown;
    let result = "internal_error";
    try {
      const route = routes[name];
      const token = bearerToken(request);
      if (
        route.admin
          ? !(await isAdmin(token, env.ADMIN_TOKEN))
          : !(identity = await authenticate(token, env))
      ) {
        result = "unauthenticated";
        return respond(failure("unauthenticated"));
      }
      try {
        body = await readBody(request);
      } catch (error) {
        result = error instanceof BodyTooLarge ? "too_large" : "invalid";
        return respond(
          failure(
            error instanceof BodyTooLarge ? "too_large" : "invalid",
            "Invalid request body",
          ),
        );
      }
      const outcome =
        preflight(name, body) ??
        (await route.run(body, request, env, identity));
      if (outcome instanceof Response) {
        result = outcome.ok ? "ok" : "internal_error";
        return outcome;
      }
      result = outcome.ok ? "ok" : outcome.error.code;
      return respond(outcome);
    } catch {
      // Backend failures are ambiguous for mutations. Never retry or expose details.
      return new Response("Internal server error", { status: 500 });
    } finally {
      const metadata: Record<string, string | number> = {
        event: "request",
        operation: name,
        result,
        duration_ms: Date.now() - started,
      };
      if (identity) metadata.identity = identity.name;
      if (record(body)) {
        for (const field of ["domain", "topic"] as const) {
          const value = body[field];
          if (
            typeof value === "string" &&
            value.length <= 256 &&
            /^[a-z0-9._-]+(?:\/[a-z0-9._-]+)*$/.test(value)
          )
            metadata[field] = value;
        }
      }
      console.log(metadata);
    }
  },
} satisfies ExportedHandler<Env>;
