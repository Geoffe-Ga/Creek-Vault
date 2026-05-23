"""Tests for creek.generate.paradox — Paradox Preservation (Section 10.2).

Tests cover the ``ParadoxDetector`` class which detects contradictory
fragment pairs (opposite phases, opposite confidence on shared threads,
opposite dosage on shared frequencies, and explicit contradiction
keywords) and writes paradox notes to ``10-Liminal/Paradoxes/`` without
ever resolving or judging the contradiction.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import frontmatter
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.generate.paradox import (
    CONTRADICTION_KEYWORDS,
    REFLECTION_PROMPT,
    Paradox,
    ParadoxDetector,
)
from creek.models import (
    Confidence,
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    VoiceClassification,
    WavelengthClassification,
)

# ---- Helpers ----


def make_fragment(
    title: str,
    *,
    fid: str | None = None,
    primary: Frequency = Frequency.UNCLASSIFIED,
    secondary: list[Frequency] | None = None,
    phase: Phase = Phase.UNCLASSIFIED,
    dosage: Dosage = Dosage.UNCLASSIFIED,
    confidence: Confidence | None = None,
    threads: list[str] | None = None,
) -> Fragment:
    """Build a synthetic Fragment with explicit classification axes."""
    from creek.models import synthetic_fragment_id

    kwargs: dict[str, object] = {
        "id": fid if fid is not None else synthetic_fragment_id(),
        "title": title,
        "source": FragmentSource(platform=SourcePlatform.JOURNAL),
        "frequency": FrequencyClassification(
            primary=primary,
            secondary=secondary or [],
        ),
        "wavelength": WavelengthClassification(phase=phase, dosage=dosage),
        "voice": VoiceClassification(confidence=confidence),
        "threads": threads or [],
    }
    return Fragment(**kwargs)


def unit_embedding(seed: int, dims: int = 8) -> list[float]:
    """Make a tiny deterministic unit vector for similarity math."""
    vec = [0.0] * dims
    vec[seed % dims] = 1.0
    return vec


# ---- Paradox dataclass ----


class TestParadox:
    """Tests for the Paradox dataclass."""

    def test_paradox_holds_two_fragment_ids(self) -> None:
        """A paradox links exactly two fragments by default."""
        p = Paradox(
            fragment_ids=["frag-a", "frag-b"],
            contradiction_type="phase",
        )
        assert p.fragment_ids == ["frag-a", "frag-b"]
        assert p.contradiction_type == "phase"
        assert p.frequencies == []
        assert p.excerpts == []
        assert p.detected_date == date.today()


# ---- Detection: phase contradiction ----


class TestDetectPhaseContradiction:
    """Rule 1: high semantic similarity + opposite wavelength phases."""

    def test_detects_rising_vs_diminishing(self) -> None:
        """Opposite phases with high similarity trigger a phase paradox."""
        a = make_fragment("Career ambitions", fid="frag-a", phase=Phase.RISING)
        b = make_fragment("Career ambitions", fid="frag-b", phase=Phase.DIMINISHING)
        embeddings = {"frag-a": unit_embedding(0), "frag-b": unit_embedding(0)}

        detector = ParadoxDetector(similarity_threshold=0.7)
        paradoxes = detector.detect_paradoxes([a, b], embeddings=embeddings)

        assert len(paradoxes) == 1
        assert paradoxes[0].contradiction_type == "phase"
        assert set(paradoxes[0].fragment_ids) == {"frag-a", "frag-b"}

    def test_low_similarity_is_not_paradox(self) -> None:
        """Opposite phases without shared topic produce no paradox."""
        a = make_fragment("Career ambitions", fid="frag-a", phase=Phase.RISING)
        b = make_fragment("Career ambitions", fid="frag-b", phase=Phase.DIMINISHING)
        embeddings = {"frag-a": unit_embedding(0), "frag-b": unit_embedding(3)}

        detector = ParadoxDetector(similarity_threshold=0.7)
        paradoxes = detector.detect_paradoxes([a, b], embeddings=embeddings)

        assert not paradoxes

    def test_same_phase_no_paradox(self) -> None:
        """Same phase even with high similarity is not a paradox."""
        a = make_fragment("x", fid="frag-a", phase=Phase.RISING)
        b = make_fragment("x", fid="frag-b", phase=Phase.RISING)
        embeddings = {"frag-a": unit_embedding(0), "frag-b": unit_embedding(0)}

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b], embeddings=embeddings)

        assert not paradoxes


# ---- Detection: confidence contradiction ----


class TestDetectConfidenceContradiction:
    """Rule 2: shared thread + opposite confidence levels."""

    def test_detects_musing_vs_settled_on_shared_thread(self) -> None:
        """Shared thread with musing vs settled confidence is a paradox."""
        a = make_fragment(
            "Trust",
            fid="frag-a",
            confidence=Confidence.MUSING,
            threads=["thread-trust"],
        )
        b = make_fragment(
            "Trust",
            fid="frag-b",
            confidence=Confidence.SETTLED,
            threads=["thread-trust"],
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert len(paradoxes) == 1
        assert paradoxes[0].contradiction_type == "confidence"

    def test_no_shared_thread_no_paradox(self) -> None:
        """Opposite confidence without a shared thread is not a paradox."""
        a = make_fragment(
            "Trust",
            fid="frag-a",
            confidence=Confidence.MUSING,
            threads=["thread-a"],
        )
        b = make_fragment(
            "Trust",
            fid="frag-b",
            confidence=Confidence.SETTLED,
            threads=["thread-b"],
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert not paradoxes


# ---- Detection: dosage contradiction ----


class TestDetectDosageContradiction:
    """Rule 3: same primary frequency + opposite dosage."""

    def test_medicine_vs_toxic_on_same_frequency(self) -> None:
        """Same primary frequency with medicine vs toxic is a paradox."""
        a = make_fragment(
            "Solitude as refuge",
            fid="frag-a",
            primary=Frequency.F2,
            dosage=Dosage.MEDICINE,
        )
        b = make_fragment(
            "Solitude as exile",
            fid="frag-b",
            primary=Frequency.F2,
            dosage=Dosage.TOXIC,
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert len(paradoxes) == 1
        assert paradoxes[0].contradiction_type == "dosage"
        assert "F2" in paradoxes[0].frequencies

    def test_different_frequency_no_paradox(self) -> None:
        """Medicine vs toxic on different primaries is not a paradox."""
        a = make_fragment(
            "x",
            fid="frag-a",
            primary=Frequency.F1,
            dosage=Dosage.MEDICINE,
        )
        b = make_fragment(
            "x",
            fid="frag-b",
            primary=Frequency.F2,
            dosage=Dosage.TOXIC,
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert not paradoxes


# ---- Detection: explicit contradiction keywords ----


class TestDetectKeywordContradiction:
    """Rule 4: fragment titles with explicit contradiction keywords."""

    @pytest.mark.parametrize("keyword", CONTRADICTION_KEYWORDS)
    def test_detects_each_keyword(self, keyword: str) -> None:
        """Each configured keyword triggers a keyword paradox."""
        earlier = make_fragment(
            "My views on commitment",
            fid="frag-earlier",
            threads=["thread-commit"],
        )
        later = make_fragment(
            f"{keyword}, commitment matters",
            fid="frag-later",
            threads=["thread-commit"],
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([earlier, later])

        assert len(paradoxes) == 1
        assert paradoxes[0].contradiction_type == "keyword"

    def test_keyword_alone_without_link_no_paradox(self) -> None:
        """Keyword without shared topic/thread/frequency produces no paradox."""
        a = make_fragment("Totally unrelated", fid="frag-a")
        b = make_fragment("I used to think sushi was great", fid="frag-b")

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert not paradoxes


# ---- Detection: general behavior ----


class TestDetectParadoxesGeneral:
    """General behaviours across all detection rules."""

    def test_no_paradox_for_unrelated_fragments(self) -> None:
        """Fragments with no overlap produce no paradoxes."""
        a = make_fragment("a", fid="frag-a")
        b = make_fragment("b", fid="frag-b")

        detector = ParadoxDetector()
        assert not detector.detect_paradoxes([a, b])

    def test_no_paradox_for_single_fragment(self) -> None:
        """A single fragment cannot be a paradox."""
        a = make_fragment("a", fid="frag-a")
        detector = ParadoxDetector()
        assert not detector.detect_paradoxes([a])

    def test_detects_multiple_paradoxes(self) -> None:
        """Multiple contradiction types across fragments each surface."""
        a = make_fragment(
            "Alpha",
            fid="frag-a",
            primary=Frequency.F3,
            dosage=Dosage.MEDICINE,
        )
        b = make_fragment(
            "Alpha inverted",
            fid="frag-b",
            primary=Frequency.F3,
            dosage=Dosage.TOXIC,
        )
        c = make_fragment(
            "Beta",
            fid="frag-c",
            confidence=Confidence.MUSING,
            threads=["thread-x"],
        )
        d = make_fragment(
            "Beta",
            fid="frag-d",
            confidence=Confidence.SETTLED,
            threads=["thread-x"],
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b, c, d])

        kinds = sorted(p.contradiction_type for p in paradoxes)
        assert kinds == ["confidence", "dosage"]

    def test_pairs_are_deduplicated(self) -> None:
        """A fragment pair produces at most one paradox."""
        a = make_fragment(
            "Same thread, same freq",
            fid="frag-a",
            primary=Frequency.F5,
            dosage=Dosage.MEDICINE,
            confidence=Confidence.MUSING,
            threads=["thread-x"],
        )
        b = make_fragment(
            "Same thread, same freq",
            fid="frag-b",
            primary=Frequency.F5,
            dosage=Dosage.TOXIC,
            confidence=Confidence.SETTLED,
            threads=["thread-x"],
        )

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b])

        assert len(paradoxes) == 1


# ---- Note creation ----


class TestCreateParadoxNote:
    """Tests for ``ParadoxDetector.create_paradox_note``."""

    @pytest.fixture()
    def paradox(self) -> Paradox:
        """Build a sample Paradox referencing two fragments on F2."""
        return Paradox(
            fragment_ids=["frag-a", "frag-b"],
            contradiction_type="dosage",
            frequencies=["F2"],
            excerpts=["Solitude as refuge", "Solitude as exile"],
            detected_date=date(2026, 4, 12),
        )

    def test_note_written_to_liminal_paradoxes(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """The note is written under 10-Liminal/Paradoxes/."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)

        assert note_path.exists()
        assert note_path.parent == tmp_path / "10-Liminal" / "Paradoxes"

    def test_frontmatter_contains_required_fields(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """Frontmatter carries type, fragment links, frequencies, date."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        post = frontmatter.load(str(note_path))

        assert post["type"] == "paradox"
        assert post["fragments"] == ["frag-a", "frag-b"]
        assert post["frequencies"] == ["F2"]
        assert post["detected_date"] == "2026-04-12"

    def test_paradox_tag_is_applied(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """The ``paradox`` tag plus relevant frequency tags are set."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        post = frontmatter.load(str(note_path))

        tags = post["tags"]
        assert "paradox" in tags
        assert "f2" in tags

    def test_body_links_to_both_fragments(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """Both fragment IDs appear as wikilinks in the body."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        body = frontmatter.load(str(note_path)).content

        assert "[[frag-a]]" in body
        assert "[[frag-b]]" in body

    def test_body_contains_both_excerpts(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """Both fragment excerpts appear in the body."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        body = frontmatter.load(str(note_path)).content

        assert "Solitude as refuge" in body
        assert "Solitude as exile" in body

    def test_body_includes_reflection_prompt(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """The reflection prompt is present, inviting integration not resolution."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        body = frontmatter.load(str(note_path)).content

        assert REFLECTION_PROMPT in body

    def test_body_contains_no_resolution_language(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """The body never prescribes a resolution or renders judgment."""
        detector = ParadoxDetector()
        note_path = detector.create_paradox_note(paradox, tmp_path)
        text = note_path.read_text(encoding="utf-8").lower()

        forbidden = [
            "resolve",
            "resolution",
            "should",
            "must",
            "correct",
            "incorrect",
            "right answer",
            "wrong answer",
            "fix this",
        ]
        for term in forbidden:
            assert term not in text, f"paradox note leaked judgment: {term!r}"

    def test_idempotent_for_same_paradox(
        self,
        paradox: Paradox,
        tmp_path: Path,
    ) -> None:
        """Writing the same paradox twice produces a single stable file."""
        detector = ParadoxDetector()
        first = detector.create_paradox_note(paradox, tmp_path)
        second = detector.create_paradox_note(paradox, tmp_path)

        assert first == second
        files = list((tmp_path / "10-Liminal" / "Paradoxes").glob("*.md"))
        assert len(files) == 1


# ---- FEAT-025: cross-level paradox filter --------------------------------


def _leveled_fragment(
    fid: str,
    *,
    level: str,
    phase: Phase = Phase.UNCLASSIFIED,
    confidence: Confidence | None = None,
    threads: list[str] | None = None,
) -> Fragment:
    """Build a fragment with an explicit structural level."""
    return Fragment(
        id=fid,
        title=f"Title for {fid}",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        frequency=FrequencyClassification(),
        wavelength=WavelengthClassification(phase=phase),
        voice=VoiceClassification(confidence=confidence),
        threads=threads or [],
        level=level,  # type: ignore[arg-type]
    )


class TestCrossLevelFilter:
    """Cross-level pairs default to *not* a paradox — they're rhetorical."""

    def test_cross_level_phase_pair_dropped_by_default(self) -> None:
        """A sentence vs. paragraph contradiction is structure, not paradox."""
        sentence = _leveled_fragment("frag-s", level="sentence", phase=Phase.RISING)
        section = _leveled_fragment("frag-x", level="section", phase=Phase.DIMINISHING)
        embeddings = {"frag-s": unit_embedding(0), "frag-x": unit_embedding(0)}

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes(
            [sentence, section],
            embeddings=embeddings,
        )
        assert paradoxes == []

    def test_same_level_phase_pair_still_a_paradox(self) -> None:
        """The default policy doesn't suppress same-level paradoxes."""
        a = _leveled_fragment("frag-a", level="paragraph", phase=Phase.RISING)
        b = _leveled_fragment("frag-b", level="paragraph", phase=Phase.DIMINISHING)
        embeddings = {"frag-a": unit_embedding(0), "frag-b": unit_embedding(0)}

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes([a, b], embeddings=embeddings)
        assert len(paradoxes) == 1

    def test_cross_level_pair_surfaces_when_opt_in(self) -> None:
        """``cross_level=True`` opts back into the pre-FEAT-025 behaviour."""
        sentence = _leveled_fragment("frag-s", level="sentence", phase=Phase.RISING)
        section = _leveled_fragment("frag-x", level="section", phase=Phase.DIMINISHING)
        embeddings = {"frag-s": unit_embedding(0), "frag-x": unit_embedding(0)}

        detector = ParadoxDetector()
        paradoxes = detector.detect_paradoxes(
            [sentence, section],
            embeddings=embeddings,
            cross_level=True,
        )
        assert len(paradoxes) == 1

    def test_cross_level_filter_applies_to_confidence_rule(self) -> None:
        """The cross-level guard applies to every detection rule, not just phase."""
        a = _leveled_fragment(
            "frag-a",
            level="sentence",
            confidence=Confidence.MUSING,
            threads=["thread-trust"],
        )
        b = _leveled_fragment(
            "frag-b",
            level="paragraph",
            confidence=Confidence.SETTLED,
            threads=["thread-trust"],
        )
        detector = ParadoxDetector()
        assert detector.detect_paradoxes([a, b]) == []
        assert len(detector.detect_paradoxes([a, b], cross_level=True)) == 1
