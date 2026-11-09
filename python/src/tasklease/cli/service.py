"""Operator-facing deployment commands and complete first-use instructions."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from tasklease.cloudflare import DeployError, DeployOptions, deploy, status
from tasklease.config import load_config
from tasklease.errors import ConfigError

DEPLOY_HELP = """Deploy or upgrade tasklease in your own Cloudflare account.

You need Workers and R2 enabled, and Node.js current LTS (with npx).
tl uses the exact Wrangler version recorded in its bundled manifest.

1. SIGN IN
   Run npx --yes wrangler@4.156.0 login once and approve the browser flow.
   For scripts, use the Cloudflare Edit Cloudflare Workers API token template,
   export CLOUDFLARE_API_TOKEN, and pass --account-id. The token must permit
   creating/updating Workers and managing R2 buckets and lifecycle rules.

2. ACCOUNT ID
   Pass --account-id, set CLOUDFLARE_ACCOUNT_ID, or reuse the saved account.
   Without one, a single account in Wrangler's OAuth sign-in is selected.
   Dashboard: Workers & Pages > Account Details > Account ID.

3. R2 SIGNING KEYS
   Give both TL_R2_ACCESS_KEY_ID and TL_R2_SECRET_ACCESS_KEY, or pass the ID
   with --r2-access-key-id and enter the secret at the private prompt.
   On first use, run without keys: tl creates the bucket and Worker, prints
   and confirms an admin token, then stops with instructions for the keys.
   In R2 Object Storage > Overview > Account Details > Manage API Tokens,
   create an Account API token with Object Read & Write permission scoped
   to the selected bucket. Record the Access Key ID and Secret Access Key.
   OAuth login credentials cannot sign S3 URLs.

The service uses https://<name>.<subdomain>.workers.dev. Open Workers & Pages
once if the account has no workers.dev subdomain. --url supplies that address
when Wrangler cannot report it. No custom domain is needed.

New admin tokens are printed before submission and never saved. Store yours
in a password manager. If confirmation cannot finish, retain the candidate;
set TL_ADMIN_TOKEN to it and rerun, or use --rotate-admin-token. A timeout
does not prove the candidate was rejected or that the old token still works.

On upgrades, TL_ADMIN_TOKEN enables the task and 16-byte R2 upload/download
check. Without it, the deploy reports that verification was skipped. Probe
identities are revoked; probe blobs expire under the seven-day lifecycle rule.
Changing the account or bucket requires a new pair of signing keys.

--config (or TL_CONFIG) selects every config read and write. Unrelated config
settings are preserved. Nonsecret coordinates are saved after upload so an
interrupted bootstrap resumes against the same target. --dry-run performs
read-only inspection and prints the generated Wrangler config and action plan.

Separate test copy:
  tl service deploy --config /tmp/tl-test.toml --name tasklease-test \\
                    --bucket tasklease-test-blobs

Exit codes: 0 completed; 4 more input needed; 1 failed.
"""


def _text(args: argparse.Namespace, name: str) -> str | None:
    value: object = getattr(args, name, None)
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if not isinstance(value, str):
        raise DeployError(f"Invalid {name} argument.", 4)
    return value


def _run_deploy(args: argparse.Namespace) -> int:
    try:
        environment = dict(os.environ)
        key_id = _text(args, "r2_access_key_id") or environment.get(
            "TL_R2_ACCESS_KEY_ID"
        )
        if (
            key_id
            and not environment.get("TL_R2_SECRET_ACCESS_KEY")
            and sys.stdin.isatty()
        ):
            environment["TL_R2_SECRET_ACCESS_KEY"] = getpass.getpass(
                "R2 Secret Access Key: "
            )
        result = deploy(
            DeployOptions(
                account_id=_text(args, "account_id"),
                name=_text(args, "name"),
                bucket=_text(args, "bucket"),
                r2_access_key_id=key_id,
                url=_text(args, "url"),
                rotate_admin_token=bool(getattr(args, "rotate_admin_token", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
            ),
            load_config(_text(args, "config"), environ=environment),
            environ=environment,
        )
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (ConfigError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 4 if isinstance(exc, ConfigError) else 1
    print(json.dumps(result))
    return 0


def _run_status(args: argparse.Namespace) -> int:
    try:
        environment = dict(os.environ)
        override = _text(args, "url")
        if override is not None:
            environment["TL_URL"] = override
        result = status(
            load_config(_text(args, "config"), environ=environment),
            environ=environment,
        )
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (ConfigError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 4 if isinstance(exc, ConfigError) else 1
    print(json.dumps(result))
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    service = subparsers.add_parser("service", help="deploy or inspect the service")
    commands = service.add_subparsers(dest="service_command", required=True)
    deployment = commands.add_parser(
        "deploy",
        help="deploy or upgrade the bundled Worker",
        description=DEPLOY_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for name, help_text in (
        ("account-id", "Cloudflare account ID"),
        ("name", "Worker name"),
        ("bucket", "R2 bucket name"),
        ("r2-access-key-id", "R2 signing access key ID"),
        ("url", "HTTPS workers.dev address when Wrangler cannot report it"),
        ("config", "tasklease config file (or TL_CONFIG)"),
    ):
        deployment.add_argument(f"--{name}", help=help_text, default=argparse.SUPPRESS)
    deployment.add_argument("--rotate-admin-token", action="store_true")
    deployment.add_argument("--dry-run", action="store_true")
    deployment.set_defaults(handler=_run_deploy)
    inspection = commands.add_parser(
        "status", help="compare service and installed versions"
    )
    inspection.add_argument(
        "--config", default=argparse.SUPPRESS, help="tasklease config file"
    )
    inspection.add_argument(
        "--url", default=argparse.SUPPRESS, help="service origin to inspect"
    )
    inspection.set_defaults(handler=_run_status)
