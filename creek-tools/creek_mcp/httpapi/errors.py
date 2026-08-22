"""The one place ``/v1`` builds a response, error or otherwise (#1074).

Two invariants live here, and both are structural rather than behavioural —
which is why they are worth concentrating in one small module. The second one
carries most of the prose: the bold headings beneath it are the arguments for
its shape, not a third and fourth invariant.

**One error-envelope construction site.**
:class:`~creek_mcp.api.models.ErrorEnvelope` is ``extra="forbid"`` with three
fields, but that only constrains the *shape*. What keeps the contents honest is
that every refusal is built from the :data:`~creek_mcp.api.models.ERROR_MESSAGES`
table at :func:`error_response` and nowhere else; a second site is where a
caller-derived ``message`` eventually appears, and a refusal that varies with
its input is an existence-and-rank oracle. ``tests/test_v1_api_structure.py``
AST-counts the construction sites across the package and pins the total at one.

**Two standing headers, two merge rules.** Every response this builder
constructs — the ``200``, the ``501``, the routing ``404``, and the ``401``
that never reaches the ceiling middleware at all — carries
``Vary: X-Creek-Tier-Ceiling, Authorization, X-Creek-Contract-Version`` and
``Cache-Control: no-store``.
Authentication sits *above* the ceiling gate, so there is no single middleware
every response passes through on the way out with the ceiling in scope. There
is, however, exactly one builder: :func:`json_response`. Routing every response
through it is what makes both headers unconditional — and, since #1128,
unforgeable. They are also the only two headers this builder stamps and so the
only two protected: everything else a call site passes rides through untouched,
which is what a ``401`` needs — see :func:`_challenge_for`.

Scope that claim to what it covers — *every response this builder constructs*,
which is not the same as every ``/v1`` response. One path still reaches a client
without passing through here: Starlette's ``ServerErrorMiddleware`` renders a
``text/plain`` ``500`` if a fault escapes the access-log layer's own ``try``. It
is filed separately and is not closable from inside this module. The router's
own ``307`` on a trailing slash used to be the second such path; it is gone,
because :data:`~creek_mcp.httpapi.app.REDIRECT_SLASHES` turns the redirect off
and the miss is rendered here like any other. A security docstring that overstates its
reach is worse than one that says nothing, because the next reader stops
looking where the hole actually is.

**Why unconditional, rather than conditional on authentication.** The ask was
for these headers on *authenticated* responses; stamping them on every response
is the only implementable reading of it. :func:`json_response` takes no
:class:`~creek_mcp.httpapi.context.RequestContext`, so conditioning on
``context.consumer`` means threading a context through the one builder and all
six of its call sites, destroying the context-freedom that lets the builder be
unit-tested as a pure function of its arguments. The cheap alternative —
stamping in :func:`error_response` alone — covers every refusal and no ``200``,
the exact inverse of what was asked. And the condition would have nothing to
decide: :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware` is item 5 of the
eight-layer stack, *above* the router, so every route is authenticated by
construction. Where it *would* have an effect is on a route that does not exist
yet — one mounted above the gate would silently stop being stamped, because
such a condition is keyed on the stack's order rather than on anything the
builder can see. The unconditional form cannot fail that way.

**What the exposure actually is.** Intersect the published status set —
``{200, 401, 403, 404, 409, 422, 500, 501, 503}`` — with the statuses RFC 9110
§15.1 enumerates as heuristically cacheable — ``{200, 203, 204, 206, 300, 301,
404, 405, 410, 414, 501}`` — and the whole of it is ``{200, 404, 501}``. Lead
with the ``404``: it is reachable today on every unrouted ``/v1`` path, it
carries no explicit freshness information, and a *conforming* shared cache may
therefore store it and reuse it on heuristic freshness alone. That is the one
case here where this is not merely prophylactic. Do not rest the argument on a
cached ``401`` instead: that status is not on the heuristic list, so the story
needs an intermediary already violating the specification, and an argument that
only holds against a broken cache is one a reviewer can wave away.

**The two headers are independent mechanisms, not one doubled.** The hazard is
a *misconfigured* intermediary — precisely the one that may ignore
``no-store``. For that cache the remedy is not a louder instruction but a
correct cache *key*, and naming ``Authorization`` in ``Vary`` is that key:
RFC 9111 §4.1 makes reuse a conjunction over the nominated field names, so
adding a token is monotone — it can only ever shrink the set of requests a
stored entry may be served to, never widen it. Neither header subsumes the
other. Drop ``no-store`` and a compliant cache is free to store; drop the token
and a non-compliant one is free to mismatch. Both, or neither is worth having.

**``Vary`` unions.** It absorbs a colliding caller value rather than discarding
it, and that is the load-bearing half of the policy. Dropping a caller's
``Vary`` would close this hole and open its mirror image — a response whose
body really does turn on ``Accept-Encoding`` but that claims to turn only on
the standing tokens invites a cache to key on less than the truth, which is
this same defect pointed the other way. Union is also the only direction that
is monotone, by the paragraph above.

So the values union: the standing tokens first, in their own fixed order, then
the caller's in the order supplied. That order is fixed rather than incidental
because the rendered value is asserted byte-for-byte by
``tests/test_v1_api_admission.py::test_a_caller_echoing_the_standing_token_does_not_double_it``,
and because a header whose bytes depended on set iteration would differ between
two worker processes answering the same request.

**``Cache-Control`` replaces.** A caller's value is dropped whole, under any
spelling. Be honest about the cost: this is *not* directive-wise monotone.
``private`` and ``no-cache`` are subsumed by ``no-store``, but ``no-transform``
and ``must-understand`` are not — a response nothing may store can still be
transformed in flight — so a call site passing ``no-store, no-transform`` loses
a restriction. It is chosen anyway. Preserving it means parsing a header whose
members take ``=``-valued arguments and then ruling on whether ``max-age=600``
contradicts ``no-store``, and a directive parser with a contradiction policy is
a larger thing to get wrong than the single directive it would rescue. Nothing
pays for it today either: no production call site passes a ``Cache-Control`` at
all — :func:`_challenge_for`, the only caller handing this builder extra
headers, passes only ``WWW-Authenticate`` — so the first call site that
genuinely needs ``no-transform`` should teach this builder about it rather than
smuggle it through an argument documented to discard it. *Prepending* the
standing directive was rejected on the same evidence rather than on taste:
``no-store, max-age=600`` is self-contradictory, RFC 9111 does not say which
half wins, and real caches resolve it inconsistently — so the response's
meaning would become the intermediary's choice rather than the server's.

The two rules share a mechanism and nothing else. :func:`_folds_to` matches a
header name case-folded, for both of them; the policies stay two named
constants and two named rules. Folding is not politeness about style: ``Vary``
and ``vary`` are one header on the wire and two keys in a dict — Starlette
lowercases each key independently and never deduplicates, so an unfolded merge
emits two ``vary`` lines and leaves it to the intermediary to decide which one
keys the cache. Caller ``Vary`` tokens outside RFC 9110's ``tchar`` set are
dropped whole rather than repaired, and only ``OWS`` — the space and the
horizontal tab — is trimmed before that test, so a token cannot be
*whitespace-stripped* back into legality either. A repaired token is still a
token the caller chose, and this header's contents decide a cache key.
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
from creek_mcp.api.routes import (
    AUTHORIZATION_HEADER,
    CEILING_HEADER,
    CONTRACT_VERSION_HEADER,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

VARY_HEADER: Final[str] = "Vary"
"""The response header naming which request headers the body depends on."""

_STANDING_VARY_TOKENS: Final[tuple[str, ...]] = (
    CEILING_HEADER,
    AUTHORIZATION_HEADER,
    CONTRACT_VERSION_HEADER,
)
"""Every field name a response declares its body depends on, in wire order.

