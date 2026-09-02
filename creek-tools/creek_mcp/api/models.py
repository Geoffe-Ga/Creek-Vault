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
from urllib.parse import urlparse

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

SUPPORTED_CONTRACT_MINORS: Final[tuple[str, ...]] = (
    CONTRACT_MINOR,
    "0.12",
    "0.11",
    "0.10",
    "0.9",
    "0.8",
    "0.7",
    "0.6",
    "0.5",
    "0.4",
    "0.3",
    "0.2",
)
"""Every contract minor this server can still serve.

The widening this constant's shape anticipated has happened three times.
Contract 0.3 (#1023) added the ``creek.upload`` tool; contract 0.4 (#1246)
gave ``creek.purge.*`` a third status, ``partial``, so an erasure that fell
short stops reporting unqualified ``ok``. Both moved the *MCP tool surface*
while no ``/v1`` wire shape moved at all.

Contract 0.5 (#1372) is the first that is not purely that. It gave
``creek.journal``, ``creek.upload`` and ``creek.link`` the operator advisories
they had been computing and dropping, and it added one **optional** field —
``warnings`` on :class:`JournalUpsertResponse` — to a published ``/v1``
response and its committed schema. Be precise about what that costs an older
client, because the honest answer is not "nothing": the field defaults to
``None`` and the route dumps with ``exclude_none``, so a write that produced
no advisory is byte-identical to what it was before, which is the ordinary
case — but a write that *did* produce one now carries an extra key regardless
of the minor the caller negotiated. A ``0.4`` consumer validating closed sees
it in exactly that case. It is retained here anyway, because refusing every
``0.4`` request outright is strictly worse than serving one that occasionally
carries a field the client can ignore, and the window is published on
``GET /v1/capabilities`` for a client that wants to move. ``0.4``, ``0.3`` and
``0.2`` are all retained and every client already sending
``X-Creek-Contract-Version: 0.2`` keeps being served.

Contract 0.6 (#1453) is a pure MCP-surface move again, of the 0.3/0.4 kind:
``creek.purge.*`` results gained ``ledger_rows_removed`` and
``meta_artifacts_removed``, two integer counters that reach the wire because
``_result_payload`` dumps every field of the result model. No ``/v1`` response
shape moved, so a ``0.5`` client is served byte-identically on every ``/v1``
route it already calls.

Contract 0.7 (#1494) is the same kind again, and the sharpest case yet for
widening rather than shifting. ``creek.journal`` and ``creek.upload`` stopped
defaulting ``tier`` to ``open`` and now refuse a call that omits it, so a
``0.6`` client's *MCP* writes genuinely have to change. Its ``/v1`` traffic
does not: ``JournalUpsertRequest.tier`` never had a default, so the HTTP
adapter already required what MCP now requires, and ``creek.upload`` has no
``/v1`` route to move. Dropping ``0.6`` here would therefore refuse a
correct-by-construction ``/v1`` client over a break it cannot even express.

Contract 0.8 (#1524) is the first since 0.2 that is **not** an MCP-only move:
it publishes a fifth ``/v1`` route, ``POST /v1/uploads``, and with it the
``upload`` capability, two wire models and one error code. Widening rather than
shifting costs a ``0.7`` client nothing at all, because every ``/v1`` shape it
already knows is byte-identical — the addition is a route it is not told about
and cannot reach. That last clause is enforced, not hoped for: see
:data:`CAPABILITY_SINCE_MINOR`, which drives both what
``GET /v1/capabilities`` advertises to a given caller and which callers
``POST /v1/uploads`` will answer.

Contract 0.9 (#873) is a 0.5-shaped move: two **optional** fields —
``related_praxis`` and ``related_eddies`` — on :class:`ReflectionResponse`, and
the two wire models they are made of. The route omits both keys when nothing
qualified, so a ``0.8`` client's ordinary reflection is byte-identical; and one
that finds an admitted compiled neighbour carries two extra keys — which a
``0.8`` consumer validating against its vendored, closed schema would reject
outright, not ignore. So the fields are gated on the declared minor via
:data:`RELATED_FIELDS_SINCE_MINOR`: a ``0.8`` caller is served the ``0.8``
shape, byte-for-byte, whether or not a neighbour qualified. ``0.8`` is retained
for the same reason ``0.4`` was retained at 0.5 — refusing it outright is
strictly worse — but retention alone was not enough here.

Contract 0.12 (#1292) is a pure MCP-surface move again, of the 0.3/0.4/0.6/0.7
kind and the plainest one in the list. ``creek.redact.scan``'s ``statistics``
object gains a fifth typed key, ``files_skipped_symlink``, the count of
symlinked children the scan declined because their target resolves outside the
scanned root — a counter that has been on ``ScanSummary`` and in
``report_markdown`` since #1087 and never reached the wire. No ``/v1`` route,
capability, wire model, error code or status moves and ``CONTRACT_MODELS`` is
unchanged, so a ``0.11`` client is answered byte-identically on every ``/v1``
route it calls and there is nothing for it to be refused over. ``0.11`` is
therefore retained here.

Contract 0.13 (#874) is a pure MCP-surface move of the same kind, and the
plainest instance of why this tuple **widens and never shifts**. It adds one
read-only MCP tool, ``creek.classify.entry``. No ``/v1`` route, capability,
wire model, error code, status or schema moves and ``CONTRACT_MODELS`` is
unchanged, so a ``0.12`` client is answered byte-identically on every ``/v1``
route it calls — it simply is not told about a tool it cannot reach over HTTP
anyway. The explicit ``"0.12"`` below is therefore the whole of what keeps this
change additive: without it, ``CONTRACT_MINOR`` moves to ``"0.13"`` and every
client still sending ``X-Creek-Contract-Version: 0.12`` is refused
``incompatible_version`` on every capability route, over a break it cannot
observe.

Each retired minor is spelled out rather than derived: :data:`CONTRACT_MINOR`
is a *prefix of* :data:`~creek_mcp.contract.CONTRACT_VERSION`, so bumping the
version alone silently drops the outgoing current minor out of this window and
strands every client still sending it.

It is a *list* on the wire because the compatibility window is expected to
widen before it ever narrows, and the client needs to see the whole window
without a second round trip. Drop an entry only when a ``/v1`` shape actually
stops being served.
"""

