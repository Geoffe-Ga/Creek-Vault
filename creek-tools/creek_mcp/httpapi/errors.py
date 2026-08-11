"""The one place ``/v1`` builds a response, error or otherwise (#1074).

Two invariants live here, and both are structural rather than behavioural —
which is why they are worth concentrating in one small module.

**One error-envelope construction site.**
:class:`~creek_mcp.api.models.ErrorEnvelope` is ``extra="forbid"`` with three
fields, but that only constrains the *shape*. What keeps the contents honest is
that every refusal is built from the :data:`~creek_mcp.api.models.ERROR_MESSAGES`
table at :func:`error_response` and nowhere else; a second site is where a
caller-derived ``message`` eventually appears, and a refusal that varies with
its input is an existence-and-rank oracle. ``tests/test_v1_api_structure.py``
AST-counts the construction sites across the package and pins the total at one.

**One ``Vary`` stamp.** Every response — the ``200``, the ``501``, the routing
``404``, and the ``401`` that never reaches the ceiling middleware at all —
must carry ``Vary: X-Creek-Tier-Ceiling``, or a shared cache could serve one
caller's ceiling-filtered response to another. Authentication sits *above* the
ceiling gate, so there is no single middleware every response passes through on
the way out with the ceiling in scope. There is, however, exactly one builder:
:func:`json_response`. Routing every response through it is what makes the
header unconditional — and, since #1128, unforgeable. The builder used to lay
this header *under* the caller's, so any call site handing in a ``Vary`` of its
own silently deleted the ceiling token; the merge now runs the other way. Note
the narrowness of that claim: ``Vary`` is the only header this builder stamps
and so the only one protected. Everything else a call site passes still rides
through untouched, which is what a ``401`` needs — see :func:`_challenge_for`.

It absorbs the colliding value rather than discarding it, and that is the
load-bearing half of the policy. Dropping a caller's ``Vary`` would close this
hole and open its mirror image — a response whose body really does turn on
``Accept-Encoding`` but that claims to turn only on the ceiling invites a cache
to key on less than the truth, which is this same defect pointed the other way.
Union is also the only direction that is *monotone*: RFC 9111 §4.1 makes reuse
a conjunction over the nominated field names, so adding a token can only ever
shrink the set of requests an entry may be served to, never widen it.

So the values union: the ceiling token first, then the caller's in the order
supplied. The order is fixed rather than incidental because the rendered value
is asserted byte-for-byte by
``tests/test_v1_api_admission.py::test_a_caller_echoing_the_standing_token_does_not_double_it``,
and because a header whose bytes depended on set iteration would differ between
two worker processes answering the same request.

Names are matched case-folded, because ``Vary`` and ``vary`` are one header on
the wire and two keys in a dict — Starlette lowercases each key independently
and never deduplicates, so an unfolded merge emits two ``vary`` lines and leaves
it to the intermediary to decide which one keys the cache. Caller tokens outside
RFC 9110's ``tchar`` set are dropped whole rather than repaired, and only ``OWS``
— the space and the horizontal tab — is trimmed before that test, so a token
cannot be *whitespace-stripped* back into legality either. A repaired token is
still a token the caller chose, and this header's contents decide a cache key.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, Any, Final

from starlette.responses import JSONResponse

from creek_mcp.api.models import (
    ERROR_MESSAGES,
    ERROR_STATUS,
    ErrorCode,
    ErrorEnvelope,
)
from creek_mcp.api.routes import CEILING_HEADER

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

VARY_HEADER: Final[str] = "Vary"
"""The response header naming which request headers the body depends on."""

_TCHAR: Final[frozenset[str]] = frozenset(
    string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~"
)
"""RFC 9110's ``tchar`` — every character a bare field name may contain.

This is the entire admission test for a caller-supplied ``Vary`` token, so it is
spelled out rather than approximated by a "no control characters" check: the
things it excludes include the space, the colon and the carriage return, each of
which turns one header value into something other than one header value.

``*`` is a member and therefore survives, which is the right outcome even though
it reads like a wildcard. RFC 9111 §4.1 tests for ``*`` by *membership* in the
stored ``Vary`` value, not only as the whole value, and a stored response
carrying it never matches — so an entry that would otherwise have been reusable
becomes uncacheable. Dropping ``*`` would be the harmful direction: it would
turn a response its author declared uncacheable into one a cache may serve on
the ceiling alone. The token degrades closed, so it is kept.
"""

_OWS: Final[str] = " \t"
"""RFC 9110's ``OWS`` — the only whitespace that may pad a list element.

Trimmed before the :data:`_TCHAR` test, and deliberately narrower than
:meth:`str.strip`'s default. A bare ``strip()`` also eats ``\\r`` and ``\\n``,
which would quietly convert ``Cookie,\\r\\nAccept`` into the perfectly legal
token ``Accept`` — repairing exactly the input this module refuses to repair.
"""

WWW_AUTHENTICATE_HEADER: Final[str] = "WWW-Authenticate"
"""The challenge header a ``401`` is obliged to send."""

BEARER_CHALLENGE: Final[str] = 'Bearer realm="creek"'
"""The whole challenge: a scheme and a realm naming the *service*.

