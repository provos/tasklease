import type { ErrorCode, ErrorResponse } from "./generated/protocol";

const statuses: Record<ErrorCode, number> = {
  invalid: 400,
  unauthenticated: 401,
  forbidden: 403,
  not_found: 404,
  empty: 404,
  exists: 409,
  too_large: 413,
};

export function failure(
  code: ErrorCode,
  message: string = code,
): ErrorResponse {
  return { ok: false, error: { code, message } };
}

export function respond(result: { ok: true } | ErrorResponse): Response {
  return Response.json(result, {
    status: result.ok ? 200 : statuses[result.error.code],
  });
}
