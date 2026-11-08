"""The tasklease command line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from tasklease import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse a command without side effects until its handler runs."""
    from tasklease.cli import identity, inbox, requester, service, tasks, topics, worker
    from tasklease.cli.common import message
    from tasklease.errors import TaskleaseError

    parser = argparse.ArgumentParser(
        prog="tl", description="A leased task board for agents and workers."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--worker")
    commands = parser.add_subparsers(dest="command")
    for module in (identity, requester, inbox, tasks, topics, service, worker):
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
