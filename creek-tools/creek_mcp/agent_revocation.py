"""Agent-token revocation, checked on every call behind a short cache (#2005).

Owner decision **R3-2**: the vault checks agent revocation on every call. An
agent stopped in Adepthood Settings must be refused from its next call onward,
not when its ten-minute access token runs out.
:class:`~creek_mcp.agent_tokens.JwtAgentTokenVerifier` asks a
:class:`RevocationCache` after every offline rule has passed and before it
returns an access token, so the edge (before it prepares any replay) and a
vault Machine (before any tool is listed or dispatched) both refuse a
withdrawn grant at the same single verification point.

**What is asked.** Adepthood's
``POST /internal/vault-provisioning/agent-token-status`` (adepthood#3630)
answers ``{"active": bool}`` for the verified token's ``sub``, ``client_id``
and ``iat`` — its own coverage rule (grant unrevoked and approved no later than
``iat``) needs nothing more. The token itself never leaves the vault, not even
to its issuer. The request is authenticated by the control plane's mounted
handoff bearer, the same credential the pilot's alerts use; no user secret is
ever in it.

**The cache** (:class:`RevocationCache`), keyed by ``(sub, client_id, iat)``
and bounded to :data:`REVOCATION_CACHE_MAX_ENTRIES` (least recently used is
evicted first):

* ``active`` is reused for :data:`REVOCATION_CACHE_SECONDS`, which is therefore
  the longest a revocation can take to land. ``0`` asks on every call.
* ``revoked`` is kept until the token expires: a withdrawn grant never covers
  that token again, and a re-approval produces a new ``iat``.
* No answer — a ``401``, ``400`` or ``429``, a ``5xx``, a redirect, a timeout,
  a malformed body — is **never** cached and refuses the call (fail closed).
  An issuer outage therefore refuses agents once their cached ``active``
  answers age out. Fail-open would quietly cancel R3-2, and an agent cannot
  obtain a fresh token from an issuer that is down either.
* Concurrent misses for one key share one request, so a session's opening
  burst spends one request of Adepthood's per-caller budget.

**Nothing identifying is logged.** The verifier logs a refusal by its fixed
category (``revoked`` / ``revocation unavailable``); this module logs nothing,
and :class:`TokenStatusQuery`'s ``repr`` names no subject and no client.

**Configuration** (default off): a vault Machine reads
:data:`STATUS_URL_ENV` and :data:`STATUS_BEARER_FILE_ENV`
(:func:`load_revocation_cache`); the edge router takes the matching
``--oauth-agent-token-status-*`` flags. With neither, tokens are verified
offline exactly as before, and startup warns that revocation is not enforced.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

REVOCATION_CACHE_SECONDS: Final[int] = 30
"""How long an ``active`` answer is reused: the bound on revocation latency."""

REVOCATION_CACHE_MAX_ENTRIES: Final[int] = 4096
"""The most ``(sub, client_id, iat)`` answers held at once."""

REVOCATION_TIMEOUT_SECONDS: Final[float] = 5.0
"""How long one status lookup may take before the call is refused."""

AGENT_TOKEN_STATUS_PATH: Final[str] = "/internal/vault-provisioning/agent-token-status"
"""Adepthood's status route (adepthood#3630); the only path the bearer goes to."""

STATUS_URL_ENV: Final[str] = "CREEK_MCP_AGENT_TOKEN_STATUS_URL"
"""A vault Machine's status URL: Adepthood's origin plus the status path."""

STATUS_BEARER_FILE_ENV: Final[str] = "CREEK_MCP_AGENT_TOKEN_STATUS_BEARER_FILE"
"""The owner-only file holding the mounted handoff bearer."""

MAX_ISSUED_AT: Final[int] = 32_503_680_000
"""The largest ``iat`` the status contract admits (year 3000)."""

MAX_SUBJECT_CHARS: Final[int] = 200
"""The longest ``sub`` the status contract admits."""

