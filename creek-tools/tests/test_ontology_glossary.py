"""Tests for the bespoke-ontology-term glossary registry and gloss detector.

Three concerns are pinned here:

* the registry is a *drift-guarded* single source of truth — every
  ``Frequency`` / ``Phase`` / ``Mode`` enum member has a gloss-seed entry, so a
  new enum member without a gloss fails the build;
* the gloss prompt-steer is present in both ``## Ask`` builders (the draft and
  voice surfaces), mirroring how the no-fabrication / no-provenance steers are
  pinned;
* the conservative ``unglossed_jargon`` detector flags a first-mention bespoke
  term with no nearby gloss, never re-flags a second occurrence, and does not
  over-flag a glossed term or an ordinary capitalised word.
"""

from __future__ import annotations

from creek.author.checks import detect_unglossed_jargon
from creek.generate.ontology_glossary import (
    GLOSS_STEER,
    OntologyTerm,
    iter_ontology_terms,
    ontology_term_registry,
)
from creek.models import Frequency, Mode, Phase


class TestRegistryDriftGuard:
    """Every taxonomy enum member must carry a gloss-seed entry."""

    def test_every_frequency_member_has_a_registry_entry(self) -> None:
        """Each ``Frequency`` member resolves to a registered term."""
        registry = ontology_term_registry()
        for freq in Frequency:
            assert freq.value in registry, f"no gloss-seed for {freq!r}"

    def test_every_phase_member_has_a_registry_entry(self) -> None:
        """Each ``Phase`` member resolves to a registered term."""
        registry = ontology_term_registry()
        for phase in Phase:
            assert phase.value in registry, f"no gloss-seed for {phase!r}"

    def test_every_mode_member_has_a_registry_entry(self) -> None:
        """Each ``Mode`` member resolves to a registered term."""
        registry = ontology_term_registry()
        for mode in Mode:
            assert mode.value in registry, f"no gloss-seed for {mode!r}"

    def test_every_term_carries_a_nonempty_gloss_seed(self) -> None:
        """No registered term may have a blank gloss-seed."""
        for term in iter_ontology_terms():
            assert isinstance(term, OntologyTerm)
            assert term.gloss_seed.strip(), f"empty gloss-seed for {term.label!r}"

    def test_named_concepts_are_registered(self) -> None:
        """The named bespoke concepts carry gloss seeds too."""
        registry = ontology_term_registry()
        for concept in (
            "APTITUDE",
            "Whole Adept",
            "Archetypal Wavelength",
            "non-dual substrate",
        ):
            assert concept.lower() in registry

    def test_altitude_terms_are_registered(self) -> None:
        """The developmental altitudes (e.g. Teal, Ultraviolet) are registered."""
        registry = ontology_term_registry()
        for altitude in ("Teal", "Ultraviolet"):
            assert altitude.lower() in registry


class TestGlossSteerPinned:
    """The gloss instruction is present in both ``## Ask`` builders."""

    def test_steer_text_mentions_first_mention_and_gloss_once(self) -> None:
        """The steer constant names the first-mention, gloss-once contract."""
        lowered = GLOSS_STEER.lower()
        assert "first time" in lowered
        assert "once" in lowered

    def test_draft_ask_carries_gloss_steer_in_every_mode(self) -> None:
        """Every ``_compose_ask_section`` variant carries the gloss steer."""
        from creek.generate.drafts import _compose_ask_section
        from creek.generate.mining import IdeaSeed, MiningStrategy

        idea = IdeaSeed(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            title="Christ",
            source_fragments=("frag-a",),
            threads=(),
            eddies=(),
            frequency_affinity=(),
            brief_description="A draft about the pattern.",
            score=0.8,
        )
        for kwargs in (
            {"per_dimension": False},
            {"per_dimension": True},
            {"per_dimension": False, "twist": True},
        ):
            section = _compose_ask_section(idea, **kwargs)
            assert _GLOSS_PHRASE in section

    def test_voice_ask_carries_gloss_steer(self) -> None:
        """The voice agent's ``## Ask`` block carries the gloss steer."""
        from creek.author.voice import _ask_section

        section = _ask_section("What is Christ?", None, None)
        assert _GLOSS_PHRASE in section


_GLOSS_PHRASE = "weave in a brief plain-English gloss"


class TestUnglossedJargonDetector:
    """The conservative first-mention gloss detector."""

    def test_flags_each_unglossed_first_mention(self) -> None:
        """Three bespoke terms with no nearby gloss each raise a finding."""
        body = (
            "The work moves toward Ultraviolet. By F8 the pattern is clear. "
            "Everything resolves at Peaking, eventually."
        )
        findings = detect_unglossed_jargon(body)
        flagged = {f.term for f in findings}
        assert flagged == {"Ultraviolet", "F8", "Peaking"}
        assert all(f.severity == "MID" for f in findings)

    def test_glossed_first_mention_is_not_flagged(self) -> None:
        """An appositive gloss on first mention suppresses the finding."""
        body = (
            "The work moves toward Ultraviolet — the altitude where your own "
            "will quiets enough that something larger acts through you. "
            "By F8, my true self speaking, the deep intuition that yields "
            "alignment, the pattern is clear. Everything resolves at Peaking "
            "(the full expression where output reaches its maximum), eventually."
        )
        findings = detect_unglossed_jargon(body)
        assert findings == []

    def test_only_first_mention_is_flagged(self) -> None:
        """A second occurrence of a term is never separately flagged."""
        body = (
            "We rise toward Ultraviolet here. Later, Ultraviolet returns and "
            "Ultraviolet stays."
        )
        findings = detect_unglossed_jargon(body)
        terms = [f.term for f in findings]
        assert terms.count("Ultraviolet") == 1

    def test_which_is_clause_counts_as_a_gloss(self) -> None:
        """A 'which is' explanatory clause suppresses the finding."""
        body = "The arc bends to F8, which is the voice of my own higher self."
        assert detect_unglossed_jargon(body) == []

    def test_ordinary_capitalised_word_is_not_flagged(self) -> None:
        """A non-bespoke capitalised word raises nothing (false-positive guard)."""
        body = "On Tuesday I walked to London and thought about Everything."
        assert detect_unglossed_jargon(body) == []

    def test_empty_body_raises_nothing(self) -> None:
        """An empty body produces no findings."""
        assert detect_unglossed_jargon("") == []

    def test_gloss_seed_keyword_nearby_suppresses(self) -> None:
        """A gloss-seed keyword near the first mention counts as explanation."""
        body = (
            "He reaches Peaking: the full creative expression pours out, "
            "maximum output, abundance everywhere."
        )
        assert detect_unglossed_jargon(body) == []