Three since #1144. :data:`~creek_mcp.api.routes.CONTRACT_VERSION_HEADER` is not
a courtesy entry: ``GET /v1/capabilities`` answers ``status: ok`` with the full
capability list to a caller declaring a served minor, and ``status:
incompatible`` with an empty list to one declaring a stale minor, and those two
responses are identical in every other respect. Without the token they share a
cache key, so an intermediary can hand a compatible client the refusal minted
for an incompatible one — on the endpoint every client calls first. Standing
rather than set only on ``/v1/capabilities``, because a cache in front of the
surface applies one rule, and the routes gated by
:func:`~creek_mcp.httpapi.app._speaks_a_served_minor` turn on the same header.

The first two arrived with #1129, load-bearing from two directions: a
``/v1`` body is a function of the declared ceiling *and* of the authenticated
consumer, so an entry keyed on either alone may be served to a caller the other
would have told apart. :data:`~creek_mcp.api.routes.AUTHORIZATION_HEADER` is
the header the credential actually arrives in —
:mod:`creek_mcp.httpapi.auth` reads that same constant to authenticate — because
a ``Vary`` naming a header no request carries varies on nothing at all.

A tuple rather than a set, and iterated rather than indexed: the order is part
of the rendered value and is asserted byte-for-byte, and a merge that seeded
itself from ``[0]`` alone would emit the second token twice the moment a caller
echoed it. That the third token was a one-line addition here is the payoff for
having written the merge once rather than at each of the sites that stamps it.
"""

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

CACHE_CONTROL_HEADER: Final[str] = "Cache-Control"
"""The response header saying whether this answer may be stored at all.

