"""``GET /v1/capabilities`` is the one endpoint that must never lie (#1074).

It is the endpoint a client calls *first*, before it knows whether anything
else will work, and the ADR's capability table gives it four states — ``ok``,
``uninitialized``, ``incompatible``, and "unreachable" (no body at all). Three
of them are HTTP ``200``; readiness lives in the ``status`` field, never in the
status line. A ``503`` from this endpoint would collapse "your vault has not
been scaffolded" into "the server is down", which is precisely the collapse
epic #1071 exists to stop happening at the application layer.

Two properties in here are load-bearing beyond the obvious.

**The double-call honesty test.** ``handshake_tool`` audits every call, and
:meth:`creek_mcp.audit.MCPAuditLog.append` did ``mkdir(parents=True,
exist_ok=True)`` unconditionally. So calling it against a vault that has no
``00-Creek-Meta`` *created* ``00-Creek-Meta/audit/`` as a side effect — and the
second call then found the marker and reported ``available: true`` for a vault
that was never initialised. Calling twice is the only way to see this; one call
passes either way.

This module's answer was to probe with
:func:`creek_mcp.tools.handshake.vault_available` and not call
``handshake_tool`` at all when the marker is absent. #1108 closed the hole in
the tool itself, so the endpoint is now honest twice over. The test below stays
exactly as it was: it pins the endpoint's observable contract, which is what the
ADR published, and it must keep passing however readiness comes to be decided.

**Advertised equals implemented.** The response's ``capabilities`` list must
equal :data:`creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES`, not
``set(Capability)``. Advertising an endpoint that answers ``501`` is a lie a
client cannot detect until it has already written the integration. That is what
turned #1075/#1076/#1077 red until each wired a real handler.

Those are closed now, and with them the deliberate gap this module used to
record: the committed ``examples/capabilities/success.json`` fixture documented
the finished steady state while the running server advertised only what it had
built. :func:`test_the_fixture_the_live_server_and_the_enum_all_agree` replaced
that record with a four-way equality — fixture, enum, implemented set, live
response — so the closed divergence cannot quietly reopen (#1112). The constant
stays a constant regardless: it is what makes "a route exists but is unbuilt"
expressible at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
import yaml
from starlette.testclient import TestClient

from creek_mcp.api.models import (
    CONTRACT_MINOR,
    ERROR_MESSAGES,
    SUPPORTED_CONTRACT_MINORS,
    CapabilitiesResponse,
    CapabilitiesStatus,
    Capability,
    ErrorCode,
)
from creek_mcp.api.openapi import build_openapi
from creek_mcp.api.routes import IMPLEMENTED_CAPABILITIES, ROUTES
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi import vault as vault_module
from creek_mcp.tools.handshake import CREEK_MARKER, vault_available
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    OP_REFLECTIONS,
    REFLECTIONS_PATH,
    build_app,
    client,
    envelope,
    headers,
    route_for,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""``<repo-root>``: ``tests`` -> ``creek-tools`` -> the root that owns ``docs``."""

_OK_STATUS: Final[int] = 200
_UNSUPPORTED_STATUS: Final[int] = 501
"""The only status a reachable, authenticated, well-formed handshake returns."""

_INVALID_REQUEST_STATUS: Final[int] = 422
"""What a caller's own malformed header gets — on this route like every other."""

SUCCESS_FIXTURE: Final[Path] = (
    REPO_ROOT
    / "docs"
    / "contracts"
    / "adepthood-v1"
    / "examples"
    / "capabilities"
    / "success.json"
)

EXPECTED_TIER_MODEL: Final[dict[str, object]] = {
    "ceilings": ["open", "personal"],
    "default": "open",
    "intimate_never_egresses": True,
}

