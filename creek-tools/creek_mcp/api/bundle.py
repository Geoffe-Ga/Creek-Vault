"""The published contract bundle for the Adepthood ``/v1`` API (#1072).

:func:`build_bundle` renders the whole of ``docs/contracts/adepthood-v1/`` from
the code in :mod:`creek_mcp.api.models`: one JSON Schema per published model, a
5x7 matrix of worked example responses, the retry table, and a manifest that
hashes every one of them. :func:`write_bundle` materialises it.

**Why generate it instead of hand-writing it.** A hand-written fixture bundle
is a snapshot of what the contract looked like on the day somebody typed it. A
committed bundle that must be byte-identical to a fresh ``build_bundle()`` is
something else: change a model and the bundle goes red until it is regenerated,
so a cross-repo consumer reading the fixtures is never reading last month's
contract. That is the whole mechanism, and it is why the tests read the
committed files from disk rather than regenerating them — a test that rebuilt
the artifact it checks would catch nothing.

**Determinism.** Every file is ``json.dumps(..., indent=2, sort_keys=True)``
plus a trailing newline, and the manifest's ``files`` list is sorted by path, so
a diff of two bundles is a diff of content rather than of ordering.

**The state axis.** Seven states per capability, chosen so a consumer can write
its whole error-handling surface against fixtures:

- ``success`` — the canonical happy response.
- ``empty`` — the *legitimately* empty answer, never an error. An uninitialised
  vault still reports both version strings, so version negotiation works before
  a vault exists; an unchanged idempotent journal write is a success carrying
  ``action: "unchanged"``; a reflection with nothing to say is ``status:
  "empty"``; an unclassified corpus is an all-zero wheel.
- ``refusal`` — an :class:`~creek_mcp.api.models.ErrorEnvelope` carrying
  ``privacy_refused``. **These are the intimate examples, and every one of them
  is a refusal rather than a success** — that is the point of publishing them.
- ``care-escalation`` — ``reflections`` only; the other four cells are
  :class:`~creek_mcp.api.models.NotApplicableExample`, because the
  acute-distress guard runs only inside ``reflect_tool``.
- ``malformed-input`` / ``incompatible-version`` / ``unavailable-service`` —
  the three remaining error envelopes.

**Security invariants on the fixtures.** No fixture carries a real journal
body, a credential or a token; the journal example is a *response*, so no entry
body appears in the bundle at all, and the one quoted line in the reflection
example is labelled synthetic in the note beside it. No fixture carries
``tier: "intimate"`` or ``routed_tier: "intimate"`` anywhere — it is not even
expressible, because :class:`~creek_mcp.api.models.WireTierCeiling` has two
members. Payloads are built from the imported runtime constants
(:data:`~creek_mcp.contract.CONTRACT_VERSION`,
:data:`~creek_mcp.contract.ONTOLOGY_VERSION`,
:data:`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`,
:data:`creek.care.guardrail.CARE_SIGNAL`,
:data:`~creek_mcp.api.models.ERROR_MESSAGES`) rather than from hand-copied
duplicates, so the bundle cannot drift from the code it documents.

Like :mod:`creek_mcp.api.models`, this module imports no web framework.
"""

from __future__ import annotations

import hashlib
import json
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from creek.care.guardrail import CARE_SIGNAL
from creek.generate.indexes import CANONICAL_FREQUENCY_NAMES
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    CONTRACT_MODELS,
    ERROR_MESSAGES,
    OK_STATUS,
    RETRY_POLICY,
    SUPPORTED_CONTRACT_MINORS,
    CapabilitiesStatus,
    Capability,
    ErrorCode,
    JournalAction,
    NoteKind,
    PraxisKind,
    PraxisLifecycle,
    ReflectionStatus,
    WireTierCeiling,
)
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION

if TYPE_CHECKING:
    from pathlib import Path


class _Example(NamedTuple):
    """One fixture cell: the model that governs it, and its payload."""

    model: str
    payload: dict[str, Any]


