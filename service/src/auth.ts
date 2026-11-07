import type { Identity } from "./generated/protocol";

const encoder = new TextEncoder();
const CACHE_TTL_MS = 60_000;
const CACHE_MAX_ENTRIES = 4096;
const CACHE_MAX_BYTES = 8 * 1024 * 1024;

export async function hashToken(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(token));
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function bearerToken(request: Request): string | null {
  return (
    /^Bearer ([^\s]+)$/i.exec(
      request.headers.get("Authorization") ?? "",
    )?.[1] ?? null
  );
}

export async function isAdmin(
  token: string | null,
  secret: string | undefined,
): Promise<boolean> {
  if (!token || !secret) return false;
  // Hash both inputs so timingSafeEqual always receives equal-length buffers.
  const [presented, expected] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(token)),
    crypto.subtle.digest("SHA-256", encoder.encode(secret)),
  ]);
  return crypto.subtle.timingSafeEqual(presented, expected);
}

type Lookup = (hash: string) => Promise<Identity | null>;

function entryBytes(hash: string, identity: Identity): number {
  // Charge UTF-16 storage even for ASCII, plus conservative string, array,
  // object and Map-entry overhead. Charge shared strings for every occurrence.
  const stringBytes = (value: string): number => 64 + value.length * 2;
  let bytes = 512 + stringBytes(hash) + stringBytes(identity.name);
  for (const values of [
    identity.domains,
    identity.scopes.create,
    identity.scopes.own,
    identity.scopes.read,
  ]) {
    bytes += 64 + values.length * 8;
    for (const value of values) bytes += stringBytes(value);
  }
  return bytes;
}

/** Positive results only. The clock is injectable to verify the revocation bound. */
export class IdentityCache {
  private readonly entries = new Map<
    string,
    { identity: Identity; expires: number; bytes: number }
  >();
  private bytes = 0;

  constructor(private readonly now: () => number = Date.now) {}

  get size(): number {
    return this.entries.size;
  }

  /** Conservative retained-memory estimate, bounded independently of count. */
  get retainedBytes(): number {
    return this.bytes;
  }

  private remove(hash: string): void {
    const entry = this.entries.get(hash);
    if (entry) {
      this.bytes -= entry.bytes;
      this.entries.delete(hash);
    }
  }

  private purgeExpired(now: number): void {
    for (const [hash, entry] of this.entries) {
      if (entry.expires <= now) this.remove(hash);
    }
  }

  async resolve(hash: string, lookup: Lookup): Promise<Identity | null> {
    const now = this.now();
    this.purgeExpired(now);
    const cached = this.entries.get(hash);
    if (cached) return cached.identity;
    const identity = await lookup(hash);
    // Start the TTL before the lookup, so an in-flight lookup cannot extend it.
    const expires = now + CACHE_TTL_MS;
    const completedAt = this.now();
    this.purgeExpired(completedAt);
    if (identity && completedAt < expires) {
      const bytes = entryBytes(hash, identity);
      // Legal scope globs have no length limit. A large identity still
      // authenticates successfully; it simply requires a lookup next time.
      if (bytes > CACHE_MAX_BYTES) return identity;
      // Concurrent misses can populate the same hash while lookup is pending.
      this.remove(hash);
      while (
        this.entries.size >= CACHE_MAX_ENTRIES ||
        this.bytes + bytes > CACHE_MAX_BYTES
      ) {
        this.remove(this.entries.keys().next().value!);
      }
      this.entries.set(hash, { identity, expires, bytes });
      this.bytes += bytes;
    }
    return identity;
  }
}

const identities = new IdentityCache();

export async function authenticate(
  token: string | null,
  env: Env,
): Promise<Identity | null> {
  if (!token) return null;
  const hash = await hashToken(token);
  return identities.resolve(hash, (tokenHash) =>
    env.REGISTRY.getByName("registry").lookup(tokenHash),
  );
}
