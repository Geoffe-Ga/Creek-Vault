"""Tests for creek.generate.compost — compost tracking for abandoned threads.

Covers the ``CompostTracker`` class (detection, note creation, report
generation) plus the ``CompostCandidate`` dataclass and helper
utilities. All behaviour maps to the acceptance criteria on Issue #40.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import frontmatter
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

from creek.generate.compost import (
    CompostCandidate,
    CompostTracker,
    _AdmittedFragments,
)
from creek.generate.compost_verifier import (
    CompostVerdict,
    CompostVerifierResult,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
)
from creek.time import today_la


class _StubVerifier:
    """Deterministic :class:`SupportsVerifyCompost` for tests.

    Maps fragment titles to a fixed verdict via the ``responses``
    mapping. Unmapped titles default to ``yes`` so tests that only
    care about the embedding gate need not enumerate them.
    """

    def __init__(
        self,
        responses: dict[str, CompostVerifierResult] | None = None,
        default: CompostVerdict = CompostVerdict.YES,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Record the call and return the configured verdict."""
        self.calls.append((title, body))
        if title in self.responses:
            return self.responses[title]
        return CompostVerifierResult(verdict=self.default, reasoning="stub")


def _high_similarity(_text: str) -> float:
    """Similarity stub that always clears the default 0.6 threshold."""
    return 0.95


def _low_similarity(_text: str) -> float:
    """Similarity stub that always falls below the default threshold."""
    return 0.1


