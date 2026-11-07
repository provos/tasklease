"""Read and stage the installed, prebuilt Worker without a source checkout."""

from __future__ import annotations

import json
import re
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from tasklease import __version__
from tasklease.cloudflare.wrangler import DeployError, json_value, object_value


def stage_bundle(destination: Path) -> tuple[str, dict[str, object]]:
    resource = files("tasklease").joinpath("service_bundle")
    if not resource.is_dir():
        raise DeployError(
            "This installation has no bundled Worker; reinstall tasklease."
        )
    with as_file(resource) as source:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    try:
        manifest = object_value(
            json_value((destination / "manifest.json").read_text(), "bundle manifest"),
            "bundle manifest",
        )
        template = object_value(
            json_value(
                (destination / "wrangler.template.jsonc").read_text(), "Worker template"
            ),
            "Worker template",
        )
    except OSError as exc:
        raise DeployError("The installed Worker bundle is incomplete.") from exc
    version = manifest.get("wrangler_version")
    main = template.get("main")
    if (
        manifest.get("service_version") != __version__
        or not isinstance(version, str)
        or re.fullmatch(r"\d+\.\d+\.\d+", version) is None
        or not isinstance(main, str)
        or not (destination / main).is_file()
        or not (destination / main).resolve().is_relative_to(destination.resolve())
    ):
        raise DeployError("The installed Worker manifest or entrypoint is invalid.")
    return version, template


def configure_bundle(
    destination: Path,
    template: dict[str, object],
    *,
    account_id: str,
    name: str,
    bucket: str,
) -> dict[str, object]:
    config = dict(template)
    config.update(account_id=account_id, name=name, no_bundle=True, workers_dev=True)
    variables = object_value(config.get("vars", {}), "Worker variables").copy()
    variables.update(R2_ACCOUNT_ID=account_id, R2_BUCKET_NAME=bucket)
    config["vars"] = variables
    bindings = config.get("r2_buckets")
    if not isinstance(bindings, list):
        raise DeployError("Worker template is missing R2 bindings.")
    found = False
    updated: list[dict[str, object]] = []
    for binding in bindings:
        record = object_value(binding, "R2 binding").copy()
        if record.get("binding") == "BLOBS":
            record["bucket_name"] = bucket
            found = True
        updated.append(record)
    if not found:
        raise DeployError("Worker template is missing the BLOBS binding.")
    config["r2_buckets"] = updated
    (destination / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    return config
