"""Drift gate for the shared Adepthood/Creek ontology vocabulary in ADRs.

Two decision records restate the cross-repo ontology identity in prose.
Prose has no compiler, so a wrong restatement can sit in ``main``
indefinitely: the ADRs have already carried both a wrong axis (*"Aspects
= frequencies = Wavelength phases"*) and an invented Mode (*"Clear
Light's is Be (Both/Neither)"*) through green CI, because nothing tied
the prose back to :mod:`creek.models`.

These tests tie it back. The canonical source is
``docs/Ontology/creek_ontology_agent_prompt.md`` §7.2, which defines the
Modes as **five** functional stances mapped over the nine frequencies
Beige through Ultraviolet; :class:`creek.models.Mode` implements exactly
those five (plus ``unclassified``). The shared ten-member identity is
therefore the colour-keyed frequency axis alone — *Adepthood Aspects =
Creek APTITUDE frequencies = Adepthood Stages* — and neither Wavelength
axis (five Modes, six phases) belongs in it.

Sibling gates: ``tests/test_taxonomy_docs_drift.py`` (INC-019 drift, but
scoped to ``creek-tools/docs/``) and ``tests/test_redaction_docs_drift.py``
(the established repo-root docs-drift pattern this module follows).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from creek.models import Mode

# parents[0] is ``tests``, parents[1] is ``creek-tools``, parents[2] is
# the repository root, where ``docs/`` lives.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DECISIONS_DIR: Final[Path] = REPO_ROOT / "docs" / "decisions"
ONTOLOGY_PROMPT: Final[Path] = (
    REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"
)

#: The runtime source of the version strings the ADRs publish; its module
#: docstring restates the same identity. Its sibling
#: ``creek_mcp/api/models.py`` carries the phrasing in a Pydantic ``Field``
#: description that is published in the JSON Schema Adepthood vendors under
#: digest verification, so correcting *that* one is a coordinated contract
#: bump (issue #1537) rather than a docs edit, and is out of scope here.
CONTRACT_MODULE: Final[Path] = REPO_ROOT / "creek-tools" / "creek_mcp" / "contract.py"

#: The ADRs that restate the cross-repo ontology identity in prose.
IDENTITY_DOCS: Final[tuple[str, ...]] = (
    "2026-06-30-adepthood-creek-mcp-contract.md",
    "2026-07-31-adepthood-http-application-api.md",
)

#: A "shared … vocabulary" claim plus the clause that qualifies it. The
#: ``[^.]`` runs keep the window inside one sentence so an unrelated
#: later paragraph cannot be read as part of the claim.
SHARED_VOCABULARY_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"shared[^.]{0,120}?vocabulary[^.]{0,160}"
)

#: Either Wavelength axis, in the hyphenated or spaced spelling used in
#: the ADRs (``Wavelength-phase``, ``Archetypal Wavelength Modes``).
WAVELENGTH_AXIS: Final[re.Pattern[str]] = re.compile(
    r"Wavelength[\s-]+(?:phase|mode)", re.IGNORECASE
)

#: A parenthesised Mode enumeration attributed to §7.2 of the ontology
#: prompt, e.g. ``§7.2 names (Inhabit, Express, …)``. Anchored on the
#: first canonical Mode so an ordinary parenthetical near a §7.2 citation
#: is not misread as a Mode list; if the enumeration is ever reworded out
#: of that shape the test fails on its own positive control instead of
#: passing vacuously.
SECTION_7_2_ENUMERATION: Final[re.Pattern[str]] = re.compile(
    r"§7\.2[^(]{0,60}\((Inhabit[^)]*)\)"
)

#: A row of the §7.2 "five Modes and their Orientations" table: the Mode
#: name is the bolded first cell.
MODE_TABLE_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*\*\*(\w+)\*\*\s*\|\s*Do\s*/?\s*Feel", re.MULTILINE
)


def _canonical_mode_names() -> set[str]:
    """Return the title-case names of the real, classified Modes."""
    return {mode.value.title() for mode in Mode if mode is not Mode.UNCLASSIFIED}


def _normalised(path: Path) -> str:
    """Read ``path`` with newlines folded, so claims can span lines."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename", IDENTITY_DOCS)
def test_shared_vocabulary_claim_names_no_wavelength_axis(filename: str) -> None:
    """No ADR puts a Wavelength axis inside the shared-vocabulary identity.

    The shared axis has ten colour-keyed members. The Modes (five) and
    the phases (six) are different axes with different cardinalities, so
    naming either one as *the* shared vocabulary is a category error —
    the one these ADRs have now made twice, once per axis.
    """
    text = _normalised(DECISIONS_DIR / filename)
    claims = SHARED_VOCABULARY_CLAIM.findall(text)
    assert claims, f"{filename} no longer states a shared-vocabulary claim to gate"
    for claim in claims:
        assert not WAVELENGTH_AXIS.search(claim), (
            f"{filename} names a Wavelength axis as the shared vocabulary: {claim!r}"
        )


def test_adr_mode_enumeration_invents_no_mode() -> None:
    """The MCP-contract ADR's §7.2 citation lists exactly the real Modes.

    Guards the specific regression: the ADR once cited §7.2 for a tenth
    Mode ("Clear Light's Be") that §7.2 does not define and
    :class:`creek.models.Mode` does not implement.
    """
    text = _normalised(DECISIONS_DIR / IDENTITY_DOCS[0])
    enumerations = SECTION_7_2_ENUMERATION.findall(text)
    assert enumerations, "the MCP-contract ADR no longer cites §7.2's Mode list"
    for enumeration in enumerations:
        named = {part.strip(" *_") for part in enumeration.split(",") if part.strip()}
        assert named == _canonical_mode_names(), (
            f"ADR §7.2 citation lists {sorted(named)}, but the canonical Modes "
            f"are {sorted(_canonical_mode_names())}"
        )


def test_ontology_prompt_section_7_2_matches_the_mode_enum() -> None:
    """§7.2's Mode table is exactly :class:`creek.models.Mode`'s members.

    This is the anchor the ADR gate cites. If the canonical section ever
    gains or loses a Mode, this fails first and the ADR wording that
    quotes it must be revisited in the same change.
    """
    rows = MODE_TABLE_ROW.findall(ONTOLOGY_PROMPT.read_text(encoding="utf-8"))
    assert set(rows) == _canonical_mode_names()
    assert len(rows) == len(_canonical_mode_names())


def test_contract_module_docstring_names_no_wavelength_axis() -> None:
    """``creek_mcp.contract``'s docstring states the same identity correctly.

    The ADR names this module as the runtime source of the version
    strings, so the two must not disagree about what ``ONTOLOGY_VERSION``
    versions.
    """
    text = _normalised(CONTRACT_MODULE)
    claims = SHARED_VOCABULARY_CLAIM.findall(text)
    assert claims, "creek_mcp/contract.py no longer states the shared vocabulary"
    for claim in claims:
        assert not WAVELENGTH_AXIS.search(claim), (
            f"creek_mcp/contract.py names a Wavelength axis as the shared "
            f"vocabulary: {claim!r}"
        )