_UNIMPLEMENTED_CAPABILITIES: Final[tuple[Capability, ...]] = tuple(
    capability
    for capability in Capability
    if capability not in IMPLEMENTED_CAPABILITIES
)


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded, initialised vault."""
    yield seed_vault(tmp_path)


@pytest.fixture
def bare(tmp_path: Path) -> Iterator[Path]:
    """Yield a directory that exists but has never been ``creek init``-ed."""
    root = tmp_path / "not-a-vault"
    root.mkdir()
    yield root


def _get(vault_path: Path, **header_kwargs: str | None) -> dict[str, object]:
    """Call ``GET /v1/capabilities`` against *vault_path* and return the body.

    Args:
        vault_path: The vault the app is built over.
        **header_kwargs: Passed to :func:`tests.v1_api_support.headers`.

    Returns:
        The decoded response body.
    """
    with client(vault_path=vault_path) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers(**header_kwargs))
    assert response.status_code == 200
    return envelope(response)


# --------------------------------------------------------------------------- #
# State (a): a present, usable vault
# --------------------------------------------------------------------------- #


def test_a_present_vault_reports_ok(vault: Path) -> None:
    """Every published field of the ``ok`` handshake, pinned.

    The version strings are compared against the runtime constants rather
    than literals, so a bump moves the test and the wire together.

    Args:
        vault: A seeded vault.
    """
    body = _get(vault)
    parsed = CapabilitiesResponse.model_validate(body)
    assert parsed.status is CapabilitiesStatus.OK
    assert parsed.contract_version == CONTRACT_VERSION
    assert parsed.contract_minor == CONTRACT_MINOR
    assert parsed.supported_contract_minors == list(SUPPORTED_CONTRACT_MINORS)
    assert parsed.ontology_version == ONTOLOGY_VERSION
    assert parsed.vault.available is True


def test_the_tier_model_is_the_standing_promise(vault: Path) -> None:
    """``tier_model`` advertises two ceilings, an ``open`` default, and the vow.

    ``intimate`` is absent from ``ceilings`` because it is not constructible
    on this wire at all, and ``default: open`` is what makes an omitted header
    fail closed.

    Args:
        vault: A seeded vault.
    """
    assert _get(vault)["tier_model"] == EXPECTED_TIER_MODEL


def test_a_present_vault_advertises_only_the_built_capabilities(
    vault: Path,
) -> None:
    """At #1527 that is every published capability, ``drive-connector`` too.

    Spelled as a literal rather than derived from
    ``IMPLEMENTED_CAPABILITIES`` — that derivation already has its own test
    below. This one is the pin that makes a capability's landing *visible* in
    a diff: a sixth capability has to add its name here, and a handler wired
    without the constant (or vice versa) cannot slip past both.

    The request carries the current minor (``headers()`` defaults to it), so
    this is the *newest*-client view. The older-client view — where ``upload``
    and ``drive-connector`` are withheld — is asserted separately below.

    Args:
        vault: A seeded vault.
    """
    assert _get(vault)["capabilities"] == [
        "capabilities",
        "journal-upsert",
        "reflections",
        "wheel",
        "upload",
        "drive-connector",
    ]


def test_the_response_carries_no_field_beyond_the_published_model(
    vault: Path,
) -> None:
    """The key set is exactly ``CapabilitiesResponse``'s.

    ``extra="forbid"`` catches an *added* key on parse, but a *missing* one
    would leave the model happy if it had a default. Comparing key sets
    catches both directions.

    Args:
        vault: A seeded vault.
    """
    assert set(_get(vault)) == set(CapabilitiesResponse.model_fields)


# --------------------------------------------------------------------------- #
# State (b): reachable, uninitialised
# --------------------------------------------------------------------------- #


def test_an_uninitialised_vault_is_a_200_not_a_503(bare: Path) -> None:
    """ "No vault yet" is a legitimate state, and it still negotiates.

    Both version strings are present, so a client can renegotiate against a
    server whose vault does not exist — otherwise version negotiation would
    need a vault to negotiate about.

    Args:
        bare: A directory with no ``00-Creek-Meta``.
    """
    body = _get(bare)
    parsed = CapabilitiesResponse.model_validate(body)
    assert parsed.status is CapabilitiesStatus.UNINITIALIZED
    assert parsed.vault.available is False
    assert parsed.capabilities == []
    assert parsed.contract_version == CONTRACT_VERSION
    assert parsed.ontology_version == ONTOLOGY_VERSION


def test_repeated_calls_never_conjure_the_creek_marker(bare: Path) -> None:
    """THE DOUBLE-CALL HONESTY TEST — two calls, both ``uninitialized``.

    ``handshake_tool`` audits, and the audit substrate creates its own
    directory on first write. So a handler that reached for the tool against a
    vault with no ``00-Creek-Meta`` would *create* ``00-Creek-Meta/audit/`` as
    a side effect and then, on the very next call, find the marker and report
    ``available: true`` for a vault nobody ever initialised. Two independent
    things now stop that — this handler's probe, and (since #1108) the tool's
    own refusal to write an audit entry into a vault that does not exist — and
    this test is deliberately blind to which one is doing the work, because the
    published contract is about the endpoint's answer, not its mechanism.

    A single call cannot see this: the first response is honest either way.
    That is exactly why it has to be two, and why the directory is asserted
    absent afterwards rather than merely the second status asserted equal to
    the first.

    Args:
        bare: A directory with no ``00-Creek-Meta``.
    """
    first = _get(bare)
    second = _get(bare)
    assert first["status"] == CapabilitiesStatus.UNINITIALIZED.value
    assert second["status"] == CapabilitiesStatus.UNINITIALIZED.value
    assert first["vault"] == {"available": False}
    assert second["vault"] == {"available": False}
    assert not (bare / CREEK_MARKER).exists()
    assert list(bare.iterdir()) == []


# --------------------------------------------------------------------------- #
# State (c): reachable, contract-incompatible
# --------------------------------------------------------------------------- #


def test_an_incompatible_minor_is_a_200_incompatible_body(vault: Path) -> None:
    """A stale client gets ``200 incompatible``, never ``409``.

    The negotiation endpoint is exempt from the version gate on purpose: a
    client pinned to ``0.1`` has to be able to read the server's real
    ``contract_version`` off *some* endpoint so it can render "upgrade
    required" instead of "vault unavailable".

    ``vault.available`` is asserted ``True`` here, and ``False`` over a bare
    directory in the sibling below. Together they prove the field is a real
    probe result at ``incompatible`` and not a constant — which is what both
    capability-state tables now document, after they spent several minors
    rendering that cell as ``—`` (#1150). ``—`` reads as absent or
    unspecified, and it is neither: ``_render`` emits
    ``VaultState(available=…)`` at every status and only ``capabilities`` is
    emptied for a non-OK one.

    Args:
        vault: A seeded vault.
    """
    body = _get(vault, minor="0.1")
    parsed = CapabilitiesResponse.model_validate(body)
    assert parsed.status is CapabilitiesStatus.INCOMPATIBLE
    assert parsed.capabilities == []
    assert parsed.contract_version == CONTRACT_VERSION
    assert parsed.ontology_version == ONTOLOGY_VERSION
    assert parsed.vault.available is True


def test_an_incompatible_minor_still_reports_an_absent_vault(bare: Path) -> None:
    """``incompatible`` over a bare directory reports ``available: false``.

    The mirror of the assertion above. A stale client is told the truth about
    its vault either way, which is the reason #1148 skipped only the *audit*
    half at an unserved minor and deliberately kept the probe: dropping it
    would report ``available: false`` against a healthy vault and turn "you are
    on an old contract" into "your vault is down".

    Args:
        bare: A directory that has never been ``creek init``-ed.
    """
    parsed = CapabilitiesResponse.model_validate(_get(bare, minor="0.1"))
    assert parsed.status is CapabilitiesStatus.INCOMPATIBLE
    assert parsed.vault.available is False


def test_a_missing_version_header_is_not_an_error(vault: Path) -> None:
    """Capabilities requires nothing on the version axis.

    Args:
        vault: A seeded vault.
    """
    body = _get(vault, minor=None)
    assert body["status"] == CapabilitiesStatus.OK.value


def test_the_current_minor_negotiates_successfully(vault: Path) -> None:
    """The supported minor is accepted, so the ``0.1`` case above is meaningful.

    Args:
        vault: A seeded vault.
    """
    assert _get(vault, minor=CONTRACT_MINOR)["status"] == CapabilitiesStatus.OK.value


# --------------------------------------------------------------------------- #
# The endpoint never fails on a broken config
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("no creek_config.yaml"),
        ValueError("malformed vault_path"),
        yaml.YAMLError("could not parse"),
    ],
    ids=["missing", "invalid", "unparseable"],
)
def test_an_unreadable_config_degrades_to_uninitialized(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A config the server cannot read is still a ``200 uninitialized``.

    The handshake is the endpoint an operator uses to *find out* the vault is
    broken. Answering with ``500`` or ``503`` would tell them the service is
    down, sending them to look at the wrong thing.

    Args:
        monkeypatch: Replaces ``load_config`` with a raiser, on the module
            that now owns vault resolution for every route (#1075).
        error: The exception ``load_config`` raises.
    """

    def _raise() -> None:
        raise error

    monkeypatch.setattr(vault_module, "load_config", _raise)
    with client(vault_path=None) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == 200
    body = envelope(response)
    assert body["status"] == CapabilitiesStatus.UNINITIALIZED.value
    assert body["vault"] == {"available": False}
    assert body["contract_version"] == CONTRACT_VERSION


# --------------------------------------------------------------------------- #
# Advertised == implemented
# --------------------------------------------------------------------------- #


def test_advertised_capabilities_equal_implemented_capabilities(
    vault: Path,
) -> None:
    """The live list is exactly what this server actually answers for.

    In ``Capability`` declaration order, not set order, so the wire is
    deterministic and two servers at the same commit emit the same bytes.

    Args:
        vault: A seeded vault.
    """
    expected = [
        capability.value
        for capability in Capability
        if capability in IMPLEMENTED_CAPABILITIES
    ]
    assert _get(vault)["capabilities"] == expected


@pytest.mark.parametrize(
    "capability", sorted(IMPLEMENTED_CAPABILITIES), ids=lambda c: str(c.value)
)
def test_every_implemented_capability_has_a_real_handler(
    vault: Path, capability: Capability
) -> None:
    """An advertised capability is mounted to a handler that is not the stub.

    ``unimplemented()`` stamps its product with ``unimplemented_capability``,
    so "is this the stub?" is a fact about the mounted endpoint rather than a
    guess from its behaviour.

    Args:
        vault: A seeded vault.
        capability: The advertised capability under test.
    """
    spec = next(entry for entry in ROUTES if entry.capability is capability)
    app = build_app(vault_path=vault)
    endpoint = route_for(app, spec.path, spec.method).endpoint
    assert getattr(endpoint, "unimplemented_capability", None) is None


def test_the_unimplemented_factory_marks_its_product() -> None:
    """The marker check above is not vacuous.

    ``getattr(endpoint, ..., None) is None`` is satisfied by *any* object that
    has never heard of the attribute, so the negative half of the pair would
    stay green if the factory stopped stamping. Feed the factory a capability
    and pin that the stamp appears.
    """
    stub = handlers_module.unimplemented(Capability.WHEEL)
    assert getattr(stub, "unimplemented_capability", None) is Capability.WHEEL


def test_no_published_capability_is_mounted_to_the_stub() -> None:
    """#1077 finished the epic, so the unimplemented set is empty. On purpose.

    Until now this module held a parametrized converse — one case per
    published-but-unbuilt capability, asserting it was mounted to the honest
    ``501`` — plus a guard asserting that set was non-empty so the parametrize
    had rows. Both are gone, because both were about the *interim*: #1075,
    #1076 and #1077 each built one handler, and there is no longer a capability
    that is advertised and unbuilt.

    Deleting them without replacement would have retired the machinery along
    with the interim, which is the wrong trade — the machinery is what stops the
    *next* capability being advertised before it works. So the guard is
    restated as the invariant that actually matters now, and
    :func:`test_the_stub_still_answers_501_for_an_unbuilt_capability` below
    keeps the stub itself exercised against the day a fifth capability lands.
    """
    assert _UNIMPLEMENTED_CAPABILITIES == ()


def test_the_stub_still_answers_501_for_an_unbuilt_capability(vault: Path) -> None:
    """The honesty stub is kept alive by exercising it, not by having a victim.

    With every capability built, nothing in the *route table* reaches
    :class:`~creek_mcp.httpapi.handlers.UnimplementedHandler` any more. That is
    exactly when an unused guard rots: the next capability added to
    ``Capability`` before its handler exists would be mounted to a stub nobody
    had run in months.

    So the machinery is driven directly — the route table's handler map is
    substituted for one operation, the app is built around it, and the mounted
    endpoint is asserted to be both *stamped* and *answering* the published
    ``501``. That is the same path a genuinely unbuilt capability would take.

    Args:
        vault: A seeded vault.
    """
    stub = handlers_module.unimplemented(Capability.REFLECTIONS)
    substituted = {**handlers_module.HANDLERS, OP_REFLECTIONS: stub}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(handlers_module, "HANDLERS", substituted)
        app = build_app(vault_path=vault)
        endpoint = route_for(app, REFLECTIONS_PATH, "POST").endpoint
        with TestClient(app) as test_client:
            response = test_client.post(
                REFLECTIONS_PATH,
                json={"content": "anything at all"},
                headers=headers(ceiling="open"),
            )

    assert getattr(endpoint, "unimplemented_capability", None) is Capability.REFLECTIONS
    assert response.status_code == _UNSUPPORTED_STATUS
    assert envelope(response)["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value


def test_the_fixture_the_live_server_and_the_enum_all_agree(vault: Path) -> None:
    """One four-way equality: fixture == enum == implemented == live response.

    This test used to be
    ``test_capabilities_fixture_documents_the_completed_steady_state``, and its
    docstring recorded a *deliberate divergence*: while epic #1071 was being
    built out, the committed fixture documented the finished steady state while
    the running server advertised only what it had actually implemented, so the
    two were knowingly unequal "for as long as #1075—#1077 are open".

    **#1077 and #1071 are both closed, so there is no divergence left to
    record** (#1112). The assertion body had already become the steady-state
    equality; only the name and the docstring still sold the gap. Rather than
    delete the test — which would drop the fixture from the gate entirely —
    it now asserts every side of the agreement at once:

    * the committed ``examples/capabilities/success.json`` fixture,
    * ``[c.value for c in Capability]`` in declaration order,
    * :data:`creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES`, and
    * a live ``GET /v1/capabilities`` over a seeded vault declaring the
      current minor.

    A seventh capability added to the enum therefore fails here, at the
    fixture, and at ``test_advertised_capabilities_equal_implemented_capabilities``
    together, rather than passing because one of the four was left behind.

    The live call declares ``CONTRACT_MINOR`` explicitly: the advertised list
    is filtered per caller by ``CAPABILITY_SINCE_MINOR``, so a caller at an
    older minor is legitimately shown fewer, and pinning the full set means
    pinning it at the minor that publishes all of them.

    Args:
        vault: A seeded vault.
    """
    declared = [capability.value for capability in Capability]
    payload = json.loads(SUCCESS_FIXTURE.read_text(encoding="utf-8"))
    assert payload["status"] == CapabilitiesStatus.OK.value
    assert payload["capabilities"] == declared
    implemented = sorted(capability.value for capability in IMPLEMENTED_CAPABILITIES)
    assert implemented == sorted(declared)
    assert _get(vault, minor=CONTRACT_MINOR)["capabilities"] == declared


# --------------------------------------------------------------------------- #
# vault_available — the probe that replaces the tool call
# --------------------------------------------------------------------------- #


def test_vault_available_is_true_for_a_seeded_vault(vault: Path) -> None:
    """The marker directory is what makes a vault "available".

    Args:
        vault: A seeded vault.
    """
    assert vault_available(vault) is True


def test_vault_available_is_false_for_a_bare_directory(bare: Path) -> None:
    """No marker, no vault — and the probe creates nothing looking.

    Args:
        bare: A directory with no ``00-Creek-Meta``.
    """
    assert vault_available(bare) is False
    assert list(bare.iterdir()) == []


def test_vault_available_is_false_for_a_missing_directory(tmp_path: Path) -> None:
    """A vault path that does not exist at all is simply unavailable.

    Args:
        tmp_path: A directory whose child is never created.
    """
    assert vault_available(tmp_path / "absent") is False


def test_vault_available_rejects_a_marker_that_is_a_file(tmp_path: Path) -> None:
    """``00-Creek-Meta`` as a *file* is not an initialised vault.

    Args:
        tmp_path: A scratch directory.
    """
    (tmp_path / CREEK_MARKER).write_text("not a directory", encoding="utf-8")
    assert vault_available(tmp_path) is False


def test_the_marker_is_the_literal_the_scaffolder_writes() -> None:
    """The probe and ``creek init`` have to agree on one directory name.

    The private ``_CREEK_MARKER`` alias this test used to also assert on is
    gone with #1148: its only reference in the tree was that assertion, so the
    name was kept alive by the test written to justify it. The literal pin is
    the half that was ever load-bearing, and it stays.
    """
    assert Path("00-Creek-Meta") == CREEK_MARKER


# --------------------------------------------------------------------------- #
# "Always 200" is a promise about the server, not about a malformed request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", [None, "open", "personal"])
def test_capabilities_answers_200_for_every_admissible_ceiling(
    vault: Path, ceiling: str | None
) -> None:
    """The negotiation endpoint answers for every ceiling a caller may declare.

    This is the half of the ADR's "always 200" promise that is load-bearing:
    across the whole admissible ceiling vocabulary — including an *absent*
    header, which fails closed to ``open`` — negotiation succeeds. Nothing about
    the caller's declared ceiling can stop a client from reading the server's
    contract version.

    Args:
        vault: A seeded vault.
        ceiling: The declared ceiling, or ``None`` to send no header at all.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers(ceiling=ceiling))
    assert response.status_code == _OK_STATUS
    assert envelope(response)["contract_version"] == CONTRACT_VERSION


def test_the_ceiling_gate_has_no_per_route_exemption(vault: Path) -> None:
    """``/v1/capabilities`` is refused an inadmissible ceiling like every route.

    **This test exists to stop a well-meant "fix".** The ADR's "always HTTP 200"
    sentence reads, on its own, as though the negotiation endpoint should be
    exempt from the ceiling gate the way it is exempt from the contract-version
    gate. It is not, and the two axes are not analogous:

    * a contract version is compiled into a client and cannot be changed to get
      an answer, which is exactly why that axis needs an exemption;
    * a ceiling is a free per-request choice, and omitting the header always
      works — so the caller is one step from a ``200`` and learns why from a
      distinct ``invalid_request`` code rather than from silence.

    And the exemption would have to be a per-route flag on a *security* gate.
    #1075-#1077 mount handlers that really read the vault; a route that set such
    a flag by copy-paste would read it uncapped. A mis-set flag on the version
    gate costs a wrong error code. A mis-set flag on this one costs intimate
    content over the network.

    So the gate stays exemption-free, and this pins that ``/v1/capabilities`` —
    the single most tempting candidate for a carve-out — is inside it.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        refused = test_client.get(
            CAPABILITIES_PATH, headers=headers(ceiling="intimate")
        )
    assert refused.status_code == _INVALID_REQUEST_STATUS
    body = envelope(refused)
    assert body["code"] == ErrorCode.INVALID_REQUEST.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]
    # Nothing about the server leaks through the refusal: no version, no
    # capability list, no vault state.
    assert set(body) == {"code", "message", "request_id"}


def test_the_published_openapi_documents_the_capabilities_422() -> None:
    """The generated document agrees with the behaviour above.

    The four places a consumer or maintainer reads this rule — the ADR prose,
    ``docs/api.md``, ``handle_capabilities``' own docstring and the generated
    OpenAPI — have to say the same thing, or the client gets built against
    whichever one its author happened to open. The ``422`` on
    ``getCapabilities`` is deliberate, not a generator artefact.

    Takes no vault fixture: :func:`~creek_mcp.api.openapi.build_openapi` reads
    the models and the route table and never touches one. That is the point of
    generating the document from Pydantic rather than from a running app.
    """
    documented = build_openapi()["paths"][CAPABILITIES_PATH]["get"]["responses"]
    assert str(_INVALID_REQUEST_STATUS) in documented
    assert str(_OK_STATUS) in documented
