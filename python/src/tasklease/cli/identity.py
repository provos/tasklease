"""Identity administration and inspection."""

from __future__ import annotations

import argparse

from tasklease.cli.common import (
    client,
    connection_options,
    flag,
    message,
    optional_text,
    output,
    text,
)
from tasklease.errors import ConfigError


def _globs(args: argparse.Namespace, name: str) -> list[str]:
    value = optional_text(args, name)
    if value is None:
        return []
    result = value.split(",")
    if any(not item for item in result):
        raise ConfigError(f"--{name} contains an empty glob")
    return result


def _add(args: argparse.Namespace) -> int:
    domains: object = getattr(args, "domain", None)
    if not isinstance(domains, list):
        raise ConfigError("Give at least one --domain")
    checked_domains: list[str] = []
    for item in domains:
        if not isinstance(item, str):
            raise ConfigError("Domain names must be text")
        checked_domains.append(item)
    own, create = _globs(args, "own"), _globs(args, "create")
    with client(args, admin=True) as api:
        result = api.identity_add(
            {
                "name": text(args, "name"),
                "domains": checked_domains,
                "scopes": {"own": own, "create": create, "read": _globs(args, "read")},
                "operator": flag(args, "operator"),
            }
        )
    output(result)
    added = list(
        dict.fromkeys(f"{glob}/error" for glob in own if f"{glob}/error" not in create)
    )
    if added:
        message(f"Added create grants: {', '.join(added)}")
    message("Store the identity token securely; it is shown only once.")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    with client(args, admin=True) as api:
        output(api.identity_revoke({"name": text(args, "name")}))
    return 0


def _rotate(args: argparse.Namespace) -> int:
    with client(args, admin=True) as api:
        output(api.identity_rotate({"name": text(args, "name")}))
    message(
        "Store the replacement token securely; the old token expires within 60 seconds."
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    with client(args, admin=True) as api:
        output(api.identity_list({}))
    return 0


def _whoami(args: argparse.Namespace) -> int:
    with client(args) as api:
        output(api.identity_whoami({}))
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("identity", help="Manage machine identities")
    commands = parser.add_subparsers(dest="identity_command", required=True)
    add = commands.add_parser("add", help="Issue an identity token")
    connection_options(add)
    add.add_argument("name")
    add.add_argument("--domain", action="append", required=True)
    for scope in ("own", "create", "read"):
        add.add_argument(f"--{scope}", help="Comma-separated topic globs")
    add.add_argument("--operator", action="store_true")
    add.set_defaults(handler=_add)
    for name, handler in (("revoke", _revoke), ("rotate", _rotate), ("list", _list)):
        command = commands.add_parser(name)
        connection_options(command)
        if name != "list":
            command.add_argument("name")
        command.set_defaults(handler=handler)
    whoami = subparsers.add_parser("whoami", help="Show the current identity")
    connection_options(whoami)
    whoami.set_defaults(handler=_whoami)