MAX_CLIENT_ID_CHARS: Final[int] = 2048
"""The longest ``client_id`` the status contract admits."""

_MAX_STATUS_BODY_BYTES: Final[int] = 1024
_HTTP_OK: Final[int] = 200
_ACTIVE_KEY: Final[str] = "active"
_UNAVAILABLE: Final[str] = "agent-token status is unavailable"
_ROUTE_REQUIRED: Final[str] = (
    "agent-token status URL must use the canonical HTTPS route"
)


class RevocationUnavailableError(OSError):
    """The status lookup got no usable answer. Names no URL and no identity."""

    def __init__(self) -> None:
        """Carry the one fixed message."""
        super().__init__(_UNAVAILABLE)


class Revocation(StrEnum):
    """What the check decided; the value is the verifier's log category."""

    ACTIVE = "active"
    REVOKED = "revoked"
    UNAVAILABLE = "revocation unavailable"


def _bounded_text(value: object, limit: int) -> str | None:
    """Return *value* when it is a non-empty string of at most *limit* chars.

    Args:
        value: A claim.
        limit: The contract's bound.

    Returns:
        The string, or ``None``.
    """
    if isinstance(value, str) and 0 < len(value) <= limit:
        return value
    return None


@dataclass(frozen=True, slots=True)
class TokenStatusQuery:
    """The verified claims one status lookup is about, and the cache key.

    Attributes:
        subject: The token's ``sub``.
        client_id: The token's ``client_id``.
        issued_at: The token's ``iat``, whole seconds.
    """

    subject: str = field(repr=False)
    client_id: str = field(repr=False)
    issued_at: int = field(repr=False)

    @classmethod
    def from_claims(
        cls, subject: object, client_id: object, issued_at: object
    ) -> TokenStatusQuery | None:
        """Return the query for verified claims, when the contract can carry them.

        Args:
            subject: The ``sub`` claim.
            client_id: The ``client_id`` claim.
            issued_at: The ``iat`` claim.

        Returns:
            The query, or ``None`` for a claim Adepthood would refuse ``400``:
            an ``iat`` that is not a strict integer in ``[0, MAX_ISSUED_AT]``
            (a boolean or a fraction included), or a blank or over-long
            ``sub`` or ``client_id``.
        """
        sub = _bounded_text(subject, MAX_SUBJECT_CHARS)
        client = _bounded_text(client_id, MAX_CLIENT_ID_CHARS)
        if (
            sub is None
            or client is None
            or type(issued_at) is not int
            or not 0 <= issued_at <= MAX_ISSUED_AT
        ):
            return None
        return cls(sub, client, issued_at)

    def body(self) -> dict[str, object]:
        """Return the status request's JSON body.

        Returns:
            ``{"sub", "client_id", "iat"}``, exactly as verified.
        """
        return {"sub": self.subject, "client_id": self.client_id, "iat": self.issued_at}


class RevocationChecker(Protocol):
    """Answer whether a verified token's grant still stands."""

    async def is_active(self, query: TokenStatusQuery) -> bool:
        """Ask the issuer.

        Args:
            query: The verified claims.

        Returns:
            ``True`` while the grant covers the token.

        Raises:
            OSError: When no usable answer was obtained.
        """


@dataclass(frozen=True, slots=True)
class _Entry:
    """One cached answer and when it was written."""

    active: bool
    stored_at: float
    expires_at: float


