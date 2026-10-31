"""Hermetic stand-ins for the shared inference pool (#1894).

Every pool test in this suite dials a loopback listener this module opened, or
nothing at all. No vendor is contacted and nothing is billed, which is what
the owner's "no spend yet" decision on ADR-0016 requires (Decision 4).

:class:`FakePool` is a real socket, so a test can assert on what actually left
the process — and can hold a connection open, which is the only way to prove
the cancel handle closes it. :class:`SocketSpy` is the opposite: it refuses
every dial and records where it was aimed, so "zero dials" is an assertion
about the network rather than about a mock.

:class:`FakeVendor` belongs here for one reason: ADR-0016 T10 is a claim about
the pool — with a writer's key the vendor answers and **the pool is never
dialled** — so proving it needs a vendor that answers beside a pool that
records. :func:`pin_pool_manifest` is shared for the same reason: both the pin
suite and the route suite need the same pinned identity, and two copies of a
manifest are two things to get out of step.
"""

from __future__ import annotations

import contextlib
import json
import select
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING, Final

import anthropic
import httpx
import openai

from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek.classify.llm.providers import pool_identity_digest
from creek_mcp import model_package
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV, ModelPackageManifest

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    import pytest

PINNED_RUNTIME_NAME: Final[str] = "creek-test-pool"
"""Runtime name the fake pool and the test manifest agree on."""

PINNED_RUNTIME_VERSION: Final[str] = "0.0.1"
"""Runtime version the fake pool and the test manifest agree on."""

PINNED_MODEL: Final[str] = "creek-test-model:q4"
"""The pinned model reference; always an exact, non-floating tag."""

PINNED_EMBED_MODEL: Final[str] = "creek-test-embed"
"""The pinned embedding model id (#1904); vendor ids carry no tag."""

PINNED_EMBED_DIMENSIONS: Final[int] = 4
"""The pinned embedding dimensionality the fake pool answers with."""

PINNED_EMBED_THRESHOLD: Final[float] = 0.5
"""The pinned similarity threshold for the embedding space."""

_SEPARATOR: Final[bytes] = b"\r\n\r\n"
_CHUNK_BYTES: Final[int] = 65536
_DRAIN_TIMEOUT: Final[float] = 5.0
_REQUEST_LINE_FIELDS: Final[int] = 2
_HTTP_OK_STATUS: Final[int] = 200


def _recv(conn: socket.socket) -> bytes:
    """Read one chunk from *conn*, returning ``b""`` on close or error."""
    try:
        return conn.recv(_CHUNK_BYTES)
    except OSError:
        return b""


def _header_map(lines: list[bytes]) -> dict[str, str]:
    """Return one request's headers, lower-cased names to stripped values."""
    headers: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().lower().decode("latin-1")] = value.strip().decode(
                "latin-1"
            )
    return headers


def _content_length(head: bytes) -> int:
    """Return the ``Content-Length`` a request's headers declare, or zero."""
    for line in head.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def pinned_identity_digest(model_name: str = PINNED_MODEL) -> str:
    """Return the manifest digest a matching fake pool would imply.

    Args:
        model_name: The pinned model reference.

    Returns:
        The canonical pool identity digest for the pinned runtime.
    """
    return pool_identity_digest(PINNED_RUNTIME_NAME, PINNED_RUNTIME_VERSION, model_name)


def identity_listing(
    *,
    runtime_name: str = PINNED_RUNTIME_NAME,
    runtime_version: str = PINNED_RUNTIME_VERSION,
    model_name: str = PINNED_MODEL,
) -> dict[str, object]:
    """Return a pool identity listing for one runtime and one model.

    Args:
        runtime_name: The runtime the pool claims to be.
        runtime_version: The version it claims.
        model_name: The model it claims to offer.

    Returns:
        The decoded listing shape the boundary reads.
    """
    return {
        "runtime": {"name": runtime_name, "version": runtime_version},
        "models": [{"name": model_name}],
    }


