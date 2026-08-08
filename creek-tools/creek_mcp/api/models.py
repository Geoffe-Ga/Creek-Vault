"""Pydantic wire models for the Adepthood ``/v1`` HTTP application API (#1072).

Every name in this module is part of a published cross-repo contract. Adepthood
reads the JSON Schemas generated from these classes (see
:mod:`creek_mcp.api.bundle`), so a rename here is a contract change, not a
refactor.

**Remote by construction.** ``/v1`` is only ever reached over the network, and
:func:`creek_mcp.policy.admitted_ceiling` — the transport-neutral owner of the
rule since #1073, applied on the MCP side by
:meth:`creek_mcp.server._BoundedFastMCP.call_tool` — already caps every remote
caller at :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` — ``open`` and
``personal``. :class:`WireTierCeiling` therefore has exactly two members. That
is deliberate: ``intimate`` is made unreachable *in the type*, in every wire
position (request tier, response tier, routed tier), rather than by a runtime
check somebody can later forget to call. There is no admissible value a
producer or a consumer could put on the wire that names it.

**No third tier reader (#1079).** Nothing in this module opens a file or reads
a fragment's ``privacy_tier``. Admission stays where it already lives —
:mod:`creek_mcp.tier_ceiling` at the boundary, and
:mod:`creek.classify.privacy_filter` for body-level filtering. These models are
declarative; the only executable code is a handful of validators.

**Framework-free.** No ``fastapi`` / ``starlette`` / ``uvicorn`` / ``httpx``
import may appear here (a test AST-checks it): #1074 chooses the framework, and
the vocabulary must not presuppose it.

**The error envelope echoes nothing.** :class:`ErrorEnvelope` carries exactly
``code``, ``message`` and ``request_id``, forbids extras, and draws its message
from :data:`ERROR_MESSAGES` — a table of constants that never sees caller data.
No message contains a ``{``, so no later refactor can turn one into a format
string that interpolates request material.

**``not_found`` is a routing code, never a content code.** It answers "there is
no such endpoint / no such capability on this server". It must never be emitted
for a vault object. Doing so would rebuild the existence oracle that #846,
#970, #972 and #1090 spent five issues collapsing: a caller who can tell "no
such fragment" from "you may not see this fragment" can enumerate the corpus
one id at a time without ever reading a byte of it. Every vault-object
non-answer — above the ceiling, purged, orphaned, schema-invalid, or deleted
out of band — collapses to :attr:`ErrorCode.PRIVACY_REFUSED` carrying
:data:`creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON`.

Today that rule is enforced by this vocabulary alone: there is no handler to
break it, since ``/v1`` has no routes until #1074 mounts them. The structural
guard that keeps it from regressing once handlers exist — an AST sweep over
every ``NOT_FOUND`` construction site in :mod:`creek_mcp`, checked against a
pinned routing-layer allowlist — is tracked in #1098.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from creek_mcp.contract import CONTRACT_VERSION
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON

# --------------------------------------------------------------------------
# Version vocabulary
# --------------------------------------------------------------------------

_MINOR_VERSION_COMPONENTS: Final[int] = 2
"""Leading dotted components that make up a contract *minor*."""

CONTRACT_MINOR: Final[str] = ".".join(
    CONTRACT_VERSION.split(".")[:_MINOR_VERSION_COMPONENTS]
)
"""The ``major.minor`` prefix of :data:`creek_mcp.contract.CONTRACT_VERSION`.

Derived rather than restated, so the published minor cannot drift from the
runtime version string it is a prefix of. Compatibility is negotiated at minor
granularity: a patch bump is invisible to the consumer, a minor bump is not.
"""

SUPPORTED_CONTRACT_MINORS: Final[tuple[str, ...]] = (CONTRACT_MINOR, "0.2")
"""Every contract minor this server can still serve.

