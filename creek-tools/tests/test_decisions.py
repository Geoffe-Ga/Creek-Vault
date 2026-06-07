"""Tests for creek.generate.decisions — decision detection for Creek fragments.

Tests cover the DecisionDetector class and its three methods:
detect_decisions (keyword + pattern detection), create_decision_note
(vault note generation), and update_decision_phase (phase transitions
with folder moves).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import frontmatter
import pytest

from creek.generate.decisions import (
    DECISION_KEYWORDS,
    DecisionDetector,
)
from creek.models import (
    Confidence,
    DecisionCandidate,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    PraxisPotential,
    SourcePlatform,
    VoiceClassification,
    WavelengthClassification,
)

# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure with decision directories."""
    dirs = [
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "08-Decisions/Active",
        "08-Decisions/Archive",
        "08-Decisions/Frameworks",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def detector() -> DecisionDetector:
    """Create a DecisionDetector instance."""
    return DecisionDetector()


def _seed_fragment(vault: Path, fragment: Fragment, body: str = "body") -> None:
    """Write *fragment* to ``01-Fragments/Conversations/`` for orchestrator tests."""
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    (frags / f"{fragment.id}.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )


def test_generate_decisions_writes_note(
    vault_path: Path,
    keyword_fragment: Fragment,
) -> None:
    """generate_decisions detects a candidate and writes a Decision note (#581)."""
    from creek.generate.decisions import generate_decisions

    _seed_fragment(vault_path, keyword_fragment)

    written = generate_decisions(vault_path)

    assert len(written) == 1
    assert written[0].parent == vault_path / "08-Decisions" / "Active"
    post = frontmatter.load(str(written[0]))
    assert post["type"] == "decision"
    assert keyword_fragment.id in post.content


def test_generate_decisions_is_idempotent(
    vault_path: Path,
    keyword_fragment: Fragment,
) -> None:
    """A re-run writes no duplicate note for an already-captured fragment (#581)."""
    from creek.generate.decisions import generate_decisions

    _seed_fragment(vault_path, keyword_fragment)

    first = generate_decisions(vault_path)
    second = generate_decisions(vault_path)

    assert len(first) == 1
    assert second == []


def test_generate_decisions_no_candidates(
    vault_path: Path,
    neutral_fragment: Fragment,
) -> None:
    """A vault with no decision signals yields no notes and writes nothing (#581)."""
    from creek.generate.decisions import generate_decisions

    _seed_fragment(vault_path, neutral_fragment)

    assert generate_decisions(vault_path) == []
    assert not any((vault_path / "08-Decisions" / "Active").glob("*.md"))


@pytest.fixture()
def keyword_fragment() -> Fragment:
    """Return a fragment containing decision keywords."""
    return Fragment(
        id="frag-decide01",
        title="Should I change careers",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 3, 10, 14, 0, 0),
        frequency=FrequencyClassification(
            primary=Frequency.F1,
            secondary=[Frequency.F5],
        ),
        wavelength=WavelengthClassification(phase=Phase.RISING),
        voice=VoiceClassification(confidence=Confidence.EXPLORING),
        praxis_potential=PraxisPotential.EXPLICIT,
        tags=["career", "decision"],
    )


@pytest.fixture()
def pattern_fragment() -> Fragment:
    """Return a fragment matching pattern detection (F1+F4, explicit, exploring)."""
    return Fragment(
        id="frag-pattern1",
        title="Organizing my life structure",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2025, 3, 12, 9, 0, 0),
        frequency=FrequencyClassification(
            primary=Frequency.F1,
            secondary=[Frequency.F4],
        ),
        wavelength=WavelengthClassification(phase=Phase.PEAKING),
        voice=VoiceClassification(confidence=Confidence.EXPLORING),
        praxis_potential=PraxisPotential.EXPLICIT,
        tags=["structure"],
    )


@pytest.fixture()
def neutral_fragment() -> Fragment:
    """Return a fragment with no decision signals."""
    return Fragment(
        id="frag-neutral1",
        title="Notes on cooking pasta",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2025, 3, 15, 12, 0, 0),
        frequency=FrequencyClassification(primary=Frequency.F2),
        wavelength=WavelengthClassification(phase=Phase.RESTORATION),
        voice=VoiceClassification(confidence=Confidence.SETTLED),
        praxis_potential=PraxisPotential.NONE,
        tags=["cooking"],
    )


