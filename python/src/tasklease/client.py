"""Typed synchronous access to the tasklease wire protocol.

No operation is retried: a lost response can mean that a mutation succeeded.
Callers decide how to recover using the operation's fencing semantics.
"""

from __future__ import annotations

import json
import math
import re
from types import TracebackType
from typing import NoReturn, Self, cast

import httpx

from tasklease.config import validate_url
from tasklease.errors import API_ERRORS, Invalid, ProtocolError, TransportError
from tasklease.generated import protocol as p

# Check the transport envelope and its required top-level members before the
# generated DTO cast. Full schemas remain a contract-test dependency, rather
# than adding a runtime JSON Schema dependency to each client installation.
_SUCCESS_FIELDS: dict[str, dict[str, type[object]]] = {
    "create": {"id": str},
    "own": {"task": dict},
    "renew": {"task": dict},
    "read": {"task": dict},
    "delete": {},
    "consume": {"ids": list},
    "topics.list": {"topics": list},
    "tasks.list": {"tasks": list},
    "topic.pollers": {"pollers": list},
    "topic.declare": {},
    "blob.put_url": {"key": str, "url": str, "expires_at": int},
    "blob.get_url": {"url": str, "expires_at": int},
    "identity.add": {"name": str, "token": str},
    "identity.revoke": {},
    "identity.rotate": {"token": str},
    "identity.list": {"identities": list},
    "identity.whoami": {
        "name": str,
        "domains": list,
        "scopes": dict,
        "operator": bool,
    },
    "monitor.snapshot": {
        "domain": str,
        "generated_at": int,
        "topics": list,
        "stats": list,
    },
}


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON number")