class RevocationCache:
    """Bounded, short-lived answers from a :class:`RevocationChecker`."""

    def __init__(
        self,
        checker: RevocationChecker,
        *,
        ttl_seconds: float = REVOCATION_CACHE_SECONDS,
        max_entries: int = REVOCATION_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the checker and the cache policy.

        Args:
            checker: Where answers come from.
            ttl_seconds: How long an ``active`` answer is reused; ``0`` never.
            max_entries: The bound on cached answers.
            clock: The wall clock (``exp`` is wall time), injectable for tests.

        Raises:
            ValueError: For a negative TTL or a non-positive bound.
        """
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._checker = checker
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[TokenStatusQuery, _Entry] = OrderedDict()
        self._inflight: dict[TokenStatusQuery, asyncio.Future[bool]] = {}

    def __len__(self) -> int:
        """Return how many answers are held.

        Returns:
            The entry count, never above the bound.
        """
        return len(self._entries)

    async def status(
        self, query: TokenStatusQuery, *, expires_at: int | None
    ) -> Revocation:
        """Return whether *query*'s token may be served now.

        Args:
            query: The verified claims.
            expires_at: The last second the token is accepted, which bounds a
                cached ``revoked`` answer; ``None`` caches no such answer.

        Returns:
            :attr:`Revocation.ACTIVE` from a fresh entry or a lookup,
            :attr:`Revocation.REVOKED`, or :attr:`Revocation.UNAVAILABLE`
            when no fresh entry exists and the lookup failed.
        """
        cached = self._fresh(query)
        if cached is not None:
            return cached
        try:
            active = await self._lookup(query)
        except OSError:
            return Revocation.UNAVAILABLE
        self._remember(query, active=active, expires_at=expires_at)
        return Revocation.ACTIVE if active else Revocation.REVOKED

    def _fresh(self, query: TokenStatusQuery) -> Revocation | None:
        """Return the cached answer for *query* while it may still be used.

        An ``active`` entry is fresh for the TTL after it was written, and
        never when the clock reads earlier than that write: a clock stepped
        backwards does not stretch it.

        Args:
            query: The cache key.

        Returns:
            The answer, or ``None`` (and the stale entry dropped).
        """
        entry = self._entries.get(query)
        if entry is None:
            return None
        now = self._clock()
        if entry.active:
            fresh = 0 <= now - entry.stored_at < self._ttl
        else:
            fresh = now < entry.expires_at
        if not fresh:
            del self._entries[query]
            return None
        self._entries.move_to_end(query)
        return Revocation.ACTIVE if entry.active else Revocation.REVOKED

    async def _lookup(self, query: TokenStatusQuery) -> bool:
        """Ask the checker, sharing one in-flight request per key.

        A waiter that is cancelled leaves the shared request running for the
        others; a finished request is never reused, so a failure is not
        remembered.

        Args:
            query: The verified claims.

        Returns:
            The checker's answer.
        """
        pending = self._inflight.get(query)
        if pending is None or pending.done():
            pending = asyncio.ensure_future(self._checker.is_active(query))
            self._inflight[query] = pending
        try:
            return await asyncio.shield(pending)
        finally:
            if pending.done() and self._inflight.get(query) is pending:
                del self._inflight[query]

    def _remember(
        self, query: TokenStatusQuery, *, active: bool, expires_at: int | None
    ) -> None:
        """Cache one answer, evicting the least recently used past the bound.

        Args:
            query: The cache key.
            active: The answer.
            expires_at: The token's last accepted second, or ``None``.
        """
        if (active and self._ttl <= 0) or (not active and expires_at is None):
            return
        now = self._clock()
        until = now + self._ttl if active else float(expires_at or 0)
        self._entries[query] = _Entry(active, now, until)
        self._entries.move_to_end(query)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def _status_url(value: str) -> str:
    """Return *value* when it is exactly the canonical HTTPS status route.

    Args:
        value: The configured URL.

    Returns:
        The URL, normalised by :class:`httpx.URL`.

    Raises:
        ValueError: Unless it is ``https`` with a host, the exact
            :data:`AGENT_TOKEN_STATUS_PATH`, and no credentials, query or
            fragment. The message never echoes the value.
    """
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError(_ROUTE_REQUIRED) from None
    if (
        url.scheme != "https"
        or not url.host
        or url.path != AGENT_TOKEN_STATUS_PATH
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError(_ROUTE_REQUIRED)
    return str(url)


def _answer(body: bytes) -> bool:
    """Read Adepthood's one-key answer.

    Args:
        body: The response body.

    Returns:
        The ``active`` boolean.

    Raises:
        RevocationUnavailableError: Unless the body is exactly
            ``{"active": <bool>}``.
    """
    try:
        document = json.loads(body)
    except ValueError:
        raise RevocationUnavailableError from None
    if not isinstance(document, dict) or set(document) != {_ACTIVE_KEY}:
        raise RevocationUnavailableError
    active = document[_ACTIVE_KEY]
    if not isinstance(active, bool):
        raise RevocationUnavailableError
    return active


async def _bounded_body(response: httpx.Response) -> bytes:
    """Read *response*'s body, refusing one over :data:`_MAX_STATUS_BODY_BYTES`.

    Args:
        response: A streamed response.

    Returns:
        The body.

    Raises:
        RevocationUnavailableError: When the body exceeds the cap.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_STATUS_BODY_BYTES:
            raise RevocationUnavailableError
        chunks.append(chunk)
    return b"".join(chunks)


class HttpRevocationChecker:
    """Ask Adepthood's status route with the mounted handoff bearer.

    Follows :class:`~creek_mcp.provisioning.pilot_alerts.HttpFleetAlertSink`'s
    conventions: the exact HTTPS route, the bearer read from an owner-only
    file, a bounded timeout, no redirects, and nothing logged — neither the
    request nor the answer.
    """

    def __init__(
        self,
        status_url: str,
        bearer_file: Path,
        *,
        timeout: float = REVOCATION_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Validate the route and load the bearer.

        Args:
            status_url: Adepthood's origin plus :data:`AGENT_TOKEN_STATUS_PATH`.
            bearer_file: The owner-only file holding the handoff bearer.
            timeout: Seconds one lookup may take.
            transport: An injected transport, for tests.

        Raises:
            ValueError: For any other URL, or an unreadable or malformed
                bearer file. Neither message echoes a value.
        """
        # Lazy: the provisioning package is heavy, and a vault Machine that
        # never configures a check should not import it.
        from creek_mcp.provisioning.handoff import read_bearer_file

        self._url = _status_url(status_url)
        self._bearer = read_bearer_file(bearer_file)
        self._timeout = timeout
        self._transport = transport

    async def is_active(self, query: TokenStatusQuery) -> bool:
        """POST *query*'s claims and read the answer.

        Args:
            query: The verified claims.

        Returns:
            Adepthood's ``active`` answer.

        Raises:
            RevocationUnavailableError: For a transport failure or timeout, any
                status but ``200`` (``401``, ``400``, ``429``, ``5xx``, a
                redirect), or a body that is not exactly ``{"active": bool}``.
        """
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    "POST",
                    self._url,
                    headers={"Authorization": f"Bearer {self._bearer}"},
                    json=query.body(),
                ) as response,
            ):
                if response.status_code != _HTTP_OK:
                    raise RevocationUnavailableError
                body = await _bounded_body(response)
        except httpx.HTTPError:
            raise RevocationUnavailableError from None
        return _answer(body)


def load_revocation_cache(
    environ: Mapping[str, str] | None = None,
) -> RevocationCache | None:
    """Return the revocation check a vault Machine's environment configures.

    Args:
        environ: The environment, or ``None`` for :data:`os.environ`.

    Returns:
        The cache over an :class:`HttpRevocationChecker`, or ``None`` when
        neither setting is present (a blank value counts as absent).

    Raises:
        ValueError: When only one of the pair is set, or either is invalid.
    """
    source = os.environ if environ is None else environ
    url = source.get(STATUS_URL_ENV, "").strip()
    bearer = source.get(STATUS_BEARER_FILE_ENV, "").strip()
    if not url and not bearer:
        return None
    if not url or not bearer:
        msg = f"{STATUS_URL_ENV} and {STATUS_BEARER_FILE_ENV} must be set together"
        raise ValueError(msg)
    return RevocationCache(HttpRevocationChecker(url, Path(bearer)))
