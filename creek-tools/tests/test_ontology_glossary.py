"""Tests for the bespoke-ontology-term glossary registry and gloss detector.

Four concerns are pinned here:

* the registry is a *drift-guarded* single source of truth — every
  non-sentinel ``Frequency`` / ``Phase`` / ``Mode`` member is matched to the
  term that declares it as its ``source``, by **identity**, so a new enum
  member without a gloss fails the build. Identity is load-bearing: all three
  taxonomies are ``StrEnum`` classes, so their members compare *and hash* as
  bare strings across classes, and all three ``UNCLASSIFIED`` sentinels are the
  same string ``"unclassified"``. A string-resolvability guard therefore lets
  one enum's term silently "cover" another enum's member — which is exactly
  how the sentinel slipped through before #1343;
* every surface form the registry holds is a form a draft actually writes: no
  bare lower-case wire values (``rising``, ``bottoming_out``), no lower-cased
  shadow keys, and no term silently overwritten by a duplicate key;
* the gloss prompt-steer is present in both ``## Ask`` builders (the draft and
  voice surfaces), mirroring how the no-fabrication / no-provenance steers are
  pinned;
* the conservative ``unglossed_jargon`` detector flags a first-mention bespoke
  term with no nearby gloss, never re-flags a second occurrence, and does not
  over-flag a glossed term, an ordinary capitalised word, or an ordinary
  lower-case English verb (``rising``, ``absorb``, ``collaborate``,
  ``express``).
"""

from __future__ import annotations

import pytest

from creek.generate.jargon import detect_unglossed_jargon
from creek.generate.ontology_glossary import (
    GLOSS_STEER,
    OntologyTerm,
    iter_ontology_terms,
    ontology_term_registry,
)
from creek.models import Frequency, Mode, Phase


class TestRegistryDriftGuard:
    """Every real taxonomy member — and only those — carries a gloss-seed entry."""

    def test_every_non_sentinel_member_resolves_to_its_own_term(self) -> None:
        """Each real taxonomy member owns a term of the matching category.

        This one guard replaces the three per-enum guards it grew out of
        (``test_every_{frequency,phase,mode}_member_has_a_registry_entry``),
        which asserted only that ``member.value in registry`` — mere *string*
        resolvability. Because ``Frequency`` / ``Phase`` / ``Mode`` are all
        ``StrEnum`` classes, that assertion could not fail for the reason it
        existed: ``Phase.UNCLASSIFIED`` "passed" by resolving to the **Mode**
        term registered under the same ``"unclassified"`` string, and the
        frequency sentinel's own term had already been overwritten in the
        registry. A member could be entirely unglossed and still pass.

        The identity-keyed form cannot be satisfied by an unrelated term: each
        member must be the declared ``source`` of a term whose ``category``
        matches its taxonomy. Sentinels are skipped by ``is`` — never by
        equality, which would also match the other two enums' sentinels.
        """
        by_source: dict[tuple[str, str], OntologyTerm] = {
            (type(term.source).__name__, term.source.value): term
            for term in iter_ontology_terms()
            if term.source is not None
        }
        expected: list[tuple[tuple[str, str], str]] = []
        for freq in Frequency:
            if freq is not Frequency.UNCLASSIFIED:
                expected.append(((Frequency.__name__, freq.value), "frequency"))
        for phase in Phase:
            if phase is not Phase.UNCLASSIFIED:
                expected.append(((Phase.__name__, phase.value), "phase"))
        for mode in Mode:
            if mode is not Mode.UNCLASSIFIED:
                expected.append(((Mode.__name__, mode.value), "mode"))

        for key, category in expected:
            assert key in by_source, f"no gloss-seed term sourced from {key}"
            assert by_source[key].category == category

    def test_sentinel_members_are_not_glossary_terms(self) -> None:
        """The three ``UNCLASSIFIED`` sentinels earn no term and no key.

        A sentinel is a wire value meaning "we could not classify this", not
        vocabulary a draft ever writes, so it must not reach the registry the
        jargon detector scans with. Each sentinel is checked with ``is``: the
        three are equal as strings, so an equality test would pass on the
        wrong enum's member.
        """
        for term in iter_ontology_terms():
            assert term.source is not Frequency.UNCLASSIFIED
            assert term.source is not Phase.UNCLASSIFIED
            assert term.source is not Mode.UNCLASSIFIED
        registry = ontology_term_registry()
        assert "Unclassified" not in registry
        assert "unclassified" not in registry

    def test_registry_has_no_lower_cased_shadow_keys(self) -> None:
        """Only exact surface forms are registered, never lower-cased shadows.

        The shadow keys were what made the detector flag ordinary English:
        ``"rising"`` / ``"express"`` / ``"absorb"`` / ``"collaborate"`` are
        common verbs, and the detector matches keys case-sensitively (#1343).
        """
        registry = ontology_term_registry()
        for shadow in (
            "rising",
            "express",
            "absorb",
            "collaborate",
            "teal",
            "aptitude",
        ):
            assert shadow not in registry, f"{shadow!r} is a lower-cased shadow key"

    def test_no_term_is_lost_to_a_key_collision(self) -> None:
        """Every derived term survives into the registry, unshadowed.

        Two terms writing the same key means the first is unreachable — it can
        never be glossed or detected. Counted against ``iter_ontology_terms()``
        rather than a pinned number so adding a term cannot fail this test for
        the wrong reason.
        """
        registry = ontology_term_registry()
        assert len(set(registry.values())) == len(iter_ontology_terms())

    def test_every_term_carries_a_nonempty_gloss_seed(self) -> None:
        """No registered term may have a blank gloss-seed."""
        for term in iter_ontology_terms():
            assert isinstance(term, OntologyTerm)
            assert term.gloss_seed.strip(), f"empty gloss-seed for {term.label!r}"

    def test_named_concepts_are_registered(self) -> None:
        """The named bespoke concepts are registered under their exact form."""
        registry = ontology_term_registry()
        for concept in (
            "APTITUDE",
            "Whole Adept",
            "Archetypal Wavelength",
            "non-dual substrate",
        ):
            assert concept in registry
            assert registry[concept].category == "concept"

    def test_altitude_terms_are_registered(self) -> None:
        """The developmental altitudes (Teal, Ultraviolet) keep their exact form."""
        registry = ontology_term_registry()
        for altitude in ("Teal", "Ultraviolet"):
            assert altitude in registry
            assert registry[altitude].category == "altitude"


