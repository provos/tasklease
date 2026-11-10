"""Topic discovery and declarations."""

from __future__ import annotations

import argparse

from tasklease.cli.common import (
    client,
    configuration,
    connection_options,
    domain,
    flag,
    optional_text,
    output,
    text,
)
from tasklease.generated.protocol import TopicsListRequest


def _topics(args: argparse.Namespace) -> int:
    selected = optional_text(args, "domain")
    with client(args) as api:
        domains = [selected] if selected else api.identity_whoami({})["domains"]
        results: list[object] = []
        for name in domains:
            request: TopicsListRequest = {"domain": name}
            glob = optional_text(args, "glob")
            if glob is not None:
                request["glob"] = glob
            results.append({"domain": name, **api.topics_list(request)})
    output(results)
    return 0


def _declare(args: argparse.Namespace) -> int:
    selected = domain(args, configuration(args))
    with client(args) as api:
        output(
            api.topic_declare(
                {
                    "domain": selected,
                    "topic": text(args, "topic"),
                    "declared": not flag(args, "remove"),
                }
            )
        )
    return 0


def configure_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    topics = subparsers.add_parser("topics", help="List topics across domains")
    connection_options(topics)
    topics.add_argument("--domain")
    topics.add_argument("--glob")
    topics.set_defaults(handler=_topics)
    topic = subparsers.add_parser("topic", help="Declare well-known topics")
    commands = topic.add_subparsers(dest="topic_command", required=True)
    declare = commands.add_parser("declare")
    connection_options(declare)
    declare.add_argument(
        "--domain", help="Domain (default: TL_DOMAIN or config.domain)"
    )
    declare.add_argument("topic")
    declare.add_argument("--remove", action="store_true", help="Remove the declaration")
    declare.set_defaults(handler=_declare)