class _BundleFile(NamedTuple):
    """One generated file, plus the manifest metadata that describes it."""

    path: str
    text: str
    model: str | None
    capability: str | None
    state: str | None


# --------------------------------------------------------------------------
# The published axes
# --------------------------------------------------------------------------

BUNDLE_DIR_NAME: Final[str] = "adepthood-v1"
"""Directory name the bundle is committed under, inside ``docs/contracts/``.

Versioned by contract major.minor, not by date: a consumer pins the directory,
and a contract change that breaks them gets a new directory rather than an
edit to this one.
"""

CAPABILITIES: Final[tuple[str, ...]] = tuple(
    capability.value for capability in Capability
)
"""The capability axis of the fixture matrix.

Read off :class:`~creek_mcp.api.models.Capability` rather than restated, so the
capability list a server advertises and the directories a consumer browses can
never name different things.
"""

_STATE_SUCCESS: Final[str] = "success"
"""The canonical happy response for a capability."""

_STATE_EMPTY: Final[str] = "empty"
"""The legitimately-empty answer, which is never an error."""

_STATE_REFUSAL: Final[str] = "refusal"
"""An above-ceiling refusal; the intimate example for every capability."""

_STATE_CARE_ESCALATION: Final[str] = "care-escalation"
"""The acute-distress escalation; reachable only for ``reflections``."""

_STATE_MALFORMED_INPUT: Final[str] = "malformed-input"
"""A request that does not satisfy the published schema."""

_STATE_INCOMPATIBLE_VERSION: Final[str] = "incompatible-version"
"""A contract minor this server does not serve."""

_STATE_UNAVAILABLE_SERVICE: Final[str] = "unavailable-service"
"""A vault that is absent or unreadable, needing an operator."""

EXAMPLE_STATES: Final[tuple[str, ...]] = (
    _STATE_SUCCESS,
    _STATE_EMPTY,
    _STATE_REFUSAL,
    _STATE_CARE_ESCALATION,
    _STATE_MALFORMED_INPUT,
    _STATE_INCOMPATIBLE_VERSION,
    _STATE_UNAVAILABLE_SERVICE,
)
"""The state axis of the fixture matrix, in the order a consumer meets them."""

UNREACHABLE_CELLS: Final[frozenset[tuple[str, str]]] = frozenset(
    (capability, _STATE_CARE_ESCALATION)
    for capability in CAPABILITIES
    if capability != Capability.REFLECTIONS.value
)
"""The matrix cells that have no reachable response shape.

Derived from the axes rather than listed, so it cannot fall out of step with
the matrix it describes. The acute-distress guard runs only inside
:func:`creek_mcp.tools.reflect.reflect_tool`, so ``capabilities``,
``journal-upsert``, ``wheel`` and ``upload`` can never escalate. Those four
cells are filled with a :class:`~creek_mcp.api.models.NotApplicableExample`
that says so.
"""

_ERROR_STATE_CODES: Final[dict[str, ErrorCode]] = {
    _STATE_REFUSAL: ErrorCode.PRIVACY_REFUSED,
    _STATE_MALFORMED_INPUT: ErrorCode.INVALID_REQUEST,
    _STATE_INCOMPATIBLE_VERSION: ErrorCode.INCOMPATIBLE_VERSION,
    _STATE_UNAVAILABLE_SERVICE: ErrorCode.UNAVAILABLE,
}
"""The four states whose fixture is an error envelope, and which code each is."""


# --------------------------------------------------------------------------
# Payloads, built from the runtime constants they document
# --------------------------------------------------------------------------

_TIER_MODEL_PAYLOAD: Final[dict[str, Any]] = {
    "ceilings": [ceiling.value for ceiling in WireTierCeiling],
    "default": WireTierCeiling.OPEN.value,
    "intimate_never_egresses": True,
}
"""The standing tier promise, listed straight off the wire enum."""

