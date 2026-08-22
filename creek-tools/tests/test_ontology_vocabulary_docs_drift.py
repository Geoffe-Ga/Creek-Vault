"""Drift gate for the shared Adepthood/Creek ontology vocabulary in ADRs.

Two decision records restate the cross-repo ontology identity in prose.
Prose has no compiler, so a wrong restatement can sit in ``main``
indefinitely: the ADRs have already carried both a wrong axis (*"Aspects
= frequencies = Wavelength phases"*) and an invented Mode (*"Clear
Light's is Be (Both/Neither)"*) through green CI, because nothing tied
the prose back to :mod:`creek.models`.

These tests tie it back. The canonical source is
``docs/Ontology/creek_ontology_agent_prompt.md``: §6.1 defines the ten
colour-keyed APTITUDE frequencies, and §7 defines the Archetypal
Wavelength — §7.1 the six phases, §7.2 the **five** functional Modes
mapped over the nine frequencies Beige through Ultraviolet.
:class:`creek.models.Mode` implements exactly those five (plus
``unclassified``). Three axes, three cardinalities.

**What this module forbids is a shape, not a word.** The bug it exists
to stop is an *equation* between the frequency axis and a Wavelength
axis — ``frequencies = Wavelength phases``, ``APTITUDE frequency /
Wavelength phase vocabulary``. Naming both axes as separate axes in one
sentence is correct and must stay sayable, because
:data:`creek_mcp.contract.ONTOLOGY_VERSION` genuinely versions both. An
earlier revision of this gate keyed on the bare ``Wavelength`` token
instead, which rejected the correct broad phrasing and passed the wrong
one whenever it was capitalised; :data:`WRONG_PHRASINGS` and
:data:`CORRECT_PHRASINGS` pin both halves of that lesson.

Sibling gates: ``tests/test_taxonomy_docs_drift.py`` (INC-019 drift, but
scoped to ``creek-tools/docs/``) and ``tests/test_redaction_docs_drift.py``
(the established repo-root docs-drift pattern this module follows).
"""

from __future__ import annotations

import json
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
CONTRACT_SCHEMA_DIR: Final[Path] = (
    REPO_ROOT / "docs" / "contracts" / "adepthood-v1" / "schemas"
)

#: The runtime source of the version strings the ADRs publish; its module
#: docstring restates the same identity. Its sibling
#: ``creek_mcp/api/models.py`` carries the phrasing in a Pydantic ``Field``
#: description that is published in the JSON Schema Adepthood vendors under
#: digest verification, so correcting *that* one is a coordinated contract
#: bump (issue #1537) rather than a docs edit, and is out of scope here.
#: #1537 also carries the note that the wire description and the vendored
#: ``CapabilitiesResponse`` schema must converge on whatever wording
#: :mod:`creek_mcp.contract` settles on.
CONTRACT_MODULE: Final[Path] = REPO_ROOT / "creek-tools" / "creek_mcp" / "contract.py"
API_MODELS_MODULE: Final[Path] = (
    REPO_ROOT / "creek-tools" / "creek_mcp" / "api" / "models.py"
)

#: The ADRs that restate the cross-repo ontology identity in prose.
IDENTITY_DOCS: Final[tuple[str, ...]] = (
    "2026-06-30-adepthood-creek-mcp-contract.md",
    "2026-07-31-adepthood-http-application-api.md",
)
MCP_CONTRACT_ADR: Final[str] = IDENTITY_DOCS[0]
HTTP_API_ADR: Final[str] = IDENTITY_DOCS[1]

#: One sentence's worth of characters. A full stop only closes the window
#: when whitespace (or the end of the text) follows it, optionally through
#: closing Markdown emphasis — so ``§6.1`` and ``creek.models.Mode`` stay
#: inside their own sentence, while the *next* sentence stays out of it.
#: That matters in both directions: the disambiguation paragraph quotes the
#: historical wrong strings one sentence after the words "shared
#: vocabulary", and must not be read as asserting them.
_IN_SENTENCE: Final[str] = r"""(?:(?!\.[*_`)\]"']*(?:\s|$)).)"""

