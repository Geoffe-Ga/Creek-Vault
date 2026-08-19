"""The ``/v1`` route table is data, and this module is its contract (#1074).

``creek_mcp/api/routes.py`` is the one declaration of what ``/v1`` exposes. It
lives beside ``models.py`` in the **framework-free** half of the surface on
purpose: the OpenAPI generator, the Starlette adapter and the capability
handshake all read the same table, so none of them can advertise an endpoint
another does not serve. That is the whole reason the table is data rather than
a sequence of ``@app.route`` decorators — a decorator-defined route table can
only be discovered by importing the framework, and a document generated from a
framework's introspection is a function of the framework's version rather than
of our own models (ADR, "HTTP framework", reason 2).

This module deliberately imports **no** web framework and **no** part of
``creek_mcp.httpapi``. If any assertion here needed Starlette to hold, the
separation the ADR bought would already be gone.

Three properties carry most of the weight:

* **Coverage.** The union of the non-``None`` capabilities equals
  ``set(Capability)``, so a capability published in ``models.py`` cannot exist
  without a route, and a route cannot claim a capability that is not published.
* **Honesty.** ``IMPLEMENTED_CAPABILITIES`` is a *strict* subset of
  ``set(Capability)`` at #1074. The table says which endpoints exist; this
  constant says which ones actually answer. Collapsing the two is exactly the
  "plausible fake success" #1074 must not ship — the three unbuilt routes are
  wired to a ``501``, not to a fabricated ``200``.
* **Immutability.** ``RouteSpec`` is frozen and slotted, so no import-time
  side effect anywhere in the process can rewrite the published route table
  after it is read.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import TYPE_CHECKING, Final

import pytest

from creek_mcp.api.models import (
    CONTRACT_MODELS,
    CapabilitiesResponse,
    Capability,
    JournalUpsertRequest,
    JournalUpsertResponse,
    ReflectionRequest,
    ReflectionResponse,
    UploadRequest,
    UploadResponse,
    WheelResponse,
)
from creek_mcp.api.routes import (
    IMPLEMENTED_CAPABILITIES,
    ROUTE_BODY_CAPS,
    ROUTES,
    UPLOAD_MAX_BODY_BYTES,
    RouteSpec,
)
from creek_mcp.tools.upload import MAX_UPLOAD_BYTES

if TYPE_CHECKING:
    from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# The published table, restated here rather than imported
#
# Asserting ROUTES against itself would prove nothing. This tuple is the
# contract as a human reads it off the ADR, so a route added, renamed,
# re-methoded or re-gated has to be a deliberate edit in two places.
# --------------------------------------------------------------------------- #

_EXPECTED: Final[
    tuple[tuple[str, str, str, Capability | None, type | None, type | None, bool], ...]
] = (
    (
        "/v1/capabilities",
        "GET",
        "getCapabilities",
        Capability.CAPABILITIES,
        None,
        CapabilitiesResponse,
        False,
    ),
    (
        "/v1/journal-entries/{external_id}",
        "PUT",
        "upsertJournalEntry",
        Capability.JOURNAL_UPSERT,
        JournalUpsertRequest,
        JournalUpsertResponse,
        True,
    ),
    (
        "/v1/reflections",
        "POST",
        "createReflection",
        Capability.REFLECTIONS,
        ReflectionRequest,
        ReflectionResponse,
        True,
    ),
    (
        "/v1/wheel",
        "GET",
        "getWheel",
        Capability.WHEEL,
        None,
        WheelResponse,
        True,
    ),
    (
        "/v1/uploads",
        "POST",
        "uploadDocument",
        Capability.UPLOAD,
        UploadRequest,
        UploadResponse,
        True,
    ),
    (
        "/v1/health",
        "GET",
        "getHealth",
        None,
        None,
        None,
        False,
    ),
)

_EXPECTED_IDS: Final[tuple[str, ...]] = (
    "capabilities",
    "journal-upsert",
    "reflections",
    "wheel",
    "upload",
    "health",
)

_EXPECTED_ROUTE_COUNT: Final[int] = 6


def _by_path(path: str) -> RouteSpec:
    """Return the single :class:`RouteSpec` declared for *path*.

    Args:
        path: The route template.

    Returns:
        The matching spec.

    Raises:
        LookupError: When the table declares no such path, which is a clearer
            failure than an ``IndexError`` three assertions later.
    """
    for spec in ROUTES:
        if spec.path == path:
            return spec
    msg = f"no RouteSpec declared for {path}"
    raise LookupError(msg)


# --------------------------------------------------------------------------- #
# Shape of the table
# --------------------------------------------------------------------------- #


def test_routes_declares_exactly_six_specs() -> None:
    """``/v1`` publishes six endpoints and no seventh.

    A seventh would be an endpoint no fixture, no OpenAPI response set and no
    capability entry describes — reachable, undocumented surface.
    """
    assert len(ROUTES) == _EXPECTED_ROUTE_COUNT


def test_routes_is_a_tuple() -> None:
    """The table is immutable at the container level, not only per entry.

    A list would let any import-time hook append a route after the document
    and the handshake had already been generated from it.
    """
    assert isinstance(ROUTES, tuple)


def test_route_paths_and_methods_are_the_published_six() -> None:
    """Every ``(path, method)`` pair matches the ADR, in order."""
    assert [(spec.path, spec.method) for spec in ROUTES] == [
        (path, method) for path, method, *_rest in _EXPECTED
    ]


def test_operation_ids_are_unique() -> None:
    """No two routes share an ``operation_id``.

    Client generators key their method names on it, so a collision silently
    drops an endpoint from a generated SDK.
    """
    operation_ids = [spec.operation_id for spec in ROUTES]
    assert len(set(operation_ids)) == len(operation_ids)


def test_operation_ids_are_the_published_ones() -> None:
    """The ``operation_id``s are the published names, not incidental ones."""
    assert [spec.operation_id for spec in ROUTES] == [
        operation_id for _path, _method, operation_id, *_rest in _EXPECTED
    ]


def test_every_route_carries_a_summary() -> None:
    """Each route documents itself; an empty summary publishes a blank cell."""
    assert all(spec.summary.strip() for spec in ROUTES)


# --------------------------------------------------------------------------- #
# The contract-version gate, per route
# --------------------------------------------------------------------------- #


def test_capabilities_requires_no_contract_version() -> None:
    """The negotiation endpoint must never itself be able to fail to negotiate.

    A client pinned to a stale minor has to be able to read the server's real
    ``contract_version`` off *some* endpoint, or "upgrade required" collapses
    into "vault unavailable" — the exact collapse epic #1071 exists to stop.
    """
    assert _by_path("/v1/capabilities").requires_contract_version is False


def test_health_requires_no_contract_version() -> None:
    """Liveness is not a contract question.

    ``GET /v1/health`` answers "is this process up", which is true or false
    independent of which minor the caller speaks. Gating it would make a
    monitoring probe depend on the negotiated contract, so an operator
    debugging a version mismatch would see the probe go red too and learn
    nothing from it. Pinned rather than left implicit.
    """
    assert _by_path("/v1/health").requires_contract_version is False


@pytest.mark.parametrize(
    "path",
    [
        "/v1/journal-entries/{external_id}",
        "/v1/reflections",
        "/v1/wheel",
        "/v1/uploads",
    ],
)
def test_content_routes_require_the_contract_version(path: str) -> None:
    """Every route that touches the vault is gated on the declared minor.

    These three are the routes whose *shape* a minor bump can change, so a
    client speaking the wrong one must be refused ``409`` before any read
    rather than served a body it will misparse.

    Args:
        path: The route template under test.
    """
    assert _by_path(path).requires_contract_version is True


def test_exactly_two_routes_are_version_exempt() -> None:
    """Only capabilities and health skip the gate — a count, so a fourth shows.

    Stated as a count as well as per route: adding a version-exempt endpoint
    is how a vault-reading route quietly loses its gate.
    """
    exempt = [spec.path for spec in ROUTES if not spec.requires_contract_version]
    assert sorted(exempt) == ["/v1/capabilities", "/v1/health"]


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #


def test_every_declared_capability_is_a_capability_member() -> None:
    """No route names a capability outside the published closed enum."""
    declared = [spec.capability for spec in ROUTES if spec.capability is not None]
    assert declared
    assert all(isinstance(capability, Capability) for capability in declared)


def test_route_capabilities_cover_every_published_capability() -> None:
    """The four published capabilities are exactly the ones routed.

    Equality, not containment. A capability advertised in the handshake with
    no route is an endpoint a client will call and get ``404`` from; a route
    claiming a capability the enum does not publish is surface no fixture
    documents.
    """
    routed = {spec.capability for spec in ROUTES if spec.capability is not None}
    assert routed == set(Capability)


def test_exactly_one_route_declares_no_capability() -> None:
    """Health is infrastructure, not a capability, and it is the only one."""
    uncapable = [spec.path for spec in ROUTES if spec.capability is None]
    assert uncapable == ["/v1/health"]


def test_implemented_capabilities_is_exactly_the_published_five() -> None:
    """Every published capability answers for real, ``upload`` included (#1524).

    An exhaustive literal, not a containment check, and still named one by
    one: a sixth capability added to ``Capability`` has to change this line
    before it can be advertised, which is what keeps a new endpoint's landing a
    visible edit rather than a silent widening.
    """
    assert (
        frozenset(
            {
                Capability.CAPABILITIES,
                Capability.JOURNAL_UPSERT,
                Capability.REFLECTIONS,
                Capability.WHEEL,
                Capability.UPLOAD,
            }
        )
        == IMPLEMENTED_CAPABILITIES
    )


def test_every_published_capability_is_implemented() -> None:
    """The epic is finished, so the subset became an equality. Read this carefully.

    Until #1077 this asserted ``set(Capability) > IMPLEMENTED_CAPABILITIES`` — a
    *strict* superset — because three capabilities were published-but-unbuilt
    and the constant had to say so out loud. Equality is now the correct claim,
    and the reason it may be asserted is that it was reached the intended way:
    #1075, #1076 and #1077 each built a handler and moved one name across. The
    failure mode the old assertion guarded against was reaching equality by
    *shrinking* ``Capability`` instead — quietly dropping endpoints Adepthood
    already codes against — and that is now guarded by
    :func:`test_implemented_capabilities_is_exactly_the_published_five` above,
    which names all five and would go red on a removal.

    **What must not happen is this assertion being relaxed in the other
    direction.** ``>=`` here would let a capability be advertised with no
    handler behind it, which is the dishonesty the whole constant exists to
    prevent. If a sixth capability is published before its handler exists, this
    test is supposed to go red, and the fix is to restore the strict-superset
    form for the interim — not to weaken the operator.
    """
    assert set(Capability) == IMPLEMENTED_CAPABILITIES


def test_implemented_capabilities_is_a_frozenset() -> None:
    """The implemented set cannot be mutated by an importer."""
    assert isinstance(IMPLEMENTED_CAPABILITIES, frozenset)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "request_model", "response_model"),
    [(entry[0], entry[4], entry[5]) for entry in _EXPECTED],
    ids=_EXPECTED_IDS,
)
def test_route_models_are_the_published_ones(
    path: str,
    request_model: type[BaseModel] | None,
    response_model: type[BaseModel] | None,
) -> None:
    """Each route names the wire models the ADR assigns it.

    Args:
        path: The route template under test.
        request_model: The expected request model, or ``None``.
        response_model: The expected response model, or ``None``.
    """
    spec = _by_path(path)
    assert spec.request_model is request_model
    assert spec.response_model is response_model


def test_every_route_model_is_a_published_contract_model() -> None:
    """No route references a model outside the published registry.

    ``CONTRACT_MODELS`` is what the fixture bundle and the JSON Schemas are
    generated from, so a route typed on an unregistered model would be a
    documented endpoint with an undocumented payload.
    """
    published = set(CONTRACT_MODELS.values())
    referenced = {
        model
        for spec in ROUTES
        for model in (spec.request_model, spec.response_model)
        if model is not None
    }
    assert referenced <= published
    assert referenced, "no route references a wire model at all"


# --------------------------------------------------------------------------- #
# RouteSpec itself
# --------------------------------------------------------------------------- #


def test_route_spec_declares_the_published_fields() -> None:
    """``RouteSpec`` carries exactly the nine fields the adapter consumes."""
    assert {field.name for field in fields(RouteSpec)} == {
        "path",
        "method",
        "operation_id",
        "capability",
        "request_model",
        "response_model",
        "requires_contract_version",
        "summary",
        "max_body_bytes",
    }


# --------------------------------------------------------------------------- #
# Per-route body caps (#1524)
# --------------------------------------------------------------------------- #


def test_only_the_upload_route_declares_its_own_body_cap() -> None:
    """One route overrides the process-wide cap, and the map names only it.

    Derived from the table rather than listed beside the middleware, so a cap
    declared on a route cannot fail to be applied to it.
    """
    assert ROUTE_BODY_CAPS == {"/v1/uploads": UPLOAD_MAX_BODY_BYTES}


def test_the_upload_body_cap_carries_the_tools_whole_document_cap() -> None:
    """The wire cap admits a document of exactly ``MAX_UPLOAD_BYTES``.

    The sharp direction is the one this asserts: base64 expands by 4/3, so a
    body cap set to the *decoded* limit would refuse a legal 10 MiB document
    as malformed — a limit the caller can neither see nor satisfy. Recomputed
    here from ``MAX_UPLOAD_BYTES`` rather than read off the constant, because
    the two live in different packages precisely so the framework-free half
    need not import the ingest stack, and this test is what stands in for the
    import.
    """
    encoded = 4 * ((MAX_UPLOAD_BYTES + 2) // 3)
    assert encoded < UPLOAD_MAX_BODY_BYTES


def test_a_templated_path_may_not_declare_a_body_cap() -> None:
    """The cap is matched on the literal path, so a template could never find it.

    Refused at construction rather than left to be discovered as a limit that
    looks configured and silently is not.
    """
    with pytest.raises(ValueError, match="templated path"):
        RouteSpec(
            path="/v1/things/{thing_id}",
            method="POST",
            operation_id="createThing",
            capability=None,
            request_model=None,
            response_model=None,
            requires_contract_version=True,
            summary="A route that could never have its cap applied.",
            max_body_bytes=1,
        )


def test_route_spec_is_frozen() -> None:
    """A mounted route cannot be rewritten after the table is read.

    The attribute name is held in a variable so the assignment is dynamic:
    mypy rejects a literal assignment to a frozen dataclass field, and
    suppressions are not allowed here.
    """
    spec = ROUTES[0]
    attribute = "requires_contract_version"
    with pytest.raises(FrozenInstanceError):
        setattr(spec, attribute, False)
    assert spec.requires_contract_version is False


def test_route_spec_uses_slots() -> None:
    """``RouteSpec`` is slotted, so a typo'd attribute cannot be attached.

    Without ``slots=True`` a caller could set ``spec.requires_contract_versio``
    and the gate would read the real field as unchanged while the author
    believed it had been configured.
    """
    assert "__slots__" in RouteSpec.__dict__
    assert not hasattr(ROUTES[0], "__dict__")