_CAPABILITIES_SUCCESS: Final[dict[str, Any]] = {
    "status": CapabilitiesStatus.OK.value,
    "contract_version": CONTRACT_VERSION,
    "contract_minor": CONTRACT_MINOR,
    "supported_contract_minors": list(SUPPORTED_CONTRACT_MINORS),
    "ontology_version": ONTOLOGY_VERSION,
    "vault": {"available": True},
    "tier_model": _TIER_MODEL_PAYLOAD,
    "capabilities": list(CAPABILITIES),
}
"""A ready server: vault present, all five capabilities served."""

_CAPABILITIES_EMPTY: Final[dict[str, Any]] = {
    **_CAPABILITIES_SUCCESS,
    "status": CapabilitiesStatus.UNINITIALIZED.value,
    "vault": {"available": False},
    "capabilities": [],
}
"""An uninitialised vault — and still both version strings.

Spread from the success payload rather than rewritten, which is what makes the
surviving ``contract_version`` / ``ontology_version`` visible as a deliberate
property: a client must be able to negotiate versions against a server whose
vault does not exist yet.
"""

_EXAMPLE_EXTERNAL_ID: Final[str] = "adepthood:entry:2026-07-31T06:12:00Z"
"""A synthetic consumer-side entry id, in the shape Adepthood mints."""

_EXAMPLE_FRAGMENT_ID: Final[str] = "frag-3f9a1c7e40b2"
"""A synthetic vault-side fragment id, in the real ``frag-`` + 12-hex shape.

Deliberately opaque rather than a readable slug. Fragment ids are unguessable
by construction, and several accepted residuals across the MCP surface rest on
exactly that — a probing caller cannot enumerate ids it was never shown. An
example in a date-and-words shape would advertise a guessable id space that
Creek does not actually have, and a client author copying the shape into a
test fixture would encode the wrong assumption.
"""

_JOURNAL_SUCCESS: Final[dict[str, Any]] = {
    "status": OK_STATUS,
    "tier_ceiling": WireTierCeiling.PERSONAL.value,
    "external_id": _EXAMPLE_EXTERNAL_ID,
    "fragment_id": _EXAMPLE_FRAGMENT_ID,
    "action": JournalAction.CREATED.value,
    "tier": WireTierCeiling.PERSONAL.value,
}
"""A first write. The fixture is the *response*, so no entry body is published."""

_JOURNAL_EMPTY: Final[dict[str, Any]] = {
    **_JOURNAL_SUCCESS,
    "action": JournalAction.UNCHANGED.value,
}
"""A re-sync that changed nothing: a success, not an error."""

_EXAMPLE_UPLOAD_EXTERNAL_ID: Final[str] = "adepthood:doc:2026-08-18T09:30:00Z"
"""A synthetic consumer-side document id, in the shape Adepthood mints."""

_EXAMPLE_UPLOAD_FRAGMENT_ID: Final[str] = "frag-8c41d0be59a7"
"""A second synthetic fragment id, opaque for the same reason as the first."""

_UPLOAD_SOURCE_TYPE: Final[str] = "document"
"""The ingestor a ``.pdf`` / ``.docx`` upload dispatches to.

A :data:`creek.ingest.INGESTOR_REGISTRY` key, spelled here as the literal it is
on the wire: the fixture documents the *response*, and a consumer reading this
cell needs to know the field carries a short registry name rather than a MIME
type or a filename.
"""

_UPLOAD_SUCCESS: Final[dict[str, Any]] = {
    "status": OK_STATUS,
    "tier_ceiling": WireTierCeiling.PERSONAL.value,
    "external_id": _EXAMPLE_UPLOAD_EXTERNAL_ID,
    "fragment_id": _EXAMPLE_UPLOAD_FRAGMENT_ID,
    "affected_fragment_ids": [_EXAMPLE_UPLOAD_FRAGMENT_ID],
    "action": JournalAction.CREATED.value,
    "source_type": _UPLOAD_SOURCE_TYPE,
}
"""A first upload.

The fixture is the *response*, so no document bytes are published — and no
``tier`` either, because :class:`~creek_mcp.api.models.UploadResponse` has none:
a document's own frontmatter can escalate the fragment above the declared tier,
so any tier field here would be either a false claim or an oracle.
"""

