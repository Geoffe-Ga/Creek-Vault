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
  item 7). :class:`PoolRequestScope` is what gives that handle a production
  caller: the route mints one on the loop, every dial opened under it
  registers itself, and the route calls :meth:`PoolRequestScope.cancel` when
  the deadline fires.
- **The closed request shape.** The scope also carries the two per-request
  facts the dial cannot know — the declared tier and the correlation id — so
  ADR-0016 Decision 1 item 4's shape can be built at all. A context with no
  declared tier is refused rather than dialled (:func:`require_pool_request`),
  because an omitted tier is precisely what makes the pool's own INTIMATE
  refusal impossible.

``tests/test_ollama_endpoint_invariant.py`` keeps this module the only place a
pool path or :data:`POOL_URL_ENV` is turned into a request.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import secrets
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from urllib.parse import SplitResult

POOL_URL_ENV: Final[str] = "CREEK_POOL_URL"
"""Machine-environment key holding the one pinned pool destination.

Under a vendor-attested pool this is the exact vendor hostname whose
measurement set the manifest pins. Under a vendor that ships a loopback proxy
sidecar it is that sidecar's loopback address, and the sidecar's single egress
destination is pinned by the same key on the Machine.
"""

POOL_VENDOR_ENV: Final[str] = "CREEK_POOL_VENDOR"
"""Machine-environment key naming the attesting vendor, when there is one.

Read from the Machine environment for the same reason :data:`POOL_URL_ENV` is:
which vendor attests this pool is a platform fact about the allocation, not a
writable-vault setting. Unset means the Creek-run pool of ADR-0016 O3, whose
provenance is ``pool:creek``; set, it names the vendor behind O5 and the
provenance becomes ``pool:attested:<vendor>``. The attesting client that
*verifies* that vendor's measurement set is #1896's; this key is only how the
answer is labelled.
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

POOL_EMBEDDINGS_PATH: Final[str] = "/v1/embeddings"
"""The pool's single embeddings endpoint (ADR-0016 Decision 1 item 13, #1904).

Grounding embeddings ride the same one pinned destination as generation under
Q2 option (i), so this path is owned here and nowhere else, exactly as the
other two are.
"""

POOL_MAX_EMBED_BATCH: Final[int] = 256
"""Most texts one embeddings request may carry (Decision 1 items 4 and 13).

