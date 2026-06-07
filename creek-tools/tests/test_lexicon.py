"""Tests for creek.generate.lexicon — glossary generation.

Covers :class:`LexiconGenerator` implementing Section 11.3 of the Creek
Ontology: build a living glossary at ``07-Voice/Lexicon/`` containing
coined terms, recurring metaphors with contexts, distinctive phrases,
and terms borrowed from specific traditions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.lexicon import (
    TRADITION_GLOSSARIES,
    BorrowedTermEntry,
    CoinedTermEntry,
    DistinctivePhraseEntry,
    Lexicon,
    LexiconContext,
    LexiconGenerator,
    MetaphorInventory,
    generate_lexicon,
)
from creek.generate.voice import (
    Exemplar,
    MetaphorFamily,
    ParagraphMetrics,
    PunctuationHabits,
    RhetoricalMoves,
    SentenceMetrics,
    VocabularyFingerprint,
    VoicePatterns,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Helpers ----


def _make_fragment(frag_id: str, title: str = "Untitled") -> Fragment:
    """Build a minimal valid fragment for lexicon tests."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        ingested=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
        ),
        voice=VoiceClassification(
            voice_register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        ),
        privacy_tier=PrivacyTier.PERSONAL,
    )


def _exemplar(frag_id: str, body: str) -> Exemplar:
    """Build an Exemplar with a default fragment and the given body."""
    return Exemplar(fragment=_make_fragment(frag_id), body=body)


