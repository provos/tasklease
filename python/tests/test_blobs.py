"""File integrity, credential separation, and transfer failure cleanup."""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest

from tasklease.blobs import (
    BlobTransferError,
    UploadedBlob,
    download_file,
    upload_file,
)
from tasklease.client import Client
from tasklease.errors import APIError, Forbidden, Invalid, TooLarge, Unauthenticated

_DATA = b"the bytes of a report\x00\xff"
_DIGEST = hashlib.sha256(_DATA).hexdigest()
_URL = "https://blobs.example/object?signature=private-signature"
_KEY = "acme/2026-11-09/01KABCDEF0123456789ABCDEFGH/report.bin"


@contextmanager
def _api(
    requests: list[httpx.Request] | None = None,
    on_sign: Callable[[], None] | None = None,
    error: type[APIError] | None = None,
) -> Iterator[Client]:
    def sign(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer api-secret"
        if requests is not None:
            requests.append(request)
        if on_sign is not None:
            on_sign()
        if error is not None:
            return httpx.Response(
                error.status_code,
                json={
                    "ok": False,
                    "error": {"code": error.code, "message": "refused api-secret"},
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "key": _KEY, "url": _URL, "expires_at": 9999999999999},
        )

    with Client(
        "https://tasks.example", "api-secret", transport=httpx.MockTransport(sign)
    ) as client:
        yield client


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, fail_at_end: bool = False) -> None:
        self.chunks = chunks
        self.fail_at_end = fail_at_end
        self.read = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk
        if self.fail_at_end:
            raise httpx.ReadError("unsafe diagnostic " + _URL)

    def close(self) -> None:
        self.closed = True


def _no_credentials(request: httpx.Request, method: str) -> None:
    assert request.method == method
    assert str(request.url) == _URL
    for name in ("Authorization", "Proxy-Authorization", "Cookie", "X-Worker"):
        assert name not in request.headers
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 7.0
    )


def test_upload_hashes_and_streams_file_without_api_or_injected_credentials(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)
    signing: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        _no_credentials(request, "PUT")
        assert request.headers["Content-Length"] == str(len(_DATA))
        assert request.content == _DATA
        return httpx.Response(200)

    with (
        _api(signing) as client,
        httpx.Client(
            transport=httpx.MockTransport(receive),
            headers={
                "Authorization": "Bearer accidental-token",
                "Proxy-Authorization": "secret",
                "X-Worker": "accidental-worker",
            },
            cookies={"session": "secret"},
            auth=("accidental", "password"),
        ) as transfer,
    ):
        uploaded = upload_file(client, "acme", source, http=transfer, timeout=7.0)
        assert not transfer.is_closed
    assert uploaded == UploadedBlob(_KEY, len(_DATA), _DIGEST)
    assert json.loads(signing[0].content) == {
        "domain": "acme",
        "name": "report.bin",
        "size": len(_DATA),
        "sha256": _DIGEST,
    }


def test_upload_uses_explicit_name_and_supports_string_path(tmp_path: Path) -> None:
    source = tmp_path / "source with spaces"
    source.write_bytes(_DATA)
    signing: list[httpx.Request] = []
    with (
        _api(signing) as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as http,
    ):
        upload_file(client, "acme", str(source), name="safe.bin", http=http)
    assert json.loads(signing[0].content)["name"] == "safe.bin"


@pytest.mark.parametrize("replacement", [b"x", b"x" * len(_DATA), _DATA + b"x"])
def test_upload_refuses_metadata_if_source_changes_during_signing(
    tmp_path: Path, replacement: bytes
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)

    def mutate() -> None:
        source.write_bytes(replacement)

    with (
        _api(on_sign=mutate) as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as http,
        pytest.raises(BlobTransferError, match="changed"),
    ):
        upload_file(client, "acme", source, http=http)


