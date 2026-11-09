import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const root = resolve(import.meta.dirname, "..");

const output = resolve(
  process.argv[2] ?? resolve(root, "python/src/tasklease"),
);
const temporary = mkdtempSync(resolve(tmpdir(), "tasklease-assets-"));
const environment = {
  ...process.env,
  WRANGLER_SEND_METRICS: "false",
  CI: "true",
};

function run(script, args, cwd) {
  const result = spawnSync(process.execPath, [resolve(root, script), ...args], {
    cwd,
    env: environment,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0)
    throw new Error(`Asset build failed: ${script} (${result.status})`);
}

try {
  // Check the entire lock and installed graph before executing build tools.
  run("scripts/dependency-policy.mjs", ["--installed"], root);
  const { serviceVersion, workerConfig } = await import("./worker-config.mjs");
  const dashboard = resolve(temporary, "static");
  const bundle = resolve(temporary, "service_bundle");
  const config = resolve(temporary, "wrangler.json");
  const metafile = resolve(temporary, "metafile.json");
  writeFileSync(config, JSON.stringify(workerConfig()));
  run(
    "node_modules/vite/bin/vite.js",
    ["build", "--outDir", dashboard],
    resolve(root, "monitor"),
  );
  run(
    "node_modules/wrangler/bin/wrangler.js",
    [
      "deploy",
      "--dry-run",
      "--config",
      config,
      "--outdir",
      bundle,
      "--metafile",
      metafile,
    ],
    resolve(root, "service"),
  );
  const metadata = JSON.parse(readFileSync(metafile, "utf8"));
  const entrypoints = Object.entries(metadata.outputs).filter(
    ([path, details]) => details.entryPoint && path.endsWith(".js"),
  );
  if (entrypoints.length !== 1)
    throw new Error(
      "Expected exactly one bundled Worker JavaScript entrypoint",
    );
  const main = basename(entrypoints[0][0]);
  const template = workerConfig();
  delete template.$schema;
  delete template.tsconfig;
  delete template.define;
  template.main = main;
  template.no_bundle = true;
  template.name = "__WORKER_NAME__";
  template.account_id = "__ACCOUNT_ID__";
  template.vars.R2_ACCOUNT_ID = "__ACCOUNT_ID__";
  template.vars.R2_BUCKET_NAME = "__BUCKET_NAME__";
  for (const bucket of template.r2_buckets)
    bucket.bucket_name = "__BUCKET_NAME__";
  writeFileSync(
    resolve(bundle, "wrangler.template.jsonc"),
    `${JSON.stringify(template, null, 2)}\n`,
  );
  const wrangler = JSON.parse(
    readFileSync(resolve(root, "node_modules/wrangler/package.json"), "utf8"),
  );
  writeFileSync(
    resolve(bundle, "manifest.json"),
    `${JSON.stringify({ service_version: serviceVersion(), wrangler_version: wrangler.version }, null, 2)}\n`,
  );
  for (const [source, destination] of [
    [dashboard, resolve(output, "monitor/static")],
    [bundle, resolve(output, "service_bundle")],
  ]) {
    rmSync(destination, { recursive: true, force: true });
    mkdirSync(destination, { recursive: true });
    cpSync(source, destination, { recursive: true });
  }
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