The same figure as the reflection path's live-embed cap (#1034), so a cold
reflection's whole live pass fits one batch. Enforced here, before any socket
opens; the batcher in :mod:`creek.classify.llm.pool_embeddings` never builds a
larger one, so this refusal is the last line rather than the first.
"""

POOL_MAX_RESPONSE_BYTES: Final[int] = 1 << 20
"""Ceiling on a generation or identity-listing answer, read incrementally (#1906).

The mirror of :data:`POOL_MAX_REQUEST_BYTES`. A reflection answer is at most a
few kilobytes and a listing smaller still, so a pool that streams more than a
mebibyte is misbehaving, and the vault refuses rather than holding the body in
memory for the rest of the twelve-second request timeout on a 1 GB Machine.
"""

POOL_MAX_EMBED_RESPONSE_BYTES: Final[int] = 8 << 20
"""Ceiling on one embeddings answer, read incrementally (#1904, #1906).

Larger than :data:`POOL_MAX_RESPONSE_BYTES` because a batch of vectors is
legitimately larger than prose: each float costs up to
:data:`POOL_EMBED_JSON_FLOAT_BYTES` on the wire. The batcher sizes each batch
so its *expected* answer fits under this ceiling at the pinned dimensionality,
so the cap binds only on a pool that answers more than it was asked for.
"""

POOL_EMBED_JSON_FLOAT_BYTES: Final[int] = 25
"""Worst-case bytes one vector component costs in a JSON answer.

A float64 ``repr`` is at most 24 characters (``-1.2345678901234567e-05``) plus
one separator. Over-counting is the safe direction: it makes batches smaller,
never answers larger than the cap.
"""

POOL_REQUEST_ID_HEADER: Final[str] = "X-Creek-Pool-Request-Id"
"""Header carrying the request id, the pool contract's only correlation key.

A header rather than a body field, exactly as ADR-0016 Decision 1 item 4
specifies, so the closed request shape stays the inference arguments and
nothing else. The id is random per dial and derived from nothing the writer
sent, so it correlates two operational records and describes no content.
"""

POOL_MAX_DEADLINE_MS: Final[int] = 12_000
"""Largest ``deadline_ms`` the pool contract admits (Decision 1 item 4).

The millisecond twin of :data:`POOL_REQUEST_TIMEOUT`: the vault declares how
long it will wait, and the pool's own engine read timeout is that less the
elapsed time. Declaring more would ask the pool to outlive the vault request
that wanted the answer.
"""

POOL_PROMPT_TOKEN_CAP: Final[int] = 4096
"""Largest prompt the pool contract admits, in tokens (Decision 1 item 4).

Enforced in the vault *before* any dial — grounding snippets are dropped
first, and an entry that still exceeds the cap is refused rather than
truncated, so a reflection never silently reads part of an entry. The pool's
own cap is defence in depth.
"""

POOL_PROMPT_CHARS_PER_TOKEN: Final[int] = 3
"""Characters per token when measuring a prompt against the cap.

Deliberately **lower** than :data:`creek.generate.state_budget.CHARS_PER_TOKEN`,
and the difference is the whole point. That constant is four, which is the
forgiving end of the published 3.5-to-4 rule of thumb, and forgiving is right for
a *report* that only has to catch order-of-magnitude growth. This one measures
a **cap**, where an under-count is a prompt the vault believed was admissible
and the pool refuses: three over-counts the tokens, so the vault refuses
slightly earlier than the pool would and never slightly later. A real
tokenizer is not an option here — it would be a model download on a 1 GB
Machine, which is the cold start this whole ADR exists to avoid.
"""

POOL_OPEN_TIER: Final[str] = "open"
"""The less sensitive of the two tiers a pool request may declare."""

POOL_PERSONAL_TIER: Final[str] = "personal"
"""The more sensitive of the two tiers a pool request may declare."""

POOL_TIERS: Final[frozenset[str]] = frozenset({POOL_OPEN_TIER, POOL_PERSONAL_TIER})
"""Every tier a pool request may declare on the wire.

``intimate`` is deliberately absent and unnameable here. The
:class:`~creek.classify.llm.router.ModelRouter` chokepoint is the first line
and refuses or redirects an INTIMATE fragment before any dial (ADR-0016
Decision 5); transmitting the tier is the defence-in-depth layer that lets the
pool refuse too, which it cannot do for a tier it is never told.
"""

_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_GET: Final[str] = "GET"
_POST: Final[str] = "POST"
_JSON: Final[str] = "application/json"
_HTTP_OK: Final[int] = 200

_REQUEST_ID_BYTES: Final[int] = 16
_MS_PER_SECOND: Final[int] = 1000

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
_RESPONSE_TOO_LARGE: Final[str] = (
    "the pool answer exceeds the boundary's size cap; discarding it"
)
_READ_CHUNK_BYTES: Final[int] = 64 * 1024
_EXHAUSTED: Final[str] = (
    "the vault request's budget is already spent; refusing to dial the pool"
)
_CANCELLED: Final[str] = "the pool dial was cancelled at the vault's deadline"
_UNREACHABLE: Final[str] = "the shared inference pool did not answer usably"
_UNDECLARED_TIER: Final[str] = (
    "the pool request declares no admitted privacy tier; refusing to dial"
)
_PROMPT_TOO_LONG: Final[str] = (
    "the pool request's prompt exceeds the contract's token cap; refusing to dial"
)


class PoolBoundaryError(RuntimeError):
    """A refused, cancelled or unusable pool dial.

    Subclasses :class:`RuntimeError`, **not** :class:`OSError`, and the
    difference is load-bearing rather than cosmetic. This class used to
    subclass :class:`OSError` on the claim that "a call site that already
    degrades on a transport failure degrades the same way here" — and that
    claim was false: the generation call sites catch ``RuntimeError`` and
    nothing else, so a pool that accepted a connection and then died
    mid-request escaped :mod:`creek_mcp.tools.reflect` as an unhandled
    exception instead of the refusal ADR-0016 Decision 1 item 10 names. The
    reachable path was the ordinary one — the ``available`` probe succeeds,
    then the *separate* generate dial is reset or hits the 12 s socket
    timeout.

    Narrowing the hierarchy was chosen over widening the catches because this
    module's own neighbours already settled the question:
    :class:`~creek.classify.llm.providers.PoolIdentityError` is a
    ``RuntimeError``, ``_pool_status_error`` returns one, and
    ``_pool_completion``'s docstring states the rule outright — an unusable
    answer is the pool misbehaving, not the vault calling this wrong, and
    *every call site already degrades on* ``RuntimeError``. A pool refusal is
    the same kind of event, so it belongs in the same hierarchy. Widening
    every generation call site to ``OSError`` instead would have made each of
    them also swallow unrelated filesystem faults from the vault read beside
    the model call, which is a strictly worse refusal shape.

    Every message is a fixed string: none of them carries the destination, the
    prompt or a response body, so a log line or a structured refusal cannot
    become an oracle for where the vault dials or what it sent.
    """


class PoolDestinationError(PoolBoundaryError):
    """No pinned destination is configured, or a foreign URL was offered."""


class PoolRequestTooLargeError(PoolBoundaryError):
    """The encoded request body exceeds :data:`POOL_MAX_REQUEST_BYTES`."""


class PoolResponseTooLargeError(PoolBoundaryError):
    """The pool's answer exceeded its response cap, so it was discarded (#1906).

    Its own type rather than a :class:`PoolUnreachableError`, so an operator
    can tell three different failures apart from the refusal alone: a pool
    that did not answer in time, one that answered something undecodable, and
    one that answered too much. The read stops at the cap rather than reading
    the whole body and then checking it, and the answer is refused rather
    than truncated — a truncated body would reach the parser as malformed and
    surface as the wrong one of those three.
    """


class PoolDialCancelledError(PoolBoundaryError):
    """The vault's deadline fired, so the dial was cancelled or refused."""


class PoolUnreachableError(PoolBoundaryError):
    """The pool could not be reached, or answered something undecodable."""


class PoolPromptTooLongError(PoolBoundaryError):
    """The prompt exceeds :data:`POOL_PROMPT_TOKEN_CAP`, so nothing is sent.

    The last line of the vault-side cap, not the first. A route that builds
    its own prompt drops grounding first and refuses an over-cap entry with
    its own published shape, so a caller learns ``prompt_too_long`` rather
    than a generic refusal; this one catches every *other* pool caller, so no
    path can dial over the cap merely because its route forgot. **Never a
    truncation** on either path: a reflection that silently read part of an
    entry would be worse than one that refused (ADR-0016 Decision 1 item 4).
    """


class PoolTierUndeclaredError(PoolBoundaryError):
    """No admitted tier was declared for this request, so nothing is sent.

    The pool contract's request shape is closed and carries the declared tier
    (ADR-0016 Decision 1 item 4). A caller that has not declared one is
    refused here rather than dialled with the field guessed or omitted: a
    guessed ``open`` on personal content would be a false claim, and an
    omitted tier is the hole that makes the pool's own INTIMATE refusal
    impossible. An INTIMATE request reaches this refusal by construction,
    because :func:`pool_wire_tier` cannot name that tier.
    """


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


def pool_attesting_vendor(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the attesting vendor named in the Machine environment, or ``None``.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The vendor name, lower-cased and stripped, or ``None`` when
        :data:`POOL_VENDOR_ENV` is unset or blank — the Creek-run pool.
    """
    source = os.environ if environ is None else environ
    return source.get(POOL_VENDOR_ENV, "").strip().lower() or None


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


def pool_prompt_tokens(prompt: str) -> int:
    """Return the conservative token estimate for *prompt*.

    Args:
        prompt: The fully formatted prompt.

    Returns:
        The estimate under :data:`POOL_PROMPT_CHARS_PER_TOKEN`, rounded **up**,
        so a prompt is never measured as smaller than it is.
    """
    return -(-len(prompt) // POOL_PROMPT_CHARS_PER_TOKEN)


def pool_prompt_within_cap(prompt: str) -> bool:
    """Return whether *prompt* fits under the pool contract's prompt cap.

    Args:
        prompt: The fully formatted prompt.

    Returns:
        ``True`` when the estimate is at or under
        :data:`POOL_PROMPT_TOKEN_CAP`.
    """
    return pool_prompt_tokens(prompt) <= POOL_PROMPT_TOKEN_CAP


def pool_wire_tier(tier: object) -> str | None:
    """Return the tier *tier* may be declared as on the pool wire, or ``None``.

    Args:
        tier: A privacy tier, as its value or as the enum member itself.

    Returns:
        The member of :data:`POOL_TIERS` it names, or ``None`` for anything
        else — ``intimate`` above all, which the pool contract has no spelling
        for and which must never reach a dial.
    """
    name = getattr(tier, "value", tier)
    return name if isinstance(name, str) and name in POOL_TIERS else None


def _tightest(first: float | None, second: float | None) -> float | None:
    """Return the smaller of two optional budgets, or ``None`` when neither is set.

    Args:
        first: One budget in seconds, or ``None``.
        second: The other, or ``None``.

    Returns:
        The tighter constraint, or ``None`` when the caller tracks no deadline
        at all.
    """
    offered = [value for value in (first, second) if value is not None]
    return min(offered) if offered else None


class PoolRequestScope:
    """The per-request pool facts, and the handle that cancels its dials.

    One of these is minted on the event loop by the route that is about to run
    a generation off it, and reaches the worker thread through the context
    :func:`anyio.to_thread.run_sync` copies — so the loop keeps a reference to
    the very object the worker registers its dial with. That is what makes
    ADR-0016 Decision 1 item 7's shed path real: the route catches the
    cancellation at the ``read_off_loop`` boundary and calls :meth:`cancel`,
    which closes the pool socket from the loop so the worker's blocked read
    fails and the thread exits. ``read_off_loop`` alone abandons the thread
    and cannot cancel it.

    It carries three things no single dial can know on its own: the declared
    tier (the factory that resolved the stage knows it, the boundary does
    not), the remaining budget of the vault request (the loop knows it), and
    the request id that correlates the two operational records (nobody else
    mints one).

    Attributes:
        request_id: The random correlation key sent in
            :data:`POOL_REQUEST_ID_HEADER`. Derived from nothing the caller
            sent.
    """

    def __init__(self, *, budget_seconds: float | None = None) -> None:
        """Mint a scope, optionally bounded by the vault request's budget.

        Args:
            budget_seconds: Seconds left in the vault request, or ``None``
                when the caller tracks no deadline.
        """
        self.request_id = secrets.token_hex(_REQUEST_ID_BYTES)
        self._deadline: float | None = (
            None if budget_seconds is None else time.monotonic() + budget_seconds
        )
        self._tier: str | None = None
        self._lock = threading.Lock()
        self._dials: list[PoolDial] = []
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        """Whether the owning request has already cancelled its model work."""
        with self._lock:
            return self._cancelled

    @property
    def tier(self) -> str | None:
        """The declared wire tier, or ``None`` when none was declared."""
        with self._lock:
            return self._tier

    def declare(self, tier: object) -> None:
        """Declare the privacy tier this request's dials may carry.

        Anything :func:`pool_wire_tier` cannot name — ``intimate`` above all —
        leaves the scope undeclared, so :meth:`require_tier` refuses rather
        than dialling with a guessed field.

        Args:
            tier: The routing tier the generation stage was resolved for.
        """
        with self._lock:
            self._tier = pool_wire_tier(tier)

    def require_tier(self) -> str:
        """Return the declared wire tier, refusing when there is none.

        Returns:
            A member of :data:`POOL_TIERS`.

        Raises:
            PoolTierUndeclaredError: When no admitted tier was declared.
        """
        tier = self.tier
        if tier is None:
            raise PoolTierUndeclaredError(_UNDECLARED_TIER)
        return tier

    def remaining(self) -> float | None:
        """Return the seconds left in the vault request, or ``None``.

        Returns:
            The remaining budget, which may be zero or negative once spent, or
            ``None`` when this scope bounds nothing.
        """
        if self._deadline is None:
            return None
        return self._deadline - time.monotonic()

    def deadline_ms(self, budget: float | None = None) -> int:
        """Return the ``deadline_ms`` this request may declare to the pool.

        Args:
            budget: The calling provider's own remaining seconds, when it
                tracks one tighter than this scope's.

        Returns:
            The pool contract's ceiling, capped by whatever is left of the
            vault request's own budget and never below one millisecond — a
            declared zero would ask the pool to answer in no time at all,
            where a spent budget is :func:`_capped`'s refusal to make.
        """
        limit = _tightest(budget, self.remaining())
        if limit is None:
            return POOL_MAX_DEADLINE_MS
        return max(1, min(POOL_MAX_DEADLINE_MS, int(limit * _MS_PER_SECOND)))

    def register(self, dial: PoolDial) -> None:
        """Bind *dial* to this scope so :meth:`cancel` can close it.

        A dial registered after the scope was already cancelled is cancelled
        at once rather than left running: the race is real, because the loop
        may fire the deadline while the worker is between
        :func:`pool_destination` and its first read.

        Args:
            dial: The freshly opened dial.
        """
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._dials.append(dial)
        if cancelled:
            dial.cancel()

    def cancel(self) -> None:
        """Cancel every dial this scope holds; safe from any thread, idempotent."""
        with self._lock:
            self._cancelled = True
            dials = self._dials.copy()
            self._dials.clear()
        for dial in dials:
            dial.cancel()


_ACTIVE_SCOPE: ContextVar[PoolRequestScope | None] = ContextVar(
    "creek_pool_request_scope", default=None
)
"""The scope every dial opened in this context registers with.

A context variable rather than a parameter threaded through
:class:`~creek.classify.llm.providers.PoolProvider`, because the provider
protocol's ``complete`` signature is shared with five other backends and the
pool's per-request facts are not theirs. It propagates the right way for this
job: ``anyio``'s worker dispatch copies the context, so a scope set on the
event loop is the *same object* in the worker thread, which is exactly what
cancellation from the loop requires.
"""


def current_pool_scope() -> PoolRequestScope | None:
    """Return the scope active in this context, or ``None``.

    Returns:
        The scope a route or factory declared, or ``None`` outside one.
    """
    return _ACTIVE_SCOPE.get()


def require_pool_request() -> PoolRequestScope:
    """Return the active scope, refusing when this context declared none.

    Returns:
        The active :class:`PoolRequestScope`.

    Raises:
        PoolTierUndeclaredError: Outside a declared pool request. A dial built
            without one could not carry the tier, the deadline or the
            correlation id the closed request shape requires, and the vault
            refuses rather than sending a request the pool must guess at.
    """
    scope = current_pool_scope()
    if scope is None:
        raise PoolTierUndeclaredError(_UNDECLARED_TIER)
    return scope


@contextmanager
def pool_request_scope(scope: PoolRequestScope) -> Iterator[PoolRequestScope]:
    """Make *scope* the active scope for the duration of the block.

    Args:
        scope: The scope the caller minted and keeps a reference to.

    Yields:
        *scope*, unchanged.
    """
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


@contextmanager
def declared_pool_request(tier: object) -> Iterator[PoolRequestScope]:
    """Declare *tier* on the active scope, minting a transient one if needed.

    The declaration happens where the tier is known — the factory that
    resolved the generation stage — rather than where the dial is built, which
    is the whole reason the scope exists.

    Args:
        tier: The routing tier the stage was resolved for.

    Yields:
        The scope the declaration landed on.
    """
    active = current_pool_scope()
    if active is not None:
        active.declare(tier)
        yield active
        return
    fresh = PoolRequestScope()
    fresh.declare(tier)
    with pool_request_scope(fresh):
        yield fresh


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
        self,
        path: str,
        *,
        payload: Mapping[str, object] | None,
        headers: Mapping[str, str] | None = None,
        max_response_bytes: int = POOL_MAX_RESPONSE_BYTES,
    ) -> PoolResponse:
        """Send one request to *path* and read its whole answer.

        Exactly one attempt is made: a failure is a refusal, never a retry
        against the pool or anywhere else.

        Args:
            path: A pool path owned by this module.
            payload: The JSON body, or ``None`` for a ``GET``.
            headers: Extra request headers, such as the correlation id in
                :data:`POOL_REQUEST_ID_HEADER`. No credential is added here or
                accepted here.
            max_response_bytes: The most answer bytes this dial will read;
                one byte more is a refusal, never a truncation.

        Returns:
            The status and decoded body.

        Raises:
            PoolRequestTooLargeError: When the body exceeds the size cap.
            PoolResponseTooLargeError: When the answer exceeds
                *max_response_bytes*.
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
                headers=self._headers(body, headers),
            )
            response = connection.getresponse()
            raw = _read_capped(response, max_response_bytes)
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

    def _headers(
        self, body: bytes | None, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Return the request headers; no credential is added here.

        Args:
            body: The encoded body, or ``None`` for a ``GET``.
            extra: Per-request headers the caller owns, such as the
                correlation id. They are laid over the fixed pair, which is
                harmless because neither ``Accept`` nor ``Content-Type`` is a
                value a pool caller has any reason to move.

        Returns:
            The whole header block for one dial.
        """
        headers = {"Accept": _JSON}
        if body is not None:
            headers["Content-Type"] = _JSON
        return headers | dict(extra or {})

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


def _read_capped(response: http.client.HTTPResponse, cap: int) -> bytes:
    """Return *response*'s body, refusing as soon as it exceeds *cap* bytes.

    A declared ``Content-Length`` over the cap is refused before a byte of the
    body is read; an undeclared or understated one is read in chunks and
    refused the moment the running total passes the cap, so memory never
    grows past ``cap`` plus one chunk whatever the pool sends.

    Args:
        response: The response whose headers have been read.
        cap: The most body bytes admitted.

    Returns:
        The whole body, at most *cap* bytes.

    Raises:
        PoolResponseTooLargeError: When the body is larger than *cap*.
    """
    declared = response.length
    if declared is not None and declared > cap:
        raise PoolResponseTooLargeError(_RESPONSE_TOO_LARGE)
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > cap:
            raise PoolResponseTooLargeError(_RESPONSE_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


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

    The active :class:`PoolRequestScope`, when there is one, both tightens the
    budget to whatever is left of the vault request and takes ownership of the
    dial, so the route that minted it can cancel this socket from the event
    loop (ADR-0016 Decision 1 item 7).

    Args:
        budget: Seconds left in the vault request, which caps both timeouts.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        An unconnected :class:`PoolDial`; it connects on first request.

    Raises:
        PoolDestinationError: When no destination is pinned.
        PoolDialCancelledError: When the budget is already spent.
    """
    scope = current_pool_scope()
    limit = _tightest(budget, None if scope is None else scope.remaining())
    dial = PoolDial(
        pool_destination(environ),
        connect_timeout=_capped(POOL_CONNECT_TIMEOUT, limit),
        request_timeout=_capped(POOL_REQUEST_TIMEOUT, limit),
    )
    if scope is not None:
        scope.register(dial)
    return dial


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
    headers: Mapping[str, str] | None = None,
) -> PoolResponse:
    """Send one generation request to the pinned destination.

    Args:
        payload: The JSON request body.
        budget: Seconds left in the vault request.
        environ: Environment to read; ``os.environ`` when omitted.
        headers: Per-request headers, such as the correlation id.

    Returns:
        The generation response.

    Raises:
        PoolBoundaryError: As :meth:`PoolDial.request`.
    """
    with open_pool_dial(budget=budget, environ=environ) as dial:
        return dial.request(POOL_GENERATE_PATH, payload=payload, headers=headers)


def pool_embed(
    payload: Mapping[str, object],
    *,
    budget: float | None = None,
    environ: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> PoolResponse:
    """Send one embeddings request to the pinned destination (#1904).

    The same single destination, transport, timeouts and request cap as
    :func:`pool_generate`; the answer is capped at
    :data:`POOL_MAX_EMBED_RESPONSE_BYTES` instead of the prose cap. A request
    carrying more than :data:`POOL_MAX_EMBED_BATCH` inputs is refused before
    any socket opens.

    Args:
        payload: The JSON request body; its ``input`` is the list of texts.
        budget: Seconds left in the vault request.
        environ: Environment to read; ``os.environ`` when omitted.
        headers: Per-request headers, such as the correlation id.

    Returns:
        The embeddings response.

    Raises:
        PoolRequestTooLargeError: When the batch or the body is over its cap.
        PoolBoundaryError: As :meth:`PoolDial.request`.
    """
    inputs = payload.get("input")
    if isinstance(inputs, list) and len(inputs) > POOL_MAX_EMBED_BATCH:
        raise PoolRequestTooLargeError(_TOO_LARGE)
    with open_pool_dial(budget=budget, environ=environ) as dial:
        return dial.request(
            POOL_EMBEDDINGS_PATH,
            payload=payload,
            headers=headers,
            max_response_bytes=POOL_MAX_EMBED_RESPONSE_BYTES,
        )


def pool_response_ok(response: PoolResponse) -> bool:
    """Return whether *response* is the pool's success status.

    Args:
        response: A response from this boundary.

    Returns:
        ``True`` only for HTTP 200.
    """
    return response.status == _HTTP_OK
