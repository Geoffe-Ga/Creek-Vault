"""Tests for creek.generate.mining — blog idea mining pipeline.

Covers :class:`IdeaMiner` (Section 11.5 of the Creek Ontology): four
mining strategies plus combined ``mine_all`` for surfacing essay-worthy
seeds from liminal fragments, thread termini, resonance chains, and
wavelength-phase windows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.mining import (
    DEFAULT_MIN_CHAIN_LENGTH,
    DEFAULT_MIN_THREAD_FRAGMENTS,
    DEFAULT_SIMILARITY_LIMINAL,
    DEFAULT_SIMILARITY_RESONANCE,
    IdeaMiner,
    IdeaSeed,
    MiningStrategy,
    _jaccard_similarity,
)
from creek.models import (
    Eddy,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    PraxisPotential,
    SourcePlatform,
    Synchronicity,
    Thread,
    ThreadStatus,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a vault with the folders the miner touches."""
    for folder in (
        "01-Fragments/Journal",
        "02-Threads/Active",
        "03-Eddies",
        "09-Reference/Published-Essays",
        "10-Liminal/Unnamed",
        "10-Liminal/Compost",
        "10-Liminal/Synchronicities",
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def miner() -> IdeaMiner:
    """Return an :class:`IdeaMiner` with default configuration."""
    return IdeaMiner()


def _build_fragment(
    *,
    frag_id: str,
    title: str,
    platform: SourcePlatform = SourcePlatform.JOURNAL,
    frequency: Frequency = Frequency.F5,
    phase: Phase = Phase.RISING,
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
    praxis: PraxisPotential = PraxisPotential.LATENT,
    created_day: int = 15,
) -> Fragment:
    """Build a minimal :class:`Fragment` with optional links."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=datetime(2026, 1, created_day, 12, 0, 0, tzinfo=UTC),
        ingested=datetime(2026, 1, created_day, 12, 0, 0, tzinfo=UTC),
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(
            phase=phase,
            mode=Mode.EXPRESS,
            orientation=Orientation.DO,
        ),
        threads=list(threads),
        eddies=list(eddies),
        praxis_potential=praxis,
    )


def _write_fragment(vault_path: Path, fragment: Fragment, body: str) -> Path:
    """Persist *fragment* under ``01-Fragments/Journal``."""
    return _write_fragment_at(
        vault_path / "01-Fragments" / "Journal" / f"{fragment.id}.md",
        fragment,
        body,
    )


def _write_liminal_fragment(
    vault_path: Path,
    fragment: Fragment,
    body: str,
    *,
    kind: str,
) -> Path:
    """Persist *fragment* under ``10-Liminal/{Unnamed|Compost}``."""
    return _write_fragment_at(
        vault_path / "10-Liminal" / kind / f"{fragment.id}.md",
        fragment,
        body,
    )


def _write_fragment_at(target: Path, fragment: Fragment, body: str) -> Path:
    """Write *fragment* as markdown-with-frontmatter at *target*."""
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=body, **data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_thread(vault_path: Path, thread: Thread) -> Path:
    """Persist *thread* under ``02-Threads/Active``."""
    data = thread.model_dump(mode="json")
    post = frontmatter.Post(content=thread.description, **data)
    target = vault_path / "02-Threads" / "Active" / f"{thread.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_eddy(vault_path: Path, eddy: Eddy) -> Path:
    """Persist *eddy* under ``03-Eddies``."""
    data = eddy.model_dump(mode="json")
    post = frontmatter.Post(content=eddy.description, **data)
    target = vault_path / "03-Eddies" / f"{eddy.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_essay(vault_path: Path, *, title: str) -> Path:
    """Persist a published essay stub with the given *title*."""
    target = vault_path / "09-Reference" / "Published-Essays" / f"{title}.md"
    post = frontmatter.Post(content="Essay body.", title=title)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_synchronicity(
    vault_path: Path,
    sync: Synchronicity,
) -> Path:
    """Persist *sync* under ``10-Liminal/Synchronicities``."""
    data = sync.model_dump(mode="json")
    post = frontmatter.Post(content="Synchronicity note.", **data)
    target = vault_path / "10-Liminal" / "Synchronicities" / f"{sync.id}.md"
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---- IdeaSeed ----


class TestIdeaSeed:
    """``IdeaSeed`` is a frozen, hashable dataclass."""

    def test_idea_seed_is_hashable(self) -> None:
        """Seeds must be hashable for set deduplication."""
        seed = IdeaSeed(
            strategy=MiningStrategy.THREAD_TERMINUS,
            title="Essay on grief",
            source_fragments=("frag-1",),
            threads=("thread-1",),
            eddies=(),
            frequency_affinity=(Frequency.F3,),
            brief_description="A pitch.",
            score=0.75,
        )
        assert isinstance(hash(seed), int)

    def test_idea_seed_rejects_negative_score(self) -> None:
        """Scores must be non-negative to rank cleanly."""
        with pytest.raises(ValueError, match="score"):
            IdeaSeed(
                strategy=MiningStrategy.THREAD_TERMINUS,
                title="t",
                source_fragments=(),
                threads=(),
                eddies=(),
                frequency_affinity=(),
                brief_description="d",
                score=-0.1,
            )


# ---- Module surface ----


class TestModuleSurface:
    """Public constants are non-zero and match the specification."""

    def test_default_thresholds_are_positive(self) -> None:
        """Public defaults must be within ontological bounds."""
        assert 0 < DEFAULT_SIMILARITY_LIMINAL < 1
        assert 0 < DEFAULT_SIMILARITY_RESONANCE < 1
        assert DEFAULT_SIMILARITY_RESONANCE > DEFAULT_SIMILARITY_LIMINAL
        assert DEFAULT_MIN_THREAD_FRAGMENTS > 0
        assert DEFAULT_MIN_CHAIN_LENGTH >= 3


# ---- Similarity helper ----


class TestJaccardSimilarity:
    """The default similarity function uses Jaccard over normalised tokens."""

    def test_identical_texts_score_one(self) -> None:
        """Two identical strings return 1.0."""
        text = "grief keeps arriving"
        assert _jaccard_similarity(text, text) == 1.0

    def test_disjoint_texts_score_zero(self) -> None:
        """Two texts with no shared tokens return 0.0."""
        assert _jaccard_similarity("apples oranges", "clouds mountains") == 0.0

    def test_empty_inputs_score_zero(self) -> None:
        """Empty strings cannot be similar."""
        assert _jaccard_similarity("", "anything") == 0.0
        assert _jaccard_similarity("", "") == 0.0

    def test_stopwords_do_not_dominate(self) -> None:
        """Common stopwords are stripped before scoring."""
        score = _jaccard_similarity("the and of", "the and of grief")
        assert score < 1.0


# ---- Thread terminus ----


def _build_thread(
    *,
    thread_id: str,
    title: str,
    fragment_count: int,
    status: ThreadStatus = ThreadStatus.ACTIVE,
    frequency_affinity: tuple[Frequency, ...] = (),
    description: str = "",
) -> Thread:
    """Return a minimal :class:`Thread` for terminus tests."""
    return Thread(
        id=thread_id,
        title=title,
        status=status,
        fragment_count=fragment_count,
        frequency_affinity=list(frequency_affinity),
        description=description,
    )


class TestMineThreadTerminus:
    """``mine_thread_terminus`` flags unpublished high-activity threads."""

    def test_active_thread_over_threshold_with_no_essay_surfaces(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A large active thread without a matching essay becomes a seed."""
        thread = _build_thread(
            thread_id="thread-grief",
            title="Grief as a long rhythm",
            fragment_count=12,
            frequency_affinity=(Frequency.F3,),
            description="How grief keeps arriving across months.",
        )
        _write_thread(vault, thread)
        for day in range(1, 13):
            frag = _build_fragment(
                frag_id=f"frag-{day:02d}",
                title=f"Entry {day}",
                threads=("thread-grief",),
                frequency=Frequency.F3,
                created_day=day,
            )
            _write_fragment(vault, frag, "Body.")

        seeds = miner.mine_thread_terminus(vault)

        assert len(seeds) == 1
        seed = seeds[0]
        assert seed.strategy is MiningStrategy.THREAD_TERMINUS
        assert seed.threads == ("thread-grief",)
        assert seed.frequency_affinity == (Frequency.F3,)
        assert seed.score > 0
        assert "thread-grief" in seed.brief_description or "Grief" in seed.title

    def test_thread_at_or_below_threshold_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A thread with fragment_count <= threshold is not a terminus."""
        thread = _build_thread(
            thread_id="thread-small",
            title="Small thread",
            fragment_count=DEFAULT_MIN_THREAD_FRAGMENTS,
        )
        _write_thread(vault, thread)

        seeds = miner.mine_thread_terminus(vault)

        assert not seeds

    def test_thread_matching_published_essay_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A thread that echoes a published essay title is already written."""
        thread = _build_thread(
            thread_id="thread-forgiveness",
            title="Forgiveness keeps arriving",
            fragment_count=15,
        )
        _write_thread(vault, thread)
        _write_essay(vault, title="Forgiveness keeps arriving")

        seeds = miner.mine_thread_terminus(vault)

        assert not seeds

    def test_non_active_threads_are_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Only active threads qualify — resolved/archived ones do not."""
        thread = _build_thread(
            thread_id="thread-resolved",
            title="Resolved thread",
            fragment_count=25,
            status=ThreadStatus.RESOLVED,
        )
        _write_thread(vault, thread)

        seeds = miner.mine_thread_terminus(vault)

        assert not seeds


# ---- Liminal cross-eddy ----


def _build_eddy(
    *,
    eddy_id: str,
    title: str,
    description: str = "",
) -> Eddy:
    """Return a minimal :class:`Eddy` for liminal tests."""
    return Eddy(id=eddy_id, title=title, description=description)


class TestMineLiminalCrossEddy:
    """``mine_liminal_cross_eddy`` pairs liminal fragments with eddies."""

    def test_liminal_fragment_echoing_eddy_produces_seed(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Shared content words between liminal body and eddy text seed."""
        eddy = _build_eddy(
            eddy_id="eddy-grief",
            title="Grief practices",
            description="Rituals for metabolising grief.",
        )
        _write_eddy(vault, eddy)

        frag = _build_fragment(
            frag_id="frag-wandering",
            title="Uncategorised wandering",
            praxis=PraxisPotential.LATENT,
        )
        _write_liminal_fragment(
            vault,
            frag,
            "Notes on grief rituals and metabolising grief slowly.",
            kind="Unnamed",
        )

        seeds = miner.mine_liminal_cross_eddy(vault)

        assert len(seeds) == 1
        seed = seeds[0]
        assert seed.strategy is MiningStrategy.LIMINAL_CROSS_EDDY
        assert seed.source_fragments == ("frag-wandering",)
        assert seed.eddies == ("eddy-grief",)

    def test_disjoint_liminal_fragment_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A fragment with no lexical overlap to any eddy is skipped."""
        eddy = _build_eddy(
            eddy_id="eddy-grief",
            title="Grief practices",
            description="Rituals for metabolising grief.",
        )
        _write_eddy(vault, eddy)

        frag = _build_fragment(frag_id="frag-trains", title="Train schedules")
        _write_liminal_fragment(
            vault,
            frag,
            "Monday at 7:02 there is a local to Poughkeepsie.",
            kind="Compost",
        )

        seeds = miner.mine_liminal_cross_eddy(vault)

        assert not seeds

    def test_non_liminal_fragments_are_ignored(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Fragments under 01-Fragments never count as liminal."""
        eddy = _build_eddy(
            eddy_id="eddy-grief",
            title="Grief practices",
            description="Rituals for metabolising grief.",
        )
        _write_eddy(vault, eddy)

        frag = _build_fragment(frag_id="frag-active", title="Grief and rituals")
        _write_fragment(vault, frag, "Notes on grief rituals and metabolising.")

        seeds = miner.mine_liminal_cross_eddy(vault)

        assert not seeds

    def test_only_highest_scoring_eddy_is_attached(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """When a fragment echoes two eddies, only the stronger match seeds."""
        _write_eddy(
            vault,
            _build_eddy(
                eddy_id="eddy-strong",
                title="Grief rituals metabolise",
                description="Grief rituals metabolise slowly.",
            ),
        )
        _write_eddy(
            vault,
            _build_eddy(
                eddy_id="eddy-weak",
                title="Grief alone",
                description="Grief alone.",
            ),
        )

        frag = _build_fragment(frag_id="frag-w", title="Wandering")
        _write_liminal_fragment(
            vault,
            frag,
            "Grief rituals metabolise the body.",
            kind="Unnamed",
        )

        seeds = miner.mine_liminal_cross_eddy(vault)

        assert len(seeds) == 1
        assert seeds[0].eddies == ("eddy-strong",)


# ---- Wavelength windows ----


class TestMineWavelengthWindows:
    """``mine_wavelength_windows`` surfaces phase-aligned fragments."""

    def test_explicit_fragments_in_current_phase_surface(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Phase-matching fragments with explicit praxis become seeds."""
        frag = _build_fragment(
            frag_id="frag-rising",
            title="Starting the new practice",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
            frequency=Frequency.F5,
        )
        _write_fragment(vault, frag, "I began the morning sit today.")

        seeds = miner.mine_wavelength_windows(vault, current_phase=Phase.RISING)

        assert len(seeds) == 1
        assert seeds[0].strategy is MiningStrategy.WAVELENGTH_WINDOW
        assert seeds[0].source_fragments == ("frag-rising",)
        assert seeds[0].frequency_affinity == (Frequency.F5,)

    def test_phase_mismatch_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Fragments in other phases are not surfaced."""
        frag = _build_fragment(
            frag_id="frag-diminishing",
            title="Winding down",
            phase=Phase.DIMINISHING,
            praxis=PraxisPotential.EXPLICIT,
        )
        _write_fragment(vault, frag, "Letting the project rest.")

        seeds = miner.mine_wavelength_windows(vault, current_phase=Phase.RISING)

        assert not seeds

    def test_none_praxis_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Phase-matching but non-practice fragments are not seeds."""
        frag = _build_fragment(
            frag_id="frag-casual",
            title="Just a note",
            phase=Phase.RISING,
            praxis=PraxisPotential.NONE,
        )
        _write_fragment(vault, frag, "Nothing actionable.")

        seeds = miner.mine_wavelength_windows(vault, current_phase=Phase.RISING)

        assert not seeds

    def test_unclassified_phase_returns_empty(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Without a known current phase, no seeds are returned."""
        frag = _build_fragment(
            frag_id="frag-r",
            title="Morning practice",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
        )
        _write_fragment(vault, frag, "Body.")

        seeds = miner.mine_wavelength_windows(
            vault,
            current_phase=Phase.UNCLASSIFIED,
        )

        assert not seeds


# ---- Resonance chains ----


def _build_sync(
    *,
    sync_id: str,
    frag_a: str,
    frag_b: str,
    similarity: float,
    source_a: SourcePlatform,
    source_b: SourcePlatform,
    time_gap_days: int = 45,
) -> Synchronicity:
    """Return a minimal :class:`Synchronicity` for chain tests."""
    return Synchronicity(
        id=sync_id,
        fragment_a_id=frag_a,
        fragment_b_id=frag_b,
        similarity=similarity,
        time_gap_days=time_gap_days,
        source_a=source_a,
        source_b=source_b,
    )


class TestMineResonanceChains:
    """``mine_resonance_chains`` walks synchronicity edges into chains."""

    def test_three_fragment_chain_surfaces(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A → B → C across distinct sources becomes a seed."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="a",
                title="A grief",
                platform=SourcePlatform.JOURNAL,
                frequency=Frequency.F3,
            ),
            "Body A.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="b",
                title="B grief",
                platform=SourcePlatform.CLAUDE,
                frequency=Frequency.F3,
            ),
            "Body B.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="c",
                title="C grief",
                platform=SourcePlatform.DISCORD,
                frequency=Frequency.F3,
            ),
            "Body C.",
        )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s1",
                frag_a="a",
                frag_b="b",
                similarity=0.95,
                source_a=SourcePlatform.JOURNAL,
                source_b=SourcePlatform.CLAUDE,
            ),
        )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s2",
                frag_a="b",
                frag_b="c",
                similarity=0.92,
                source_a=SourcePlatform.CLAUDE,
                source_b=SourcePlatform.DISCORD,
            ),
        )

        seeds = miner.mine_resonance_chains(vault)

        assert len(seeds) == 1
        seed = seeds[0]
        assert seed.strategy is MiningStrategy.RESONANCE_CHAIN
        assert set(seed.source_fragments) == {"a", "b", "c"}
        assert seed.score > 0

    def test_short_chain_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A chain shorter than min_chain_length is skipped."""
        _write_fragment(
            vault,
            _build_fragment(frag_id="a", title="A"),
            "Body.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="b",
                title="B",
                platform=SourcePlatform.CLAUDE,
            ),
            "Body.",
        )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s1",
                frag_a="a",
                frag_b="b",
                similarity=0.95,
                source_a=SourcePlatform.JOURNAL,
                source_b=SourcePlatform.CLAUDE,
            ),
        )

        seeds = miner.mine_resonance_chains(vault)

        assert not seeds

    def test_single_source_chain_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Chains whose fragments share a single source are skipped."""
        for fid in ("a", "b", "c"):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=fid,
                    title=f"frag {fid}",
                    platform=SourcePlatform.JOURNAL,
                ),
                "Body.",
            )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s1",
                frag_a="a",
                frag_b="b",
                similarity=0.95,
                source_a=SourcePlatform.JOURNAL,
                source_b=SourcePlatform.JOURNAL,
            ),
        )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s2",
                frag_a="b",
                frag_b="c",
                similarity=0.95,
                source_a=SourcePlatform.JOURNAL,
                source_b=SourcePlatform.JOURNAL,
            ),
        )

        seeds = miner.mine_resonance_chains(vault)

        assert not seeds

    def test_low_similarity_edge_is_skipped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Edges below similarity_resonance do not count."""
        for fid, platform in (
            ("a", SourcePlatform.JOURNAL),
            ("b", SourcePlatform.CLAUDE),
            ("c", SourcePlatform.DISCORD),
        ):
            _write_fragment(
                vault,
                _build_fragment(frag_id=fid, title=fid, platform=platform),
                "Body.",
            )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s1",
                frag_a="a",
                frag_b="b",
                similarity=0.3,
                source_a=SourcePlatform.JOURNAL,
                source_b=SourcePlatform.CLAUDE,
            ),
        )
        _write_synchronicity(
            vault,
            _build_sync(
                sync_id="s2",
                frag_a="b",
                frag_b="c",
                similarity=0.3,
                source_a=SourcePlatform.CLAUDE,
                source_b=SourcePlatform.DISCORD,
            ),
        )

        seeds = miner.mine_resonance_chains(vault)

        assert not seeds


# ---- mine_all ----


class TestMineAll:
    """``mine_all`` unions strategy outputs, dedupes, and ranks."""

    def test_mine_all_unions_strategies(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Seeds from multiple strategies appear in a single list."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Unique thread title",
                fragment_count=15,
            ),
        )
        frag = _build_fragment(
            frag_id="frag-explicit",
            title="Morning practice",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
        )
        _write_fragment(vault, frag, "Body.")

        seeds = miner.mine_all(vault, current_phase=Phase.RISING)

        strategies = {seed.strategy for seed in seeds}
        assert MiningStrategy.THREAD_TERMINUS in strategies
        assert MiningStrategy.WAVELENGTH_WINDOW in strategies

    def test_seeds_are_ranked_by_score_desc(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Higher-score seeds come first."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Unique thread title",
                fragment_count=30,
            ),
        )
        frag = _build_fragment(
            frag_id="frag-explicit",
            title="Morning practice",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
        )
        _write_fragment(vault, frag, "Body.")

        seeds = miner.mine_all(vault, current_phase=Phase.RISING)

        scores = [seed.score for seed in seeds]
        assert scores == sorted(scores, reverse=True)

    def test_duplicate_seeds_are_deduped(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Identical seeds across strategies collapse to one entry."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Unique thread title",
                fragment_count=15,
            ),
        )

        seeds = miner.mine_all(vault, current_phase=Phase.UNCLASSIFIED)
        seeds_again = miner.mine_all(vault, current_phase=Phase.UNCLASSIFIED)

        assert len(seeds) == len(set(seeds))
        assert seeds == seeds_again

    def test_snapshot_reused_across_strategies(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Passing an explicit snapshot avoids re-scanning the vault."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Unique thread title",
                fragment_count=15,
            ),
        )

        from creek.generate.mining import _load_mining_snapshot

        snap = _load_mining_snapshot(vault)
        seeds = miner.mine_all(
            vault,
            current_phase=Phase.UNCLASSIFIED,
            snapshot=snap,
        )

        assert any(s.strategy is MiningStrategy.THREAD_TERMINUS for s in seeds)
