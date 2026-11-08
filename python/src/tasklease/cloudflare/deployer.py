"""Idempotent provisioning, verification and read-only service status."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from tasklease import __version__
from tasklease.client import Client
from tasklease.cloudflare.bundle import configure_bundle, stage_bundle
from tasklease.cloudflare.wrangler import (
    DeployError,
    Wrangler,
    bucket_names,
    deployment_url,
    json_value,
    lifecycle_rules,
    object_value,
    secret_names,
    worker_url,
)
from tasklease.config import Config, save_deployment
from tasklease.errors import TaskleaseError

type Reporter = Callable[[str], None]

_ACCOUNT_ID = re.compile(r"[a-fA-F0-9]{32}")

R2_HELP = (
    "Open Cloudflare dashboard > R2 Object Storage > Overview > Account Details "
    "> Manage API Tokens > Create Account API token. Choose Object Read & Write "
    "and scope it to bucket {bucket}. Export both TL_R2_ACCESS_KEY_ID and "
    "TL_R2_SECRET_ACCESS_KEY, then rerun. Keep the admin token and set "
    "TL_ADMIN_TOKEN to it so the next run can verify the service."
)


@dataclass(frozen=True)
class DeployOptions:
    account_id: str | None = None
    name: str | None = None
    bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    url: str | None = None
    rotate_admin_token: bool = False
    dry_run: bool = False


def _report(message: str) -> None:
    print(message, file=sys.stderr)


def _saved(config: Config) -> dict[str, object]:
    try:
        return object_value(config.values.get("cloudflare", {}), "[cloudflare] config")
    except DeployError as exc:
        raise DeployError(str(exc), 4) from exc


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DeployError(f"{name} must be a nonempty string.", 4)
    return value


def _account(wrangler: Wrangler, requested: str | None) -> str:
    if requested:
        if _ACCOUNT_ID.fullmatch(requested) is None:
            raise DeployError("Cloudflare account ID must be 32 hex digits.", 4)
        wrangler.environ["CLOUDFLARE_ACCOUNT_ID"] = requested
    try:
        who = object_value(
            json_value(wrangler.run(["whoami", "--json"]), "Wrangler whoami"),
            "Wrangler whoami",
        )
    except DeployError as exc:
        raise DeployError(
            f"{exc}\nSign in with npx --yes wrangler@{wrangler.version} login, "
            "or set CLOUDFLARE_API_TOKEN and pass --account-id."
        ) from exc
    if who.get("loggedIn") is not True:
        raise DeployError("Wrangler is not signed in; run its login command.", 4)
    if requested is None:
        accounts = who.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise DeployError(
                "Pass --account-id when more than one account is visible.", 4
            )
        requested = _optional_string(
            object_value(accounts[0], "Cloudflare account").get("id"), "account ID"
        )
    if requested is None or _ACCOUNT_ID.fullmatch(requested) is None:
        raise DeployError(
            "Cloudflare account ID must contain 32 hexadecimal digits.", 4
        )
    wrangler.environ["CLOUDFLARE_ACCOUNT_ID"] = requested
    return requested


def _version(http: httpx.Client, url: str) -> str:
    response = http.get(f"{url.rstrip('/')}/v1/version")
    response.raise_for_status()
    value = object_value(
        json_value(response.text, "service version"), "service version"
    )
    version = value.get("service_version")
    if value.get("protocol") != "v1" or not isinstance(version, str):
        raise DeployError(
            "Service version response has an unsupported protocol or shape."
        )
    return version


def _confirm_admin(
    http: httpx.Client,
    url: str,
    token: str,
    retry_seconds: float,
) -> bool:
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            # Authorization precedes this endpoint's bounded streaming response.
            # Reading only headers avoids buffering an unbounded identity list.
            with http.stream(
                "POST",
                f"{url}/v1/identity.list",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _check_version(http: httpx.Client, url: str, retry_seconds: float) -> None:
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            deployed = _version(http, url)
            if deployed == __version__:
                return
            problem = f"Service version is {deployed}; expected {__version__}."
        except (httpx.HTTPError, DeployError) as exc:
            problem = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeployError(f"Version check failed: {problem}")
        time.sleep(min(1.0, remaining))


def _probe(
    url: str,
    token: str,
    transport: httpx.BaseTransport | None,
    report: Reporter,
) -> None:
    name = f"deploy-check-{secrets.token_hex(8)}"
    domain = "tasklease.deploy-check"
    stage = "temporary identity"
    original: BaseException | None = None
    with Client(url, token, transport=transport) as admin:
        try:
            identity = admin.identity_add(
                {
                    "name": name,
                    "domains": [domain],
                    "operator": False,
                    "scopes": {
                        "create": ["check"],
                        "own": ["check"],
                        "read": ["check"],
                    },
                }
            )
            with Client(url, identity["token"], transport=transport) as client:
                stage = "Durable Objects task flow"
                client.create(
                    {"domain": domain, "topic": "check", "data": {"probe": True}}
                )
                owned = client.own(
                    {"domain": domain, "topic": "check", "hold_seconds": 30}
                )
                client.consume(
                    {
                        "domain": domain,
                        "topic": "check",
                        "id": owned["task"]["id"],
                        "tasks": [],
                    }
                )
                payload = secrets.token_bytes(16)
                stage = "R2 upload"
                put = client.blob_put_url(
                    {
                        "domain": domain,
                        "name": "probe.bin",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
                # A separate HTTP client prevents bearer tokens reaching R2 URLs.
                with httpx.Client(transport=transport, timeout=30) as blob_http:
                    blob_http.put(put["url"], content=payload).raise_for_status()
                    stage = "R2 download"
                    get = client.blob_get_url({"domain": domain, "key": put["key"]})
                    result = blob_http.get(get["url"])
                    result.raise_for_status()
                    if result.content != payload:
                        raise DeployError(
                            "Downloaded R2 probe bytes differ from the upload."
                        )
        except (TaskleaseError, httpx.HTTPError, DeployError) as exc:
            original = exc
            detail = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else "HTTP connection failed"
                if isinstance(exc, httpx.HTTPError)
                else str(exc)
            )
            raise DeployError(f"End-to-end check failed ({stage}): {detail}") from exc
        finally:
            # Also try after a lost identity.add response: it may have committed.
            try:
                admin.identity_revoke({"name": name})
            except (TaskleaseError, httpx.HTTPError) as exc:
                if original is None:
                    raise DeployError(f"Probe identity cleanup failed: {exc}") from exc
                report(f"Probe identity cleanup also failed: {exc}")


def deploy(
    options: DeployOptions,
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    report: Reporter = _report,
    retry_seconds: float = 15,
) -> dict[str, object]:
    """Deploy an installed bundle, retaining recoverable partial state on failure."""
    environment = dict(os.environ if environ is None else environ)
    saved = _saved(config)
    account = (
        options.account_id
        or environment.get("CLOUDFLARE_ACCOUNT_ID")
        or _optional_string(saved.get("account_id"), "saved account ID")
    )
    name = (
        options.name
        or _optional_string(saved.get("name"), "saved Worker name")
        or "tasklease"
    )
    bucket = (
        options.bucket
        or _optional_string(saved.get("bucket"), "saved bucket")
        or "tasklease-blobs"
    )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) is None:
        raise DeployError(
            "Worker name must be 1–63 lowercase letters, digits or hyphens.", 4
        )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None:
        raise DeployError(
            "Bucket must be 3–63 lowercase letters, digits or hyphens.", 4
        )
    key_id = options.r2_access_key_id or environment.get("TL_R2_ACCESS_KEY_ID")
    key_secret = options.r2_secret_access_key or environment.get(
        "TL_R2_SECRET_ACCESS_KEY"
    )
    if bool(key_id) != bool(key_secret):
        raise DeployError(
            "Give both R2 keys together: TL_R2_ACCESS_KEY_ID and "
            "TL_R2_SECRET_ACCESS_KEY.",
            4,
        )
    credentials_pending = saved.get("r2_credentials_pending", False)
    if not isinstance(credentials_pending, bool):
        raise DeployError("cloudflare.r2_credentials_pending must be a boolean.", 4)
    if credentials_pending and not key_id:
        raise DeployError(
            "An R2 credential update is pending; supply a fresh pair of R2 keys "
            "before retrying. Secret names do not confirm their values.",
            4,
        )
    if (
        any(
            saved.get(key) not in (None, value)
            for key, value in (("account_id", account), ("bucket", bucket))
        )
        and not key_id
    ):
        raise DeployError(
            "Changing account or bucket requires a new pair of R2 keys.", 4
        )
    if environment.get("CLOUDFLARE_API_TOKEN") and not account:
        raise DeployError("Pass --account-id with CLOUDFLARE_API_TOKEN.", 4)
    if options.url and worker_url(options.url, name) is None:
        raise DeployError("--url must be this Worker's HTTPS workers.dev URL.", 4)
    with tempfile.TemporaryDirectory(prefix="tasklease-deploy-") as temporary:
        project = Path(temporary)
        pin, template = stage_bundle(project)
        wrangler = Wrangler(pin, project, environment)
        wrangler.redactions.extend(v for v in (key_id, key_secret) if v)
        wrangler.check_node()
        account = _account(wrangler, account)
        rendered = configure_bundle(
            project, template, account_id=account, name=name, bucket=bucket
        )
        buckets = bucket_names(wrangler.run(["r2", "bucket", "list"]))
        exists = bucket in buckets
        rules = (
            lifecycle_rules(wrangler.run(["r2", "bucket", "lifecycle", "list", bucket]))
            if exists
            else []
        )
        ours = [rule for rule in rules if rule["name"] == "tasklease-expire"]
        if len(ours) > 1:
            raise DeployError(
                "Multiple tasklease-expire lifecycle rules exist; "
                "repair them before deploying."
            )
        exact_rule = bool(ours) and ours[0] == {
            "name": "tasklease-expire",
            "enabled": "Yes",
            "prefix": "(all prefixes)",
            "action": "Expire objects after 7 days",
        }
        with httpx.Client(transport=transport, timeout=10) as http:
            if options.dry_run:
                known_secrets: list[str] | None = None
                if (
                    config.url
                    and saved.get("name") == name
                    and saved.get("account_id") == account
                    and worker_url(config.url, name) is not None
                ):
                    try:
                        _version(http, config.url)
                    except (httpx.HTTPError, DeployError):
                        report(
                            "Worker existence is unknown; secret inspection skipped."
                        )
                    else:
                        known_secrets = sorted(
                            secret_names(
                                wrangler.run(
                                    [
                                        "secret",
                                        "list",
                                        "--name",
                                        name,
                                        "--format",
                                        "json",
                                    ]
                                )
                            )
                        )
                else:
                    report("Worker existence is unknown; secret inspection skipped.")
                report("Dry run: no resources, secrets or configuration were changed.")
                return {
                    "dry_run": True,
                    "config": rendered,
                    "account_id": account,
                    "name": name,
                    "bucket": bucket,
                    "bucket_exists": exists,
                    "lifecycle": "unchanged" if exact_rule else "would update",
                    "secrets": known_secrets,
                    "plan": [
                        "deploy Worker",
                        "reconcile secrets",
                        "verify version and task/blob flow",
                        "save configuration",
                    ],
                }
            if not exists:
                wrangler.run(["r2", "bucket", "create", bucket])
            report(f"Bucket {bucket}: {'unchanged' if exists else 'created'}")
            if not exact_rule:
                if ours:
                    wrangler.run(
                        [
                            "r2",
                            "bucket",
                            "lifecycle",
                            "remove",
                            bucket,
                            "--name",
                            "tasklease-expire",
                        ]
                    )
                wrangler.run(
                    [
                        "r2",
                        "bucket",
                        "lifecycle",
                        "add",
                        bucket,
                        "tasklease-expire",
                        "--expire-days",
                        "7",
                        "--force",
                    ]
                )
            rule_action = (
                "unchanged" if exact_rule else "updated" if ours else "created"
            )
            report(f"Lifecycle tasklease-expire: {rule_action}")
            output = project / "deploy.ndjson"
            wrangler.run(
                ["deploy", "--config", str(project / "wrangler.jsonc")],
                output_file=output,
            )
            url = deployment_url(output, name, options.url)
            save_deployment(
                config,
                url=url,
                account_id=account,
                name=name,
                bucket=bucket,
                r2_credentials_pending=True if key_id else None,
            )
            report(
                "Worker uploaded; deployment coordinates saved. "
                "Verification is pending."
            )
            existing = secret_names(
                wrangler.run(["secret", "list", "--name", name, "--format", "json"])
            )
            values: dict[str, str] = {}
            candidate: str | None = None
            if "ADMIN_TOKEN" not in existing or options.rotate_admin_token:
                candidate = "tla_" + secrets.token_urlsafe(32)
                report(
                    "ADMIN TOKEN (not yet confirmed; store now, never saved): "
                    f"{candidate}"
                )
                values["ADMIN_TOKEN"] = candidate
            if key_id and key_secret:
                values.update(R2_ACCESS_KEY_ID=key_id, R2_SECRET_ACCESS_KEY=key_secret)
            bulk_error: DeployError | None = None
            if values:
                try:
                    wrangler.run(["secret", "bulk", "--name", name], secrets=values)
                except DeployError as exc:
                    bulk_error = exc
                else:
                    if key_id:
                        save_deployment(
                            config,
                            url=url,
                            account_id=account,
                            name=name,
                            bucket=bucket,
                            r2_credentials_pending=False,
                        )
            if candidate:
                if _confirm_admin(http, url, candidate, retry_seconds):
                    report("Admin token confirmed.")
                else:
                    report(
                        "Admin token remains unconfirmed; retain it. "
                        "Set TL_ADMIN_TOKEN to the candidate and rerun, or rotate "
                        "again. No conclusion about the previous token is possible."
                    )
                    if bulk_error:
                        raise DeployError(
                            f"{bulk_error}\nAdmin token status is unconfirmed."
                        ) from bulk_error
                    raise DeployError(
                        "Admin token remains unconfirmed after waiting for propagation."
                    )
            if bulk_error:
                raise bulk_error
            present = existing | values.keys()
            if not {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"} <= present:
                raise DeployError(
                    "R2 signing credentials are missing. "
                    + R2_HELP.format(bucket=bucket),
                    4,
                )
            _check_version(http, url, retry_seconds)
            admin_token = candidate or environment.get("TL_ADMIN_TOKEN")
            verified = bool(admin_token)
            if admin_token:
                _probe(url, admin_token, transport, report)
                report("Task and R2 upload/download checks passed.")
            else:
                report(
                    "Deployment is unverified: set TL_ADMIN_TOKEN and rerun "
                    "for the task and R2 checks."
                )
            save_deployment(
                config, url=url, account_id=account, name=name, bucket=bucket
            )
            report(
                "Next: tl identity add for each machine; "
                "tl topic declare for well-known topics."
            )
            return {
                "url": url,
                "account_id": account,
                "name": name,
                "bucket": bucket,
                "service_version": __version__,
                "verified": verified,
            }


def status(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    report: Reporter = _report,
) -> dict[str, object]:
    """Compare versions even on machines without Node or Cloudflare sign-in."""
    if not config.url:
        raise DeployError("No service URL configured; run tl service deploy first.", 4)
    with httpx.Client(transport=transport, timeout=10) as http:
        try:
            deployed = _version(http, config.url)
        except (httpx.HTTPError, DeployError) as exc:
            raise DeployError(f"Cannot read service version: {exc}") from exc
    result: dict[str, object] = {
        "url": config.url,
        "service_version": deployed,
        "installed_version": __version__,
        "upgrade_needed": deployed != __version__,
    }
    configured_url = config.values.get("url")
    if isinstance(configured_url, str) and config.url != configured_url.rstrip("/"):
        report("Cloudflare inspection skipped: the URL overrides the saved deployment.")
        return result
    saved = _saved(config)
    account = _optional_string(saved.get("account_id"), "saved account ID")
    name = _optional_string(saved.get("name"), "saved Worker name")
    bucket = _optional_string(saved.get("bucket"), "saved bucket")
    if not all((account, name, bucket)):
        return result
    environment = dict(os.environ if environ is None else environ)
    if account:
        environment["CLOUDFLARE_ACCOUNT_ID"] = account
    try:
        with tempfile.TemporaryDirectory(prefix="tasklease-status-") as temporary:
            project = Path(temporary)
            pin, template = stage_bundle(project)
            wrangler = Wrangler(pin, project, environment)
            wrangler.check_node()
            account = _account(wrangler, account)
            assert name is not None and bucket is not None
            configure_bundle(
                project, template, account_id=account, name=name, bucket=bucket
            )
            buckets = bucket_names(wrangler.run(["r2", "bucket", "list"]))
            result["cloudflare"] = {
                "bucket_exists": bucket in buckets,
                "lifecycle": lifecycle_rules(
                    wrangler.run(["r2", "bucket", "lifecycle", "list", bucket])
                )
                if bucket in buckets
                else [],
                "secrets": sorted(
                    secret_names(
                        wrangler.run(
                            ["secret", "list", "--name", name, "--format", "json"]
                        )
                    )
                ),
            }
    except DeployError as exc:
        report(f"Cloudflare inspection unavailable: {exc}")
        result["cloudflare"] = {"available": False}
    return result