def _seed_exemplar_file(vault: Path, frag_id: str, body: str) -> None:
    """Write a qualifying voice-exemplar fragment file under the vault."""
    fragments_dir = vault / "01-Fragments" / "Journal"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content=body,
        **_make_fragment(frag_id).model_dump(mode="json"),
    )
    (fragments_dir / f"{frag_id}.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )


def test_generate_lexicon_writes_populated_glossary(tmp_path: Path) -> None:
    """generate_lexicon collects vault exemplars and writes a filled glossary.

    Bodies carry Buddhist tradition keywords (``dharma`` / ``karma``) and a
    twice-repeated phrase so the borrowed-term and distinctive-phrase
    inventories are non-empty (#580).
    """
    _seed_exemplar_file(
        tmp_path,
        "ex-1",
        "The dharma teaches that the river flows toward the sea; the river flows on.",
    )
    _seed_exemplar_file(
        tmp_path,
        "ex-2",
        "With karma in mind, the river flows again and the river flows still.",
    )

    lexicon, paths = generate_lexicon(tmp_path)

    assert lexicon is not None
    glossary = tmp_path / "07-Voice" / "Lexicon" / "glossary.md"
    assert glossary.exists()
    assert glossary == paths[0]
    text = glossary.read_text(encoding="utf-8").lower()
    assert "voice lexicon" in text
    # The Buddhist borrowed terms were detected — a non-empty glossary section.
    assert "dharma" in text


def test_generate_lexicon_empty_vault_returns_none(tmp_path: Path) -> None:
    """A vault with no qualifying exemplars yields (None, []) and writes nothing."""
    (tmp_path / "01-Fragments").mkdir(parents=True)

    lexicon, paths = generate_lexicon(tmp_path)

    assert lexicon is None
    assert paths == []
    assert not (tmp_path / "07-Voice" / "Lexicon").exists()


def _empty_patterns() -> VoicePatterns:
    """Build a VoicePatterns with all-zero structural metrics."""
    return VoicePatterns(
        sentence_metrics=SentenceMetrics(
            average_length=0.0,
            short_count=0,
            medium_count=0,
            long_count=0,
            fragment_count=0,
            question_frequency=0.0,
            total_sentences=0,
        ),
        paragraph_metrics=ParagraphMetrics(
            average_sentences_per_paragraph=0.0,
        ),
        transitions=(),
        metaphor_families=(),
        rhetorical_moves=RhetoricalMoves(
            self_deprecation_count=0,
            paradox_count=0,
            callback_count=0,
        ),
        vocabulary=VocabularyFingerprint(
            distinctive_words=(),
            coined_terms=(),
            recurring_phrases=(),
        ),
        punctuation=PunctuationHabits(
            em_dash_frequency=0.0,
            ellipsis_frequency=0.0,
            parenthetical_frequency=0.0,
            exclamation_frequency=0.0,
        ),
    )


def _patterns_with_water_metaphor() -> VoicePatterns:
    """Build VoicePatterns with a single ``water`` metaphor family."""
    base = _empty_patterns()
    return VoicePatterns(
        sentence_metrics=base.sentence_metrics,
        paragraph_metrics=base.paragraph_metrics,
        transitions=base.transitions,
        metaphor_families=(
            MetaphorFamily(
                domain="water",
                matches=("creek", "flow"),
                example_sentences=("The creek flows downstream.",),
            ),
        ),
        rhetorical_moves=base.rhetorical_moves,
        vocabulary=base.vocabulary,
        punctuation=base.punctuation,
    )


# ---- Fixtures ----


@pytest.fixture()
def generator() -> LexiconGenerator:
    """A LexiconGenerator with a small reference word set for coinage tests."""
    reference = frozenset(
        {
            "the",
            "and",
            "of",
            "a",
            "is",
            "that",
            "we",
            "it",
            "be",
            "this",
            "have",
            "not",
            "for",
            "to",
            "in",
            "on",
            "with",
            "as",
            "are",
            "at",
            "by",
            "an",
            "about",
            "into",
            "through",
            "word",
            "words",
            "day",
            "way",
            "sense",
            "thing",
            "part",
            "people",
            "world",
            "life",
            "idea",
            "good",
            "see",
            "seen",
            "make",
            "made",
            "know",
            "known",
            "take",
            "takes",
            "said",
            "say",
            "says",
            "thought",
            "think",
            "thinks",
            "feel",
            "feels",
            "found",
            "find",
            "finds",
            "walk",
            "walks",
            "road",
            "between",
            "creek",
            "flow",
            "flows",
            "downstream",
            "gentle",
            "water",
        },
    )
    return LexiconGenerator(reference_words=reference)


@pytest.fixture()
def empty_patterns() -> VoicePatterns:
    """VoicePatterns with zeroed metrics and no metaphors."""
    return _empty_patterns()


# ---- Module surface ----


class TestModuleSurface:
    """Public exports and default constants."""

    def test_tradition_glossaries_contains_buddhist(self) -> None:
        """Default glossary includes buddhist keyword set."""
        assert "buddhist" in TRADITION_GLOSSARIES
        buddhist = TRADITION_GLOSSARIES["buddhist"]
        for term in ("dharma", "sangha", "anatta"):
            assert term in buddhist

    def test_tradition_glossaries_contains_spiral_dynamics(self) -> None:
        """Default glossary includes spiral dynamics keyword set."""
        assert "spiral_dynamics" in TRADITION_GLOSSARIES

    def test_tradition_glossaries_contains_recovery(self) -> None:
        """Default glossary includes recovery keyword set."""
        assert "recovery" in TRADITION_GLOSSARIES


# ---- Coined terms ----


class TestCoinedTerms:
    """Detection of coined terms via reference-set absence + repetition."""

    def test_coined_term_detected_when_repeated(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Word absent from reference set + 3+ uses is a coinage."""
        exemplars = [
            _exemplar("frag-001", "I have coined polygnosticism as a stance."),
            _exemplar("frag-002", "Polygnosticism honours many ways of knowing."),
            _exemplar("frag-003", "The polygnosticism I advocate is pragmatic."),
        ]
        lexicon = generator.build_lexicon(exemplars, empty_patterns)
        terms = {entry.term for entry in lexicon.coined_terms}
        assert "polygnosticism" in terms

    def test_coined_term_below_threshold_excluded(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Word appearing fewer than min_coinage_occurrences is excluded."""
        exemplars = [
            _exemplar("frag-001", "I tried polygnosticism once."),
            _exemplar("frag-002", "Polygnosticism is only used twice here."),
        ]
        lexicon = generator.build_lexicon(exemplars, empty_patterns)
        terms = {entry.term for entry in lexicon.coined_terms}
        assert "polygnosticism" not in terms

    def test_coined_term_entry_has_count(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """CoinedTermEntry records total usage count across fragments."""
        exemplars = [
            _exemplar("frag-001", "Polygnosticism once. Polygnosticism twice."),
            _exemplar("frag-002", "Again polygnosticism."),
        ]
        lexicon = generator.build_lexicon(exemplars, empty_patterns)
        entry = next(e for e in lexicon.coined_terms if e.term == "polygnosticism")
        assert entry.count == 3

    def test_coined_term_entry_has_contexts(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Each coined term records example contexts with fragment IDs."""
        exemplars = [
            _exemplar("frag-001", "I have coined polygnosticism as a stance."),
            _exemplar("frag-002", "Polygnosticism honours many ways of knowing."),
            _exemplar("frag-003", "The polygnosticism I advocate is pragmatic."),
        ]
        lexicon = generator.build_lexicon(exemplars, empty_patterns)
        entry = next(e for e in lexicon.coined_terms if e.term == "polygnosticism")
        assert len(entry.contexts) >= 1
        fragment_ids = {ctx.fragment_id for ctx in entry.contexts}
        assert fragment_ids & {"frag-001", "frag-002", "frag-003"}

    def test_no_coined_terms_without_reference(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """A generator with no reference words returns no coinages."""
        gen = LexiconGenerator()
        exemplars = [
            _exemplar("frag-001", "polygnosticism polygnosticism polygnosticism."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        assert lexicon.coined_terms == ()


# ---- Metaphor inventory ----


class TestMetaphorInventory:
    """Rich metaphor usage collection, linked to fragments."""

    def test_metaphor_inventory_builds_from_patterns(
        self,
        generator: LexiconGenerator,
    ) -> None:
        """Each metaphor family in patterns becomes a MetaphorInventory."""
        exemplars = [
            _exemplar("frag-001", "The creek flows through the valley."),
        ]
        patterns = _patterns_with_water_metaphor()
        lexicon = generator.build_lexicon(exemplars, patterns)
        domains = {inv.domain for inv in lexicon.metaphors}
        assert "water" in domains

    def test_metaphor_usage_links_to_fragment(
        self,
        generator: LexiconGenerator,
    ) -> None:
        """A metaphor usage records the fragment id of its source."""
        exemplars = [
            _exemplar("frag-alpha", "The creek of thought flows into a pool."),
        ]
        patterns = _patterns_with_water_metaphor()
        lexicon = generator.build_lexicon(exemplars, patterns)
        water = next(inv for inv in lexicon.metaphors if inv.domain == "water")
        assert any(u.fragment_id == "frag-alpha" for u in water.usages)

    def test_metaphor_usage_captures_surrounding_sentence(
        self,
        generator: LexiconGenerator,
    ) -> None:
        """A metaphor usage includes the sentence that contains the match."""
        exemplars = [
            _exemplar(
                "frag-beta",
                "A short opener. The creek flows gently downstream. End.",
            ),
        ]
        patterns = _patterns_with_water_metaphor()
        lexicon = generator.build_lexicon(exemplars, patterns)
        water = next(inv for inv in lexicon.metaphors if inv.domain == "water")
        sentences = {u.sentence for u in water.usages}
        assert any("creek flows" in s for s in sentences)

    def test_metaphor_inventory_preserves_keywords(
        self,
        generator: LexiconGenerator,
    ) -> None:
        """The MetaphorInventory carries the matched keywords."""
        exemplars = [
            _exemplar("frag-001", "The creek flows downstream."),
        ]
        patterns = _patterns_with_water_metaphor()
        lexicon = generator.build_lexicon(exemplars, patterns)
        water = next(inv for inv in lexicon.metaphors if inv.domain == "water")
        assert set(water.keywords) == {"creek", "flow"}


# ---- Distinctive phrases ----


class TestDistinctivePhrases:
    """N-gram extraction (bigrams through 4-grams) with recurrence."""

    def test_repeated_bigram_captured(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """A bigram repeated across fragments appears as a distinctive phrase."""
        gen = LexiconGenerator(min_phrase_occurrences=2)
        exemplars = [
            _exemplar(
                "frag-001",
                "The creek of thought flows. The creek of thought returns.",
            ),
            _exemplar("frag-002", "Again the creek of thought appears."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        phrases = {entry.phrase for entry in lexicon.distinctive_phrases}
        assert "creek of thought" in phrases

    def test_phrase_count_is_recorded(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Each distinctive phrase records its occurrence count."""
        gen = LexiconGenerator(min_phrase_occurrences=2)
        exemplars = [
            _exemplar(
                "frag-001",
                "The creek of thought flows. The creek of thought returns.",
            ),
            _exemplar("frag-002", "Again the creek of thought appears."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        entry = next(
            e for e in lexicon.distinctive_phrases if e.phrase == "creek of thought"
        )
        assert entry.count >= 3

    def test_phrase_below_threshold_excluded(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Phrases below min_phrase_occurrences are excluded."""
        gen = LexiconGenerator(min_phrase_occurrences=3)
        exemplars = [
            _exemplar("frag-001", "A rare phrase never repeated here."),
            _exemplar("frag-002", "Another single occurrence of something."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        # None of these phrases repeat 3 times, so distinctive_phrases is empty.
        assert lexicon.distinctive_phrases == ()


# ---- Borrowed terms ----


class TestBorrowedTerms:
    """Detection of tradition-specific terms via glossary matching."""

    def test_buddhist_term_detected(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """A buddhist term like ``dharma`` is detected and attributed."""
        gen = LexiconGenerator()
        exemplars = [
            _exemplar("frag-001", "The dharma asks for presence, not perfection."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        terms = {e.term: e.tradition for e in lexicon.borrowed_terms}
        assert terms.get("dharma") == "buddhist"

    def test_borrowed_term_count_and_contexts(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Borrowed term entry records count and a fragment-linked context."""
        gen = LexiconGenerator()
        exemplars = [
            _exemplar("frag-001", "Dharma walks in a small sangha."),
            _exemplar("frag-002", "Again dharma calls."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        dharma = next(e for e in lexicon.borrowed_terms if e.term == "dharma")
        assert dharma.count >= 2
        assert any(ctx.fragment_id == "frag-001" for ctx in dharma.contexts)

    def test_custom_tradition_glossary(
        self,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Callers can supply their own tradition glossaries."""
        custom = {"sufi": frozenset({"fana", "baqa"})}
        gen = LexiconGenerator(tradition_glossaries=custom)
        exemplars = [
            _exemplar("frag-001", "The doctrine of fana dissolves the self."),
        ]
        lexicon = gen.build_lexicon(exemplars, empty_patterns)
        terms = {e.term: e.tradition for e in lexicon.borrowed_terms}
        assert terms.get("fana") == "sufi"


# ---- Integration: build_lexicon ----


class TestBuildLexicon:
    """End-to-end build_lexicon invariants."""

    def test_returns_lexicon_type(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """build_lexicon returns a Lexicon instance."""
        lexicon = generator.build_lexicon(
            [_exemplar("frag-001", "A sentence.")],
            empty_patterns,
        )
        assert isinstance(lexicon, Lexicon)

    def test_empty_input_returns_empty_lexicon(
        self,
        generator: LexiconGenerator,
        empty_patterns: VoicePatterns,
    ) -> None:
        """Empty exemplars produce an empty Lexicon."""
        lexicon = generator.build_lexicon([], empty_patterns)
        assert lexicon.coined_terms == ()
        assert lexicon.metaphors == ()
        assert lexicon.distinctive_phrases == ()
        assert lexicon.borrowed_terms == ()


# ---- Glossary writing ----


class TestWriteLexicon:
    """Persistence of the glossary to 07-Voice/Lexicon/glossary.md."""

    def _sample_lexicon(self) -> Lexicon:
        return Lexicon(
            coined_terms=(
                CoinedTermEntry(
                    term="polygnosticism",
                    count=5,
                    contexts=(
                        LexiconContext(
                            fragment_id="frag-001",
                            sentence="Polygnosticism is a stance.",
                        ),
                    ),
                ),
            ),
            metaphors=(
                MetaphorInventory(
                    domain="water",
                    keywords=("creek", "flow"),
                    usages=(
                        LexiconContext(
                            fragment_id="frag-002",
                            sentence="The creek flows downstream.",
                        ),
                    ),
                ),
            ),
            distinctive_phrases=(
                DistinctivePhraseEntry(phrase="creek of thought", count=4),
            ),
            borrowed_terms=(
                BorrowedTermEntry(
                    term="dharma",
                    tradition="buddhist",
                    count=2,
                    contexts=(
                        LexiconContext(
                            fragment_id="frag-003",
                            sentence="The dharma asks for presence.",
                        ),
                    ),
                ),
            ),
        )

    def test_writes_file_at_expected_path(self, tmp_path: Path) -> None:
        """Glossary is written to 07-Voice/Lexicon/glossary.md."""
        gen = LexiconGenerator()
        target = gen.write_lexicon(self._sample_lexicon(), tmp_path)
        assert target == tmp_path / "07-Voice" / "Lexicon" / "glossary.md"
        assert target.is_file()

    def test_glossary_contains_all_sections(self, tmp_path: Path) -> None:
        """Output markdown includes the four required sections."""
        gen = LexiconGenerator()
        target = gen.write_lexicon(self._sample_lexicon(), tmp_path)
        text = target.read_text(encoding="utf-8")
        for heading in (
            "## Coined Terms",
            "## Metaphor Families",
            "## Distinctive Phrases",
            "## Borrowed Terms",
        ):
            assert heading in text

    def test_glossary_entries_render_term_and_link(self, tmp_path: Path) -> None:
        """Each coined term entry embeds the term and a fragment link."""
        gen = LexiconGenerator()
        target = gen.write_lexicon(self._sample_lexicon(), tmp_path)
        text = target.read_text(encoding="utf-8")
        assert "polygnosticism" in text
        assert "[[frag-001]]" in text
        assert "dharma" in text
        assert "buddhist" in text.lower()

    def test_glossary_has_lexicon_frontmatter(self, tmp_path: Path) -> None:
        """The output file has ``type: lexicon`` frontmatter."""
        gen = LexiconGenerator()
        target = gen.write_lexicon(self._sample_lexicon(), tmp_path)
        post = frontmatter.load(str(target))
        assert post.metadata.get("type") == "lexicon"


# ---- Metaphor index ----


class TestValidation:
    """Constructor guards against invalid configuration."""

    def test_rejects_non_positive_coinage_threshold(self) -> None:
        """min_coinage_occurrences < 1 raises ValueError."""
        with pytest.raises(ValueError, match="min_coinage_occurrences"):
            LexiconGenerator(min_coinage_occurrences=0)

    def test_rejects_non_positive_phrase_threshold(self) -> None:
        """min_phrase_occurrences < 1 raises ValueError."""
        with pytest.raises(ValueError, match="min_phrase_occurrences"):
            LexiconGenerator(min_phrase_occurrences=0)

    def test_rejects_non_positive_top_phrases(self) -> None:
        """top_phrases < 1 raises ValueError."""
        with pytest.raises(ValueError, match="top_phrases"):
            LexiconGenerator(top_phrases=0)

    def test_rejects_inverted_ngram_range(self) -> None:
        """ngram_range with min > max raises ValueError."""
        with pytest.raises(ValueError, match="ngram_range"):
            LexiconGenerator(ngram_range=(5, 2))


class TestEmptyLexiconRendering:
    """Empty-state sections render human-readable placeholders."""

    def test_glossary_with_empty_lexicon_has_placeholders(
        self,
        tmp_path: Path,
    ) -> None:
        """All four sections render a placeholder when empty."""
        gen = LexiconGenerator()
        empty = Lexicon(
            coined_terms=(),
            metaphors=(),
            distinctive_phrases=(),
            borrowed_terms=(),
        )
        target = gen.write_lexicon(empty, tmp_path)
        text = target.read_text(encoding="utf-8")
        assert "_No coined terms detected._" in text
        assert "_No metaphor families detected._" in text
        assert "_No distinctive phrases detected._" in text
        assert "_No borrowed terms detected._" in text

    def test_empty_metaphor_inventory_renders_placeholder(
        self,
        tmp_path: Path,
    ) -> None:
        """A metaphor domain with no usages renders a placeholder note."""
        gen = LexiconGenerator()
        lexicon = Lexicon(
            coined_terms=(),
            metaphors=(
                MetaphorInventory(
                    domain="void",
                    keywords=(),
                    usages=(),
                ),
            ),
            distinctive_phrases=(),
            borrowed_terms=(),
        )
        paths = gen.write_metaphor_index(lexicon, tmp_path)
        text = paths[0].read_text(encoding="utf-8")
        assert "_No recorded usages._" in text
        assert "_none_" in text


class TestWriteMetaphorIndex:
    """Per-domain metaphor notes under 07-Voice/Lexicon/Metaphors/."""

    def _multi_domain_lexicon(self) -> Lexicon:
        return Lexicon(
            coined_terms=(),
            metaphors=(
                MetaphorInventory(
                    domain="water",
                    keywords=("creek", "flow"),
                    usages=(
                        LexiconContext(
                            fragment_id="frag-001",
                            sentence="The creek flows.",
                        ),
                    ),
                ),
                MetaphorInventory(
                    domain="fire",
                    keywords=("spark",),
                    usages=(
                        LexiconContext(
                            fragment_id="frag-002",
                            sentence="A spark kindles.",
                        ),
                    ),
                ),
            ),
            distinctive_phrases=(),
            borrowed_terms=(),
        )

    def test_writes_one_note_per_domain(self, tmp_path: Path) -> None:
        """Every MetaphorInventory produces its own note file."""
        gen = LexiconGenerator()
        paths = gen.write_metaphor_index(self._multi_domain_lexicon(), tmp_path)
        names = {p.name for p in paths}
        assert "water.md" in names
        assert "fire.md" in names

    def test_metaphor_note_contains_keywords_and_usages(
        self,
        tmp_path: Path,
    ) -> None:
        """A metaphor note lists keywords and linked example sentences."""
        gen = LexiconGenerator()
        paths = gen.write_metaphor_index(self._multi_domain_lexicon(), tmp_path)
        water = next(p for p in paths if p.name == "water.md")
        text = water.read_text(encoding="utf-8")
        assert "creek" in text
        assert "[[frag-001]]" in text
        assert "The creek flows." in text
