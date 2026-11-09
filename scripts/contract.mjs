import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

export const root = fileURLToPath(new URL("../", import.meta.url));
export const schemaBase = "https://tasklease.dev/schema/";
export const pascal = (name) =>
  name
    .split(/[._]/)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join("");

export async function readContract() {
  const common = JSON.parse(
    await readFile(path.join(root, "schema/common.json"), "utf8"),
  );
  const operations = {};
  for (const name of (await readdir(path.join(root, "schema/ops"))).sort()) {
    if (!name.endsWith(".json")) continue;
    operations[name.slice(0, -5)] = JSON.parse(
      await readFile(path.join(root, "schema/ops", name), "utf8"),
    );
  }
  return { common, operations };
}

// Resolve only the repository's schema references. Both generators receive
// the same self-contained graph, with no network resolution or cwd coupling.
export function bundleContract({ common, operations }) {
  const definitions = { ...common.$defs };
  for (const [operation, schema] of Object.entries(operations)) {
    for (const kind of ["Request", "Response"]) {
      definitions[pascal(operation) + kind] = schema.$defs[kind];
    }
  }
  const reference = (name) => ({ $ref: `#/$defs/${name}` });
  for (const kind of ["Request", "Response"]) {
    definitions[`Operation${kind}s`] = {
      title: `Operation${kind}s`,
      type: "object",
      properties: Object.fromEntries(
        Object.keys(operations)
          .filter((operation) => operation !== "version")
          .map((operation) => [operation, reference(pascal(operation) + kind)]),
      ),
      required: Object.keys(operations).filter(
        (operation) => operation !== "version",
      ),
      additionalProperties: false,
    };
  }
  const flatten = (value) => {
    if (Array.isArray(value)) return value.map(flatten);
    if (value === null || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        key === "$ref" && item.startsWith(`${schemaBase}common.json#/$defs/`)
          ? item.replace(`${schemaBase}common.json#/$defs/`, "#/$defs/")
          : flatten(item),
      ]),
    );
  };
  return flatten({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: `${schemaBase}protocol.json`,
    title: "ProtocolTypes",
    type: "object",
    properties: Object.fromEntries(
      Object.keys(definitions).map((name) => [name, reference(name)]),
    ),
    additionalProperties: false,
    $defs: definitions,
  });
}

export async function readExamples(operations) {
  const examples = [];
  for (const operation of Object.keys(operations)) {
    for (const validity of ["valid", "invalid"]) {
      const directory = path.join(root, "schema/examples", operation, validity);
      for (const name of (await readdir(directory)).sort()) {
        if (!name.endsWith(".json")) continue;
        const file = path.join(directory, name);
        const example = JSON.parse(await readFile(file, "utf8"));
        if (!["request", "response", "error"].includes(example.kind)) {
          throw new Error(`${file}: unexpected fixture kind`);
        }
        examples.push({
          operation,
          valid: validity === "valid",
          file,
          ...example,
        });
      }
    }
  }
  return examples;
}