# ---- DECISION_KEYWORDS Tests ----


class TestDecisionKeywords:
    """Tests for the DECISION_KEYWORDS constant."""

    def test_keywords_is_nonempty_tuple(self) -> None:
        """DECISION_KEYWORDS should be a non-empty tuple of strings."""
        assert isinstance(DECISION_KEYWORDS, tuple)
        assert len(DECISION_KEYWORDS) > 0

    def test_keywords_are_lowercase(self) -> None:
        """All keywords should be lowercase for case-insensitive matching."""
        for kw in DECISION_KEYWORDS:
            assert kw == kw.lower(), f"Keyword not lowercase: {kw}"

    def test_known_keywords_present(self) -> None:
        """Required keywords from the issue spec should be present."""
        required = [
            "should i",
            "trying to decide",
            "weighing options",
            "not sure whether",
            "torn between",
            "considering",
            "the question is",
        ]
        for kw in required:
            assert kw in DECISION_KEYWORDS, f"Missing keyword: {kw}"


# ---- DecisionDetector Init Tests ----


class TestDecisionDetectorInit:
    """Tests for DecisionDetector.__init__."""

    def test_creates_instance(self) -> None:
        """Should create a DecisionDetector instance."""
        det = DecisionDetector()
        assert isinstance(det, DecisionDetector)


# ---- detect_decisions Tests ----


