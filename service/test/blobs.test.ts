import { env as runtimeEnv } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import { blobGetUrl, blobPutUrl } from "../src/blobs";
import {
  validateBlobGetUrlResponse,
  validateBlobPutUrlResponse,
} from "../src/generated/validators.js";
import { newUlid } from "../src/ids";

const env: Env = runtimeEnv;
const SHA256 = "a".repeat(64);
const DATE = "2026-11-07";
const ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV";

function request(domain = "acme/pipeline", size = 16) {
  return { domain, name: "artifact.zip", size, sha256: SHA256 };
}

function awsEncode(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function hex(value: ArrayBuffer): string {
  return [...new Uint8Array(value)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function hmac(
  key: string | ArrayBuffer,
  value: string,
): Promise<ArrayBuffer> {
  const imported = await crypto.subtle.importKey(
    "raw",
    typeof key === "string" ? new TextEncoder().encode(key) : key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", imported, new TextEncoder().encode(value));
}

// Verify the SigV4 result without calling aws4fetch again. Real R2 acceptance
// and rejection remain an external-access check, separate from local signing.
async function signature(
  url: URL,
  method: string,
  size?: number,
): Promise<string> {
  const parameters = new URLSearchParams(url.search);
  parameters.delete("X-Amz-Signature");
  const query = [...parameters]
    .map(([key, value]) => [awsEncode(key), awsEncode(value)].join("="))
    .sort()
    .join("&");
  const signedHeaders = url.searchParams.get("X-Amz-SignedHeaders")!;
  const headers =
    size === undefined
      ? `host:${url.host}\n`
      : `content-length:${size}\nhost:${url.host}\n`;
  const canonical = [
    method,
    url.pathname,
    query,
    headers,
    signedHeaders,
    "UNSIGNED-PAYLOAD",
  ].join("\n");
  const scope = url.searchParams
    .get("X-Amz-Credential")!
    .split("/")
    .slice(1)
    .join("/");
  const datetime = url.searchParams.get("X-Amz-Date")!;
  const signingInput = [
    "AWS4-HMAC-SHA256",
    datetime,
    scope,
    hex(
      await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(canonical),
      ),
    ),
  ].join("\n");
  let key = await hmac(`AWS4${env.R2_SECRET_ACCESS_KEY}`, datetime.slice(0, 8));
  for (const part of ["auto", "s3", "aws4_request"])
    key = await hmac(key, part);
  return hex(await hmac(key, signingInput));
}

function expectedExpiry(url: URL): number {
  const datetime = url.searchParams.get("X-Amz-Date")!;
  const iso = `${datetime.slice(0, 4)}-${datetime.slice(4, 6)}-${datetime.slice(6, 8)}T${datetime.slice(9, 11)}:${datetime.slice(11, 13)}:${datetime.slice(13, 15)}Z`;
  return Date.parse(iso) + 3600 * 1000;
}

describe("blob URL signing in Workers", () => {
  it("signs an exact-size PUT for one hour against the configured bucket", async () => {
    const before = Date.now();
    const result = await blobPutUrl(request(), {
      ...env,
      R2_ACCOUNT_ID: "test-account",
      R2_BUCKET_NAME: "custom-blobs",
    });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("Expected a PUT URL");
    expect(validateBlobPutUrlResponse(result)).toBe(true);
    expect(result.key).toMatch(
      /^acme%2Fpipeline\/\d{4}-\d{2}-\d{2}\/[0-7][0-9A-HJKMNP-TV-Z]{25}\/artifact\.zip$/,
    );
    const url = new URL(result.url);
    expect(url.hostname).toBe("test-account.r2.cloudflarestorage.com");
    expect(url.pathname).toBe(
      `/custom-blobs/${result.key.split("/").map(encodeURIComponent).join("/")}`,
    );
    expect(url.pathname).toContain("acme%252Fpipeline/");
    expect(url.searchParams.get("X-Amz-Expires")).toBe("3600");
    expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe(
      "content-length;host",
    );
    expect(url.searchParams.get("X-Amz-Credential")).toContain(
      "/auto/s3/aws4_request",
    );
    expect(result.expires_at).toBe(expectedExpiry(url));
    expect(result.expires_at).toBeGreaterThanOrEqual(
      before + 3600 * 1000 - 1000,
    );
    expect(url.searchParams.get("X-Amz-Signature")).toBe(
      await signature(url, "PUT", 16),
    );
    expect(url.searchParams.get("X-Amz-Signature")).not.toBe(
      await signature(url, "PUT", 17),
    );
    expect(result.url).not.toContain(env.R2_SECRET_ACCESS_KEY!);
    // Asking for an upload URL does not create or move bytes through the API.
    expect(await env.BLOBS.head(result.key)).toBeNull();
  });

  it("checks the actual R2 binding before signing GET", async () => {
    const key = `acme%2Fpipeline/${DATE}/${newUlid()}/result.json`;
    expect(
      await blobGetUrl({ domain: "acme/pipeline", key }, env),
    ).toMatchObject({
      ok: false,
      error: { code: "not_found" },
    });
    await env.BLOBS.put(key, "stored bytes");
    try {
      const result = await blobGetUrl({ domain: "acme/pipeline", key }, env);
      expect(result.ok).toBe(true);
      if (!result.ok) throw new Error("Expected a GET URL");
      expect(validateBlobGetUrlResponse(result)).toBe(true);
      const url = new URL(result.url);
      expect(url.pathname).toBe(
        `/tasklease-blobs/${key.split("/").map(encodeURIComponent).join("/")}`,
      );
      expect(url.searchParams.get("X-Amz-Expires")).toBe("3600");
      expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe("host");
      expect(result.expires_at).toBe(expectedExpiry(url));
      expect(url.searchParams.get("X-Amz-Signature")).toBe(
        await signature(url, "GET"),
      );
      expect(result).not.toHaveProperty("body");
    } finally {
      await env.BLOBS.delete(key);
    }
  });

  it.each([".", ".."])(
    "rejects the exact dot-only domain %s",
    async (domain) => {
      expect(await blobPutUrl(request(domain), env)).toMatchObject({
        ok: false,
        error: { code: "invalid" },
      });
      for (const prefix of [domain, domain.replace(/\./g, "%2E")]) {
        expect(
          await blobGetUrl(
            { domain, key: `${prefix}/${DATE}/${ULID}/file` },
            env,
          ),
        ).toMatchObject({
          ok: false,
          error: { code: "invalid" },
        });
      }
    },
  );

  it.each([
    "github.com/acme/repo",
    "acme/./pipeline",
    "acme/../pipeline",
    "...",
  ])("preserves dots inside the longer valid domain %s", async (domain) => {
    const put = await blobPutUrl(request(domain), env);
    expect(put.ok).toBe(true);
    if (!put.ok) throw new Error("Expected a PUT URL");
    const prefix = domain.replace(/\//g, "%2F");
    expect(put.key.split("/")[0]).toBe(prefix);
    expect(new URL(put.url).pathname).toContain(
      `/tasklease-blobs/${encodeURIComponent(prefix)}/`,
    );
    await env.BLOBS.put(put.key, "stored bytes");
    try {
      const get = await blobGetUrl({ domain, key: put.key }, env);
      expect(get.ok).toBe(true);
      if (!get.ok) throw new Error("Expected a GET URL");
      expect(new URL(get.url).pathname).toBe(new URL(put.url).pathname);
      expect(
        await blobGetUrl({ domain: "acme", key: put.key }, env),
      ).toMatchObject({
        ok: false,
        error: { code: "invalid" },
      });
    } finally {
      await env.BLOBS.delete(put.key);
    }
  });

  it.each([
    ["acme", `acme%2Fpipeline/${DATE}/${ULID}/file`],
    ["acme/pipeline", `acme/${DATE}/${ULID}/file`],
    ["acme/pipeline", `acme/pipeline/${DATE}/${ULID}/file`],
    ["acme/pipeline", `acme%2fpipeline/${DATE}/${ULID}/file`],
    ["acme/pipeline", `acme%252Fpipeline/${DATE}/${ULID}/file`],
    ["acme", `acme.other/${DATE}/${ULID}/file`],
    ["acme", `acme/2026-02-30/${ULID}/file`],
    ["acme", `acme/2026-13-01/${ULID}/file`],
    ["acme", `acme/2026-1-01/${ULID}/file`],
    ["acme", `acme/${DATE}/81ARZ3NDEKTSV4RRFFQ69G5FAV/file`],
    ["acme", `acme/${DATE}/${ULID.toLowerCase()}/file`],
    ["acme", `acme/${DATE}/${ULID}/..`],
    ["acme", `acme/${DATE}/${ULID}/.`],
    ["acme", `acme/${DATE}/${ULID}/file/extra`],
    ["acme", `acme/${DATE}/${ULID}/file%2Fextra`],
    ["acme", `acme/${DATE}/${ULID}/file\n`],
    [".", `./${DATE}/${ULID}/file`],
    ["..", `../${DATE}/${ULID}/file`],
  ])("rejects a foreign or malformed key for %s: %s", async (domain, key) => {
    expect(await blobGetUrl({ domain, key }, env)).toMatchObject({
      ok: false,
      error: { code: "invalid" },
    });
  });

  it.each([
    { name: "../file" },
    { name: "." },
    { name: ".." },
    { name: "file\n" },
    { name: "x".repeat(129) },
    { size: 0 },
    { size: 1.1 },
    { size: -1 },
    { sha256: "not-a-digest" },
    { sha256: `${SHA256}\n` },
  ])("rejects an invalid upload request: %j", async (change) => {
    expect(await blobPutUrl({ ...request(), ...change }, env)).toMatchObject({
      ok: false,
      error: { code: "invalid" },
    });
  });

  it("accepts the maximum blob size and maps an oversize request to too_large", async () => {
    expect((await blobPutUrl(request("acme", 2 * 1024 ** 3), env)).ok).toBe(
      true,
    );
    expect(
      await blobPutUrl(request("acme", 2 * 1024 ** 3 + 1), env),
    ).toMatchObject({
      ok: false,
      error: { code: "too_large" },
    });
  });

  it("fails closed without signing credentials or with unsafe configuration", async () => {
    const unconfigured = { ...env };
    delete unconfigured.R2_ACCESS_KEY_ID;
    delete unconfigured.R2_SECRET_ACCESS_KEY;
    await expect(blobPutUrl(request(), unconfigured)).rejects.toThrow(
      "Blob signing is unavailable",
    );
    for (const configuration of [
      { R2_ACCESS_KEY_ID: "" },
      { R2_SECRET_ACCESS_KEY: " " },
      { R2_ACCOUNT_ID: "other.example/escape" },
      { R2_BUCKET_NAME: "other/bucket" },
      { R2_BUCKET_NAME: ".." },
    ]) {
      await expect(
        blobPutUrl(request(), { ...env, ...configuration }),
      ).rejects.toThrow("Blob signing is unavailable");
    }
  });
});