def pool_listing(*models: str) -> dict[str, object]:
    """Return an identity listing offering every model in *models*.

    Args:
        models: The model ids the pinned runtime claims to serve.

    Returns:
        The decoded listing shape.
    """
    return {
        "runtime": {"name": PINNED_RUNTIME_NAME, "version": PINNED_RUNTIME_VERSION},
        "models": [{"name": name} for name in models],
    }


def fake_vector(text: str, dimensions: int = PINNED_EMBED_DIMENSIONS) -> list[float]:
    """Return a deterministic, non-degenerate vector for *text*.

    Stable across processes (no ``hash``), so a test may compare vectors.

    Args:
        text: The text the fake pool embeds.
        dimensions: The vector length.

    Returns:
        *dimensions* floats derived from the text's characters.
    """
    return [
        float(sum(ord(char) for char in text[i::dimensions]) % 97 + 1)
        for i in range(dimensions)
    ]


def generation(
    *, model_name: str = PINNED_MODEL, text: str = "a reflection"
) -> dict[str, object]:
    """Return a pool generation response.

    Args:
        model_name: The model the pool says answered.
        text: The generated text.

    Returns:
        The decoded generation shape the provider reads.
    """
    return {"model": model_name, "response": text}


class FakePool:
    """A loopback listener standing in for the pool; never a vendor host.

    Answers each accepted connection with one canned HTTP response, holds it
    open forever when *hang* is set, so the cancel handle has something real
    to interrupt, or accepts the whole request and then resets the connection
    when *reset* is set — the pool that takes a prompt and dies mid-request,
    which is the one failure a listener that simply refuses to connect cannot
    stand in for.

    Attributes:
        accepted: How many connections this listener has accepted.
        requests: The ``(method, path)`` of each request it read, in order —
            so a test can assert that a probe asked for an identity listing
            and never for a generation.
        bodies: The decoded JSON body of each request that carried one, in the
            same order — so a test can assert on the closed request shape that
            actually left the process rather than on a mock's arguments.
        headers: The header block of each request, lower-cased names to
            values, in the same order.
    """

    def __init__(
        self,
        *,
        body: bytes = b"{}",
        status: int = 200,
        hang: bool = False,
        reset: bool = False,
    ) -> None:
        """Bind a loopback listener and serve it on a daemon thread.

        Args:
            body: The response body to answer with.
            status: The HTTP status to answer with.
            hang: When true, accept and never answer.
            reset: When true, read the whole request and then reset the
                connection instead of answering it.
        """
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._body = body
        self._status = status
        self._hang = hang
        self._reset = reset
        self._by_path: dict[str, bytes] = {}
        self._held: list[socket.socket] = []
        self.accepted = 0
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[object] = []
        self.headers: list[dict[str, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @classmethod
    def routing(cls, payloads: dict[str, object], *, status: int = 200) -> FakePool:
        """Return a pool that answers a different JSON payload per path.

        One canned body is not enough for a route test: the production
        generation path reads the identity listing *and* then generates, on
        two separate dials, and a listener that answered the same body to both
        could not tell a probe from a prompt.

        Args:
            payloads: Pool path to the decoded body served for it.
            status: The HTTP status to serve every path with.

        Returns:
            A running listener.
        """
        pool = cls(status=status)
        pool._by_path = {
            path: json.dumps(body).encode("utf-8") for path, body in payloads.items()
        }
        return pool

    @classmethod
    def serving(cls, payload: object, *, status: int = 200) -> FakePool:
        """Return a pool that answers *payload* as JSON.

        Args:
            payload: The decoded body to serve.
            status: The HTTP status to serve it with.

        Returns:
            A running listener.
        """
        return cls(body=json.dumps(payload).encode("utf-8"), status=status)

    @property
    def url(self) -> str:
        """The loopback base URL this listener answers on."""
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the Machine-environment destination to this listener.

        Args:
            monkeypatch: Restores the variable when the test finishes.
        """
        monkeypatch.setenv(POOL_URL_ENV, self.url)

    def _serve(self) -> None:
        """Accept connections until stopped, answering or holding each one."""
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.accepted += 1
            self._held.append(conn)
            if self._reset:
                self._abort(conn)
            elif not self._hang:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        """Read the whole request, then answer it and close.

        Reading first matters: a listener that answers and closes without
        draining the request body resets the connection, so the client sees a
        transport failure instead of the canned status under test.
        """
        self._record(self._drain(conn))
        path = self.requests[-1][1] if self.requests else ""
        try:
            conn.sendall(self._response(self._by_path.get(path, self._body)))
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        conn.close()

    def _abort(self, conn: socket.socket) -> None:
        """Read the whole request, then reset the connection without answering.

        ``SO_LINGER`` with a zero timeout makes ``close`` send a TCP ``RST``
        rather than a ``FIN``, so the client's read fails the way a pool that
        died mid-request fails rather than the way a clean close does.
        """
        self._record(self._drain(conn))
        with contextlib.suppress(OSError):
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        conn.close()

    def _record(self, head: bytes) -> None:
        """Note the method, path and headers of one request."""
        lines = head.split(b"\r\n")
        line = lines[0].decode("latin-1").split(" ")
        if len(line) >= _REQUEST_LINE_FIELDS:
            self.requests.append((line[0], line[1]))
        self.headers.append(_header_map(lines[1:]))

    def _record_body(self, body: bytes) -> None:
        """Note the decoded body of one request, when it carried a JSON one."""
        if not body:
            return
        try:
            self.bodies.append(json.loads(body))
        except ValueError:
            self.bodies.append(body.decode("latin-1"))

    def _drain(self, conn: socket.socket) -> bytes:
        """Read one whole HTTP request, and return its header block."""
        conn.settimeout(_DRAIN_TIMEOUT)
        buffer = b""
        while _SEPARATOR not in buffer:
            chunk = _recv(conn)
            if not chunk:
                return buffer
            buffer += chunk
        head, _, body = buffer.partition(_SEPARATOR)
        declared = _content_length(head)
        while len(body) < declared:
            chunk = _recv(conn)
            if not chunk:
                break
            body += chunk
        self._record_body(body)
        return head

    def _response(self, body: bytes | None = None) -> bytes:
        """Return the canned HTTP/1.1 response bytes for *body*."""
        payload = self._body if body is None else body
        head = (
            f"HTTP/1.1 {self._status} X\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode("ascii") + payload

    def held_reached_eof(self, timeout: float) -> bool:
        """Return whether the first held connection was closed by the client.

        The server side of ADR-0016 Decision 1 item 7: when the vault sheds a
        request at its deadline, the cancel handle shuts the socket down, so
        the *pool* sees the connection end. Without that wiring the worker
        stays blocked on its own read and this returns ``False`` — which is
        what makes it a regression test rather than a tautology.

        Args:
            timeout: Seconds to wait for the end of the connection.

        Returns:
            ``True`` when the held socket reached end-of-stream or errored
            within *timeout*.
        """
        if not self._held:
            return False
        conn = self._held[0]
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([conn], [], [], remaining)
            # A held connection still has the unread request in its buffer, so
            # the first readable event is that, not the close. Drain until the
            # stream actually ends.
            if readable and _recv(conn) == b"":
                return True

    def close(self) -> None:
        """Stop serving and drop every held connection."""
        self._stop.set()
        self._listener.close()
        for conn in self._held:
            conn.close()


class EmbeddingPool(FakePool):
    """A fake pool that answers ``/v1/embeddings`` with one vector per input.

    Every other path answers its canned payload as :meth:`FakePool.routing`
    does. ``fail_embeddings_after`` makes every embeddings request after that
    many successes answer HTTP 503, so a test can interrupt a pass mid-way.

    Attributes:
        embed_model: The model the answer echoes.
        dimensions: The vector length answered.
    """

    def __init__(
        self,
        payloads: dict[str, object],
        *,
        embed_model: str = PINNED_EMBED_MODEL,
        dimensions: int = PINNED_EMBED_DIMENSIONS,
        fail_embeddings_after: int | None = None,
    ) -> None:
        """Serve *payloads* by path and compute embeddings answers.

        Args:
            payloads: Pool path to the decoded body served for it.
            embed_model: The model the embeddings answer names.
            dimensions: The vector length answered.
            fail_embeddings_after: Successful embeddings answers before every
                later one answers 503, or ``None`` to never fail.
        """
        self.embed_model = embed_model
        self.dimensions = dimensions
        self._fail_after = fail_embeddings_after
        self._embeds_answered = 0
        super().__init__()
        self._by_path = {
            path: json.dumps(body).encode("utf-8") for path, body in payloads.items()
        }

    def embedding_bodies(self) -> list[dict[str, Any]]:
        """Return the decoded body of every embeddings request, in order."""
        return [
            body
            for (_method, path), body in zip(
                self.requests, self._bodies_by_request(), strict=True
            )
            if path == "/v1/embeddings" and isinstance(body, dict)
        ]

    def _bodies_by_request(self) -> list[object]:
        """Return one body (or ``None``) per recorded request, aligned."""
        bodies = iter(self.bodies)
        return [
            next(bodies, None) if method == "POST" else None
            for method, _path in self.requests
        ]

    def _answer(self, conn: socket.socket) -> None:
        """Answer embeddings requests from their own inputs; others as canned."""
        self._record(self._drain(conn))
        path = self.requests[-1][1] if self.requests else ""
        status = self._status
        if path == "/v1/embeddings":
            payload, status = self._embeddings_answer()
        else:
            payload = self._by_path.get(path, self._body)
        try:
            conn.sendall(self._raw_response(payload, status))
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        conn.close()

    def _embeddings_answer(self) -> tuple[bytes, int]:
        """Return the answer for the embeddings request just recorded."""
        if self._fail_after is not None and self._embeds_answered >= self._fail_after:
            return b"{}", 503
        self._embeds_answered += 1
        body = self.bodies[-1] if self.bodies else {}
        inputs = body.get("input", []) if isinstance(body, dict) else []
        data = [
            {"index": index, "embedding": fake_vector(text, self.dimensions)}
            for index, text in enumerate(inputs)
        ]
        answer = {"model": self.embed_model, "data": data}
        return json.dumps(answer).encode("utf-8"), _HTTP_OK_STATUS

    @staticmethod
    def _raw_response(payload: bytes, status: int) -> bytes:
        """Return a whole HTTP/1.1 response for *payload* at *status*."""
        head = (
            f"HTTP/1.1 {status} X\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode("ascii") + payload


class SocketSpy:
    """Record every TCP destination a dial attempts, and refuse all of them.

    Attributes:
        destinations: The ``(host, port)`` pairs that were attempted.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace :func:`socket.create_connection` with the recorder.

        Args:
            monkeypatch: Restores the real function when the test finishes.
        """
        self.destinations: list[tuple[str, int]] = []
        monkeypatch.setattr(socket, "create_connection", self._connect)

    @property
    def hosts(self) -> list[str]:
        """The hostnames that were dialled, in order."""
        return [host for host, _port in self.destinations]

    def _connect(
        self, address: tuple[str, int], *_args: object, **_kwargs: object
    ) -> socket.socket:
        """Record *address* and refuse the connection.

        Args:
            address: The destination the caller aimed at.

        Returns:
            Never; the call always raises.

        Raises:
            OSError: Always, so the caller's refusal path runs.
        """
        self.destinations.append(address)
        msg = "the socket spy refuses every dial"
        raise OSError(msg)


def embedding_pin(**overrides: object) -> dict[str, object]:
    """Return a manifest ``embedding`` block matching :class:`EmbeddingPool`.

    Args:
        overrides: Fields to replace, to pin a mismatching identity.

    Returns:
        The JSON-ready block.
    """
    return {
        "model_name": PINNED_EMBED_MODEL,
        "dimensions": PINNED_EMBED_DIMENSIONS,
        "similarity_threshold": PINNED_EMBED_THRESHOLD,
        "runtime_inventory_digest": pinned_identity_digest(PINNED_EMBED_MODEL),
        **overrides,
    }


def pin_pool_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inventory_digest: str | None = None,
    model_name: str = PINNED_MODEL,
    embedding: dict[str, object] | None = None,
) -> ModelPackageManifest:
    """Configure an approved model-package manifest pinning the pool's identity.

    Shared by the pin suite and the route suite: the digest a matching fake
    pool implies is derived here rather than restated, so a change to
    :data:`~creek.classify.llm.providers.POOL_IDENTITY_SCHEME` cannot leave one
    suite pinning a stale identity the other has moved past.

    Args:
        tmp_path: The test's temporary directory; the manifest is written
            under it.
        monkeypatch: Restores the environment and the licence allowlist.
        inventory_digest: The digest to pin, or ``None`` for the one a
            matching pool implies.
        model_name: The model reference to pin.
        embedding: The optional embedding pin (#1904), or ``None`` for a
            manifest that pins no attested embedding model.

    Returns:
        The parsed manifest.
    """
    fields = {
        "schema_version": 1,
        "runtime_name": PINNED_RUNTIME_NAME,
        "runtime_version": PINNED_RUNTIME_VERSION,
        "runtime_digest": "c" * 64,
        "model_name": model_name,
        "model_blob_sha256": "b" * 64,
        "runtime_inventory_digest": inventory_digest
        or pinned_identity_digest(model_name),
        "quantization": "Q4_K_M",
        "parameter_count": 1_000_000,
        "size_bytes": 4096,
        "license_spdx": "Apache-2.0",
        "license_url": "https://example.test/LICENSE",
    }
    if embedding is not None:
        fields["embedding"] = embedding
    path = tmp_path / "model-package" / "manifest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    monkeypatch.setattr(
        model_package, "APPROVED_MODEL_LICENSES", frozenset({"Apache-2.0"})
    )
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(path))
    return ModelPackageManifest.model_validate(fields)


class FakeVendor:
    """A fake Anthropic/OpenAI API behind the real SDK, recording requests.

    The real client classes run end to end — their auth headers, their error
    mapping — with only the socket replaced by an
    :class:`httpx.MockTransport`. Nothing is billed and no vendor host is
    resolved.

    Attributes:
        requests: Every request the SDK made, in order.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status: int = 200,
        turn: str = "{}",
    ) -> None:
        """Replace both vendor SDKs' transports with the recorder.

        Args:
            monkeypatch: Restores the real client classes.
            status: The HTTP status to answer with; anything but 200 answers
                an error body the SDK raises on.
            turn: The assistant text to answer a 200 with.
        """
        self.requests: list[httpx.Request] = []
        self._status = status
        self._turn = turn
        real_anthropic = anthropic.Anthropic
        real_openai = openai.OpenAI
        transport = httpx.MockTransport(self._answer)

        def _anthropic(**kwargs: Any) -> anthropic.Anthropic:
            return real_anthropic(
                http_client=httpx.Client(transport=transport), **kwargs
            )

        def _openai(**kwargs: Any) -> openai.OpenAI:
            return real_openai(http_client=httpx.Client(transport=transport), **kwargs)

        monkeypatch.setattr(anthropic, "Anthropic", _anthropic)
        monkeypatch.setattr(openai, "OpenAI", _openai)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it, or answer the configured failure."""
        self.requests.append(request)
        if self._status != _HTTP_OK_STATUS:
            return httpx.Response(
                self._status,
                json={
                    "type": "error",
                    "error": {"type": "api_error", "message": "vendor is unwell"},
                },
            )
        return httpx.Response(
            _HTTP_OK_STATUS,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": self._turn}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