def test_empty_or_unreadable_upload_never_requests_a_url(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.touch()
    signing: list[httpx.Request] = []
    with _api(signing) as client:
        for path in (source, tmp_path / "absent"):
            with pytest.raises(BlobTransferError):
                upload_file(client, "acme", path)
    assert signing == []


@pytest.mark.parametrize("failure", ["http", "redirect", "network"])
def test_upload_transfer_failures_are_sanitized_and_never_retried(
    tmp_path: Path, failure: str
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)
    attempts = 0

    def receive(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if failure == "network":
            raise httpx.WriteError("unsafe diagnostic " + _URL)
        return httpx.Response(
            500 if failure == "http" else 307,
            headers={"Location": "https://elsewhere.example"},
        )

    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(receive), follow_redirects=True
        ) as http,
        pytest.raises(BlobTransferError) as caught,
    ):
        upload_file(client, "acme", source, http=http)
    assert attempts == 1
    if failure != "network":
        assert f"HTTP {500 if failure == 'http' else 307}" in str(caught.value)
    assert "private-signature" not in str(caught.value)
    assert "unsafe diagnostic" not in str(caught.value)
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "private-signature" not in diagnostic
    assert "unsafe diagnostic" not in diagnostic


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_closed_injected_client_raises_transfer_error(
    tmp_path: Path, direction: str
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    http.close()
    with _api() as client, pytest.raises(BlobTransferError):
        if direction == "upload":
            upload_file(client, "acme", source, http=http)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
            )
    assert not (tmp_path / "out").exists()


def test_download_verifies_and_atomically_replaces_destination(tmp_path: Path) -> None:
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"previous result")
    stream = _Chunks([_DATA[:5], _DATA[5:]])

    def receive(request: httpx.Request) -> httpx.Response:
        _no_credentials(request, "GET")
        assert destination.read_bytes() == b"previous result"
        return httpx.Response(200, stream=stream)

    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(receive),
            headers={"Authorization": "Bearer accidental-token"},
            auth=("accidental", "password"),
        ) as http,
    ):
        result = download_file(
            client,
            "acme",
            _KEY,
            str(destination),
            size=len(_DATA),
            sha256=_DIGEST.upper(),
            http=http,
            timeout=7.0,
        )
        assert not http.is_closed
    assert result == destination
    assert destination.read_bytes() == _DATA
    assert stream.closed
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.bin"]


@pytest.mark.parametrize("failure", ["short", "hash", "stream", "http", "redirect"])
@pytest.mark.parametrize("existing", [False, True])
def test_download_failure_removes_partial_and_preserves_existing_destination(
    tmp_path: Path, failure: str, existing: bool
) -> None:
    destination = tmp_path / "result.bin"
    if existing:
        destination.write_bytes(b"previous result")
    stream = _Chunks(
        [_DATA[:-1] if failure == "short" else _DATA],
        fail_at_end=failure == "stream",
    )
    status = 404 if failure == "http" else 302 if failure == "redirect" else 200
    attempts = 0

    def receive(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            status, stream=stream, headers={"Location": "https://elsewhere.example"}
        )

    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(receive), follow_redirects=True
        ) as http,
        pytest.raises(BlobTransferError) as caught,
    ):
        download_file(
            client,
            "acme",
            _KEY,
            destination,
            size=len(_DATA),
            sha256="0" * 64 if failure == "hash" else _DIGEST,
            http=http,
        )
    assert attempts == 1
    assert "private-signature" not in str(caught.value)
    assert "unsafe diagnostic" not in str(caught.value)
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "private-signature" not in diagnostic
    assert "unsafe diagnostic" not in diagnostic
    assert stream.closed
    if existing:
        assert destination.read_bytes() == b"previous result"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_download_stops_reading_as_soon_as_expected_size_is_exceeded(
    tmp_path: Path,
) -> None:
    stream = _Chunks([b"abc", b"d", b"must not be read"])
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http,
        pytest.raises(BlobTransferError, match="exceeds"),
    ):
        download_file(
            client,
            "acme",
            _KEY,
            tmp_path / "out",
            size=3,
            sha256=hashlib.sha256(b"abc").hexdigest(),
            http=http,
        )
    assert stream.read == 2
    assert stream.closed
    assert list(tmp_path.iterdir()) == []


def test_download_hashes_stored_bytes_without_decompressing_content_encoding(
    tmp_path: Path,
) -> None:
    stored = gzip.compress(_DATA * 1000)
    stream = _Chunks([stored])
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=stream, headers={"Content-Encoding": "gzip"}
                )
            )
        ) as http,
    ):
        destination = download_file(
            client,
            "acme",
            _KEY,
            tmp_path / "report.gz",
            size=len(stored),
            sha256=hashlib.sha256(stored).hexdigest(),
            http=http,
        )
    assert destination.read_bytes() == stored
    assert stream.closed


