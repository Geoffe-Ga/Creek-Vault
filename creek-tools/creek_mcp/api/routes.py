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
moved the last three capabilities from the first set into the second, so the two
now coincide. The distinction is not obsolete: while a capability was unbuilt it
was wired to an honest ``501`` rather than to a fabricated ``200``, because a
stub that looks like success is one a consumer integrates against and only
discovers in production. A fifth capability published before its handler exists
gets exactly that treatment, from the same constant, with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek_mcp.api.models import (
    CapabilitiesResponse,
    Capability,
    JournalUpsertRequest,
    JournalUpsertResponse,
    ReflectionRequest,
    ReflectionResponse,
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

Also the value of every response's ``Vary``, so an intermediary cache can never
serve one caller's ceiling-filtered response to another.
"""

OP_CAPABILITIES: Final[str] = "getCapabilities"
"""``operation_id`` of ``GET /v1/capabilities``."""

OP_JOURNAL_UPSERT: Final[str] = "upsertJournalEntry"
"""``operation_id`` of ``PUT /v1/journal-entries/{external_id}``."""

OP_REFLECTIONS: Final[str] = "createReflection"
"""``operation_id`` of ``POST /v1/reflections``."""

OP_WHEEL: Final[str] = "getWheel"
"""``operation_id`` of ``GET /v1/wheel``."""

OP_HEALTH: Final[str] = "getHealth"
"""``operation_id`` of ``GET /v1/health``.

The five are named constants rather than bare literals in the table below
because the adapter's handler map is keyed on them: a client generator's method
name and a server's dispatch key have to be the same string, and two spellings
of it are one rename away from a route mounted to nothing.
"""


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
    """

    path: str
    method: str
    operation_id: str
    capability: Capability | None
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | None
    requires_contract_version: bool
    summary: str


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

``/v1/health`` is the one entry with no capability. Liveness is infrastructure,
not something a client negotiates, and it is exempt from the contract-version
gate for the same reason — an operator debugging a version mismatch must not
also lose their monitoring probe.
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
