import Ajv2020 from "ajv/dist/2020.js";
import * as validators from "../service/src/generated/validators.js";
import { pascal, readContract, readExamples } from "./contract.mjs";

const contract = await readContract();
const ajv = new Ajv2020({
  schemas: [contract.common, ...Object.values(contract.operations)],
  allErrors: true,
  strict: true,
});
const failures = [];
const examples = await readExamples(contract.operations);
for (const example of examples) {
  const kind = example.kind[0].toUpperCase() + example.kind.slice(1);
  const name =
    kind === "Error" ? "ErrorResponse" : pascal(example.operation) + kind;
  const uri =
    kind === "Error"
      ? `${contract.common.$id}#/$defs/ErrorResponse`
      : `${contract.operations[example.operation].$id}#/$defs/${kind}`;
  for (const [source, validate] of [
    ["schema", ajv.getSchema(uri)],
    ["standalone", validators[`validate${name}`]],
  ]) {
    if (validate(example.body) !== example.valid) {
      failures.push(
        `${example.file} (${source}): ${JSON.stringify(validate.errors)}`,
      );
    }
  }
}
if (failures.length) throw new Error(failures.join("\n"));
console.log(
  `Validated ${examples.length} examples against schema and standalone validators.`,
);
