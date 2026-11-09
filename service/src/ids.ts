const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const MAX_ULID_TIME = 2 ** 48 - 1;
const TASK_ID = /^t_[0-7][0-9A-HJKMNP-TV-Z]{25}(?![\s\S])/;

/** ULIDs use 48 timestamp bits and 80 independent, cryptographically random bits. */
export function newUlid(now: number = Date.now()): string {
  if (!Number.isSafeInteger(now) || now < 0 || now > MAX_ULID_TIME) {
    throw new RangeError("ULID timestamp is outside the 48-bit range");
  }

  let timestamp = "";
  for (let index = 0; index < 10; index++) {
    timestamp = ALPHABET[now % 32] + timestamp;
    now = Math.floor(now / 32);
  }

  let random = "";
  let bits = 0;
  let value = 0;
  for (const byte of crypto.getRandomValues(new Uint8Array(10))) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      bits -= 5;
      random += ALPHABET[(value >>> bits) & 31];
    }
    value &= (1 << bits) - 1;
  }
  return timestamp + random;
}

export function newTaskId(now: number = Date.now()): string {
  return `t_${newUlid(now)}`;
}

export function base64url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function encodeCursor(createdAt: number, id: string): string {
  if (!Number.isSafeInteger(createdAt) || createdAt < 0 || !TASK_ID.test(id)) {
    throw new RangeError("Invalid cursor position");
  }
  return base64url(`v1:${createdAt}:${id}`);
}

/** A keyset position remains valid when the task that supplied it is gone. */
export function decodeCursor(
  cursor: string,
): { created_at: number; id: string } | null {
  // The largest payload is v1:<16-digit safe integer>:<28-character task ID>.
  if (cursor.length > 64 || !/^[A-Za-z0-9_-]+(?![\s\S])/.test(cursor)) {
    return null;
  }
  try {
    const value = atob(cursor.replace(/-/g, "+").replace(/_/g, "/"));
    if (base64url(value) !== cursor) return null;
    const match = /^v1:(0|[1-9][0-9]*):([^:]+)(?![\s\S])/.exec(value);
    if (!match) return null;
    const createdAt = Number(match[1]);
    const id = match[2];
    if (
      !Number.isSafeInteger(createdAt) ||
      id === undefined ||
      !TASK_ID.test(id)
    )
      return null;
    return { created_at: createdAt, id };
  } catch {
    return null;
  }
}
