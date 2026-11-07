import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, resolve } from "node:path";
import { spawn } from "node:child_process";
import { root, workerConfig } from "./worker-config.mjs";

const temporary = mkdtempSync(resolve(tmpdir(), "tasklease-worker-"));
// Keep the config beside the source config, so Wrangler finds local .dev.vars.
const config = resolve(root, "service", `.${basename(temporary)}.json`);
writeFileSync(config, JSON.stringify(workerConfig()));
const args = process.argv.slice(2);
const child = spawn(
  process.execPath,
  [
    resolve(root, "node_modules/wrangler/bin/wrangler.js"),
    ...args,
    "--config",
    config,
  ],
  {
    cwd: resolve(root, "service"),
    stdio: "inherit",
  },
);
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () => child.kill(signal));
function cleanup() {
  rmSync(config, { force: true });
  rmSync(temporary, { recursive: true, force: true });
}
child.on("error", (error) => {
  cleanup();
  console.error(error.message);
  process.exitCode = 1;
});
child.on("exit", (code) => {
  cleanup();
  process.exitCode = code ?? 1;
});