@pytest.mark.parametrize("preload", ["content", "read"])
@pytest.mark.parametrize("expected_decoded", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_download_rejects_decoded_preloaded_responses_before_creating_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preload: str,
    expected_decoded: bool,
    existing: bool,
) -> None:
    stored = gzip.compress(_DATA * 1000)
    expected = _DATA * 1000 if expected_decoded else stored
    destination = tmp_path / "result.bin"
    if existing:
        destination.write_bytes(b"previous")
    responses: list[httpx.Response] = []

    def receive(request: httpx.Request) -> httpx.Response:
        if preload == "content":
            response = httpx.Response(
                200, content=stored, headers={"Content-Encoding": "gzip"}
            )
        else:
            response = httpx.Response(
                200,
                stream=_Chunks([stored]),
                headers={"Content-Encoding": "gzip"},
            )
            response.read()
        assert response.is_stream_consumed
        responses.append(response)
        return response

    def unexpected_partial(**kwargs: object) -> None:
        pytest.fail("Rejected preloaded encoded response created a partial file")

    monkeypatch.setattr(
        "tasklease.blobs.tempfile.NamedTemporaryFile", unexpected_partial
    )
    with (
        _api() as client,
        httpx.Client(transport=httpx.MockTransport(receive)) as http,
        pytest.raises(BlobTransferError, match="preserve the raw response stream"),
    ):
        download_file(
            client,
            "acme",
            _KEY,
            destination,
            size=len(expected),
            sha256=hashlib.sha256(expected).hexdigest(),
            http=http,
        )
    assert len(responses) == 1 and responses[0].is_closed
    if existing:
        assert destination.read_bytes() == b"previous"
        assert list(tmp_path.iterdir()) == [destination]
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("encoding", [None, "", "identity", " Identity, identity "])
def test_download_accepts_unencoded_preloaded_responses(
    tmp_path: Path, encoding: str | None
) -> None:
    headers = {} if encoding is None else {"Content-Encoding": encoding}
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=_DATA, headers=headers)
            )
        ) as http,
    ):
        destination = download_file(
            client,
            "acme",
            _KEY,
            tmp_path / "result.bin",
            size=len(_DATA),
            sha256=_DIGEST,
            http=http,
        )
    assert destination.read_bytes() == _DATA


class _BrokenOutput:
    def __init__(self, path: Path) -> None:
        self.name = str(path)
        path.touch()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def write(self, chunk: bytes) -> int:
        raise OSError("disk full")


def test_download_removes_partial_after_disk_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"previous")
    output = _BrokenOutput(tmp_path / "output.part")
    monkeypatch.setattr(
        "tasklease.blobs.tempfile.NamedTemporaryFile", lambda **_: output
    )
    stream = _Chunks([_DATA])
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http,
        pytest.raises(BlobTransferError),
    ):
        download_file(
            client,
            "acme",
            _KEY,
            destination,
            size=len(_DATA),
            sha256=_DIGEST,
            http=http,
        )
    assert destination.read_bytes() == b"previous"
    assert not Path(output.name).exists()
    assert stream.closed