def _similarity_by_title(scores: dict[str, float]) -> Callable[[str], float]:
    """Return a similarity fn that maps title substrings to scores."""

    def _fn(text: str) -> float:
        for token, value in scores.items():
            if token in text:
                return value
        return 0.0

    return _fn


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
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
) -> Fragment:
    """Build a minimal fragment for testing.

    The supplied ``created`` is mirrored into ``ingested`` so the
    FEAT-031 ``effective_authored_at`` helper — which falls back to
    ``ingested`` when ``authored_at`` is unset — sees the time the
    test cares about. Without the mirror, every test fragment would
    surface at construction-time wall-clock (tz-aware) and silence
    calculations against a naive ``self._now`` would raise.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        ingested=created,
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(confidence=confidence),
        praxis_potential=praxis_potential,
        threads=threads or [],
        tags=tags or [],
        privacy_tier=privacy_tier,
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

    def test_default_tracker_skips_fragment_detection(
        self,
        tracker: CompostTracker,
    ) -> None:
        """A tracker built without ``similarity_fn`` runs thread + project only."""
        frag = _fragment(
            frag_id="frag-abandon1",
            title="I finally let go of the novel",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert [c for c in candidates if c.source_type == "fragment"] == []

    def test_embedding_gate_accepts_above_threshold(
        self,
        reference_now: datetime,
    ) -> None:
        """Fragments clearing the embedding floor become compost candidates."""
        tracker = CompostTracker(now=reference_now, similarity_fn=_high_similarity)
        frag = _fragment(
            frag_id="frag-let-go",
            title="Letting this thread go",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        fragment_hits = [c for c in candidates if c.source_type == "fragment"]
        assert any(c.source_id == frag.id for c in fragment_hits)
        assert fragment_hits[0].similarity == pytest.approx(0.95)

    def test_embedding_gate_rejects_below_threshold(
        self,
        reference_now: datetime,
    ) -> None:
        """Fragments below the embedding floor are filtered out."""
        tracker = CompostTracker(now=reference_now, similarity_fn=_low_similarity)
        frag = _fragment(
            frag_id="frag-neutral1",
            title="Notes on a productive morning",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert [c for c in candidates if c.source_type == "fragment"] == []

    def test_verifier_no_verdict_drops_candidate(
        self,
        reference_now: datetime,
    ) -> None:
        """A ``no`` verifier verdict suppresses an above-threshold candidate."""
        verifier = _StubVerifier(default=CompostVerdict.NO)
        tracker = CompostTracker(
            now=reference_now,
            similarity_fn=_high_similarity,
            verifier=verifier,
        )
        frag = _fragment(
            frag_id="frag-false-positive",
            title="This passage borrows the language of release",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert [c for c in candidates if c.source_type == "fragment"] == []
        assert verifier.calls == [(frag.title, "")]

    def test_verifier_ambiguous_routes_to_review(
        self,
        reference_now: datetime,
    ) -> None:
        """An ``ambiguous`` verifier verdict sets ``for_review``."""
        ambiguous = CompostVerifierResult(
            verdict=CompostVerdict.AMBIGUOUS,
            reasoning="Reader could plausibly call this a pause, not abandonment.",
        )
        verifier = _StubVerifier(
            responses={"Pausing or composting?": ambiguous},
        )
        tracker = CompostTracker(
            now=reference_now,
            similarity_fn=_high_similarity,
            verifier=verifier,
        )
        frag = _fragment(
            frag_id="frag-pause",
            title="Pausing or composting?",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        fragment_hits = [c for c in candidates if c.source_type == "fragment"]
        assert len(fragment_hits) == 1
        assert fragment_hits[0].for_review is True
        assert fragment_hits[0].verifier_reasoning == ambiguous.reasoning

    def test_paradox_fragment_skipped(
        self,
        reference_now: datetime,
    ) -> None:
        """Paradox-tagged fragments never reach the embedding gate."""
        called: list[str] = []

        def _record(text: str) -> float:
            called.append(text)
            return 0.95

        tracker = CompostTracker(now=reference_now, similarity_fn=_record)
        frag = _fragment(
            frag_id="frag-paradox",
            title="The thread holds two truths",
            created=datetime(2025, 6, 1, 8, 0, 0),
            tags=["paradox"],
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert candidates == []
        assert called == []

    def test_intimate_fragment_skipped(
        self,
        reference_now: datetime,
    ) -> None:
        """Intimate fragments never reach the embedding gate (or the LLM)."""
        verifier = _StubVerifier()
        tracker = CompostTracker(
            now=reference_now,
            similarity_fn=_high_similarity,
            verifier=verifier,
        )
        frag = _fragment(
            frag_id="frag-intimate",
            title="A private letting-go",
            created=datetime(2025, 6, 1, 8, 0, 0),
            privacy_tier=PrivacyTier.INTIMATE,
        )
        candidates = tracker.detect_compost_candidates([], [frag])
        assert candidates == []
        assert verifier.calls == []

    def test_fragment_body_passed_to_similarity_and_verifier(
        self,
        reference_now: datetime,
    ) -> None:
        """When bodies are supplied they reach both stages of the pipeline."""
        seen: list[str] = []

        def _sim(text: str) -> float:
            seen.append(text)
            return 0.9

        verifier = _StubVerifier()
        tracker = CompostTracker(
            now=reference_now,
            similarity_fn=_sim,
            verifier=verifier,
        )
        frag = _fragment(
            frag_id="frag-with-body",
            title="The air went out of it",
            created=datetime(2025, 6, 1, 8, 0, 0),
        )
        body = "Three weeks of cold and the room feels empty."
        tracker.detect_compost_candidates(
            [],
            [frag],
            fragment_bodies={frag.id: body},
        )
        assert seen == [f"{frag.title}\n{body}"]
        assert verifier.calls == [(frag.title, body)]

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

    def test_project_silence_cutoff_uses_authored_at(
        self,
        reference_now: datetime,
    ) -> None:
        """FEAT-031: project silence is measured against ``authored_at``.

        All fragments share the same recent ``created`` (they were
        ingested today, in one batch), but they were *authored* years
        ago — well past the dormancy threshold. Without FEAT-031 the
        project would look active and silently escape compost; with the
        fix the authored history surfaces it as a silent project.
        """
        ingest_moment = reference_now - timedelta(hours=1)
        authored_dates = [
            datetime(2024, 1, 15, 12, 0, 0),
            datetime(2024, 2, 10, 12, 0, 0),
            datetime(2024, 3, 5, 12, 0, 0),
        ]
        fragments = [
            Fragment(
                id=f"frag-feat031-proj-{i}",
                title=f"Greenhouse notes {i}",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
                created=ingest_moment,
                ingested=ingest_moment,
                authored_at=authored,
                frequency=FrequencyClassification(primary=Frequency.F5),
                tags=["greenhouse"],
            )
            for i, authored in enumerate(authored_dates)
        ]
        tracker = CompostTracker(now=reference_now)
        candidates = tracker.detect_compost_candidates([], fragments)
        project_candidates = [c for c in candidates if c.source_type == "project"]
        assert any(c.source_id == "greenhouse" for c in project_candidates)


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
            title="I finally let go of the novel",
            reason="Embedding-similarity 0.91 + verifier yes",
            fragment_ids=["frag-abandon"],
            energy_fragment_ids=[],
            similarity=0.91,
            verifier_reasoning="Self-honest naming of a release.",
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert "original_thread" not in post.metadata
        assert post["original_fragment"] == "[[frag-abandon]]"
        assert post["embedding_similarity"] == pytest.approx(0.91)
        assert post["verifier_reasoning"] == "Self-honest naming of a release."


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
        )
        path = tracker.create_compost_note(candidate, vault_path)
        body = frontmatter.load(str(path)).content
        assert "_No related fragments were recorded._" in body

    def test_similarity_and_reasoning_persist_in_frontmatter(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """Embedding similarity + verifier reasoning round-trip via frontmatter."""
        candidate = CompostCandidate(
            source_type="fragment",
            source_id="frag-released",
            title="Releasing the project quietly",
            reason="Verifier: clear release statement (similarity 0.82)",
            fragment_ids=["frag-released"],
            energy_fragment_ids=[],
            similarity=0.82,
            verifier_reasoning="Clear release statement.",
        )
        path = tracker.create_compost_note(candidate, vault_path)
        post = frontmatter.load(str(path))
        assert post["embedding_similarity"] == pytest.approx(0.82)
        assert post["verifier_reasoning"] == "Clear release statement."

    def test_ambiguous_candidate_routes_to_review_queue(
        self,
        tracker: CompostTracker,
        vault_path: Path,
    ) -> None:
        """``for_review`` candidates land in the review subdirectory."""
        candidate = CompostCandidate(
            source_type="fragment",
            source_id="frag-maybe",
            title="Pausing or composting?",
            reason="Verifier ambiguous (similarity 0.71)",
            fragment_ids=["frag-maybe"],
            energy_fragment_ids=[],
            similarity=0.71,
            verifier_reasoning="Could be a pause, could be a release.",
            for_review=True,
        )
        path = tracker.create_compost_note(candidate, vault_path)
        assert path.parent == vault_path / "10-Liminal" / "Compost" / "Review"
        post = frontmatter.load(str(path))
        tags = post.get("tags") or []
        assert "compost-review" in tags

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


# ---- Issue #938: the clock must be tz-aware -------------------------------
#
# ``CompostTracker.__init__`` was the only place in ``creek/`` that stripped
# tzinfo off a datetime. Two consequences, both live crashes:
#
#   1. the naive clock cannot be compared against a tz-aware fragment
#      (``TypeError: can't compare offset-naive and offset-aware datetimes``),
#      and every fragment built from model defaults is tz-aware; and
#   2. ``self._now.date()`` reads the *UTC* calendar, so for the seven-odd
#      hours a day that UTC runs a day ahead of LA, compost notes and the
#      compost report are stamped with tomorrow's date in a vault whose
#      every other timestamp is LA-normalised.
#
# Normalising only the clock fixes neither the reverse pairing (aware clock,
# naive fragment) nor the ``max()`` over a tag whose fragments mix both, so
# the tests below pin all three call sites.

_LA_ZONE = ZoneInfo("America/Los_Angeles")
"""Explicit LA zone for these tests — never the host ``TZ``."""

_TZ_PROJECT_TAG = "greenhouse"
"""Tag the timezone tests group their fragments under."""


def _tz_fragment(
    *,
    frag_id: str,
    title: str,
    ingested: datetime,
    tags: list[str],
) -> Fragment:
    """Build a tagged fragment whose ``ingested`` value is stored verbatim.

    Deliberately separate from :func:`_fragment`, which mirrors ``created``
    into ``ingested`` precisely so the legacy tests stay on naive
    timestamps. These tests need to choose naive *or* tz-aware per
    fragment — including mixing both inside one tag group — so they need a
    builder that takes ``ingested`` directly and leaves ``authored_at``
    unset, letting ``effective_authored_at`` fall through to it.

    Args:
        frag_id: Fragment id.
        title: Fragment title.
        ingested: Exact value stored on ``ingested`` (and ``created``);
            may be naive or tz-aware.
        tags: Tags the fragment carries — project grouping keys off these.

    Returns:
        A minimal :class:`Fragment` suitable for project-silence detection.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=ingested,
        ingested=ingested,
        frequency=FrequencyClassification(primary=Frequency.F5),
        tags=tags,
    )


