import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";

export const root = resolve(import.meta.dirname, "..");

export function serviceVersion() {
  const project = readFileSync(resolve(root, "python/pyproject.toml"), "utf8");
  const section = project.match(
    /^\[project\]\s*\n([\s\S]*?)(?=^\[|$(?![\s\S]))/m,
  );
  const version = section?.[1].match(
    /^version\s*=\s*"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"\s*$/m,
  )?.[1];
  if (!version)
    throw new Error(
      "pyproject.toml must define a MAJOR.MINOR.PATCH project version",
    );
  return version;
}

export function workerConfig() {
  const filename = resolve(root, "service/wrangler.jsonc");
  const parsed = ts.parseConfigFileTextToJson(
    filename,
    readFileSync(filename, "utf8"),
  );
  if (parsed.error) throw new Error("Cannot parse service/wrangler.jsonc");
  return {
    ...parsed.config,
    main: resolve(root, "service/src/index.ts"),
    define: { TASKLEASE_VERSION: JSON.stringify(serviceVersion()) },
  };
}
