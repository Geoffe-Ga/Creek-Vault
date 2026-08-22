"""The published ``/v1`` route table, declared as data (#1074).

This module is the single declaration of what ``/v1`` exposes. Three consumers
read it — the OpenAPI generator (:mod:`creek_mcp.api.openapi`), the Starlette
adapter (:mod:`creek_mcp.httpapi.app`) and the capability handshake
(:mod:`creek_mcp.httpapi.capabilities`) — so none of them can advertise an
endpoint another does not serve.

**Why data rather than decorators.** A ``@app.route``-defined table can only be
discovered by importing the framework, and a document generated from a
framework's introspection is a function of that framework's version rather than
of our own models (ADR, "HTTP framework", reason 2). Keeping the table here, in
the framework-free half of the surface beside :mod:`creek_mcp.api.models`, is
what lets the published OpenAPI document outlive whatever serves it.

**Exposed versus implemented.** :data:`ROUTES` says which endpoints exist;
:data:`IMPLEMENTED_CAPABILITIES` says which of them actually answer. #1075—#1077
moved the last three capabilities from the first set into the second, and #1524
(``upload``) and #1527 (``drive-connector``) both arrived already built, so the
two still coincide. The distinction is
not obsolete: while a capability was unbuilt it was wired to an honest ``501``
rather than to a fabricated ``200``, because a stub that looks like success is
one a consumer integrates against and only discovers in production. A seventh
capability published before its handler exists gets exactly that treatment,
from the same constant, with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek_mcp.api.models import (
    CapabilitiesResponse,
    Capability,
    DriveConnectorStatusResponse,
    DriveDisconnectResponse,
    DriveSyncResponse,
    JournalUpsertRequest,
    JournalUpsertResponse,
    ReflectionRequest,
    ReflectionResponse,
    UploadRequest,
    UploadResponse,
    WheelResponse,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

CONTRACT_VERSION_HEADER: Final[str] = "X-Creek-Contract-Version"
"""Header carrying the client's ``major.minor`` contract version.

Declared here rather than in the adapter because it is published contract: the
OpenAPI generator documents it as a required parameter and the adapter refuses
without it, and those two must name the same header.
"""

CEILING_HEADER: Final[str] = "X-Creek-Tier-Ceiling"
"""Header carrying the caller's declared tier ceiling.

Also one of the two standing ``Vary`` tokens, so an intermediary cache can never
serve one caller's ceiling-filtered response to another. The other is
:data:`AUTHORIZATION_HEADER`.
"""

AUTHORIZATION_HEADER: Final[str] = "Authorization"
"""The header a consumer presents its bearer token in.