class TestRegistryMemoization:
    """The registry is built once and reused across calls."""

    def test_registry_is_memoized(self) -> None:
        """Two calls return the same cached object (identity)."""
        assert ontology_term_registry() is ontology_term_registry()


class TestSurfaceFormInvariant:
    """A term's surface forms must be prose a draft writes, not wire values.

    ``OntologyTerm`` is the boundary the detector trusts. An all-lower-case
    single word is a serialised enum value (``"rising"``, ``"bottoming_out"``),
    indistinguishable from ordinary English, so constructing a term around one
    is rejected at the source rather than filtered downstream (#1343).
    """

    def test_a_bare_lower_case_label_is_rejected(self) -> None:
        """A single all-lower-case word as the ``label`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="rising"):
            OntologyTerm(label="rising", category="phase", gloss_seed="x")

    def test_a_bare_lower_case_alias_is_rejected(self) -> None:
        """A bare wire value hidden in ``aliases`` is rejected just as hard.

        Separate from the label case so the validation loop is exercised on
        both arms: a legal label must not excuse an illegal alias.
        """
        with pytest.raises(ValueError, match="rising"):
            OntologyTerm(
                label="Rising",
                category="phase",
                gloss_seed="x",
                aliases=("rising",),
            )

    def test_a_multi_word_lower_case_phrase_is_allowed(self) -> None:
        """A lower-case *phrase* is real vocabulary and constructs fine."""
        term = OntologyTerm(
            label="non-dual substrate",
            category="concept",
            gloss_seed="the ground where self and world stop being two",
        )
        assert term.label == "non-dual substrate"


class TestRegistryCollisionDetection:
    """Building the registry refuses to silently drop a term.

    ``_build_registry`` is the pure, un-memoised builder behind
    ``ontology_term_registry()``, so these call it directly — no cache to
    clear, no monkeypatching of the module.
    """

    def test_two_terms_sharing_a_surface_form_raise(self) -> None:
        """Two terms claiming one surface form raise, naming the clash."""
        from creek.generate.ontology_glossary import _build_registry

        terms = [
            OntologyTerm(
                label="Peaking",
                category="phase",
                gloss_seed="the full expression",
            ),
            OntologyTerm(
                label="Peaking",
                category="mode",
                gloss_seed="a wholly different sense",
            ),
        ]

        with pytest.raises(ValueError) as excinfo:
            _build_registry(terms)

        assert "Peaking" in str(excinfo.value)

    def test_two_terms_claiming_the_same_source_raise(self) -> None:
        """Two terms deriving from one taxonomy member raise, naming it."""
        from creek.generate.ontology_glossary import _build_registry

        terms = [
            OntologyTerm(
                label="Rising",
                category="phase",
                gloss_seed="energy gathering",
                source=Phase.RISING,
            ),
            OntologyTerm(
                label="Ascending",
                category="phase",
                gloss_seed="energy gathering",
                source=Phase.RISING,
            ),
        ]

        with pytest.raises(ValueError) as excinfo:
            _build_registry(terms)

        assert "rising" in str(excinfo.value).lower()


class TestGlossSteerPinned:
    """The gloss instruction is present in both ``## Ask`` builders."""

    def test_steer_text_mentions_first_mention_and_gloss_once(self) -> None:
        """The steer constant names the first-mention, gloss-once contract."""
        lowered = GLOSS_STEER.lower()
        assert "first time" in lowered
        assert "once" in lowered

    def test_steer_names_mode_not_mood(self) -> None:
        """The steer names ``Mode`` — the concept the registry/detector covers."""
        assert "Mode" in GLOSS_STEER
        assert "Mood" not in GLOSS_STEER

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

    def test_ordinary_lower_case_words_are_not_flagged(self) -> None:
        """Ordinary English verbs are prose, not bespoke jargon.

        ``jargon.py``'s module docstring already promises this — "an ordinary
        lower-case word (``rising`` the verb) … never trips the scan" — but no
        test stood behind the claim, and the lower-cased shadow keys in the
        registry made it false for every phase and mode value (#1343).
        """
        body = (
            "The bread was rising in the pan while I waited. I could not "
            "absorb what he said. We collaborate on Tuesdays and express "
            "ourselves badly."
        )
        assert detect_unglossed_jargon(body) == []

    def test_a_wavelength_essay_with_the_verb_rising_is_not_flagged(self) -> None:
        """The issue's literal acceptance criterion: one sentence, no findings."""
        assert detect_unglossed_jargon("The bread was rising in the pan.") == []
