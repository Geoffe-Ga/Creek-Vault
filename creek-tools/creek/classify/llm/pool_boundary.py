"""The shared inference pool's one pinned destination beyond loopback (#1894).

A managed vault ships no inference runtime and a 1 GB Machine cannot hold a
model, so ADR-0016 moves the warm weights into a shared, vendor-attested pool.
That pool is the **only** destination a vault dials beyond loopback, and this
module is the only place a request to it is built. It is to the pool what
:mod:`creek.classify.llm.local_boundary` is to Ollama.

Four properties are owned here rather than left to call sites:

- **The destination.** :data:`POOL_URL_ENV` is read from the Machine
  environment and nothing else. No function here accepts an
  :class:`~creek.config.LLMConfig`, so an edit to the writable vault config
  cannot move the destination (ADR-0016 Decision 1 item 2). Any URL a caller
  offers is admitted only when it is byte-for-byte the pinned destination
  (:func:`admit_pool_url`); every refusal carries a fixed, URL-free message.
- **The transport.** The dial is :mod:`http.client`, which — unlike a
  general-purpose client — reads no proxy environment variable at all, follows
  no redirect, and retries nothing. A pinned destination that could be bounced
  to a proxy or a ``Location`` header would not be pinned.
- **The budget.** A connect timeout of :data:`POOL_CONNECT_TIMEOUT` and a
  request timeout of :data:`POOL_REQUEST_TIMEOUT`, each further capped by the
  vault request's remaining budget, and **no retries** (ADR-0016 R2,
  Decision 1 item 2).
- **Cancellation.** :meth:`PoolDial.cancel` shuts the socket down from the
  event loop, so the worker thread blocked on the read actually exits.
  Abandoning the thread — which is all a bare off-loop read can do — leaves a
  vendor call running after the vault has already answered (Decision 1
  item 7).

``tests/test_ollama_endpoint_invariant.py`` keeps this module the only place a
pool path or :data:`POOL_URL_ENV` is turned into a request.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping
    from urllib.parse import SplitResult

POOL_URL_ENV: Final[str] = "CREEK_POOL_URL"
"""Machine-environment key holding the one pinned pool destination.

Under a vendor-attested pool this is the exact vendor hostname whose
measurement set the manifest pins. Under a vendor that ships a loopback proxy
sidecar it is that sidecar's loopback address, and the sidecar's single egress
destination is pinned by the same key on the Machine.
"""

POOL_CONNECT_TIMEOUT: Final[float] = 1.0
"""Seconds allowed to establish the pool connection."""

POOL_REQUEST_TIMEOUT: Final[float] = 12.0
"""Seconds allowed for the pool to answer once connected."""

POOL_MAX_REQUEST_BYTES: Final[int] = 1 << 20
"""Hard ceiling on an encoded pool request body (ADR-0016 Decision 1 item 3)."""

POOL_MODELS_PATH: Final[str] = "/v1/models"
"""The pool's identity listing: which runtime serves which models."""

POOL_GENERATE_PATH: Final[str] = "/v1/generate"
"""The pool's single generation endpoint."""

_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_GET: Final[str] = "GET"
_POST: Final[str] = "POST"
_JSON: Final[str] = "application/json"
_HTTP_OK: Final[int] = 200

_NO_DESTINATION: Final[str] = (
    "no shared inference pool destination is configured in the Machine "
    "environment; refusing to dial"
)
_FOREIGN_DESTINATION: Final[str] = (
    "the offered inference URL is not the pinned pool destination; refusing to dial"
)
_TOO_LARGE: Final[str] = (
    "the pool request exceeds the boundary's size cap; refusing to dial"
)
_EXHAUSTED: Final[str] = (
    "the vault request's budget is already spent; refusing to dial the pool"
)
_CANCELLED: Final[str] = "the pool dial was cancelled at the vault's deadline"
_UNREACHABLE: Final[str] = "the shared inference pool did not answer usably"