class TestTimezoneAwareClock:
    """The compost clock must compare against any fragment (issue #938)."""

    def _frags(self, moments: list[datetime]) -> list[Fragment]:
        """Build one fragment per supplied ``ingested`` moment, all one tag.

        Args:
            moments: The ``ingested`` timestamps, in order.

        Returns:
            Fragments ``frag-tz-0`` … ``frag-tz-N``, all tagged
            ``greenhouse`` so project grouping fires.
        """
        return [
            _tz_fragment(
                frag_id=f"frag-tz-{index}",
                title=f"Greenhouse notes {index}",
                ingested=moment,
                tags=[_TZ_PROJECT_TAG],
            )
            for index, moment in enumerate(moments)
        ]

    def _assert_single_project_candidate(
        self,
        candidates: list[CompostCandidate],
    ) -> None:
        """Assert the run produced exactly the expected project candidate.

        Asserting the whole candidate — not merely "no exception" — is what
        makes these tests survive a fix that swallows the ``TypeError``
        instead of resolving it.

        Args:
            candidates: The full result of ``detect_compost_candidates``.
        """
        assert [c.source_id for c in candidates] == [_TZ_PROJECT_TAG]
        candidate = candidates[0]
        assert candidate.source_type == "project"
        assert candidate.title == _TZ_PROJECT_TAG
        assert candidate.fragment_ids == ["frag-tz-0", "frag-tz-1", "frag-tz-2"]

    def test_default_clock_handles_tz_aware_fragments(self) -> None:
        """A default-clock tracker survives tz-aware fragments.

        This is the crash as reported: no ``now=`` argument, fragments
        whose ``ingested`` is tz-aware (the shape every default-constructed
        Fragment has), and ``TypeError`` out of the ``last_seen >= cutoff``
        comparison. The fragments sit in 2020 so the expected verdict holds
        no matter when the suite runs.
        """
        aware = [
            datetime(2020, 1, 1, 12, 0, tzinfo=_LA_ZONE) + timedelta(days=offset)
            for offset in range(3)
        ]
        tracker = CompostTracker(project_gap_days=30, project_min_fragments=3)
        candidates = tracker.detect_compost_candidates([], self._frags(aware))
        self._assert_single_project_candidate(candidates)

    def test_aware_clock_handles_naive_fragments(self) -> None:
        """An explicitly tz-aware clock survives naive fragments.

        The mirror image of the reported crash, and the reason normalising
        the *default clock* alone is not a fix: naive ``ingested`` values
        are a real production input (any fragment round-tripped through
        YAML without an offset), so the comparison has to be safe from
        whichever side the naive value arrives on.
        """
        naive = [
            datetime(2020, 1, 1, 12, 0) + timedelta(days=offset) for offset in range(3)
        ]
        tracker = CompostTracker(
            now=datetime(2026, 4, 1, 12, 0, tzinfo=_LA_ZONE),
            project_gap_days=30,
            project_min_fragments=3,
        )
        candidates = tracker.detect_compost_candidates([], self._frags(naive))
        self._assert_single_project_candidate(candidates)

    def test_naive_clock_handles_tz_aware_fragments(self) -> None:
        """An explicitly *naive* ``now=`` keeps working against aware fragments.

        Callers that already pass a naive reference — every legacy test in
        this module, and any downstream script doing the same — must not be
        broken by the fix. Their clock is interpreted as LA wall time.
        """
        aware = [
            datetime(2020, 1, 1, 12, 0, tzinfo=_LA_ZONE) + timedelta(days=offset)
            for offset in range(3)
        ]
        tracker = CompostTracker(
            now=datetime(2026, 4, 1, 12, 0),
            project_gap_days=30,
            project_min_fragments=3,
        )
        candidates = tracker.detect_compost_candidates([], self._frags(aware))
        self._assert_single_project_candidate(candidates)

    def test_mixed_naive_and_aware_fragments_under_one_tag(self) -> None:
        """One tag may carry both naive and tz-aware fragments.

        This is the third crash site and the one a clock-only fix cannot
        reach: ``max(effective_authored_at(frag) for frag in frags)`` raises
        while ranking the group, before ``self._now`` is consulted at all.
        The third fragment is Sydney-anchored so the group also spans two
        real zones, not just "aware vs naive".
        """
        mixed = [
            datetime(2020, 1, 1, 12, 0),
            datetime(2020, 1, 2, 12, 0, tzinfo=_LA_ZONE),
            datetime(2020, 1, 3, 12, 0, tzinfo=ZoneInfo("Australia/Sydney")),
        ]
        tracker = CompostTracker(
            now=datetime(2026, 4, 1, 12, 0, tzinfo=_LA_ZONE),
            project_gap_days=30,
            project_min_fragments=3,
        )
        candidates = tracker.detect_compost_candidates([], self._frags(mixed))
        self._assert_single_project_candidate(candidates)

    def test_default_clock_is_tz_aware_and_la(self, vault_path: Path) -> None:
        """The default clock stamps the LA date on a note, not the UTC one.

        Asserted through the public filename rather than ``_now`` so the
        test pins behaviour rather than internals. A UTC wall clock files
        the note under tomorrow's date for the roughly seven hours a day
        that UTC leads LA; ``tests/test_cli_fill.py`` carries the frozen,
        always-deterministic version of this assertion.

        The LA calendar is sampled either side of the call and the result
        accepted against both readings, so a run that straddles LA
        midnight cannot produce a false failure. Bracketing rather than
        freezing keeps this test honest about the *real* default clock:
        it would still fail against a UTC-derived one, which is off by a
        whole day for hours at a time and so can never fall inside a
        bracket that is at most one day wide.

        Args:
            vault_path: Vault fixture with the Compost subtree present.
        """
        # ``before`` is sampled ahead of construction because that is when
        # the tracker latches its clock; ``after`` trails the write. The
        # bracket therefore provably contains the instant the filename was
        # derived from.
        before = today_la()
        tracker = CompostTracker()
        candidate = CompostCandidate(
            source_type="project",
            source_id=_TZ_PROJECT_TAG,
            title=_TZ_PROJECT_TAG,
            reason="Project silent for 400 days",
            fragment_ids=["frag-tz-0"],
        )
        path = tracker.create_compost_note(candidate, vault_path)
        after = today_la()
        assert path.name in {
            f"{before.isoformat()}-{_TZ_PROJECT_TAG}.md",
            f"{after.isoformat()}-{_TZ_PROJECT_TAG}.md",
        }


