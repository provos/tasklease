"""Idempotent provisioning, verification and read-only service status."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
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
_EXPIRE_RULE = "tasklease-expire"
_EXACT_RULE = {
    "name": _EXPIRE_RULE,
    "enabled": "Yes",
    "prefix": "(all prefixes)",
    "action": "Expire objects after 7 days",
}
_R2_SECRETS = {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}

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


@dataclass(frozen=True)
class _Target:
    """The requested deployment; the account is resolved after sign-in."""

    account: str | None
    name: str
    bucket: str
    key_id: str | None
    key_secret: str | None

    def r2_secrets(self) -> dict[str, str]:
        if self.key_id and self.key_secret:
            return {
                "R2_ACCESS_KEY_ID": self.key_id,
                "R2_SECRET_ACCESS_KEY": self.key_secret,
            }
        return {}


@dataclass(frozen=True)
class _Workspace:
    project: Path
    wrangler: Wrangler
    account: str
    rendered: dict[str, object]


def _choose(
    chosen: str | None, saved: Mapping[str, object], key: str, label: str
) -> str | None:
    """An explicit value, else the saved one, which is checked only when used."""
    return chosen or _optional_string(saved.get(key), label)


def _target(
    options: DeployOptions, saved: Mapping[str, object], environment: Mapping[str, str]
) -> _Target:
    """Resolve options, environment and saved settings, then validate them."""
    target = _Target(
        account=_choose(
            options.account_id or environment.get("CLOUDFLARE_ACCOUNT_ID"),
            saved,
            "account_id",
            "saved account ID",
        ),
        name=_choose(options.name, saved, "name", "saved Worker name") or "tasklease",
        bucket=_choose(options.bucket, saved, "bucket", "saved bucket")
        or "tasklease-blobs",
        key_id=options.r2_access_key_id or environment.get("TL_R2_ACCESS_KEY_ID"),
        key_secret=options.r2_secret_access_key
        or environment.get("TL_R2_SECRET_ACCESS_KEY"),
    )
    _check_names(target)
    _check_r2_keys(target, saved)
    if environment.get("CLOUDFLARE_API_TOKEN") and not target.account:
        raise DeployError("Pass --account-id with CLOUDFLARE_API_TOKEN.", 4)
    if options.url and worker_url(options.url, target.name) is None:
        raise DeployError("--url must be this Worker's HTTPS workers.dev URL.", 4)
    return target


def _check_names(target: _Target) -> None:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", target.name) is None:
        raise DeployError(
            "Worker name must be 1–63 lowercase letters, digits or hyphens.", 4
        )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", target.bucket) is None:
        raise DeployError(
            "Bucket must be 3–63 lowercase letters, digits or hyphens.", 4
        )


def _check_r2_keys(target: _Target, saved: Mapping[str, object]) -> None:
    if bool(target.key_id) != bool(target.key_secret):
        raise DeployError(
            "Give both R2 keys together: TL_R2_ACCESS_KEY_ID and "
            "TL_R2_SECRET_ACCESS_KEY.",
            4,
        )
    pending = saved.get("r2_credentials_pending", False)
    if not isinstance(pending, bool):
        raise DeployError("cloudflare.r2_credentials_pending must be a boolean.", 4)
    if target.key_id:
        return
    if pending:
        raise DeployError(
            "An R2 credential update is pending; supply a fresh pair of R2 keys "
            "before retrying. Secret names do not confirm their values.",
            4,
        )
    if saved.get("account_id") not in (None, target.account) or saved.get(
        "bucket"
    ) not in (None, target.bucket):
        raise DeployError(
            "Changing account or bucket requires a new pair of R2 keys.", 4
        )


@contextmanager
def _workspace(
    prefix: str,
    environment: Mapping[str, str],
    *,
    account: str | None,
    name: str,
    bucket: str,
    redactions: Iterable[str | None] = (),
) -> Iterator[_Workspace]:
    """Stage the bundle, check Node and sign-in, and render wrangler.jsonc."""
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        project = Path(temporary)
        pin, template = stage_bundle(project)
        wrangler = Wrangler(pin, project, environment)
        wrangler.redactions.extend(value for value in redactions if value)
        wrangler.check_node()
        resolved = _account(wrangler, account)
        rendered = configure_bundle(
            project, template, account_id=resolved, name=name, bucket=bucket
        )
        yield _Workspace(project, wrangler, resolved, rendered)


def _bucket_state(wrangler: Wrangler, bucket: str) -> tuple[bool, list[dict[str, str]]]:
    """Whether the bucket exists, and its lifecycle rules."""
    exists = bucket in bucket_names(wrangler.run(["r2", "bucket", "list"]))
    if not exists:
        return False, []
    return True, lifecycle_rules(
        wrangler.run(["r2", "bucket", "lifecycle", "list", bucket])
    )


def _our_rule(rules: Iterable[Mapping[str, str]]) -> Mapping[str, str] | None:
    ours = [rule for rule in rules if rule["name"] == _EXPIRE_RULE]
    if len(ours) > 1:
        raise DeployError(
            "Multiple tasklease-expire lifecycle rules exist; "
            "repair them before deploying."
        )
    return ours[0] if ours else None


def _secret_names(wrangler: Wrangler, name: str) -> set[str]:
    return secret_names(
        wrangler.run(["secret", "list", "--name", name, "--format", "json"])
    )


def _known_secrets(
    http: httpx.Client,
    config: Config,
    saved: Mapping[str, object],
    target: _Target,
    workspace: _Workspace,
    report: Reporter,
) -> list[str] | None:
    """Secret names of the saved Worker, if it demonstrably exists."""
    url = _saved_worker_url(config, saved, target.name, workspace.account)
    if url is not None:
        try:
            _version(http, url)
        except (httpx.HTTPError, DeployError):
            pass
        else:
            return sorted(_secret_names(workspace.wrangler, target.name))
    report("Worker existence is unknown; secret inspection skipped.")
    return None


def _saved_worker_url(
    config: Config, saved: Mapping[str, object], name: str, account: str
) -> str | None:
    """The configured URL, if it belongs to the Worker being deployed."""
    if (
        config.url
        and saved.get("name") == name
        and saved.get("account_id") == account
        and worker_url(config.url, name) is not None
    ):
        return config.url
    return None


def _dry_run(
    known_secrets: list[str] | None,
    target: _Target,
    workspace: _Workspace,
    bucket_exists: bool,
    exact_rule: bool,
    report: Reporter,
) -> dict[str, object]:
    report("Dry run: no resources, secrets or configuration were changed.")
    return {
        "dry_run": True,
        "config": workspace.rendered,
        "account_id": workspace.account,
        "name": target.name,
        "bucket": target.bucket,
        "bucket_exists": bucket_exists,
        "lifecycle": "unchanged" if exact_rule else "would update",
        "secrets": known_secrets,
        "plan": [
            "deploy Worker",
            "reconcile secrets",
            "verify version and task/blob flow",
            "save configuration",
        ],
    }


def _reconcile_bucket(
    wrangler: Wrangler,
    bucket: str,
    exists: bool,
    rule: Mapping[str, str] | None,
    report: Reporter,
) -> None:
    if not exists:
        wrangler.run(["r2", "bucket", "create", bucket])
    report(f"Bucket {bucket}: {'unchanged' if exists else 'created'}")
    if rule == _EXACT_RULE:
        report(f"Lifecycle {_EXPIRE_RULE}: unchanged")
        return
    if rule is not None:
        wrangler.run(
            ["r2", "bucket", "lifecycle", "remove", bucket, "--name", _EXPIRE_RULE]
        )
    wrangler.run(
        [
            "r2",
            "bucket",
            "lifecycle",
            "add",
            bucket,
            _EXPIRE_RULE,
            "--expire-days",
            "7",
            "--force",
        ]
    )
    report(f"Lifecycle {_EXPIRE_RULE}: {'updated' if rule else 'created'}")


def _upload(workspace: _Workspace, name: str, requested_url: str | None) -> str:
    """Deploy the Worker and return its workers.dev URL."""
    output = workspace.project / "deploy.ndjson"
    workspace.wrangler.run(
        ["deploy", "--config", str(workspace.project / "wrangler.jsonc")],
        output_file=output,
    )
    return deployment_url(output, name, requested_url)


def _save(
    config: Config,
    workspace: _Workspace,
    target: _Target,
    url: str,
    r2_credentials_pending: bool | None = None,
) -> None:
    save_deployment(
        config,
        url=url,
        account_id=workspace.account,
        name=target.name,
        bucket=target.bucket,
        r2_credentials_pending=r2_credentials_pending,
    )


def _admin_candidate(existing: set[str], rotate: bool, report: Reporter) -> str | None:
    """A new admin token when none exists or rotation was requested."""
    if "ADMIN_TOKEN" in existing and not rotate:
        return None
    candidate = "tla_" + secrets.token_urlsafe(32)
    report(f"ADMIN TOKEN (not yet confirmed; store now, never saved): {candidate}")
    return candidate


def _install_secrets(
    wrangler: Wrangler, name: str, values: Mapping[str, str]
) -> DeployError | None:
    """Upload secrets in one call; the failure is returned so the admin
    candidate can still be confirmed before it is raised."""
    if not values:
        return None
    try:
        wrangler.run(["secret", "bulk", "--name", name], secrets=dict(values))
    except DeployError as exc:
        return exc
    return None


def _settle_admin(
    http: httpx.Client,
    url: str,
    candidate: str | None,
    bulk_error: DeployError | None,
    retry_seconds: float,
    report: Reporter,
) -> None:
    """Confirm a new admin token, then raise any secret upload failure."""
    if candidate and _confirm_admin(http, url, candidate, retry_seconds):
        report("Admin token confirmed.")
    elif candidate:
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


def _verify(
    url: str,
    admin_token: str | None,
    transport: httpx.BaseTransport | None,
    report: Reporter,
) -> bool:
    """Run the end-to-end probe when an admin token is available."""
    if not admin_token:
        report(
            "Deployment is unverified: set TL_ADMIN_TOKEN and rerun "
            "for the task and R2 checks."
        )
        return False
    _probe(url, admin_token, transport, report)
    report("Task and R2 upload/download checks passed.")
    return True


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
    target = _target(options, saved, environment)
    with (
        _workspace(
            "tasklease-deploy-",
            environment,
            account=target.account,
            name=target.name,
            bucket=target.bucket,
            redactions=(target.key_id, target.key_secret),
        ) as workspace,
        httpx.Client(transport=transport, timeout=10) as http,
    ):
        wrangler = workspace.wrangler
        exists, rules = _bucket_state(wrangler, target.bucket)
        rule = _our_rule(rules)
        if options.dry_run:
            known = _known_secrets(http, config, saved, target, workspace, report)
            return _dry_run(
                known, target, workspace, exists, rule == _EXACT_RULE, report
            )
        _reconcile_bucket(wrangler, target.bucket, exists, rule, report)
        url = _upload(workspace, target.name, options.url)
        _save(config, workspace, target, url, True if target.key_id else None)
        report(
            "Worker uploaded; deployment coordinates saved. Verification is pending."
        )
        existing = _secret_names(wrangler, target.name)
        candidate = _admin_candidate(existing, options.rotate_admin_token, report)
        values = ({"ADMIN_TOKEN": candidate} if candidate else {}) | target.r2_secrets()
        bulk_error = _install_secrets(wrangler, target.name, values)
        if target.key_id and bulk_error is None:
            _save(config, workspace, target, url, r2_credentials_pending=False)
        _settle_admin(http, url, candidate, bulk_error, retry_seconds, report)
        if not _R2_SECRETS <= existing | values.keys():
            raise DeployError(
                "R2 signing credentials are missing. "
                + R2_HELP.format(bucket=target.bucket),
                4,
            )
        _check_version(http, url, retry_seconds)
        verified = _verify(
            url, candidate or environment.get("TL_ADMIN_TOKEN"), transport, report
        )
        _save(config, workspace, target, url)
        report(
            "Next: tl identity add for each machine; "
            "tl topic declare for well-known topics."
        )
        return {
            "url": url,
            "account_id": workspace.account,
            "name": target.name,
            "bucket": target.bucket,
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
    if not (account and name and bucket):
        return result
    environment = dict(os.environ if environ is None else environ)
    try:
        with _workspace(
            "tasklease-status-", environment, account=account, name=name, bucket=bucket
        ) as workspace:
            exists, rules = _bucket_state(workspace.wrangler, bucket)
            result["cloudflare"] = {
                "bucket_exists": exists,
                "lifecycle": rules,
                "secrets": sorted(_secret_names(workspace.wrangler, name)),
            }
    except DeployError as exc:
        report(f"Cloudflare inspection unavailable: {exc}")
        result["cloudflare"] = {"available": False}
    return result