_UPLOAD_EMPTY: Final[dict[str, Any]] = {
    **_UPLOAD_SUCCESS,
    "action": JournalAction.UNCHANGED.value,
}
"""A re-send of identical bytes under the same id: a success, not an error.

This is the cell that documents idempotency. The ledger recognised the content,
nothing was written, and the *same* ``fragment_id`` comes back — which is why
the payload is spread from the success cell rather than written out with a new
id.
"""

_EXAMPLE_NOTE: Final[dict[str, Any]] = {
    "quote": "I keep saying yes to things I do not want",
    "kind": NoteKind.PATTERN.value,
    "note": (
        "Synthetic example prose. A real note mirrors the writer's own words "
        "back to them and never advises."
    ),
}
"""One margin note. Deliberately invented, and labelled as such in the note."""

_EXAMPLE_PRAXIS: Final[dict[str, Any]] = {
    "title": "Rest before the collapse",
    "praxis_type": PraxisKind.PRACTICE.value,
    "status": PraxisLifecycle.ACTIVE.value,
    "excerpt": (
        "Synthetic example prose. A real excerpt is the praxis page's own "
        "opening lines, taken verbatim and capped."
    ),
}
"""The ``related_praxis`` cell of the success fixture (contract 0.9, #873).

Synthetic, like the note beside it: no fixture in this bundle carries real
vault prose. What it documents is the *shape*, and one property a consumer
cannot see from the shape — a praxis page reaches the wire only when every
fragment its ``derived_from`` names is within the caller's ceiling.
"""

_EXAMPLE_EDDY: Final[dict[str, Any]] = {
    "title": "Rest and Ruin",
    "description": "Synthetic example description of a topic cluster.",
    "fragment_count": 12,
    "formed": "2026-03-04",
}
"""The ``related_eddies`` cell of the success fixture (contract 0.9, #873).

``description`` and ``fragment_count`` are *compiled from* the eddy's members,
which is why the page is published only when the members that can be
enumerated account for ``fragment_count`` exactly and every one of them is
admitted. An eddy whose provenance is partial is withheld, not summarised.
"""

_REFLECTIONS_SUCCESS: Final[dict[str, Any]] = {
    "status": ReflectionStatus.OK.value,
    "tier_ceiling": WireTierCeiling.PERSONAL.value,
    "routed_tier": WireTierCeiling.PERSONAL.value,
    "notes": [_EXAMPLE_NOTE],
    "essay": None,
    "essay_grounded": False,
    "related_praxis": [_EXAMPLE_PRAXIS],
    "related_eddies": [_EXAMPLE_EDDY],
}
"""A reflection that found something.

``routed_tier`` equals ``tier_ceiling`` here, and always will: it is a constant
function of the declared ceiling, which is why publishing it discloses nothing.

The two ``related_*`` fields are optional at 0.9 and are shown *present* here
on purpose: a consumer needs the populated shape to write its parser against,
and :data:`_REFLECTIONS_EMPTY` next door shows the absent case, which is what
the route actually emits whenever nothing qualified.
"""

_REFLECTIONS_EMPTY: Final[dict[str, Any]] = {
    **{
        key: value
        for key, value in _REFLECTIONS_SUCCESS.items()
        if key not in {"related_praxis", "related_eddies"}
    },
    "status": ReflectionStatus.EMPTY.value,
    "notes": [],
}
"""A reflection with nothing to say. Still a 200.

Also the published example of the ``related_*`` fields being **absent** rather
than present-and-empty — the shape a pre-0.9 consumer keeps seeing, and the
one the route emits when the vault has no compiled neighbours the caller is
admitted to. The two cases are indistinguishable here by design: telling "no
eddies exist" from "the eddies that exist were withheld" would be a one-bit
oracle over the compiled layer.
"""

