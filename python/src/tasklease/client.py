"""Typed synchronous access to the tasklease wire protocol.

No operation is retried: a lost response can mean that a mutation succeeded.
Callers decide how to recover using the operation's fencing semantics.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator
from types import TracebackType
from typing import NoReturn, Self, cast

import httpx

from tasklease.config import validate_url
from tasklease.errors import (
    API_ERRORS,
    Empty,
    Invalid,
    ProtocolError,
    TaskleaseError,
    TransportError,
    WaitUnsupported,
)
from tasklease.generated import protocol as p
from tasklease.names import is_bearer_token, is_worker_label

# The service caps Own's wait at 30 seconds. Clients allow a little more for
# the response to arrive before the read phase times out.
MAX_WAIT_SECONDS = 30
# The response must arrive after the server's own wait expires.
MAX_WAIT_GRACE = 15.0
# The first service release that accepts Own's wait_seconds.
WAIT_SERVICE_VERSION = (0, 4, 0)
# The service's message for a request that fails its schema.
_INVALID_BODY = "Invalid request body"
# The version lookup explaining a refused long poll is brief.
_VERSION_TIMEOUT = 10.0

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


def reject_nonfinite(value: str) -> NoReturn:
    """json parse_constant hook: the protocol has no NaN or Infinity."""
    raise ValueError("Nonfinite JSON number")


class _TimedResponseStream(httpx.SyncByteStream):
    """Check elapsed signing time between network response body chunks."""

    def __init__(self, stream: httpx.SyncByteStream, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            self.check()
            yield chunk
        self.check()

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TransportError("Blob signing total_timeout exceeded")

    def close(self) -> None:
        self.stream.close()


class Client:
    """One HTTP connection pool, identity credential and worker label.

    Own, Renew, Consume, Delete and blob signing accept a per-call timeout cap.
    It limits each configured HTTPX phase timeout, including unlimited phases.
    Omitting the cap preserves settings. The one lengthened limit is a long
    poll's read phase: Own with ``wait_seconds`` reads for up to the wait plus
    15 seconds, still within the per-call cap when one is given.
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
        if not is_bearer_token(token):
            raise Invalid("token must be a nonempty bearer token")
        if not is_worker_label(worker_label):
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
        total_timeout: float | None = None,
        read_timeout: float | None = None,
    ) -> httpx.Response:
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise Invalid("timeout must be a positive finite number of seconds")
        if total_timeout is not None:
            if (
                isinstance(total_timeout, bool)
                or not math.isfinite(total_timeout)
                or total_timeout <= 0
            ):
                raise Invalid(
                    "total_timeout must be a positive finite number of seconds"
                )
            deadline = time.monotonic() + total_timeout
            timeout = total_timeout if timeout is None else min(timeout, total_timeout)
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
            bounded: httpx.Timeout | None = None
            if timeout is not None or read_timeout is not None:
                configured = self._http.timeout
                cap = timeout

                def limit(value: float | None) -> float | None:
                    # Without a cap, an unlimited phase stays unlimited.
                    if cap is None:
                        return value
                    return cap if value is None else min(value, cap)

                # A long poll needs a read allowance beyond its wait, but never
                # past the caller's cap. Other phases keep their limits.
                read = limit(configured.read)
                if read_timeout is not None and read is not None:
                    read = max(read, read_timeout)
                    if cap is not None:
                        read = min(read, cap)
                bounded = httpx.Timeout(
                    connect=limit(configured.connect),
                    read=read,
                    write=limit(configured.write),
                    pool=limit(configured.pool),
                )
            request = self._http.build_request(
                method,
                url,
                headers=headers,
                content=content,
                timeout=self._http.timeout if bounded is None else bounded,
            )
            response = self._http.send(request, stream=True)
            try:
                if total_timeout is not None:
                    assert isinstance(response.stream, httpx.SyncByteStream)
                    stream = _TimedResponseStream(response.stream, deadline)
                    stream.check()
                    response.stream = stream
                response.read()
                if total_timeout is not None:
                    stream.check()
            finally:
                response.close()
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
                response.content, parse_constant=reject_nonfinite
            )
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("The service returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ProtocolError("The service returned a non-object response")
        return cast(dict[str, object], value)

    def _post(
        self,
        operation: str,
        request: object,
        *,
        timeout: float | None = None,
        total_timeout: float | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, object]:
        try:
            content = json.dumps(
                request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise Invalid("Request must contain valid JSON values") from None
        response = self._send(
            "POST",
            operation,
            content,
            timeout=timeout,
            total_timeout=total_timeout,
            read_timeout=read_timeout,
        )
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
                    message = message.replace(self._token, "[redacted]")
                    if error_type is Empty:
                        raise Empty(
                            message, wait_refused=details.get("wait_refused") is True
                        )
                    raise error_type(message)
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
        """Own a task, optionally capping each configured HTTP timeout in seconds.

        A long poll's ``wait_seconds`` raises the read timeout to the wait plus
        a response margin, still within ``timeout``, so the default 30-second
        read limit does not cut off the service's answer. A service that
        predates long-poll raises ``WaitUnsupported``, naming its version.
        """
        wait = request.get("wait_seconds")
        waiting = (
            isinstance(wait, int | float) and not isinstance(wait, bool) and wait > 0
        )
        read = float(cast(float, wait)) + MAX_WAIT_GRACE if waiting else None
        try:
            return cast(
                p.OwnResponse,
                self._post("own", request, timeout=timeout, read_timeout=read),
            )
        except Invalid as error:
            # Only the service's schema refusal can mean an unknown field.
            if waiting and type(error) is Invalid and str(error) == _INVALID_BODY:
                self._check_wait_support(error, timeout)
            raise

    def _check_wait_support(self, error: Invalid, timeout: float | None) -> None:
        """Explain an Invalid long poll if the service predates wait_seconds."""
        try:
            reported = self.version(
                timeout=_VERSION_TIMEOUT
                if timeout is None
                else min(timeout, _VERSION_TIMEOUT)
            )["service_version"]
        except TaskleaseError:
            return
        release = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", reported)
        if release is None:
            return
        if tuple(int(part) for part in release.groups()) >= WAIT_SERVICE_VERSION:
            return
        required = ".".join(str(part) for part in WAIT_SERVICE_VERSION)
        raise WaitUnsupported(
            f"The tasklease service (version {reported}) rejected Own's "
            f"wait_seconds: long-polling needs service {required} or later. "
            "Upgrade the service, or disable long-polling with wait_seconds = 0 "
            "(--wait-seconds 0; in the config, [worker] wait_seconds for tl "
            "worker and top-level wait_seconds for tl request and tl wait)."
        ) from error

    def renew(
        self, request: p.RenewRequest, *, timeout: float | None = None
    ) -> p.RenewResponse:
        """Renew a task, optionally capping each configured HTTP timeout."""
        return cast(p.RenewResponse, self._post("renew", request, timeout=timeout))

    def read(self, request: p.ReadRequest) -> p.ReadResponse:
        return cast(p.ReadResponse, self._post("read", request))

    def delete(
        self, request: p.DeleteRequest, *, timeout: float | None = None
    ) -> p.DeleteResponse:
        """Delete a task, optionally capping each configured HTTP timeout in seconds."""
        return cast(p.DeleteResponse, self._post("delete", request, timeout=timeout))

    def consume(
        self, request: p.ConsumeRequest, *, timeout: float | None = None
    ) -> p.ConsumeResponse:
        """Consume a task, optionally capping each configured HTTP timeout."""
        return cast(p.ConsumeResponse, self._post("consume", request, timeout=timeout))

    def topics_list(self, request: p.TopicsListRequest) -> p.TopicsListResponse:
        return cast(p.TopicsListResponse, self._post("topics.list", request))

    def tasks_list(self, request: p.TasksListRequest) -> p.TasksListResponse:
        return cast(p.TasksListResponse, self._post("tasks.list", request))

    def topic_pollers(self, request: p.TopicPollersRequest) -> p.TopicPollersResponse:
        return cast(p.TopicPollersResponse, self._post("topic.pollers", request))

    def topic_declare(self, request: p.TopicDeclareRequest) -> p.TopicDeclareResponse:
        return cast(p.TopicDeclareResponse, self._post("topic.declare", request))

    def blob_put_url(
        self,
        request: p.BlobPutUrlRequest,
        *,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> p.BlobPutUrlResponse:
        """Sign a PUT; optional total_timeout checks the signing body stream.

        The total budget also caps HTTP phases. Header reads and blocking custom
        transports remain subject to HTTPX's per-operation timeout behavior.
        """
        return cast(
            p.BlobPutUrlResponse,
            self._post(
                "blob.put_url", request, timeout=timeout, total_timeout=total_timeout
            ),
        )

    def blob_get_url(
        self,
        request: p.BlobGetUrlRequest,
        *,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> p.BlobGetUrlResponse:
        """Sign a GET, with the same optional budgets as blob_put_url."""
        return cast(
            p.BlobGetUrlResponse,
            self._post(
                "blob.get_url", request, timeout=timeout, total_timeout=total_timeout
            ),
        )

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

    def version(self, *, timeout: float | None = None) -> p.VersionResponse:
        """The service version, optionally capping each HTTP timeout in seconds."""
        response = self._send("GET", "version", timeout=timeout)
        body = self._decode(response)
        if (
            response.status_code != 200
            or body.get("protocol") != "v1"
            or not isinstance(body.get("service_version"), str)
            or not body["service_version"]
        ):
            raise ProtocolError("The service returned an invalid version envelope")
        return cast(p.VersionResponse, body)
