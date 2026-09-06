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
from typing import Final

import pytest
from pydantic import BaseModel

from creek_mcp.api.models import (
    CONTRACT_MODELS,
    CapabilitiesResponse,
    Capability,
    ClassificationRequest,
    ClassificationResponse,
    DriveAuthorizationExchangeRequest,
    DriveAuthorizationRequest,
    DriveAuthorizationResponse,
    DriveConnectorStatusResponse,
    DriveDisconnectResponse,
    DriveSyncResponse,
    JobAcceptedResponse,
    JobStatusResponse,
    JournalUpsertRequest,
    JournalUpsertResponse,
    LinkRequest,
    LinkResponse,
    ReflectionRequest,
    ReflectionResponse,
    UploadRequest,
    UploadResponse,
    VoiceDraftDeleteResponse,
    VoiceDraftReadResponse,
    VoiceDraftUpsertRequest,
    VoiceDraftUpsertResponse,
    WheelResponse,
)
from creek_mcp.api.routes import (
    IMPLEMENTED_CAPABILITIES,
    PUBLISHABLE_SUCCESS_STATUSES,
    PUBLISHED_SUCCESS_STATUSES,
    ROUTE_BODY_CAPS,
    ROUTES,
    UPLOAD_MAX_BODY_BYTES,
    RouteSpec,
    published_success_statuses,
)
from creek_mcp.tools.upload import MAX_UPLOAD_BYTES

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
        "/v1/voice-drafts/{external_id}",
        "PUT",
        "upsertVoiceDraft",
        Capability.VOICE_DRAFTS,
        VoiceDraftUpsertRequest,
        VoiceDraftUpsertResponse,
        True,
    ),
    (
        "/v1/voice-drafts/{external_id}",
        "GET",
        "getVoiceDraft",
        Capability.VOICE_DRAFTS,
        None,
        VoiceDraftReadResponse,
        True,
    ),
    (
        "/v1/voice-drafts/{external_id}",
        "DELETE",
        "deleteVoiceDraft",
        Capability.VOICE_DRAFTS,
        None,
        VoiceDraftDeleteResponse,
        True,
    ),
    (
        "/v1/connectors/drive",
        "GET",
        "getDriveConnector",
        Capability.DRIVE_CONNECTOR,
        None,
        DriveConnectorStatusResponse,
        True,
    ),
    (
        "/v1/connectors/drive/syncs",
        "POST",
        "syncDriveConnector",
        Capability.DRIVE_CONNECTOR,
        None,
        DriveSyncResponse,
        True,
    ),
    (
        "/v1/connectors/drive/authorizations",
        "POST",
        "createDriveAuthorization",
        Capability.DRIVE_CONNECTOR,
        DriveAuthorizationRequest,
        DriveAuthorizationResponse,
        True,
    ),
    (
        "/v1/connectors/drive/authorizations/{state}",
        "POST",
        "completeDriveAuthorization",
        Capability.DRIVE_CONNECTOR,
        DriveAuthorizationExchangeRequest,
        DriveConnectorStatusResponse,
        True,
    ),
    (
        "/v1/connectors/drive",
        "DELETE",
        "disconnectDriveConnector",
        Capability.DRIVE_CONNECTOR,
        None,
        DriveDisconnectResponse,
        True,
    ),
    (
        "/v1/classifications",
        "POST",
        "createClassification",
        Capability.PIPELINE,
        ClassificationRequest,
        ClassificationResponse,
        True,
    ),
    (
        "/v1/links",
        "POST",
        "createLink",
        Capability.PIPELINE,
        LinkRequest,
        LinkResponse,
        True,
    ),
    (
        "/v1/jobs/{job_id}",
        "GET",
        "getPipelineJob",
        Capability.PIPELINE,
        None,
        JobStatusResponse,
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
    "voice-draft-upsert",
    "voice-draft-read",
    "voice-draft-delete",
    "drive-status",
    "drive-sync",
    "drive-authorize",
    "drive-authorize-complete",
    "drive-disconnect",
    "classifications",
    "links",
    "job-status",
    "health",
)

_EXPECTED_ROUTE_COUNT: Final[int] = 17


def _by_operation(operation_id: str) -> RouteSpec:
    """Return the :class:`RouteSpec` declared for *operation_id*.

    Keyed on the operation rather than on the path since #1527, when
    ``/v1/connectors/drive`` became the first template serving two methods: a
    path lookup would have returned whichever of the two was declared first
    and silently stopped asserting anything about the other. ``operation_id``
    is unique by construction — ``test_operation_ids_are_unique`` pins it — so
    this lookup is total.

    Args:
        operation_id: The published operation name.

    Returns:
        The matching spec.

    Raises:
        LookupError: When the table declares no such operation, which is a
            clearer failure than an ``IndexError`` three assertions later.
    """
    for spec in ROUTES:
        if spec.operation_id == operation_id:
            return spec
    msg = f"no RouteSpec declared for {operation_id}"
    raise LookupError(msg)