OK_STATUS: Final[Literal["ok"]] = "ok"
"""The sole success status on the journal-upsert and wheel envelopes.

Both carry ``status: "ok"`` and nothing else, because every non-success outcome
is an :class:`ErrorEnvelope` with its own HTTP status. A second success-ish
status value would give a consumer two places to look for failure.

Typed ``Literal["ok"]`` rather than ``str`` so a handler can *build* a response
from it: :attr:`JournalUpsertResponse.status` and :attr:`WheelResponse.status`
are themselves ``Literal["ok"]``, and a plain ``str`` constant would not satisfy
them under strict typing — leaving a handler to spell the literal a second time,
which is the drift this constant exists to prevent.
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
    """The seven capabilities ``/v1`` publishes.

    The list was identical for every minor in
    :data:`SUPPORTED_CONTRACT_MINORS` up to and including 0.7: contract 0.3
    grew the *MCP* tool surface (``creek.upload``, #1023) and added no ``/v1``
    route, so a ``0.2`` client and a ``0.7`` client were answered from the same
    four names. Contract 0.8 (#1524) ends that by publishing ``UPLOAD``, and
    the list is therefore **no longer minor-independent**: what a caller is
    told, and what it is served, are both keyed on
    :data:`CAPABILITY_SINCE_MINOR`.

    The values are also the directory names of the example matrix in
    :mod:`creek_mcp.api.bundle`, so the advertised capability list and the
    documented fixtures cannot name different things.

    Attributes:
        CAPABILITIES: Version and feature handshake; safe before any read.
        JOURNAL_UPSERT: Idempotent journal entry create/update.
        REFLECTIONS: Margin notes on an entry, care-guarded.
        WHEEL: Aggregate APTITUDE frequency distribution.
        UPLOAD: Idempotent document upload — base64 bytes plus a filename,
            dispatched to an ingestor by extension. Since contract 0.8.
        DRIVE_CONNECTOR: The read-only Google Drive connector's *state* and
            its two verbs — one incremental sync, and disconnection. Since
            contract 0.9 (#1527). Three routes rather than one, and one
            capability rather than three, because a client cannot usefully
            negotiate "may I read the connector's state" separately from "may
            I sync it": they are one feature, and splitting them would let a
            server advertise half a connector.

            **It does not include authorisation.** No route begins, completes
            or carries an OAuth flow; the credential is minted locally by
            ``creek gdrive --download`` and never leaves the host.
        PIPELINE: Whole-vault classification and linking — the two passes that
            turn ingested bytes into a *usable* corpus. Since contract 0.10
            (#1570). Two routes and one capability, for the
            ``drive-connector`` reason: a consumer cannot usefully negotiate
            "may I classify" apart from "may I link", because a classified
            corpus with no resonances is as inert as an unclassified one, and
            a server advertising half the pipeline would be advertising a
            vault it cannot finish.

            **It is the tightening half of the surface, not a widening one.**
            :data:`creek_mcp.tier_ceiling._TIER_RANK` admits ``unclassified``
            at ceiling ``personal``, so a network-seeded corpus is *already*
            readable by a remote consumer; classification is what can move a
            fragment up to ``intimate`` and out of that reach. Neither
            response carries a fragment id, a path or a byte of prose, which
            is what makes running the whole vault under a remote ceiling
            disclose nothing.
    """

    CAPABILITIES = "capabilities"
    JOURNAL_UPSERT = "journal-upsert"
    REFLECTIONS = "reflections"
    WHEEL = "wheel"
    UPLOAD = "upload"
    DRIVE_CONNECTOR = "drive-connector"
    PIPELINE = "pipeline"


_FOUNDING_MINOR: Final[str] = "0.2"
"""The minor ``/v1`` was first published at; the four founding capabilities."""

_UPLOAD_MINOR: Final[str] = "0.8"
"""The minor ``upload`` was published at (#1524).

A literal, deliberately, and **not** :data:`CONTRACT_MINOR`. Written as the
derived constant it would silently follow the next bump forward, at which point
every client pinned to 0.8 — the very clients this table exists to serve — would
stop being told about a capability they negotiated correctly for.
"""

_DRIVE_CONNECTOR_MINOR: Final[str] = "0.9"
"""The minor ``drive-connector`` was published at (#1527).

A literal for the same reason :data:`_UPLOAD_MINOR` is one: written as
:data:`CONTRACT_MINOR` it would follow the next bump forward and stop
advertising the capability to the very clients pinned to the minor that
introduced it.
"""

_PIPELINE_MINOR: Final[str] = "0.10"
"""The minor ``pipeline`` was published at (#1570).

A literal for the same reason :data:`_UPLOAD_MINOR` is one. It is also the
first double-digit minor this contract has had, which is why
:func:`minor_at_least` compares componentwise as integers: a string compare
would sort ``"0.10"`` below ``"0.8"`` and hide the capability from precisely
the clients new enough to negotiate it.
"""

RELATED_FIELDS_SINCE_MINOR: Final[str] = "0.9"
"""First contract minor whose ``ReflectionResponse`` carries the #873 fields.

The same two-sided rule :data:`CAPABILITY_SINCE_MINOR` enforces for routes,
applied to *fields*: a client is served only what the minor it declared can
describe.

It is load-bearing rather than decorative because every published response
schema sets ``additionalProperties: false``. A ``0.8`` consumer that vendored
the ``0.8`` ``ReflectionResponse`` and validates closed does **not** "ignore" an
unknown key — it rejects the entire response. Emitting ``related_praxis`` to a
caller that declared ``0.8`` would therefore break exactly the consumer #873
exists to unblock, and only on the reflections that found a compiled neighbour,
which is the worst possible distribution of a failure.
"""

CAPABILITY_SINCE_MINOR: Final[dict[Capability, str]] = {
    Capability.CAPABILITIES: _FOUNDING_MINOR,
    Capability.JOURNAL_UPSERT: _FOUNDING_MINOR,
    Capability.REFLECTIONS: _FOUNDING_MINOR,
    Capability.WHEEL: _FOUNDING_MINOR,
    Capability.UPLOAD: _UPLOAD_MINOR,
    Capability.DRIVE_CONNECTOR: _DRIVE_CONNECTOR_MINOR,
    Capability.PIPELINE: _PIPELINE_MINOR,
}
"""The contract minor each capability was first published at.

Total over :class:`Capability`, so no lookup can ``KeyError`` mid-request, and
pinned as total by a test — a capability added without an entry here would
otherwise be advertised to everyone including clients whose vendored contract
predates it.

**One table, two enforcement points, and that is the point.**
:mod:`creek_mcp.httpapi.capabilities` filters the advertised list with it, and
:func:`creek_mcp.httpapi.app._endpoint_for` refuses a route with it. Two
separate tables would drift into the worst of both readings: a capability
advertised but refused (a server calling itself a liar) or one refused but
reachable (an endpoint a client integrates against without ever having
negotiated it, which is the ``501``-stub hazard
:mod:`creek_mcp.httpapi.handlers` documents, one layer up).

Every value must be a served minor; nothing is ever *removed* from this table,
because :data:`SUPPORTED_CONTRACT_MINORS` only ever widens.
"""


def minor_at_least(declared: str, required: str) -> bool:
    """Return whether *declared* is at or above the *required* contract minor.

    Compared componentwise as integers, never as strings. Lexicographic order
    puts ``"0.10"`` below ``"0.8"``, so a string compare here would start
    hiding capabilities from the newest clients on the day the minor reaches
    double digits — a failure that cannot be caught by any test written before
    that day unless the comparison is right from the start.

    Args:
        declared: The caller's ``major.minor``, already checked for membership
            in :data:`SUPPORTED_CONTRACT_MINORS` by the caller of this
            function. Anything unparseable is treated as *below* every
            requirement, which fails closed.
        required: The minor being demanded, from :data:`CAPABILITY_SINCE_MINOR`.

    Returns:
        ``True`` when *declared* is at least *required*.
    """
    try:
        parsed = tuple(int(part) for part in declared.split("."))
    except ValueError:
        return False
    return parsed >= tuple(int(part) for part in required.split("."))


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
    """What an idempotent write actually did — journal upsert or upload.

    ``UNCHANGED`` is a success, not an error: re-sending an unmodified entry is
    the steady state of a continuous sync, and a client must be able to tell it
    apart from a write without diffing content itself.

    Shared by :class:`JournalUpsertResponse` and :class:`UploadResponse` rather
    than duplicated under a second name. The three words are the same three
    :func:`creek_mcp.tools.upload._action_of` collapses an ingest tally into,
    and the ledger that decides them is one ledger. The name kept its
    ``Journal`` prefix through contract 0.8 because it is a **published schema
    component name**: Adepthood has vendored ``$defs/JournalAction`` since 0.2,
    and renaming it would break every consumer's schema validation to buy
    nothing a docstring cannot say.

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


class PraxisKind(StrEnum):
    """The five praxis types, mirroring the vault's own vocabulary.

    Kept in lock-step with :class:`creek.models.PraxisType`, which is what a
    ``04-Praxis/`` page's ``praxis_type`` is actually constrained to;
    :mod:`creek_mcp.compiled_pages` withholds any page naming something outside
    it, so a wire enum that drifted would refuse a legitimate page on the way
    out — or, worse, admit a hand-edited value the vault never sanctioned.
    ``tests/test_adepthood_contract_models.py`` pins the two lists equal.

    Attributes:
        COMMITMENT: A promise the writer has made.
        FRAMEWORK: A way of seeing they reuse.
        HABIT: Something done repeatedly, near-automatically.
        INSIGHT: A distilled realisation.
        PRACTICE: Something done deliberately and returned to.
    """

    COMMITMENT = "commitment"
    FRAMEWORK = "framework"
    HABIT = "habit"
    INSIGHT = "insight"
    PRACTICE = "practice"


class PraxisLifecycle(StrEnum):
    """The four praxis lifecycle statuses, mirroring :class:`creek.models.PraxisStatus`.

    Attributes:
        ACTIVE: Currently being practised.
        INTEGRATED: Absorbed; no longer needs deliberate attention.
        PROPOSED: Named but not yet taken up.
        RELEASED: Deliberately let go.
    """

    ACTIVE = "active"
    INTEGRATED = "integrated"
    PROPOSED = "proposed"
    RELEASED = "released"


class ErrorCode(StrEnum):
    """The ten wire error codes: nine from contract 0.2, one added at 0.8.

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
        UNSUPPORTED_SOURCE: The uploaded file's **extension** names a format
            Creek must not ingest as one document — a conversation export, an
            archive, a legacy binary Office file (#1526). A statement about
            the caller's own filename and nothing else, so it discloses
            nothing about the vault. Added at contract 0.8 (#1524), and
            reachable only on ``POST /v1/uploads``.
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
    UNSUPPORTED_SOURCE = "unsupported_source"
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
    ErrorCode.UNSUPPORTED_SOURCE: 415,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TEMPORARILY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}
"""HTTP status for each wire error code.

Total over :class:`ErrorCode`, so no dispatch path can ``KeyError`` while
building a response. As prose: 401 Unauthorized, 422 Unprocessable Content, 409
Conflict, 403 Forbidden, 404 Not Found, 501 Not Implemented, 415 Unsupported
Media Type, 503 Service Unavailable (twice — the two 503s differ only in retry
disposition, which is the distinction that actually matters to a client) and
500 Internal Server Error.

``415`` is the one status contract 0.8 added to the published set, and it is a
status of its own rather than a second ``422`` on purpose: the OpenAPI document
carries one response object per status, so a code sharing ``422`` with
``INVALID_REQUEST`` could not be documented at all — and, more to the point, a
client's generic "fix your JSON and retry" handling is exactly the wrong
handling for "this file format is never going to work".

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
    # Phrased about the FORMAT, never about the file, and it carries the
    # remedy rather than only the refusal — a refusal that does not say what
    # to do instead is one the caller retries verbatim. It is one constant
    # covering all three refused families (#1526's conversation exports,
    # archives and legacy binary Office documents) precisely so that it can
    # stay a constant: a per-extension message would have to be selected at
    # render time, and the moment `error_response` can be handed a message it
    # is a place caller — or vault — material eventually gets interpolated.
    ErrorCode.UNSUPPORTED_SOURCE: (
        "this file format cannot be ingested as one document: unpack a "
        "conversation export or archive and run `creek ingest --type "
        "<chatgpt|claude|discord|substack> --input <dir>`, or re-save a "
        "legacy Office document as .docx, .xlsx or .pptx"
    ),
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
    ErrorCode.UNSUPPORTED_SOURCE: RetryDisposition.TERMINAL,
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

_MAX_RELATED_PRAXIS: Final[int] = 3
"""Published ceiling on :attr:`ReflectionResponse.related_praxis` (#873)."""

_MAX_RELATED_EDDIES: Final[int] = 2
"""Published ceiling on :attr:`ReflectionResponse.related_eddies` (#873).

Restated here rather than imported from
:mod:`creek_mcp.compiled_pages`, because this module is deliberately
declarative — it opens no file and imports no vault reader (#1079) — and that
producer module reaches the filesystem. The two are pinned equal by
``test_the_published_related_bounds_match_the_producer`` in
``tests/test_adepthood_contract_models.py``, so the restatement cannot drift
into a schema that promises a bound the producer does not keep.
"""

MAX_EXTERNAL_ID_CHARS: Final[int] = 512
"""Inclusive upper bound on a consumer-supplied ``external_id``.

Generous enough for any namespaced consumer id — the published example is 38
characters — and small enough that the id cannot become a payload.
:func:`creek_mcp.staged_names.safe_stem` accepts literally any string and
truncates only the *readable slug* to 80 characters before appending a digest
of the whole raw id, so without a bound here an unbounded id would be accepted,
stored and echoed back at full length.

Declared in this framework-free module, and imported by
:mod:`creek_mcp.httpapi.journal` rather than restated there, because both write
surfaces have to mean the same thing by "an id that can serve as an idempotency
key": the journal route takes it as a URL path segment and the upload route as
a JSON field, and two bounds would let the same id be addressable through one
and not the other.
"""

MAX_FILENAME_CHARS: Final[int] = 255
"""Inclusive upper bound on the uploaded ``filename``.

The classical POSIX single-component limit. Only the extension is ever trusted
— :func:`creek_mcp.staged_names.safe_suffix` truncates that, and the staged
name is built from the ``external_id``, never from this string — so the bound
is not protecting a filesystem call. It is protecting the audit log and the
wire from a caller who discovers that "filename" is an unbounded free-text
field nobody reads.
"""

_MAX_REDIRECT_URI_CHARS: Final[int] = 2048
"""Inclusive upper bound on a Drive-authorization ``redirect_uri`` (#1568).

The de-facto URL ceiling every browser and proxy already enforces. Like
:data:`MAX_FILENAME_CHARS` it is not protecting a downstream call — the URI is
parsed, never dereferenced by this server — it is protecting the audit log and
the store from an unbounded caller-supplied string.
"""

_MAX_AUTHORIZATION_CODE_CHARS: Final[int] = 2048
"""Inclusive upper bound on a relayed OAuth authorization code (#1568).

Google's codes are a few hundred characters. The bound exists so a caller
cannot use the exchange body as an unbounded write channel into a request this
server has to buffer and log around.
"""

_REDIRECT_URI_SCHEME: Final[str] = "https"
"""The only redirect scheme a Drive authorization may be issued for.

Not a preference. The authorization code travels back to the caller over this
URI, and that code plus the client secret this host holds is a Drive
credential; over plain ``http`` it would cross the network in the clear.
"""


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
        warnings: Content-free operator advisories this write produced, and
            absent entirely when there were none (#1372). Optional rather
            than an always-present empty list on purpose: ``_WireModel`` is
            ``extra="forbid"`` and this server still serves contract minors
            0.2, 0.3 and 0.4, so an unconditional key would put an
            unnegotiated field in every ``200`` those clients receive. Paired
            with ``exclude_none`` at the dump, the quiet write stays
            byte-identical to what it was before this field existed. Every
            entry is proven free of vault content at the producer — see
            :attr:`creek.ingest.pipeline.IngestRunResult.ceiling_safe_warnings`
            — so nothing here varies with the caller's ceiling.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    external_id: str = Field(description="Consumer-side entry identity.")
    fragment_id: str = Field(description="Vault-side fragment identity.")
    action: JournalAction = Field(description="Created, updated, or unchanged.")
    tier: WireTierCeiling = Field(description="Tier the entry was created at.")
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Content-free operator advisories this write produced; absent when none."
        ),
    )


class UploadRequest(_WireModel):
    """One document's bytes, handed to the vault under a stable id (#1524).

    The wire half of :func:`creek_mcp.tools.upload.upload_tool`, and
    deliberately its *subset*: it carries no ``source_type``. The tool refuses
    that override because the ``chatgpt`` / ``claude`` / ``discord`` /
    ``substack`` ingestors are directory-only and would discover nothing from a
    single staged file, reporting a silent no-op as a success. Dispatch is by
    the filename's extension, through
    :func:`creek.ingest.route_to_ingestor`, and nothing else. Uploading a whole
    export archive is tracked separately, in #1525.

    **JSON and base64, never multipart.** The consumer's HTTP client is JSON
    and its CI bans ``python-multipart``, so a multipart route would be one
    neither end could use. The cost is the ~33% base64 expansion, which is what
    :data:`creek_mcp.api.routes.UPLOAD_MAX_BODY_BYTES` is sized around.

    Attributes:
        filename: The caller's original filename. Only its **extension** is
            trusted, and only to choose an ingestor; the staged file's name is
            derived from *external_id*, so nothing here reaches a path.
        content_base64: The document's bytes, base64-encoded. Refused when
            blank; refused by the tool when it is not valid base64, and — via
            the encoded-length guard — before an oversize payload is ever
            turned into memory.
        external_id: The consumer-side stable id, and the idempotency key. The
            same id updates in place; a new id creates a new fragment.
        timestamp: Optional ISO-8601 upload time, accepted for symmetry with
            :class:`JournalUpsertRequest` and echoed nowhere on disk — a binary
            document has no frontmatter to put it in, so the fragment's
            timestamp comes from the ingestor.
        tier: The tier to file the document at. **Required, and typed
            :class:`WireTierCeiling`,** so ``intimate`` is not expressible and
            omission is not defaultable. Both halves matter: #1494 removed this
            field's ``open`` default from ``creek.upload`` precisely because
            only the caller knows what the document holds, and a default filed
            intimate-derived bytes in the clear.
    """

    filename: str = Field(
        max_length=MAX_FILENAME_CHARS,
        description="Original filename; only its extension is trusted.",
    )
    content_base64: str = Field(description="The document's bytes, base64.")
    external_id: str = Field(
        max_length=MAX_EXTERNAL_ID_CHARS,
        description="Consumer-side stable id; the idempotency key.",
    )
    timestamp: str | None = Field(
        default=None,
        description="Optional client-supplied upload time.",
    )
    tier: WireTierCeiling = Field(description="Tier to file the document at.")

    @field_validator("filename", "content_base64", "external_id")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Refuse a whitespace-only value on any of the three required strings.

        One validator over three fields rather than three validators: they
        share a rule, and a rule stated three times is one that gets relaxed
        twice.

        Args:
            value: The submitted field value.

        Returns:
            *value* unchanged when it carries a non-space character.

        Raises:
            ValueError: When *value* is empty or whitespace-only.
        """
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class UploadResponse(_WireModel):
    """The result of an idempotent document upload.

    **There is deliberately no ``tier`` field**, and its absence is the whole
    of this model's privacy argument. ``creek classify`` is escalate-only and
    :meth:`creek.vault.writer.VaultWriter.update_fragment` re-derives a
    fragment's tier the same way, so an uploaded ``.md`` whose own frontmatter
    declares ``intimate`` is filed at ``intimate`` however modest a tier the
    caller declared. A ``tier`` field here would have exactly two possible
    values and both are wrong: echo the declared tier and the response asserts
    something about the file on disk that is false — the #1491 defect, where a
    response claimed a tier the bytes did not carry — or report the resolved
    tier and the response becomes a tier oracle, spelled in a vocabulary
    (:class:`WireTierCeiling`) that cannot even express the answer. So the
    response says only what it can say truthfully: which ceiling the call ran
    at, and which fragments now exist.

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call was served under — the caller's own
            declared value, echoed for correlation and carrying no bit of the
            vault's state.
        external_id: The consumer-side identity of the document, unchanged.
        fragment_id: The vault-side identity now backing it. For the
            one-fragment-per-file majority this is simply *the* fragment; for a
            document that splits (a multi-sheet workbook) it is the first of
            :attr:`affected_fragment_ids` in ledger-key order, which is
            deterministic across runs.
        affected_fragment_ids: **Every** fragment this document produced, in
            that same order. Published rather than left implicit because a
            right-to-be-forgotten request answered from ``fragment_id`` alone
            would miss the other sheets of a workbook (#1305).
        action: Whether the upload created, updated, or changed nothing.
        source_type: The ingestor the extension dispatched to (``markdown``,
            ``document``, ``spreadsheet``, …). Derived from the caller's own
            filename, so it discloses nothing, and worth publishing because
            "which reader interpreted my bytes" is otherwise unknowable.
        warnings: Content-free operator advisories this ingest produced, absent
            entirely when there were none — the same optional-and-omitted shape
            :class:`JournalUpsertResponse` uses, and for the same reason. This
            is the surface that most needs it: the collapsed-unit advisory
            reports actual data loss on the multi-sheet workbook path. Every
            entry is proven ceiling-safe at the producer (see
            :attr:`creek.ingest.pipeline.IngestRunResult.ceiling_safe_warnings`).
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    external_id: str = Field(description="Consumer-side document identity.")
    fragment_id: str = Field(description="Vault-side fragment identity.")
    affected_fragment_ids: list[str] = Field(
        description="Every fragment this document produced, in ledger order.",
    )
    action: JournalAction = Field(description="Created, updated, or unchanged.")
    source_type: str = Field(description="Ingestor the extension dispatched to.")
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Content-free operator advisories this upload produced; absent when none."
        ),
    )


class DriveConnectionState(StrEnum):
    """The four states the read-only Drive connector can be in (#1527).

    A closed enum rather than a boolean, because the three unusable states are
    answered by three different operator actions and a client that could only
    see ``connected: false`` would have to guess which to prompt for.

    **Every member is a fact about this server's own configuration**, never
    about the user's Drive: no file, folder, id or count is expressible here.

    Attributes:
        CONNECTED: A cached credential exists and is usable, so a sync will
            run without a human present.
        NOT_CONNECTED: No cached credential. The operator authorises with
            ``creek gdrive --download`` on the host; there is no ``/v1`` route
            that can do it for them.
        EXPIRED: A credential exists but has lapsed and carries no refresh
            token, so it cannot be renewed non-interactively. The same
            operator action clears it.
        UNSUPPORTED: The optional Google client libraries are not installed on
            this server, so no credential could be used even if one existed.
            Distinguished from :attr:`NOT_CONNECTED` because the remedy is an
            install rather than an authorisation, and a client that conflated
            them would send the operator round a loop that cannot terminate.
    """

    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"


class DriveConnectorStatusResponse(_WireModel):
    """What ``GET /v1/connectors/drive`` reports about the connector (#1527).

    **The one thing this model must never carry is the credential**, and its
    closed field list is what enforces that: ``_WireModel`` forbids extras, so
    a token cannot be bolted on without editing this class and the schema it
    publishes. What a caller gets is the *state* of the connection and the
    *scope set* it was granted under — never the token, the refresh token, the
    client secret, or the path any of them live at.

    Publishing connection state deliberately is not in tension with the
    no-oracle rule the refusals follow. This route **is** the negotiated
    disclosure — a client that cannot tell "connected" from "not connected"
    cannot render a connect button — and it is reachable only by an
    authenticated consumer. The rule the refusals keep is the complementary
    one: because this route answers the question honestly, no *other* route
    has any reason to leak the answer sideways.

    Attributes:
        status: Always ``"ok"``. An unreadable vault or config is an
            :class:`ErrorEnvelope`, not a state.
        tier_ceiling: The ceiling the call ran at — the caller's own declared
            value, echoed for correlation.
        connection: The connector's state.
        scopes: The OAuth scope set the cached credential was requested
            under, verbatim. Published because a client showing a user "Creek
            can read your Drive" should be able to *check* that claim rather
            than repeat it;
            :meth:`creek.config.GoogleDriveConfig.validate_readonly_scopes`
            refuses to construct a config carrying anything else, so a widened
            scope cannot reach this field without failing validation first.
        can_sync: Whether ``POST /v1/connectors/drive/syncs`` would run rather
            than refuse. Derived from :attr:`connection` and published
            separately so a client need not encode this server's state
            algebra to know whether its button is live.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    connection: DriveConnectionState = Field(description="Connector state.")
    scopes: list[str] = Field(
        description="Read-only OAuth scopes the credential was granted under.",
    )
    can_sync: bool = Field(description="True when a sync would run, not refuse.")


class DriveSyncResponse(_WireModel):
    """The tally of one incremental Drive sync (#1527).

    **Counts, and nothing but counts.** Not a file name, not a folder name,
    not a Drive id, not a fragment id, not a tier. Every one of those is the
    user's own content or a handle onto it, and this response is the only
    thing a remote caller learns from a run that may have touched material far
    above its ceiling — a sync ingests whatever the user's own Drive holds,
    because the tier of that content is derived from the content, never
    declared by the caller.

    That is also why there is no ``affected_fragment_ids`` here, despite
    :class:`UploadResponse` publishing one for the #1305 right-to-be-forgotten
    argument. An upload's fragments are the caller's own bytes, handed over a
    moment earlier; a sync's are the vault owner's, and a list of them handed
    to an ``open``-ceiling consumer is a corpus enumeration primitive dressed
    as a receipt. The vault-side record of what a sync wrote is the ingest
    ledger, which RTBF already reads.

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call ran at.
        files_fetched: Files Drive reported as newer than the local copy and
            this run downloaded.
        files_unchanged: Files the incremental skip passed over. This is the
            number that makes a second sync of an untouched Drive visibly a
            no-op rather than an invisible one.
        files_failed: Files whose download raised. The run continues past
            each, so a quota blip mid-sync does not abandon the rest — and the
            count is published because a caller told only about successes
            would believe an incomplete sync was complete. The *reasons* stay
            in the server log: they interpolate Drive file names.
        fragments_failed: Files that downloaded cleanly and then failed to
            become fragments — a per-unit assemble or write error. Published
            separately from ``files_failed`` because the two are different
            shortfalls: one never landed, the other landed and was lost.
            Without it a sync whose content half-failed reports ``ok`` with
            silently fewer fragments and no signal anywhere.
        files_unsupported: Files that downloaded cleanly and were **not**
            ingested, because their extension names a format Creek must not
            read as one document (#1526) — a conversation export, an archive,
            a legacy binary Office file. Published rather than folded into
            :attr:`files_failed`, which would be untrue, and rather than left
            out, which would be worse: a fetched-but-undigested file is
            precisely the silent no-op this whole surface exists to prevent.
            The bytes are in the staging mirror and ``creek ingest`` can read
            them; nothing is lost, but nothing happened either.
        fragments_created: Fragments this run's ingest newly wrote.
        fragments_updated: Fragments it rewrote in place.
        fragments_unchanged: Fragments the ledger recognised as identical.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    files_fetched: int = Field(ge=0, description="Files downloaded this run.")
    files_unchanged: int = Field(ge=0, description="Files the skip passed over.")
    files_failed: int = Field(ge=0, description="Files whose download raised.")
    files_unsupported: int = Field(
        ge=0,
        description="Files fetched but of a format Creek must not ingest.",
    )
    fragments_failed: int = Field(
        ge=0,
        description=(
            "Files that downloaded cleanly and then failed to become "
            "fragments. Distinct from files_failed, which is a download that "
            "never landed."
        ),
    )
    fragments_created: int = Field(ge=0, description="Fragments newly written.")
    fragments_updated: int = Field(ge=0, description="Fragments rewritten.")
    fragments_unchanged: int = Field(ge=0, description="Fragments left as they were.")


class DriveDisconnectResponse(_WireModel):
    """The outcome of ``DELETE /v1/connectors/drive`` (#1527).

    Idempotent, and constant-shaped: disconnecting an already-disconnected
    connector is a success, and reports the same
    :attr:`~DriveConnectionState.NOT_CONNECTED` as disconnecting a live one.
    That is why :attr:`connection` is published rather than a
    ``token_erased`` boolean — the latter would answer "was there a
    credential a moment ago", which is a question about the past that this
    verb has no reason to answer.

    Attributes:
        status: Always ``"ok"``.
        tier_ceiling: The ceiling the call ran at.
        connection: The state afterwards. Always
            :attr:`~DriveConnectionState.NOT_CONNECTED` on a ``200``: the
            local credential is erased before the response is built, and a
            failure to erase it is an :class:`ErrorEnvelope` rather than a
            success reporting that nothing happened.
        remote_revoked: Whether Google's revocation endpoint confirmed the
            credential is dead on their side too. ``False`` does **not** mean
            the local erase failed — it means the operator should finish the
            job at Google's account page, which is the one piece of advice a
            client can act on and cannot derive.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    connection: DriveConnectionState = Field(description="State after the call.")
    remote_revoked: bool = Field(description="Google confirmed the revocation.")


class DriveAuthorizationRequest(_WireModel):
    """Begin a Drive authorization for a redirect URI the *caller* owns (#1568).

    The one field is the whole of ADR-0012's option C. Creek mints the
    authorization URL and holds the client secret; the caller owns the public
    address Google redirects the user's browser to, and relays the resulting
    code back over the bearer credential it already holds. That is why there is
    no callback route on this server, and therefore no anonymous path through
    :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware`.

    Attributes:
        redirect_uri: The caller's own redirect URI. Absolute, ``https``, and
            fragment-free — validated here rather than left to Google, because
            a plain-``http`` redirect would carry the authorization code in the
            clear and RFC 6749 §3.1.2 forbids the fragment outright. Never a
            URI on this server.
    """

    redirect_uri: str = Field(
        min_length=1,
        max_length=_MAX_REDIRECT_URI_CHARS,
        description="Caller-owned absolute https redirect URI, no fragment.",
    )

    @field_validator("redirect_uri")
    @classmethod
    def _validate_redirect_uri(cls, value: str) -> str:
        """Reject any redirect URI the grant cannot be issued for.

        Args:
            value: The submitted URI.

        Returns:
            The URI, unchanged.

        Raises:
            ValueError: When the URI is relative, not ``https``, or carries a
                fragment. The message names the requirement and never echoes
                the value, which is caller-supplied text — a validation error
                is rendered into a ``422`` a remote caller reads.
        """
        parsed = urlparse(value)
        if parsed.scheme != _REDIRECT_URI_SCHEME:
            msg = "redirect_uri must be an absolute https URI"
            raise ValueError(msg)
        if not parsed.netloc:
            msg = "redirect_uri must name a host"
            raise ValueError(msg)
        if parsed.fragment:
            msg = "redirect_uri must not carry a fragment"
            raise ValueError(msg)
        return value


class DriveAuthorizationResponse(_WireModel):
    """The URL to send a user to, and the state binding the code (#1568).

    **Carries no secret.** The client secret stays on this host; the
    authorization URL is a public value by construction — it is what a browser
    address bar shows — and ``state`` is a server-minted nonce that is useless
    without an authorization code.

    Attributes:
        status: Always ``"ok"``. A refusal is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call ran at, echoed for correlation.
        authorization_url: Where the caller sends its user. Built by
            ``google_auth_oauthlib`` from this deployment's client description
            and the config's read-only scope list, re-validated by
            :func:`creek_mcp.tools.drive_grant._readonly_scopes` on the way out
            so a remote connect button cannot widen the grant.
        state: The single-use, expiring value the eventual code must be
            presented under. It binds a relayed code to an authorization *this*
            server issued; it is not a CSRF token, because Creek never sees the
            browser.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    authorization_url: str = Field(description="Where to send the user's browser.")
    state: str = Field(description="Single-use value the code must be relayed under.")


class DriveAuthorizationExchangeRequest(_WireModel):
    """The authorization code, relayed back to Creek (#1568).

    **One field, and the absence of the second is deliberate.** The
    ``redirect_uri`` is *not* re-supplied here: it is read from the record the
    ``state`` was issued under. A caller able to vary it between the two legs
    could complete an authorization against an address the first leg never
    named, which is exactly what binding the code to a stored authorization
    exists to prevent.

    Attributes:
        code: Google's authorization code, as the caller's redirect handler
            received it. Single-use and short-lived at Google's end too; it is
            worthless without the client secret this server holds.
    """

    code: str = Field(
        min_length=1,
        max_length=_MAX_AUTHORIZATION_CODE_CHARS,
        description="The authorization code Google handed the caller.",
    )


class ClassificationMethod(StrEnum):
    """The classification methods ``POST /v1/classifications`` serves.

    One member, and the single member is the point. ``creek classify`` also
    accepts ``llm``, and it is **deliberately absent here** rather than
    accepted-and-refused:

    * **Time.** :data:`creek_mcp.httpapi.middleware.limits.DEFAULT_TIMEOUT_SECONDS`
      is thirty seconds, and the timeout middleware uses ``anyio.fail_after``,
      which cannot cancel a call already inside ``run_in_threadpool``. An LLM
      pass over a seeded corpus is minutes to hours: the caller would be handed
      a ``503`` while the work carried on with no handle on it.
    * **Egress.** With no ``llm`` member there is no value a producer or a
      consumer can put on the wire that reaches a provider at all, so "no byte
      of the vault leaves the host on this route" is a property of the type
      rather than of a runtime check somebody can forget to call.

    Reaching ``llm`` classification over the network needs an asynchronous job
    surface — ``202``, a job id, a status route — which this contract does not
    have. Until then it stays an operator step on the host.

    Attributes:
        RULES: Keyword classification. No model, no key, no consent, no egress.
    """

    RULES = "rules"


class LinkMethod(StrEnum):
    """The linker stages ``POST /v1/links`` serves.

    Three of :data:`creek.surface_modes.LINK_METHODS`. ``embeddings`` is the
    fourth and is excluded for the same shape of reason ``llm`` is excluded
    from :class:`ClassificationMethod`, with a different cause: it is the
    O(n²) pairwise-similarity stage, which has been observed being abandoned
    at 35k fragments and is unbounded in a way no cache repairs.

    **Excluding it does not make every served member cheap, and the schema
    should not be read as promising that.** ``eddies`` and ``threads`` both
    need vectors, so both fill the embeddings parquet on a cold cache — a
    local sentence-transformer pass over every uncached fragment, which is
    minutes of work on a large vault and can outrun the request deadline.
    They are served anyway because the parquet is a *cache*: the work a
    timed-out first call performs still lands, so a retry converges instead of
    starting over. Nothing about that reaches a network provider; the model is
    local (ADR-0004). Budget for the first call on a cold vault, and prefer
    ``temporal`` when you only need the corpus navigable.

    Attributes:
        TEMPORAL: Same-window adjacency links. The one genuinely cheap stage —
            no vectors, no model — and the one that makes a freshly-seeded
            corpus navigable at all.
        EDDIES: Topic clusters, materialised under ``03-Eddies/``. Embeds on a
            cold cache.
        THREADS: Narrative currents, materialised under ``02-Threads/``. Embeds
            on a cold cache, and reads the APTITUDE labels a classification
            pass writes, so run classification first.
    """

    TEMPORAL = "temporal"
    EDDIES = "eddies"
    THREADS = "threads"


class ClassificationRequest(_WireModel):
    """A whole-vault classification pass (#1570).

    There is no fragment selector, and there will not be one. The pass is
    idempotent and resumable — :func:`creek.classify.classify_engine.run_classify`
    short-circuits on the ``classification_method`` stamp it already wrote — so
    "classify everything" converges on retry, whereas a caller-supplied id list
    would be an enumeration primitive on a corpus this consumer cannot read.

    Attributes:
        method: Which classifier to run. Defaults to the only served member so
            the empty body — the request a consumer writes first — is the
            correct one.
        retier: Whether to also re-derive the privacy tier of fragments that
            already carry a concrete one. Off by default, because re-deriving a
            tier the operator settled by hand is the operator's decision. It is
            the *only* way this surface can correct a consumer that declared
            ``personal`` on intimate bytes at upload time, and it cannot loosen
            anything: the engine merges the new verdict with
            ``escalate``, so a recorded tier is replaced only by a stricter one
            (#1106).
    """

    method: ClassificationMethod = Field(
        default=ClassificationMethod.RULES,
        description="Classifier to run; rules is the only served method.",
    )
    retier: bool = Field(
        default=False,
        description="Also re-derive an already-recorded tier, escalate-only.",
    )


class ClassificationResponse(_WireModel):
    """What one classification pass did, in counts alone (#1570).

    **Nothing here names a fragment.** No id, no path, no title, no excerpt,
    not even an error string — the engine's per-fragment errors interpolate
    file paths, so they are collapsed into :attr:`complete`. That is what makes
    it safe for this route to run over the *whole* vault while serving a caller
    capped at ``personal``: the pass reads material above the ceiling on the
    host, and reports nothing whatsoever about it.

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call ran at.
        method: The classifier that ran, echoed so a client that sent an empty
            body learns which default it got.
        total: Creek fragments visited.
        classified: Fragments whose frontmatter this run rewrote.
        preserved_manual: Fragments left alone because an operator had stamped
            them by hand.
        preserved_llm: Fragments left alone by the resume short-circuit.
        privacy_tiers_assigned: Fragments whose privacy tier this run derived.
            **Zero is the expected answer for a network-seeded corpus**, and it
            is not a failure: ``POST /v1/uploads`` requires an explicit tier
            (#1497), so an uploaded fragment already carries the caller's
            declared one and the tier pass does not own it. Only ``retier``
            re-opens that case.
        retiered: Subset of the above where an already-recorded tier was
            replaced by a stricter one. Non-zero only under ``retier``.
        praxis_marked: Fragments raised to a stronger praxis potential.
        tags_extracted: Fragments that gained a hashtag ``tags`` entry.
        complete: Whether every visited fragment was processed without error.
            ``False`` means some were skipped, and — because the pass is
            resumable — that the honest next step is to call again rather than
            to treat the corpus as done. The *reasons* stay in the server log:
            each one names the file it failed on.
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    method: ClassificationMethod = Field(description="The classifier that ran.")
    total: int = Field(ge=0, description="Creek fragments visited.")
    classified: int = Field(ge=0, description="Fragments rewritten this run.")
    preserved_manual: int = Field(ge=0, description="Left alone: operator stamp.")
    preserved_llm: int = Field(ge=0, description="Left alone: resume stamp.")
    privacy_tiers_assigned: int = Field(ge=0, description="Tiers derived this run.")
    retiered: int = Field(ge=0, description="Recorded tiers replaced by stricter.")
    praxis_marked: int = Field(ge=0, description="Fragments raised to praxis.")
    tags_extracted: int = Field(ge=0, description="Fragments that gained tags.")
    complete: bool = Field(description="Every visited fragment processed cleanly.")


class LinkRequest(_WireModel):
    """One linker stage over the whole vault (#1570).

    ``method`` is required and has no default, unlike
    :class:`ClassificationRequest`'s. The three stages are not
    interchangeable — one writes adjacency into fragment frontmatter, one
    materialises ``03-Eddies/``, one materialises ``02-Threads/`` — so a
    default would silently run a pass the caller did not choose and report it
    as the one they asked for.

    Attributes:
        method: The linker stage to run.
    """

    method: LinkMethod = Field(description="Linker stage to run.")


class LinkResponse(_WireModel):
    """What one linker stage did, in counts alone (#1570).

    Counts only, for the same reason :class:`ClassificationResponse` carries
    none: the stage runs over every fragment in the vault, including material
    far above the caller's ceiling, and a thread title or an eddy name is the
    corpus talking.

    There is no ``complete`` twin of the classification field. The linker has
    no per-fragment error accumulator to collapse — a stage either finishes or
    raises, and a raise is an :class:`ErrorEnvelope` — so a boolean here would
    be a constant ``True`` wearing the shape of a fact.
    :attr:`oversized_discarded` is the honest partial-loss signal instead.

    Attributes:
        status: Always ``"ok"``. Failure is an :class:`ErrorEnvelope`.
        tier_ceiling: The ceiling the call ran at.
        method: The stage that ran, echoed for correlation.
        fragment_count: Fragments loaded from the vault.
        link_count: The stage's own count — temporal links, eddies detected,
            or threads detected.
        largest_cluster_fragments: Members of the biggest cluster emitted; the
            proof that no single cluster swallowed the corpus (#880).
        clusters_split: Clusters re-clustered at a tighter parameter for
            exceeding the configured ceiling.
        oversized_discarded: Fragments dropped to noise because their cluster
            stayed oversized after the split budget was spent. **This is data
            loss** — those fragments carry no link at all — and it is published
            rather than folded away because a caller who cannot see it reads a
            lossy pass as a clean one (#1372).
    """

    status: Literal["ok"] = Field(description="Always ok; failure is an error.")
    tier_ceiling: WireTierCeiling = Field(description="Ceiling the call ran at.")
    method: LinkMethod = Field(description="The linker stage that ran.")
    fragment_count: int = Field(ge=0, description="Fragments loaded from the vault.")
    link_count: int = Field(ge=0, description="The stage's own count.")
    largest_cluster_fragments: int = Field(ge=0, description="Biggest cluster's size.")
    clusters_split: int = Field(ge=0, description="Clusters re-clustered tighter.")
    oversized_discarded: int = Field(ge=0, description="Fragments dropped to noise.")


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


class RelatedPraxis(_WireModel):
    """One praxis page the reflected entry contributed to (contract 0.9, #873).

    **Compiled, not raw.** A praxis page is distilled *from* fragments, so it
    is published only when every id in its ``derived_from`` resolves to a
    fragment within the caller's ceiling — and withheld outright when its
    provenance cannot be enumerated in full. That rule lives in
    :mod:`creek_mcp.compiled_pages`; nothing on this model re-derives it, and
    no field here carries a tier, because a page that reached the wire is one
    whose whole provenance was already admitted.

    Attributes:
        title: The praxis page's title.
        praxis_type: Which of the five :class:`PraxisKind` values it is.
        status: Where it sits in the :class:`PraxisLifecycle`.
        excerpt: The page's own opening prose, whitespace-collapsed and capped
            at :data:`creek_mcp.compiled_pages.EXCERPT_CHARS`. Not a model
            summary and not grounding-checked — it is the page as written.
    """

    title: str = Field(description="The praxis page's title.")
    praxis_type: PraxisKind = Field(description="Which kind of praxis this is.")
    status: PraxisLifecycle = Field(description="Where it sits in its lifecycle.")
    excerpt: str = Field(description="The page's own opening prose, capped.")


class RelatedEddy(_WireModel):
    """One eddy the reflected entry belongs to (contract 0.9, #873).

    Carries the same compiled-page caveat as :class:`RelatedPraxis`, and one
    more that is specific to it: ``description`` and ``fragment_count`` are
    *synthesised from the eddy's members*, so publishing the page to a caller
    admitted to only some of them would hand over a summary of content they
    cannot read. :mod:`creek_mcp.compiled_pages` therefore admits an eddy only
    when the members it can enumerate account for ``fragment_count`` exactly
    **and** every one of them is within the ceiling.

    Attributes:
        title: The eddy page's title.
        description: The eddy's own one-line description; ``""`` when the page
            declares none.
        fragment_count: How many fragments the eddy clusters. A tally, so never
            negative.
        formed: The ``YYYY-MM-DD`` the eddy was first detected.
    """

    title: str = Field(description="The eddy page's title.")
    description: str = Field(description="The eddy's own description; may be empty.")
    fragment_count: int = Field(
        ge=_MIN_COUNT, description="How many fragments the eddy clusters."
    )
    formed: str = Field(description="ISO date the eddy was first detected.")


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
    # Contract 0.9 (#873). Optional and bounded. The route drops these two
    # keys when they are ``None`` -- and only these two, never ``essay``, whose
    # explicit ``null`` a 0.2 consumer has been reading since the contract
    # opened -- so a reflection with no admitted compiled neighbours is
    # byte-identical to what a 0.8 client already receives. Present only when
    # something qualified — which is the whole compatibility claim, and the
    # reason these are ``| None`` rather than defaulting to ``[]``: an empty
    # list is a *statement* that nothing qualified, and this contract does not
    # make that statement, because a vault that has never run ``creek link``
    # and one whose neighbours were all withheld above the ceiling must be
    # indistinguishable to the caller. Telling them apart would be a one-bit
    # oracle over the compiled layer.
    related_praxis: list[RelatedPraxis] | None = Field(
        default=None,
        max_length=_MAX_RELATED_PRAXIS,
        description="Praxis pages the entry contributed to; absent when none.",
    )
    related_eddies: list[RelatedEddy] | None = Field(
        default=None,
        max_length=_MAX_RELATED_EDDIES,
        description="Eddies the entry belongs to; absent when none.",
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

    The published example matrix is 5 capabilities x 7 states. Four of those
    cells — care escalation for ``capabilities``, ``journal-upsert``, ``wheel``
    and ``upload`` — are *structurally* unreachable: the acute-distress guard runs
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
    "ClassificationRequest": ClassificationRequest,
    "ClassificationResponse": ClassificationResponse,
    "DriveAuthorizationExchangeRequest": DriveAuthorizationExchangeRequest,
    "DriveAuthorizationRequest": DriveAuthorizationRequest,
    "DriveAuthorizationResponse": DriveAuthorizationResponse,
    "DriveConnectorStatusResponse": DriveConnectorStatusResponse,
    "DriveDisconnectResponse": DriveDisconnectResponse,
    "DriveSyncResponse": DriveSyncResponse,
    "ErrorEnvelope": ErrorEnvelope,
    "JournalUpsertRequest": JournalUpsertRequest,
    "JournalUpsertResponse": JournalUpsertResponse,
    "LinkRequest": LinkRequest,
    "LinkResponse": LinkResponse,
    "NotApplicableExample": NotApplicableExample,
    "ReflectionNote": ReflectionNote,
    "ReflectionRequest": ReflectionRequest,
    "ReflectionResponse": ReflectionResponse,
    "RelatedEddy": RelatedEddy,
    "RelatedPraxis": RelatedPraxis,
    "TierModel": TierModel,
    "UploadRequest": UploadRequest,
    "UploadResponse": UploadResponse,
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
