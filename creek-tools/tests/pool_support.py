"""Hermetic stand-ins for the shared inference pool (#1894).

Every pool test in this suite dials a loopback listener this module opened, or
nothing at all. No vendor is contacted and nothing is billed, which is what
the owner's "no spend yet" decision on ADR-0016 requires (Decision 4).

:class:`FakePool` is a real socket, so a test can assert on what actually left
the process — and can hold a connection open, which is the only way to prove
the cancel handle closes it. :class:`SocketSpy` is the opposite: it refuses
every dial and records where it was aimed, so "zero dials" is an assertion
about the network rather than about a mock.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import TYPE_CHECKING, Final

from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek.classify.llm.providers import pool_identity_digest

if TYPE_CHECKING:
    import pytest

PINNED_RUNTIME_NAME: Final[str] = "creek-test-pool"
"""Runtime name the fake pool and the test manifest agree on."""

PINNED_RUNTIME_VERSION: Final[str] = "0.0.1"
"""Runtime version the fake pool and the test manifest agree on."""

PINNED_MODEL: Final[str] = "creek-test-model:q4"
"""The pinned model reference; always an exact, non-floating tag."""

_SEPARATOR: Final[bytes] = b"\r\n\r\n"
_CHUNK_BYTES: Final[int] = 65536
_DRAIN_TIMEOUT: Final[float] = 5.0
_REQUEST_LINE_FIELDS: Final[int] = 2


def _recv(conn: socket.socket) -> bytes:
    """Read one chunk from *conn*, returning ``b""`` on close or error."""
    try:
        return conn.recv(_CHUNK_BYTES)
    except OSError:
        return b""


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

    Answers each accepted connection with one canned HTTP response, or holds
    it open forever when *hang* is set, so the cancel handle has something
    real to interrupt.

    Attributes:
        accepted: How many connections this listener has accepted.
        requests: The ``(method, path)`` of each request it read, in order —
            so a test can assert that a probe asked for an identity listing
            and never for a generation.
    """

    def __init__(
        self,
        *,
        body: bytes = b"{}",
        status: int = 200,
        hang: bool = False,
    ) -> None:
        """Bind a loopback listener and serve it on a daemon thread.

        Args:
            body: The response body to answer with.
            status: The HTTP status to answer with.
            hang: When true, accept and never answer.
        """
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._body = body
        self._status = status
        self._hang = hang
        self._held: list[socket.socket] = []
        self.accepted = 0
        self.requests: list[tuple[str, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

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
            if not self._hang:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        """Read the whole request, then answer it and close.

        Reading first matters: a listener that answers and closes without
        draining the request body resets the connection, so the client sees a
        transport failure instead of the canned status under test.
        """
        self._record(self._drain(conn))
        try:
            conn.sendall(self._response())
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        conn.close()

    def _record(self, head: bytes) -> None:
        """Note the method and path of one request line."""
        line = head.split(b"\r\n", maxsplit=1)[0].decode("latin-1").split(" ")
        if len(line) >= _REQUEST_LINE_FIELDS:
            self.requests.append((line[0], line[1]))

    @staticmethod
    def _drain(conn: socket.socket) -> bytes:
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
        return head

    def _response(self) -> bytes:
        """Return the canned HTTP/1.1 response bytes."""
        head = (
            f"HTTP/1.1 {self._status} X\r\n"
            f"Content-Length: {len(self._body)}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode("ascii") + self._body

    def close(self) -> None:
        """Stop serving and drop every held connection."""
        self._stop.set()
        self._listener.close()
        for conn in self._held:
            conn.close()


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