class PoolBoundaryError(OSError):
    """A refused, cancelled or unusable pool dial.

    Subclasses :class:`OSError` so a call site that already degrades on a
    transport failure degrades the same way here. Every message is a fixed
    string: none of them carries the destination, the prompt or a response
    body, so a log line or a structured refusal cannot become an oracle for
    where the vault dials or what it sent.
    """


class PoolDestinationError(PoolBoundaryError):
    """No pinned destination is configured, or a foreign URL was offered."""


class PoolRequestTooLargeError(PoolBoundaryError):
    """The encoded request body exceeds :data:`POOL_MAX_REQUEST_BYTES`."""


class PoolDialCancelledError(PoolBoundaryError):
    """The vault's deadline fired, so the dial was cancelled or refused."""


class PoolUnreachableError(PoolBoundaryError):
    """The pool could not be reached, or answered something undecodable."""


@dataclass(frozen=True)
class PoolResponse:
    """One fully read pool response.

    Attributes:
        status: The HTTP status code the pool answered with.
        payload: The decoded JSON body.
    """

    status: int
    payload: object


def pool_url_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether a pool destination is set in the Machine environment.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        ``True`` when :data:`POOL_URL_ENV` holds a non-blank value. Whether
        that value is *well formed* is :func:`pool_destination`'s question.
    """
    source = os.environ if environ is None else environ
    return bool(source.get(POOL_URL_ENV, "").strip())


def pool_destination(environ: Mapping[str, str] | None = None) -> str:
    """Return the one pinned pool destination, normalised.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The destination's scheme, host and port, with any trailing slash
        removed.

    Raises:
        PoolDestinationError: When :data:`POOL_URL_ENV` is unset, blank or not
            a bare HTTP(S) origin. The message never echoes the value.
    """
    source = os.environ if environ is None else environ
    raw = source.get(POOL_URL_ENV, "").strip().rstrip("/")
    if not raw:
        raise PoolDestinationError(_NO_DESTINATION)
    _require_parts(raw)
    return raw


def admit_pool_url(url: str, environ: Mapping[str, str] | None = None) -> str:
    """Return *url* only when it is exactly the pinned pool destination.

    The runtime half of "one destination beyond loopback": any other hostname,
    any IP literal and any URL that reached the process through vault config
    is refused here, whatever it points at.

    Args:
        url: The destination a caller offers.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        *url* unchanged, when it matches the pinned destination.

    Raises:
        PoolDestinationError: When no destination is pinned, or *url* is not
            it. The message echoes neither URL.
    """
    if url.strip().rstrip("/") != pool_destination(environ):
        raise PoolDestinationError(_FOREIGN_DESTINATION)
    return url


def _require_parts(destination: str) -> SplitResult:
    """Return *destination*'s parts, refusing anything but a bare origin.

    Args:
        destination: The already-stripped destination string.

    Returns:
        The parsed parts, guaranteed to carry an HTTP(S) scheme and a host.

    Raises:
        PoolDestinationError: On a bad scheme, a missing host, embedded
            credentials, a malformed port, or a path, query or fragment.
    """
    try:
        parts = urlsplit(destination)
        port = parts.port
    except ValueError as exc:
        raise PoolDestinationError(_NO_DESTINATION) from exc
    if parts.scheme not in _SCHEMES or not parts.hostname:
        raise PoolDestinationError(_NO_DESTINATION)
    if "@" in parts.netloc or parts.path or parts.query or parts.fragment:
        raise PoolDestinationError(_NO_DESTINATION)
    if port is None and parts.netloc.endswith(":"):
        raise PoolDestinationError(_NO_DESTINATION)
    return parts


def _pool_verify() -> ssl.SSLContext:
    """Return the CA trust a pool connection verifies TLS against.

    ``SSL_CERT_FILE`` wins over ``SSL_CERT_DIR``, matching the local
    boundary, so an operator-supplied private CA is honoured identically on
    both boundaries. Neither variable can redirect the dial — only widen what
    certificate the pinned host may present.
    """
    cafile = os.environ.get("SSL_CERT_FILE", "")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    capath = os.environ.get("SSL_CERT_DIR", "")
    if capath:
        return ssl.create_default_context(capath=capath)
    return ssl.create_default_context()


def _encode(payload: Mapping[str, object] | None) -> bytes | None:
    """Return *payload* as compact JSON bytes, or ``None`` for a bodiless call.

    Args:
        payload: The request body, or ``None``.

    Returns:
        The encoded body, or ``None``.

    Raises:
        PoolRequestTooLargeError: When the encoding exceeds
            :data:`POOL_MAX_REQUEST_BYTES` — raised before any socket is
            opened.
    """
    if payload is None:
        return None
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > POOL_MAX_REQUEST_BYTES:
        raise PoolRequestTooLargeError(_TOO_LARGE)
    return body


def _capped(ceiling: float, budget: float | None) -> float:
    """Return *ceiling* capped by the vault request's remaining *budget*.

    Args:
        ceiling: The boundary's own timeout for this leg.
        budget: Seconds left in the vault request, or ``None`` when the caller
            tracks no deadline.

    Returns:
        The smaller of the two.

    Raises:
        PoolDialCancelledError: When *budget* is already spent.
    """
    if budget is None:
        return ceiling
    if budget <= 0:
        raise PoolDialCancelledError(_EXHAUSTED)
    return min(ceiling, budget)


class PoolDial:
    """One pool connection, plus the handle that cancels it.

    :meth:`request` blocks on the pool; :meth:`cancel` is safe to call from
    another thread — typically the event loop that owns the vault request's
    deadline — and shuts the socket down so the blocked reader raises instead
    of being abandoned.

    A dial is **single-use**: :meth:`request` closes the connection on its way
    out, success or failure, so there is no pool of warm sockets to a vendor
    host and a retry would have to be an explicit new dial. There are no
    retries, so there is none.

    Attributes:
        connect_timeout: Seconds allowed to establish the connection.
        request_timeout: Seconds allowed for the answer, once connected.
    """

    def __init__(
        self,
        destination: str,
        *,
        connect_timeout: float,
        request_timeout: float,
    ) -> None:
        """Pin this dial to *destination* and its two timeouts.

        Args:
            destination: The already-admitted pool origin.
            connect_timeout: Seconds allowed to establish the connection.
            request_timeout: Seconds allowed for the answer.

        Raises:
            PoolDestinationError: When *destination* is not a bare origin.
        """
        self._parts = _require_parts(destination)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._cancelled = False

    def __enter__(self) -> PoolDial:
        """Return this dial for use in a ``with`` block."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the connection however the block ended."""
        self.close()

    def request(
        self, path: str, *, payload: Mapping[str, object] | None
    ) -> PoolResponse:
        """Send one request to *path* and read its whole answer.

        Exactly one attempt is made: a failure is a refusal, never a retry
        against the pool or anywhere else.

        Args:
            path: A pool path owned by this module.
            payload: The JSON body, or ``None`` for a ``GET``.

        Returns:
            The status and decoded body.

        Raises:
            PoolRequestTooLargeError: When the body exceeds the size cap.
            PoolDialCancelledError: When the dial was cancelled, before or during.
            PoolUnreachableError: On a transport failure or an undecodable
                body.
        """
        body = _encode(payload)
        connection = self._connect()
        try:
            connection.request(
                _POST if body is not None else _GET,
                path,
                body=body,
                headers=self._headers(body),
            )
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise self._failure() from exc
        finally:
            self.close()
        return PoolResponse(status=status, payload=_decode(raw))

    def cancel(self) -> None:
        """Shut the pool socket down so a blocked reader raises and exits.

        Idempotent, and safe before the connection exists or after it has been
        closed. A dial cancelled before it starts never opens a socket.
        """
        with self._lock:
            self._cancelled = True
            connection = self._connection
        if connection is None:
            return
        sock = connection.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        """Release the connection; idempotent."""
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _headers(self, body: bytes | None) -> dict[str, str]:
        """Return the fixed request headers; no credential is added here."""
        headers = {"Accept": _JSON}
        if body is not None:
            headers["Content-Type"] = _JSON
        return headers

    def _failure(self) -> PoolBoundaryError:
        """Return the refusal that matches why this dial ended."""
        with self._lock:
            cancelled = self._cancelled
        if cancelled:
            return PoolDialCancelledError(_CANCELLED)
        return PoolUnreachableError(_UNREACHABLE)

    def _connect(self) -> http.client.HTTPConnection:
        """Open the pinned connection and arm its request timeout.

        Returns:
            The connected client.

        Raises:
            PoolDialCancelledError: When the dial was already cancelled.
            PoolUnreachableError: When the connection cannot be established.
        """
        with self._lock:
            if self._cancelled:
                raise PoolDialCancelledError(_CANCELLED)
        connection = self._open()
        with self._lock:
            if self._cancelled:
                connection.close()
                raise PoolDialCancelledError(_CANCELLED)
            self._connection = connection
        if connection.sock is not None:
            connection.sock.settimeout(self.request_timeout)
        return connection

    def _open(self) -> http.client.HTTPConnection:
        """Establish the TCP (and, for ``https``, TLS) connection.

        Returns:
            The connected client.

        Raises:
            PoolUnreachableError: On any connection failure.
        """
        host = self._parts.hostname or ""
        connection: http.client.HTTPConnection
        if self._parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                host,
                self._parts.port,
                timeout=self.connect_timeout,
                context=_pool_verify(),
            )
        else:
            connection = http.client.HTTPConnection(
                host, self._parts.port, timeout=self.connect_timeout
            )
        try:
            connection.connect()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise PoolUnreachableError(_UNREACHABLE) from exc
        return connection


