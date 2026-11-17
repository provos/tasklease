import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  cp,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { checkDependencies, policyCutoff } from "./dependency-policy.mjs";

const require = createRequire(import.meta.url);
const merge = require("./dependency-policy-vendor/deepmerge/index.cjs");
const sourceRoot = fileURLToPath(new URL("../", import.meta.url));
const vendor = "scripts/dependency-policy-vendor/deepmerge";
const now = new Date("2026-11-08T03:00:00Z");
const aged = "2026-10-01T00:00:00Z";

async function save(root, path, value) {
  const target = resolve(root, path);
  await mkdir(resolve(target, ".."), { recursive: true });
  await writeFile(target, `${JSON.stringify(value, null, 2)}\n`);
}

async function fixture(t) {
  const root = await mkdtemp(resolve(tmpdir(), "tasklease-policy-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const manifest = {
    devDependencies: { example: "1.0.0" },
    overrides: {},
  };
  const lock = {
    lockfileVersion: 3,
    packages: {
      "": manifest,
      "node_modules/example": {
        version: "1.0.0",
        resolved: "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
        integrity: "sha512-example",
      },
    },
  };
  for (const name of ["deepmerge", "picomatch"]) {
    const path = `scripts/dependency-policy-vendor/${name}`;
    await cp(resolve(sourceRoot, path), resolve(root, path), {
      recursive: true,
    });
    const fork = JSON.parse(
      await readFile(resolve(root, path, "package.json"), "utf8"),
    );
    manifest.devDependencies[name] = `file:${path}`;
    manifest.overrides[name] = `$${name}`;
    lock.packages[path] = { name: fork.name, version: fork.version };
    lock.packages[`node_modules/${name}`] = { resolved: path, link: true };
  }
  await save(root, "package.json", manifest);
  await save(root, "package-lock.json", lock);
  const calls = [];
  const metadata = (name, published = aged) => ({
    time: { "1.0.0": published },
    versions: {
      "1.0.0": {
        dist: {
          tarball: `https://registry.npmjs.org/${name}/-/${name}-1.0.0.tgz`,
          integrity: "sha512-example",
        },
      },
    },
  });
  const options = {
    root,
    now,
    fetchImpl: async (url) => {
      const name = decodeURIComponent(url.split("/").at(-1));
      calls.push(name);
      return { ok: true, json: async () => metadata(name) };
    },
  };
  return { root, lock, manifest, options, calls, metadata };
}

async function installFixture(fixture) {
  const { root, lock } = fixture;
  await mkdir(resolve(root, "node_modules"));
  for (const name of ["deepmerge", "picomatch"]) {
    await symlink(
      resolve(root, lock.packages[`node_modules/${name}`].resolved),
      resolve(root, `node_modules/${name}`),
      "junction",
    );
  }
  await save(root, "node_modules/example/package.json", {
    name: "example",
    version: "1.0.0",
  });
  await save(root, "node_modules/.package-lock.json", {
    lockfileVersion: 3,
    packages: Object.fromEntries(
      Object.entries(lock.packages).filter(([path]) => path),
    ),
  });
}

test("cutoff enforces a full seven days and permits only earlier UTC dates", () => {
  assert.equal(policyCutoff(now), Date.parse("2026-11-01T03:00:00Z"));
  assert.equal(policyCutoff(now, "2026-10-01T00:00:00Z"), Date.parse(aged));
  assert.throws(() => policyCutoff(now, "2026-11-02T00:00:00Z"), /seven days/);
  assert.throws(() => policyCutoff(now, "yesterday"), /ISO UTC/);
  assert.throws(() => policyCutoff(now, "2026-11-01"), /ISO UTC/);
});

test("default checks the entire lock without node_modules", async (t) => {
  const f = await fixture(t);
  const result = await checkDependencies(f.options);
  assert.equal(result.packages, 1);
  assert.deepEqual(f.calls, ["example"]);
});

test("rejects young optional packages for other platforms and unknown dates", async (t) => {
  const f = await fixture(t);
  f.lock.packages["node_modules/example"].optional = true;
  f.lock.packages["node_modules/example"].os = ["win32"];
  await save(f.root, "package-lock.json", f.lock);
  for (const published of ["2026-11-07T00:00:00Z", undefined]) {
    await assert.rejects(
      checkDependencies({
        ...f.options,
        fetchImpl: async () => ({
          ok: true,
          json: async () => ({
            ...f.metadata("example"),
            time: { "1.0.0": published },
          }),
        }),
      }),
      /example@1.0.0/,
    );
  }
});

test("fails closed on registry errors", async (t) => {
  const f = await fixture(t);
  await assert.rejects(
    checkDependencies({
      ...f.options,
      fetchImpl: async () => ({ ok: false, status: 503 }),
    }),
    /Registry metadata failed/,
  );
});

test("binds publication dates to the exact locked registry artifact", async (t) => {
  const f = await fixture(t);
  f.lock.packages["node_modules/example"].integrity = "sha512-other";
  await save(f.root, "package-lock.json", f.lock);
  await assert.rejects(checkDependencies(f.options), /Registry source differs/);
});

test("rejects arbitrary file dependencies and changed vendor source", async (t) => {
  const f = await fixture(t);
  f.lock.packages["node_modules/example"].resolved = "file:unreviewed";
  await save(f.root, "package-lock.json", f.lock);
  await assert.rejects(checkDependencies(f.options), /Unapproved registry/);
  delete f.lock.packages["node_modules/example"];
  await save(f.root, "package-lock.json", f.lock);
  await writeFile(resolve(f.root, vendor, "index.cjs"), "changed");
  await assert.rejects(checkDependencies(f.options), /Unreviewed vendor/);
});

test("requires the reviewed fork and matching dependency manifests", async (t) => {
  const f = await fixture(t);
  f.manifest.devDependencies.example = "2.0.0";
  await save(f.root, "package.json", f.manifest);
  await assert.rejects(
    checkDependencies(f.options),
    /Root dependency specifications/,
  );
  f.lock.packages[""] = f.manifest;
  f.lock.packages["node_modules/deepmerge"] = { version: "4.3.1" };
  await save(f.root, "package-lock.json", f.lock);
  await assert.rejects(checkDependencies(f.options), /security backport/);
});

test("installed mode checks hidden lock and actual versions", async (t) => {
  const f = await fixture(t);
  await installFixture(f);
  assert.equal(
    (await checkDependencies({ ...f.options, installed: true })).installed,
    true,
  );
  await save(f.root, "node_modules/example/package.json", {
    name: "example",
    version: "2.0.0",
  });
  await assert.rejects(
    checkDependencies({ ...f.options, installed: true }),
    /Installed package differs/,
  );
  await save(f.root, "node_modules/example/package.json", {
    name: "example",
    version: "1.0.0",
  });
  const hidden = structuredClone(f.lock);
  delete hidden.packages[""];
  hidden.packages["node_modules/example"].version = "2.0.0";
  await save(f.root, "node_modules/.package-lock.json", hidden);
  await assert.rejects(
    checkDependencies({ ...f.options, installed: true }),
    /Installed lock differs/,
  );
});

test("installed mode permits absent optional packages but rejects stale extras", async (t) => {
  const f = await fixture(t);
  f.lock.packages["node_modules/other-platform"] = {
    ...f.lock.packages["node_modules/example"],
    resolved:
      "https://registry.npmjs.org/other-platform/-/other-platform-1.0.0.tgz",
    optional: true,
    os: ["win32"],
  };
  await save(f.root, "package-lock.json", f.lock);
  await installFixture(f);
  await checkDependencies({ ...f.options, installed: true });
  await save(f.root, "node_modules/stale/package.json", {
    name: "stale",
    version: "1.0.0",
  });
  await assert.rejects(
    checkDependencies({ ...f.options, installed: true }),
    /Unlisted installed package/,
  );
});

test("installed mode rejects missing required packages", async (t) => {
  const f = await fixture(t);
  await installFixture(f);
  await rm(resolve(f.root, "node_modules/example"), { recursive: true });
  await assert.rejects(
    checkDependencies({ ...f.options, installed: true }),
    /Required installed package missing/,
  );
});

test("deepmerge backport prevents returned-object prototype poisoning", () => {
  const target = JSON.parse(
    '{"__proto__":{"poisoned":true},"nested":{"__proto__":{"nestedPoison":true}}}',
  );
  const result = merge(target, { nested: { safe: true } });
  assert.equal(Object.getPrototypeOf(result), Object.prototype);
  assert.equal(Object.getPrototypeOf(result.nested), Object.prototype);
  assert.equal(result.poisoned, undefined);
  assert.equal(result.nested.nestedPoison, undefined);
  assert.deepEqual(Object.getOwnPropertyDescriptor(result, "__proto__"), {
    value: { poisoned: true },
    enumerable: true,
    configurable: true,
    writable: true,
  });
  assert.equal(result.nested.safe, true);
  assert.equal({}.poisoned, undefined);
});

test("deepmerge retains source guards, arrays, symbols and custom merge options", () => {
  const symbol = Symbol("configuration");
  const target = { list: [{ a: 1 }], nested: { a: 1 }, [symbol]: { a: 1 } };
  const source = { list: [{ b: 2 }], nested: { b: 2 }, [symbol]: { b: 2 } };
  const result = merge(target, source);
  assert.deepEqual(result.list, [{ a: 1 }, { b: 2 }]);
  assert.notEqual(result.list[0], target.list[0]);
  assert.deepEqual(result[symbol], { a: 1, b: 2 });
  assert.deepEqual(result.nested, { a: 1, b: 2 });
  const poisoned = merge(
    {},
    JSON.parse(
      '{"__proto__":{"poison":true},"constructor":{"prototype":{"poison":true}}}',
    ),
  );
  assert.equal(Object.hasOwn(poisoned, "__proto__"), false);
  assert.equal(Object.hasOwn(poisoned, "constructor"), false);
  assert.equal({}.poison, undefined);
  assert.deepEqual(
    merge(target, source, {
      customMerge: (key) => (key === "list" ? (_a, b) => b : undefined),
    }).list,
    source.list,
  );
  assert.deepEqual(merge.all([{ a: 1 }, { b: 2 }]), { a: 1, b: 2 });
});

test("minimatch override preserves the typescript-estree matching API", () => {
  const estreeRequire = createRequire(
    require.resolve("@typescript-eslint/typescript-estree/package.json"),
  );
  const { minimatch } = estreeRequire("minimatch");
  assert.equal(estreeRequire("minimatch/package.json").version, "11.0.3");
  assert.equal(minimatch("src/.hidden.ts", "src/**/*.ts", { dot: true }), true);
  assert.equal(minimatch("src/index.js", "src/**/*.ts", { dot: true }), false);
  assert.equal(
    minimatch("src/index.ts", "{src,test}/**/*.ts", { dot: true }),
    true,
  );
});

test("rejects an additional registry picomatch and modified backport files", async (t) => {
  const f = await fixture(t);
  const nested = "node_modules/example/node_modules/picomatch";
  f.lock.packages[nested] = { version: "4.0.7" };
  await save(f.root, "package-lock.json", f.lock);
  await assert.rejects(
    checkDependencies(f.options),
    /Unpatched upstream picomatch/,
  );
  delete f.lock.packages[nested];
  await save(f.root, "package-lock.json", f.lock);
  await writeFile(
    resolve(f.root, "scripts/dependency-policy-vendor/picomatch/lib/parse.js"),
    "modified",
  );
  await assert.rejects(
    checkDependencies(f.options),
    /Unreviewed vendor source: picomatch/,
  );
});

test("glob consumers use the patched matcher and retain normal file matching", () => {
  for (const parent of ["vite", "vitest", "tinyglobby", "fdir"]) {
    const consumer = createRequire(require.resolve(parent));
    assert.equal(
      consumer("picomatch/package.json").name,
      "@tasklease/picomatch",
    );
    for (const match of [consumer("picomatch"), consumer("picomatch/posix")]) {
      assert.equal(match("src/**/*.{js,ts}")("src/client/api.ts"), true);
      assert.equal(match("src/+(api|web).ts")("src/apiweb.ts"), true);
      assert.equal(match("src/+(api|web).ts")("src/app.ts"), false);
    }
  }
});

test("repeated single-wildcard branches compile without an outer unbounded repeat", () => {
  const match = require("picomatch");
  for (const options of [
    {},
    { capture: true },
    { windows: true },
    { maxExtglobRecursion: false },
  ]) {
    for (const operator of ["+", "*"]) {
      const pattern = `images/${operator}(frame*.png)/end`;
      const re = match.makeRe(pattern, options);
      assert.doesNotMatch(re.source, /\)[+*]/);
      assert.equal(re.test("images/frame1.pngframe2.png/end"), true);
      assert.equal(re.test("images/frame1.jpg/end"), false);
      assert.equal(re.test("images/frame1/2.png/end"), false);
      assert.equal(re.test("images//end"), operator === "*");
    }
  }
  // A complex body takes the parser's literal fallback; literal star classes
  // still take the ordinary character-class path.
  assert.equal(match("images/+(part*/end)")("images/part2/end"), false);
  assert.equal(match("images/+([*])")("images/**"), true);
});