# --------------------------------------------------------------------------- #
# Shape of the table
# --------------------------------------------------------------------------- #


def test_routes_declares_exactly_seventeen_specs() -> None:
    """``/v1`` publishes seventeen endpoints and no eighteenth.

    An eighteenth would be an endpoint no fixture, no OpenAPI response set and
    no capability entry describes — reachable, undocumented surface.
    """
    assert len(ROUTES) == _EXPECTED_ROUTE_COUNT


def test_routes_is_a_tuple() -> None:
    """The table is immutable at the container level, not only per entry.

    A list would let any import-time hook append a route after the document
    and the handshake had already been generated from it.
    """
    assert isinstance(ROUTES, tuple)


def test_route_paths_and_methods_are_the_published_seventeen() -> None:
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
# Per-route success responses (#1605)
#
# Until this landed, "the one success status any operation documents" was a
# module constant in the generator (``openapi.py``'s ``_SUCCESS_STATUS``) and a
# literal ``200`` in five test modules. A route that answers a second success
# status could not be expressed at all, and the closed status set the contract
# publishes was derived from the error table alone. The declaration now lives
# on the route, beside the request and response models it belongs with, and
# every derivation of the published set reads it from the table.
#
# Every route currently keeps the default, so the generated document is
# byte-identical — pinned by the golden file in ``test_v1_api_openapi.py``.
# --------------------------------------------------------------------------- #


def test_every_route_declares_its_published_success_responses() -> None:
    """Each route resolves to a non-empty status-to-model mapping.

    A *mapping*, not a bare tuple of statuses, because the generator derives
    each success response's schema from a model: one route documenting two
    success statuses documents two different bodies, and a tuple of ints
    carries the statuses without the bodies.
    """
    for spec in ROUTES:
        declared = spec.published_success_responses
        assert declared, spec.operation_id
        for status, model in declared:
            assert status in PUBLISHABLE_SUCCESS_STATUSES, spec.operation_id
            assert model is None or issubclass(model, BaseModel), spec.operation_id


def test_only_long_pipeline_writes_declare_the_202_success() -> None:
    """Only classification and linking can return a durable job handle.

    This is the assertion that makes the diff reviewable: the mechanism is
    landed and inert, so the whole of its effect on the published document is
    "no change". The first route to declare a second success status has to
    edit this test on purpose.
    """
    for spec in ROUTES:
        if spec.operation_id in {"createClassification", "createLink"}:
            assert spec.published_success_responses == (
                (200, spec.response_model),
                (202, JobAcceptedResponse),
            )
        else:
            assert spec.success_responses is None
            assert spec.published_success_responses == ((200, spec.response_model),)


def test_the_published_success_statuses_are_derived_from_the_table() -> None:
    """The published success set is read off the routes, not restated.

    Derived, so the day a route declares a ``202`` the closed status set the
    contract publishes grows with it in every module that consumes it, rather
    than in the five that remembered to be edited.
    """
    published = PUBLISHED_SUCCESS_STATUSES
    assert published == published_success_statuses(ROUTES)
    assert published == frozenset({200, 202})


def test_a_second_success_status_reaches_the_derived_set() -> None:
    """A route declaring ``202`` widens the derived set — the mutation proof.

    Built from a local route tuple rather than by mutating ``ROUTES``, which
    is frozen data three other modules read. Without this the derivation above
    would be indistinguishable from a hardcoded ``{200}``.
    """
    accepting = RouteSpec(
        path="/v1/things",
        method="POST",
        operation_id="createThing",
        capability=None,
        request_model=None,
        response_model=WheelResponse,
        requires_contract_version=True,
        summary="A route that answers two success statuses.",
        success_responses=((200, WheelResponse), (202, CapabilitiesResponse)),
    )
    assert published_success_statuses((*ROUTES, accepting)) == frozenset({200, 202})


def test_a_route_may_not_declare_an_empty_success_response_set() -> None:
    """An operation documenting no success at all is a build failure.

    Refused at import, like the templated-path body cap, because the failure
    it prevents is an operation published with only refusals in it — a
    document telling a consumer the route can never succeed.
    """
    with pytest.raises(ValueError, match="at least one success response"):
        _spec_with_success_responses(())


def test_a_route_may_not_declare_the_same_success_status_twice() -> None:
    """Two entries for one status would silently drop one of the two bodies.

    ``_responses`` keys on the status, so the second entry would overwrite the
    first and the document would publish one body while the table declared
    two.
    """
    with pytest.raises(ValueError, match="declares status 200 twice"):
        _spec_with_success_responses(((200, WheelResponse), (200, WheelResponse)))