#: A "shared … vocabulary" claim plus the clause that qualifies it, kept
#: inside one sentence by :data:`_IN_SENTENCE`. Case-insensitive: the exact
#: wrong string still live in ``creek_mcp/api/models.py`` opens a sentence,
#: so it is capitalised, and a gate that missed it would miss the one
#: wording most likely to be copied back in (#1537).
SHARED_VOCABULARY_CLAIM: Final[re.Pattern[str]] = re.compile(
    rf"shared{_IN_SENTENCE}{{0,120}}?vocabular(?:y|ies){_IN_SENTENCE}{{0,200}}",
    re.IGNORECASE,
)

#: Terms naming the ten-member colour-keyed axis, under any of the three
#: names the two repos use for it.
_TEN_MEMBER_AXIS: Final[str] = r"(?:APTITUDE[\s-]*)?frequenc(?:y|ies)|Aspects?|Stages?"

#: Terms naming either Archetypal Wavelength axis, hyphenated or spaced,
#: qualified or bare (``Wavelength-phase``, ``Archetypal Wavelength Modes``,
#: and the bare ``phase`` of ``frequency/phase vocabulary``).
_WAVELENGTH_AXIS: Final[str] = (
    r"(?:Archetypal[\s-]+)?Wavelength(?:[\s-]+(?:phase|mode)s?)?|(?:phase|mode)s?"
)

#: Up to two qualifier words between an equals sign and the axis it names,
#: e.g. ``= Archetypal Wavelength phases`` or ``= the six phases``.
_QUALIFIER: Final[str] = r"(?:\w+[\s-]+){0,2}"

#: The forbidden **shape**: one axis equated with the other. ``=`` and ``/``
#: are the two connectives both historical regressions used ("Aspects =
#: frequencies = Wavelength phases"; "APTITUDE frequency / Wavelength phase
#: vocabulary"), plus the one copula form that says it in words. A
#: conjunction — "the frequencies *and* the Wavelength axes" — is not an
#: equation and deliberately does not match.
AXIS_IDENTITY: Final[re.Pattern[str]] = re.compile(
    rf"(?:{_TEN_MEMBER_AXIS})\s*(?:=|/|≡)\s*{_QUALIFIER}(?:{_WAVELENGTH_AXIS})"
    rf"|(?:{_WAVELENGTH_AXIS})\s*(?:=|/|≡)\s*{_QUALIFIER}(?:{_TEN_MEMBER_AXIS})"
    rf"|vocabular(?:y|ies)\s+(?:is|are)\s+{_QUALIFIER}(?:{_WAVELENGTH_AXIS})",
    re.IGNORECASE,
)

#: Phrasings that MUST trip :data:`AXIS_IDENTITY`. The first two are the
#: literal strings this repo has shipped or still ships; ``M7`` (the third)
#: is the capitalised twin that an earlier, case-sensitive revision of this
#: gate let through 5/5 — and is character-for-character the description on
#: ``creek_mcp/api/models.py``'s ``ontology_version`` field.
WRONG_PHRASINGS: Final[tuple[str, ...]] = (
    "the shared vocabulary — Adepthood Aspects = Creek APTITUDE frequencies = "
    "Archetypal Wavelength phases — is defined canonically in",
    "it is the shared APTITUDE-frequency / Wavelength-phase vocabulary, not the "
    "wire shape",
    "Shared APTITUDE frequency / Wavelength phase vocabulary.",
    "Pinned version of the shared frequency/phase vocabulary.",
    "the shared vocabulary is the Archetypal Wavelength Modes",
    "the shared vocabulary — Adepthood Aspects = Creek APTITUDE frequencies "
    "= Wavelength Modes — has ten members",
)

