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
client cannot detect until it has already written the integration. That makes
this module the thing that turns #1075/#1076/#1077 red until each wires a real
handler — and the committed ``examples/capabilities/success.json`` fixture,
which documents the *completed* steady state with all four, is pinned
separately so the divergence is recorded rather than latent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
import yaml

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
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.tools import handshake as handshake_module
from creek_mcp.tools.handshake import CREEK_MARKER, vault_available
from tests.v1_api_support import (
    CAPABILITIES_PATH,
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
    """At #1074 that is exactly ``["capabilities"]``.

    Args:
        vault: A seeded vault.
    """
    assert _get(vault)["capabilities"] == ["capabilities"]


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

    Args:
        vault: A seeded vault.
    """
    body = _get(vault, minor="0.1")
    parsed = CapabilitiesResponse.model_validate(body)
    assert parsed.status is CapabilitiesStatus.INCOMPATIBLE
    assert parsed.capabilities == []
    assert parsed.contract_version == CONTRACT_VERSION
    assert parsed.ontology_version == ONTOLOGY_VERSION


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
        monkeypatch: Replaces ``load_config`` with a raiser.
        error: The exception ``load_config`` raises.
    """

    def _raise() -> None:
        raise error

    monkeypatch.setattr(capabilities_module, "load_config", _raise)
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


@pytest.mark.parametrize(
    "capability", _UNIMPLEMENTED_CAPABILITIES, ids=lambda c: str(c.value)
)
def test_every_unimplemented_capability_is_mounted_to_the_stub(
    vault: Path, capability: Capability
) -> None:
    """The converse. An unbuilt capability answers ``501``, honestly stubbed.

    This is what turns #1075/#1076/#1077 red: wiring a real handler without
    adding the capability to ``IMPLEMENTED_CAPABILITIES`` fails here, and
    adding it to the constant without wiring a handler fails the test above.

    Args:
        vault: A seeded vault.
        capability: The unbuilt capability under test.
    """
    spec = next(entry for entry in ROUTES if entry.capability is capability)
    app = build_app(vault_path=vault)
    endpoint = route_for(app, spec.path, spec.method).endpoint
    assert getattr(endpoint, "unimplemented_capability", None) is capability


def test_the_unimplemented_factory_marks_its_product() -> None:
    """The marker check above is not vacuous.

    ``getattr(endpoint, ..., None) is None`` is satisfied by *any* object that
    has never heard of the attribute, so the negative half of the pair would
    stay green if the factory stopped stamping. Feed the factory a capability
    and pin that the stamp appears.
    """
    stub = handlers_module.unimplemented(Capability.WHEEL)
    assert getattr(stub, "unimplemented_capability", None) is Capability.WHEEL


def test_there_is_at_least_one_unimplemented_capability() -> None:
    """The stub parametrize above has cases to run.

    When #1075—#1077 land this becomes an empty set and this test goes red on
    purpose: at that point the honesty machinery has done its job and the
    parametrized converse has nothing left to guard.
    """
    assert _UNIMPLEMENTED_CAPABILITIES


def test_capabilities_fixture_documents_the_completed_steady_state() -> None:
    """The committed fixture shows all four capabilities. That is deliberate.

    ``docs/contracts/adepthood-v1/examples/capabilities/success.json`` is the
    shape a consumer vendors and codes against once the epic is finished — the
    *completed* steady state, not today's partial one. It therefore diverges
    from the live response asserted by
    ``test_advertised_capabilities_equal_implemented_capabilities`` for as long
    as #1075—#1077 are open.

    Recording the divergence in a test is the point: an undocumented gap
    between a published fixture and a running server is exactly the kind of
    drift the bundle exists to prevent, and "we know, here is why" is a
    different thing from "nobody noticed".
    """
    payload = json.loads(SUCCESS_FIXTURE.read_text(encoding="utf-8"))
    assert payload["capabilities"] == [capability.value for capability in Capability]
    assert payload["status"] == CapabilitiesStatus.OK.value


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


def test_the_private_marker_alias_is_preserved() -> None:
    """``_CREEK_MARKER`` stays as an alias so nothing importing it breaks.

    Promoting the constant to a public name is an addition, not a rename.
    """
    assert handshake_module._CREEK_MARKER is CREEK_MARKER
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