def test_download_removes_verified_partial_if_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(source: Path, target: Path) -> Path:
        raise OSError("replacement failed")

    monkeypatch.setattr("tasklease.blobs.Path.replace", fail_replace)
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_DATA))
        ) as http,
        pytest.raises(BlobTransferError),
    ):
        download_file(
            client,
            "acme",
            _KEY,
            tmp_path / "out",
            size=len(_DATA),
            sha256=_DIGEST,
            http=http,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("error_type", [Unauthenticated, Forbidden, Invalid, TooLarge])
def test_signing_failures_are_transfer_errors_without_blob_requests(
    tmp_path: Path, direction: str, error_type: type[APIError]
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with (
        _api(error=error_type) as client,
        httpx.Client(transport=httpx.MockTransport(receive)) as http,
        pytest.raises(BlobTransferError) as caught,
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
            )
    assert requests == []
    assert not (tmp_path / "out").exists()
    cause = caught.value.__cause__
    assert type(cause) is error_type
    assert cause.code == error_type.code
    assert cause.status_code == error_type.status_code
    assert error_type.code in str(caught.value)
    assert "api-secret" not in str(cause)
    assert "[redacted]" in str(cause)


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_timeout_is_a_transfer_error(
    tmp_path: Path, direction: str, timeout: float
) -> None:
    signing: list[httpx.Request] = []
    with _api(signing) as client, pytest.raises(BlobTransferError, match="timeout"):
        if direction == "upload":
            upload_file(client, "acme", tmp_path / "source", timeout=timeout)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                timeout=timeout,
            )
    assert signing == []


@pytest.mark.parametrize(
    ("size", "sha256"),
    [(0, _DIGEST), (2**31 + 1, _DIGEST), (True, _DIGEST), (1, "x" * 64), (1, "a")],
)
def test_invalid_expected_metadata_never_signs_or_creates_a_file(
    tmp_path: Path, size: int, sha256: str
) -> None:
    signing: list[httpx.Request] = []
    with _api(signing) as client, pytest.raises(BlobTransferError):
        download_file(client, "acme", _KEY, tmp_path / "out", size=size, sha256=sha256)
    assert signing == []
    assert list(tmp_path.iterdir()) == []


def test_helpers_close_only_clients_they_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(_DATA)
    real_client = httpx.Client
    transfers: list[httpx.Client] = []

    def new_client(**kwargs: object) -> httpx.Client:
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        transfer = real_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, content=_DATA if r.method == "GET" else b""
                )
            )
        )
        transfers.append(transfer)
        return transfer

    with _api() as client:
        monkeypatch.setattr("tasklease.blobs.httpx.Client", new_client)
        upload_file(client, "acme", source)
        download_file(
            client,
            "acme",
            _KEY,
            tmp_path / "out",
            size=len(_DATA),
            sha256=_DIGEST,
        )
    assert len(transfers) == 2
    assert all(transfer.is_closed for transfer in transfers)


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("limit", [0.0, -1.0, float("inf"), float("nan"), True])
def test_invalid_total_timeout_never_signs(
    tmp_path: Path, direction: str, limit: float
) -> None:
    signing: list[httpx.Request] = []
    with (
        _api(signing) as client,
        pytest.raises(BlobTransferError, match="total_timeout"),
    ):
        if direction == "upload":
            upload_file(client, "acme", tmp_path / "absent", total_timeout=limit)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                total_timeout=limit,
            )
    assert signing == []


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_total_budget_includes_signing_and_caps_api_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    now = [10.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])
    source = tmp_path / "source"
    source.write_bytes(_DATA)
    signing: list[httpx.Request] = []

    def sign() -> None:
        now[0] += 2.0

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("An exhausted signing budget must not send a blob request")

    with (
        _api(signing, on_sign=sign) as client,
        httpx.Client(transport=httpx.MockTransport(unexpected)) as http,
        pytest.raises(BlobTransferError, match="total_timeout"),
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http, total_timeout=1.0)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
                total_timeout=1.0,
            )
    assert signing[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 1.0
    )
    assert not (tmp_path / "out").exists()


def test_total_budget_includes_upload_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])

    class SlowFile(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            now[0] += 0.6
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *a, **kw: SlowFile(_DATA))
    signing: list[httpx.Request] = []
    with (
        _api(signing) as client,
        pytest.raises(BlobTransferError, match="total_timeout"),
    ):
        upload_file(client, "acme", tmp_path / "source", total_timeout=0.5)
    assert signing == []


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_total_budget_stops_trickled_signing_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    now = [0.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])
    source = tmp_path / "source"
    source.write_bytes(_DATA)
    consumed = 0
    closed = False

    class SigningStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal consumed
            # Every chunk arrives before the .1s read timeout, but the entire
            # response must still stop after the .1s total budget is exhausted.
            for _ in range(80):
                now[0] += 0.03
                consumed += 1
                yield b" "

        def close(self) -> None:
            nonlocal closed
            closed = True

    def signing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SigningStream())

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Signing timeout must not start a blob request")

    with (
        Client(
            "https://tasks.example",
            "api-secret",
            transport=httpx.MockTransport(signing),
        ) as client,
        httpx.Client(transport=httpx.MockTransport(unexpected)) as http,
        pytest.raises(BlobTransferError, match="total_timeout") as caught,
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http, total_timeout=0.1)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
                total_timeout=0.1,
            )
    assert consumed == 4
    assert closed
    assert not (tmp_path / "out").exists()
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_total_budget_caps_transfer_to_remainder_after_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    now = [0.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])
    source = tmp_path / "source"
    source.write_bytes(_DATA)

    def signed() -> None:
        now[0] += 0.75

    def transfer(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 0.25
        )
        return httpx.Response(200, content=_DATA if direction == "download" else b"")

    with (
        _api(on_sign=signed) as client,
        httpx.Client(transport=httpx.MockTransport(transfer)) as http,
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http, timeout=2, total_timeout=1)
        else:
            result = download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
                timeout=2,
                total_timeout=1,
            )
            assert result.read_bytes() == _DATA


