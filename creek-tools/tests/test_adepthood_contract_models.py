"""Contract-model guardrails for the Adepthood HTTP application API (#1072).

The ``/v1`` surface Adepthood consumes is *remote by construction*: every call
arrives over the network transport, and that transport is already capped at
``personal`` by ``creek_mcp.policy.REMOTE_ADMITTED_CEILINGS``.
:mod:`creek_mcp.api.models` is the wire vocabulary for that surface -- a
framework-free Pydantic layer that #1074 can mount behind whichever HTTP
framework it picks without renegotiating a single field name.

These tests *are* the contract, not a description of one. Twelve groups, each
pinning a property the cross-repo consumer is entitled to rely on:

1.  :data:`~creek_mcp.api.models.CONTRACT_MODELS` is total over the module's
    live ``_WireModel`` subclasses -- checked by reflection, since every
    fixture below is keyed off the registry and so cannot check it -- and
    every model in it accepts a canonical happy payload and forbids unknown
    keys;
2.  every enum is *closed* -- membership is frozen, and the two enums that
    mirror a runtime constant (``NoteKind`` against
    ``reflect._ALLOWED_KINDS``, ``WireTierCeiling`` against
    ``policy.REMOTE_ADMITTED_CEILINGS``) are compared to that constant rather
    than to a copy of it;
3.  the wheel declares ten separate required frequency fields, in canonical
    order, with bounded ``count`` / ``share``;
4.  :class:`~creek_mcp.api.models.ErrorEnvelope` echoes nothing -- three
    fields, constant messages, and a privacy refusal reason imported from
    :mod:`creek_mcp.read_gate` rather than restated;
5.  the retry table answers #1082: a privacy refusal is ``TERMINAL``;
6.  the committed fixture bundle round-trips against ``build_bundle()``, so it
    cannot rot away from the models it documents;
7.  the ADR's published version strings equal the runtime constants;
8.  ``routed_tier`` is a constant function of the caller's own declared
    ceiling and therefore carries no bits about the content's tier;
9.  ``essay_grounded`` is required and may not be ``True`` at contract 0.2;
10. ``intimate`` is structurally unreachable in every wire position;
11. the care envelope tracks :data:`creek.care.guardrail.CARE_SIGNAL`, and the
    reflection request enforces exactly-one-of ``content`` / ``entry_ref``;
12. the layering rules hold -- ``creek/`` never imports ``creek_mcp`` (#1032),
    and ``creek_mcp/api/`` imports no web framework.

Groups 6, 7 and 12 read committed repository files on purpose. That is the
anti-rot mechanism: a bundle or an ADR that has drifted from the code is
exactly the failure these tests exist to catch, and a test that regenerated
the artifact it checks would catch nothing.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from creek.care.guardrail import CARE_SIGNAL
from creek.generate.indexes import CANONICAL_FREQUENCY_NAMES
from creek.models import PrivacyTier
from creek_mcp import policy, read_gate
from creek_mcp.api import models as api_models
from creek_mcp.api.bundle import (
    BUNDLE_DIR_NAME,
    CAPABILITIES,
    EXAMPLE_STATES,
    UNREACHABLE_CELLS,
    build_bundle,
    write_bundle,
)
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    CONTRACT_MODELS,
    ERROR_MESSAGES,
    ERROR_STATUS,
    RETRY_POLICY,
    SUPPORTED_CONTRACT_MINORS,
    CapabilitiesResponse,
    CapabilitiesStatus,
    CareEscalationResponse,
    CareResource,
    CareSignal,
    DriveConnectorStatusResponse,
    DriveDisconnectResponse,
    DriveSyncResponse,
    ErrorCode,
    ErrorEnvelope,
    JournalAction,
    JournalUpsertRequest,
    JournalUpsertResponse,
    NotApplicableExample,
    NoteKind,
    ReflectionNote,
    ReflectionRequest,
    ReflectionResponse,
    ReflectionStatus,
    RetryDisposition,
    TierModel,
    UploadRequest,
    UploadResponse,
    VaultState,
    WheelFrequencies,
    WheelFrequency,
    WheelResponse,
    WireTierCeiling,
)
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.tier_ceiling import CEILING_ROUTING_TIER, routing_tier, tier_allowed
from creek_mcp.tools import reflect

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek_mcp.tier_ceiling import TierCeiling

# ---------------------------------------------------------------------------
# Repository anchors
# ---------------------------------------------------------------------------

# This file lives at ``<repo-root>/creek-tools/tests/``: parents[0] is
# ``tests``, parents[1] is ``creek-tools``, parents[2] is the repository root
# that owns ``docs/``.
REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOT = REPO_ROOT / "docs" / "contracts" / "adepthood-v1"
ADR_PATH = (
    REPO_ROOT / "docs" / "decisions" / "2026-07-31-adepthood-http-application-api.md"
)
DECISIONS_INDEX = REPO_ROOT / "docs" / "decisions.md"

CREEK_PACKAGE = REPO_ROOT / "creek-tools" / "creek"
API_PACKAGE = REPO_ROOT / "creek-tools" / "creek_mcp" / "api"

# ---------------------------------------------------------------------------
# Canonical happy-path payloads, reused by every later group
# ---------------------------------------------------------------------------

FREQUENCY_KEYS: tuple[str, ...] = tuple(f.value for f in CANONICAL_FREQUENCY_NAMES)

ERROR_ENVELOPE_PAYLOAD: dict[str, Any] = {
    "code": "not_found",
    # Drawn from the table, never restated: the whole no-echo invariant is that
    # a message is a function of the code alone, so a fixture carrying its own
    # prose would model the thing the contract forbids.
    "message": ERROR_MESSAGES[ErrorCode.NOT_FOUND],
    "request_id": "01J0000000000000000000REQ",
}

TIER_MODEL_PAYLOAD: dict[str, Any] = {
    "ceilings": ["open", "personal"],
    "default": "open",
    "intimate_never_egresses": True,
}

VAULT_STATE_PAYLOAD: dict[str, Any] = {"available": True}

CAPABILITIES_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "contract_version": CONTRACT_VERSION,
    "contract_minor": CONTRACT_MINOR,
    "supported_contract_minors": list(SUPPORTED_CONTRACT_MINORS),
    "ontology_version": ONTOLOGY_VERSION,
    "vault": VAULT_STATE_PAYLOAD,
    "tier_model": TIER_MODEL_PAYLOAD,
    "capabilities": [
        "capabilities",
        "journal-upsert",
        "reflections",
        "wheel",
        "upload",
    ],
}

JOURNAL_UPSERT_REQUEST_PAYLOAD: dict[str, Any] = {
    "content": "Woke before the alarm and walked the ridge until the fog lifted.",
    "timestamp": "2026-07-31T06:12:00Z",
    "tier": "personal",
}

JOURNAL_UPSERT_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "personal",
    "external_id": "adepthood:entry:2026-07-31T06:12:00Z",
    "fragment_id": "frag-2026-07-31-ridge-fog",
    "action": "created",
    "tier": "personal",
}

UPLOAD_REQUEST_PAYLOAD: dict[str, Any] = {
    "filename": "Ridge Notes.md",
    # Derived, never a pasted literal: a hand-typed base64 blob is a fixture
    # nobody can read and nobody notices going stale.
    "content_base64": base64.b64encode(
        b"Woke before the alarm and walked the ridge.\n"
    ).decode("ascii"),
    "external_id": "adepthood:doc:2026-07-31T06:12:00Z",
    "timestamp": "2026-07-31T06:12:00Z",
    "tier": "personal",
}

UPLOAD_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "personal",
    "external_id": "adepthood:doc:2026-07-31T06:12:00Z",
    "fragment_id": "frag-2026-07-31-ridge-notes",
    "affected_fragment_ids": ["frag-2026-07-31-ridge-notes"],
    "action": "created",
    "source_type": "markdown",
}

DRIVE_STATUS_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "open",
    "connection": "connected",
    "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    "can_sync": True,
}

DRIVE_SYNC_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "open",
    "files_fetched": 3,
    "files_unchanged": 11,
    "files_failed": 0,
    "files_unsupported": 1,
    "fragments_failed": 0,
    "fragments_created": 2,
    "fragments_updated": 1,
    "fragments_unchanged": 0,
}

DRIVE_DISCONNECT_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "open",
    "connection": "not_connected",
    "remote_revoked": True,
}

REFLECTION_REQUEST_PAYLOAD: dict[str, Any] = {
    "content": "I keep saying yes to things I do not want.",
    "max_notes": 3,
}

REFLECTION_NOTE_PAYLOAD: dict[str, Any] = {
    "quote": "I keep saying yes",
    "kind": "pattern",
    "note": "The same yes turns up whenever the room goes quiet.",
}

CARE_RESOURCE_PAYLOAD: dict[str, Any] = {
    "name": "988 Suicide & Crisis Lifeline (US)",
    "contact": "Call or text 988",
}

REFLECTION_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "personal",
    "routed_tier": "personal",
    "notes": [REFLECTION_NOTE_PAYLOAD],
    "essay": None,
    "essay_grounded": False,
}

CARE_ESCALATION_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "escalate",
    "tier_ceiling": "personal",
    "reason": "acute_distress",
    "care_signal": CARE_SIGNAL,
}

WHEEL_FREQUENCY_PAYLOAD: dict[str, Any] = {
    "name": "Agency",
    "count": 4,
    "share": 0.4,
}

WHEEL_FREQUENCIES_PAYLOAD: dict[str, Any] = {
    frequency.value: {"name": name, "count": 1, "share": 0.1}
    for frequency, name in CANONICAL_FREQUENCY_NAMES.items()
}

EMPTY_WHEEL_FREQUENCIES_PAYLOAD: dict[str, Any] = {
    frequency.value: {"name": name, "count": 0, "share": 0.0}
    for frequency, name in CANONICAL_FREQUENCY_NAMES.items()
}

WHEEL_RESPONSE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "tier_ceiling": "open",
    "total_classified": 10,
    "unclassified": 0,
    "wheel": WHEEL_FREQUENCIES_PAYLOAD,
}

NOT_APPLICABLE_EXAMPLE_PAYLOAD: dict[str, Any] = {
    "unreachable": True,
    "reason": (
        "the acute-distress guard runs only inside reflect_tool, so this "
        "capability has no care-escalation response shape"
    ),
}

# Keyed by ``__name__`` rather than by a literal, so the table cannot drift
# from the classes it describes and every imported model is genuinely used.
HAPPY_PAYLOADS: dict[str, dict[str, Any]] = {
    CapabilitiesResponse.__name__: CAPABILITIES_RESPONSE_PAYLOAD,
    CareEscalationResponse.__name__: CARE_ESCALATION_RESPONSE_PAYLOAD,
    CareResource.__name__: CARE_RESOURCE_PAYLOAD,
    CareSignal.__name__: CARE_SIGNAL,
    DriveConnectorStatusResponse.__name__: DRIVE_STATUS_RESPONSE_PAYLOAD,
    DriveDisconnectResponse.__name__: DRIVE_DISCONNECT_RESPONSE_PAYLOAD,
    DriveSyncResponse.__name__: DRIVE_SYNC_RESPONSE_PAYLOAD,
    ErrorEnvelope.__name__: ERROR_ENVELOPE_PAYLOAD,
    JournalUpsertRequest.__name__: JOURNAL_UPSERT_REQUEST_PAYLOAD,
    JournalUpsertResponse.__name__: JOURNAL_UPSERT_RESPONSE_PAYLOAD,
    NotApplicableExample.__name__: NOT_APPLICABLE_EXAMPLE_PAYLOAD,
    ReflectionNote.__name__: REFLECTION_NOTE_PAYLOAD,
    ReflectionRequest.__name__: REFLECTION_REQUEST_PAYLOAD,
    ReflectionResponse.__name__: REFLECTION_RESPONSE_PAYLOAD,
    TierModel.__name__: TIER_MODEL_PAYLOAD,
    UploadRequest.__name__: UPLOAD_REQUEST_PAYLOAD,
    UploadResponse.__name__: UPLOAD_RESPONSE_PAYLOAD,
    VaultState.__name__: VAULT_STATE_PAYLOAD,
    WheelFrequencies.__name__: WHEEL_FREQUENCIES_PAYLOAD,
    WheelFrequency.__name__: WHEEL_FREQUENCY_PAYLOAD,
    WheelResponse.__name__: WHEEL_RESPONSE_PAYLOAD,
}

MODEL_NAMES: tuple[str, ...] = tuple(sorted(HAPPY_PAYLOADS))

# ---------------------------------------------------------------------------
# Frozen enum / table expectations
# ---------------------------------------------------------------------------

EXPECTED_ERROR_CODES: frozenset[str] = frozenset(
    {
        "unauthenticated",
        "invalid_request",
        "incompatible_version",
        "privacy_refused",
        "not_found",
        "unsupported_capability",
        "unsupported_source",
        "unavailable",
        "temporarily_unavailable",
        "internal_error",
    }
)

EXPECTED_ERROR_STATUS: tuple[tuple[str, int], ...] = (
    ("unauthenticated", 401),
    ("invalid_request", 422),
    ("incompatible_version", 409),
    ("privacy_refused", 403),
    ("not_found", 404),
    ("unsupported_capability", 501),
    ("unsupported_source", 415),
    ("unavailable", 503),
    ("temporarily_unavailable", 503),
    ("internal_error", 500),
)

ALLOWED_HTTP_STATUSES: frozenset[int] = frozenset(
    {401, 403, 404, 409, 415, 422, 500, 501, 503}
)

EXPECTED_RETRY_POLICY: tuple[tuple[str, str], ...] = (
    ("unauthenticated", "terminal"),
    ("invalid_request", "terminal"),
    ("incompatible_version", "terminal"),
    ("privacy_refused", "terminal"),
    ("not_found", "terminal"),
    ("unsupported_capability", "terminal"),
    ("unsupported_source", "terminal"),
    ("unavailable", "retry_after_operator_action"),
    ("temporarily_unavailable", "retry_with_backoff"),
    ("internal_error", "retry_with_backoff"),
)

ERROR_TABLES: Mapping[str, Mapping[ErrorCode, object]] = {
    "ERROR_STATUS": ERROR_STATUS,
    "ERROR_MESSAGES": ERROR_MESSAGES,
    "RETRY_POLICY": RETRY_POLICY,
}

# Request-shaped keys a debug-minded implementer is most likely to bolt onto
# an error response. Every one of them would echo caller material -- or, worse,
# resolved vault material -- back out over the wire.
ECHO_KEYS: tuple[str, ...] = (
    "input",
    "detail",
    "received",
    "body",
    "content",
    "entry_ref",
    "tier",
)

EXPECTED_UNREACHABLE_CELLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("capabilities", "care-escalation"),
        ("journal-upsert", "care-escalation"),
        ("wheel", "care-escalation"),
        ("upload", "care-escalation"),
        ("drive-connector", "care-escalation"),
    }
)

FORBIDDEN_API_IMPORTS: frozenset[str] = frozenset(
    {"fastapi", "starlette", "uvicorn", "httpx"}
)

CREEK_SOURCES: tuple[str, ...] = tuple(
    path.relative_to(REPO_ROOT).as_posix()
    for path in sorted(CREEK_PACKAGE.rglob("*.py"))
)

ADR_CONTRACT_VERSION_PATTERN = r"- \*\*Contract version\*\*: `(?P<v>[^`]+)`"
ADR_ONTOLOGY_VERSION_PATTERN = r"- \*\*Ontology version\*\*: `(?P<v>[^`]+)`"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matrix() -> tuple[tuple[str, str], ...]:
    """Return every ``(capability, state)`` cell of the fixture matrix."""
    return tuple(
        (capability, state) for capability in CAPABILITIES for state in EXAMPLE_STATES
    )


def _example_path(capability: str, state: str) -> Path:
    """Return the committed fixture path for one matrix cell."""
    return BUNDLE_ROOT / "examples" / capability / f"{state}.json"


def _load_manifest() -> dict[str, Any]:
    """Return the committed bundle manifest, parsed."""
    parsed: dict[str, Any] = json.loads(
        (BUNDLE_ROOT / "manifest.json").read_text(encoding="utf-8"),
    )
    return parsed


def _manifest_entries() -> list[dict[str, Any]]:
    """Return every file entry recorded in the bundle manifest."""
    entries: list[dict[str, Any]] = _load_manifest()["files"]
    return entries


def _example_entries() -> list[dict[str, Any]]:
    """Return the manifest entries that describe example fixtures."""
    return [entry for entry in _manifest_entries() if entry["capability"] is not None]


def _files_on_disk() -> dict[str, str]:
    """Return every committed bundle file as ``relative posix path -> text``."""
    return {
        path.relative_to(BUNDLE_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(BUNDLE_ROOT.rglob("*"))
        if path.is_file()
    }


def _imported_roots(source: str) -> set[str]:
    """Return the top-level module names *source* imports, by AST alone."""
    tree = ast.parse(source)
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    return roots


def _api_sources() -> list[Path]:
    """Return every Python source under ``creek_mcp/api/``, sorted."""
    if not API_PACKAGE.is_dir():
        return []
    return sorted(API_PACKAGE.rglob("*.py"))


def _wire_model_subclasses(
    namespace: Mapping[str, object],
) -> dict[str, type[BaseModel]]:
    """Return every published wire model in *namespace*, keyed by class name.

    Discovery walks the live objects in the namespace rather than
    :data:`CONTRACT_MODELS`, which is the whole point: every other fixture in
    this module (``HAPPY_PAYLOADS``, ``MODEL_NAMES``) is keyed off the registry,
    so the registry cannot be checked for completeness against itself.

    The base class is excluded by identity, not by a name convention, and no
    module filter is applied -- a wire model defined elsewhere and merely
    imported into :mod:`creek_mcp.api.models` still has to be registered.
    """
    base = api_models._WireModel
    return {
        obj.__name__: obj
        for obj in namespace.values()
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base
    }


def _admitted_ceiling_tier_pairs() -> tuple[tuple[TierCeiling, PrivacyTier], ...]:
    """Return every ``(remote ceiling, admitted tier)`` pair ``/v1`` can see."""
    return tuple(
        (ceiling, tier)
        for ceiling in sorted(policy.REMOTE_ADMITTED_CEILINGS)
        for tier in PrivacyTier
        if tier_allowed(tier, ceiling)
    )


# ---------------------------------------------------------------------------
# Group 1 -- happy path
# ---------------------------------------------------------------------------


def test_every_wire_model_subclass_is_registered_in_contract_models() -> None:
    """``CONTRACT_MODELS`` is total over the module's live ``_WireModel`` tree.

    A model defined in :mod:`creek_mcp.api.models` but forgotten in the registry
    would be invisible to every other test here -- they key their fixtures off
    ``CONTRACT_MODELS`` itself -- and, because :mod:`creek_mcp.api.bundle` walks
    the same mapping to emit ``schemas/<name>.schema.json``, it would also be
    absent from the published bundle without a single check going red. This is
    the one assertion that reads the module's class list instead of the
    registry, and it is an equality so it catches the reverse too: a registry
    entry whose class no longer lives in the module.
    """
    assert _wire_model_subclasses(vars(api_models)) == CONTRACT_MODELS


def test_wire_model_registry_check_catches_an_unregistered_subclass() -> None:
    """The completeness check above is not vacuous.

    Reflection-based guards fail open when the discovery predicate stops
    matching -- the sweep finds nothing, the equality holds, and the test stays
    green while the invariant rots. So this pins the negative case: a
    ``_WireModel`` subclass that nobody added to ``CONTRACT_MODELS`` is
    discovered, and the equality that
    ``test_every_wire_model_subclass_is_registered_in_contract_models`` asserts
    is exactly what breaks.
    """

    class ForgottenModel(api_models._WireModel):
        """A wire model whose author forgot the ``CONTRACT_MODELS`` entry."""

        value: str = Field(description="Filler field; the omission is the point.")

    namespace = {**vars(api_models), ForgottenModel.__name__: ForgottenModel}
    discovered = _wire_model_subclasses(namespace)

    assert discovered == {**CONTRACT_MODELS, ForgottenModel.__name__: ForgottenModel}
    assert discovered != CONTRACT_MODELS


def test_happy_payloads_cover_every_contract_model() -> None:
    """The happy-path payload table is total over ``CONTRACT_MODELS``."""
    assert set(HAPPY_PAYLOADS) == set(CONTRACT_MODELS)


def test_contract_models_are_keyed_by_class_name() -> None:
    """``CONTRACT_MODELS`` keys are the class names the manifest references."""
    actual = {name: cls.__name__ for name, cls in CONTRACT_MODELS.items()}
    assert actual == {name: name for name in CONTRACT_MODELS}


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_contract_model_is_a_pydantic_model(model_name: str) -> None:
    """Every contract entry is a Pydantic ``BaseModel`` subclass."""
    assert issubclass(CONTRACT_MODELS[model_name], BaseModel)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_contract_model_validates_its_happy_payload(model_name: str) -> None:
    """Each contract model accepts its canonical well-formed payload."""
    model = CONTRACT_MODELS[model_name]
    instance = model.model_validate(HAPPY_PAYLOADS[model_name])
    assert isinstance(instance, model)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_contract_model_forbids_extra_fields(model_name: str) -> None:
    """Every contract model is closed: ``extra="forbid"``, no passthrough."""
    assert CONTRACT_MODELS[model_name].model_config.get("extra") == "forbid"


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_contract_model_rejects_an_unknown_key(model_name: str) -> None:
    """An unknown key is a hard validation error, not a tolerated addition."""
    payload = {**HAPPY_PAYLOADS[model_name], "smuggled_field": "surprise"}
    with pytest.raises(ValidationError):
        CONTRACT_MODELS[model_name].model_validate(payload)


# ---------------------------------------------------------------------------
# Group 2 -- closed enums
# ---------------------------------------------------------------------------


def test_error_code_membership_is_frozen() -> None:
    """``ErrorCode`` carries exactly the ten agreed wire codes.

    Nine since contract 0.2; ``unsupported_source`` joined at 0.8 (#1524),
    which is also what brought ``415`` into the published status set.
    """
    assert {code.value for code in ErrorCode} == EXPECTED_ERROR_CODES
    assert len(ErrorCode) == 10


def test_error_code_has_no_care_escalation_member() -> None:
    """Care escalation is a 200-shaped success, so it is never an error code."""
    assert "care_escalation" not in {code.value for code in ErrorCode}


def test_note_kind_mirrors_the_reflect_tool_allowed_kinds() -> None:
    """``NoteKind`` tracks ``reflect._ALLOWED_KINDS`` instead of copying it."""
    assert set(NoteKind) == {NoteKind(kind) for kind in reflect._ALLOWED_KINDS}
    assert {kind.value for kind in NoteKind} == reflect._ALLOWED_KINDS
    assert len(NoteKind) == 7


def test_wire_tier_ceiling_matches_the_remote_admitted_ceilings() -> None:
    """``/v1`` is remote by construction, so its ceilings are the remote cap."""
    remote = {ceiling.value for ceiling in policy.REMOTE_ADMITTED_CEILINGS}
    assert {ceiling.value for ceiling in WireTierCeiling} == remote


@pytest.mark.parametrize("value", ["intimate", "all"])
def test_wire_tier_ceiling_rejects_non_remote_values(value: str) -> None:
    """``intimate`` / ``all`` are not constructible on the wire at all."""
    with pytest.raises(ValueError, match=value):
        WireTierCeiling(value)


@pytest.mark.parametrize(
    ("enum_cls", "expected_values"),
    [
        (WireTierCeiling, {"open", "personal"}),
        (CapabilitiesStatus, {"ok", "uninitialized", "incompatible"}),
        (JournalAction, {"created", "updated", "unchanged"}),
        (ReflectionStatus, {"ok", "empty", "escalate"}),
        (
            RetryDisposition,
            {"terminal", "retry_after_operator_action", "retry_with_backoff"},
        ),
    ],
    ids=[
        "WireTierCeiling",
        "CapabilitiesStatus",
        "JournalAction",
        "ReflectionStatus",
        "RetryDisposition",
    ],
)
def test_enum_membership_is_pinned(
    enum_cls: type[StrEnum], expected_values: set[str]
) -> None:
    """Each wire enum is a closed ``StrEnum`` with exactly the agreed members."""
    assert issubclass(enum_cls, StrEnum)
    assert {member.value for member in enum_cls} == expected_values


# ---------------------------------------------------------------------------
# Group 3 -- the wheel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", FREQUENCY_KEYS)
def test_wheel_frequencies_requires_every_frequency(key: str) -> None:
    """All ten frequencies are required; a missing one is a validation error."""
    payload = {k: v for k, v in WHEEL_FREQUENCIES_PAYLOAD.items() if k != key}
    with pytest.raises(ValidationError):
        WheelFrequencies.model_validate(payload)


def test_wheel_frequencies_field_order_is_canonical() -> None:
    """Ten declared fields in canonical order, so the JSON Schema is stable."""
    canonical = [frequency.value for frequency in CANONICAL_FREQUENCY_NAMES]
    assert list(WheelFrequencies.model_fields) == canonical
    assert canonical == list(FREQUENCY_KEYS)


def test_wheel_frequencies_rejects_an_unknown_frequency_key() -> None:
    """An eleventh frequency is a contract change, not an extensible field."""
    payload = {**WHEEL_FREQUENCIES_PAYLOAD, "F11": WHEEL_FREQUENCY_PAYLOAD}
    with pytest.raises(ValidationError):
        WheelFrequencies.model_validate(payload)


def test_wheel_frequencies_round_trip_preserves_key_order() -> None:
    """``model_dump()`` re-emits the frequencies in canonical order."""
    dumped = WheelFrequencies.model_validate(WHEEL_FREQUENCIES_PAYLOAD).model_dump()
    assert list(dumped) == list(FREQUENCY_KEYS)


@pytest.mark.parametrize("share", [1.5, -0.1])
def test_wheel_frequency_rejects_out_of_range_share(share: float) -> None:
    """``share`` is a proportion bounded to ``[0.0, 1.0]``."""
    with pytest.raises(ValidationError):
        WheelFrequency.model_validate({**WHEEL_FREQUENCY_PAYLOAD, "share": share})


def test_wheel_frequency_rejects_a_negative_count() -> None:
    """``count`` is a tally, so it can never be negative."""
    with pytest.raises(ValidationError):
        WheelFrequency.model_validate({**WHEEL_FREQUENCY_PAYLOAD, "count": -1})


def test_empty_corpus_wheel_validates_with_zero_totals() -> None:
    """A freshly-initialised vault is an all-zero wheel, not an error."""
    response = WheelResponse.model_validate(
        {
            **WHEEL_RESPONSE_PAYLOAD,
            "total_classified": 0,
            "unclassified": 0,
            "wheel": EMPTY_WHEEL_FREQUENCIES_PAYLOAD,
        }
    )
    assert response.total_classified == 0
    assert response.unclassified == 0
    dumped = response.wheel.model_dump()
    assert [dumped[key]["count"] for key in FREQUENCY_KEYS] == [0] * 10
    assert [dumped[key]["share"] for key in FREQUENCY_KEYS] == [0.0] * 10


@pytest.mark.parametrize("field_name", ["total_classified", "unclassified"])
def test_wheel_response_rejects_negative_totals(field_name: str) -> None:
    """Both wheel totals are counts and are bounded below by zero."""
    with pytest.raises(ValidationError):
        WheelResponse.model_validate({**WHEEL_RESPONSE_PAYLOAD, field_name: -1})


# ---------------------------------------------------------------------------
# Group 4 -- the error envelope echoes nothing
# ---------------------------------------------------------------------------


def test_error_envelope_has_exactly_three_fields() -> None:
    """The envelope is code + message + request_id, and nothing else, ever."""
    assert set(ErrorEnvelope.model_fields) == {"code", "message", "request_id"}


@pytest.mark.parametrize("echo_key", ECHO_KEYS)
def test_error_envelope_refuses_to_echo_request_material(echo_key: str) -> None:
    """A debug key that echoes caller or vault material is a validation error."""
    payload = {**ERROR_ENVELOPE_PAYLOAD, echo_key: "would-have-been-echoed"}
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate(payload)


@pytest.mark.parametrize("table_name", sorted(ERROR_TABLES))
def test_error_table_is_total_over_error_code(table_name: str) -> None:
    """Every error table answers for every code -- no ``KeyError`` at runtime."""
    assert set(ERROR_TABLES[table_name]) == set(ErrorCode)


@pytest.mark.parametrize(("code_value", "status"), EXPECTED_ERROR_STATUS)
def test_error_status_is_pinned(code_value: str, status: int) -> None:
    """Each error code maps to its agreed HTTP status."""
    assert ERROR_STATUS[ErrorCode(code_value)] == status


def test_every_error_status_is_an_agreed_http_code() -> None:
    """No error maps to a status outside the nine the contract publishes."""
    assert set(ERROR_STATUS.values()) <= ALLOWED_HTTP_STATUSES


def test_privacy_refused_message_is_the_read_gate_constant() -> None:
    """The refusal reason is imported from ``read_gate``, never restated.

    A copied string drifts, and a drifted refusal reason is how a refusal
    starts ranking the content the caller was not admitted to.
    """
    assert (
        ERROR_MESSAGES[ErrorCode.PRIVACY_REFUSED]
        == read_gate.GENERIC_ABOVE_CEILING_REASON
    )


@pytest.mark.parametrize("code_value", sorted(EXPECTED_ERROR_CODES))
def test_error_message_is_a_constant_string(code_value: str) -> None:
    """Messages are constants: non-empty, carrying no format placeholder."""
    message = ERROR_MESSAGES[ErrorCode(code_value)]
    assert message
    assert "{" not in message
    assert "%s" not in message


# ---------------------------------------------------------------------------
# Group 5 -- the #1082 retry policy
# ---------------------------------------------------------------------------


def test_privacy_refused_is_terminal() -> None:
    """A privacy refusal is ``TERMINAL`` -- the contract's answer to #1082.

    ``creek.journal``'s idempotent update-in-place refuses on the *existing*
    fragment's vault tier, and a remote consumer token is capped at
    ``ceiling=personal``. So once an ``external_id`` resolves to an
    intimate-escalated (or purged, or orphaned) fragment, that id is
    permanently un-sendable by any remote consumer: no amount of waiting, and
    no operator action a remote client can trigger, changes the answer.
    Marking it ``TERMINAL`` is what stops an Adepthood client looping forever
    on an entry it can never deliver. If a content-hash carve-out for the
    unchanged-resend case ever lands, that is a contract change with a minor
    bump -- not a quiet relabelling here.
    """
    assert RETRY_POLICY[ErrorCode.PRIVACY_REFUSED] is RetryDisposition.TERMINAL


def test_temporarily_unavailable_is_retryable_with_backoff() -> None:
    """Terminal-vs-transient is decided by the code alone, not by prose."""
    assert (
        RETRY_POLICY[ErrorCode.TEMPORARILY_UNAVAILABLE]
        is RetryDisposition.RETRY_WITH_BACKOFF
    )


def test_unavailable_waits_on_the_operator() -> None:
    """A missing vault needs a human, so backoff alone will never clear it."""
    assert (
        RETRY_POLICY[ErrorCode.UNAVAILABLE]
        is RetryDisposition.RETRY_AFTER_OPERATOR_ACTION
    )


@pytest.mark.parametrize(("code_value", "disposition_value"), EXPECTED_RETRY_POLICY)
def test_retry_policy_is_pinned(code_value: str, disposition_value: str) -> None:
    """Each error code carries its agreed retry disposition."""
    assert RETRY_POLICY[ErrorCode(code_value)] is RetryDisposition(disposition_value)


# ---------------------------------------------------------------------------
# Group 6 -- the fixture bundle round-trips
# ---------------------------------------------------------------------------


def test_bundle_root_exists() -> None:
    """The committed fixture bundle is present under ``docs/contracts/``."""
    assert BUNDLE_ROOT.is_dir()


def test_bundle_root_name_matches_the_declared_dir_name() -> None:
    """``BUNDLE_DIR_NAME`` names the directory that is actually committed."""
    assert BUNDLE_ROOT.name == BUNDLE_DIR_NAME


def test_capability_and_state_axes_are_pinned() -> None:
    """The fixture matrix is 6 capabilities x 7 states = 42 cells."""
    assert CAPABILITIES == (
        "capabilities",
        "journal-upsert",
        "reflections",
        "wheel",
        "upload",
        "drive-connector",
    )
    assert EXAMPLE_STATES == (
        "success",
        "empty",
        "refusal",
        "care-escalation",
        "malformed-input",
        "incompatible-version",
        "unavailable-service",
    )
    assert len(_matrix()) == 42


@pytest.mark.parametrize(("capability", "state"), _matrix())
def test_every_matrix_cell_has_exactly_one_example(capability: str, state: str) -> None:
    """Every ``(capability, state)`` cell is documented by one fixture file."""
    assert _example_path(capability, state).is_file()


def test_bundle_has_no_extra_example_files() -> None:
    """The examples tree holds the 28 matrix cells and nothing else."""
    examples_root = BUNDLE_ROOT / "examples"
    found = {
        path.relative_to(examples_root).as_posix()
        for path in examples_root.rglob("*")
        if path.is_file()
    }
    assert found == {f"{capability}/{state}.json" for capability, state in _matrix()}


def test_manifest_pins_the_runtime_versions() -> None:
    """The manifest publishes the runtime contract and ontology versions."""
    manifest = _load_manifest()
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["ontology_version"] == ONTOLOGY_VERSION
    assert manifest["bundle"] == BUNDLE_DIR_NAME


def test_manifest_entries_are_sorted_by_path() -> None:
    """Manifest ordering is deterministic so a diff shows real changes only."""
    paths = [entry["path"] for entry in _manifest_entries()]
    assert paths == sorted(paths)


def test_manifest_lists_every_generated_file_except_itself() -> None:
    """The manifest is total over the bundle it describes, minus itself."""
    listed = {entry["path"] for entry in _manifest_entries()}
    assert listed == set(build_bundle()) - {"manifest.json"}


def test_every_manifest_entry_hashes_to_the_committed_bytes() -> None:
    """Each recorded sha256 matches the file actually on disk."""
    for entry in _manifest_entries():
        path = BUNDLE_ROOT / entry["path"]
        assert path.is_file(), f"manifest names a missing file: {entry['path']}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"stale sha256 for {entry['path']}"


def test_every_example_validates_against_its_named_model() -> None:
    """Every fixture validates against *its own* named contract model."""
    for entry in _example_entries():
        text = (BUNDLE_ROOT / entry["path"]).read_text(encoding="utf-8")
        model = CONTRACT_MODELS[entry["model"]]
        assert isinstance(model.model_validate(json.loads(text)), model)


def test_schema_entries_carry_no_capability_or_state() -> None:
    """Schema entries are capability-agnostic, so both axes are null."""
    entries = [e for e in _manifest_entries() if e["path"].startswith("schemas/")]
    assert entries
    assert all(entry["capability"] is None for entry in entries)
    assert all(entry["state"] is None for entry in entries)


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_schema_file_matches_the_model_json_schema(model_name: str) -> None:
    """Each committed JSON Schema is the model's own, byte for byte."""
    expected = (
        json.dumps(
            CONTRACT_MODELS[model_name].model_json_schema(),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    path = BUNDLE_ROOT / "schemas" / f"{model_name}.schema.json"
    assert path.read_text(encoding="utf-8") == expected


def test_retry_policy_json_mirrors_the_runtime_table() -> None:
    """``retry-policy.json`` is the runtime table serialised, not a retelling."""
    text = (BUNDLE_ROOT / "retry-policy.json").read_text(encoding="utf-8")
    assert json.loads(text) == {
        code.value: RETRY_POLICY[code].value for code in ErrorCode
    }


def test_unreachable_cells_are_the_five_non_reflection_care_escalations() -> None:
    """Only ``reflections`` can escalate; the other five cells are N/A."""
    assert UNREACHABLE_CELLS == EXPECTED_UNREACHABLE_CELLS


def test_unreachable_cells_are_exactly_the_not_applicable_examples() -> None:
    """``NotApplicableExample`` marks the structurally unreachable cells only."""
    marked = {
        (entry["capability"], entry["state"])
        for entry in _example_entries()
        if entry["model"] == "NotApplicableExample"
    }
    assert marked == set(UNREACHABLE_CELLS)


@pytest.mark.parametrize(("capability", "state"), sorted(EXPECTED_UNREACHABLE_CELLS))
def test_unreachable_fixture_states_why(capability: str, state: str) -> None:
    """Each unreachable cell says *why* it is unreachable, not merely that."""
    payload = json.loads(_example_path(capability, state).read_text(encoding="utf-8"))
    example = NotApplicableExample.model_validate(payload)
    assert example.unreachable is True
    assert example.reason.strip()


def test_committed_bundle_equals_a_fresh_build() -> None:
    """The committed bundle is byte-identical to ``build_bundle()``.

    This is what makes the bundle un-rottable: change a model and the bundle
    goes red until it is regenerated, so a cross-repo consumer reading the
    fixtures is never reading last month's contract.
    """
    on_disk = _files_on_disk()
    generated = {path: text for path, text in on_disk.items() if path != "README.md"}
    assert generated == build_bundle()


def test_build_bundle_covers_every_committed_file_except_the_readme() -> None:
    """``README.md`` is docs-owned; every other bundle file is generated."""
    assert set(build_bundle()) == set(_files_on_disk()) - {"README.md"}


def test_every_error_fixture_message_comes_from_the_table() -> None:
    """No committed error fixture invents prose outside ``ERROR_MESSAGES``.

    This is the no-echo invariant expressed over the bytes a consumer actually
    vendors. ``ErrorEnvelope.message`` is typed ``str``, so the type system
    cannot stop a fixture -- or, later, a handler copying a fixture -- from
    carrying caller-derived text. Membership in the constant table is what
    proves the message is a function of the ``code`` alone.
    """
    allowed = set(ERROR_MESSAGES.values())
    offenders = [
        entry["path"]
        for entry in _example_entries()
        if entry["model"] == "ErrorEnvelope"
        and json.loads((BUNDLE_ROOT / entry["path"]).read_text(encoding="utf-8"))[
            "message"
        ]
        not in allowed
    ]
    assert offenders == []


def test_bundle_readme_is_present() -> None:
    """The docs-owned README exists.

    Every other bundle assertion *subtracts* ``README.md`` from the file set,
    so deleting it would otherwise be invisible to this suite -- and it is the
    only place the vendoring recipe, the "do not hand-edit generated files"
    warning and the key-ordering caveat live.
    """
    assert (BUNDLE_ROOT / "README.md").is_file()


def _string_values(payload: object) -> list[str]:
    """Return every string *value* in *payload*, at any depth, ignoring keys.

    Keys are excluded deliberately: ``intimate_never_egresses`` is a promise
    the contract makes *about* intimate content, and matching it would make
    the sweep below fire on exactly the fixtures that prove the promise.
    """
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [v for item in payload.values() for v in _string_values(item)]
    if isinstance(payload, list):
        return [v for item in payload for v in _string_values(item)]
    return []


def test_no_committed_fixture_carries_an_intimate_tier_value() -> None:
    """No example carries ``intimate`` as a *value*, at any depth.

    The model-level tests pin three typed fields, but a fixture is a hand-built
    payload and the security invariant is about the bytes a consumer vendors:
    no ``intimate``-tier success response may be published, and every INTIMATE
    example must be a refusal. This walks every string value of all 28
    committed examples rather than trusting the fields that happen to be typed.
    """
    offenders = [
        entry["path"]
        for entry in _example_entries()
        if any(
            "intimate" in value
            for value in _string_values(
                json.loads(
                    (BUNDLE_ROOT / entry["path"]).read_text(encoding="utf-8"),
                ),
            )
        )
    ]
    assert offenders == []


def test_write_bundle_materialises_build_bundle(tmp_path: Path) -> None:
    """``write_bundle`` writes exactly what ``build_bundle`` returns."""
    write_bundle(tmp_path)
    written = {
        path.relative_to(tmp_path).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert written == build_bundle()


# ---------------------------------------------------------------------------
# Group 7 -- the ADR is pinned to the code
# ---------------------------------------------------------------------------


def test_adr_exists() -> None:
    """The #1072 decision record is committed."""
    assert ADR_PATH.is_file()


def test_adr_publishes_the_runtime_contract_version() -> None:
    """The ADR's ``Contract version`` line equals ``CONTRACT_VERSION``."""
    text = ADR_PATH.read_text(encoding="utf-8")
    match = re.search(ADR_CONTRACT_VERSION_PATTERN, text)
    assert match is not None, "ADR has no '- **Contract version**: `...`' line"
    assert match.group("v") == CONTRACT_VERSION


def test_adr_publishes_the_runtime_ontology_version() -> None:
    """The ADR's ``Ontology version`` line equals ``ONTOLOGY_VERSION``."""
    text = ADR_PATH.read_text(encoding="utf-8")
    match = re.search(ADR_ONTOLOGY_VERSION_PATTERN, text)
    assert match is not None, "ADR has no '- **Ontology version**: `...`' line"
    assert match.group("v") == ONTOLOGY_VERSION


def test_contract_minor_is_derived_from_the_runtime_version() -> None:
    """``CONTRACT_MINOR`` is ``CONTRACT_VERSION``'s major.minor, not a literal.

    The exact-minor compatibility rule is the whole point of the version gate,
    so the minor a client negotiates against must be a function of the version
    the server actually speaks. Pinned here because several contract rules are
    explicitly scoped to 0.2 -- ``essay_grounded`` may only be ``False``, and
    ``NotApplicableExample`` marks three unreachable cells -- and each of those
    is owed a re-examination when ``CONTRACT_VERSION`` moves.
    """
    assert ".".join(CONTRACT_VERSION.split(".")[:2]) == CONTRACT_MINOR
    assert CONTRACT_MINOR in SUPPORTED_CONTRACT_MINORS


def test_adr_publishes_the_negotiable_contract_minor() -> None:
    """The ADR's own version block agrees with the negotiated minor."""
    text = ADR_PATH.read_text(encoding="utf-8")
    match = re.search(ADR_CONTRACT_VERSION_PATTERN, text)
    assert match is not None
    assert ".".join(match.group("v").split(".")[:2]) == CONTRACT_MINOR


def test_decisions_index_lists_the_adr() -> None:
    """The decisions index links the new ADR, so it is discoverable."""
    assert DECISIONS_INDEX.is_file()
    assert ADR_PATH.name in DECISIONS_INDEX.read_text(encoding="utf-8")


@pytest.mark.parametrize("token", ["#757", "1082", "1071", "1072"])
def test_adr_carries_its_required_cross_references(token: str) -> None:
    """The ADR names the confidential-hosting link and its sibling issues."""
    assert token in ADR_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Group 8 -- routed_tier is not an oracle (SECURITY-LOAD-BEARING)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("ceiling", "tier"), _admitted_ceiling_tier_pairs())
def test_routed_tier_is_a_constant_function_of_the_declared_ceiling(
    ceiling: TierCeiling, tier: PrivacyTier
) -> None:
    """``routed_tier`` carries zero bits about the content's own tier.

    For every ceiling ``/v1`` permits (``open`` and ``personal`` -- the remote
    cap), and every :class:`~creek.models.PrivacyTier` that ceiling admits, the
    routing tier is one and the same value: ``CEILING_ROUTING_TIER[ceiling]``.
    The caller already knows the ceiling -- they declared it -- so echoing
    ``routed_tier`` back to them discloses nothing they did not supply. That is
    exactly what makes exposing the field on
    :class:`~creek_mcp.api.models.ReflectionResponse` safe.

    The invariant holds because ``routing_tier`` takes the *more sensitive* of
    the ceiling-derived tier and the content tier, and an admitted tier is by
    definition no more sensitive than its ceiling. It would break the moment a
    ceiling admitted a tier ranking above it -- at which point ``routed_tier``
    becomes a one-bit tier oracle over the corpus.

    If this test ever fails, remove ``routed_tier`` from
    ``ReflectionResponse``. Do not relax the assertion.
    """
    assert routing_tier(ceiling, tier) is CEILING_ROUTING_TIER[ceiling]


def test_admitted_ceiling_tier_sweep_is_not_vacuous() -> None:
    """The non-oracle sweep covers both remote ceilings and several tiers."""
    pairs = _admitted_ceiling_tier_pairs()
    assert {ceiling for ceiling, _ in pairs} == set(policy.REMOTE_ADMITTED_CEILINGS)
    assert len(pairs) >= 4


def test_routed_tier_never_reports_intimate_for_a_remote_ceiling() -> None:
    """No ceiling ``/v1`` permits can route intimate, so none can report it."""
    pairs = _admitted_ceiling_tier_pairs()
    assert PrivacyTier.INTIMATE not in {routing_tier(c, t) for c, t in pairs}


# ---------------------------------------------------------------------------
# Group 9 -- essay_grounded
# ---------------------------------------------------------------------------


def test_essay_grounded_is_required() -> None:
    """``essay_grounded`` has no default: a producer must state it explicitly."""
    payload = {
        key: value
        for key, value in REFLECTION_RESPONSE_PAYLOAD.items()
        if key != "essay_grounded"
    }
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate(payload)


def test_essay_grounded_true_is_rejected() -> None:
    """No grounded-essay path exists at contract 0.2, so ``True`` is a bug.

    Lifting this validator is a capability the consumer can detect, so it
    needs a contract minor bump -- not a quiet relaxation of the field.
    """
    payload = {**REFLECTION_RESPONSE_PAYLOAD, "essay_grounded": True}
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate(payload)


@pytest.mark.parametrize("essay", [None, "One paragraph, ungrounded, in voice."])
def test_essay_grounded_false_validates_with_or_without_an_essay(
    essay: str | None,
) -> None:
    """``essay_grounded=False`` is the only admissible value at 0.2."""
    response = ReflectionResponse.model_validate(
        {**REFLECTION_RESPONSE_PAYLOAD, "essay": essay, "essay_grounded": False}
    )
    assert response.essay_grounded is False
    assert response.essay == essay


def test_essay_defaults_to_none_when_omitted() -> None:
    """``essay`` is optional; ``essay_grounded`` is not."""
    payload = {
        key: value
        for key, value in REFLECTION_RESPONSE_PAYLOAD.items()
        if key != "essay"
    }
    response = ReflectionResponse.model_validate(payload)
    assert response.essay is None
    assert response.essay_grounded is False


# ---------------------------------------------------------------------------
# Group 10 -- intimate is structurally unreachable
# ---------------------------------------------------------------------------


def test_journal_upsert_request_rejects_an_intimate_tier() -> None:
    """A remote consumer cannot even express an intimate write."""
    payload = {**JOURNAL_UPSERT_REQUEST_PAYLOAD, "tier": "intimate"}
    with pytest.raises(ValidationError):
        JournalUpsertRequest.model_validate(payload)


def test_journal_upsert_response_rejects_an_intimate_tier() -> None:
    """Nor can the server express one back on the same field."""
    payload = {**JOURNAL_UPSERT_RESPONSE_PAYLOAD, "tier": "intimate"}
    with pytest.raises(ValidationError):
        JournalUpsertResponse.model_validate(payload)


def test_reflection_response_rejects_an_intimate_routed_tier() -> None:
    """``routed_tier`` cannot say ``intimate`` on a remote-only surface."""
    payload = {**REFLECTION_RESPONSE_PAYLOAD, "routed_tier": "intimate"}
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate(payload)


def test_tier_model_rejects_a_false_never_egresses_claim() -> None:
    """``intimate_never_egresses`` is a ``Literal[True]`` promise, not a flag."""
    payload = {**TIER_MODEL_PAYLOAD, "intimate_never_egresses": False}
    with pytest.raises(ValidationError):
        TierModel.model_validate(payload)


def test_tier_model_rejects_a_non_open_default() -> None:
    """The advertised default ceiling is ``open`` -- the most restrictive one."""
    payload = {**TIER_MODEL_PAYLOAD, "default": "personal"}
    with pytest.raises(ValidationError):
        TierModel.model_validate(payload)


# ---------------------------------------------------------------------------
# Group 11 -- care signal fidelity and request rules
# ---------------------------------------------------------------------------


def test_care_signal_model_tracks_the_runtime_constant() -> None:
    """``CareSignal`` validates ``creek.care.guardrail.CARE_SIGNAL`` itself."""
    signal = CareSignal.model_validate(CARE_SIGNAL)
    expected_names = [resource["name"] for resource in CARE_SIGNAL["resources"]]
    expected_contacts = [rsrc["contact"] for rsrc in CARE_SIGNAL["resources"]]
    assert signal.kind == CARE_SIGNAL["kind"]
    assert signal.message == CARE_SIGNAL["message"]
    assert [resource.name for resource in signal.resources] == expected_names
    assert [resource.contact for resource in signal.resources] == expected_contacts


def test_care_escalation_response_carries_the_runtime_care_signal() -> None:
    """The escalation envelope wraps the real signal, never an abridged copy."""
    response = CareEscalationResponse.model_validate(CARE_ESCALATION_RESPONSE_PAYLOAD)
    expected = CareSignal.model_validate(CARE_SIGNAL)
    assert response.status is ReflectionStatus.ESCALATE
    assert response.care_signal.model_dump() == expected.model_dump()


def test_care_resource_requires_a_contact() -> None:
    """A resource with no contact would dead-end the person reading it."""
    with pytest.raises(ValidationError):
        CareResource.model_validate({"name": CARE_RESOURCE_PAYLOAD["name"]})


def test_reflection_request_rejects_both_content_and_entry_ref() -> None:
    """Supplying both sources is ambiguous, so it is refused rather than ranked."""
    payload = {
        "content": "I keep saying yes.",
        "entry_ref": "frag-2026-07-31-ridge-fog",
        "max_notes": 3,
    }
    with pytest.raises(ValidationError):
        ReflectionRequest.model_validate(payload)


def test_reflection_request_rejects_neither_content_nor_entry_ref() -> None:
    """A request with nothing to reflect on is a client bug, not an empty result."""
    with pytest.raises(ValidationError):
        ReflectionRequest.model_validate({"max_notes": 3})


@pytest.mark.parametrize("field_name", ["content", "entry_ref"])
def test_reflection_request_accepts_exactly_one_source(field_name: str) -> None:
    """Exactly one of ``content`` / ``entry_ref`` is the admissible shape."""
    request = ReflectionRequest.model_validate({field_name: "a single source"})
    assert getattr(request, field_name) == "a single source"


def test_reflection_request_max_notes_defaults_to_three() -> None:
    """``max_notes`` defaults to 3 so a minimal request is well-defined."""
    assert ReflectionRequest.model_validate({"content": "a line"}).max_notes == 3


@pytest.mark.parametrize("max_notes", [0, 11])
def test_reflection_request_rejects_out_of_range_max_notes(max_notes: int) -> None:
    """``max_notes`` is bounded to ``[1, 10]`` at the boundary, not near it."""
    payload = {"content": "a line", "max_notes": max_notes}
    with pytest.raises(ValidationError):
        ReflectionRequest.model_validate(payload)


@pytest.mark.parametrize("max_notes", [1, 10])
def test_reflection_request_accepts_the_inclusive_bounds(max_notes: int) -> None:
    """Both bounds are inclusive, so 1 and 10 are valid requests."""
    request = ReflectionRequest.model_validate(
        {"content": "a line", "max_notes": max_notes}
    )
    assert request.max_notes == max_notes


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
def test_journal_upsert_request_rejects_blank_content(content: str) -> None:
    """A whitespace-only entry creates an empty fragment, so it is refused."""
    payload = {**JOURNAL_UPSERT_REQUEST_PAYLOAD, "content": content}
    with pytest.raises(ValidationError):
        JournalUpsertRequest.model_validate(payload)


def test_journal_upsert_request_timestamp_is_optional() -> None:
    """``timestamp`` defaults to ``None`` so the server may stamp it."""
    payload = {
        key: value
        for key, value in JOURNAL_UPSERT_REQUEST_PAYLOAD.items()
        if key != "timestamp"
    }
    assert JournalUpsertRequest.model_validate(payload).timestamp is None


# ---------------------------------------------------------------------------
# Group 12 -- layering
# ---------------------------------------------------------------------------


def test_creek_package_sweep_is_not_vacuous() -> None:
    """The ``creek/`` layering sweep actually has sources to walk."""
    assert "creek-tools/creek/models.py" in CREEK_SOURCES


@pytest.mark.parametrize("relative_path", CREEK_SOURCES)
def test_creek_package_never_imports_creek_mcp(relative_path: str) -> None:
    """The domain layer never depends on the MCP layer (#1032)."""
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "creek_mcp" not in _imported_roots(source)


def test_api_package_contains_python_sources() -> None:
    """``creek_mcp/api/`` exists and ships modules -- guards the sweep below."""
    assert _api_sources(), "creek_mcp/api/ has no Python sources to check"


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_API_IMPORTS))
def test_api_package_imports_no_web_framework(forbidden: str) -> None:
    """The models layer stays framework-free so #1074 can choose freely."""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _api_sources()
        if forbidden in _imported_roots(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_models_module_reads_no_files() -> None:
    """``models.py`` opens nothing -- the #1079 "no third tier reader" claim.

    Its module docstring promises that nothing here reads a fragment's
    ``privacy_tier``. #1079 is open precisely because two readers of the same
    note already disagree, so a *third* one appearing inside the wire
    vocabulary would be the same bug one layer out. The claim is cheap to make
    in prose and easy to break with one convenience helper, so it is pinned by
    AST: no call in ``models.py`` may name a file-reading builtin or
    ``Path`` method.
    """
    source = (API_PACKAGE / "models.py").read_text(encoding="utf-8")
    readers = {"open", "read_text", "read_bytes", "read", "load", "safe_load"}
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
    }
    assert called & readers == set()


def test_every_contract_version_earns_a_change_log_row() -> None:
    """The running version is documented in the ADR's change log (#1246).

    A bump with no row is a version string a consumer cannot interpret:
    they can see the number moved and not what moved with it. The two
    existing ADR pins check the *header* line; this one checks that the
    table a consumer actually reads to decide whether to care has caught
    up too.
    """
    text = ADR_PATH.read_text(encoding="utf-8")
    row = rf"^\| `{re.escape(CONTRACT_VERSION)}` \| \d{{4}}-\d{{2}}-\d{{2}} \| \S"
    assert re.search(row, text, re.MULTILINE) is not None, (
        f"the change log has no row for {CONTRACT_VERSION}"
    )


def test_the_compatibility_window_only_ever_widens() -> None:
    """Every minor ever served is still served (#1023, #1246, #1372, #1494).

    :data:`SUPPORTED_CONTRACT_MINORS` is the promise that a bump does
    not strand existing clients, and the promise is only worth anything
    if it is checked *per bump* rather than restated. Each entry here
    was the current minor once; dropping one means a client that used
    to be answered now gets ``incompatible_version``, which is a
    deliberate breaking change and must not happen by omission when
    somebody bumps :data:`CONTRACT_VERSION` and forgets this tuple.

    ``0.4`` joins the list at the 0.5 bump (#1372, the advisory fields on
    ``creek.journal`` / ``creek.upload`` / ``creek.link``); ``0.5`` joins it
    at the 0.6 bump (#1453, the two ``creek.purge.*`` erasure counters). Each
    is the case this guard exists for: the tuple's head entry is *derived*
    from :data:`CONTRACT_VERSION`, so bumping the version and nothing else
    silently shifts the outgoing minor out of the window rather than widening
    it, and every live Adepthood consumer still sending it starts being
    refused. Neither change warranted that — 0.5's one new ``/v1`` field is
    optional and omitted when empty, and 0.6 moved no ``/v1`` shape at all.

    ``0.6`` joins at the 0.7 bump (#1494, ``tier`` becoming mandatory on
    ``creek.journal`` and ``creek.upload``). That one is the sharpest case in
    the list for widening rather than shifting: the break is real, but it is
    real only on MCP. ``JournalUpsertRequest.tier`` never had a default and
    ``creek.upload`` has no ``/v1`` route, so a ``0.6``-pinned ``/v1`` client
    was already sending exactly what the new contract demands — shifting the
    window would refuse it over a change it cannot express.
    """
    for retired_minor in ("0.2", "0.3", "0.4", "0.5", "0.6"):
        assert retired_minor in SUPPORTED_CONTRACT_MINORS
