"""Tests for creek.generate.voice — voice register profile generation.

Covers :class:`VoiceProfileGenerator` implementing Section 11.2 of the
Creek Ontology: per-register profile notes combining top exemplar
passages, extracted patterns, a reusable LLM voice prompt, and
register-specific anti-patterns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.voice import (
    VOICE_REGISTERS,
    Exemplar,
    MetaphorFamily,
    ParagraphMetrics,
    PunctuationHabits,
    RhetoricalMoves,
    SentenceMetrics,
    VocabularyFingerprint,
    VoicePatterns,
    VoiceProfile,
    VoiceProfileGenerator,
)
from creek.models import (
    Authorship,
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


def _make_fragment(
    frag_id: str,
    title: str,
    register: VoiceRegister = VoiceRegister.CONFESSIONAL,
    confidence: Confidence = Confidence.CONVICTION,
    author: Authorship = Authorship.SELF,
) -> Fragment:
    """Build a fully classified fragment for exemplar use.

    No ``voice_weight`` is passed, so the model default of ``1.0`` applies —
    the shape ``DocumentIngestor`` produces for a DOCX/PDF carrying an
    ``author`` in its file metadata (#1213).
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL, author=author),
        created=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        ingested=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
        ),
        voice=VoiceClassification(
            voice_register=register,
            confidence=confidence,
        ),
        privacy_tier=PrivacyTier.PERSONAL,
    )


def _exemplar(frag_id: str, body: str) -> Exemplar:
    """Build an Exemplar with a default fragment and the given body."""
    return Exemplar(
        fragment=_make_fragment(frag_id, f"Title {frag_id}"),
        body=body,
    )


def _flat_patterns() -> VoicePatterns:
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


def _rich_patterns() -> VoicePatterns:
    """Build a VoicePatterns with enough content to exercise prompt rendering."""
    return VoicePatterns(
        sentence_metrics=SentenceMetrics(
            average_length=14.2,
            short_count=5,
            medium_count=12,
            long_count=3,
            fragment_count=2,
            question_frequency=0.15,
            total_sentences=20,
        ),
        paragraph_metrics=ParagraphMetrics(
            average_sentences_per_paragraph=3.4,
        ),
        transitions=(("however", 4), ("therefore", 2)),
        metaphor_families=(
            MetaphorFamily(
                domain="water",
                matches=("creek", "flow", "eddy"),
                example_sentences=("The creek of thought flows into a deep eddy.",),
            ),
            MetaphorFamily(
                domain="body",
                matches=("heart", "breath"),
                example_sentences=("I felt it in my bones.",),
            ),
        ),
        rhetorical_moves=RhetoricalMoves(
            self_deprecation_count=3,
            paradox_count=2,
            callback_count=1,
        ),
        vocabulary=VocabularyFingerprint(
            distinctive_words=("creek", "eddy", "fragment"),
            coined_terms=("polygnosticism",),
            recurring_phrases=("creek of thought",),
        ),
        punctuation=PunctuationHabits(
            em_dash_frequency=5.0,
            ellipsis_frequency=1.5,
            parenthetical_frequency=2.0,
            exclamation_frequency=0.5,
        ),
    )


def _write_fragment_file(
    vault_path: Path,
    fragment: Fragment,
    body: str,
    *,
    subfolder: str = "Journal",
) -> Path:
    """Persist *fragment* with *body* under ``01-Fragments/{subfolder}/``."""
    fragments_dir = vault_path / "01-Fragments" / subfolder
    fragments_dir.mkdir(parents=True, exist_ok=True)
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=body, **data)
    target = fragments_dir / f"{fragment.id}.md"
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---- Fixtures ----