def test_download_budget_checks_before_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])
    destination = tmp_path / "out"
    destination.write_bytes(b"previous")

    class SlowClose(_Chunks):
        def close(self) -> None:
            now[0] += 2
            super().close()

    stream = SlowClose([_DATA])
    with (
        _api() as client,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http,
        pytest.raises(BlobTransferError, match="total_timeout"),
    ):
        download_file(
            client,
            "acme",
            _KEY,
            destination,
            size=len(_DATA),
            sha256=_DIGEST,
            http=http,
            total_timeout=1,
        )
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("chunks", [1, 4])
def test_total_budget_stops_trickling_transfers_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str, chunks: int
) -> None:
    now = [0.0]
    monkeypatch.setattr("tasklease.blobs.time.monotonic", lambda: now[0])
    data = b"x" * (64 * 1024 * chunks)
    source = tmp_path / "source"
    source.write_bytes(data)
    destination = tmp_path / "out"
    destination.write_bytes(b"previous")
    consumed = 0
    responses: list[httpx.Response] = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal consumed
            for offset in range(0, len(data), 64 * 1024):
                now[0] += 0.6 if chunks == 1 else 0.3
                consumed += 1
                yield data[offset : offset + 64 * 1024]

    class SlowTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal consumed
            assert request.extensions["timeout"] == dict.fromkeys(
                ("connect", "read", "write", "pool"), 0.5
            )
            if request.method == "PUT":
                assert isinstance(request.stream, httpx.SyncByteStream)
                for _ in request.stream:
                    consumed += 1
                    now[0] += 0.6 if chunks == 1 else 0.3
                response = httpx.Response(200)
            else:
                response = httpx.Response(200, stream=SlowStream())
            responses.append(response)
            return response

    with (
        _api() as client,
        httpx.Client(transport=SlowTransport()) as http,
        pytest.raises(BlobTransferError, match="total_timeout"),
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http, timeout=1, total_timeout=0.5)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                destination,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                http=http,
                timeout=1,
                total_timeout=0.5,
            )
    assert consumed == min(chunks, 2)
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))
    assert all(response.is_closed for response in responses)


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout(_URL), "read timeout"),
        (httpx.WriteTimeout(_URL), "write timeout"),
        (httpx.ConnectTimeout(_URL), "connect timeout"),
        (httpx.PoolTimeout(_URL), "pool timeout"),
        (OSError(errno.ENOENT, _URL, _URL), "OS error 2:"),
        (OSError(_URL), "filesystem error"),
    ],
)
def test_safe_error_reasons_hide_untrusted_messages_and_tracebacks(
    tmp_path: Path, direction: str, error: Exception, reason: str
) -> None:
    source = tmp_path / "source"
    source.write_bytes(_DATA)

    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    with (
        _api() as client,
        httpx.Client(transport=httpx.MockTransport(fail)) as http,
        pytest.raises(BlobTransferError, match=reason) as caught,
    ):
        if direction == "upload":
            upload_file(client, "acme", source, http=http)
        else:
            download_file(
                client,
                "acme",
                _KEY,
                tmp_path / "out",
                size=len(_DATA),
                sha256=_DIGEST,
                http=http,
            )
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert _URL not in diagnostic
    assert "private-signature" not in diagnostic
    assert caught.value.__cause__ is None
