"""Validate the shared protocol corpus with the Python JSON Schema implementation."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA = Path(__file__).resolve().parents[2] / "schema"
OPERATIONS = sorted((SCHEMA / "ops").glob("*.json"))
EXAMPLES = sorted((SCHEMA / "examples").glob("*/*/*.json"))
KINDS = {"request": "Request", "response": "Response", "error": "ErrorResponse"}
type Schema = bool | Mapping[str, object]


def read_object(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"Expected a JSON object in {path}"
    return cast(dict[str, object], value)


@pytest.fixture(scope="module")
def registry() -> Registry[Schema]:
    resources: list[tuple[str, Resource[Schema]]] = []
    for path in [SCHEMA / "common.json", *OPERATIONS]:
        schema = read_object(path)
        identifier = schema["$id"]
        assert isinstance(identifier, str)
        Draft202012Validator.check_schema(schema)
        resource: Resource[Schema] = Resource.from_contents(schema)
        resources.append((identifier, resource))
    return Registry[Schema]().with_resources(resources)


@pytest.mark.parametrize(
    "path", EXAMPLES, ids=lambda path: str(path.relative_to(SCHEMA))
)
def test_shared_example(path: Path, registry: Registry[Schema]) -> None:
    example = read_object(path)
    kind = example["kind"]
    assert isinstance(kind, str) and kind in KINDS, f"Unknown example kind in {path}"
    operation = path.parents[1].name
    document = SCHEMA / ("common.json" if kind == "error" else f"ops/{operation}.json")
    identifier = read_object(document)["$id"]
    assert isinstance(identifier, str)
    validator = Draft202012Validator(
        {"$ref": f"{identifier}#/$defs/{KINDS[kind]}"}, registry=registry
    )
    errors = list(validator.iter_errors(example["body"]))
    if path.parent.name == "valid":
        assert not errors, "\n".join(str(error) for error in errors)
    else:
        assert path.parent.name == "invalid"
        assert errors, f"An invalid example unexpectedly validates: {path}"


def test_every_operation_has_positive_and_negative_examples() -> None:
    assert OPERATIONS, "The protocol operation schemas are missing"
    assert {path.parents[1].name for path in EXAMPLES} == {
        path.stem for path in OPERATIONS
    }
    for operation in OPERATIONS:
        for validity in ("valid", "invalid"):
            examples = (SCHEMA / "examples" / operation.stem / validity).glob("*.json")
            kinds = {read_object(path)["kind"] for path in examples}
            assert kinds >= KINDS.keys(), (
                f"Missing {validity} examples for {operation.stem}"
            )