@pytest.fixture()
def generator() -> VoiceProfileGenerator:
    """Create a default VoiceProfileGenerator."""
    return VoiceProfileGenerator()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a minimal vault tree with folders the generator uses."""
    for folder in ("01-Fragments/Journal", "07-Voice"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_collect_all_exemplars_returns_flat_bodies(vault: Path) -> None:
    """collect_all_exemplars flattens every qualifying exemplar, with bodies.

    Shares the eligibility gate with ``collect_exemplars`` — a ``musing``
    fragment is excluded — but returns one flat list across registers for
    consumers like the lexicon (#580).
    """
    from creek.generate.voice import VoiceExemplarCollector

    _write_fragment_file(
        vault,
        _make_fragment("ex-1", "T1", VoiceRegister.CONFESSIONAL, Confidence.CONVICTION),
        "First exemplar body about tides and rivers.",
    )
    _write_fragment_file(
        vault,
        _make_fragment("ex-2", "T2", VoiceRegister.ANALYTICAL, Confidence.SETTLED),
        "Second exemplar body, analytical and measured.",
    )
    _write_fragment_file(
        vault,
        _make_fragment("ex-3", "T3", VoiceRegister.CONFESSIONAL, Confidence.MUSING),
        "Ineligible musing body.",
    )

    exemplars = VoiceExemplarCollector().collect_all_exemplars(vault)

    assert {e.fragment.id for e in exemplars} == {"ex-1", "ex-2"}
    assert all(e.body for e in exemplars)


def test_generate_rhetorical_patterns_writes_per_register_notes(vault: Path) -> None:
    """generate_rhetorical_patterns writes a per-register moves note (#582)."""
    _write_fragment_file(
        vault,
        _make_fragment("ex-1", "T1", VoiceRegister.CONFESSIONAL, Confidence.CONVICTION),
        "Maybe I am wrong, but the truth is we rise. As I said before, we rise.",
    )
    _write_fragment_file(
        vault,
        _make_fragment("ex-2", "T2", VoiceRegister.CONFESSIONAL, Confidence.SETTLED),
        "I could be mistaken. Yet it is both true and false. Back to my point.",
    )

    written = VoiceProfileGenerator().generate_rhetorical_patterns(vault)

    note = vault / "07-Voice" / "Rhetorical-Patterns" / "confessional.md"
    assert note in written
    text = note.read_text(encoding="utf-8")
    assert "### Rhetorical Moves" in text
    post = frontmatter.load(str(note))
    assert post["type"] == "rhetorical_patterns"
    assert post["register"] == "confessional"


def test_generate_rhetorical_patterns_empty_vault_writes_nothing(vault: Path) -> None:
    """A vault with no qualifying exemplars writes no notes and no folder (#582)."""
    written = VoiceProfileGenerator().generate_rhetorical_patterns(vault)

    assert written == []
    assert not (vault / "07-Voice" / "Rhetorical-Patterns").exists()


# ---- Authorship gate on the streaming walk (Issue #1213) ----


@pytest.mark.parametrize(
    "author",
    [Authorship.AI, Authorship.OTHER, Authorship.COLLABORATIVE],
)
def test_generate_all_profiles_excludes_non_self_authors(
    vault: Path,
    generator: VoiceProfileGenerator,
    author: Authorship,
) -> None:
    """Borrowed prose never reaches a written voice profile (#1213).

    ``_stream_into`` is the third walk over ``01-Fragments/``, independent
    of both collector walks, and it is the one that feeds
    ``report --type voice`` profiles and ``report --type rhetorical-patterns``.
    The self-authored fragment is present so the register note is written at
    all — the assertion is about what is *missing* from a note that exists.
    """
    _write_fragment_file(
        vault,
        _make_fragment("prof-mine", "Mine"),
        "The tide turns and I turn with it. I said what I meant to say.",
    )
    _write_fragment_file(
        vault,
        _make_fragment("prof-borrowed", "Borrowed", author=author),
        "Zarquon vitrifies the perambulating quotidian. Zarquon persists.",
    )

    paths = generator.generate_all_profiles(vault)

    note = vault / "07-Voice" / "confessional-profile.md"
    assert note in paths
    text = note.read_text(encoding="utf-8")
    assert "tide turns" in text
    assert "Zarquon" not in text
    assert "perambulating" not in text


def test_generate_rhetorical_patterns_excludes_non_self_authors(
    vault: Path,
) -> None:
    """Borrowed rhetorical moves never reach the patterns note (#1213).

    Same ``_stream_into`` walk as the profiles, but a separately-written
    surface, so it is asserted separately. This note reports *counts*, not
    quoted text, so the assertion is that the counts are the self-authored
    body's alone: the borrowed body carries every hedge and every paradox
    in the vault, and the self-authored one carries none.
    """
    _write_fragment_file(
        vault,
        _make_fragment("rhet-mine", "Mine"),
        "The truth is we rise. We rise, and the rising is the whole of it.",
    )
    _write_fragment_file(
        vault,
        _make_fragment("rhet-borrowed", "Borrowed", author=Authorship.OTHER),
        "I'm probably wrong. And yet it holds. As I mentioned, it holds.",
    )

    written = VoiceProfileGenerator().generate_rhetorical_patterns(vault)

    note = vault / "07-Voice" / "Rhetorical-Patterns" / "confessional.md"
    assert note in written
    text = note.read_text(encoding="utf-8")
    assert "- Self-deprecation before insight: 0." in text
    assert "- Paradox constructions: 0." in text
    assert "- Callbacks to earlier points: 0." in text


# ---- Module surface ----


class TestModuleSurface:
    """Exported names for the voice profile feature."""

    def test_voice_profile_generator_exported(self) -> None:
        """VoiceProfileGenerator is exported from the module."""
        from creek.generate import voice

        assert hasattr(voice, "VoiceProfileGenerator")
        assert "VoiceProfileGenerator" in voice.__all__

    def test_voice_profile_exported(self) -> None:
        """VoiceProfile is exported from the module."""
        from creek.generate import voice

        assert hasattr(voice, "VoiceProfile")
        assert "VoiceProfile" in voice.__all__

    def test_exemplar_exported(self) -> None:
        """Exemplar is exported from the module."""
        from creek.generate import voice

        assert hasattr(voice, "Exemplar")
        assert "Exemplar" in voice.__all__


# ---- VoiceProfileGenerator __init__ ----


class TestVoiceProfileGeneratorInit:
    """Constructor validation."""

    def test_default_max_exemplars_is_ten(self) -> None:
        """Default upper bound is 10 exemplars per profile (ontology spec)."""
        assert VoiceProfileGenerator().max_exemplars == 10

    def test_default_min_exemplars_is_five(self) -> None:
        """Default lower bound is 5 exemplars per profile (ontology spec)."""
        assert VoiceProfileGenerator().min_exemplars == 5

    def test_max_exemplars_below_one_rejected(self) -> None:
        """max_exemplars < 1 is invalid."""
        with pytest.raises(ValueError, match="max_exemplars"):
            VoiceProfileGenerator(max_exemplars=0)

    def test_min_exemplars_below_one_rejected(self) -> None:
        """min_exemplars < 1 is invalid."""
        with pytest.raises(ValueError, match="min_exemplars"):
            VoiceProfileGenerator(min_exemplars=0)

    def test_min_exceeding_max_rejected(self) -> None:
        """min_exemplars must not exceed max_exemplars."""
        with pytest.raises(ValueError, match="min_exemplars"):
            VoiceProfileGenerator(min_exemplars=8, max_exemplars=6)


# ---- generate_profile ----


class TestGenerateProfile:
    """VoiceProfileGenerator.generate_profile behaviour."""

    def test_returns_voice_profile(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """generate_profile returns a VoiceProfile instance."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert isinstance(profile, VoiceProfile)

    def test_register_is_stored(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """The profile records the target register."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "analytical",
            exemplars,
            _flat_patterns(),
        )
        assert profile.register == "analytical"

    def test_exemplar_count_respects_max(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """At most max_exemplars passages are retained per profile."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(25)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert profile.exemplar_count == generator.max_exemplars
        assert len(profile.exemplars) == generator.max_exemplars

    def test_preserves_passages(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """Passages are the exemplar body texts, in order."""
        exemplars = [
            _exemplar("f1", "First body."),
            _exemplar("f2", "Second body."),
        ]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert profile.exemplars == ("First body.", "Second body.")

    def test_unknown_register_rejected(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """A register outside the canonical seven is rejected."""
        with pytest.raises(ValueError, match="register"):
            generator.generate_profile(
                "mystic",
                [_exemplar("f1", "Body.")],
                _flat_patterns(),
            )

    def test_voice_prompt_mentions_register(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """The voice prompt names the register so the LLM knows its tone."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert "confessional" in profile.voice_prompt.lower()

    def test_voice_prompt_has_never_clause(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """The voice prompt contains an explicit NEVER section."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert "NEVER" in profile.voice_prompt

    def test_voice_prompt_reflects_metaphor_families(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """Detected metaphor domains are surfaced in the voice prompt."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _rich_patterns(),
        )
        assert "water" in profile.voice_prompt

    def test_anti_patterns_register_specific(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """Each register has its own tuple of anti-patterns."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        confessional = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        analytical = generator.generate_profile(
            "analytical",
            exemplars,
            _flat_patterns(),
        )
        assert confessional.anti_patterns != analytical.anti_patterns
        assert confessional.anti_patterns
        assert analytical.anti_patterns

    def test_confessional_anti_patterns_match_ontology_example(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """Confessional anti-patterns follow the issue's worked example."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        joined = " ".join(profile.anti_patterns).lower()
        assert "corporate" in joined
        assert "bullet" in joined

    def test_generated_at_populated(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """generated_at is timezone-aware and close to now."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        assert profile.generated_at.tzinfo is not None

    def test_below_minimum_logs_warning(
        self,
        generator: VoiceProfileGenerator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Registers with fewer than ``min_exemplars`` samples warn."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(2)]
        with caplog.at_level("WARNING", logger="creek.generate.voice"):
            generator.generate_profile(
                "confessional",
                exemplars,
                _flat_patterns(),
            )
        assert any("confessional" in rec.message for rec in caplog.records)

    def test_empty_exemplars_rejected(
        self,
        generator: VoiceProfileGenerator,
    ) -> None:
        """A register with no exemplars cannot produce a profile."""
        with pytest.raises(ValueError, match="exemplars"):
            generator.generate_profile(
                "confessional",
                [],
                _flat_patterns(),
            )


# ---- Ranking heuristics via generate_all_profiles ----


class TestRankingHeuristics:
    """Exercise the scoring branches of ``_exemplar_score``."""

    def test_short_body_does_not_earn_length_bonus(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Bodies outside 200-800 words skip the length bonus."""
        short = _make_fragment(
            "short-1",
            "Short body",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment_file(vault, short, "Just a few words.")
        for i in range(4):
            fragment = _make_fragment(
                f"medium-{i}",
                f"Medium {i}",
                register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            )
            _write_fragment_file(vault, fragment, "word " * 400)
        paths = generator.generate_all_profiles(vault)
        assert len(paths) == 1

    def test_unclassified_fragment_does_not_earn_classification_bonus(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Fragments with missing classification axes skip the bonus."""
        unclassified = Fragment(
            id="unclassified-1",
            title="Unclassified",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ingested=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            # Defaults leave frequency/phase unclassified.
            frequency=FrequencyClassification(),
            wavelength=WavelengthClassification(),
            voice=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            ),
            privacy_tier=PrivacyTier.PERSONAL,
        )
        _write_fragment_file(vault, unclassified, "word " * 400)
        for i in range(4):
            fragment = _make_fragment(
                f"classified-{i}",
                f"Classified {i}",
                register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            )
            _write_fragment_file(vault, fragment, "word " * 400)
        paths = generator.generate_all_profiles(vault)
        assert len(paths) == 1
        # Fully classified fragments should rank above the unclassified one.
        post = frontmatter.load(str(paths[0]))
        assert post.get("exemplar_count") == 5


# ---- write_profile ----


class TestWriteProfile:
    """VoiceProfileGenerator.write_profile produces a correct markdown note."""

    def test_writes_to_voice_folder(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """The profile lives at ``07-Voice/<register>-profile.md``."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(profile, vault)
        assert path == vault / "07-Voice" / "confessional-profile.md"
        assert path.is_file()

    def test_creates_voice_directory_if_missing(
        self,
        generator: VoiceProfileGenerator,
        tmp_path: Path,
    ) -> None:
        """The 07-Voice directory is created if it does not yet exist."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(profile, tmp_path)
        assert path.parent == tmp_path / "07-Voice"

    def test_frontmatter_type_is_voice_profile(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Frontmatter ``type`` is ``voice_profile``."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(profile, vault)
        post = frontmatter.load(str(path))
        assert post.get("type") == "voice_profile"

    def test_frontmatter_has_register(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Frontmatter includes the register name."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "analytical",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(profile, vault)
        post = frontmatter.load(str(path))
        assert post.get("register") == "analytical"

    def test_frontmatter_has_exemplar_count(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Frontmatter records how many exemplar passages are present."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(profile, vault)
        post = frontmatter.load(str(path))
        assert post.get("exemplar_count") == 5

    def test_frontmatter_has_generated_date(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Frontmatter records the generation date as ISO 8601 string."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        generated = frontmatter.load(
            str(generator.write_profile(profile, vault)),
        ).get("generated_date")
        assert isinstance(generated, str)
        # Must be parseable ISO 8601
        datetime.fromisoformat(generated)

    @pytest.mark.parametrize(
        "heading",
        [
            "## Prompt for LLM",
            "### Structural Patterns",
            "### Metaphor Families",
            "### Rhetorical Moves",
            "### Sample Passages",
            "### Anti-Patterns (DO NOT)",
        ],
    )
    def test_body_contains_required_headings(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
        heading: str,
    ) -> None:
        """The profile body follows the Section 11.2 template headings."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _rich_patterns(),
        )
        content = generator.write_profile(profile, vault).read_text(encoding="utf-8")
        assert heading in content

    def test_body_includes_sample_passages(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Each exemplar body appears in the Sample Passages section."""
        exemplars = [
            _exemplar("f1", "First unique marker one."),
            _exemplar("f2", "Second unique marker two."),
            _exemplar("f3", "Third unique marker three."),
            _exemplar("f4", "Fourth unique marker four."),
            _exemplar("f5", "Fifth unique marker five."),
        ]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        content = generator.write_profile(profile, vault).read_text(encoding="utf-8")
        for body in (
            "First unique marker one.",
            "Fifth unique marker five.",
        ):
            assert body in content

    def test_body_includes_anti_patterns(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Anti-patterns appear in the DO NOT section."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        profile = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        content = generator.write_profile(profile, vault).read_text(encoding="utf-8")
        for pattern in profile.anti_patterns:
            assert pattern in content

    def test_rejects_profile_with_non_canonical_register(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """A profile whose register is not canonical is refused (path safety)."""
        legitimate = generator.generate_profile(
            "confessional",
            [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)],
            _flat_patterns(),
        )
        # Construct a malicious profile with a traversal-style register.
        tampered = VoiceProfile(
            register="../etc/passwd",
            exemplars=legitimate.exemplars,
            patterns=legitimate.patterns,
            voice_prompt=legitimate.voice_prompt,
            anti_patterns=legitimate.anti_patterns,
            generated_at=legitimate.generated_at,
        )
        with pytest.raises(ValueError, match="register"):
            generator.write_profile(tampered, vault)

    def test_overwrites_existing_profile(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Calling write_profile twice overwrites the previous file."""
        exemplars = [_exemplar(f"f{i}", f"Body {i}.") for i in range(5)]
        first = generator.generate_profile(
            "confessional",
            exemplars,
            _flat_patterns(),
        )
        path = generator.write_profile(first, vault)
        second = generator.generate_profile(
            "confessional",
            [_exemplar(f"g{i}", f"Updated body {i}.") for i in range(5)],
            _flat_patterns(),
        )
        path2 = generator.write_profile(second, vault)
        assert path == path2
        content = path2.read_text(encoding="utf-8")
        assert "Updated body 0." in content


# ---- generate_all_profiles ----


class TestGenerateAllProfiles:
    """End-to-end: scan vault, generate a profile for every non-empty register."""

    def _seed_register(
        self,
        vault_path: Path,
        register: VoiceRegister,
        count: int,
    ) -> None:
        """Seed *count* fragments of *register* with distinctive bodies."""
        for i in range(count):
            fragment = _make_fragment(
                f"{register.value}-{i}",
                f"{register.value} fragment {i}",
                register=register,
            )
            body = (
                f"Paragraph about the {register.value} register: "
                "the creek of thought flows here. " * 30
            )
            _write_fragment_file(vault_path, fragment, body)

    def test_returns_list_of_paths(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """generate_all_profiles returns the list of paths it wrote."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        paths = generator.generate_all_profiles(vault)
        assert isinstance(paths, list)
        assert len(paths) == 1
        assert paths[0].is_file()

    def test_creates_profile_per_populated_register(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Every register with exemplars gets a profile."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        self._seed_register(vault, VoiceRegister.ANALYTICAL, count=5)
        paths = generator.generate_all_profiles(vault)
        registers = {p.stem.replace("-profile", "") for p in paths}
        assert registers == {"confessional", "analytical"}

    def test_skips_registers_without_exemplars(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Registers without any fragments are not written."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        paths = generator.generate_all_profiles(vault)
        names = {p.name for p in paths}
        assert "confessional-profile.md" in names
        assert "prophetic-profile.md" not in names

    def test_all_seven_registers_when_seeded(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Seeding every register produces seven profile files."""
        for register in VoiceRegister:
            self._seed_register(vault, register, count=5)
        paths = generator.generate_all_profiles(vault)
        assert len(paths) == len(VOICE_REGISTERS)

    def test_profiles_have_valid_frontmatter(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Generated files have valid YAML frontmatter with required keys."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        paths = generator.generate_all_profiles(vault)
        post = frontmatter.load(str(paths[0]))
        assert post.get("type") == "voice_profile"
        assert post.get("register") == "confessional"
        assert post.get("exemplar_count") == 5

    def test_missing_vault_returns_empty_list(
        self,
        generator: VoiceProfileGenerator,
        tmp_path: Path,
    ) -> None:
        """A vault without a 01-Fragments dir yields no profiles."""
        paths = generator.generate_all_profiles(tmp_path)
        assert not paths

    def test_skips_unreadable_and_ineligible_files(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Non-fragment, invalid, and ineligible markdown files are skipped."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        # A file that is not a fragment (missing ``type: fragment``).
        stray = vault / "01-Fragments" / "Journal" / "stray.md"
        stray.write_text("---\ntype: note\n---\njust prose", encoding="utf-8")
        # An ineligible fragment (musing confidence — too early to qualify).
        musing = _make_fragment(
            "musing-1",
            "Too early",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.MUSING,
        )
        _write_fragment_file(vault, musing, "Body text here.")
        paths = generator.generate_all_profiles(vault)
        assert len(paths) == 1
        post = frontmatter.load(str(paths[0]))
        assert post.get("exemplar_count") == 5


# ---- Streaming exemplar accumulator (PERF-004) ----


class TestStreamingAccumulator:
    """Tests for the on-disk JSONL exemplar accumulator path."""

    def _seed_register(
        self,
        vault_path: Path,
        register: VoiceRegister,
        count: int,
    ) -> None:
        """Seed *count* fragments of *register* with distinctive bodies."""
        for i in range(count):
            fragment = _make_fragment(
                f"{register.value}-{i}",
                f"{register.value} fragment {i}",
                register=register,
            )
            body = (
                f"Paragraph about the {register.value} register: "
                "the creek of thought flows here. " * 30
            )
            _write_fragment_file(vault_path, fragment, body)

    def test_temp_jsonl_is_cleaned_up(
        self,
        monkeypatch: pytest.MonkeyPatch,
        generator: VoiceProfileGenerator,
        vault: Path,
        tmp_path: Path,
    ) -> None:
        """The accumulator's temp dir is actually deleted after the call returns.

        The previous version of this test was vacuous: it scanned
        ``tmp_path`` for ``creek-voice-*`` entries, but
        ``tempfile.TemporaryDirectory`` creates under
        ``tempfile.gettempdir()`` (e.g. ``/tmp``) by default, so the
        scan never had anything to find. The fix monkey-patches the
        ``tempfile.TemporaryDirectory`` constructor used inside
        ``creek.generate.voice`` so the directory lands under
        ``tmp_path`` and we can directly assert that the directory is
        gone after ``generate_all_profiles`` returns.
        """
        import os
        import tempfile

        from creek.generate import voice as voice_mod

        created: list[str] = []
        real_td = tempfile.TemporaryDirectory

        def patched_td(*args: object, **kwargs: object) -> object:
            kwargs["dir"] = str(tmp_path)
            td = real_td(*args, **kwargs)  # type: ignore[arg-type]
            created.append(td.name)
            return td

        monkeypatch.setattr(voice_mod.tempfile, "TemporaryDirectory", patched_td)
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=5)
        generator.generate_all_profiles(vault)

        # The patched constructor must have been used, and every
        # directory it returned must be deleted by the time the call
        # exits the streaming-accumulator context manager.
        assert created, "patched TemporaryDirectory was never invoked"
        for path in created:
            assert not os.path.exists(path), f"Temp dir leaked: {path}"

    def test_streaming_matches_legacy_output(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Streaming path produces the same exemplar count as legacy in-memory."""
        self._seed_register(vault, VoiceRegister.CONFESSIONAL, count=7)
        paths = generator.generate_all_profiles(vault)
        assert len(paths) == 1
        post = frontmatter.load(str(paths[0]))
        # max_exemplars defaults to 10; we seeded 7.
        assert post.get("exemplar_count") == 7

    def test_jsonl_writer_flushes_on_each_append(
        self,
        tmp_path: Path,
    ) -> None:
        """``_JsonlWriter.append`` flushes per call to leave a coherent prefix."""
        from creek.generate.voice import _JsonlWriter

        target = tmp_path / "exemplar.jsonl"
        writer = _JsonlWriter(target)
        try:
            writer.append({"a": 1})
            # Without flush, the new line might still be in the userspace
            # buffer; with the per-append flush, an out-of-band reader
            # sees it immediately.
            assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
            writer.append({"b": 2})
            assert target.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'
        finally:
            writer.close()


@pytest.mark.slow
class TestVoiceMemoryFootprint:
    """Memory benchmark guarding against PERF-004's load-everything bug."""

    def test_peak_under_200mb_for_large_vault(
        self,
        generator: VoiceProfileGenerator,
        vault: Path,
    ) -> None:
        """Peak heap stays under 200 MB while processing 2 000 large fragments."""
        import tracemalloc

        # 2000 fragments x ~5 KB body = ~10 MB total. With the legacy
        # in-memory path that produces a single ~10 MB peak; the
        # streaming path's peak should be well under the 200 MB envelope.
        # The threshold guards against future regressions that would
        # re-introduce the load-everything pattern.
        body_template = "the creek of thought flows here. " * 150  # ~5 KB
        for register in VoiceRegister:
            for i in range(2_000 // len(VoiceRegister)):
                fragment = _make_fragment(
                    f"{register.value}-{i}",
                    f"{register.value} fragment {i}",
                    register=register,
                )
                _write_fragment_file(vault, fragment, body_template)

        tracemalloc.start()
        try:
            paths = generator.generate_all_profiles(vault)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert paths
        peak_mb = peak / (1024 * 1024)
        assert peak_mb < 200, f"Peak memory was {peak_mb:.1f} MB"