class TestDetectDecisions:
    """Tests for DecisionDetector.detect_decisions."""

    def test_returns_list(self, detector: DecisionDetector) -> None:
        """Should return a list."""
        result = detector.detect_decisions([])
        assert isinstance(result, list)

    def test_empty_input_returns_empty(self, detector: DecisionDetector) -> None:
        """Empty fragment list should return no candidates."""
        result = detector.detect_decisions([])
        assert result == []

    def test_detects_keyword_in_title(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Fragment with keyword in title should be detected."""
        result = detector.detect_decisions([keyword_fragment])
        assert len(result) == 1
        assert isinstance(result[0], DecisionCandidate)

    def test_keyword_match_records_matched_keywords(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Detected candidate should record which keywords matched."""
        result = detector.detect_decisions([keyword_fragment])
        assert len(result[0].matched_keywords) > 0
        assert "should i" in result[0].matched_keywords

    def test_keyword_detection_case_insensitive(
        self, detector: DecisionDetector
    ) -> None:
        """Keywords should match regardless of case."""
        frag = Fragment(
            id="frag-upper01",
            title="SHOULD I Move To Another City",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 3, 10, 14, 0, 0),
        )
        result = detector.detect_decisions([frag])
        assert len(result) == 1

    def test_pattern_detection_f1_f4(
        self,
        detector: DecisionDetector,
        pattern_fragment: Fragment,
    ) -> None:
        """Fragment with F1+F4, explicit praxis, exploring confidence -> detected."""
        result = detector.detect_decisions([pattern_fragment])
        assert len(result) >= 1

    def test_pattern_detection_f1_f5(self, detector: DecisionDetector) -> None:
        """Fragment with F1+F5, explicit praxis, exploring confidence -> detected."""
        frag = Fragment(
            id="frag-f1f5pat",
            title="Achievement planning",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=datetime(2025, 3, 12, 9, 0, 0),
            frequency=FrequencyClassification(
                primary=Frequency.F1,
                secondary=[Frequency.F5],
            ),
            voice=VoiceClassification(confidence=Confidence.EXPLORING),
            praxis_potential=PraxisPotential.EXPLICIT,
        )
        result = detector.detect_decisions([frag])
        assert len(result) >= 1

    def test_neutral_fragment_not_detected(
        self,
        detector: DecisionDetector,
        neutral_fragment: Fragment,
    ) -> None:
        """Fragment with no decision signals should not be detected."""
        result = detector.detect_decisions([neutral_fragment])
        assert result == []

    def test_candidate_has_fragment_id(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should reference the source fragment ID."""
        result = detector.detect_decisions([keyword_fragment])
        assert result[0].fragment_id == "frag-decide01"

    def test_candidate_has_fragment_title(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should record the source fragment title."""
        result = detector.detect_decisions([keyword_fragment])
        assert result[0].fragment_title == "Should I change careers"

    def test_candidate_has_detection_method(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should specify the detection method used."""
        result = detector.detect_decisions([keyword_fragment])
        assert result[0].detection_method != ""

    def test_candidate_has_confidence_score(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should have a confidence score between 0 and 1."""
        result = detector.detect_decisions([keyword_fragment])
        assert 0.0 < result[0].confidence_score <= 1.0

    def test_candidate_records_wavelength_phase(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should record the wavelength phase at detection."""
        result = detector.detect_decisions([keyword_fragment])
        assert result[0].wavelength_phase_at_detection == "rising"

    def test_candidate_records_frequency_context(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
    ) -> None:
        """Candidate should record frequency context from the fragment."""
        result = detector.detect_decisions([keyword_fragment])
        freqs = result[0].frequency_context
        assert "F1" in freqs

    def test_no_duplicate_candidates(self, detector: DecisionDetector) -> None:
        """A fragment matching both keyword and pattern should produce one candidate."""
        frag = Fragment(
            id="frag-both01",
            title="Should I restructure my approach",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 3, 10, 14, 0, 0),
            frequency=FrequencyClassification(
                primary=Frequency.F1,
                secondary=[Frequency.F4],
            ),
            voice=VoiceClassification(confidence=Confidence.EXPLORING),
            praxis_potential=PraxisPotential.EXPLICIT,
        )
        result = detector.detect_decisions([frag])
        assert len(result) == 1

    def test_multiple_fragments_multiple_candidates(
        self,
        detector: DecisionDetector,
        keyword_fragment: Fragment,
        pattern_fragment: Fragment,
        neutral_fragment: Fragment,
    ) -> None:
        """Multiple fragments: only decision-relevant ones produce candidates."""
        result = detector.detect_decisions(
            [keyword_fragment, pattern_fragment, neutral_fragment],
        )
        assert len(result) == 2
        ids = {c.fragment_id for c in result}
        assert "frag-decide01" in ids
        assert "frag-pattern1" in ids
        assert "frag-neutral1" not in ids


# ---- create_decision_note Tests ----


class TestCreateDecisionNote:
    """Tests for DecisionDetector.create_decision_note."""

    @pytest.fixture()
    def candidate(self) -> DecisionCandidate:
        """Return a sample DecisionCandidate for note creation."""
        return DecisionCandidate(
            id="candidate-test01",
            fragment_id="frag-decide01",
            fragment_title="Should I change careers",
            matched_keywords=["should i"],
            detection_method="keyword",
            confidence_score=0.8,
            wavelength_phase_at_detection="rising",
            frequency_context=["F1", "F5"],
        )

    def test_returns_path(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Should return a Path object."""
        result = detector.create_decision_note(candidate, vault_path)
        assert isinstance(result, Path)

    def test_file_exists(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Created note should exist on disk."""
        result = detector.create_decision_note(candidate, vault_path)
        assert result.is_file()

    def test_file_in_active_dir(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Decision note should be created in 08-Decisions/Active/."""
        result = detector.create_decision_note(candidate, vault_path)
        assert result.parent == vault_path / "08-Decisions" / "Active"

    def test_file_is_markdown(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Decision note should have .md extension."""
        result = detector.create_decision_note(candidate, vault_path)
        assert result.suffix == ".md"

    def test_has_yaml_frontmatter(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Decision note should have YAML frontmatter."""
        result = detector.create_decision_note(candidate, vault_path)
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        rest = content[4:]
        assert "\n---\n" in rest

    def test_frontmatter_type_field(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Frontmatter should include type: decision."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert post["type"] == "decision"

    def test_frontmatter_id_field(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Frontmatter should include a decision ID."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert post["id"].startswith("decision-")

    def test_frontmatter_title_field(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Frontmatter should include a title derived from the candidate."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert len(post["title"]) > 0

    def test_frontmatter_status_sensing(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Default status should be 'sensing'."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert post["status"] == "sensing"

    def test_frontmatter_opened_date(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Frontmatter should include an opened date."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert "opened" in post.metadata

    def test_frontmatter_wavelength_phase(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Frontmatter should record wavelength phase at opening."""
        result = detector.create_decision_note(candidate, vault_path)
        post = frontmatter.load(str(result))
        assert post["wavelength_phase_at_opening"] == "rising"

    def test_body_contains_source_fragments(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Body should contain linked source fragment references."""
        result = detector.create_decision_note(candidate, vault_path)
        content = result.read_text(encoding="utf-8")
        assert "frag-decide01" in content

    def test_body_contains_options_section(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Body should contain an Options section."""
        result = detector.create_decision_note(candidate, vault_path)
        content = result.read_text(encoding="utf-8")
        assert "## Options" in content

    def test_body_contains_criteria_section(
        self,
        detector: DecisionDetector,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> None:
        """Body should contain an empty Criteria section."""
        result = detector.create_decision_note(candidate, vault_path)
        content = result.read_text(encoding="utf-8")
        assert "## Criteria" in content


# ---- update_decision_phase Tests ----


class TestUpdateDecisionPhase:
    """Tests for DecisionDetector.update_decision_phase."""

    @pytest.fixture()
    def active_decision_path(self, vault_path: Path) -> Path:
        """Create a decision note in Active/ and return its path."""
        active_dir = vault_path / "08-Decisions" / "Active"
        note_path = active_dir / "2025-03-10-Career-Change.md"
        post = frontmatter.Post(
            content="## Source Fragments\n\n- frag-decide01\n",
            type="decision",
            id="decision-test0001",
            title="Career Change",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening="rising",
        )
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def test_updates_status_in_frontmatter(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Phase update should change the status field in frontmatter."""
        detector.update_decision_phase("decision-test0001", "deliberating", vault_path)
        post = frontmatter.load(str(active_decision_path))
        assert post["status"] == "deliberating"

    def test_moves_to_archive_when_enacted(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Changing to 'enacted' should move the note from Active/ to Archive/."""
        detector.update_decision_phase("decision-test0001", "enacted", vault_path)
        # Original should be gone
        assert not active_decision_path.exists()
        # Should be in Archive
        archive_dir = vault_path / "08-Decisions" / "Archive"
        archive_files = list(archive_dir.glob("*.md"))
        assert len(archive_files) == 1

    def test_moves_to_archive_when_reflecting(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Changing to 'reflecting' should move the note to Archive/."""
        detector.update_decision_phase("decision-test0001", "reflecting", vault_path)
        assert not active_decision_path.exists()
        archive_dir = vault_path / "08-Decisions" / "Archive"
        archive_files = list(archive_dir.glob("*.md"))
        assert len(archive_files) == 1

    def test_stays_in_active_when_deliberating(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Changing to 'deliberating' should keep note in Active/."""
        detector.update_decision_phase("decision-test0001", "deliberating", vault_path)
        assert active_decision_path.exists()

    def test_stays_in_active_when_committing(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Changing to 'committing' should keep note in Active/."""
        detector.update_decision_phase("decision-test0001", "committing", vault_path)
        assert active_decision_path.exists()

    def test_raises_for_unknown_decision_id(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """Should raise ValueError for a decision ID not found in vault."""
        with pytest.raises(ValueError, match="not found"):
            detector.update_decision_phase("decision-nonexist", "enacted", vault_path)

    def test_raises_for_invalid_phase(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Should raise ValueError for an invalid phase name."""
        with pytest.raises(ValueError, match="Invalid"):
            detector.update_decision_phase("decision-test0001", "invalid", vault_path)

    def test_moved_file_preserves_content(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """Moved file should preserve the original body content."""
        detector.update_decision_phase("decision-test0001", "enacted", vault_path)
        archive_dir = vault_path / "08-Decisions" / "Archive"
        archive_files = list(archive_dir.glob("*.md"))
        post = frontmatter.load(str(archive_files[0]))
        assert "frag-decide01" in post.content

    def test_archive_to_active_move(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """Moving an archived decision back to active status should work."""
        # Create decision in Archive
        archive_dir = vault_path / "08-Decisions" / "Archive"
        note_path = archive_dir / "2025-03-10-Archived-Decision.md"
        post = frontmatter.Post(
            content="## Source Fragments\n\n- frag-arch01\n",
            type="decision",
            id="decision-arch0001",
            title="Archived Decision",
            status="enacted",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening="peaking",
        )
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")

        detector.update_decision_phase(
            "decision-arch0001",
            "deliberating",
            vault_path,
        )
        assert not note_path.exists()
        active_dir = vault_path / "08-Decisions" / "Active"
        active_files = list(active_dir.glob("*.md"))
        assert any(
            frontmatter.load(str(f))["id"] == "decision-arch0001" for f in active_files
        )
