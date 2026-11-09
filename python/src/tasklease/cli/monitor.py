"""Open a read-only dashboard with credentials kept in this Python process."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from tasklease.cli.common import (
    client,
    connection_options,
    flag,
    message,
    optional_text,
    output,
)
from tasklease.errors import ConfigError
from tasklease.monitor.server import MonitorServer, validate_domain_selection


def _run(args: argparse.Namespace) -> int:
    selected_domain = validate_domain_selection(optional_text(args, "domain"))
    port: object = getattr(args, "port", 0)
    state_root: object = getattr(args, "state_dir", None)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ConfigError("port must be an integer from 0 to 65535")
    if state_root is not None and not isinstance(state_root, Path):
        raise ConfigError("state-dir must be a path")
    with (
        client(args) as api,
        MonitorServer(
            api, port=port, state_root=state_root, domain=selected_domain
        ) as server,
    ):
        output({"url": server.url})
        message(f"Monitor at {server.url}; press Ctrl-C to stop")
        sys.stdout.flush()
        sys.stderr.flush()
        if not flag(args, "no_browser"):
            webbrowser.open(server.url)
        server.serve_forever(poll_interval=0.2)
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("monitor", help="Open the read-only local dashboard")
    connection_options(parser)
    parser.add_argument("--domain", help="Limit service views to an authorized domain")
    parser.add_argument(
        "--port", type=int, default=0, help="Local port, default free port"
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    parser.set_defaults(handler=_run)