def _decode(raw: bytes) -> object:
    """Return *raw* decoded as JSON.

    Args:
        raw: The response body.

    Returns:
        The decoded value.

    Raises:
        PoolUnreachableError: When the body is not JSON. The message carries
            none of it.
    """
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PoolUnreachableError(_UNREACHABLE) from exc


def open_pool_dial(
    *, budget: float | None = None, environ: Mapping[str, str] | None = None
) -> PoolDial:
    """Return a dial pinned to the configured destination.

    Args:
        budget: Seconds left in the vault request, which caps both timeouts.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        An unconnected :class:`PoolDial`; it connects on first request.

    Raises:
        PoolDestinationError: When no destination is pinned.
        PoolDialCancelledError: When *budget* is already spent.
    """
    return PoolDial(
        pool_destination(environ),
        connect_timeout=_capped(POOL_CONNECT_TIMEOUT, budget),
        request_timeout=_capped(POOL_REQUEST_TIMEOUT, budget),
    )


def pool_list_models(
    *, budget: float | None = None, environ: Mapping[str, str] | None = None
) -> PoolResponse:
    """Read the pool's identity listing from the pinned destination.

    Args:
        budget: Seconds left in the vault request.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The listing response.

    Raises:
        PoolBoundaryError: As :meth:`PoolDial.request`.
    """
    with open_pool_dial(budget=budget, environ=environ) as dial:
        return dial.request(POOL_MODELS_PATH, payload=None)


def pool_generate(
    payload: Mapping[str, object],
    *,
    budget: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> PoolResponse:
    """Send one generation request to the pinned destination.

    Args:
        payload: The JSON request body.
        budget: Seconds left in the vault request.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The generation response.

    Raises:
        PoolBoundaryError: As :meth:`PoolDial.request`.
    """
    with open_pool_dial(budget=budget, environ=environ) as dial:
        return dial.request(POOL_GENERATE_PATH, payload=payload)


def pool_response_ok(response: PoolResponse) -> bool:
    """Return whether *response* is the pool's success status.

    Args:
        response: A response from this boundary.

    Returns:
        ``True`` only for HTTP 200.
    """
    return response.status == _HTTP_OK