# ---- Issue #1311: the screened-fragment boundary ---------------------------
#
# ``skip_intimate`` / ``skip_paradox`` used to be enforced inside a single
# detector, so the thread and project detectors copied withheld fragments'
# titles and IDs into vault notes — and, on the project path, the withheld
# fragment's *tag* became the note's identity: its title, its
# ``original_project``, its filename, and a line in ``_Compost-Report.md``.
# Filtering the emitted lists cannot reach that, because the candidate *is*
# the tag. The screen therefore runs once, on the fragment sequence, before
# any detector sees it.


_GRIEF_THREAD_TITLE = "Grief Work"
"""Thread title shared by the residual-channel test's fixtures."""


class _RecordingTracker(CompostTracker):
    """Tracker that records the fragment IDs each private detector was handed.

    The inheritance property under test is structural: a future fourth
    detector added to ``detect_compost_candidates`` is guarded by
    construction only if the screen happens at the boundary rather than
    inside each detector.
    """

    def __init__(self, *, now: datetime) -> None:
        """Initialise the tracker and the per-detector recording map."""
        super().__init__(now=now, similarity_fn=_high_similarity)
        self.seen: dict[str, tuple[str, ...]] = {}

    def _detect_threads(
        self,
        threads: list[Thread],
        admitted: _AdmittedFragments,
    ) -> list[CompostCandidate]:
        """Record the admitted IDs, then delegate."""
        self.seen["threads"] = tuple(frag.id for frag in admitted.fragments)
        return super()._detect_threads(threads, admitted)

    def _detect_abandonment_fragments(
        self,
        admitted: _AdmittedFragments,
        bodies: Mapping[str, str],
    ) -> list[CompostCandidate]:
        """Record the admitted IDs, then delegate."""
        self.seen["fragments"] = tuple(frag.id for frag in admitted.fragments)
        return super()._detect_abandonment_fragments(admitted, bodies)

    def _detect_disappeared_projects(
        self,
        admitted: _AdmittedFragments,
    ) -> list[CompostCandidate]:
        """Record the admitted IDs, then delegate."""
        self.seen["projects"] = tuple(frag.id for frag in admitted.fragments)
        return super()._detect_disappeared_projects(admitted)