The second header this builder owns, and the one whose merge rule is
*replacement* rather than union. ``Vary`` can only get safer as tokens are
added; this header cannot, because ``public`` and ``max-age`` are each strictly
more permissive than what the server insists on, and a value carrying both
theirs and ours would be self-contradictory rather than merely wrong.
"""

NO_STORE: Final[str] = "no-store"
"""The whole directive, and deliberately nothing else.

The strongest thing RFC 9111 lets a response say: no cache of any kind may keep
any part of it. That is the right claim for a body computed from one caller's
credential under one caller's ceiling. Three plausible neighbours were
considered and rejected rather than overlooked:

* ``private`` — subsumed. It scopes storage to a single-user cache instead of
  forbidding storage, so it says less than ``no-store`` already says.
* ``no-cache, max-age=0`` — strictly weaker. It permits storage and merely
  demands revalidation before reuse, so a copy of the body still lands on the
  intermediary's disk, which is the outcome being refused.
* ``Pragma: no-cache`` — deprecated by RFC 9111 §5.4, and a *request*
  directive besides. Sending it is cargo cult here: ``/v1``'s clients are
  contract-versioned JSON consumers, not the HTTP/1.0 browsers the habit was
  formed for, and an extra header nobody reads is one more thing to keep true.
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


def _folds_to(name: str, standing: str) -> bool:
    """Return whether *name* is the *standing* header under any spelling.

    The shared half of the two policies, and deliberately the only shared half.
    A ``STANDING_HEADERS`` mapping driving one merge loop over both headers was
    declined: ``Vary`` unions and ``Cache-Control`` replaces, so the mapping
    would have to carry the rule as well as the name —
    ``Mapping[str, MergeRule]``, a strictly more complex type than two named
    constants and two named rules — and two members is not enough repetition to
    buy an abstraction. Written down so the next reader need not re-derive it.

    Args:
        name: A header name as some call site chose to capitalise it.
        standing: The canonical spelling of a header this builder stamps:
            :data:`VARY_HEADER` or :data:`CACHE_CONTROL_HEADER`.

    Returns:
        ``True`` when the two are one field name — ``Vary``, ``vary``, ``VARY``
        and the rest all fold together. Folding here is not politeness about
        style: an unfolded comparison lets a second spelling through as a
        separate dict key, and Starlette renders that as a second header line
        rather than merging it, leaving an intermediary to decide which line
        governs.
    """
    return name.casefold() == standing.casefold()


