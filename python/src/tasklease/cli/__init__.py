"""Command line entry point."""

import argparse
from collections.abc import Sequence

from tasklease import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the selected command."""
    parser = argparse.ArgumentParser(
        prog="tl", description="A leased task board for agents and workers."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0
