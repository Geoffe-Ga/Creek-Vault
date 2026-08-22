"""Transport-neutral admission + consumer-identity policy (#1073).

Two questions have to be answered the same way for every caller, whichever
transport carried the call:

1. *May this caller request this tier ceiling?* — :func:`admitted_ceiling`;
2. *Whose identity is this call audited under?* — :func:`effective_consumer`.

Until #1073 both answers lived inside :mod:`creek_mcp.server`, keyed off the
MCP SDK's request-scoped ``get_access_token()`` and reachable only from
``_BoundedFastMCP.call_tool``. "Remote" therefore meant *carries an MCP access
token* — a fact about one transport's middleware rather than about the network.
The ``/v1`` HTTP application API of epic #1071 (see
``docs/decisions/2026-07-31-adepthood-http-application-api.md``) carries no MCP
access token and stands up no MCP request context at all, so under that
definition every ``/v1`` caller would have read as *local*: the cap that keeps
intimate content off the network would silently not have applied, ``intimate``
and ``all`` would have been admitted over HTTP, and the call would have been
audited as the local operator instead of as the network consumer that made it.

Moving the decision here is the fix, but the load-bearing half is what the move
*removes*. ``is_remote`` is now a plain bool the adapter asserts, never
something policy sniffs out of a transport it happens to be able to reach —
and this module imports no MCP SDK and no web framework, so there is nothing
here to sniff even by accident. ``tests/test_mcp_policy.py`` AST-pins that
import list, and constructs its remote callers as one dataclass with no
transport in scope.

**Who calls this.** Adapters, and only adapters — the code that already knows
which transport it is. :meth:`creek_mcp.server._BoundedFastMCP.call_tool`
today; the ``/v1`` handlers per the ADR. Each supplies its own transport's
answers to *who is the caller* and *is this remote*, and renders the verdict in
its own vocabulary: MCP returns the
:func:`creek_mcp.tier_ceiling.refusal_response` payload, ``/v1`` answers ``422
invalid_request``. ``/v1`` additionally expresses the same boundary
*structurally*, through :class:`creek_mcp.api.models.WireTierCeiling`'s two
members — ``intimate`` is not constructible on that wire at all. That is
deliberate belt-and-braces rather than duplication: this module is the shared
reasoning the two surfaces agree on, not a runtime check ``/v1`` could forget
to call.

**What this module deliberately does not do.**

- *It emits no audit records.* Emission stays at the ~24 call sites in
  ``creek_mcp/tools/*.py``, which receive ``consumer=`` top-down from the
  adapter. Handing policy a vault path and file I/O would destroy the property
  that makes it testable with no context at all, and would stand up a second
  emission site free to diverge from the first.
- *It opens no file and reads no fragment.* It ranks nothing on disk; it
  decides on the two values it is handed. So it is not a fourth privacy-tier
  reader (#1079).
- *It never interpolates caller or vault data into a refusal* (#1090). A
  refusal that varies with its input is an existence-and-rank oracle: a remote
  consumer that can tell two refusals apart can read back what it was refused,
  and eventually what tier a piece of content actually is.
  :data:`REMOTE_CEILING_REFUSAL_REASON` is therefore one module constant shared
  by every refused input, whatever the input was. The one interpolation it
  contains is ``TierCeiling.PERSONAL.value`` — a static constant naming the
  *rule*, resolved once at import, never a value a caller supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from creek_mcp.tier_ceiling import TierCeiling


class Transport(StrEnum):
    """The channel a Creek MCP server was started on, named once (#1583).

    This is a *process-lifetime* fact chosen by the operator at start-up —
    ``creek-tools-mcp --transport`` — and it is deliberately not the same thing
    as :attr:`CallerIdentity.is_remote`, which is a *per-call* fact an adapter
    asserts about one request. The two agree in practice; they are separate
    because their lifetimes are, and conflating them is how a value cached at
    build time comes to describe a caller who arrived later.

    It lives in this module because this module already owns the one sentence
    that depends on it — :data:`REMOTE_ADMITTED_CEILINGS`, what a caller on the
    far side of a network may request. Writing the transport names down a
    second time (an argparse ``choices`` tuple here, a module constant in the
    handshake there) is exactly how ``creek.handshake`` came to tell a
    streamable-HTTP consumer it was talking over ``stdio``. The argument
    parser's choices, the server it builds, and the handshake it answers with
    all read these members now.
    """

    STDIO = "stdio"
    NETWORK = "network"

    @property
    def is_remote(self) -> bool:
        """Whether a caller reaching a server on this transport crossed a network.

        Note what this is *not*: it is not a way for policy to sniff a caller's
        remoteness out of ambient state. The transport is handed in by the
        adapter that was started on it, the same way ``is_remote`` is handed to
        :class:`CallerIdentity` — this property only spells out which of the two
        published transports is the networked one, so no caller has to
        rediscover it by string comparison.

        Returns:
            ``True`` for :attr:`NETWORK`, ``False`` for :attr:`STDIO`.
        """
        return self is Transport.NETWORK


REMOTE_ADMITTED_CEILINGS: Final[frozenset[TierCeiling]] = frozenset(
    {TierCeiling.OPEN, TierCeiling.PERSONAL}
)
"""The tier ceilings a remote (network) caller may request.

``INTIMATE`` and ``ALL`` are excluded so intimate content can never be reached
over the network — the load-bearing boundary of #759, and the reason
:class:`creek_mcp.api.models.WireTierCeiling` has exactly these two members.
Local callers are not capped: the *network* is the boundary, not the tier.
"""

REMOTE_CEILING_REFUSAL_REASON: Final[str] = (
    "remote consumers may not request a ceiling above "
    f"'{TierCeiling.PERSONAL.value}'; intimate content is not "
    "reachable over the network"
)
"""The single reason every refused remote request is given.

One constant, not a template. It describes the rule and names no fragment, no
path, no count and no requested value, so a remote consumer learns nothing from
a refusal beyond the published rule it already had (#1090). Interpolating the
caller's input here — even "for debugging" — is what turns the refusal into a
channel, so the tests pin the wording against a hard-coded literal and pin that
it carries no ``{``, ``}`` or ``%``.
"""


def offered_ceilings(transport: Transport) -> tuple[TierCeiling, ...]:
    """The tier ceilings a caller on *transport* may actually request.

    The negotiation half of :data:`REMOTE_ADMITTED_CEILINGS`, and its only
    other reader. ``admitted_ceiling`` refuses an over-ceiling request once it
    has been made; this answers the same question *before* the caller makes
    one, so ``creek.handshake`` can publish the menu the caller will actually
    be served rather than the four-member enum. Advertising ``intimate`` and
    ``all`` to a network consumer that :func:`admitted_ceiling` refuses before
    dispatch is not a courtesy — it is the contract describing a door that is
    bolted shut.

    Returned in :class:`~creek_mcp.tier_ceiling.TierCeiling` declaration order
    (most restrictive first), not in set order, so the published list is stable
    across processes.

    Args:
        transport: The channel the server was started on.

    Returns:
        The requestable ceilings, least- to most-permissive: every member for a
        local ``stdio`` server, and :data:`REMOTE_ADMITTED_CEILINGS` for a
        networked one.
    """
    admitted = (
        REMOTE_ADMITTED_CEILINGS if transport.is_remote else frozenset(TierCeiling)
    )
    return tuple(ceiling for ceiling in TierCeiling if ceiling in admitted)


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Who is calling, and whether they reached us over the network.

    Neither field has a default, on purpose. ``is_remote: bool = False`` would
    make a *forgotten* field fail open — a new adapter wired up by somebody who
    never heard of the flag would be treated as local and uncapped, which is
    precisely the #1073 bug reintroduced by omission. Requiring both makes
    "which side of the network is this?" a question the adapter has to answer
    out loud.

    Frozen because the answer must not change between the adapter's assertion
    and the decision: a mutable identity is a time-of-check/time-of-use hole
    where a handler could flip ``is_remote`` on the way past.

    Attributes:
        consumer: The identity the caller's credential names — audited as the
            author of the call. ``None`` for a local caller, which has no
            credential and is audited under the process default instead (see
            :func:`effective_consumer`).
        is_remote: Whether the call arrived over the network. Asserted by the
            adapter from its own transport, never inferred here.
    """

    consumer: str | None
    is_remote: bool


@dataclass(frozen=True, slots=True)
class Admission:
    """The call may proceed, at *ceiling*.

    Attributes:
        ceiling: The parsed ceiling the caller is admitted at, or ``None`` for
            "policy imposes no cap" — see :func:`admitted_ceiling` for why
            those are one answer rather than two.
    """

    ceiling: TierCeiling | None


@dataclass(frozen=True, slots=True)
class Refusal:
    """The call is refused before dispatch, and this is all the caller is told.

    Attributes:
        reason: Always :data:`REMOTE_CEILING_REFUSAL_REASON`. Carried as a
            field rather than looked up by the adapter so a second refusal
            rule can be added later without every adapter growing a branch —
            but it stays a constant, never a message built from the request.
    """

    reason: str


def _parse_ceiling(requested: object) -> TierCeiling | None:
    """Return *requested* as a :class:`TierCeiling`, or ``None`` if it is not one.

    Parsing has to happen *before* any membership test against
    :data:`REMOTE_ADMITTED_CEILINGS`: that is a ``frozenset``, and ``[] in
    frozenset()`` raises :class:`TypeError`, so testing the caller's raw value
    directly would let a remote caller turn a refusal into a server fault just
    by passing a list.

    The non-``str`` rejection is what makes that safe, and it is a type
    narrowing rather than a behaviour change: ``TierCeiling(0)`` raised
    :class:`ValueError` before, and an unhashable ``[]`` or ``{}`` never
    reaches a hash here at all. Enum members pass through because
    :class:`~creek_mcp.tier_ceiling.TierCeiling` is a ``StrEnum``, so its
    members are themselves ``str``.

    Args:
        requested: Whatever the caller supplied as a ceiling — a string, an
            enum member, or something that is neither. Deliberately typed
            ``object``: it is unvalidated caller input, and pretending
            otherwise is how a gate ends up trusting its argument.

    Returns:
        The matching member, or ``None`` when *requested* does not name one.
        Matching is exact: wrong case and surrounding whitespace do not name a
        ceiling, and are never repaired into one.
    """
    if not isinstance(requested, str):
        return None
    try:
        return TierCeiling(requested)
    except ValueError:
        return None


def admitted_ceiling(
    identity: CallerIdentity, requested: object
) -> Admission | Refusal:
    """Decide whether *identity* may make this call at *requested*.

    A remote caller gets the cap: only :data:`REMOTE_ADMITTED_CEILINGS` passes,
    and **anything else is refused, unrecognised values included**. That
    direction is the whole point — an unparseable ceiling must never be
    quietly coerced to ``open`` and dispatched, because coercion is how a
    typo'd or hostile value ends up admitted at *some* ceiling rather than at
    none.

    A local caller is not capped at all, and an unrecognised value from one is
    an ``Admission(ceiling=None)`` rather than a :class:`Refusal`. The
    asymmetry is deliberate: ``None`` states "policy imposes no cap, and this
    value is not policy's to validate". Argument *shape* belongs to the
    adapter's schema layer — FastMCP's pydantic coercion of the
    ``privacy_tier_ceiling: TierCeiling`` parameter — which is what raises on
    it, one layer down, exactly as it does today. Refusing here instead would
    move schema validation into the security policy and would change local
    behaviour, which this extraction is not permitted to do; answering with a
    *ceiling* would be worse still, since policy would then be inventing a cap
    nobody asked for out of a value nobody understood.

    Args:
        identity: Who is calling, and from which side of the network.
        requested: The ceiling as the caller spelled it. Unvalidated by
            construction — that is why this takes ``object``.

    Returns:
        An :class:`Admission` naming the parsed member, or a :class:`Refusal`
        carrying the one published reason. The parsed member is offered so an
        adapter *need* not re-parse the raw value; the MCP adapter currently
        does not take it up, because FastMCP's own schema layer coerces the
        argument one step later anyway. For the remote-admitted set the two
        parses cannot disagree — both accept exactly ``open`` and ``personal``.
    """
    member = _parse_ceiling(requested)
    if identity.is_remote:
        if member in REMOTE_ADMITTED_CEILINGS:
            return Admission(ceiling=member)
        return Refusal(reason=REMOTE_CEILING_REFUSAL_REASON)
    return Admission(ceiling=member)


def effective_consumer(identity: CallerIdentity, default: str) -> str:
    """Return the identity this call is audited under.

    A remote call is audited under the consumer its credential names, so the
    audit log can say *which* network consumer acted. A local call keeps the
    process-global default (``CREEK_MCP_CONSUMER``, resolved by the adapter and
    passed in), and keeps it even if an identity carries a consumer: a stale or
    carried-over value winning on the local path would start attributing the
    operator's own stdio calls to a network consumer, which is misattribution
    nobody would notice until it mattered.

    Args:
        identity: Who is calling, and from which side of the network.
        default: The process-global consumer a local call is audited under.

    Returns:
        ``identity.consumer`` when remote; *default* when local.

    Raises:
        ValueError: When a remote identity's consumer is ``None``. This is the
            one row the pre-#1073 code could not represent at all — it read
            ``token.client_id`` off an ``AccessToken``, whose ``client_id`` is
            typed ``str`` — so there is no prior behaviour to preserve here and
            the fail-closed answer is free. It is a hard failure rather than a
            fallback to *default* precisely because the fallback is the
            dangerous answer: it would stamp a network call with the local
            operator's identity, forging the one field the audit trail exists
            to record.

            An **empty** consumer is deliberately *not* rejected, and returns
            ``""`` exactly as before. That row was representable before the
            extraction, so refusing it now would be a behaviour change smuggled
            into a pure move. It is also the wrong place to refuse: this
            function is called inside the tool wrapper at every
            ``consumer=_effective_consumer(...)`` site, so raising here escapes
            into the FastMCP tool surface and skips the audit entry the call
            was going to write — losing the trail for precisely the call whose
            attribution is suspect. :mod:`creek_mcp.auth` records the same
            hazard for the elevated gate. A blank ``client_id`` belongs to the
            verifier boundary, refused before dispatch;
            :func:`creek_mcp.remote_auth.load_consumer_tokens` already drops
            blank consumer names, so only a custom ``token_verifier`` handed to
            :func:`creek_mcp.server.build_server` can produce one. Tightening
            that is #1100.
    """
    if not identity.is_remote:
        return default
    if identity.consumer is None:
        msg = "a remote call must name the consumer its credential identifies"
        raise ValueError(msg)
    return identity.consumer
