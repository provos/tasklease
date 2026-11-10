"""The tasklease command line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import cast

from tasklease import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse a command without side effects until its handler runs."""
    from tasklease.cli import (
        identity,
        inbox,
        monitor,
        requester,
        service,
        tasks,
        topics,
        worker,
    )
    from tasklease.cli.common import connection_options, message
    from tasklease.errors import TaskleaseError

    parser = argparse.ArgumentParser(
        prog="tl", description="A leased task board for agents and workers."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    connection_options(parser)
    commands = parser.add_subparsers(dest="command")
    for module in (identity, requester, inbox, tasks, topics, service, worker, monitor):
        module.configure_parser(commands)
    args = parser.parse_args(argv)
    selected: object = getattr(args, "handler", None)
    if selected is None:
        parser.print_help()
        return 0
    handler = cast(Callable[[argparse.Namespace], int], selected)
    try:
        return handler(args)
    except (TaskleaseError, OSError, ValueError) as error:
        message(str(error))
        return 1
    except KeyboardInterrupt:
        message("Interrupted")
        return 130
