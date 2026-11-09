import { AwsClient } from "aws4fetch";
import { failure } from "./errors";
import type {
  BlobGetUrlRequest,
  BlobGetUrlResponse,
  BlobPutUrlRequest,
  BlobPutUrlResponse,
} from "./generated/protocol";
import {
  validateBlobGetUrlRequest,
  validateBlobPutUrlRequest,
} from "./generated/validators.js";
import { newUlid } from "./ids";
import type { Result } from "./types";

type BlobEnv = Pick<
  Env,
  "BLOBS" | "R2_ACCESS_KEY_ID" | "R2_SECRET_ACCESS_KEY"
> & {
  R2_ACCOUNT_ID: string;
  R2_BUCKET_NAME: string;
};

export const MAX_BLOB_SIZE = 2 * 1024 ** 3;
const URL_LIFETIME_SECONDS = 3600;
const KEY_SUFFIX =
  /^(\d{4}-\d{2}-\d{2})\/[0-7][0-9A-HJKMNP-TV-Z]{25}\/([A-Za-z0-9._-]{1,128})(?![\s\S])/;

function domainPrefix(domain: string): string {
  return domain.replace(/\//g, "%2F");
}

function validKey(domain: string, key: string): boolean {
  const prefix = `${domainPrefix(domain)}/`;
  if (!key.startsWith(prefix)) return false;
  const match = KEY_SUFFIX.exec(key.slice(prefix.length));
  if (!match || match[2] === "." || match[2] === "..") return false;
  const day = match[1];
  const date = new Date(`${day}T00:00:00.000Z`);
  return (
    Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === day
  );
}

async function signedUrl(
  key: string,
  method: "GET" | "PUT",
  env: BlobEnv,
  now: number,
  size?: number,
): Promise<{ url: string; expires_at: number }> {
  const accessKeyId = env.R2_ACCESS_KEY_ID;
  const secretAccessKey = env.R2_SECRET_ACCESS_KEY;
  if (
    !accessKeyId?.trim() ||
    !secretAccessKey?.trim() ||
    !/^[a-z0-9-]{1,63}(?![\s\S])/.test(env.R2_ACCOUNT_ID) ||
    !/^[A-Za-z0-9._-]{1,63}(?![\s\S])/.test(env.R2_BUCKET_NAME)
  ) {
    throw new Error("Blob signing is unavailable");
  }

  // The key contains literal %2F in its domain segment. Encoding each segment
  // again preserves those bytes as %252F on the S3 wire path.
  const path = `/${[env.R2_BUCKET_NAME, ...key.split("/")]
    .map(encodeURIComponent)
    .join("/")}`;
  const url = new URL(
    `https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com${path}`,
  );
  if (url.pathname !== path) throw new Error("Blob signing is unavailable");
  url.searchParams.set("X-Amz-Expires", String(URL_LIFETIME_SECONDS));

  const signer = new AwsClient({
    accessKeyId,
    secretAccessKey,
    service: "s3",
    region: "auto",
  });
  const signed = await signer.sign(url, {
    method,
    ...(size === undefined
      ? {}
      : { headers: { "Content-Length": String(size) } }),
    aws: {
      signQuery: true,
      // aws4fetch excludes Content-Length from signatures by default.
      allHeaders: true,
      datetime: new Date(now).toISOString().replace(/[:-]|\.\d{3}/g, ""),
    },
  });
  return {
    url: signed.url,
    expires_at: Math.floor(now / 1000) * 1000 + URL_LIFETIME_SECONDS * 1000,
  };
}

/** The router authenticates the identity and authorizes its domain and scope. */
export async function blobPutUrl(
  request: BlobPutUrlRequest,
  env: BlobEnv,
): Promise<Result<BlobPutUrlResponse>> {
  if (request.size > MAX_BLOB_SIZE)
    return failure("too_large", "Blob exceeds 2 GiB");
  if (!validateBlobPutUrlRequest(request))
    return failure("invalid", "Invalid blob request");
  const now = Date.now();
  const day = new Date(now).toISOString().slice(0, 10);
  const key = `${domainPrefix(request.domain)}/${day}/${newUlid(now)}/${request.name}`;
  return {
    ok: true,
    key,
    ...(await signedUrl(key, "PUT", env, now, request.size)),
  };
}

export async function blobGetUrl(
  request: BlobGetUrlRequest,
  env: BlobEnv,
): Promise<Result<BlobGetUrlResponse>> {
  if (
    !validateBlobGetUrlRequest(request) ||
    !validKey(request.domain, request.key)
  ) {
    return failure(
      "invalid",
      "Blob key does not belong to the domain or has invalid shape",
    );
  }
  if (!(await env.BLOBS.head(request.key)))
    return failure("not_found", "Blob not found");
  return {
    ok: true,
    ...(await signedUrl(request.key, "GET", env, Date.now())),
  };
}