The widening this constant's shape anticipated has happened: contract 0.3
(#1023) added the ``creek.upload`` *MCP* tool and changed no ``/v1`` wire
shape, so ``0.2`` is retained and every client already sending
``X-Creek-Contract-Version: 0.2`` keeps being served. It is a *list* on the
wire because the compatibility window is expected to widen before it ever
narrows, and the client needs to see the whole window without a second round
trip. Drop an entry only when a ``/v1`` shape actually stops being served.
"""

OK_STATUS: Final[str] = "ok"
"""The sole success status on the journal-upsert and wheel envelopes.

Both carry ``status: "ok"`` and nothing else, because every non-success outcome
is an :class:`ErrorEnvelope` with its own HTTP status. A second success-ish
status value would give a consumer two places to look for failure.
"""


# --------------------------------------------------------------------------
# Closed enums
# --------------------------------------------------------------------------


class WireTierCeiling(StrEnum):
    """The only two tier ceilings expressible on ``/v1``.

    ``/v1`` is remote by construction, and
    :func:`creek_mcp.policy.admitted_ceiling` refuses any remote call declaring
    a ceiling outside :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` *before
    dispatch*. Since #1073 that decision is transport-neutral, so every adapter
    is meant to reach it the same way; the MCP surface does so today through
    :meth:`creek_mcp.server._BoundedFastMCP.call_tool`, and #1074 mounts the
    ``/v1`` handlers that will. This enum mirrors that frozenset, so
    ``intimate`` and ``all`` are not merely refused at runtime — they cannot be
    constructed at all, which is the half of the guarantee that does not depend
    on a handler remembering to ask.

    The same two values double as the wire's *tier* vocabulary (see
    :class:`JournalUpsertRequest`, :class:`JournalUpsertResponse` and
    :class:`ReflectionResponse`). The two vocabularies coincide by
    construction: the set of tiers a remote caller may name is exactly the set
    of ceilings a remote caller may declare, because a tier above the remote
    cap is one no remote request can reach in either direction.

    Attributes:
        OPEN: Openly publishable content only (ontology §13.2).
        PERSONAL: Adds personal — and, per #961, ``unclassified`` — content.
    """

    OPEN = "open"
    PERSONAL = "personal"


class Capability(StrEnum):
    """The four capabilities ``/v1`` publishes.

    The list is identical for every minor in
    :data:`SUPPORTED_CONTRACT_MINORS`: contract 0.3 grew the *MCP* tool surface
    (``creek.upload``, #1023) and added no ``/v1`` route, so a ``0.2`` client
    and a ``0.3`` client are answered from the same four names.

    The values are also the directory names of the example matrix in
    :mod:`creek_mcp.api.bundle`, so the advertised capability list and the
    documented fixtures cannot name different things.

    Attributes:
        CAPABILITIES: Version and feature handshake; safe before any read.
        JOURNAL_UPSERT: Idempotent journal entry create/update.
        REFLECTIONS: Margin notes on an entry, care-guarded.
        WHEEL: Aggregate APTITUDE frequency distribution.
    """

    CAPABILITIES = "capabilities"
    JOURNAL_UPSERT = "journal-upsert"
    REFLECTIONS = "reflections"
    WHEEL = "wheel"


class CapabilitiesStatus(StrEnum):
    """Overall readiness reported by the capabilities handshake.

    Attributes:
        OK: The vault is present and this server can serve the contract.
        UNINITIALIZED: No vault has been scaffolded yet. Both version strings
            are still reported, so a client can always negotiate versions
            against a server whose vault does not exist.
        INCOMPATIBLE: The client's requested contract minor is not in
            :data:`SUPPORTED_CONTRACT_MINORS`.
    """

    OK = "ok"
    UNINITIALIZED = "uninitialized"
    INCOMPATIBLE = "incompatible"


class JournalAction(StrEnum):
    """What an idempotent journal upsert actually did.

    ``UNCHANGED`` is a success, not an error: re-sending an unmodified entry is
    the steady state of a continuous sync, and a client must be able to tell it
    apart from a write without diffing content itself.

    Attributes:
        CREATED: A new fragment was written.
        UPDATED: An existing fragment was updated in place.
        UNCHANGED: The content hash matched; nothing was written.
    """

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class ReflectionStatus(StrEnum):
    """Outcome of a reflection request.

    Attributes:
        OK: At least one margin note was produced.
        EMPTY: The request succeeded and yielded no notes. Not an error.
        ESCALATE: The acute-distress guard fired; the response is a
            :class:`CareEscalationResponse` carrying
            :data:`creek.care.guardrail.CARE_SIGNAL` instead of model prose.
    """

    OK = "ok"
    EMPTY = "empty"
    ESCALATE = "escalate"


class NoteKind(StrEnum):
    """The seven margin-note kinds, mirroring the reflect tool's vocabulary.

    Kept in lock-step with :data:`creek_mcp.tools.reflect._ALLOWED_KINDS`,
    which is what the produced notes are actually constrained to; a wire enum
    that drifted from it would let a legitimate note fail validation on the way
    out.

    Attributes:
        FEAR: A named fear surfacing in the entry.
        GIFT: Something the writer is giving or has been given.
        LONGING: An unmet want the writing circles.
        PATTERN: A recurrence the writer has not named yet.
        REFRAME: The same material seen from another angle.
        TENSION: Two things the entry holds at once.
        VALUE: A commitment the entry reveals.
    """

    FEAR = "fear"
    GIFT = "gift"
    LONGING = "longing"
    PATTERN = "pattern"
    REFRAME = "reframe"
    TENSION = "tension"
    VALUE = "value"


class ErrorCode(StrEnum):
    """The nine wire error codes, closed at contract 0.3 and unchanged since 0.2.

    There is deliberately **no** ``care_escalation`` member: an escalation is a
    successful, 200-shaped :class:`CareEscalationResponse`, because a person in
    acute distress must not have their response swallowed by a client's error
    path.

    Attributes:
        UNAUTHENTICATED: No valid consumer credential accompanied the request.
        INVALID_REQUEST: The body does not satisfy the published schema.
        INCOMPATIBLE_VERSION: The requested contract minor is not served here.
        PRIVACY_REFUSED: The single answer for every vault-object non-answer.
            Above the ceiling, purged, orphaned, schema-invalid and deleted out
            of band all collapse to this one code with this one message.
        NOT_FOUND: A **routing** code only — no such endpoint or path on this
            server. Never emitted for a vault object. Emitting it for a
            fragment would reintroduce the existence oracle that #846 / #970 /
            #972 / #1090 collapsed: the difference between "no such id" and
            "not for you" is a corpus enumeration primitive.
        UNSUPPORTED_CAPABILITY: A published capability this server does not
            implement.
        UNAVAILABLE: The vault is absent or unreadable; a human must act.
        TEMPORARILY_UNAVAILABLE: A transient condition; backoff will clear it.
        INTERNAL_ERROR: An unexpected server fault.
    """

    UNAUTHENTICATED = "unauthenticated"
    INVALID_REQUEST = "invalid_request"
    INCOMPATIBLE_VERSION = "incompatible_version"
    PRIVACY_REFUSED = "privacy_refused"
    NOT_FOUND = "not_found"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    UNAVAILABLE = "unavailable"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INTERNAL_ERROR = "internal_error"


class RetryDisposition(StrEnum):
    """What a client should do about an error, decided by the code alone.

    Attributes:
        TERMINAL: Never retry this ``(endpoint, identifier)`` pair. The answer
            will not change on its own.
        RETRY_AFTER_OPERATOR_ACTION: A human must act first; backoff alone will
            never clear it.
        RETRY_WITH_BACKOFF: Transient. Retry with exponential backoff.
    """

    TERMINAL = "terminal"
    RETRY_AFTER_OPERATOR_ACTION = "retry_after_operator_action"
    RETRY_WITH_BACKOFF = "retry_with_backoff"


# --------------------------------------------------------------------------
# Error tables — each total over ErrorCode
# --------------------------------------------------------------------------

ERROR_STATUS: Final[dict[ErrorCode, int]] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.INCOMPATIBLE_VERSION: 409,
    ErrorCode.PRIVACY_REFUSED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.UNSUPPORTED_CAPABILITY: 501,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TEMPORARILY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}
"""HTTP status for each wire error code.

Total over :class:`ErrorCode`, so no dispatch path can ``KeyError`` while
building a response. As prose: 401 Unauthorized, 422 Unprocessable Content, 409
Conflict, 403 Forbidden, 404 Not Found, 501 Not Implemented, 503 Service
Unavailable (twice — the two 503s differ only in retry disposition, which is
the distinction that actually matters to a client) and 500 Internal Server
Error.

``PRIVACY_REFUSED`` is 403 rather than 404 on purpose. 404 would be the very
oracle :attr:`ErrorCode.NOT_FOUND` is fenced off to avoid: a caller able to
distinguish "absent" from "forbidden" can enumerate the corpus.
"""

ERROR_MESSAGES: Final[dict[ErrorCode, str]] = {
    ErrorCode.UNAUTHENTICATED: "the request carried no valid consumer credential",
    ErrorCode.INVALID_REQUEST: "the request does not satisfy the published schema",
    ErrorCode.INCOMPATIBLE_VERSION: "the requested contract minor is not served here",
    ErrorCode.PRIVACY_REFUSED: GENERIC_ABOVE_CEILING_REASON,
    # Phrased about the ROUTE, never about a resource. "the requested resource
    # does not exist" would read as a statement about a vault object on a path
    # carrying {external_id} — and a 404 a caller could distinguish from a 403
    # over a vault object is the existence oracle #846/#970/#972/#1090
    # collapsed.
    ErrorCode.NOT_FOUND: "no such endpoint on this server",
    ErrorCode.UNSUPPORTED_CAPABILITY: "this capability is not implemented here",
    ErrorCode.UNAVAILABLE: "the vault is not available to serve this request",
    ErrorCode.TEMPORARILY_UNAVAILABLE: "the service is briefly unable to answer",
    ErrorCode.INTERNAL_ERROR: "the server failed to complete this request",
}
"""The one message per error code, and the only text an envelope may carry.

Constants, never templates. None contains a ``{`` or a ``%s``, which is the
property a test pins: a message with a format placeholder is a message someone
will eventually interpolate caller — or resolved vault — material into, and
that is how a refusal starts ranking content the caller was never admitted to.

:attr:`ErrorCode.PRIVACY_REFUSED` is bound to
:data:`creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` by import, never by a
copy of its text. A copied string drifts, and a drifted refusal reason is a
tier-classification oracle: the whole point of that constant is that it reads
identically for every tier above every ceiling.
"""

RETRY_POLICY: Final[dict[ErrorCode, RetryDisposition]] = {
    ErrorCode.UNAUTHENTICATED: RetryDisposition.TERMINAL,
    ErrorCode.INVALID_REQUEST: RetryDisposition.TERMINAL,
    ErrorCode.INCOMPATIBLE_VERSION: RetryDisposition.TERMINAL,
    ErrorCode.PRIVACY_REFUSED: RetryDisposition.TERMINAL,
    ErrorCode.NOT_FOUND: RetryDisposition.TERMINAL,
    ErrorCode.UNSUPPORTED_CAPABILITY: RetryDisposition.TERMINAL,
    ErrorCode.UNAVAILABLE: RetryDisposition.RETRY_AFTER_OPERATOR_ACTION,
    ErrorCode.TEMPORARILY_UNAVAILABLE: RetryDisposition.RETRY_WITH_BACKOFF,
    ErrorCode.INTERNAL_ERROR: RetryDisposition.RETRY_WITH_BACKOFF,
}
"""Retryability, keyed on the code alone — the #1082 answer.

**Why ``PRIVACY_REFUSED`` is ``TERMINAL``.** ``creek classify`` is
escalate-only: a fragment's tier can rise to ``intimate`` but never fall back.
A remote consumer is capped at ``ceiling=personal`` by
:data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS`. So once the fragment behind
an ``external_id`` has reached ``intimate``, no remote consumer can ever send
that id again — not even an unchanged, idempotent re-sync, because the ceiling
gate sits *above* the content-hash compare in ``write_fragment_idempotent`` and
therefore refuses before the "nothing changed" shortcut is ever reached. A
client that treats this as transient loops forever on an entry it can never
deliver. The correct behaviour is to mark that ``(endpoint, identifier)`` pair
terminal and escalate to a local operator, who is not subject to the remote
cap.

**And a client must not infer a tier from it.** The refusal deliberately blurs
"above your ceiling" with "unresolvable" — purged, orphaned, schema-invalid,
deleted out of band. Reading ``TERMINAL`` as "therefore intimate" reconstructs
exactly the bit the blur exists to withhold.

**Why a table and not a field.** Retryability is carried by this static table,
keyed on the code, and never by a per-response ``retryable`` boolean. A
computed field is an open invitation to refine it — "this privacy refusal is
retryable, that one is not" — and a disposition that varies *within* one code
is a one-bit oracle wearing a helpful-ergonomics hat.

If a content-hash carve-out for the unchanged-resend case ever lands, that is a
contract change with a minor bump, not a quiet relabelling of this entry.
"""


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

_MIN_COUNT: Final[int] = 0
"""Lower bound for every tally on the wire; a count is never negative."""

_MIN_SHARE: Final[float] = 0.0
"""Lower bound for a proportion."""

_MAX_SHARE: Final[float] = 1.0
"""Upper bound for a proportion."""

_MIN_NOTES: Final[int] = 1
"""Fewest margin notes a reflection request may ask for."""

_MAX_NOTES: Final[int] = 10
"""Most margin notes a reflection request may ask for."""

_DEFAULT_WIRE_MAX_NOTES: Final[int] = 3
"""Notes returned when a ``/v1`` request does not say, so a bare request is total.

Deliberately **half** :data:`creek_mcp.tools.reflect._DEFAULT_MAX_NOTES` (6),
and named differently so the two are not mistaken for one constant that has
drifted. The tool default serves a local agent caller reading a whole
reflection; the wire default serves a mobile client rendering margin notes
beside an entry, where six notes is more than the surface can show and each
one costs model tokens. A ``/v1`` caller that wants the tool's default asks
for it explicitly — ``max_notes`` is published, bounded and required to be in
``[1, 10]``, so the difference is visible in the contract rather than hidden
in a default.
"""

_MIN_CARE_RESOURCES: Final[int] = 1
"""A care signal with no resources would dead-end the person reading it."""


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class _WireModel(BaseModel):
    """Base for every published wire model: closed, no silent passthrough.

    ``extra="forbid"`` across the whole surface is a contract property, not a
    style preference. An open model lets a producer bolt a debug key onto a
    response and a consumer start depending on it, at which point the field
    exists without ever having been negotiated — and on an error path such a
    key is, by construction, derived from material the caller may not be
    entitled to.
    """

    model_config = ConfigDict(extra="forbid")


class ErrorEnvelope(_WireModel):
    """The one error shape on ``/v1``: three fields, and never a fourth.

    It echoes nothing. Not the request body, not the resolved tier, not the
    identifier that was probed. Everything a caller needs in order to act is in
    :data:`ERROR_STATUS` and :data:`RETRY_POLICY`, both keyed on :attr:`code`
    alone; everything else they might want is something this server must not
    tell them.

    Attributes:
        code: The closed wire code. Look up HTTP status, human message and
            retry disposition from it.
        message: A constant drawn from :data:`ERROR_MESSAGES`. Never composed
            from request or vault material.
        request_id: A server-generated correlation id for the operator's logs.
            Opaque to the client, and derived from nothing the caller sent.
    """

    code: ErrorCode = Field(description="Closed wire error code.")
    message: str = Field(description="Constant message from ERROR_MESSAGES.")
    request_id: str = Field(description="Server-generated correlation id.")


class TierModel(_WireModel):
    """What the server promises about privacy tiers, advertised up front.

    Attributes:
        ceilings: Every ceiling this surface admits — the two members of
            :class:`WireTierCeiling`, in ascending breadth.
        default: The ceiling applied when a caller declares none. Pinned to
            ``open``, the most restrictive value, so an omitted ceiling can
            only ever fail closed.
        intimate_never_egresses: ``Literal[True]``. A promise, not a flag: the
            schema admits no other value, so a server cannot advertise the
            opposite and a client need not branch on it.
    """

    ceilings: list[WireTierCeiling] = Field(
        description="Tier ceilings this surface admits.",
    )
    default: Literal[WireTierCeiling.OPEN] = Field(
        description="Ceiling applied when the caller declares none.",
    )
    intimate_never_egresses: Literal[True] = Field(
        description="Standing promise that intimate content never egresses.",
    )


class VaultState(_WireModel):
    """Whether a vault exists behind this server.

    Deliberately one boolean. Anything richer — fragment counts, last-write
    timestamps, initialisation progress — is a measurement of the corpus, and
    the handshake is reachable before any tier gate has run.

    Attributes:
        available: ``True`` when a scaffolded vault is readable.
    """

    available: bool = Field(description="True when a scaffolded vault is readable.")


class CapabilitiesResponse(_WireModel):
    """The handshake: versions, vault readiness and the capability list.

    Safe to call before anything else, including against an uninitialised vault
    — which is why both version strings are required regardless of
    :attr:`status`. A client must always be able to read the version off a
    server whose vault does not exist yet, or version negotiation would need a
    vault to negotiate about.

    Attributes:
        status: Overall readiness.
        contract_version: :data:`creek_mcp.contract.CONTRACT_VERSION`.
        contract_minor: The ``major.minor`` this response speaks.
        supported_contract_minors: Every minor still served here.
        ontology_version: :data:`creek_mcp.contract.ONTOLOGY_VERSION` — the
            shared frequency/phase vocabulary. A mismatch means "renegotiate".
        vault: Vault readiness.
        tier_model: The standing tier promise.
        capabilities: The capabilities actually served. Empty on an
            uninitialised vault.
    """

    status: CapabilitiesStatus = Field(description="Overall server readiness.")
    contract_version: str = Field(description="Full semantic contract version.")
    contract_minor: str = Field(description="The major.minor spoken here.")
    supported_contract_minors: list[str] = Field(
        description="Every contract minor this server still serves.",
    )
    ontology_version: str = Field(
        description="Shared APTITUDE frequency / Wavelength phase vocabulary.",
    )
    vault: VaultState = Field(description="Vault readiness.")
    tier_model: TierModel = Field(description="Standing privacy-tier promise.")
    capabilities: list[Capability] = Field(
        description="Capabilities served; empty on an uninitialised vault.",
    )


class JournalUpsertRequest(_WireModel):
    """An idempotent journal write from a remote consumer.

    Attributes:
        content: The entry body. Whitespace-only is refused rather than
            written: an empty fragment is indistinguishable from a corrupted
            one once it is in the vault, and it would still consume an
            ``external_id``.
        timestamp: Optional client-supplied entry time. Omitted means "the
            server stamps it", so a client with no reliable clock is still a
            valid client.
        tier: The tier to create the entry at. Typed
            :class:`WireTierCeiling`, so ``intimate`` is not expressible — a
            remote consumer cannot even *ask* for an intimate write.
    """

    content: str = Field(description="Entry body; must not be blank.")
    timestamp: str | None = Field(
        default=None,
        description="Optional client-supplied entry time.",
    )
    tier: WireTierCeiling = Field(description="Tier to create the entry at.")

    @field_validator("content")
    @classmethod
    def _reject_blank_content(cls, value: str) -> str:
        """Refuse whitespace-only content.

        Args:
            value: The submitted entry body.

        Returns:
            *value* unchanged when it carries a non-space character.

        Raises:
            ValueError: When *value* is empty or whitespace-only.
        """
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class JournalUpsertResponse(_WireModel):
    """The result of an idempotent journal write.

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call was served under — the caller's own
            declared value, echoed for correlation.
        external_id: The consumer-side identity of the entry, unchanged.
        fragment_id: The vault-side identity now backing it.
        action: Whether the write created, updated, or changed nothing.
        tier: The tier the entry was created at. Not expressible as
            ``intimate``, for the same reason as on the request.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    external_id: str = Field(description="Consumer-side entry identity.")
    fragment_id: str = Field(description="Vault-side fragment identity.")
    action: JournalAction = Field(description="Created, updated, or unchanged.")
    tier: WireTierCeiling = Field(description="Tier the entry was created at.")


class ReflectionRequest(_WireModel):
    """A request for margin notes on exactly one source.

    Attributes:
        content: Inline text supplied by the caller — their own words, so no
            vault tier applies to it.
        entry_ref: A vault fragment reference instead. Resolving it is a read,
            so it is subject to the ceiling gate.
        max_notes: How many notes at most, bounded to ``[1, 10]`` inclusive.
    """

    content: str | None = Field(default=None, description="Inline text to reflect on.")
    entry_ref: str | None = Field(
        default=None,
        description="Vault fragment reference to reflect on.",
    )
    max_notes: int = Field(
        default=_DEFAULT_WIRE_MAX_NOTES,
        ge=_MIN_NOTES,
        le=_MAX_NOTES,
        description="Maximum margin notes to return.",
    )

    @model_validator(mode="after")
    def _require_exactly_one_source(self) -> Self:
        """Require exactly one of ``content`` / ``entry_ref``.

        Both together is ambiguous, and the server must not silently rank one
        above the other: a client that believed ``entry_ref`` won while the
        server preferred ``content`` would be reflecting on the wrong text
        without either side noticing. Neither is a client bug, and answering it
        with an empty result would hide that bug behind a plausible response.

        A *blank* source is refused alongside a missing one. Checking only
        ``is None`` would admit ``content=""`` as a legitimate single source,
        so the request would pass validation and then be refused deeper in
        ``reflect_tool`` (``"no entry content supplied"``) — an avoidable
        round-trip, and an inconsistency with
        :class:`JournalUpsertRequest`, which already refuses blank ``content``
        at the boundary.

        Returns:
            The validated request.

        Raises:
            ValueError: When both sources are supplied, neither is, or the
                supplied one is blank.
        """
        if (self.content is None) == (self.entry_ref is None):
            raise ValueError("supply exactly one of content or entry_ref")
        supplied = self.content if self.content is not None else self.entry_ref
        if supplied is not None and not supplied.strip():
            raise ValueError("the supplied content or entry_ref is blank")
        return self


class ReflectionNote(_WireModel):
    """One margin note: a verbatim span, a kind, and the reflection itself.

    Attributes:
        quote: A span copied verbatim from the entry. The producing tool
            verifies this; a note whose quote is not verbatim is dropped rather
            than returned.
        kind: One of the seven :class:`NoteKind` values.
        note: The reflection, addressed to the writer in second person.
    """

    quote: str = Field(description="Verbatim span copied from the entry.")
    kind: NoteKind = Field(description="Margin-note kind.")
    note: str = Field(description="The reflection itself.")


class CareResource(_WireModel):
    """A way to reach a human, named and contactable.

    Attributes:
        name: What the resource is called.
        contact: How to reach it. Required — a resource without a contact
            dead-ends the person reading it at the worst possible moment.
    """

    name: str = Field(description="Name of the care resource.")
    contact: str = Field(description="How to reach it; required.")


class CareSignal(_WireModel):
    """The structured "reach a human" envelope, mirroring the runtime constant.

    Validates :data:`creek.care.guardrail.CARE_SIGNAL` itself rather than an
    abridged copy of it, so the wire shape cannot fall behind the guardrail.

    Attributes:
        kind: The care guard that fired.
        message: Prose addressed to the person, not to the client.
        resources: At least one contactable resource. The signal never
            dead-ends.
    """

    kind: str = Field(description="The care guard that fired.")
    message: str = Field(description="Prose addressed to the person.")
    resources: list[CareResource] = Field(
        min_length=_MIN_CARE_RESOURCES,
        description="At least one contactable resource.",
    )


class ReflectionResponse(_WireModel):
    """Margin notes for one entry.

    Attributes:
        status: ``ok`` or ``empty``. ``escalate`` arrives as a
            :class:`CareEscalationResponse` instead.
        tier_ceiling: The caller's own declared ceiling, echoed.
        routed_tier: The tier the model call was keyed with. **Safe to expose,
            and provably so:** for every ceiling ``/v1`` permits and every
            :class:`~creek.models.PrivacyTier` that ceiling admits,
            ``routing_tier(ceiling, tier)`` equals
            ``CEILING_ROUTING_TIER[ceiling]``. It is a constant function of the
            caller's *own declared ceiling*, so it carries zero bits about the
            content's tier — the caller learns only what they themselves
            supplied. The invariant holds because ``routing_tier`` takes the
            more sensitive of the ceiling-derived tier and the content tier,
            and an admitted tier is by definition no more sensitive than its
            ceiling. It is pinned by
            ``test_routed_tier_is_a_constant_function_of_the_declared_ceiling``
            in ``tests/test_adepthood_contract_models.py``. **If that test ever
            fails, remove this field — do not relax the test.** A
            ``routed_tier`` that varies with content is a one-bit tier oracle
            over the whole corpus.
        notes: The margin notes; empty when ``status`` is ``empty``.
        essay: Optional free prose. Ungrounded by construction.
        essay_grounded: Required, and only ``False`` is admissible at 0.2.
    """

    status: Literal[ReflectionStatus.OK, ReflectionStatus.EMPTY] = Field(
        description="ok or empty; escalate is CareEscalationResponse instead.",
    )
    tier_ceiling: WireTierCeiling = Field(description="Caller's declared ceiling.")
    routed_tier: WireTierCeiling = Field(
        description="Tier the call was keyed with; a function of tier_ceiling.",
    )
    notes: list[ReflectionNote] = Field(description="Margin notes; may be empty.")
    essay: str | None = Field(default=None, description="Optional free prose.")
    # ``Literal[False]`` rather than ``bool`` plus a validator, so the
    # constraint reaches the PUBLISHED schema as a ``const`` and not only the
    # running server. A bare ``bool`` here made
    # ``schemas/ReflectionResponse.schema.json`` looser than the code it
    # documents: a consumer validating against the bundle would have accepted
    # ``essay_grounded: true``, which the server rejects. Publishing a schema
    # weaker than the server is the exact drift this bundle exists to prevent.
    #
    # No grounded-essay path exists at contract 0.2: ``essay`` is free model
    # prose and is never verbatim-checked the way ``notes[].quote`` is, so a
    # ``True`` claim is a producer bug and a consumer that trusted it would
    # cite ungrounded text. Required with no default, so a producer must state
    # the claim rather than inherit it. Lifting this is a capability a consumer
    # can detect, so it needs a contract minor bump — not a quiet relaxation.
    essay_grounded: Literal[False] = Field(
        description="Required; only False is admissible at contract 0.2.",
    )


class CareEscalationResponse(_WireModel):
    """A 200-shaped escalation: the care signal instead of model prose.

    Not an error, and there is no ``care_escalation`` member of
    :class:`ErrorCode`, because a person in acute distress must not have their
    response swallowed by a client's error path.

    Attributes:
        status: Pinned to :attr:`ReflectionStatus.ESCALATE`.
        tier_ceiling: The caller's own declared ceiling, echoed.
        reason: Which care guard fired.
        care_signal: The runtime :data:`creek.care.guardrail.CARE_SIGNAL`,
            whole.
    """

    status: Literal[ReflectionStatus.ESCALATE] = Field(
        description="Always escalate.",
    )
    tier_ceiling: WireTierCeiling = Field(description="Caller's declared ceiling.")
    reason: str = Field(description="Which care guard fired.")
    care_signal: CareSignal = Field(description="The runtime care signal, whole.")


class WheelFrequency(_WireModel):
    """One APTITUDE frequency's slice of the wheel.

    Attributes:
        name: The canonical frequency name from
            :data:`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`.
        count: Classified fragments at this frequency. A tally, so never
            negative.
        share: Fraction of *classified* fragments at this frequency, bounded to
            ``[0.0, 1.0]``.
    """

    name: str = Field(description="Canonical APTITUDE frequency name.")
    count: int = Field(ge=_MIN_COUNT, description="Classified fragments here.")
    share: float = Field(
        ge=_MIN_SHARE,
        le=_MAX_SHARE,
        description="Fraction of classified fragments at this frequency.",
    )


class WheelFrequencies(_WireModel):
    """All ten frequencies, as ten separate required fields.

    Ten declared fields rather than a mapping, on purpose. It is what makes the
    published JSON Schema say ``required: [F1 .. F10]`` — a consumer generating
    code from the schema gets ten guaranteed members instead of an open
    dictionary it has to defend against — and it fixes the emission order at
    the canonical one, so a diff of two wheels is a diff of numbers rather than
    of key ordering.

    Fields are declared in the order of
    :data:`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`. An eleventh
    frequency is a change to the shared ontology vocabulary — a contract change
    with a version bump — which is why ``extra="forbid"`` rejects one rather
    than passing it through.

    Attributes:
        F1: Agency.
        F2: Receptivity.
        F3: Self-Love / Power.
        F4: Community Love / Conformity.
        F5: Achievism.
        F6: Pluralism.
        F7: Integration.
        F8: True Self / Transcendence.
        F9: Unity.
        F10: Emptiness.
    """

    F1: WheelFrequency
    F2: WheelFrequency
    F3: WheelFrequency
    F4: WheelFrequency
    F5: WheelFrequency
    F6: WheelFrequency
    F7: WheelFrequency
    F8: WheelFrequency
    F9: WheelFrequency
    F10: WheelFrequency


class WheelResponse(_WireModel):
    """The aggregate APTITUDE frequency distribution of the admitted corpus.

    An empty corpus is an all-zero wheel with ``total_classified == 0``, not an
    error: a freshly-initialised vault is a legitimate state, and answering it
    with an error would make a client treat "new" as "broken".

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The caller's own declared ceiling, echoed. Fragments
            above it were excluded from the tally.
        total_classified: Admitted fragments carrying a frequency.
        unclassified: Admitted fragments carrying no frequency yet.
        wheel: The ten frequency slices.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Caller's declared ceiling.")
    total_classified: int = Field(
        ge=_MIN_COUNT,
        description="Admitted fragments carrying a frequency.",
    )
    unclassified: int = Field(
        ge=_MIN_COUNT,
        description="Admitted fragments with no frequency yet.",
    )
    wheel: WheelFrequencies = Field(description="The ten frequency slices.")


class NotApplicableExample(_WireModel):
    """Marks a fixture-matrix cell that has no response shape to document.

    The published example matrix is 4 capabilities x 7 states. Three of those
    cells — care escalation for ``capabilities``, ``journal-upsert`` and
    ``wheel`` — are *structurally* unreachable: the acute-distress guard runs
    only inside :func:`creek_mcp.tools.reflect.reflect_tool`, so no other
    capability can ever escalate.

    Both alternatives were worse. Fabricating a care-escalation response for
    those cells would publish a shape the server can never emit, and a consumer
    would write handling code for a branch that cannot be taken. Omitting the
    cells would break the rectangular matrix, and a hole in a matrix reads as
    "not documented yet" rather than "cannot happen" — which is exactly the
    distinction a cross-repo consumer needs.

    So the cell is filled with an explicit, machine-checkable statement of
    unreachability that says *why*.

    Attributes:
        unreachable: ``Literal[True]``. The claim is the whole point of the
            model, so the schema admits no other value.
        reason: Why this cell cannot occur.
    """

    unreachable: Literal[True] = Field(
        description="Always true; this cell has no reachable response shape.",
    )
    reason: str = Field(description="Why this cell is structurally unreachable.")


CONTRACT_MODELS: Final[dict[str, type[BaseModel]]] = {
    "CapabilitiesResponse": CapabilitiesResponse,
    "CareEscalationResponse": CareEscalationResponse,
    "CareResource": CareResource,
    "CareSignal": CareSignal,
    "ErrorEnvelope": ErrorEnvelope,
    "JournalUpsertRequest": JournalUpsertRequest,
    "JournalUpsertResponse": JournalUpsertResponse,
    "NotApplicableExample": NotApplicableExample,
    "ReflectionNote": ReflectionNote,
    "ReflectionRequest": ReflectionRequest,
    "ReflectionResponse": ReflectionResponse,
    "TierModel": TierModel,
    "VaultState": VaultState,
    "WheelFrequencies": WheelFrequencies,
    "WheelFrequency": WheelFrequency,
    "WheelResponse": WheelResponse,
}
"""Every published model, keyed by its own class name.

The key is the class name because the bundle manifest and the schema filenames
reference models by name; keying on anything else would let a rename land
without the published artifacts noticing. :mod:`creek_mcp.api.bundle` walks this
mapping to emit ``schemas/<name>.schema.json``, so a model added here is
published automatically and a model removed here disappears from the bundle —
in both cases turning the committed-bundle round-trip test red until the bundle
is regenerated.
"""