Never the vault and never a path. This is the one header a ``401`` must emit,
so anything interpolated into it would leak through the single channel an
unauthenticated caller is guaranteed to see.
"""

HTTP_OK: Final[int] = 200
"""The one success status ``/v1`` returns; readiness lives in the body."""


def _is_vary(name: str) -> bool:
    """Return whether *name* is the ``Vary`` header under any spelling.

    Args:
        name: A header name as some call site chose to capitalise it.

    Returns:
        ``True`` for ``Vary``, ``vary``, ``VARY`` and the rest. Folding here is
        not politeness about style: an unfolded comparison lets a second
        spelling through as a separate dict key, and Starlette renders that as
        a second ``vary`` line rather than merging it.
    """
    return name.casefold() == VARY_HEADER.casefold()


def _is_token(candidate: str) -> bool:
    """Return whether *candidate* is a whole, well-formed field name.

    Args:
        candidate: One comma-separated piece of a caller ``Vary``, already
            stripped of surrounding ``OWS``.

    Returns:
        ``True`` only when it is non-empty and made entirely of :data:`_TCHAR`.
        The empty pieces a doubled or trailing comma leaves behind fall out here
        for free, and so does anything carrying a space, a colon or a line break
        — which matters, because Starlette writes a header value out verbatim,
        and a value that ends its own line describes a second header the
        application never wrote. An allowlist rather than a CR/LF blocklist:
        the set of characters that are legal in a field name is small, closed
        and written down, and the set that is dangerous is neither.
    """
    return bool(candidate) and all(char in _TCHAR for char in candidate)


def _vary_tokens(supplied: Iterable[str]) -> list[str]:
    """Return the merged ``Vary`` tokens, the standing one first.

    Args:
        supplied: The value of every caller header whose name folds to
            ``Vary``. Usually empty; a dict can hold more than one of them
            only because ``Vary`` and ``vary`` are distinct keys, which is the
            case this exists to collapse.

    Returns:
        :data:`~creek_mcp.api.routes.CEILING_HEADER`, then each well-formed
        caller token in the order it was supplied, deduplicated case-folded
        because field names are case-insensitive on the wire. Malformed tokens
        are absent rather than corrected: only ``OWS`` is trimmed, so a piece
        whose whitespace is a line break stays malformed and is dropped instead
        of being tidied into a legal token. The order is fixed rather than
        incidental — the rendered value is asserted byte-for-byte by
        ``test_a_caller_echoing_the_standing_token_does_not_double_it``.
    """
    tokens = [CEILING_HEADER]
    seen = {CEILING_HEADER.casefold()}
    for value in supplied:
        for piece in value.split(","):
            token = piece.strip(_OWS)
            if _is_token(token) and token.casefold() not in seen:
                seen.add(token.casefold())
                tokens.append(token)
    return tokens


def _merged_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Return *headers* with the standing ``Vary`` merged in over the top.

    Args:
        headers: The caller's extra headers for this response, or ``None``. It
            may hold more than one spelling of ``Vary`` — ``Vary`` and ``vary``
            are one header on the wire but two keys in a dict — and every one
            of them is collected, which is why the collection is a list.

    Returns:
        Every non-colliding caller header unchanged and unexamined — the bearer
        challenge has to reach the caller or a ``401`` is a protocol violation —
        plus exactly one ``Vary`` carrying the ceiling token and whatever the
        caller asked to add to it. ``Vary`` is placed first so that a response
        with no colliding header renders byte-for-byte as it did before this
        merge existed.
    """
    supplied = headers or {}
    caller_vary = [value for name, value in supplied.items() if _is_vary(name)]
    passthrough = {
        name: value for name, value in supplied.items() if not _is_vary(name)
    }
    return {VARY_HEADER: ", ".join(_vary_tokens(caller_vary))} | passthrough


def json_response(
    payload: Mapping[str, Any],
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Return *payload* as JSON, stamped with the standing ``Vary``.

    The single builder. Every ``/v1`` response — success, refusal, and the two
    produced by exception handlers — is constructed here, which is what makes
    ``Vary: X-Creek-Tier-Ceiling`` unconditional rather than something each
    call site has to remember.

    Args:
        payload: The already-serialised body. Callers hand in
            ``model_dump(mode="json")`` output rather than a model, so this
            function never has to know which model it is rendering.
        status: The HTTP status line.
        headers: Extra headers for this particular response, such as the
            bearer challenge. The standing ``Vary`` wins — it is the only
            header this builder stamps and so the only one protected. A name
            that collides with it — case-folded, so ``vary`` is the same name —
            neither replaces the standing value nor is dropped, but has its
            well-formed tokens folded in after the ceiling token. Every other
            name passes through unexamined.

    Returns:
        The response, ready to be awaited as an ASGI application.
    """
    return JSONResponse(payload, status_code=status, headers=_merged_headers(headers))


def _challenge_for(code: ErrorCode) -> Mapping[str, str]:
    """Return the extra headers a refusal with *code* must carry.

    Args:
        code: The wire error code being rendered.

    Returns:
        The bearer challenge for an unauthenticated refusal, and nothing for
        every other code — a challenge on a ``422`` would invite a client to
        re-present credentials that were never the problem.
    """
    if code is ErrorCode.UNAUTHENTICATED:
        return {WWW_AUTHENTICATE_HEADER: BEARER_CHALLENGE}
    return {}


def error_response(code: ErrorCode, context: RequestContext) -> Response:
    """Return the published envelope for *code*, correlated to *context*.

    The message is looked up from :data:`~creek_mcp.api.models.ERROR_MESSAGES`
    and is never composed. Nothing the caller sent — not the path, not the
    body, not the header that was refused — reaches the body, so two refusals
    of the same code are byte-identical but for the correlation id.

    Args:
        code: The wire error code. It alone determines the HTTP status, the
            message and the retry disposition.
        context: The request's context, which carries the correlation id an
            operator joins to the access line.

    Returns:
        The refusal, ready to be awaited as an ASGI application.
    """
    envelope = ErrorEnvelope(
        code=code,
        message=ERROR_MESSAGES[code],
        request_id=context.request_id,
    )
    return json_response(
        envelope.model_dump(mode="json"),
        ERROR_STATUS[code],
        headers=_challenge_for(code),
    )