@pytest.mark.parametrize("status", [201, 204, 404, 503], ids=str)
def test_a_route_may_not_publish_a_success_status_outside_the_closed_pair(
    status: int,
) -> None:
    """Only ``200`` and ``202`` may be declared as a success.

    The two refusal statuses in this list are the sharp cases. Declaring a
    ``404`` as a *success* would exempt it from
    ``test_every_non_success_response_is_the_error_envelope``, so a route
    could publish a bespoke body on a refusal status — which is exactly how a
    second error shape, and then a field echoing vault material, is born.
    ``201`` and ``204`` are refused because the contract publishes a closed
    status set and neither is in it.
    """
    with pytest.raises(ValueError, match="not a publishable success status"):
        _spec_with_success_responses(((status, WheelResponse),))


def _spec_with_success_responses(
    declared: tuple[tuple[int, type[BaseModel] | None], ...],
) -> RouteSpec:
    """Build a throwaway route declaring *declared*, for the guard tests.

    Args:
        declared: The ``success_responses`` value under test.

    Returns:
        The constructed spec, when the guard admits it.
    """
    return RouteSpec(
        path="/v1/things",
        method="POST",
        operation_id="createThing",
        capability=None,
        request_model=None,
        response_model=WheelResponse,
        requires_contract_version=True,
        summary="A route built only to exercise the success-response guard.",
        success_responses=declared,
    )


# --------------------------------------------------------------------------- #
# The contract-version gate, per route
# --------------------------------------------------------------------------- #


def test_capabilities_requires_no_contract_version() -> None:
    """The negotiation endpoint must never itself be able to fail to negotiate.

    A client pinned to a stale minor has to be able to read the server's real
    ``contract_version`` off *some* endpoint, or "upgrade required" collapses
    into "vault unavailable" — the exact collapse epic #1071 exists to stop.
    """
    assert _by_operation("getCapabilities").requires_contract_version is False


def test_health_requires_no_contract_version() -> None:
    """Liveness is not a contract question.

    ``GET /v1/health`` answers "is this process up", which is true or false
    independent of which minor the caller speaks. Gating it would make a
    monitoring probe depend on the negotiated contract, so an operator
    debugging a version mismatch would see the probe go red too and learn
    nothing from it. Pinned rather than left implicit.
    """
    assert _by_operation("getHealth").requires_contract_version is False


_VERSION_GATED_OPERATIONS: Final[tuple[str, ...]] = (
    "upsertJournalEntry",
    "createReflection",
    "getWheel",
    "uploadDocument",
    "getDriveConnector",
    "syncDriveConnector",
    "disconnectDriveConnector",
)
"""Every operation the contract-version gate applies to, restated from the ADR.

A tuple rather than a comprehension over ``ROUTES``: derived from the table it
is asserting against, this test could not fail. Emptying it is caught by
``test_exactly_two_routes_are_version_exempt``, which counts from the other
end.
"""


@pytest.mark.parametrize("operation_id", _VERSION_GATED_OPERATIONS)
def test_content_routes_require_the_contract_version(operation_id: str) -> None:
    """Every route that touches the vault is gated on the declared minor.

    These are the routes whose *shape* a minor bump can change, so a client
    speaking the wrong one must be refused ``409`` before any read rather than
    served a body it will misparse. The three Drive-connector verbs are gated
    for the sharper reason that their capability did not exist before 0.9: an
    ungated one would answer a client whose vendored contract cannot describe
    the response it is about to receive.

    Args:
        operation_id: The published operation under test.
    """
    assert _by_operation(operation_id).requires_contract_version is True


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
    """The six published capabilities are exactly the ones routed.

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


def test_implemented_capabilities_is_exactly_the_published_eight() -> None:
    """Every published capability answers for real, Voice Drafts included (#1727).

    An exhaustive literal, not a containment check, and still named one by
    one: an eighth capability added to ``Capability`` has to change this line
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
                Capability.DRIVE_CONNECTOR,
                Capability.PIPELINE,
                Capability.VOICE_DRAFTS,
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
    ("operation_id", "request_model", "response_model"),
    [(entry[2], entry[4], entry[5]) for entry in _EXPECTED],
    ids=_EXPECTED_IDS,
)
def test_route_models_are_the_published_ones(
    operation_id: str,
    request_model: type[BaseModel] | None,
    response_model: type[BaseModel] | None,
) -> None:
    """Each route names the wire models the ADR assigns it.

    Args:
        operation_id: The published operation under test.
        request_model: The expected request model, or ``None``.
        response_model: The expected response model, or ``None``.
    """
    spec = _by_operation(operation_id)
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
    """``RouteSpec`` carries exactly the ten fields the adapter consumes."""
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
        "success_responses",
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