Declared in this framework-free module, rather than beside the middleware that
reads it, because two modules now have to agree on the string and one of them
cannot import the other. :mod:`creek_mcp.httpapi.auth` *reads* this header to
authenticate; :mod:`creek_mcp.httpapi.errors` *names* it in every response's
``Vary`` so a cache keys on the credential (#1129). Those two must be the same
header or the ``Vary`` declares a dependency on something no request carries —
and ``auth`` already imports ``errors``, so the constant cannot live there
without a cycle.

Unlike its two neighbours it is **not** published in the OpenAPI document: that
document declares no ``securitySchemes`` and no bearer requirement at all,
despite every route sitting behind the gate. That gap is tracked separately; it
is a reason this constant will gain a third reader, not a reason to file it
somewhere else.
"""

OP_CAPABILITIES: Final[str] = "getCapabilities"
"""``operation_id`` of ``GET /v1/capabilities``."""

OP_JOURNAL_UPSERT: Final[str] = "upsertJournalEntry"
"""``operation_id`` of ``PUT /v1/journal-entries/{external_id}``."""

OP_REFLECTIONS: Final[str] = "createReflection"
"""``operation_id`` of ``POST /v1/reflections``."""

OP_WHEEL: Final[str] = "getWheel"
"""``operation_id`` of ``GET /v1/wheel``."""

OP_UPLOAD: Final[str] = "uploadDocument"
"""``operation_id`` of ``POST /v1/uploads``."""

OP_DRIVE_STATUS: Final[str] = "getDriveConnector"
"""``operation_id`` of ``GET /v1/connectors/drive``."""

OP_DRIVE_SYNC: Final[str] = "syncDriveConnector"
"""``operation_id`` of ``POST /v1/connectors/drive/syncs``."""

OP_DRIVE_DISCONNECT: Final[str] = "disconnectDriveConnector"
"""``operation_id`` of ``DELETE /v1/connectors/drive``."""

OP_HEALTH: Final[str] = "getHealth"
"""``operation_id`` of ``GET /v1/health``.

The nine are named constants rather than bare literals in the table below
because the adapter's handler map is keyed on them: a client generator's method
name and a server's dispatch key have to be the same string, and two spellings
of it are one rename away from a route mounted to nothing.
"""

_UPLOAD_DECODED_CAP_BYTES: Final[int] = 10 * 1024 * 1024
"""The decoded-document cap this route is sized to carry: 10 MiB.

Equal to :data:`creek_mcp.tools.upload.MAX_UPLOAD_BYTES`, which is the limit
that actually enforces it, and restated here rather than imported: this module
is the framework-free half of the published surface and importing the tool
would drag the whole ingest stack — ``creek.ingest``, the ledger, the
ingestor registry — into the import graph of the OpenAPI generator. The two are
pinned equal by a test, which is the cheaper end of that trade.
"""

_BASE64_NUMERATOR: Final[int] = 4
"""Base64 emits four characters per three input bytes."""

_BASE64_DENOMINATOR: Final[int] = 3
"""…and three input bytes are what those four characters encode."""

_UPLOAD_ENVELOPE_SLACK_BYTES: Final[int] = 64 * 1024
"""Headroom for the JSON around the payload: keys, quoting, id, tier, filename.

64 KiB is far more than the other four fields can occupy — ``external_id`` and
``filename`` are bounded at 512 and 255 characters — and the surplus is
deliberate: a cap that is *tight* against the encoding turns a legal 10 MiB
document into a ``422`` for reasons the caller cannot see, which is a worse
failure than a slightly generous buffer ceiling.
"""

UPLOAD_MAX_BODY_BYTES: Final[int] = (
    _BASE64_NUMERATOR
    * ((_UPLOAD_DECODED_CAP_BYTES + _BASE64_DENOMINATOR - 1) // _BASE64_DENOMINATOR)
    + _UPLOAD_ENVELOPE_SLACK_BYTES
)
"""The request-body cap ``POST /v1/uploads`` is served under.

**Why this route has its own cap at all.** The process-wide default is
:data:`creek_mcp.httpapi.middleware.limits.DEFAULT_MAX_BODY_BYTES` — one
mebibyte, which is comfortable for a journal entry and would silently cap
uploads at roughly 750 KiB of document, refusing an ordinary PDF as a malformed
request. Raising the *global* cap instead would have been the smaller diff and
the worse change: every route would then be allowed to make the server buffer
thirteen megabytes, and with
:data:`~creek_mcp.httpapi.middleware.limits.DEFAULT_MAX_CONCURRENCY` in flight
that is a memory commitment nothing on the reflection or journal path has any
use for.

Derived, never typed as a number: base64 of *N* bytes is exactly
``4 * ceil(N / 3)`` characters, so this is the encoding of the tool's own
decoded cap plus envelope slack. A literal here would drift the day either the
document cap or the encoding assumption moved.
"""


_PATH_TEMPLATE_MARKER: Final[str] = "{"
"""What makes a path a template rather than a literal one caller can be matched on."""


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One published endpoint, in the vocabulary every consumer of the table needs.

    Frozen and slotted on purpose. Frozen, so no import-time side effect
    anywhere in the process can rewrite the route table after the OpenAPI
    document and the handshake have been generated from it. Slotted, so a typo'd
    attribute (``requires_contract_versio``) cannot be attached while the gate
    goes on reading the real field as unchanged.

    Attributes:
        path: The route *template*, with ``{...}`` placeholders. This — never a
            concrete path — is what may reach an access log.
        method: The single HTTP method this spec serves.
        operation_id: The stable name client generators key their method names
            on. Unique across the table.
        capability: The :class:`~creek_mcp.api.models.Capability` this route
            serves, or ``None`` for infrastructure endpoints that are not a
            published capability at all.
        request_model: The wire model governing the request body, or ``None``
            when the route takes no body.
        response_model: The wire model governing the ``200`` body, or ``None``
            when the route's success shape is not part of the published
            contract.
        requires_contract_version: Whether the route refuses a request that
            does not declare a served contract minor.
        summary: One line of prose for the published document. Never empty — a
            blank summary publishes a blank cell.
        max_body_bytes: A request-body cap for this route alone, or ``None`` to
            be governed by the process-wide default. Declared on the route
            rather than passed to the middleware so that "how big may this
            endpoint's body be" is a published property of the endpoint, in the
            same table the OpenAPI document and the handler map are read from.
    """

    path: str
    method: str
    operation_id: str
    capability: Capability | None
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | None
    requires_contract_version: bool
    summary: str
    max_body_bytes: int | None = None

    def __post_init__(self) -> None:
        """Refuse a per-route body cap on a templated path.

        The body-size middleware runs *above* the router — it has to, or an
        unauthenticated caller could make the server buffer a large body before
        anything decided whether the path even exists — so it can only match a
        route by its literal ``scope["path"]``. A cap declared on
        ``/v1/things/{id}`` would therefore never be found and the route would
        quietly run under the global default, which is the failure mode where a
        limit *looks* configured and is not. Raised at import so it is a build
        failure rather than a surprise in production.

        Raises:
            ValueError: When a templated path declares its own cap.
        """
        if self.max_body_bytes is not None and _PATH_TEMPLATE_MARKER in self.path:
            msg = (
                f"{self.operation_id}: a per-route body cap cannot be matched on "
                f"a templated path ({self.path})"
            )
            raise ValueError(msg)


ROUTES: Final[tuple[RouteSpec, ...]] = (
    RouteSpec(
        path="/v1/capabilities",
        method="GET",
        operation_id=OP_CAPABILITIES,
        capability=Capability.CAPABILITIES,
        request_model=None,
        response_model=CapabilitiesResponse,
        requires_contract_version=False,
        summary="Negotiate versions, vault readiness and the capability list.",
    ),
    RouteSpec(
        path="/v1/journal-entries/{external_id}",
        method="PUT",
        operation_id=OP_JOURNAL_UPSERT,
        capability=Capability.JOURNAL_UPSERT,
        request_model=JournalUpsertRequest,
        response_model=JournalUpsertResponse,
        requires_contract_version=True,
        summary="Create or update one journal entry, idempotently.",
    ),
    RouteSpec(
        path="/v1/reflections",
        method="POST",
        operation_id=OP_REFLECTIONS,
        capability=Capability.REFLECTIONS,
        request_model=ReflectionRequest,
        response_model=ReflectionResponse,
        requires_contract_version=True,
        summary="Return anchored margin notes for one entry.",
    ),
    RouteSpec(
        path="/v1/wheel",
        method="GET",
        operation_id=OP_WHEEL,
        capability=Capability.WHEEL,
        request_model=None,
        response_model=WheelResponse,
        requires_contract_version=True,
        summary="Return the aggregate APTITUDE frequency distribution.",
    ),
    RouteSpec(
        path="/v1/uploads",
        method="POST",
        operation_id=OP_UPLOAD,
        capability=Capability.UPLOAD,
        request_model=UploadRequest,
        response_model=UploadResponse,
        requires_contract_version=True,
        summary="Ingest one uploaded document as a fragment, idempotently.",
        max_body_bytes=UPLOAD_MAX_BODY_BYTES,
    ),
    RouteSpec(
        path="/v1/connectors/drive",
        method="GET",
        operation_id=OP_DRIVE_STATUS,
        capability=Capability.DRIVE_CONNECTOR,
        request_model=None,
        response_model=DriveConnectorStatusResponse,
        requires_contract_version=True,
        summary="Report the read-only Google Drive connector's state.",
    ),
    RouteSpec(
        path="/v1/connectors/drive/syncs",
        method="POST",
        operation_id=OP_DRIVE_SYNC,
        capability=Capability.DRIVE_CONNECTOR,
        request_model=None,
        response_model=DriveSyncResponse,
        requires_contract_version=True,
        summary="Run one incremental Google Drive sync and ingest what it fetched.",
    ),
    RouteSpec(
        path="/v1/connectors/drive",
        method="DELETE",
        operation_id=OP_DRIVE_DISCONNECT,
        capability=Capability.DRIVE_CONNECTOR,
        request_model=None,
        response_model=DriveDisconnectResponse,
        requires_contract_version=True,
        summary="Revoke the cached Drive credential and erase it from disk.",
    ),
    RouteSpec(
        path="/v1/health",
        method="GET",
        operation_id=OP_HEALTH,
        capability=None,
        request_model=None,
        response_model=None,
        requires_contract_version=False,
        summary="Liveness probe; answers one constant derived from nothing.",
    ),
)
"""Every endpoint ``/v1`` publishes, in document order.

A tuple rather than a list so the table is immutable at the container level as
well as per entry: a list would let any import-time hook append a route after
the document and the handshake had already been read off it.

**Three entries share one capability**, and that is the first time the table
has done so. ``drive-connector`` is one feature — a client that may read the
connector's state may sync and disconnect it, and there is nothing useful it
could negotiate in between — so it is published as one name over three verbs
rather than as three names a server could half-implement. Nothing downstream
assumed the mapping was injective: :data:`~creek_mcp.httpapi.handlers.HANDLERS`
is keyed on ``operation_id``, the OpenAPI ``paths`` mapping is built with
``setdefault`` per path, and the handshake advertises a *set*.

``/v1/health`` is the one entry with no capability. Liveness is infrastructure,
not something a client negotiates, and it is exempt from the contract-version
gate for the same reason — an operator debugging a version mismatch must not
also lose their monitoring probe.
"""

ROUTE_BODY_CAPS: Final[dict[str, int]] = {
    spec.path: spec.max_body_bytes for spec in ROUTES if spec.max_body_bytes is not None
}
"""Per-path request-body caps, for the one middleware that has to apply them.

Derived from :data:`ROUTES` rather than written out beside the middleware, so a
route that declares a cap cannot be mounted without it — and so the OpenAPI
document, the handler map and the body limit are all read off one table.

Keyed by the literal path because the middleware sits above the router and has
only ``scope["path"]`` to match on; :meth:`RouteSpec.__post_init__` refuses a
cap on a templated path for exactly that reason, which is what makes this
comprehension total rather than lossy.
"""

IMPLEMENTED_CAPABILITIES: Final[frozenset[Capability]] = frozenset(Capability)
"""The capabilities this server actually answers for, as opposed to publishes.

Equal to ``set(Capability)`` since #1077 — spelled ``frozenset(Capability)``
rather than as four names, so a capability added to the enum is advertised only
once a handler exists for it: :func:`creek_mcp.httpapi.handlers._handler_for`
raises at import if it does not, which is the failure mode worth having.

It was a *strict* subset for the whole of #1074—#1076, and the machinery that
made that safe is unchanged. One constant drives both the advertised list in ``GET
/v1/capabilities`` and which routes get the stub, so the two cannot disagree —
adding a capability here without wiring a handler, or wiring a handler without
adding it here, turns ``tests/test_v1_api_capabilities.py`` red.

A ``frozenset`` so no importer can widen the server's own claim about itself.
"""