def _is_standing(name: str) -> bool:
    """Return whether this builder stamps *name* itself.

    Args:
        name: A caller header name, however capitalised.

    Returns:
        ``True`` for ``Vary`` and ``Cache-Control`` under any spelling — the two
        headers whose value the builder decides, and therefore the two a caller
        may not have written out beside it. Named rather than inlined into the
        passthrough filter so that filter reads as the single idea it is: what
        rides through is every header this module holds no opinion about.
    """
    return _folds_to(name, VARY_HEADER) or _folds_to(name, CACHE_CONTROL_HEADER)


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
    """Return the merged ``Vary`` tokens, the standing ones first.

    Args:
        supplied: The value of every caller header whose name folds to
            ``Vary``. Usually empty; a dict can hold more than one of them
            only because ``Vary`` and ``vary`` are distinct keys, which is the
            case this exists to collapse.

    Returns:
        Every member of :data:`_STANDING_VARY_TOKENS` in its declared order,
        then each well-formed caller token in the order it was supplied,
        deduplicated case-folded because field names are case-insensitive on
        the wire. Both the seed list and the ``seen`` set are built from the
        whole tuple: seeding the list from one element would drop a standing
        token, and seeding ``seen`` from one would emit the other twice as soon
        as a call site echoed it — which, now that ``Authorization`` is
        published as part of the contract, is the likeliest collision there is.
        Malformed tokens are absent rather than corrected: only ``OWS`` is
        trimmed, so a piece whose whitespace is a line break stays malformed and
        is dropped instead of being tidied into a legal token. The order is
        fixed rather than incidental — the rendered value is asserted
        byte-for-byte by
        ``test_a_caller_echoing_the_standing_token_does_not_double_it``.
    """
    tokens = list(_STANDING_VARY_TOKENS)
    seen = {token.casefold() for token in _STANDING_VARY_TOKENS}
    for value in supplied:
        for piece in value.split(","):
            token = piece.strip(_OWS)
            if _is_token(token) and token.casefold() not in seen:
                seen.add(token.casefold())
                tokens.append(token)
    return tokens


def _merged_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Return *headers* with the two standing headers laid over the top.

    Args:
        headers: The caller's extra headers for this response, or ``None``. It
            may hold more than one spelling of either standing header —
            ``Vary`` and ``vary`` are one header on the wire but two keys in a
            dict, and so are ``Cache-Control`` and ``cache-control`` — so every
            colliding ``Vary`` is collected, which is why that collection is a
            list, and every colliding ``Cache-Control`` is discarded, which is
            why that one is not collected at all.

    Returns:
        Three groups, in this order: exactly one ``Vary`` carrying both
        standing tokens and whatever the caller asked to add to them; exactly
        one ``Cache-Control``, always :data:`NO_STORE`, whatever the caller
        asked for; and then every caller header that folds to neither,
        unchanged and unexamined — the bearer challenge has to reach the caller
        or a ``401`` is a protocol violation. The order is a decision rather
        than an accident, for the reason the module docstring gives about token
        order: a header block whose bytes depended on which key a dict happened
        to yield first would differ between two worker processes answering the
        same request, and the byte-exact assertions over it would then report a
        flake instead of a fault.
    """
    supplied = headers or {}
    caller_vary = [
        value for name, value in supplied.items() if _folds_to(name, VARY_HEADER)
    ]
    passthrough = {
        name: value for name, value in supplied.items() if not _is_standing(name)
    }
    return {
        VARY_HEADER: ", ".join(_vary_tokens(caller_vary)),
        CACHE_CONTROL_HEADER: NO_STORE,
    } | passthrough


def json_response(
    payload: Mapping[str, Any],
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Return *payload* as JSON, stamped with both standing headers.

    The single builder. Every ``/v1`` response constructed by this application
    — success, refusal, and the two produced by exception handlers — is built
    here, which is what makes ``Vary: X-Creek-Tier-Ceiling, Authorization,
    X-Creek-Contract-Version`` and ``Cache-Control: no-store``
    unconditional rather than something each call
    site has to remember. The module docstring names the two paths that reach a
    client without being constructed here; the guarantee is over this builder's
    output, not over every byte the process emits.

    Args:
        payload: The already-serialised body. Callers hand in
            ``model_dump(mode="json")`` output rather than a model, so this
            function never has to know which model it is rendering.
        status: The HTTP status line.
        headers: Extra headers for this particular response, such as the
            bearer challenge. The two standing headers win, and they are the
            only two this builder stamps. A name that folds to ``Vary`` —
            case-folded, so ``vary`` is the same name — is neither honoured nor
            dropped, but has its well-formed tokens merged in after the
            standing ones; a name that folds to ``Cache-Control`` is dropped
            whole, value and all, because that header replaces rather than
            unions. Every other name passes through unexamined.

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
