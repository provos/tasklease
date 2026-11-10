"""Bounded-memory file transfers through the service's presigned blob URLs.

Transfer clients never receive the API bearer token. Downloads replace their
destination atomically only after checking the expected byte count and SHA-256.
These helpers make one attempt; callers decide whether and when to retry.
"""

from __future__ import annotations

import hashlib
import math
import re
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from tasklease.client import Client
from tasklease.errors import APIError, TaskleaseError

_CHUNK_SIZE = 64 * 1024
_MAX_SIZE = 2 * 1024**3
_SHA256 = re.compile(r"[a-fA-F0-9]{64}")


class BlobTransferError(TaskleaseError):
    """Signing, HTTP, file I/O, or integrity verification failed."""


@dataclass(frozen=True)
class UploadedBlob:
    """Metadata to record beside a blob key in a profile's task data."""

    key: str
    size: int
    sha256: str


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise BlobTransferError("timeout must be a positive finite number of seconds")


@contextmanager
def _transfer_client(http: httpx.Client | None) -> Iterator[httpx.Client]:
    if http is not None:
        yield http
    else:
        with httpx.Client(trust_env=False, follow_redirects=False) as owned:
            yield owned


def _request(
    http: httpx.Client,
    method: str,
    url: str,
    timeout: float,
    *,
    content: httpx.SyncByteStream | None = None,
    size: int | None = None,
) -> httpx.Request:
    request = http.build_request(
        method,
        url,
        content=content,
        headers={
            "Accept-Encoding": "identity",
            **({"Content-Length": str(size)} if size is not None else {}),
        },
        timeout=timeout,
    )
    # An injected client can carry default credentials. Discard them after
    # building the request, and disable its auth flow when sending it.
    for header in ("Authorization", "Proxy-Authorization", "Cookie", "X-Worker"):
        request.headers.pop(header, None)
    return request


class _UploadBody(httpx.SyncByteStream):
    def __init__(self, source: BinaryIO, size: int, sha256: str) -> None:
        self.source = source
        self.expected_size = size
        self.expected_sha256 = sha256
        self.size = 0
        self.digest = hashlib.sha256()
        self.complete = False

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.source.read(_CHUNK_SIZE):
            self.size += len(chunk)
            if self.size > self.expected_size:
                raise BlobTransferError("The upload file changed during transfer")
            self.digest.update(chunk)
            yield chunk
        self.complete = True

    def verify(self) -> None:
        if (
            not self.complete
            or self.size != self.expected_size
            or self.digest.hexdigest() != self.expected_sha256
        ):
            raise BlobTransferError("The upload file changed during transfer")


def _measure(source: BinaryIO) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    while chunk := source.read(_CHUNK_SIZE):
        size += len(chunk)
        if size > _MAX_SIZE:
            raise BlobTransferError("Blob exceeds 2 GiB")
        digest.update(chunk)
    if size == 0:
        raise BlobTransferError("Blob must contain at least one byte")
    return size, digest.hexdigest()


def upload_file(
    client: Client,
    domain: str,
    path: Path | str,
    *,
    name: str | None = None,
    http: httpx.Client | None = None,
    timeout: float = 30.0,
) -> UploadedBlob:
    """Hash and upload a file, returning its key, byte count, and SHA-256.

    ``name`` defaults to the source basename. ``http`` must be a separate blob
    client; it remains open after this call. If the source changes between
    hashing and transfer, the upload fails rather than returning wrong metadata.
    A failed upload may leave an object for the bucket lifecycle to remove.
    """
    _validate_timeout(timeout)
    try:
        source_path = Path(path)
        with source_path.open("rb") as source:
            size, sha256 = _measure(source)
            try:
                signed = client.blob_put_url(
                    {
                        "domain": domain,
                        "name": source_path.name if name is None else name,
                        "size": size,
                        "sha256": sha256,
                    }
                )
            except APIError as error:
                raise BlobTransferError(
                    f"Blob upload signing failed: {error.code}"
                ) from error
            source.seek(0)
            body = _UploadBody(source, size, sha256)
            with _transfer_client(http) as transfer:
                request = _request(
                    transfer, "PUT", signed["url"], timeout, content=body, size=size
                )
                with closing(
                    transfer.send(
                        request, stream=True, auth=None, follow_redirects=False
                    )
                ) as response:
                    response.raise_for_status()
                    body.verify()
            return UploadedBlob(signed["key"], size, sha256)
    except BlobTransferError:
        raise
    except (
        TaskleaseError,
        httpx.HTTPError,
        httpx.InvalidURL,
        RuntimeError,
        OSError,
        ValueError,
    ):
        raise BlobTransferError("Blob upload failed") from None


def download_file(
    client: Client,
    domain: str,
    key: str,
    path: Path | str,
    *,
    size: int,
    sha256: str,
    http: httpx.Client | None = None,
    timeout: float = 30.0,
) -> Path:
    """Verify a streamed download, then atomically replace ``path``.

    The expected size and SHA-256 come from the profile's task data. A failure
    removes the temporary file and preserves an existing destination. ``http``
    remains open when supplied. No redirect is followed and no attempt is retried.
    Transfer clients must preserve encoded responses' raw streams: once HTTPX
    has consumed and decoded one, the original stored bytes cannot be verified.
    """
    _validate_timeout(timeout)
    if type(size) is not int or not 1 <= size <= _MAX_SIZE:
        raise BlobTransferError("size must be between 1 byte and 2 GiB")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise BlobTransferError("sha256 must contain 64 hexadecimal characters")
    temporary: Path | None = None
    try:
        try:
            destination = Path(path)
            try:
                signed = client.blob_get_url({"domain": domain, "key": key})
            except APIError as error:
                raise BlobTransferError(
                    f"Blob download signing failed: {error.code}"
                ) from error
            with _transfer_client(http) as transfer:
                request = _request(transfer, "GET", signed["url"], timeout)
                with closing(
                    transfer.send(
                        request, stream=True, auth=None, follow_redirects=False
                    )
                ) as response:
                    response.raise_for_status()
                    if response.is_stream_consumed and any(
                        encoding.strip().lower() not in {"", "identity"}
                        for encoding in response.headers.get_list(
                            "Content-Encoding", split_commas=True
                        )
                    ):
                        raise BlobTransferError(
                            "Encoded blob response was already consumed; "
                            "transfer clients must preserve the raw response stream"
                        )
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=destination.parent,
                        prefix=f".{destination.name}.",
                        suffix=".part",
                        delete=False,
                    ) as output:
                        temporary = Path(output.name)
                        received = 0
                        digest = hashlib.sha256()
                        # Unencoded preloaded responses retain the stored bytes.
                        # Streamed responses must not be content-decoded or
                        # buffered before enforcing the expected size.
                        chunks = (
                            response.iter_bytes()
                            if response.is_stream_consumed
                            else response.iter_raw()
                        )
                        for chunk in chunks:
                            received += len(chunk)
                            if received > size:
                                raise BlobTransferError(
                                    "Blob exceeds its expected size"
                                )
                            output.write(chunk)
                            digest.update(chunk)
                        if received != size:
                            raise BlobTransferError("Blob size does not match")
                        if digest.hexdigest() != sha256.lower():
                            raise BlobTransferError("Blob SHA-256 does not match")
            temporary.replace(destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except BlobTransferError:
        raise
    except (
        TaskleaseError,
        httpx.HTTPError,
        httpx.InvalidURL,
        RuntimeError,
        OSError,
        ValueError,
    ):
        raise BlobTransferError("Blob download failed") from None