def _fingerprint(candidate: CompostCandidate) -> tuple[object, ...]:
    """Return every field of *candidate* that a compost note renders."""
    return (
        candidate.source_type,
        candidate.source_id,
        candidate.title,
        candidate.reason,
        tuple(candidate.fragment_ids),
        tuple(candidate.energy_fragment_ids),
        tuple(candidate.energy_excerpts),
    )


def _dormant(thread_id: str, title: str, last_seen: date) -> Thread:
    """Build a dormant thread with a caller-chosen ``last_seen``."""
    return Thread(
        id=thread_id,
        title=title,
        status=ThreadStatus.DORMANT,
        first_seen=date(2023, 1, 1),
        last_seen=last_seen,
        fragment_count=3,
        frequency_affinity=[Frequency.F5],
        description="Ran hot for a season and then went quiet.",
    )


class TestScreenedFragmentBoundary:
    """The single screen at the top of ``detect_compost_candidates``."""

    def test_every_detector_receives_only_admitted_fragments(
        self,
        reference_now: datetime,
    ) -> None:
        """All three detectors are handed the same screened tuple."""
        corpus = [
            _fragment(
                frag_id="f-open",
                title="Kept going",
                created=datetime(2024, 5, 1, 9, 0, 0),
            ),
            _fragment(
                frag_id="f-intimate",
                title="Therapy: the affair and the shame",
                created=datetime(2024, 5, 2, 9, 0, 0),
                privacy_tier=PrivacyTier.INTIMATE,
            ),
            _fragment(
                frag_id="f-paradox",
                title="Both true at once",
                created=datetime(2024, 5, 3, 9, 0, 0),
                tags=["paradox"],
            ),
        ]
        tracker = _RecordingTracker(now=reference_now)

        tracker.detect_compost_candidates([], corpus)

        assert tracker.seen == {
            "threads": ("f-open",),
            "fragments": ("f-open",),
            "projects": ("f-open",),
        }

    def test_every_detector_declares_the_screened_fragment_type(self) -> None:
        """A fourth detector taking a raw ``list[Fragment]`` must not compile.

        MyPy already rejects *passing* an unscreened list to a screened
        detector, but it cannot object to a new detector that declares
        ``list[Fragment]`` and is called with the raw sequence — which is
        exactly how this defect was introduced. Annotations are compared as
        strings: ``compost.py`` uses ``from __future__ import annotations``
        and imports ``Mapping`` only under ``TYPE_CHECKING``, so
        ``typing.get_type_hints`` raises ``NameError`` on
        ``_detect_abandonment_fragments`` — the one method this most needs
        to police.
        """
        detectors = {
            name: fn
            for name, fn in inspect.getmembers(CompostTracker, inspect.isfunction)
            if name.startswith("_detect_")
        }

        assert set(detectors) == {
            "_detect_threads",
            "_detect_abandonment_fragments",
            "_detect_disappeared_projects",
        }
        for name, fn in detectors.items():
            annotations = [
                param.annotation for param in inspect.signature(fn).parameters.values()
            ]
            assert "_AdmittedFragments" in annotations, name

    def test_screening_equals_removing_the_withheld_fragments_up_front(
        self,
        reference_now: datetime,
    ) -> None:
        """FORBIDDEN DIRECTION: nothing admitted at HEAD stops being emitted.

        Screening is proved to be exactly subtraction — every candidate the
        unguarded tracker derives from the admitted fragments alone is still
        produced, field for field. Because the withheld fragment is the
        *most recent* member of the ``zine`` group, equality of ``reason``
        also pins that counts and dates come from the admitted set only.
        """
        thread = _dormant("thr-deep", "Deep Work", date(2025, 1, 1))
        admitted = [
            _fragment(
                frag_id="f-open-a",
                title="Kept going",
                created=datetime(2024, 5, 1, 9, 0, 0),
                tags=["zine"],
                threads=["[[Deep Work]]"],
            ),
            _fragment(
                frag_id="f-open-b",
                title="Also kept",
                created=datetime(2024, 5, 2, 9, 0, 0),
                tags=["zine"],
            ),
        ]
        withheld = _fragment(
            frag_id="f-hidden",
            title="Therapy: the affair and the shame",
            created=datetime(2024, 5, 3, 9, 0, 0),
            tags=["zine"],
            threads=["[[Deep Work]]"],
            privacy_tier=PrivacyTier.INTIMATE,
        )

        screened = CompostTracker(now=reference_now).detect_compost_candidates(
            [thread],
            [*admitted, withheld],
        )
        baseline = CompostTracker(
            now=reference_now,
            skip_intimate=False,
            skip_paradox=False,
        ).detect_compost_candidates([thread], admitted)

        assert [_fingerprint(c) for c in screened] == [
            _fingerprint(c) for c in baseline
        ]
        assert screened
        assert all("f-hidden" not in c.fragment_ids for c in screened)

    def test_a_project_alive_only_in_withheld_fragments_becomes_silent(
        self,
        reference_now: datetime,
    ) -> None:
        """ACCEPTED BEHAVIOUR CHANGE — decided here, not discovered in review.

        Screening the fragment sequence changes the *silence* arithmetic: a
        project whose only recent activity is withheld now looks dormant and
        gets a compost note it would not have got before. The semantics are
        deliberate — "a project alive only in intimate content is silent in
        the non-intimate view" — and the direction is the safe one: no
        withheld fragment becomes admitted, and the new note is derived
        entirely from admitted fragments, disclosing nothing about the
        protected content. Operators will see new files appear.
        """
        corpus = [
            _fragment(
                frag_id="f-zine-1",
                title="Laying out the zine",
                created=datetime(2024, 5, 1, 9, 0, 0),
                tags=["zine"],
            ),
            _fragment(
                frag_id="f-zine-2",
                title="Zine paper stock",
                created=datetime(2024, 5, 1, 9, 0, 0),
                tags=["zine"],
            ),
            _fragment(
                frag_id="f-zine-private",
                title="Therapy: the affair and the shame",
                created=datetime(2026, 3, 25, 9, 0, 0),
                tags=["zine"],
                privacy_tier=PrivacyTier.INTIMATE,
            ),
        ]

        unguarded = CompostTracker(
            now=reference_now,
            skip_intimate=False,
        ).detect_compost_candidates([], corpus)
        guarded = CompostTracker(now=reference_now).detect_compost_candidates(
            [],
            corpus,
        )

        assert [c for c in unguarded if c.source_type == "project"] == []
        projects = [c for c in guarded if c.source_type == "project"]
        assert len(projects) == 1
        assert projects[0].source_id == "zine"
        assert projects[0].fragment_ids == ["f-zine-1", "f-zine-2"]
        assert projects[0].reason == "Project silent for 700 days"

    def test_a_thread_reason_is_thread_derived_not_fragment_derived(
        self,
        reference_now: datetime,
    ) -> None:
        """ACCEPTED RESIDUAL CHANNEL — pinned rather than recomputed.

        ``_thread_reason`` reads ``Thread.last_seen`` off the thread's own
        frontmatter, so screening the fragment sequence cannot change it: a
        thread whose ``last_seen`` was stamped by a withheld fragment still
        reports the dormancy measured from that date. This is accepted, not
        overlooked. ``02-Threads/<id>.md`` is an ordinary vault note any
        reader already sees, so the compost note discloses nothing new — and
        recomputing the reason from fragments would break the guarantee that
        a thread with no ingested fragments still composts at all.
        """
        thread = _dormant("thr-grief", _GRIEF_THREAD_TITLE, date(2025, 1, 1))
        corpus = [
            _fragment(
                frag_id="f-open",
                title="A note anyone may read",
                created=datetime(2023, 6, 1, 9, 0, 0),
                threads=[f"[[{_GRIEF_THREAD_TITLE}]]"],
            ),
            _fragment(
                frag_id="f-hidden",
                title="Therapy: the affair and the shame",
                created=datetime(2025, 1, 1, 9, 0, 0),
                threads=[f"[[{_GRIEF_THREAD_TITLE}]]"],
                privacy_tier=PrivacyTier.INTIMATE,
            ),
        ]

        candidates = CompostTracker(now=reference_now).detect_compost_candidates(
            [thread],
            corpus,
        )

        threads = [c for c in candidates if c.source_type == "thread"]
        assert len(threads) == 1
        assert threads[0].fragment_ids == ["f-open"]
        assert threads[0].reason == "Dormant for 455 days"
