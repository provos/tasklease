import { describe, expect, it } from "vitest";
import { decodeCursor, encodeCursor, newTaskId, newUlid } from "../src/ids";

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV";

function encoded(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("task IDs", () => {
  it("encodes all 48 timestamp bits and uses the ULID alphabet", () => {
    for (const now of [0, 1, 32, Date.now(), 2 ** 48 - 1]) {
      const ulid = newUlid(now);
      expect(ulid).toMatch(/^[0-7][0-9A-HJKMNP-TV-Z]{25}$/);
      const timestamp = [...ulid.slice(0, 10)].reduce(
        (value, character) => value * 32 + ALPHABET.indexOf(character),
        0,
      );
      expect(timestamp).toBe(now);
      expect(newTaskId(now)).toMatch(/^t_[0-7][0-9A-HJKMNP-TV-Z]{25}$/);
    }
  });

  it("draws independent random suffixes within one millisecond", () => {
    const ids = Array.from({ length: 1000 }, () => newUlid(1234));
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(ids.map((id) => id.slice(0, 10))).size).toBe(1);
    expect(new Set(ids.map((id) => id.slice(10))).size).toBe(ids.length);
  });

  it.each([-1, 0.1, NaN, Infinity, 2 ** 48, Number.MAX_SAFE_INTEGER])(
    "rejects a timestamp outside the ULID range: %s",
    (timestamp) => {
      expect(() => newUlid(timestamp)).toThrow(RangeError);
    },
  );
});

describe("keyset cursors", () => {
  it("uses the versioned wire format and needs only a saved position", () => {
    const position = { created_at: 123456, id: ID };
    const cursor = encodeCursor(position.created_at, position.id);
    expect(cursor).toBe(encoded(`v1:123456:${ID}`));
    // No record or backing store is involved, so deleting/rotating the source
    // task cannot invalidate its saved keyset position.
    expect(decodeCursor(cursor)).toEqual(position);
    expect(decodeCursor(encodeCursor(0, ID))).toEqual({
      created_at: 0,
      id: ID,
    });
    expect(decodeCursor(encodeCursor(Number.MAX_SAFE_INTEGER, ID))).toEqual({
      created_at: Number.MAX_SAFE_INTEGER,
      id: ID,
    });
  });

  it.each([
    "",
    "@",
    "a",
    "A".repeat(65),
    `${encoded(`v1:1:${ID}`)}=`,
    `${encoded(`v1:1:${ID}`)}\n`,
    "77-977-977-9",
    ...[
      `v2:1:${ID}`,
      `v1:-1:${ID}`,
      `v1:01:${ID}`,
      `v1:+1:${ID}`,
      `v1:1.5:${ID}`,
      `v1:1e2:${ID}`,
      `v1:9007199254740992:${ID}`,
      `v1:1:${ID}\n`,
      `v1:1:${ID}:extra`,
      `v1:1:${ID.toLowerCase()}`,
      `v1:1:t_81ARZ3NDEKTSV4RRFFQ69G5FAV`,
      `v1:1:t_01ARZ3NDEKTSV4RRFFQ69G5FAI`,
      `v1:1:01ARZ3NDEKTSV4RRFFQ69G5FAV`,
    ].map(encoded),
  ])("rejects a malformed cursor: %s", (cursor) => {
    expect(decodeCursor(cursor)).toBeNull();
  });

  it("rejects noncanonical unused base64 bits", () => {
    const canonical = encodeCursor(12, ID);
    const alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    const last = canonical.at(-1);
    expect(last).toBeDefined();
    const noncanonical =
      canonical.slice(0, -1) + alphabet[alphabet.indexOf(last!) + 1];
    expect(atob(noncanonical.replace(/-/g, "+").replace(/_/g, "/"))).toBe(
      atob(canonical),
    );
    expect(decodeCursor(noncanonical)).toBeNull();
  });

  it("refuses to encode an invalid saved position", () => {
    for (const timestamp of [
      -1,
      0.5,
      NaN,
      Infinity,
      Number.MAX_SAFE_INTEGER + 1,
    ]) {
      expect(() => encodeCursor(timestamp, ID)).toThrow(RangeError);
    }
    for (const id of [
      "",
      `${ID}\n`,
      ID.toLowerCase(),
      ID.replace("01", "81"),
    ]) {
      expect(() => encodeCursor(1, id)).toThrow(RangeError);
    }
  });
});
