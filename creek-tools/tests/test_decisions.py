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

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate import decisions as decisions_mod
from creek.generate.decisions import (
    _ACTIVE_STATUSES,
    _DECISION_FREQUENCY_PAIRS,
    _VALID_PHASES,
    DECISION_KEYWORDS,
    DecisionDetector,
    DecisionsReport,
    _sanitize_title,
    generate_decisions,
    withheld_notice,
)
from creek.models import (
    Confidence,
    DecisionCandidate,
    DecisionStatus,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    PraxisPotential,
    PrivacyTier,
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

    written = generate_decisions(vault_path).notes

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

    first = generate_decisions(vault_path).notes
    second = generate_decisions(vault_path).notes

    assert len(first) == 1
    assert second == ()


def test_generate_decisions_no_candidates(
    vault_path: Path,
    neutral_fragment: Fragment,
) -> None:
    """A vault with no decision signals yields no notes and writes nothing (#581)."""
    from creek.generate.decisions import generate_decisions

    _seed_fragment(vault_path, neutral_fragment)

    assert generate_decisions(vault_path).notes == ()
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

    def test_a_status_member_round_trips_through_frontmatter(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        active_decision_path: Path,
    ) -> None:
        """A ``DecisionStatus`` member must serialise as its plain value.

        Issue #991. ``_VALID_PHASES`` holds ``DecisionStatus`` members, so a
        member passes the validity guard, is written straight into
        ``post["status"]``, and only then reaches ``frontmatter.dumps`` --
        where PyYAML has no representer for the enum and raises
        ``RepresenterError``. The guard accepting a value the writer cannot
        write is what makes this a crash rather than a rejection, so the
        assertion is on the written bytes, not merely on not raising.
        """
        detector.update_decision_phase(
            "decision-test0001",
            DecisionStatus.ENACTED,
            vault_path,
        )

        archive_dir = vault_path / "08-Decisions" / "Archive"
        archive_files = list(archive_dir.glob("*.md"))
        assert len(archive_files) == 1
        written = archive_files[0].read_text(encoding="utf-8")
        assert "!!python/object/apply" not in written, (
            f"the enum was serialised as a Python object tag: {written!r}"
        )
        post = frontmatter.load(str(archive_files[0]))
        assert post["status"] == "enacted"
        assert type(post["status"]) is str, (
            "status round-tripped as something other than a plain string: "
            f"{type(post['status'])!r}"
        )

    def test_the_valid_phase_sets_hold_the_strings_they_are_annotated_to_hold(
        self,
    ) -> None:
        """``frozenset[str]`` must mean strings, not ``StrEnum`` members.

        Issue #991. Both sets were built from ``DecisionStatus`` members
        while annotated ``frozenset[str]``. ``StrEnum`` comparison hides
        that at every membership test, so the annotation lie was invisible
        until a member survived the guard and reached a YAML serialiser.
        ``type(...) is str`` is deliberate -- ``isinstance`` is satisfied by
        a ``StrEnum`` member and would leave this test unable to fail.
        """
        for name, members in (
            ("_ACTIVE_STATUSES", _ACTIVE_STATUSES),
            ("_VALID_PHASES", _VALID_PHASES),
        ):
            assert members, f"{name} is empty, so the loop below asserts nothing"
            for member in members:
                assert type(member) is str, (
                    f"{name} is annotated frozenset[str] but holds "
                    f"{type(member)!r}: {member!r}"
                )

    def test_the_decision_frequency_pairs_hold_the_strings_they_promise(self) -> None:
        """``frozenset[tuple[str, str]]`` must likewise mean strings.

        Issue #991. This one has no crash path -- it is only ever compared
        against a set of ``str(...)``-converted frequencies, never
        serialised -- so this is an annotation-honesty lock, not a bug fix.
        It is pinned anyway because the next reader has no way to tell the
        harmless case from the crashing one by inspection.
        """
        pairs = _DECISION_FREQUENCY_PAIRS
        assert pairs, "_DECISION_FREQUENCY_PAIRS is empty"
        for pair in pairs:
            for member in pair:
                assert type(member) is str, (
                    "_DECISION_FREQUENCY_PAIRS is annotated "
                    f"frozenset[tuple[str, str]] but holds {type(member)!r}: "
                    f"{member!r}"
                )


# ---- Issue #1334: colliding filenames destroy notes and oscillate forever ----
#
# ``create_decision_note`` names its output ``<today>-<sanitised title>.md`` but
# ``_existing_decision_fragment_ids`` indexes notes by a *different* key — the
# source fragment id in the body. Two candidates sharing a title therefore land
# on one path: the second overwrites the first, the read-back index only ever
# sees the survivor, and the generator rediscovers the fragment it just lost:
#
#   run1: written=2 files=1 survivor=frag-b id=decision-31eecda0
#   run2: written=1 files=1 survivor=frag-a id=decision-1dbcf6ef
#   run3: written=1 files=1 survivor=frag-b id=decision-0be4a97a  ... forever
#
# The fix resolves the path by IDENTITY OWNERSHIP, not path occupancy:
#
#   1. the natural name ``<date>-<slug>.md`` when it is free;
#   2. that same name when the note already there records *this* identity —
#      a legitimate refresh, which is today's direct-call contract and is
#      preserved deliberately (see the ``_atomic_create`` guard below);
#   3. otherwise the next ordinal — ``-1``, ``-2``, … — applying the same rule
#      at each step, when the occupant records a *different* identity or an
#      identity that cannot be positively established as ours (unreadable,
#      malformed YAML, or no ``## Source Fragments`` bullet).
#
# The resolver probes exact paths; it never lists the directory. The ordinal
# loop is bounded by a named module constant and exhaustion raises loudly.


_PORTLAND_TITLE = "Should I move to Portland"
"""Decision title shared by the colliding candidates in this section."""

_PORTLAND_SLUG = "Should-I-move-to-Portland"
"""``_sanitize_title(_PORTLAND_TITLE)`` — the stem both candidates want."""

_LONG_TITLE_A = (
    "Should I move to Portland or stay in the city where everything "
    "already makes sense anyway"
)
"""A title longer than the 80-character truncation in ``_sanitize_title``."""

_LONG_TITLE_B = (
    "Should I move to Portland or stay in the city where everything "
    "already makes sensory sense"
)
"""A *different* title that survives truncation as the same stem as A."""

_COLLIDING_STEM = (
    "Should-I-move-to-Portland-or-stay-in-the-city-where-everything-already-makes-sen"
)
"""The 80-character stem both long titles collapse to.

Produced by ``decisions._sanitize_title``'s ``[:80]`` truncation.
"""

_MALFORMED_DECISION_NOTE = """---
title: [unclosed
status: sensing
---

Whatever this file is, the writer must not clobber it.
"""
"""A neighbour whose YAML frontmatter will not parse at all."""

_UNIDENTIFIED_DECISION_NOTE = """---
id: decision-stranger1
status: sensing
title: Should I move to Portland
type: decision
---

## Options

- Move to Portland
- Stay where I am
"""
"""A well-formed neighbour carrying no ``## Source Fragments`` bullet."""

_DAMAGED_DECISION_NEIGHBOURS: dict[str, str] = {
    "malformed-yaml": _MALFORMED_DECISION_NOTE,
    "no-source-fragments-bullet": _UNIDENTIFIED_DECISION_NOTE,
}
"""Neighbour shapes whose identity cannot be established as ours.

Every one of them must push the incoming note onto an ordinal and must come
through the write byte-identical. Emptying a parametrize list makes its tests
vanish behind a green gate, so
:func:`test_the_damaged_decision_neighbour_matrix_is_not_empty` pins the keys.
"""

_SEEDED_DECISION_NOTE = """---
confidence_score: 0.7
detection_method: keyword
frequency_context:
- F1
- F5
id: decision-survivor01
opened: '2026-04-01'
status: deliberating
title: Should I move to Portland
type: decision
wavelength_phase_at_opening: rising
---

## Source Fragments

- frag-b

## Options

- Move to Portland
- Stay where I am

## Criteria

- Cost of living

I keep coming back to the light in November.
"""
"""A vault already in the post-collision state: one survivor, operator-edited.

``status`` has been advanced to ``deliberating`` and a line of the operator's
own prose has been added under the generated sections. Today's writer destroys
both on the next run.
"""


def _decision_fragment(frag_id: str, title: str) -> Fragment:
    """Build a keyword-detectable fragment with a caller-chosen id and title.

    Args:
        frag_id: Fragment ID, also the filename stem under ``01-Fragments``.
        title: Fragment title — the string that becomes the note's filename.

    Returns:
        A :class:`~creek.models.Fragment` that ``detect_decisions`` flags.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 3, 10, 14, 0, 0),
        frequency=FrequencyClassification(
            primary=Frequency.F1,
            secondary=[Frequency.F5],
        ),
        wavelength=WavelengthClassification(phase=Phase.RISING),
        voice=VoiceClassification(confidence=Confidence.EXPLORING),
        praxis_potential=PraxisPotential.EXPLICIT,
        tags=["decision"],
    )


def _candidate(frag_id: str, title: str) -> DecisionCandidate:
    """Build a candidate for direct ``create_decision_note`` calls.

    Args:
        frag_id: The source fragment id — the note's *identity* under #1334.
        title: The fragment title the filename is derived from.

    Returns:
        A populated :class:`~creek.models.DecisionCandidate`.
    """
    return DecisionCandidate(
        fragment_id=frag_id,
        fragment_title=title,
        matched_keywords=["should i"],
        detection_method="keyword",
        confidence_score=0.8,
        wavelength_phase_at_detection="rising",
        frequency_context=["F1", "F5"],
    )


def _active_notes(vault: Path) -> list[Path]:
    """Return every note in ``08-Decisions/Active``, sorted by name."""
    return sorted((vault / "08-Decisions" / "Active").glob("*.md"))


def _note_names(vault: Path) -> set[str]:
    """Return the filenames currently present in ``08-Decisions/Active``."""
    return {path.name for path in _active_notes(vault)}


def _source_fragment_of(note_path: Path) -> str:
    """Return the source fragment id a decision note records, read from disk.

    Deliberately re-implements the parse rather than calling
    ``creek.generate.decisions._existing_decision_fragment_ids``. A test that
    reads identity back through the module's own extractor stops being evidence
    the moment that extractor learns to repair or infer what it is looking for
    — the self-healing-reader trap. ``frontmatter.load`` and nothing else.

    Args:
        note_path: Path to a decision note.

    Returns:
        The first bullet under ``## Source Fragments``, or ``""`` when the
        note records no source fragment at all.
    """
    post = frontmatter.load(str(note_path))
    in_source_section = False
    for line in post.content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_source_section = stripped.lower() == "## source fragments"
            continue
        if in_source_section and stripped.startswith("- "):
            return stripped[2:].strip()
    return ""


def _natural_and_ordinal(paths: list[Path]) -> tuple[Path, Path]:
    """Split exactly two note paths into the unsuffixed one and the ``-1`` one.

    Args:
        paths: The two notes written for one colliding title.

    Returns:
        ``(natural, ordinal)``, having asserted that the ordinal's stem is
        precisely the natural stem plus ``-1`` — the suffix convention
        ``VaultWriter._atomic_create`` already uses.
    """
    assert len(paths) == 2, f"expected two notes, got {[p.name for p in paths]}"
    natural = [p for p in paths if not p.stem.endswith("-1")]
    ordinal = [p for p in paths if p.stem.endswith("-1")]
    assert len(natural) == 1, f"expected one unsuffixed note, got {natural}"
    assert len(ordinal) == 1, f"expected one '-1' note, got {ordinal}"
    assert ordinal[0].stem == f"{natural[0].stem}-1"
    return natural[0], ordinal[0]


def _seed_decision_note(
    vault: Path,
    *,
    filename: str,
    source_id: str,
    decision_id: str = "decision-seeded001",
    subfolder: str = "Active",
    status: str = "sensing",
    prose: str = "",
) -> Path:
    """Write a well-formed decision note recording *source_id* at *filename*.

    Args:
        vault: Vault root.
        filename: Filename to write inside ``08-Decisions/<subfolder>``.
        source_id: The source fragment id the note records — its identity.
        decision_id: Value for the note's ``id`` frontmatter key.
        subfolder: Folder under ``08-Decisions`` to write into — ``Active``
            or ``Archive``. #1430's move tests need to seed the latter.
        status: Value for the note's ``status`` frontmatter key.
        prose: Operator prose appended under the generated sections, so a
            byte comparison has something of the operator's own to lose.

    Returns:
        The path written.
    """
    target = vault / "08-Decisions" / subfolder
    target.mkdir(parents=True, exist_ok=True)
    body = f"## Source Fragments\n\n- {source_id}\n\n## Options\n\n- _One_\n"
    if prose:
        body += f"\n{prose}\n"
    post = frontmatter.Post(
        content=body,
        type="decision",
        id=decision_id,
        title=_PORTLAND_TITLE,
        status=status,
        opened=date(2026, 4, 1).isoformat(),
        wavelength_phase_at_opening="rising",
    )
    path = target / filename
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def test_the_damaged_decision_neighbour_matrix_is_not_empty() -> None:
    """Guard the #1334 neighbour parametrize list against silently emptying."""
    assert set(_DAMAGED_DECISION_NEIGHBOURS) == {
        "malformed-yaml",
        "no-source-fragments-bullet",
    }


class TestDecisionNoteIdentityCollision:
    """Two same-stemmed decision identities must become two notes (#1334)."""

    def test_three_runs_settle_with_one_note_per_identity(
        self,
        vault_path: Path,
    ) -> None:
        """The headline oscillation: run 1 writes both, runs 2 and 3 write nothing.

        A single-run assertion would pass today for the wrong reason — run 1
        already *reports* ``written=2`` while leaving one file on disk. The
        load-bearing assertion is therefore the set of identities recorded on
        disk: two notes both recording ``frag-b`` satisfy ``len(files) == 2``
        and are still exactly the #1334 bug.
        """
        _seed_fragment(vault_path, _decision_fragment("frag-a", _PORTLAND_TITLE))
        _seed_fragment(vault_path, _decision_fragment("frag-b", _PORTLAND_TITLE))
        before = date.today()

        first = generate_decisions(vault_path).notes
        snapshot_1 = _note_names(vault_path)
        second = generate_decisions(vault_path).notes
        snapshot_2 = _note_names(vault_path)
        third = generate_decisions(vault_path).notes
        snapshot_3 = _note_names(vault_path)
        after = date.today()

        assert len(first) == 2
        assert second == ()
        assert third == ()
        assert snapshot_1 == snapshot_2 == snapshot_3

        notes = _active_notes(vault_path)
        natural, ordinal = _natural_and_ordinal(notes)
        # ``before``/``after`` bracket the run so a suite straddling local
        # midnight cannot produce a false failure; in every other run they
        # are the same day and the assertion is exact.
        assert natural.stem in {
            f"{day.isoformat()}-{_PORTLAND_SLUG}" for day in (before, after)
        }
        assert ordinal.stem == f"{natural.stem}-1"

        assert {_source_fragment_of(path) for path in notes} == {"frag-a", "frag-b"}
        contents = [frontmatter.load(str(path)).content for path in notes]
        assert sum("\n- frag-a\n" in text for text in contents) == 1
        assert sum("\n- frag-b\n" in text for text in contents) == 1

    def test_note_ids_and_bytes_do_not_churn_across_runs(
        self,
        vault_path: Path,
    ) -> None:
        """Each note's ``id:`` survives runs 2 and 3 untouched (#1334).

        Today the surviving note is rewritten every run, so its ``id:`` churns
        (``decision-31eecda0`` → ``decision-1dbcf6ef`` → ``decision-0be4a97a``)
        and every operator edit beneath it is destroyed. Bytes are compared as
        well as ids, because "did not rewrite" is the property that matters.
        """
        _seed_fragment(vault_path, _decision_fragment("frag-a", _PORTLAND_TITLE))
        _seed_fragment(vault_path, _decision_fragment("frag-b", _PORTLAND_TITLE))

        generate_decisions(vault_path)
        ids_first = {
            path.name: frontmatter.load(str(path))["id"]
            for path in _active_notes(vault_path)
        }
        bytes_first = {
            path.name: path.read_bytes() for path in _active_notes(vault_path)
        }
        generate_decisions(vault_path)
        bytes_second = {
            path.name: path.read_bytes() for path in _active_notes(vault_path)
        }
        generate_decisions(vault_path)
        bytes_third = {
            path.name: path.read_bytes() for path in _active_notes(vault_path)
        }
        ids_third = {
            path.name: frontmatter.load(str(path))["id"]
            for path in _active_notes(vault_path)
        }

        assert len(ids_first) == 2
        assert len(set(ids_first.values())) == 2
        assert all(str(value).startswith("decision-") for value in ids_first.values())
        assert ids_third == ids_first
        assert bytes_second == bytes_first
        assert bytes_third == bytes_first

    def test_recreating_the_same_candidate_refreshes_the_one_note(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """REGRESSION GUARD against adopting a path-occupancy counter-suffix.

        The tempting shortcut for #1334 is ``VaultWriter._atomic_create``'s
        counter (``VaultWriter._atomic_create``), which advances on *occupancy*.
        Dropped in here it would mint a second file for one decision and break
        the direct-call refresh contract that #1320/#1417 rely on. Branch 2 of
        the #1334 resolver — "occupied by a note recording the same identity" —
        exists precisely to keep this test green.
        """
        candidate = _candidate("frag-refresh", _PORTLAND_TITLE)

        first = detector.create_decision_note(candidate, vault_path)
        second = detector.create_decision_note(candidate, vault_path)

        assert second == first
        assert not first.stem.endswith("-1")
        assert _active_notes(vault_path) == [first]
        assert _source_fragment_of(first) == "frag-refresh"

    def test_titles_colliding_only_after_truncation_get_two_notes(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """Two distinct titles truncate to one 80-char stem — still two notes.

        The strongest evidence that the #1334 fix belongs in the *write* and
        not in the naming: no amount of care in ``_sanitize_title`` can keep
        these two apart, because the collision is created by the truncation
        the sanitiser exists to apply (``decisions._sanitize_title``).
        """
        assert len(_COLLIDING_STEM) == 80
        assert _LONG_TITLE_A != _LONG_TITLE_B
        assert _sanitize_title(_LONG_TITLE_A) == _COLLIDING_STEM
        assert _sanitize_title(_LONG_TITLE_B) == _COLLIDING_STEM
        before = date.today()

        first = detector.create_decision_note(
            _candidate("frag-f1", _LONG_TITLE_A),
            vault_path,
        )
        second = detector.create_decision_note(
            _candidate("frag-f2", _LONG_TITLE_B),
            vault_path,
        )
        after = date.today()

        assert first != second
        assert first.stem in {
            f"{day.isoformat()}-{_COLLIDING_STEM}" for day in (before, after)
        }
        assert second.stem == f"{first.stem}-1"
        assert {_source_fragment_of(path) for path in _active_notes(vault_path)} == {
            "frag-f1",
            "frag-f2",
        }

    def test_both_empty_titled_candidates_get_their_own_note(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """The ``<date>.md`` fallback is *more* collision-prone, not exempt.

        ``create_decision_note`` drops the slug entirely when the sanitised
        title is empty, so every untitled candidate of a given day wants the
        same filename. The resolver has to wrap the final filename, not just
        the sanitised-title branch.
        """
        assert _sanitize_title("") == ""
        before = date.today()

        first = detector.create_decision_note(_candidate("frag-g1", ""), vault_path)
        second = detector.create_decision_note(_candidate("frag-g2", ""), vault_path)
        after = date.today()

        assert first.stem in {day.isoformat() for day in (before, after)}
        assert second.stem == f"{first.stem}-1"
        assert {_source_fragment_of(path) for path in _active_notes(vault_path)} == {
            "frag-g1",
            "frag-g2",
        }

    def test_a_title_ending_in_dash_one_does_not_hijack_an_ordinal(
        self,
        detector: DecisionDetector,
        vault_path: Path,
    ) -> None:
        """A real title may sanitise to another identity's *suffixed* name.

        ``frag-b`` was pushed onto ``…-Portland-1.md`` by the collision with
        ``frag-a``. ``frag-c``'s genuine title, "Should I move to Portland 1",
        sanitises to exactly that stem. Ownership-based resolution probes the
        path, finds a stranger, and advances to ``…-Portland-1-1.md``;
        occupancy-blind logic silently overwrites ``frag-b``.
        """
        first = detector.create_decision_note(
            _candidate("frag-a", _PORTLAND_TITLE),
            vault_path,
        )
        second = detector.create_decision_note(
            _candidate("frag-b", _PORTLAND_TITLE),
            vault_path,
        )
        third = detector.create_decision_note(
            _candidate("frag-c", f"{_PORTLAND_TITLE} 1"),
            vault_path,
        )

        assert second.stem == f"{first.stem}-1"
        assert third.stem == f"{first.stem}-1-1"
        assert _source_fragment_of(first) == "frag-a"
        assert _source_fragment_of(second) == "frag-b"
        assert _source_fragment_of(third) == "frag-c"
        assert len(_active_notes(vault_path)) == 3


class TestDecisionDamagedVaultSelfHeal:
    """Vaults already damaged by #1334 heal without losing operator work."""

    def test_the_survivor_is_untouched_and_the_lost_note_reappears(
        self,
        vault_path: Path,
    ) -> None:
        """The migration ruling, made executable (#1334).

        The vault starts in the post-collision state: one note at the natural
        path recording ``frag-b``, advanced to ``deliberating`` and carrying a
        line of the operator's own prose; ``frag-a`` has no note at all. The
        next run must restore ``frag-a`` onto an ordinal and leave the survivor
        byte-for-byte alone — no path move, no ``id:`` churn, no lost prose.
        """
        _seed_fragment(vault_path, _decision_fragment("frag-a", _PORTLAND_TITLE))
        _seed_fragment(vault_path, _decision_fragment("frag-b", _PORTLAND_TITLE))
        survivor = (
            vault_path
            / "08-Decisions"
            / "Active"
            / f"{date.today().isoformat()}-{_PORTLAND_SLUG}.md"
        )
        survivor.write_text(_SEEDED_DECISION_NOTE, encoding="utf-8")
        original_bytes = survivor.read_bytes()

        first = generate_decisions(vault_path).notes
        second = generate_decisions(vault_path).notes

        assert len(first) == 1
        assert second == ()
        assert survivor.read_bytes() == original_bytes
        post = frontmatter.load(str(survivor))
        assert post["status"] == "deliberating"
        assert post["id"] == "decision-survivor01"
        assert "I keep coming back to the light in November." in post.content
        assert _source_fragment_of(survivor) == "frag-b"

        healed = first[0]
        assert healed.name == f"{survivor.stem}-1.md"
        assert _source_fragment_of(healed) == "frag-a"
        assert len(_active_notes(vault_path)) == 2

    @pytest.mark.parametrize("shape", sorted(_DAMAGED_DECISION_NEIGHBOURS))
    def test_a_neighbour_of_unestablished_identity_is_never_clobbered(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        shape: str,
    ) -> None:
        """An identity we cannot establish as ours means advance (#1334).

        An unreadable neighbour and a neighbour with no ``## Source Fragments``
        bullet both fail to establish ownership. Today both are silently
        replaced, because the writer only asks whether the *name* is the one it
        wants.

        Args:
            detector: The detector under test.
            vault_path: Vault fixture with ``08-Decisions/Active`` present.
            shape: Key into :data:`_DAMAGED_DECISION_NEIGHBOURS`.
        """
        stem = f"{date.today().isoformat()}-{_PORTLAND_SLUG}"
        neighbour = vault_path / "08-Decisions" / "Active" / f"{stem}.md"
        neighbour.write_text(_DAMAGED_DECISION_NEIGHBOURS[shape], encoding="utf-8")
        original_bytes = neighbour.read_bytes()

        written = detector.create_decision_note(
            _candidate("frag-new", _PORTLAND_TITLE),
            vault_path,
        )

        assert written.name == f"{stem}-1.md"
        assert neighbour.read_bytes() == original_bytes
        assert _source_fragment_of(written) == "frag-new"
        assert len(_active_notes(vault_path)) == 2


class TestDecisionOrdinalCap:
    """The ordinal probe loop is bounded and fails loudly (#1334)."""

    def test_the_module_declares_a_named_ordinal_cap(self) -> None:
        """The bound is a named module constant, not an inline magic number.

        No exact value is pinned: the contract is only that the cap is high
        enough that no real vault reaches it, mirroring
        ``creek/vault/writer.py``'s ``_MAX_FILENAME_COLLISION_RETRIES``.
        """
        assert hasattr(decisions_mod, "_MAX_FILENAME_ORDINALS"), (
            "#1334: creek.generate.decisions must name its ordinal-loop bound "
            "_MAX_FILENAME_ORDINALS so tests can lower it without creating "
            "thousands of files"
        )
        assert decisions_mod._MAX_FILENAME_ORDINALS >= 1000

    def test_exhausting_the_ordinals_raises_runtime_error(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every probed path belongs to a stranger — raise, never spin or clobber.

        The cap is patched down rather than seeding its production value's
        worth of files; the production value is asserted separately above.
        """
        # ``raising=False`` so that, before the fix lands, this test fails on
        # the *behaviour* below rather than erroring on a missing attribute.
        monkeypatch.setattr(decisions_mod, "_MAX_FILENAME_ORDINALS", 3, raising=False)
        stem = f"{date.today().isoformat()}-{_PORTLAND_SLUG}"
        for index, suffix in enumerate(("", "-1", "-2")):
            _seed_decision_note(
                vault_path,
                filename=f"{stem}{suffix}.md",
                source_id=f"frag-stranger-{index}",
                decision_id=f"decision-stranger{index}",
            )

        with pytest.raises(RuntimeError, match="unique filename") as excinfo:
            detector.create_decision_note(
                _candidate("frag-homeless", _PORTLAND_TITLE),
                vault_path,
            )

        assert stem in str(excinfo.value)
        assert len(_active_notes(vault_path)) == 3

    def test_the_last_ordinal_within_the_cap_is_still_used(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cap of 3 probes three paths — the natural one, ``-1`` and ``-2``.

        The boundary companion to the exhaustion test: without it, an
        off-by-one that gives up an ordinal early would go unnoticed.
        """
        # ``raising=False`` so that, before the fix lands, this test fails on
        # the *behaviour* below rather than erroring on a missing attribute.
        monkeypatch.setattr(decisions_mod, "_MAX_FILENAME_ORDINALS", 3, raising=False)
        stem = f"{date.today().isoformat()}-{_PORTLAND_SLUG}"
        for index, suffix in enumerate(("", "-1")):
            _seed_decision_note(
                vault_path,
                filename=f"{stem}{suffix}.md",
                source_id=f"frag-stranger-{index}",
                decision_id=f"decision-stranger{index}",
            )

        written = detector.create_decision_note(
            _candidate("frag-late", _PORTLAND_TITLE),
            vault_path,
        )

        assert written.name == f"{stem}-2.md"
        assert _source_fragment_of(written) == "frag-late"
        assert len(_active_notes(vault_path)) == 3


# ---------------------------------------------------------------------------
# #1431 — the unconditional intimate screen in generate_decisions
#
# Every case here is driven at ``PrivacyTierOverride.ALL`` on purpose. ALL is
# the widest ceiling — the one ``creek fill`` states for every step and the one
# ``creek report`` uses when no ``--include-tier`` is typed — so it is the
# ceiling at which the ordinary rank cutoff admits everything and the screen is
# the only thing left. A case pinned at ``OPEN`` would pass on the cutoff alone
# and prove nothing.
# ---------------------------------------------------------------------------


def _tiered_fragment(frag_id: str, title: str, tier: PrivacyTier) -> Fragment:
    """Return a decision-signalling fragment at *tier*.

    The title must open with ``"Should I"`` or
    ``DecisionDetector._detect_keywords`` never flags it and every assertion
    below would pass for the wrong reason.

    Args:
        frag_id: Fragment id.
        title: Fragment title — the value that reaches the note's filename.
        tier: The declared privacy tier.

    Returns:
        The fragment.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2025, 3, 10, 14, 0, 0),
        frequency=FrequencyClassification(primary=Frequency.F1),
        wavelength=WavelengthClassification(phase=Phase.RISING),
        voice=VoiceClassification(confidence=Confidence.EXPLORING),
        praxis_potential=PraxisPotential.EXPLICIT,
        privacy_tier=tier,
    )


def test_generate_decisions_screens_an_intimate_fragment_at_the_all_ceiling(
    vault_path: Path,
) -> None:
    """An ``intimate`` fragment gets no Decision note, even at ``ALL`` (#1431).

    ``ALL`` is the operator's explicit "show me everything" override for the
    tier *ceiling*, and for the other five reports that is the end of it. It is
    not the end of it here, because a Decision note's filename is the source
    fragment's title sanitised and that filename is visible to anything that
    lists the directory — Obsidian's file pane, ``ls``, Spotlight, a sync
    client — with no front matter attached to declare what it came from.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-intimate", "Should I leave my marriage", PrivacyTier.INTIMATE
        ),
    )

    written = generate_decisions(vault_path, override=PrivacyTierOverride.ALL).notes

    assert written == ()
    assert not list((vault_path / "08-Decisions" / "Active").glob("*.md"))


def test_generate_decisions_still_writes_an_open_fragments_note(
    vault_path: Path,
) -> None:
    """Positive control: the screen drops intimate content, not all content.

    Without this,
    :func:`test_generate_decisions_screens_an_intimate_fragment_at_the_all_ceiling`
    is satisfied by a generator that writes nothing ever.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment("frag-open", "Should I buy a bicycle", PrivacyTier.OPEN),
    )

    written = generate_decisions(vault_path, override=PrivacyTierOverride.ALL).notes

    assert len(written) == 1
    assert "Should-I-buy-a-bicycle" in written[0].name


def test_generate_decisions_admits_an_explicitly_unclassified_fragment(
    vault_path: Path,
) -> None:
    """``privacy_tier: unclassified`` written out is admitted, not screened.

    The screen fails *closed on a missing key*, which is not the same rule as
    "screen anything that isn't neatly tiered". An explicit ``unclassified`` is
    a statement the pipeline made — ``VaultWriter._write_model`` serialises
    ``model_dump(mode="json")``, so it is what an unclassified fragment
    actually looks like on disk — and it ranks with ``personal``, below the
    screen. This is the assertion that stops the fail-closed reader being
    widened by a later edit into a blanket "untidy means intimate" rule.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-unclassified",
            "Should I take the job",
            PrivacyTier.UNCLASSIFIED,
        ),
    )

    written = generate_decisions(vault_path, override=PrivacyTierOverride.ALL).notes

    assert len(written) == 1
    assert "Should-I-take-the-job" in written[0].name


def test_generate_decisions_screens_a_fragment_with_no_privacy_tier_key(
    vault_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A note with no ``privacy_tier:`` key at all is withheld (#1431).

    This is the case that decides *which* tier reader the screen uses, and it
    is the mutant-killer for that choice. The model reader
    (``tier_of``/``fragment.privacy_tier``) resolves a missing key to
    ``unclassified`` and would admit this fragment — writing
    ``08-Decisions/Active/<date>-Should-I-leave-my-marriage-with-SECRET.md``
    on the day the fix merges. ``raw_privacy_tier`` reads the raw front matter
    and fails closed to ``intimate``, which is the same answer
    ``tests/test_mcp_report_tier_ceiling.py``'s
    ``test_fragment_with_no_privacy_tier_key_fails_closed_to_intimate``
    already pins for report artifacts.

    The fragment is hand-written rather than seeded through ``_seed_fragment``
    precisely because ``_seed_fragment`` dumps the model and therefore always
    emits the key — the keyless shape only exists for legacy and hand-authored
    notes.
    """
    frags = vault_path / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "frag-notier.md").write_text(
        "---\ntype: fragment\nid: frag-notier\n"
        'title: "Should I leave my marriage with SECRET"\n'
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="creek.generate.decisions"):
        written = generate_decisions(
            vault_path,
            override=PrivacyTierOverride.ALL,
        ).notes

    assert written == ()
    assert not list((vault_path / "08-Decisions" / "Active").glob("*.md"))
    assert any("withheld" in r.getMessage() for r in caplog.records), (
        "a fail-closed screen that disables the report must say so; "
        f"records={[r.getMessage() for r in caplog.records]}"
    )


def test_generate_decisions_stays_idempotent_with_the_screen(
    vault_path: Path,
) -> None:
    """A second run after the screen writes nothing and mutates no bytes (#1431).

    The screen runs *before* detection, so the withheld fragment never reaches
    the ``_existing_decision_fragment_ids`` gate or the #1334 ownership
    resolution in ``_resolve_decision_note_path``. Those two are what stop the
    admitted fragment's note being re-written on every run, and a screen that
    perturbed the fragment list could have made the second run re-detect and
    clobber. Bytes are compared, not just the return value: a rewrite with a
    fresh ``id:`` is exactly the oscillation #1334 fixed.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment("frag-open", "Should I buy a bicycle", PrivacyTier.OPEN),
    )
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-intimate", "Should I leave my marriage", PrivacyTier.INTIMATE
        ),
    )

    first = generate_decisions(vault_path, override=PrivacyTierOverride.ALL).notes
    before = {p: p.read_bytes() for p in (vault_path / "08-Decisions").rglob("*.md")}
    second = generate_decisions(vault_path, override=PrivacyTierOverride.ALL).notes
    after = {p: p.read_bytes() for p in (vault_path / "08-Decisions").rglob("*.md")}

    assert len(first) == 1
    assert second == ()
    assert after == before


def test_generate_decisions_counts_only_screened_not_ceiling_refused_fragments(
    vault_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The withheld count covers the intimate screen only, not the ceiling (#1431).

    ``_admitted_decision_fragments`` evaluates ``within_ceiling`` *first* and
    counts nothing it refuses, because a ceiling refusal is what the operator
    asked for and warning about it would be noise. Only the unconditional
    screen — the rule no caller asked for and no caller can lift — is counted.

    One vault, two ceilings, is what makes that distinction observable. The
    *same* intimate fragment is ceiling-refused at ``OPEN`` (silent) and
    screen-refused at ``ALL`` (counted). Asserting the count at one ceiling
    alone would be satisfied by a helper that counted every exclusion.

    Since #1487 the count is also part of the *return value*, and both are
    asserted here. The log assertions alone would be satisfied by a
    ``DecisionsReport.withheld`` hardwired to ``0`` — which is exactly the
    field ``creek.cli._report_decisions`` branches on, so a wrong value there
    silently restores the "no new decision candidates found" lie the issue is
    about. The two ceilings pin both directions of that field.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment("frag-open", "Should I buy a bicycle", PrivacyTier.OPEN),
    )
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-intimate",
            "Should I leave my marriage",
            PrivacyTier.INTIMATE,
        ),
    )

    with caplog.at_level("WARNING", logger="creek.generate.decisions"):
        at_open = generate_decisions(vault_path, override=PrivacyTierOverride.OPEN)
    ceiling_refused = [r for r in caplog.records if "withheld" in r.getMessage()]
    caplog.clear()

    with caplog.at_level("WARNING", logger="creek.generate.decisions"):
        at_all = generate_decisions(vault_path, override=PrivacyTierOverride.ALL)
    screen_refused = [
        r.getMessage() for r in caplog.records if "withheld" in r.getMessage()
    ]

    assert ceiling_refused == [], (
        "a fragment the operator's own ceiling excluded was counted as "
        f"withheld: {[r.getMessage() for r in ceiling_refused]}"
    )
    assert len(screen_refused) == 1, screen_refused
    assert "1 fragment" in screen_refused[0], screen_refused[0]
    assert at_open.withheld == 0, (
        "#1487: the ceiling refusal leaked into the returned count, so the "
        "CLI would announce a withholding the operator asked for."
    )
    assert at_all.withheld == 1, (
        "#1487: the screen refusal never reached the return value, so the "
        "CLI has nothing to announce and prints 'no new decision candidates "
        "found' over a report it could not fully read."
    )


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (PrivacyTierOverride.OPEN, 0),
        (PrivacyTierOverride.PERSONAL, 0),
        (PrivacyTierOverride.INTIMATE, 1),
        (PrivacyTierOverride.ALL, 1),
    ],
)
def test_generate_decisions_withheld_count_across_every_ceiling(
    vault_path: Path,
    override: PrivacyTierOverride,
    expected: int,
) -> None:
    """The withheld count is nonzero at *two* ceilings, not one (#1487).

    This pins a claim that was wrong the first time it was written down. The
    reasoning recorded in ``creek_mcp/tools/report.py::_generate_decisions``
    for not widening the MCP envelope originally said the count is
    "structurally always zero except at ``ceiling=all``". It is not.
    :func:`~creek.classify.privacy_filter.tier_within_override` admits a tier
    when its rank is ``<=`` the ceiling's, and ``INTIMATE`` ranks equal to
    itself, so an intimate fragment clears ``ceiling=intimate`` as well and
    only then meets the unconditional screen that counts it.

    Enumerating **all four** overrides rather than the two interesting ones is
    the point: a spot-check at ``OPEN`` and ``ALL`` is exactly what let the
    wrong claim stand, because both endpoints agree with it. The two interior
    ceilings are where it fails.

    Args:
        vault_path: Temporary vault root.
        override: The tier ceiling under test.
        expected: The withheld count that ceiling should produce.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment("frag-open", "Should I buy a bicycle", PrivacyTier.OPEN),
    )
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-intimate",
            "Should I leave my marriage",
            PrivacyTier.INTIMATE,
        ),
    )

    report = generate_decisions(vault_path, override=override)

    assert report.withheld == expected, (
        f"at ceiling={override.value} the intimate screen should have counted "
        f"{expected} fragment(s), not {report.withheld}. The ceiling is "
        "evaluated first, so it decides whether the screen ever sees the "
        "fragment at all."
    )


# ---------------------------------------------------------------------------
# #1487 — one wording, one source of it, and a return type no caller can misread
#
# The count used to reach ``logger.warning`` and die there, so ``creek report
# --type decisions`` printed "no new decision candidates found" over a vault
# whose only candidate had been refused. The fix returns the count and shares
# one wording function between the log and the console; these tests pin the
# wording, the ``None`` at zero, and the shape of the return value.
# ---------------------------------------------------------------------------

_EXPECTED_WITHHELD_NOTICE_ONE = (
    "1 fragment(s) withheld from the decisions report: intimate tier, or no "
    "privacy_tier key at all (which fails closed to intimate). Re-run `creek "
    "classify` to tier a keyless note — a self-authored journal note is "
    "tiered intimate and stays out — or set privacy_tier by hand. A tier "
    "already recorded as intimate is never lowered."
)
"""The exact operator-facing wording at ``withheld == 1`` (#1487).

Pinned in full, once, here — ``tests/test_cli.py`` asserts distinctive
substrings of it against stdout rather than restating it, so there is only one
copy of the sentence to keep true.

Every clause is load-bearing and was checked by execution before being written:

* "fragment(s)", never "candidate(s)" — the count is taken *before* detection
  and includes withheld fragments that are not decision candidates at all.
* "or no privacy_tier key at all (which fails closed to intimate)" — the
  keyless case is the common one in a hand-written or legacy vault, and it is
  the one an operator has no other way to diagnose.
* "Re-run `creek classify` … — a self-authored journal note is tiered intimate
  and stays out —" is stated *with* its exception because
  ``creek/classify/privacy_pass.py`` assigns a tier outright to a keyless note
  (the ratchet binds only an already-concrete tier), while
  ``creek/classify/privacy.py`` classifies a self-authored journal note
  ``INTIMATE`` unconditionally. Promising the re-run alone would print a new
  false statement — the same class of defect this issue exists to remove.
* "A tier already recorded as intimate is never lowered" — the ratchet, which
  is what makes a hand edit the remedy for that case and not the first one.

No square brackets anywhere: ``creek.cli.console`` is a Rich console and would
eat them as markup.
"""


def test_withheld_notice_is_none_when_nothing_was_withheld() -> None:
    """``withheld_notice(0)`` is ``None``, not an empty or zero-count string.

    ``None`` is what makes the notice *conditional* by construction: the caller
    writes ``if (notice := withheld_notice(n)) is not None``, so a truthy
    sentinel here would print "0 fragment(s) withheld …" on every clean run.
    Kills the mutant that returns the formatted string unconditionally.
    """
    assert withheld_notice(0) is None


@pytest.mark.parametrize("count", [1, 2, 17])
def test_withheld_notice_states_its_count_and_carries_no_markup(count: int) -> None:
    """The notice interpolates the count and stays free of Rich markup (#1487).

    Three counts, because a notice that hardcodes ``1`` — the mutant this
    parametrize exists for — passes any single-count assertion. The
    square-bracket guard is not cosmetic: the console prints this string
    through Rich, which silently eats ``[anything]`` as a style tag, so a
    bracketed clause would vanish from the operator's terminal while remaining
    visible in the log.

    Args:
        count: Number of withheld fragments.
    """
    notice = withheld_notice(count)

    assert notice is not None
    assert notice.startswith(f"{count} fragment(s) withheld"), notice
    assert "withheld" in notice
    assert "[" not in notice, notice
    assert "]" not in notice, notice


def test_withheld_notice_states_the_exact_remedy() -> None:
    """The full wording at ``withheld == 1`` is pinned verbatim (#1487).

    This is the only full copy of the sentence in the suite. It is pinned by
    equality rather than by substring because every clause was chosen against
    an execution check of what ``creek classify`` actually does to a keyless
    self-authored journal note — see
    :data:`_EXPECTED_WITHHELD_NOTICE_ONE` — and a paraphrase would quietly
    reintroduce a remedy that does not work.

    It also holds the two substrings older tests depend on: ``"withheld"``
    (``tests/test_cli.py``'s log assertion) and ``"1 fragment"``
    (``test_generate_decisions_counts_only_screened_not_ceiling_refused_fragments``).
    """
    assert withheld_notice(1) == _EXPECTED_WITHHELD_NOTICE_ONE


def test_decisions_report_is_not_iterable() -> None:
    """``DecisionsReport`` must not be unpackable or iterable (#1487).

    ``creek_mcp/tools/report.py`` does ``list(generate_decisions(...))``. Had
    the new return value been a tuple or a NamedTuple, that call site would
    have kept type-checking and kept running, and would have silently started
    returning ``[[Path, …], 0]`` — a list whose first element is a list and
    whose second is an int — into the MCP ``report_paths`` envelope. A plain
    dataclass turns that into a loud ``TypeError`` no call site can miss.

    ``notes`` is asserted to be a ``tuple`` for the same reason the empty-case
    assertions elsewhere read ``== ()``: an immutable sequence cannot be
    appended to by a caller that mistakes it for the old ``list[Path]``.
    """
    report = DecisionsReport(notes=(), withheld=0)

    assert isinstance(report.notes, tuple)
    with pytest.raises(TypeError):
        list(report)


def test_generate_decisions_logs_exactly_the_shared_notice(
    vault_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The logged warning is byte-identical to ``withheld_notice`` (#1487).

    The log and the console are two audiences for one fact, and the whole
    point of routing both through ``withheld_notice`` is that they cannot
    drift — an operator reading the terminal and a maintainer reading the log
    must not be given two different explanations of the same refusal. This
    kills the mutant that keeps the old inline ``logger.warning`` format string
    alongside a new console message: both would contain "withheld", both would
    contain the count, and every other assertion in the suite would still pass.

    Args:
        vault_path: Vault fixture.
        caplog: Pytest log-capture fixture.
    """
    _seed_fragment(
        vault_path,
        _tiered_fragment(
            "frag-intimate",
            "Should I leave my marriage",
            PrivacyTier.INTIMATE,
        ),
    )

    with caplog.at_level("WARNING", logger="creek.generate.decisions"):
        report = generate_decisions(vault_path, override=PrivacyTierOverride.ALL)

    assert report.withheld == 1
    logged = [
        r.getMessage() for r in caplog.records if r.name == "creek.generate.decisions"
    ]
    assert logged == [withheld_notice(report.withheld)], (
        "#1487: the log wording and withheld_notice() have drifted apart, so "
        "the console and the log now explain the same refusal differently."
    )


# ---------------------------------------------------------------------------
# Issue #1448: the module-level coercers and lookups
# ---------------------------------------------------------------------------


class TestDetectPatternRequiresExploringConfidence:
    """``_detect_pattern`` demands *all three* signals, not just the frequencies.

    The confidence arm is the one nothing had driven: every existing
    pattern fixture already carries ``exploring``, so a mutant deleting
    the check would have gone unnoticed. A fragment that is explicit and
    carries the F1+F5 pair but has *settled* confidence is the exact
    shape that must not be flagged — the decision has already been made,
    so surfacing it is noise.
    """

    @staticmethod
    def _fragment(confidence: Confidence) -> Fragment:
        """Return an F1+F5, explicit fragment at the given *confidence*."""
        return Fragment(
            id="frag-conf0001",
            title="Structure and meaning",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=datetime(2025, 3, 12, 9, 0, 0),
            frequency=FrequencyClassification(
                primary=Frequency.F1,
                secondary=[Frequency.F5],
            ),
            wavelength=WavelengthClassification(phase=Phase.PEAKING),
            voice=VoiceClassification(confidence=confidence),
            praxis_potential=PraxisPotential.EXPLICIT,
        )

    def test_exploring_confidence_matches(self) -> None:
        """The positive control: exploring + explicit + F1/F5 does match."""
        assert DecisionDetector._detect_pattern(self._fragment(Confidence.EXPLORING))

    def test_settled_confidence_does_not_match(self) -> None:
        """Everything else identical, settled confidence is rejected."""
        assert not DecisionDetector._detect_pattern(self._fragment(Confidence.SETTLED))


class TestJaccard:
    """``_jaccard`` scores set overlap and never divides by zero.

    Recorded as a finding rather than covered: the second guard,
    ``if not union: return 0.0`` (``decisions.py:660-661``), is
    **unreachable**. ``a | b`` is empty if and only if both ``a`` and
    ``b`` are empty, which the preceding ``if not a and not b`` has
    already returned for. No input can reach it, so no honest test can
    execute it, and the PR proposes deleting it rather than fabricating a
    path to it.
    """

    def test_both_empty_scores_zero(self) -> None:
        """Two empty sets score 0.0 rather than raising ZeroDivisionError."""
        assert decisions_mod._jaccard(set(), set()) == 0.0

    def test_one_empty_scores_zero(self) -> None:
        """No overlap with an empty set, but the union is non-empty."""
        assert decisions_mod._jaccard(set(), {"a"}) == 0.0
        assert decisions_mod._jaccard({"a"}, set()) == 0.0

    def test_partial_overlap_is_the_exact_ratio(self) -> None:
        """The score is intersection over union, asserted exactly."""
        assert decisions_mod._jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_identical_sets_score_one(self) -> None:
        """Full overlap scores 1.0."""
        assert decisions_mod._jaccard({"a", "b"}, {"a", "b"}) == 1.0


class TestListify:
    """``_listify`` coerces a frontmatter scalar/list into a list of strings."""

    def test_list_becomes_strings(self) -> None:
        """Every element is stringified, order preserved."""
        assert decisions_mod._listify(["F1", 2, None]) == ["F1", "2", "None"]

    def test_bare_string_is_wrapped(self) -> None:
        """A single string becomes a one-element list, not a list of characters.

        The failure this pins is real and silent: ``list("F1")`` yields
        ``["F", "1"]``, which would then never match a frequency code and
        would quietly empty every affinity comparison.
        """
        assert decisions_mod._listify("F1") == ["F1"]

    @pytest.mark.parametrize("value", [42, 3.5, {"a": 1}, object()])
    def test_unsupported_scalar_yields_empty(self, value: object) -> None:
        """A value that is neither list nor string coerces to no entries."""
        assert decisions_mod._listify(value) == []

    @pytest.mark.parametrize("value", [None, "", [], 0])
    def test_falsy_values_yield_empty(self, value: object) -> None:
        """Falsy input short-circuits to an empty list."""
        assert decisions_mod._listify(value) == []


class TestFindDecisionByIdWalksBothSubfolders:
    """``_find_decision_by_id`` tolerates a half-scaffolded decisions folder."""

    def test_missing_active_folder_still_searches_archive(
        self,
        tmp_path: Path,
    ) -> None:
        """With only ``Archive`` present, a note there is still found.

        The absent ``Active`` folder is the ``continue`` arm; without it
        the walk would raise instead of falling through to ``Archive``.
        """
        archive = tmp_path / "08-Decisions" / "Archive"
        archive.mkdir(parents=True)
        note = archive / "archived.md"
        note.write_text(
            "---\ntype: decision\nid: decision-arch0001\n---\nbody\n",
            encoding="utf-8",
        )

        found = DecisionDetector()._find_decision_by_id(
            "decision-arch0001",
            tmp_path / "08-Decisions",
        )

        assert found == note

    def test_absent_decisions_tree_returns_none(self, tmp_path: Path) -> None:
        """Neither subfolder present means "not found", not an exception."""
        assert not (tmp_path / "08-Decisions").exists()

        found = DecisionDetector()._find_decision_by_id(
            "decision-missing1",
            tmp_path / "08-Decisions",
        )

        assert found is None

    def test_unmatched_id_exhausts_both_folders(self, tmp_path: Path) -> None:
        """A populated tree with no matching id returns None.

        This drives the loop all the way to its fallthrough rather than
        returning early, which is the branch a test that only ever
        searched for a *present* id would leave dead.
        """
        for subfolder in ("Active", "Archive"):
            folder = tmp_path / "08-Decisions" / subfolder
            folder.mkdir(parents=True)
            (folder / f"{subfolder}.md").write_text(
                f"---\ntype: decision\nid: decision-{subfolder.lower()}1\n---\nbody\n",
                encoding="utf-8",
            )

        found = DecisionDetector()._find_decision_by_id(
            "decision-absent01",
            tmp_path / "08-Decisions",
        )

        assert found is None


class TestExistingDecisionFragmentIds:
    """``_existing_decision_fragment_ids`` indexes what the vault already holds."""

    def test_missing_subfolder_is_stepped_over(self, tmp_path: Path) -> None:
        """Only ``Archive`` present: its ids are still indexed.

        The absent ``Active`` folder is the ``continue`` arm. Asserting
        the exact id set (not merely "non-empty") is what proves the walk
        reached ``Archive`` rather than bailing out of the loop entirely.
        """
        archive = tmp_path / "08-Decisions" / "Archive"
        archive.mkdir(parents=True)
        (archive / "note.md").write_text(
            "---\ntype: decision\nid: decision-arch0001\n---\n"
            "## Source Fragments\n\n- frag-archived1\n",
            encoding="utf-8",
        )

        assert decisions_mod._existing_decision_fragment_ids(tmp_path) == {
            "frag-archived1",
        }

    def test_note_without_a_source_is_not_indexed(self, tmp_path: Path) -> None:
        """A decision note recording no source fragment contributes nothing.

        This is the ``fragment_id is not None`` arm. The good note beside
        it is what keeps the assertion from being satisfied by a walk
        that indexed nothing at all.
        """
        active = tmp_path / "08-Decisions" / "Active"
        active.mkdir(parents=True)
        (active / "sourced.md").write_text(
            "---\ntype: decision\nid: decision-sourced1\n---\n"
            "## Source Fragments\n\n- frag-sourced1\n",
            encoding="utf-8",
        )
        # No ``## Source Fragments`` section at all: the extractor cannot
        # establish this note's identity, so it must contribute nothing.
        (active / "unsourced.md").write_text(
            "---\ntype: decision\nid: decision-unsrc01\n---\n"
            "## Context\n\n- not a source bullet\n",
            encoding="utf-8",
        )

        assert decisions_mod._existing_decision_fragment_ids(tmp_path) == {
            "frag-sourced1",
        }


# ---------------------------------------------------------------------------
# Issue #1430: the Active/Archive move renamed straight over an occupied path
#
# ``update_decision_phase`` moved a note with a bare ``Path.rename``, which on
# POSIX *silently replaces* an existing destination. ``08-Decisions/Archive``
# is addressed by the same ``<date>-<sanitised title>`` stem that #1334 proved
# is a name and not an identity, so archiving a decision whose stem was already
# taken in ``Archive`` destroyed the note already there — no exception, no
# return-value signal, and the operator's own writing gone.
#
# The move now resolves its destination through the same
# ``_resolve_decision_note_path`` the writers use, passing ``None`` for the
# identity: a move carries a note that already exists in full, so there is
# nothing at the destination it could be a legitimate refresh *of*. Claiming no
# ownership makes every occupied path a stranger's, so the move takes the first
# free ordinal instead. Unlike creation, that cannot accumulate copies — the
# source name is freed by the very move that takes the destination one, so a
# note shuttled between the folders reclaims its natural name every time.
# ---------------------------------------------------------------------------

_MOVER_NAME = "2026-04-01-Should-I-move-to-Portland.md"
"""Filename shared by the note being archived and the stranger already there."""

_MOVER_STEM = "2026-04-01-Should-I-move-to-Portland"
"""``_MOVER_NAME`` without its extension — the stem the move naturally wants."""

_OPERATOR_PROSE = "I keep coming back to the light in November."
"""A line only the operator could have written; a clobber destroys it."""


def _archive_notes(vault: Path) -> list[Path]:
    """Return every note in ``08-Decisions/Archive``, sorted by name.

    Args:
        vault: Vault root.

    Returns:
        Sorted list of markdown paths in the Archive folder.
    """
    return sorted((vault / "08-Decisions" / "Archive").glob("*.md"))


class TestDecisionPhaseMoveNeverOverwrites:
    """Archiving a decision never destroys a note already at the target name."""

    @pytest.fixture()
    def mover(self, vault_path: Path) -> Path:
        """Seed the Active note that the tests below archive.

        Args:
            vault_path: Vault root fixture.

        Returns:
            Path to the seeded note in ``08-Decisions/Active``.
        """
        return _seed_decision_note(
            vault_path,
            filename=_MOVER_NAME,
            source_id="frag-mine",
            decision_id="decision-mover0001",
        )

    def test_a_stranger_at_the_target_name_survives_the_move(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        mover: Path,
    ) -> None:
        """The stranger's bytes are unchanged and both notes exist (#1430).

        Bytes, not merely existence: ``Path.rename`` replaces the destination
        inode outright, so an existence check alone is satisfied by the very
        file that overwrote it.
        """
        stranger = _seed_decision_note(
            vault_path,
            filename=_MOVER_NAME,
            source_id="frag-other",
            decision_id="decision-stranger1",
            subfolder="Archive",
            status="enacted",
            prose=_OPERATOR_PROSE,
        )
        before = stranger.read_bytes()

        moved = detector.update_decision_phase(
            "decision-mover0001",
            "enacted",
            vault_path,
        )

        assert stranger.read_bytes() == before, (
            "#1430: the note already in Archive was overwritten by the move"
        )
        assert moved.name == f"{_MOVER_STEM}-1.md"
        assert _source_fragment_of(moved) == "frag-mine"
        assert frontmatter.load(str(moved))["status"] == "enacted"
        assert not mover.exists()
        assert len(_archive_notes(vault_path)) == 2

    @pytest.mark.parametrize("shape", sorted(_DAMAGED_DECISION_NEIGHBOURS))
    def test_a_neighbour_of_unestablished_identity_survives_the_move(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        mover: Path,
        shape: str,
    ) -> None:
        """An identity we cannot read is not an identity we may overwrite.

        Args:
            detector: Detector under test.
            vault_path: Vault root fixture.
            mover: The Active note being archived.
            shape: Key into :data:`_DAMAGED_DECISION_NEIGHBOURS`.
        """
        neighbour = vault_path / "08-Decisions" / "Archive" / _MOVER_NAME
        neighbour.write_text(_DAMAGED_DECISION_NEIGHBOURS[shape], encoding="utf-8")
        before = neighbour.read_bytes()

        moved = detector.update_decision_phase(
            "decision-mover0001",
            "enacted",
            vault_path,
        )

        assert neighbour.read_bytes() == before, (
            f"#1430: the {shape} neighbour was clobbered by the move"
        )
        assert moved.name == f"{_MOVER_STEM}-1.md"
        assert not mover.exists()
        assert len(_archive_notes(vault_path)) == 2

    def test_a_second_note_recording_the_same_fragment_survives_the_move(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        mover: Path,
    ) -> None:
        """Sharing a source fragment does not make two notes one note (#1430).

        This is where a move differs from a write. ``create_decision_note``
        treats a note recording the candidate's own fragment as *its own* and
        refreshes it in place, because it is regenerating that note from the
        candidate. A move regenerates nothing: the occupant is a separate
        decision, with its own ``id:`` and its own prose, and replacing it
        would destroy a file no later run could rebuild.
        """
        twin = _seed_decision_note(
            vault_path,
            filename=_MOVER_NAME,
            source_id="frag-mine",
            decision_id="decision-twin00001",
            subfolder="Archive",
            status="reflecting",
            prose=_OPERATOR_PROSE,
        )
        before = twin.read_bytes()

        moved = detector.update_decision_phase(
            "decision-mover0001",
            "enacted",
            vault_path,
        )

        assert twin.read_bytes() == before, (
            "#1430: a note sharing the source fragment is still a second note"
        )
        assert moved.name == f"{_MOVER_STEM}-1.md"
        assert frontmatter.load(str(moved))["id"] == "decision-mover0001"
        assert len(_archive_notes(vault_path)) == 2

    def test_shuttling_between_the_folders_reclaims_the_natural_name(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        mover: Path,
    ) -> None:
        """Ordinals must not creep on every archive/unarchive cycle (#1430).

        The move frees its own source name, so first-free resolution converges
        where a creation-time counter-suffix would mint a new copy per call.
        """
        archived = detector.update_decision_phase(
            "decision-mover0001",
            "enacted",
            vault_path,
        )
        reactivated = detector.update_decision_phase(
            "decision-mover0001",
            "deliberating",
            vault_path,
        )
        rearchived = detector.update_decision_phase(
            "decision-mover0001",
            "enacted",
            vault_path,
        )

        assert archived.name == _MOVER_NAME
        assert reactivated.name == _MOVER_NAME
        assert rearchived.name == _MOVER_NAME
        assert len(_archive_notes(vault_path)) == 1
        assert not _active_notes(vault_path)

    def test_an_unallocatable_destination_leaves_the_source_untouched(
        self,
        detector: DecisionDetector,
        vault_path: Path,
        mover: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exhausting the ordinals raises before the note is rewritten (#1430).

        The destination is resolved *before* the status rewrite, so a folder
        with no free name leaves the vault exactly as it was rather than
        half-updated: a note stamped ``enacted`` still sitting in ``Active``.
        """
        monkeypatch.setattr(decisions_mod, "_MAX_FILENAME_ORDINALS", 1)
        _seed_decision_note(
            vault_path,
            filename=_MOVER_NAME,
            source_id="frag-other",
            decision_id="decision-stranger1",
            subfolder="Archive",
        )
        before = mover.read_bytes()

        with pytest.raises(RuntimeError, match="unique filename"):
            detector.update_decision_phase(
                "decision-mover0001",
                "enacted",
                vault_path,
            )

        assert mover.read_bytes() == before, (
            "#1430: the status rewrite ran before the destination was resolved"
        )
        assert len(_archive_notes(vault_path)) == 1