#: Phrasings that MUST NOT trip :data:`AXIS_IDENTITY`. Naming both axes as
#: separate axes is the *correct* statement of what ``ONTOLOGY_VERSION``
#: covers, and the gate exists to permit it — an earlier revision rejected
#: the second entry here, which is why the module docstring got narrowed to
#: a scope the rest of the module and the wire disagree with.
CORRECT_PHRASINGS: Final[tuple[str, ...]] = (
    "the shared vocabulary is Creek's APTITUDE frequency axis, defined "
    "canonically in the ontology prompt §6.1",
    "bump ONTOLOGY_VERSION when the shared classification vocabulary changes on "
    "either of its axes — the ten APTITUDE frequencies or the Archetypal "
    "Wavelength phases and Modes",
    "Pinned version of the shared APTITUDE-frequency and Archetypal-Wavelength "
    "vocabularies, versioned together.",
    "Neither Wavelength axis is that shared vocabulary. It has already produced "
    'wrong text in both repos — first as "= Wavelength phases", then as '
    '"= Wavelength Modes".',
    "the shared vocabulary is pinned to aptitude-wavelength/2026-05-23",
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

#: The sibling ADR's ruling, quoted verbatim from its Accepted text. The
#: MCP-contract ADR must carry this hedge rather than contradict it.
SIBLING_HEDGE: Final[str] = "numeric coincidence\nis not a semantic identity"

#: How this repo marks a cross-repo assertion it cannot substantiate.
UNVERIFIED_MARKER: Final[str] = "Unverified from this repo"

#: Anything that would publish a frequency colour on the wire.
COLOUR_FIELD: Final[re.Pattern[str]] = re.compile(r"\bcolou?r\b", re.IGNORECASE)


def _canonical_mode_names() -> set[str]:
    """Return the title-case names of the real, classified Modes."""
    return {mode.value.title() for mode in Mode if mode is not Mode.UNCLASSIFIED}


def _normalised(path: Path) -> str:
    """Read ``path`` with newlines folded, so claims can span lines."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def _section(text: str, heading: str) -> str:
    """Return one ``## `` section of :func:`_normalised` text, subsections included.

    Newlines are already folded by then, so the stop marker is the literal
    ``" ## "`` that a level-two heading becomes. ``" ### "`` does not contain
    it, which is what keeps a section's own subsections inside it.
    """
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = rest.find(" ## ")
    return rest if end == -1 else rest[:end]


@pytest.mark.parametrize("phrasing", WRONG_PHRASINGS)
def test_axis_identity_catches_every_known_wrong_phrasing(phrasing: str) -> None:
    """Every wrong wording this repo has shipped trips the gate.

    Including the capitalised one. ``creek_mcp/api/models.py`` opens its
    ``ontology_version`` description with *"Shared APTITUDE frequency /
    Wavelength phase vocabulary."*; a case-sensitive gate could not see
    it, so the string most likely to be re-introduced when #1537 edits
    that field was the one string the gate was blind to.
    """
    claims = SHARED_VOCABULARY_CLAIM.findall(phrasing)
    assert claims, f"no shared-vocabulary claim found in {phrasing!r}"
    assert any(AXIS_IDENTITY.search(claim) for claim in claims), (
        f"{phrasing!r} equates the axes but the gate did not see it"
    )


@pytest.mark.parametrize("phrasing", CORRECT_PHRASINGS)
def test_axis_identity_admits_the_correct_broad_phrasings(phrasing: str) -> None:
    """Naming both axes as *separate* axes is allowed, and stays allowed.

    ``ONTOLOGY_VERSION`` versions both the frequencies and the Archetypal
    Wavelength; a gate that forbade mentioning the Wavelength anywhere
    near the words "shared vocabulary" would force the module docstring
    to under-describe its own bump trigger — which is exactly what
    happened.
    """
    for claim in SHARED_VOCABULARY_CLAIM.findall(phrasing):
        assert not AXIS_IDENTITY.search(claim), (
            f"the gate rejects a correct broad phrasing: {claim!r}"
        )


def test_the_phrasing_corpora_are_not_empty() -> None:
    """Positive control: neither parametrised corpus has been emptied.

    An empty ``parametrize`` list is a silent skip, and a silent skip
    here is a green gate that checks nothing.
    """
    assert len(WRONG_PHRASINGS) >= 6
    assert len(CORRECT_PHRASINGS) >= 5


@pytest.mark.parametrize("filename", IDENTITY_DOCS)
def test_shared_vocabulary_claim_equates_no_wavelength_axis(filename: str) -> None:
    """No ADR equates a Wavelength axis with the shared vocabulary.

    The shared axis has ten colour-keyed members. The Modes (five) and
    the phases (six) are different axes with different cardinalities, so
    equating either one with it is a category error — the one these ADRs
    have now made twice, once per axis. Naming them alongside it as
    separate axes is fine and is not what this asserts.
    """
    text = _normalised(DECISIONS_DIR / filename)
    claims = SHARED_VOCABULARY_CLAIM.findall(text)
    assert claims, f"{filename} no longer states a shared-vocabulary claim to gate"
    for claim in claims:
        assert not AXIS_IDENTITY.search(claim), (
            f"{filename} equates a Wavelength axis with the shared "
            f"vocabulary: {claim!r}"
        )


def test_adr_mode_enumeration_invents_no_mode() -> None:
    """The MCP-contract ADR's §7.2 citation lists exactly the real Modes.

    Guards the specific regression: the ADR once cited §7.2 for a tenth
    Mode ("Clear Light's Be") that §7.2 does not define and
    :class:`creek.models.Mode` does not implement.
    """
    text = _normalised(DECISIONS_DIR / MCP_CONTRACT_ADR)
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


def test_contract_module_equates_no_wavelength_axis() -> None:
    """``creek_mcp.contract`` states the same identity correctly.

    The ADR names this module as the runtime source of the version
    strings, so the two must not disagree about what ``ONTOLOGY_VERSION``
    versions.
    """
    text = _normalised(CONTRACT_MODULE)
    claims = SHARED_VOCABULARY_CLAIM.findall(text)
    assert claims, "creek_mcp/contract.py no longer states the shared vocabulary"
    for claim in claims:
        assert not AXIS_IDENTITY.search(claim), (
            f"creek_mcp/contract.py equates a Wavelength axis with the shared "
            f"vocabulary: {claim!r}"
        )


def test_contract_module_bump_trigger_covers_both_axes() -> None:
    """The bump trigger names the Wavelength, and the module agrees with itself.

    Removing the false equation must not shrink the trigger: the
    published constant is ``aptitude-wavelength/…``, the wire field and
    the vendored ``CapabilitiesResponse`` schema both describe it as
    covering the Wavelength too, and a client that renegotiated only on
    frequency changes would miss a Mode or phase revision entirely. So
    the module docstring and the ``ONTOLOGY_VERSION`` docstring must both
    name both axes.
    """
    source = CONTRACT_MODULE.read_text(encoding="utf-8")
    module_doc, _, rest = source.partition('"""\n\nfrom __future__')
    constant_doc = rest[rest.index("ONTOLOGY_VERSION") :]
    for label, chunk in (("module docstring", module_doc), ("ONTOLOGY_VERSION", rest)):
        assert re.search(r"APTITUDE[\s-]*frequenc", chunk), (
            f"{label} no longer names the APTITUDE frequency axis"
        )
        assert re.search(r"Wavelength", chunk), (
            f"{label} narrows the ontology version to one axis; the constant "
            f"is 'aptitude-wavelength/…' and the wire says both"
        )
    assert "Wavelength" in constant_doc


def test_mcp_contract_adr_carries_the_sibling_adrs_hedge() -> None:
    """The draft ADR does not contradict the Accepted one about identity.

    ``2026-07-31-adepthood-http-application-api.md`` is **Accepted** and
    rules that the ten-frequency / ten-stage correspondence "is not a
    semantic identity". The MCP-contract ADR states the same
    correspondence, so it must carry that hedge rather than assert an
    unqualified equation — and must not argue the equation from shared
    cardinality, which is the inference the sibling refuses.
    """
    sibling = (DECISIONS_DIR / HTTP_API_ADR).read_text(encoding="utf-8")
    assert SIBLING_HEDGE in sibling, "the Accepted ADR's ruling has moved; re-anchor"

    adr = _normalised(DECISIONS_DIR / MCP_CONTRACT_ADR)
    ontology_section = _section(adr, "## Ontology version")
    assert "not a semantic identity" in ontology_section, (
        "the MCP-contract ADR states the frequency/Aspect/Stage correspondence "
        "without the Accepted sibling ADR's hedge"
    )
    no_cardinality_argument = "shared cardinality is not evidence of identity"
    assert no_cardinality_argument in ontology_section.lower(), (
        "the ADR must say outright that matching member counts do not "
        "establish identity; arguing the identity from ten-ness is the "
        "reasoning error this gate exists to keep out"
    )


def test_adepthood_side_claims_are_marked_unverified() -> None:
    """Cross-repo assertions this repo cannot substantiate are labelled.

    Nothing in this repository records Adepthood's Aspect or Stage
    definitions. Stating the correspondence is fine; stating it as though
    Creek's sources showed it is not.
    """
    adr = _normalised(DECISIONS_DIR / MCP_CONTRACT_ADR)
    ontology_section = _section(adr, "## Ontology version")
    assert UNVERIFIED_MARKER in ontology_section, (
        f"the MCP-contract ADR names Adepthood's Aspects/Stages without a "
        f"{UNVERIFIED_MARKER!r} marker"
    )


def test_identity_paragraph_cites_only_the_frequency_section() -> None:
    """The identity's canonical citation is §6.1, not "§6.1 and §7".

    §7 defines a different subject — the Archetypal Wavelength — so
    citing it for the *frequency* identity is the citation-level form of
    the same category error. The §7 reference belongs in the
    disambiguation paragraph that explains why neither Wavelength axis is
    in scope, and this asserts it is there and not here.
    """
    adr = _normalised(DECISIONS_DIR / MCP_CONTRACT_ADR)
    ontology_section = _section(adr, "## Ontology version")
    identity_paragraph = ontology_section.split(" ### ")[0]
    assert "§6.1" in identity_paragraph
    assert "§7" not in identity_paragraph, (
        "the identity's canonical citation still points at §7 (the Archetypal "
        "Wavelength), which defines a different axis"
    )
    assert "§7" in ontology_section.replace(identity_paragraph, ""), (
        "the §7 reference was dropped rather than moved; the disambiguation "
        "paragraph needs it to say what the Wavelength axes are"
    )


def test_colour_join_guidance_is_an_open_question_with_its_limits() -> None:
    """Colour-join advice sits in Open questions, sourced and bounded.

    Joining on the colour designation is only sound advice if the key
    exists. It is on neither side's wire, and this repo records only
    Creek's half of the map, so the ADR carries it as an open question
    with both limits stated and §6.1 — the sole in-repo colour source —
    cited, rather than as normative interop guidance.
    """
    adr = _normalised(DECISIONS_DIR / MCP_CONTRACT_ADR)
    open_questions = _section(adr, "## Open questions (resolve before `Accepted`)")
    assert "colour" in open_questions.lower(), (
        "the colour join key is stated as guidance somewhere other than Open "
        "questions, or has been dropped entirely"
    )
    assert "§6.1" in open_questions, (
        "the colour-join entry does not cite §6.1, the only in-repo source for "
        "the colour designations"
    )
    for limit in ("on neither side's wire", "not recorded\nanywhere in this repo"):
        assert re.sub(r"\s+", " ", limit) in open_questions, (
            f"the colour-join entry omits the limit {limit!r}"
        )

    identity_section = _section(adr, "## Ontology version")
    assert "join on the colour" not in identity_section.lower(), (
        "normative colour-join guidance is back in the identity section"
    )


def test_no_wire_surface_publishes_a_frequency_colour() -> None:
    """Positive control for the ADR's "colour is on neither wire" limit.

    If a colour designation ever *is* published — as a ``/v1`` field or in
    a vendored schema — the open question above becomes answerable and its
    stated limit becomes false, so this fails and forces the ADR to be
    revisited in the same change.
    """
    assert not COLOUR_FIELD.search(API_MODELS_MODULE.read_text(encoding="utf-8"))

    schemas = sorted(CONTRACT_SCHEMA_DIR.glob("*.schema.json"))
    assert schemas, f"no vendored schemas found under {CONTRACT_SCHEMA_DIR}"
    for schema in schemas:
        payload = json.loads(schema.read_text(encoding="utf-8"))
        assert not COLOUR_FIELD.search(json.dumps(payload)), (
            f"{schema.name} publishes a colour designation; the colour-join "
            f"open question in {MCP_CONTRACT_ADR} says it is on neither wire"
        )