_CARE_PAYLOAD: Final[dict[str, Any]] = {
    "status": ReflectionStatus.ESCALATE.value,
    "tier_ceiling": WireTierCeiling.PERSONAL.value,
    "reason": CARE_SIGNAL["kind"],
    "care_signal": CARE_SIGNAL,
}
"""The escalation, wrapping the real :data:`creek.care.guardrail.CARE_SIGNAL`.

The signal is embedded by reference, not abridged, so the resources a person in
distress is handed here are exactly the ones the guardrail ships.
"""

_UNREACHABLE_PAYLOAD: Final[dict[str, Any]] = {
    "unreachable": True,
    "reason": (
        "the acute-distress guard runs only inside reflect_tool, so this "
        "capability has no care-escalation response shape"
    ),
}
"""What fills a structurally unreachable cell, and why it is unreachable."""

_EXAMPLE_FREQUENCY_COUNT: Final[int] = 1
"""One classified fragment per frequency, so the shares are readable."""

_EXAMPLE_TOTAL_CLASSIFIED: Final[int] = _EXAMPLE_FREQUENCY_COUNT * len(
    CANONICAL_FREQUENCY_NAMES
)
"""Total classified fragments in the worked wheel, derived from the per-key count."""

_EXAMPLE_FREQUENCY_SHARE: Final[float] = (
    _EXAMPLE_FREQUENCY_COUNT / _EXAMPLE_TOTAL_CLASSIFIED
)
"""Each frequency's share of the worked wheel; the ten sum to 1.0."""

_EMPTY_COUNT: Final[int] = 0
"""The tally on an unclassified corpus."""

_EMPTY_SHARE: Final[float] = 0.0
"""The share on an unclassified corpus."""

_WHEEL_SUCCESS: Final[dict[str, Any]] = {
    "status": OK_STATUS,
    "tier_ceiling": WireTierCeiling.OPEN.value,
    "total_classified": _EXAMPLE_TOTAL_CLASSIFIED,
    "unclassified": _EMPTY_COUNT,
    "wheel": {
        frequency.value: {
            "name": name,
            "count": _EXAMPLE_FREQUENCY_COUNT,
            "share": _EXAMPLE_FREQUENCY_SHARE,
        }
        for frequency, name in CANONICAL_FREQUENCY_NAMES.items()
    },
}
"""An evenly-spread wheel, with the canonical frequency names read off the spec."""

_WHEEL_EMPTY: Final[dict[str, Any]] = {
    **_WHEEL_SUCCESS,
    "total_classified": _EMPTY_COUNT,
    "wheel": {
        frequency.value: {
            "name": name,
            "count": _EMPTY_COUNT,
            "share": _EMPTY_SHARE,
        }
        for frequency, name in CANONICAL_FREQUENCY_NAMES.items()
    },
}
"""A freshly-initialised vault: all zeroes, and emphatically not an error."""


# --------------------------------------------------------------------------
# The 5 x 7 matrix
# --------------------------------------------------------------------------

_SUCCESS_EXAMPLES: Final[dict[str, _Example]] = {
    Capability.CAPABILITIES.value: _Example(
        model="CapabilitiesResponse",
        payload=_CAPABILITIES_SUCCESS,
    ),
    Capability.JOURNAL_UPSERT.value: _Example(
        model="JournalUpsertResponse",
        payload=_JOURNAL_SUCCESS,
    ),
    Capability.REFLECTIONS.value: _Example(
        model="ReflectionResponse",
        payload=_REFLECTIONS_SUCCESS,
    ),
    Capability.WHEEL.value: _Example(
        model="WheelResponse",
        payload=_WHEEL_SUCCESS,
    ),
    Capability.UPLOAD.value: _Example(
        model="UploadResponse",
        payload=_UPLOAD_SUCCESS,
    ),
}
"""The ``success`` column: one canonical happy response per capability."""

