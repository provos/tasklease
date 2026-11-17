import { createHash } from "node:crypto";
import { readFile, readdir, realpath } from "node:fs/promises";
import { resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";

const WEEK = 7 * 24 * 60 * 60 * 1000;
const vendorPath = (name) => `scripts/dependency-policy-vendor/${name}`;
const vendorNames = ["deepmerge", "picomatch"];
const LOCAL_PACKAGES = new Map([
  ["service", "@tasklease/service"],
  ["monitor", "@tasklease/monitor"],
  ...vendorNames.map((name) => [vendorPath(name), `@tasklease/${name}`]),
]);
const LOCAL_LINKS = new Map([
  ["node_modules/@tasklease/service", "service"],
  ["node_modules/@tasklease/monitor", "monitor"],
  ...vendorNames.map((name) => [`node_modules/${name}`, vendorPath(name)]),
]);
// This is reviewed first-party source, based on an aged upstream release.
// Updating these hashes requires reviewing the backport and its regression tests.
const VENDOR_HASHES = {
  deepmerge: {
    "index.cjs":
      "be8e1b8bf1e6b11e31ac41782c07fc54a5569706096ad86a2714ab9c11a5317e",
    "index.d.ts":
      "146ba4b99c5feb82663e17a671bf9f53bb39c704cd76345d6c5a801c26372f44",
    LICENSE: "6cfc4687cb2f2d86f4a77e6b526290d3878e5e512f3fec2f4cb36a9cb36f798b",
    "package.json":
      "f5e0272c5f3c402259a249db9d30e42fb986fedaea04fbb7590ec6abbf1983bc",
  },
  picomatch: {
    LICENSE: "d0cd141b0c322fded5dfad1d4645bb2fedfc05b7321fe1009469638190d59ef9",
    "index.js":
      "02ae26406508e28f9ba0865be16cf77f0b33de6c4d4d49c212c4176df1d5625a",
    "lib/constants.js":
      "c28f952d2758261fe5428a121add485cc25b03e0193633f649de903dc0e5e222",
    "lib/parse.js":
      "16fbd61a0ad9351911f41ff466f47e982617c906cd48b3139ba4782fcb4fb4de",
    "lib/picomatch.js":
      "fdd249d7ffff853f3bb1c77eafc8baa1a437bea85a94c35877b83c6a40d9ca94",
    "lib/scan.js":
      "6457a1764e5c2e543cc3a0c00ce12ebb3764097628868198370b64d4e642aaae",
    "lib/utils.js":
      "f04604f70cd1a3f6ace80b84766468674ff38b4bb83818d2ef1d81fc6355f935",
    "package.json":
      "6061a4e721bfc82a2f68a16025990139f2dc6b6abd6463b78e548666e50c9f65",
    "posix.js":
      "5695ff5dfcd5a338a40630bd506056f2950bcb08fe1cc068519cbbfe60add480",
  },
};

async function json(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

function packageName(path) {
  const namePattern = "(?:@[a-z0-9._-]+/)?[a-z0-9._-]+";
  if (
    !new RegExp(
      `^(?:(?:service|monitor)/)?node_modules/${namePattern}(?:/node_modules/${namePattern})*$`,
      "i",
    ).test(path) ||
    path.split("/").some((part) => part === "." || part === "..")
  )
    throw new Error(`Invalid locked package path: ${path}`);
  return path.split("node_modules/").at(-1);
}

export function policyCutoff(now = new Date(), requested) {
  const maximum = new Date(now).getTime() - WEEK;
  if (
    requested !== undefined &&
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(requested)
  )
    throw new Error("Cutoff must be an ISO UTC date");
  const cutoff = requested === undefined ? maximum : Date.parse(requested);
  if (!Number.isFinite(maximum) || !Number.isFinite(cutoff) || cutoff > maximum)
    throw new Error(
      "Cutoff must be an ISO date at least seven days before now",
    );
  return cutoff;
}

async function verifyVendor(root, name) {
  for (const [file, expected] of Object.entries(VENDOR_HASHES[name])) {
    const bytes = await readFile(resolve(root, vendorPath(name), file));
    const actual = createHash("sha256").update(bytes).digest("hex");
    if (actual !== expected)
      throw new Error(`Unreviewed vendor source: ${name}/${file}`);
  }
}

function samePackage(actual, expected) {
  return ["version", "resolved", "integrity", "link"].every(
    (field) => actual[field] === expected[field],
  );
}

function dependencySpecs(manifest) {
  return Object.fromEntries(
    ["dependencies", "devDependencies", "optionalDependencies"].map((field) => [
      field,
      Object.entries(manifest[field] ?? {}).sort(([a], [b]) =>
        a.localeCompare(b),
      ),
    ]),
  );
}

async function verifyInstalled(root, packages) {
  const hidden = await json(resolve(root, "node_modules/.package-lock.json"));
  if (hidden.lockfileVersion !== 3 || !hidden.packages)
    throw new Error("Missing npm v3 installed lock; run npm ci");
  for (const [path, entry] of Object.entries(hidden.packages)) {
    if (!packages[path] || !samePackage(entry, packages[path]))
      throw new Error(`Installed lock differs from package-lock.json: ${path}`);
  }
  const found = new Set();
  async function scan(directory) {
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch (error) {
      if (error.code === "ENOENT") return;
      throw error;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".")) continue;
      const path = resolve(directory, entry.name);
      if (entry.name.startsWith("@")) {
        await scan(path);
        continue;
      }
      const key = relative(root, path).replaceAll("\\", "/");
      const locked = packages[key];
      if (!locked) throw new Error(`Unlisted installed package: ${key}`);
      found.add(key);
      const installed = await json(resolve(path, "package.json"));
      const target = locked.link ? packages[locked.resolved] : locked;
      const name = locked.link
        ? LOCAL_PACKAGES.get(locked.resolved)
        : (locked.name ?? packageName(key));
      if (installed.name !== name || installed.version !== target.version)
        throw new Error(
          `Installed package differs from package-lock.json: ${key}`,
        );
      if (locked.link) {
        if (
          (await realpath(path)) !==
          (await realpath(resolve(root, locked.resolved)))
        )
          throw new Error(`Unexpected installed link: ${key}`);
      } else {
        if (entry.isSymbolicLink())
          throw new Error(`Unexpected installed link: ${key}`);
        if (!hidden.packages[key] || !samePackage(hidden.packages[key], locked))
          throw new Error(`Package missing from installed lock: ${key}`);
        await scan(resolve(path, "node_modules"));
      }
    }
  }
  await scan(resolve(root, "node_modules"));
  // Workspaces may contain nested node_modules directories of their own.
  for (const workspace of ["service", "monitor"])
    await scan(resolve(root, workspace, "node_modules"));
  for (const [path, entry] of Object.entries(packages)) {
    if (path.includes("node_modules/") && !entry.optional && !found.has(path))
      throw new Error(`Required installed package missing: ${path}`);
  }
}

