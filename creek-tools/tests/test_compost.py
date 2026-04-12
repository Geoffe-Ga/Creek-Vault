"""Tests for creek.generate.compost — compost tracking for abandoned threads.

Covers the ``CompostTracker`` class (detection, note creation, report
generation) plus the ``CompostCandidate`` dataclass and helper
utilities. All behaviour maps to the acceptance criteria on Issue #40.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.generate.compost import (
    ABANDONMENT_KEYWORDS,
    CompostCandidate,
    CompostTracker,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PraxisPotential,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
)

# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure with the Compost subtree."""
    dirs = [
        "00-Creek-Meta",
        "02-Threads",
        "10-Liminal/Compost",
    ]
    for rel in dirs:
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def reference_now() -> datetime:
    """Reference "now" that all age calculations use in tests."""
    return datetime(2026, 4, 1, 12, 0, 0)


@pytest.fixture()
def tracker(reference_now: datetime) -> CompostTracker:
    """Create a CompostTracker with a deterministic clock."""
    return CompostTracker(now=reference_now)


def _fragment(
    *,
    frag_id: str,
    title: str,
    created: datetime,
    confidence: Confidence | None = None,
    praxis_potential: PraxisPotential = PraxisPotential.NONE,
    threads: list[str] | None = None,
    tags: list[str] | None = None,
) -> Fragment:
    """Build a minimal fragment for testing."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(confidence=confidence),
        praxis_potential=praxis_potential,
        threads=threads or [],
        tags=tags or [],
    )


@pytest.fixture()
def dormant_thread() -> Thread:
    """Thread whose last_seen is beyond the dormancy threshold."""
    return Thread(
        id="thread-dormant1",
        title="Abandoned Sourdough Experiments",
        status=ThreadStatus.DORMANT,
        first_seen=date(2024, 1, 1),
        last_seen=date(2025, 8, 1),
        fragment_count=4,
        frequency_affinity=[Frequency.F5],
        description="Tried sourdough every weekend for months then drifted.",
    )


@pytest.fixture()
def resolved_thread() -> Thread:
    """Thread explicitly marked resolved."""
    return Thread(
        id="thread-resolved1",
        title="Deciding on a thesis topic",
        status=ThreadStatus.RESOLVED,
        first_seen=date(2025, 1, 1),
        last_seen=date(2025, 3, 15),
        fragment_count=6,
        frequency_affinity=[Frequency.F6],
        description="Settled on a direction in March.",
    )


@pytest.fixture()
def active_thread() -> Thread:
    """Thread that is still active (not a compost candidate)."""
    return Thread(
        id="thread-active01",
        title="Daily Meditation Practice",
        status=ThreadStatus.ACTIVE,
        first_seen=date(2025, 6, 1),
        last_seen=date(2026, 3, 20),
        fragment_count=40,
        frequency_affinity=[Frequency.F7],
        description="Ongoing sit practice.",
    )


# ---- ABANDONMENT_KEYWORDS ----


class TestAbandonmentKeywords:
    """Ensure the canonical keyword list meets issue spec."""

    def test_keywords_is_nonempty_tuple(self) -> None:
        """ABANDONMENT_KEYWORDS should be a non-empty tuple of strings."""
        assert isinstance(ABANDONMENT_KEYWORDS, tuple)
        assert len(ABANDONMENT_KEYWORDS) > 0

    def test_keywords_are_lowercase(self) -> None:
        """All keywords must be lowercase (we match case-insensitively)."""
        for keyword in ABANDONMENT_KEYWORDS:
            assert keyword == keyword.lower()

    def test_required_keywords_present(self) -> None:
        """The keywords listed in the issue spec must be present."""
        required = (
            "gave up on",
            "abandoned",
            "never finished",
            "put aside",
            "shelved",
        )
        for keyword in required:
            assert keyword in ABANDONMENT_KEYWORDS


# ---- CompostTracker.detect_compost_candidates ----


class TestDetectCompostCandidates:
    """Detection of compost candidates across threads and fragments."""

    def test_dormant_thread_detected(
        self,
        tracker: CompostTracker,
        dormant_thread: Thread,
    ) -> None:
        """Threads inactive >180 days should surface."""
        candidates = tracker.detect_compost_candidates([dormant_thread], [])
        thread_candidates = [c for c in candidates if c.source_type == "thread"]
        assert any(c.source_id == dormant_thread.id for c in thread_candidates)

    def test_resolved_thread_detected(
        self,
        tracker: CompostTracker,
        resolved_thread: Thread,
    ) -> None:
        """Resolved threads should always be compost candidates."""
        candidates = tracker.detect_compost_candidates([resolved_thread], [])
        assert any(c.source_id == resolved_thread.id for c in candidates)

    def test_active_thread_excluded(
        self,
        tracker: CompostTracker,
        active_thread: Thread,
    ) -> None:
        """Active threads must not surface as compost candidates."""
        candidates = tracker.detect_compost_candidates([active_thread], [])
        thread_ids = {c.source_id for c in candidates}
        assert active_thread.id not in thread_ids

    def test_keyword_fragment_detected(
        self,
        tracker: CompostTracker,
    ) -> None:
        """Fragments whose title contains an abandonment keyword surface."""
        frag = _fragment(
            frag_id="frag-abandon1",
            title="I finally gave up on the novel",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        keyword_hits = [c for c in candidates if c.source_type == "fragment"]
        assert any(c.source_id == frag.id for c in keyword_hits)
        assert "gave up on" in keyword_hits[0].matched_keywords

    def test_fragment_without_keyword_ignored(
        self,
        tracker: CompostTracker,
    ) -> None:
        """Fragments without abandonment keywords should not surface."""
        frag = _fragment(
            frag_id="frag-neutral1",
            title="Notes on a productive morning",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert [c for c in candidates if c.source_id == frag.id] == []

    def test_disappeared_project_detected(
        self,
        tracker: CompostTracker,
        reference_now: datetime,
    ) -> None:
        """Projects (tags) that appear early but vanish become candidates."""
        early = datetime(2024, 1, 15, 12, 0, 0)
        later_cutoff = reference_now - timedelta(days=300)
        fragments = [
            _fragment(
                frag_id=f"frag-proj-{idx}",
                title=f"Greenhouse notes {idx}",
                created=early + timedelta(days=idx),
                tags=["greenhouse"],
            )
            for idx in range(3)
        ]
        # A totally unrelated recent fragment so the fragment list is not empty
        fragments.append(
            _fragment(
                frag_id="frag-current",
                title="Current unrelated fragment",
                created=later_cutoff + timedelta(days=250),
                tags=["other"],
            ),
        )
        candidates = tracker.detect_compost_candidates([], fragments)
        project_candidates = [c for c in candidates if c.source_type == "project"]
        assert any(c.source_id == "greenhouse" for c in project_candidates)

    def test_active_project_not_flagged(
        self,
        tracker: CompostTracker,
        reference_now: datetime,
    ) -> None:
        """Projects with a recent fragment must not be flagged."""
        fragments = [
            _fragment(
                frag_id="frag-p-a",
                title="Piano early",
                created=reference_now - timedelta(days=400),
                tags=["piano"],
            ),
            _fragment(
                frag_id="frag-p-b",
                title="Piano middle",
                created=reference_now - timedelta(days=200),
                tags=["piano"],
            ),
            _fragment(
                frag_id="frag-p-c",
                title="Piano recent",
                created=reference_now - timedelta(days=10),
                tags=["piano"],
            ),
        ]
        candidates = tracker.detect_compost_candidates([], fragments)
        project_ids = {c.source_id for c in candidates if c.source_type == "project"}
        assert "piano" not in project_ids

    def test_empty_inputs_return_empty(self, tracker: CompostTracker) -> None:
        """No threads or fragments → no candidates."""
        assert tracker.detect_compost_candidates([], []) == []

    def test_candidate_captures_energy_fragments(
        self,
        tracker: CompostTracker,
        dormant_thread: Thread,
    ) -> None:
        """Candidates must link to high-confidence fragments as ``energy``."""
        settled_frag = _fragment(
            frag_id="frag-energy1",
            title="Key sourdough insight",
            created=datetime(2025, 7, 15, 9, 0, 0),
            confidence=Confidence.SETTLED,
            threads=[f"[[{dormant_thread.title}]]"],
        )
        low_frag = _fragment(
            frag_id="frag-musing1",
            title="Stray sourdough notes",
            created=datetime(2025, 7, 16, 9, 0, 0),
            confidence=Confidence.MUSING,
            threads=[f"[[{dormant_thread.title}]]"],
        )
        candidates = tracker.detect_compost_candidates(
            [dormant_thread],
            [settled_frag, low_frag],
        )
        thread_cand = next(c for c in candidates if c.source_type == "thread")
        assert settled_frag.id in thread_cand.energy_fragment_ids
        assert low_frag.id not in thread_cand.energy_fragment_ids

    def test_praxis_potential_counts_as_energy(
        self,
        tracker: CompostTracker,
        resolved_thread: Thread,
    ) -> None:
        """Fragments with explicit praxis_potential count as energy."""
        frag = _fragment(
            frag_id="frag-praxis1",
            title="Praxis fragment for thesis",
            created=datetime(2025, 2, 1, 9, 0, 0),
            praxis_potential=PraxisPotential.EXPLICIT,
            threads=[f"[[{resolved_thread.title}]]"],
        )
        candidates = tracker.detect_compost_candidates([resolved_thread], [frag])
        thread_cand = next(c for c in candidates if c.source_type == "thread")
        assert frag.id in thread_cand.energy_fragment_ids


# ---- CompostTracker.create_compost_note ----


class TestCreateCompostNote:
    """Vault note creation for a compost candidate."""

    def test_note_written_to_compost_folder(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Notes must land in ``10-Liminal/Compost/``."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-dormant1",
            title="Abandoned Sourdough Experiments",
            reason="Dormant for 244 days",
            fragment_ids=["frag-1", "frag-2"],
            energy_fragment_ids=["frag-1"],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        assert path.parent == vault_path / "10-Liminal" / "Compost"
        assert path.exists()

    def test_note_has_required_frontmatter(
        self,
        tracker: CompostTracker,
        reference_now: datetime,
        vault_path: Path,
    ) -> None:
        """Frontmatter must record type, reason, fragments, and date."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-dormant1",
            title="Abandoned Sourdough Experiments",
            reason="Dormant for 244 days",
            fragment_ids=["frag-1", "frag-2"],
            energy_fragment_ids=["frag-1"],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert post["type"] == "compost"
        assert post["original_thread"] == "[[thread-dormant1]]"
        assert post["composted_date"] == reference_now.date().isoformat()
        assert post["reason"] == "Dormant for 244 days"
        assert post["fragments"] == ["[[frag-1]]", "[[frag-2]]"]

    def test_note_body_has_required_sections(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """The body must contain the three required narrative sections."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-dormant1",
            title="Abandoned Sourdough Experiments",
            reason="Dormant for 244 days",
            fragment_ids=["frag-1"],
            energy_fragment_ids=["frag-1"],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        body = frontmatter.load(str(path)).content
        assert "## What it was" in body
        assert "## Why it composted" in body
        assert "## What energy remains" in body

    def test_note_lists_energy_fragment_links(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """The energy section must list wiki-links to energy fragments."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-dormant1",
            title="Abandoned Sourdough Experiments",
            reason="Dormant",
            fragment_ids=["frag-1", "frag-2"],
            energy_fragment_ids=["frag-1"],
            matched_keywords=[],
            energy_excerpts=["Key insight about fermentation"],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        body = frontmatter.load(str(path)).content
        assert "[[frag-1]]" in body
        assert "Key insight about fermentation" in body

    def test_note_tagged_compost(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Compost notes must carry the ``#compost`` tag."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-x",
            title="Some thread",
            reason="Resolved",
            fragment_ids=[],
            energy_fragment_ids=[],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        tags = post.get("tags") or []
        assert "compost" in tags

    def test_unknown_reason_recorded(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Missing reason renders as 'unknown' in the body."""
        candidate = CompostCandidate(
            source_type="fragment",
            source_id="frag-a",
            title="Fragment without reason",
            reason="",
            fragment_ids=["frag-a"],
            energy_fragment_ids=[],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        body = frontmatter.load(str(path)).content
        assert "unknown" in body.lower()

    def test_fragment_source_uses_original_fragment_link(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Fragment-sourced candidates link to their fragment, not a thread."""
        candidate = CompostCandidate(
            source_type="fragment",
            source_id="frag-abandon",
            title="I finally gave up on the novel",
            reason="Abandonment keyword: gave up on",
            fragment_ids=["frag-abandon"],
            energy_fragment_ids=[],
            matched_keywords=["gave up on"],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert "original_thread" not in post.metadata
        assert post["original_fragment"] == "[[frag-abandon]]"


# ---- CompostTracker.generate_compost_report ----


class TestGenerateCompostReport:
    """Vault-level compost report generation."""

    def test_report_written_to_compost_root(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """The report is written inside the Compost directory."""
        path = tracker.generate_compost_report(vault_path)
        assert path.parent == vault_path / "10-Liminal" / "Compost"
        assert path.exists()

    def test_report_contains_dataview_query(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Report should use a Dataview query to list compost notes."""
        path = tracker.generate_compost_report(vault_path)
        text = path.read_text(encoding="utf-8")
        assert "```dataview" in text
        assert 'type = "compost"' in text

    def test_report_cross_references_active_threads(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Report should include an active-thread cross-reference section."""
        path = tracker.generate_compost_report(vault_path)
        text = path.read_text(encoding="utf-8")
        assert "Active Threads" in text

    def test_report_lists_existing_compost_notes(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Existing compost notes must appear in the report body."""
        compost_dir = vault_path / "10-Liminal" / "Compost"
        existing = compost_dir / "2025-08-01-abandoned-sourdough.md"
        existing.write_text(
            "---\n"
            "type: compost\n"
            "title: Abandoned Sourdough\n"
            "composted_date: 2025-08-01\n"
            "---\n\n"
            "## What it was\n\nSourdough explorations.\n",
            encoding="utf-8",
        )
        path = tracker.generate_compost_report(vault_path)
        text = path.read_text(encoding="utf-8")
        assert "Abandoned Sourdough" in text

    def test_report_skips_non_compost_notes(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Files that aren't compost notes should be ignored."""
        compost_dir = vault_path / "10-Liminal" / "Compost"
        stranger = compost_dir / "stranger.md"
        stranger.write_text(
            "---\ntype: note\ntitle: Something Else\n---\n\nbody\n",
            encoding="utf-8",
        )
        path = tracker.generate_compost_report(vault_path)
        text = path.read_text(encoding="utf-8")
        assert "Something Else" not in text

    def test_report_ignores_unparseable_compost_file(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Compost files that cannot be parsed should not crash the report."""
        compost_dir = vault_path / "10-Liminal" / "Compost"
        # Binary bytes will make frontmatter.load raise.
        (compost_dir / "broken.md").write_bytes(b"\x00\x01\x02")
        path = tracker.generate_compost_report(vault_path)
        assert path.exists()

    def test_report_regenerates_over_existing_report(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Running the report twice must not re-list the report itself."""
        first = tracker.generate_compost_report(vault_path)
        second = tracker.generate_compost_report(vault_path)
        assert first == second
        text = second.read_text(encoding="utf-8")
        assert "[[_Compost-Report|" not in text

    def test_report_lists_active_threads(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Active threads in ``02-Threads`` should appear in the report."""
        threads_dir = vault_path / "02-Threads"
        (threads_dir / "daily-meditation.md").write_text(
            "---\n"
            "id: thread-active01\n"
            "title: Daily Meditation Practice\n"
            "status: active\n"
            "---\n",
            encoding="utf-8",
        )
        (threads_dir / "resolved-thesis.md").write_text(
            "---\nid: thread-res1\ntitle: Old Thesis\nstatus: resolved\n---\n",
            encoding="utf-8",
        )
        (threads_dir / "broken-thread.md").write_bytes(b"\x00\x01")
        path = tracker.generate_compost_report(vault_path)
        text = path.read_text(encoding="utf-8")
        assert "Daily Meditation Practice" in text
        assert "Old Thesis" not in text

    def test_report_handles_missing_threads_dir(
        self,
        tracker: CompostTracker,
        tmp_path: Path,
    ) -> None:
        """A vault without a threads dir should still produce a report."""
        (tmp_path / "10-Liminal" / "Compost").mkdir(parents=True)
        path = tracker.generate_compost_report(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "No active threads" in text


# ---- Project candidate note generation ----


class TestProjectCandidateNote:
    """Ensure project-sourced candidates produce valid notes."""

    def test_project_note_has_original_project_field(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Project candidates should record ``original_project`` in metadata."""
        candidate = CompostCandidate(
            source_type="project",
            source_id="greenhouse",
            title="greenhouse",
            reason="Project silent for 400 days",
            fragment_ids=["frag-a", "frag-b"],
            energy_fragment_ids=[],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert post["original_project"] == "greenhouse"
        assert "original_thread" not in post.metadata
        assert "original_fragment" not in post.metadata

    def test_note_without_related_fragments_has_fallback(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """A candidate with no fragment IDs uses the fallback sentence."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-empty",
            title="Empty thread",
            reason="Resolved",
            fragment_ids=[],
            energy_fragment_ids=[],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        body = frontmatter.load(str(path)).content
        assert "_No related fragments were recorded._" in body

    def test_matched_keywords_persist_in_frontmatter(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """matched_keywords on a candidate must round-trip into frontmatter."""
        candidate = CompostCandidate(
            source_type="fragment",
            source_id="frag-keyword",
            title="abandoned plans",
            reason="Abandonment keyword: abandoned",
            fragment_ids=["frag-keyword"],
            energy_fragment_ids=[],
            matched_keywords=["abandoned"],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert post["matched_keywords"] == ["abandoned"]

    def test_title_with_special_chars_sanitized(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Non-word characters in titles must not break filename creation."""
        candidate = CompostCandidate(
            source_type="thread",
            source_id="thread-weird",
            title="What?! / a wild ride * indeed",
            reason="Resolved",
            fragment_ids=[],
            energy_fragment_ids=[],
            matched_keywords=[],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        assert path.exists()
        assert "?" not in path.name
        assert "/" not in path.name

    def test_tracker_defaults_to_wall_clock(self) -> None:
        """When ``now`` is omitted, tracker uses a real timestamp."""
        tracker = CompostTracker()
        # Detection on empty inputs should not error and must return empty.
        assert tracker.detect_compost_candidates([], []) == []
