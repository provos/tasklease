"""Validate live responses to every valid request example against the contract."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA = Path(__file__).resolve().parents[1] / "schema"
VALID_REQUESTS = sorted((SCHEMA / "examples").glob("*/valid/request*.json"))
type Schema = bool | Mapping[str, object]


def read_object(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text())
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.fixture(scope="module")
def registry() -> Registry[Schema]:
    resources: list[tuple[str, Resource[Schema]]] = []
    for path in [SCHEMA / "common.json", *sorted((SCHEMA / "ops").glob("*.json"))]:
        schema = read_object(path)
        identifier = schema["$id"]
        assert isinstance(identifier, str)
        resource: Resource[Schema] = Resource.from_contents(schema)
        resources.append((identifier, resource))
    return Registry[Schema]().with_resources(resources)


@pytest.mark.parametrize(
    "path", VALID_REQUESTS, ids=lambda path: str(path.relative_to(SCHEMA))
)
def test_valid_request_corpus(
    service_url: str, corpus_token: str, registry: Registry[Schema], path: Path
) -> None:
    operation = path.parents[1].name
    token = (
        "conformance-admin-fake-secret"
        if operation.startswith("identity.") and operation != "identity.whoami"
        else corpus_token
    )
    response = (
        httpx.get(service_url + "/v1/version")
        if operation == "version"
        else httpx.post(
            service_url + "/v1/" + operation,
            headers={"Authorization": f"Bearer {token}", "X-Worker": "corpus"},
            json=read_object(path)["body"],
        )
    )
    # State-dependent refusals are legitimate; syntax and authentication must
    # agree with the examples, including the unauthenticated version GET.
    assert response.status_code in (200, 403, 404, 409), response.text
    document = SCHEMA / (
        f"ops/{operation}.json" if response.status_code == 200 else "common.json"
    )
    identifier = read_object(document)["$id"]
    assert isinstance(identifier, str)
    definition = "Response" if response.status_code == 200 else "ErrorResponse"
    validator = Draft202012Validator(
        {"$ref": f"{identifier}#/$defs/{definition}"}, registry=registry
    )
    validator.validate(response.json())