_EMPTY_EXAMPLES: Final[dict[str, _Example]] = {
    Capability.CAPABILITIES.value: _Example(
        model="CapabilitiesResponse",
        payload=_CAPABILITIES_EMPTY,
    ),
    Capability.JOURNAL_UPSERT.value: _Example(
        model="JournalUpsertResponse",
        payload=_JOURNAL_EMPTY,
    ),
    Capability.REFLECTIONS.value: _Example(
        model="ReflectionResponse",
        payload=_REFLECTIONS_EMPTY,
    ),
    Capability.WHEEL.value: _Example(
        model="WheelResponse",
        payload=_WHEEL_EMPTY,
    ),
    Capability.UPLOAD.value: _Example(
        model="UploadResponse",
        payload=_UPLOAD_EMPTY,
    ),
}
"""The ``empty`` column. Every cell is a success envelope, not an error."""


def _care_example(capability: str) -> _Example:
    """Return the ``care-escalation`` cell for *capability*.

    Args:
        capability: The capability whose cell is being filled.

    Returns:
        The real escalation envelope for ``reflections``; an explicit
        unreachability marker for the three capabilities that cannot escalate.
    """
    if (capability, _STATE_CARE_ESCALATION) in UNREACHABLE_CELLS:
        return _Example(model="NotApplicableExample", payload=_UNREACHABLE_PAYLOAD)
    return _Example(model="CareEscalationResponse", payload=_CARE_PAYLOAD)


def _error_example(capability: str, state: str) -> _Example:
    """Return the error-envelope cell for *capability* / *state*.

    The message is looked up in :data:`~creek_mcp.api.models.ERROR_MESSAGES`
    rather than written here, so a published fixture cannot show a caller a
    refusal reason the server would never send.

    Args:
        capability: The capability whose cell is being filled.
        state: One of the four error states.

    Returns:
        The fixture for that cell.
    """
    code = _ERROR_STATE_CODES[state]
    return _Example(
        model="ErrorEnvelope",
        payload={
            "code": code.value,
            "message": ERROR_MESSAGES[code],
            "request_id": f"req-example-{capability}-{state}",
        },
    )


def _example_for(capability: str, state: str) -> _Example:
    """Return the fixture for one ``(capability, state)`` matrix cell.

    Args:
        capability: One of :data:`CAPABILITIES`.
        state: One of :data:`EXAMPLE_STATES`.

    Returns:
        The fixture for that cell.
    """
    if state == _STATE_SUCCESS:
        return _SUCCESS_EXAMPLES[capability]
    if state == _STATE_EMPTY:
        return _EMPTY_EXAMPLES[capability]
    if state == _STATE_CARE_ESCALATION:
        return _care_example(capability)
    return _error_example(capability, state)


EXAMPLES: Final[dict[tuple[str, str], _Example]] = {
    (capability, state): _example_for(capability, state)
    for capability in CAPABILITIES
    for state in EXAMPLE_STATES
}
"""Every cell of the matrix, built from the two axes so none can go missing."""


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_JSON_INDENT: Final[int] = 2
"""Indent width for every file in the bundle."""

_SCHEMA_DIR: Final[str] = "schemas"
"""Bundle subdirectory holding one JSON Schema per published model."""

_EXAMPLE_DIR: Final[str] = "examples"
"""Bundle subdirectory holding the ``<capability>/<state>.json`` matrix."""

_RETRY_POLICY_FILENAME: Final[str] = "retry-policy.json"
"""The serialised retry table, at the bundle root."""

_MANIFEST_FILENAME: Final[str] = "manifest.json"
"""The manifest, which describes every other generated file but not itself."""


