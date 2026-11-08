"""Inspect and prune the machine's durable local reply stores."""

from __future__ import annotations

import argparse

from tasklease.cli.common import connection_options, flag, output
from tasklease.cli.requester import agent_store
from tasklease.store import Store


def _inbox(args: argparse.Namespace) -> int:
    stores = (
        [Store(agent_id) for agent_id in Store.agents()]
        if flag(args, "all")
        else [agent_store(args)]
    )
    pruned = sum(store.prune() for store in stores) if flag(args, "prune") else None
    result: dict[str, object] = {
        "replies": [reply.to_dict() for store in stores for reply in store.inbox()]
    }
    if pruned is not None:
        result["pruned"] = pruned
    output(result)
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("inbox", help="List replies saved on this machine")
    connection_options(parser)
    parser.add_argument("--all", action="store_true", help="Include every local agent")
    parser.add_argument(
        "--prune", action="store_true", help="Remove records over 7 days"
    )
    parser.set_defaults(handler=_inbox)
