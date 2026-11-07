"""Meaningful generated-type assignments, checked by mypy during codegen."""

from tasklease.generated.protocol import CreateRequest, Data, TopicName

topic: TopicName = "work/linux"
data: Data = {"nested": [{"numbers": [1, 2.5], "null": None, "text": "hello"}]}
request: CreateRequest = {"domain": "acme/pipeline", "topic": topic, "data": data}