def _serialise(payload: object) -> str:
    """Render *payload* in the bundle's one canonical JSON form.

    Args:
        payload: Any JSON-serialisable object.

    Returns:
        Two-space-indented, key-sorted JSON with a trailing newline. Single
        form, everywhere, so a bundle diff shows content and never formatting.
    """
    return json.dumps(payload, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _schema_files() -> list[_BundleFile]:
    """Return one JSON Schema file per published contract model.

    Returns:
        A file per entry in :data:`~creek_mcp.api.models.CONTRACT_MODELS`.
        Schemas are capability-agnostic, so both matrix axes are ``None``.
    """
    return [
        _BundleFile(
            path=f"{_SCHEMA_DIR}/{name}.schema.json",
            text=_serialise(model.model_json_schema()),
            model=name,
            capability=None,
            state=None,
        )
        for name, model in CONTRACT_MODELS.items()
    ]


def _example_files() -> list[_BundleFile]:
    """Return one fixture file per matrix cell.

    Returns:
        A file per entry in :data:`EXAMPLES`, each tagged with the model that
        governs it so a consumer — and a test — can validate it against its
        own named model rather than guessing.
    """
    return [
        _BundleFile(
            path=f"{_EXAMPLE_DIR}/{capability}/{state}.json",
            text=_serialise(example.payload),
            model=example.model,
            capability=capability,
            state=state,
        )
        for (capability, state), example in EXAMPLES.items()
    ]


def _retry_policy_file() -> _BundleFile:
    """Return the serialised retry table.

    Returns:
        :data:`~creek_mcp.api.models.RETRY_POLICY` rendered as
        ``code -> disposition``. Serialised from the runtime table rather than
        retold, so a client reading it is reading what the server enforces.
    """
    return _BundleFile(
        path=_RETRY_POLICY_FILENAME,
        text=_serialise({code.value: RETRY_POLICY[code].value for code in ErrorCode}),
        model=None,
        capability=None,
        state=None,
    )


def _manifest_entry(generated: _BundleFile) -> dict[str, Any]:
    """Return the manifest record describing one generated file.

    Args:
        generated: The file to describe.

    Returns:
        Its path, the sha256 of its UTF-8 bytes, and the model / capability /
        state it belongs to.
    """
    return {
        "path": generated.path,
        "sha256": hashlib.sha256(generated.text.encode("utf-8")).hexdigest(),
        "model": generated.model,
        "capability": generated.capability,
        "state": generated.state,
    }


def _manifest_file(generated: list[_BundleFile]) -> _BundleFile:
    """Return the manifest describing every other file in the bundle.

    Args:
        generated: Every generated file except the manifest itself. The
            manifest cannot list its own hash, so it is excluded by
            construction rather than filtered out later.

    Returns:
        The manifest file, with entries sorted by path.
    """
    ordered = sorted(generated, key=attrgetter("path"))
    payload: dict[str, Any] = {
        "bundle": BUNDLE_DIR_NAME,
        "contract_version": CONTRACT_VERSION,
        "ontology_version": ONTOLOGY_VERSION,
        "files": [_manifest_entry(item) for item in ordered],
    }
    return _BundleFile(
        path=_MANIFEST_FILENAME,
        text=_serialise(payload),
        model=None,
        capability=None,
        state=None,
    )


def build_bundle() -> dict[str, str]:
    """Return the whole bundle as ``bundle-relative posix path -> file text``.

    Returns:
        Every generated file: ``schemas/<Model>.schema.json`` for each
        published model, ``examples/<capability>/<state>.json`` for all 28
        matrix cells, ``retry-policy.json``, and ``manifest.json``. The
        committed bundle must equal this exactly, minus the docs-owned
        ``README.md``.
    """
    generated = [*_schema_files(), *_example_files(), _retry_policy_file()]
    manifest = _manifest_file(generated)
    return {item.path: item.text for item in [*generated, manifest]}


def write_bundle(dest: Path) -> None:
    """Materialise :func:`build_bundle` under *dest*.

    Args:
        dest: Directory to write into. Parent directories are created as
            needed; existing files are overwritten, because the bundle is a
            rendering of the code and never a place to hand-edit.
    """
    for relative_path, text in build_bundle().items():
        target = dest / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # ``newline=""`` disables the platform line-ending translation
        # ``write_text`` would otherwise apply. ``manifest.json`` is the file
        # consumers are told to pin the sha256 of, so a CRLF rewrite on a
        # non-POSIX checkout would hand every downstream verifier a different
        # hash for identical content — and the round-trip test, which reads
        # back with universal-newline translation, would not notice.
        target.write_text(text, encoding="utf-8", newline="")