async function registryMetadata(name, fetchImpl) {
  const response = await fetchImpl(
    `https://registry.npmjs.org/${encodeURIComponent(name)}`,
    { signal: AbortSignal.timeout(30_000) },
  );
  if (!response.ok)
    throw new Error(`Registry metadata failed for ${name}: ${response.status}`);
  const metadata = await response.json();
  if (!metadata.time) throw new Error(`Publication times missing for ${name}`);
  return metadata;
}

export async function checkDependencies({
  root = fileURLToPath(new URL("../", import.meta.url)),
  installed = false,
  now = new Date(),
  cutoff,
  fetchImpl = fetch,
} = {}) {
  const threshold = policyCutoff(now, cutoff);
  const lock = await json(resolve(root, "package-lock.json"));
  if (lock.lockfileVersion !== 3 || !lock.packages?.[""])
    throw new Error("Expected npm v3 package-lock.json");
  const manifest = await json(resolve(root, "package.json"));
  if (
    JSON.stringify(dependencySpecs(manifest)) !==
    JSON.stringify(dependencySpecs(lock.packages[""]))
  )
    throw new Error(
      "Root dependency specifications differ from package-lock.json",
    );
  for (const name of vendorNames) {
    const path = vendorPath(name);
    const link = lock.packages[`node_modules/${name}`];
    if (
      manifest.devDependencies?.[name] !== `file:${path}` ||
      manifest.overrides?.[name] !== `$${name}` ||
      link?.resolved !== path ||
      !link.link ||
      !lock.packages[path]
    ) {
      throw new Error(
        `Reviewed ${name} security backport must replace the upstream package`,
      );
    }
    await verifyVendor(root, name);
  }
  const registry = new Map();
  for (const [path, entry] of Object.entries(lock.packages)) {
    if (!path) continue;
    if (LOCAL_PACKAGES.has(path)) {
      const manifest = await json(resolve(root, path, "package.json"));
      if (
        manifest.name !== LOCAL_PACKAGES.get(path) ||
        manifest.version !== entry.version ||
        JSON.stringify(dependencySpecs(manifest)) !==
          JSON.stringify(dependencySpecs(entry))
      )
        throw new Error(`Local package differs from lock: ${path}`);
      continue;
    }
    if (entry.link) {
      if (
        LOCAL_LINKS.get(path) !== entry.resolved ||
        !lock.packages[entry.resolved]
      )
        throw new Error(`Unapproved local dependency: ${path}`);
      continue;
    }
    const name = packageName(path);
    if (entry.name && entry.name !== name)
      throw new Error(`Unapproved package alias: ${path}`);
    if (vendorNames.includes(name))
      throw new Error(`Unpatched upstream ${name}: ${path}`);
    if (
      !entry.version ||
      !entry.integrity?.startsWith("sha512-") ||
      !entry.resolved?.startsWith("https://registry.npmjs.org/")
    )
      throw new Error(`Unapproved registry dependency: ${path}`);
    if (!registry.has(name)) registry.set(name, new Map());
    const previous = registry.get(name).get(entry.version);
    if (previous && !samePackage(previous, entry))
      throw new Error(`Conflicting registry sources: ${name}@${entry.version}`);
    registry.get(name).set(entry.version, entry);
  }
  const jobs = [...registry];
  const violations = [];
  let index = 0;
  await Promise.all(
    Array.from({ length: Math.min(8, jobs.length) }, async () => {
      while (index < jobs.length) {
        const [name, versions] = jobs[index++];
        const metadata = await registryMetadata(name, fetchImpl);
        const times = metadata.time;
        for (const [version, entry] of versions) {
          const dist = metadata.versions?.[version]?.dist;
          if (
            dist?.tarball !== entry.resolved ||
            dist?.integrity !== entry.integrity
          )
            throw new Error(
              `Registry source differs from lock: ${name}@${version}`,
            );
          const published = Date.parse(times[version]);
          if (!Number.isFinite(published) || published > threshold)
            violations.push(
              `${name}@${version} (${times[version] ?? "unknown publication date"})`,
            );
        }
      }
    }),
  );
  if (violations.length)
    throw new Error(
      `Dependencies newer than cutoff ${new Date(threshold).toISOString()}:\n${violations.sort().join("\n")}`,
    );
  if (installed) await verifyInstalled(root, lock.packages);
  return {
    packages: [...registry.values()].reduce(
      (sum, versions) => sum + versions.size,
      0,
    ),
    cutoff: new Date(threshold).toISOString(),
    installed,
  };
}

async function main() {
  const args = process.argv.slice(2);
  if (args.some((arg) => arg !== "--installed" && !arg.startsWith("--cutoff=")))
    throw new Error(
      "Usage: node scripts/dependency-policy.mjs [--installed] [--cutoff=ISO]",
    );
  const result = await checkDependencies({
    installed: args.includes("--installed"),
    cutoff: args.find((arg) => arg.startsWith("--cutoff="))?.slice(9),
  });
  console.log(
    `Dependency policy passed: ${result.packages} registry versions, cutoff ${result.cutoff}${result.installed ? ", installed tree matches" : ""}`,
  );
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  main().catch((error) => {
    console.error(`Dependency policy failed: ${error.message}`);
    process.exitCode = 1;
  });