class Client:
    """One HTTP connection pool, identity credential and worker label.

    Own and Delete accept a per-call timeout cap in seconds. It limits each
    configured HTTPX phase timeout, including unlimited phases, and never
    lengthens a configured limit. Omitting the cap preserves client settings.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        worker_label: str = "cli",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout | None = 30.0,
    ) -> None:
        self._base_url = validate_url(base_url)
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise Invalid("token must be a nonempty bearer token")
        if (
            not isinstance(worker_label, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,256}", worker_label) is None
        ):
            raise Invalid(
                "worker_label must contain 1 to 256 letters, digits, ., _ or -"
            )
        self._token = token
        self._worker_label = worker_label
        self._http = httpx.Client(
            transport=transport, timeout=timeout, follow_redirects=False
        )

    def __enter__(self) -> Self:
        self._http.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._http.__exit__(exc_type, exc_value, traceback)

    def close(self) -> None:
        self._http.close()

    def _send(
        self,
        method: str,
        operation: str,
        content: bytes | None = None,
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise Invalid("timeout must be a positive finite number of seconds")
        headers = (
            {
                "Authorization": f"Bearer {self._token}",
                "X-Worker": self._worker_label,
                "Content-Type": "application/json",
            }
            if method == "POST"
            else {}
        )
        try:
            url = f"{self._base_url}/v1/{operation}"
            if timeout is None:
                response = self._http.request(
                    method, url, headers=headers, content=content
                )
            else:
                configured = self._http.timeout

                def cap(value: float | None) -> float:
                    return timeout if value is None else min(value, timeout)

                bounded = httpx.Timeout(
                    connect=cap(configured.connect),
                    read=cap(configured.read),
                    write=cap(configured.write),
                    pool=cap(configured.pool),
                )
                response = self._http.request(
                    method, url, headers=headers, content=content, timeout=bounded
                )
        except httpx.RequestError:
            raise TransportError("The tasklease service could not be reached") from None
        if response.status_code >= 500:
            raise TransportError(
                f"The tasklease service returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response

    def _decode(self, response: httpx.Response) -> dict[str, object]:
        try:
            value: object = json.loads(
                response.content, parse_constant=_invalid_constant
            )
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("The service returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ProtocolError("The service returned a non-object response")
        return cast(dict[str, object], value)

    def _post(
        self, operation: str, request: object, *, timeout: float | None = None
    ) -> dict[str, object]:
        try:
            content = json.dumps(
                request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise Invalid("Request must contain valid JSON values") from None
        response = self._send("POST", operation, content, timeout=timeout)
        body = self._decode(response)
        if body.get("ok") is False:
            error = body.get("error")
            if isinstance(error, dict):
                details = cast(dict[str, object], error)
                code, message = details.get("code"), details.get("message")
                error_type = API_ERRORS.get(code) if isinstance(code, str) else None
                if (
                    error_type is not None
                    and isinstance(message, str)
                    and response.status_code == error_type.status_code
                ):
                    raise error_type(message.replace(self._token, "[redacted]"))
            raise ProtocolError("The service returned an invalid error envelope")
        if response.status_code != 200 or body.get("ok") is not True:
            raise ProtocolError("The service returned an invalid success envelope")
        for name, value_type in _SUCCESS_FIELDS[operation].items():
            if type(body.get(name)) is not value_type:
                raise ProtocolError(
                    "The service returned an incomplete success envelope"
                )
        return body

    def create(self, request: p.CreateRequest) -> p.CreateResponse:
        return cast(p.CreateResponse, self._post("create", request))

    def own(
        self, request: p.OwnRequest, *, timeout: float | None = None
    ) -> p.OwnResponse:
        """Own a task, optionally capping each configured HTTP timeout in seconds."""
        return cast(p.OwnResponse, self._post("own", request, timeout=timeout))

    def renew(self, request: p.RenewRequest) -> p.RenewResponse:
        return cast(p.RenewResponse, self._post("renew", request))

    def read(self, request: p.ReadRequest) -> p.ReadResponse:
        return cast(p.ReadResponse, self._post("read", request))

    def delete(
        self, request: p.DeleteRequest, *, timeout: float | None = None
    ) -> p.DeleteResponse:
        """Delete a task, optionally capping each configured HTTP timeout in seconds."""
        return cast(p.DeleteResponse, self._post("delete", request, timeout=timeout))

    def consume(self, request: p.ConsumeRequest) -> p.ConsumeResponse:
        return cast(p.ConsumeResponse, self._post("consume", request))

    def topics_list(self, request: p.TopicsListRequest) -> p.TopicsListResponse:
        return cast(p.TopicsListResponse, self._post("topics.list", request))

    def tasks_list(self, request: p.TasksListRequest) -> p.TasksListResponse:
        return cast(p.TasksListResponse, self._post("tasks.list", request))

    def topic_pollers(self, request: p.TopicPollersRequest) -> p.TopicPollersResponse:
        return cast(p.TopicPollersResponse, self._post("topic.pollers", request))

    def topic_declare(self, request: p.TopicDeclareRequest) -> p.TopicDeclareResponse:
        return cast(p.TopicDeclareResponse, self._post("topic.declare", request))

    def blob_put_url(self, request: p.BlobPutUrlRequest) -> p.BlobPutUrlResponse:
        return cast(p.BlobPutUrlResponse, self._post("blob.put_url", request))

    def blob_get_url(self, request: p.BlobGetUrlRequest) -> p.BlobGetUrlResponse:
        return cast(p.BlobGetUrlResponse, self._post("blob.get_url", request))

    def identity_add(self, request: p.IdentityAddRequest) -> p.IdentityAddResponse:
        return cast(p.IdentityAddResponse, self._post("identity.add", request))

    def identity_revoke(
        self, request: p.IdentityRevokeRequest
    ) -> p.IdentityRevokeResponse:
        return cast(p.IdentityRevokeResponse, self._post("identity.revoke", request))

    def identity_rotate(
        self, request: p.IdentityRotateRequest
    ) -> p.IdentityRotateResponse:
        return cast(p.IdentityRotateResponse, self._post("identity.rotate", request))

    def identity_list(self, request: p.IdentityListRequest) -> p.IdentityListResponse:
        return cast(p.IdentityListResponse, self._post("identity.list", request))

    def identity_whoami(
        self, request: p.IdentityWhoamiRequest
    ) -> p.IdentityWhoamiResponse:
        return cast(p.IdentityWhoamiResponse, self._post("identity.whoami", request))

    def monitor_snapshot(
        self, request: p.MonitorSnapshotRequest
    ) -> p.MonitorSnapshotResponse:
        return cast(p.MonitorSnapshotResponse, self._post("monitor.snapshot", request))

    def version(self) -> p.VersionResponse:
        response = self._send("GET", "version")
        body = self._decode(response)
        if (
            response.status_code != 200
            or body.get("protocol") != "v1"
            or not isinstance(body.get("service_version"), str)
            or not body["service_version"]
        ):
            raise ProtocolError("The service returned an invalid version envelope")
        return cast(p.VersionResponse, body)
