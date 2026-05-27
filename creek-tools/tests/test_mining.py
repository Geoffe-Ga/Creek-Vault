"""Tests for creek.generate.mining — blog idea mining pipeline.

Covers :class:`IdeaMiner` (Section 11.5 of the Creek Ontology): four
mining strategies plus combined ``mine_all`` for surfacing essay-worthy
seeds from liminal fragments, thread termini, resonance chains, and
wavelength-phase windows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.mining import (
    DEFAULT_MIN_CHAIN_LENGTH,
    DEFAULT_MIN_THREAD_FRAGMENTS,
    DEFAULT_SIMILARITY_LIMINAL,
    DEFAULT_SIMILARITY_RESONANCE,
    DEFAULT_UNEXPLORED_LIMIT,
    IdeaMiner,
    IdeaSeed,
    MiningStrategy,
    OntologyTuple,
    _jaccard_similarity,
    phase_filtered_seeds,
)
from creek.models import (
    Dosage,
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
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)
from tests.factories.compiled import (
    write_compiled_eddy_page as _write_compiled_eddy_page,
)
from tests.factories.compiled import (
    write_compiled_frequency_page as _write_compiled_frequency_page,
)
from tests.factories.compiled import (
    write_compiled_thread_page as _write_compiled_thread_page,
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
    mode: Mode = Mode.EXPRESS,
    dosage: Dosage = Dosage.MEDICINE,
    voice_register: VoiceRegister | None = VoiceRegister.ANALYTICAL,
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
            mode=mode,
            orientation=Orientation.DO,
            dosage=dosage,
        ),
        voice=VoiceClassification(voice_register=voice_register),
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


# ---- FEAT-004 compiled-layer routing -------------------------------------


class TestCompileFirstThreadTerminus:
    """``mine_thread_terminus`` reads compiled pages before fragments."""

    def test_provenance_drives_source_fragments_when_compiled_page_exists(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Compiled-page provenance — not raw fragment scan — provides IDs."""
        thread = _build_thread(
            thread_id="thread-grief",
            title="Grief rhythm",
            fragment_count=12,
        )
        _write_thread(vault, thread)
        _write_compiled_thread_page(
            vault,
            target_id="thread-grief",
            title="Grief rhythm",
            fragment_ids=("frag-compiled-1", "frag-compiled-2"),
        )
        for day in range(1, 13):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-raw-{day}",
                    title=f"raw {day}",
                    threads=("thread-grief",),
                    created_day=day,
                ),
                "Body.",
            )

        seeds = miner.mine_thread_terminus(vault)

        assert len(seeds) == 1
        assert set(seeds[0].source_fragments) == {
            "frag-compiled-1",
            "frag-compiled-2",
        }
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()

    def test_missing_compiled_page_falls_back_and_logs_gap(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Without a compiled page, fragments-as-fallback + gap entry."""
        thread = _build_thread(
            thread_id="thread-fallback",
            title="Fallback thread",
            fragment_count=12,
        )
        _write_thread(vault, thread)
        for day in range(1, 13):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-{day:02d}",
                    title=f"raw {day}",
                    threads=("thread-fallback",),
                    created_day=day,
                ),
                "Body.",
            )

        seeds = miner.mine_thread_terminus(vault)

        assert len(seeds) == 1
        assert "frag-01" in seeds[0].source_fragments
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert gaps_log.exists()
        contents = gaps_log.read_text(encoding="utf-8")
        assert "thread-fallback" in contents
        assert "mine.thread_terminus" in contents

    def test_bypass_compiled_skips_log_and_uses_fragment_scan(
        self,
        vault: Path,
    ) -> None:
        """``bypass_compiled=True`` reads raw fragments and never logs."""
        thread = _build_thread(
            thread_id="thread-bypass",
            title="Bypass thread",
            fragment_count=12,
        )
        _write_thread(vault, thread)
        _write_compiled_thread_page(
            vault,
            target_id="thread-bypass",
            title="Bypass thread",
            fragment_ids=("frag-compiled-1",),
        )
        for day in range(1, 13):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-{day:02d}",
                    title=f"raw {day}",
                    threads=("thread-bypass",),
                    created_day=day,
                ),
                "Body.",
            )

        bypass_miner = IdeaMiner(bypass_compiled=True)
        seeds = bypass_miner.mine_thread_terminus(vault)

        assert len(seeds) == 1
        assert "frag-compiled-1" not in seeds[0].source_fragments
        assert "frag-01" in seeds[0].source_fragments
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()


class TestCompileFirstLiminalCrossEddy:
    """``mine_liminal_cross_eddy`` compares against compiled eddy bodies."""

    def test_compiled_eddy_body_drives_similarity(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Compile-first routing reads the eddy's compiled body, not its description."""
        eddy = _build_eddy(
            eddy_id="eddy-grief",
            title="Bare frontmatter title",
            description="bland",
        )
        _write_eddy(vault, eddy)
        _write_compiled_eddy_page(
            vault,
            target_id="eddy-grief",
            title="Grief rituals metabolise",
            body="# Grief rituals\nGrief rituals metabolise the body slowly.\n",
        )

        frag = _build_fragment(frag_id="frag-w", title="Wandering")
        _write_liminal_fragment(
            vault,
            frag,
            "Grief rituals metabolise the body slowly.",
            kind="Unnamed",
        )

        seeds = miner.mine_liminal_cross_eddy(vault)

        assert len(seeds) == 1
        assert seeds[0].eddies == ("eddy-grief",)
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()

    def test_missing_compiled_eddy_logs_gap(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A frontmatter-only eddy still seeds (fallback) but logs a gap."""
        eddy = _build_eddy(
            eddy_id="eddy-grief",
            title="Grief practices",
            description="Rituals for metabolising grief.",
        )
        _write_eddy(vault, eddy)

        frag = _build_fragment(frag_id="frag-w", title="Wandering")
        _write_liminal_fragment(
            vault,
            frag,
            "Notes on grief rituals and metabolising grief slowly.",
            kind="Unnamed",
        )

        miner.mine_liminal_cross_eddy(vault)

        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert gaps_log.exists()
        assert "eddy-grief" in gaps_log.read_text(encoding="utf-8")

    def test_missing_compiled_eddy_logs_one_gap_per_eddy(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Per-run dedup: each eddy logs once regardless of liminal-fragment count.

        Mirrors the wavelength-window dedup contract. With L liminal
        fragments and E uncompiled eddies, the log should grow by E
        entries per run, not L * E.
        """
        for eid in ("eddy-grief", "eddy-rituals"):
            _write_eddy(
                vault,
                _build_eddy(
                    eddy_id=eid,
                    title=f"{eid} title",
                    description="Notes on grief rituals and metabolising grief slowly.",
                ),
            )
        for n in range(3):
            _write_liminal_fragment(
                vault,
                _build_fragment(frag_id=f"frag-{n}", title=f"Wandering {n}"),
                "Notes on grief rituals and metabolising grief slowly.",
                kind="Unnamed",
            )

        miner.mine_liminal_cross_eddy(vault)

        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        lines = gaps_log.read_text(encoding="utf-8").strip().splitlines()
        eddy_ids_logged = [json.loads(line)["target_id"] for line in lines]
        assert sorted(eddy_ids_logged) == sorted(["eddy-grief", "eddy-rituals"])


class TestCompileFirstWavelengthWindow:
    """``mine_wavelength_windows`` admits via compiled frequency provenance."""

    def test_compiled_frequency_filters_to_provenance(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Only fragments listed in the compiled F<x> page surface."""
        in_provenance = _build_fragment(
            frag_id="frag-listed",
            title="Listed fragment",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
            frequency=Frequency.F5,
        )
        outside_provenance = _build_fragment(
            frag_id="frag-orphan",
            title="Orphan fragment",
            phase=Phase.RISING,
            praxis=PraxisPotential.EXPLICIT,
            frequency=Frequency.F5,
        )
        _write_fragment(vault, in_provenance, "body")
        _write_fragment(vault, outside_provenance, "body")
        _write_compiled_frequency_page(
            vault,
            target_id="F5",
            fragment_ids=("frag-listed",),
        )

        seeds = miner.mine_wavelength_windows(vault, current_phase=Phase.RISING)

        ids = {fid for seed in seeds for fid in seed.source_fragments}
        assert ids == {"frag-listed"}

    def test_missing_frequency_index_logs_one_gap_per_frequency(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """All fragments fall through and the gap log records one entry per F<x>."""
        for fid in ("frag-1", "frag-2"):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=fid,
                    title=fid,
                    phase=Phase.RISING,
                    praxis=PraxisPotential.EXPLICIT,
                    frequency=Frequency.F5,
                ),
                "body",
            )

        seeds = miner.mine_wavelength_windows(vault, current_phase=Phase.RISING)

        assert len(seeds) == 2
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        lines = gaps_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "F5" in lines[0]


class TestCompileFirstUnchangedExternalBehaviour:
    """User-facing seeds are unchanged when the vault has compiled coverage."""

    def test_present_vault_yields_same_seeds_with_or_without_routing(
        self,
        vault: Path,
    ) -> None:
        """Seeds from a fully-compiled vault match the bypass output (regression AC)."""
        thread = _build_thread(
            thread_id="thread-grief",
            title="Grief rhythm",
            fragment_count=12,
        )
        _write_thread(vault, thread)
        for day in range(1, 13):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-{day:02d}",
                    title=f"raw {day}",
                    threads=("thread-grief",),
                    created_day=day,
                ),
                "Body.",
            )
        _write_compiled_thread_page(
            vault,
            target_id="thread-grief",
            title="Grief rhythm",
            fragment_ids=tuple(f"frag-{day:02d}" for day in range(1, 13)),
        )

        compiled_seeds = IdeaMiner().mine_all(vault, current_phase=Phase.UNCLASSIFIED)
        bypass_seeds = IdeaMiner(bypass_compiled=True).mine_all(
            vault,
            current_phase=Phase.UNCLASSIFIED,
        )

        compiled_titles = {s.title for s in compiled_seeds}
        bypass_titles = {s.title for s in bypass_seeds}
        assert compiled_titles == bypass_titles


# ---------------------------------------------------------------------------
# #356 unexplored-ontology strategy
# ---------------------------------------------------------------------------


class TestOntologyTuple:
    """``OntologyTuple`` is a frozen, hashable position in dimension space."""

    def test_ontology_tuple_is_hashable(self) -> None:
        """Tuples must be hashable for set/dict membership."""
        position = OntologyTuple(
            phase=Phase.WITHDRAWAL,
            frequency=Frequency.F3,
            mode=Mode.INHABIT,
            voice_register=VoiceRegister.CONFESSIONAL,
            dosage=Dosage.TOXIC,
        )
        assert isinstance(hash(position), int)

    def test_ontology_tuple_renders_human_label(self) -> None:
        """The human label joins each dimension's value with separators."""
        label = OntologyTuple(
            phase=Phase.PEAKING,
            frequency=Frequency.F7,
            mode=Mode.EXPRESS,
            voice_register=VoiceRegister.PROPHETIC,
            dosage=Dosage.MEDICINE,
        ).label()
        assert "peaking" in label
        assert "F7" in label
        assert "express" in label
        assert "prophetic" in label
        assert "medicine" in label


class TestMineUnexploredOntology:
    """``mine_unexplored_ontology`` surfaces the rarest classification tuples."""

    def test_dominant_combination_leaves_huge_gap(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A corpus piled into one tuple surfaces everything else as a gap."""
        # 20 fragments piled into (peaking, F7, express, prophetic, medicine).
        for n in range(20):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-dom-{n:02d}",
                    title=f"Dominant {n}",
                    phase=Phase.PEAKING,
                    frequency=Frequency.F7,
                    mode=Mode.EXPRESS,
                    voice_register=VoiceRegister.PROPHETIC,
                    dosage=Dosage.MEDICINE,
                    created_day=(n % 28) + 1,
                ),
                "Body.",
            )

        seeds = miner.mine_unexplored_ontology(vault, limit=5)

        assert len(seeds) == 5
        # No surfaced gap reproduces the dominant tuple verbatim.
        dominant_label = OntologyTuple(
            phase=Phase.PEAKING,
            frequency=Frequency.F7,
            mode=Mode.EXPRESS,
            voice_register=VoiceRegister.PROPHETIC,
            dosage=Dosage.MEDICINE,
        ).label()
        assert all(dominant_label not in s.title for s in seeds)
        # Every surfaced seed identifies the unexplored strategy.
        assert all(s.strategy is MiningStrategy.UNEXPLORED_ONTOLOGY for s in seeds)

    def test_seed_includes_dimensional_position_and_framing(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Each surfaced seed reports the tuple and a framing description."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-anchor",
                title="Anchor",
                phase=Phase.PEAKING,
                frequency=Frequency.F7,
                mode=Mode.EXPRESS,
                voice_register=VoiceRegister.PROPHETIC,
                dosage=Dosage.MEDICINE,
            ),
            "Body.",
        )

        seeds = miner.mine_unexplored_ontology(vault, limit=3)

        for seed in seeds:
            assert seed.brief_description
            # The brief contains the natural-language framing question.
            assert "?" in seed.brief_description
            # The title carries the dimensional tuple.
            assert " x " in seed.title

    def test_even_coverage_corpus_yields_equal_scores(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """With every tuple tied at zero coverage, surfaced scores are equal."""
        # Empty corpus: every tuple has count == 0. The require_corpus
        # guard would otherwise short-circuit; we disable it to exercise
        # the rank-ordering across a tied corpus.
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=8,
            require_corpus=False,
        )

        assert len(seeds) == 8
        # All surfaced seeds carry the same maximal score.
        first_score = seeds[0].score
        assert all(seed.score == first_score for seed in seeds)
        # And the score is positive (rarest possible).
        assert first_score > 0

    def test_rank_is_inverse_to_fragment_count(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Combinations with more coverage rank below those with less."""
        # 5 fragments at one tuple, 1 fragment at a less-covered tuple.
        for n in range(5):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=f"frag-dense-{n}",
                    title=f"Dense {n}",
                    phase=Phase.PEAKING,
                    frequency=Frequency.F7,
                    mode=Mode.EXPRESS,
                    voice_register=VoiceRegister.PROPHETIC,
                    dosage=Dosage.MEDICINE,
                    created_day=n + 1,
                ),
                "Body.",
            )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-sparse",
                title="Sparse",
                phase=Phase.WITHDRAWAL,
                frequency=Frequency.F3,
                mode=Mode.INHABIT,
                voice_register=VoiceRegister.CONFESSIONAL,
                dosage=Dosage.TOXIC,
            ),
            "Body.",
        )

        # Use a large explicit limit to surface both tuples.
        seeds = miner.mine_unexplored_ontology(vault, limit=10_000)

        # Build a lookup: tuple_label -> rank.
        labels = [seed.title for seed in seeds]
        dense_label = OntologyTuple(
            phase=Phase.PEAKING,
            frequency=Frequency.F7,
            mode=Mode.EXPRESS,
            voice_register=VoiceRegister.PROPHETIC,
            dosage=Dosage.MEDICINE,
        ).label()
        sparse_label = OntologyTuple(
            phase=Phase.WITHDRAWAL,
            frequency=Frequency.F3,
            mode=Mode.INHABIT,
            voice_register=VoiceRegister.CONFESSIONAL,
            dosage=Dosage.TOXIC,
        ).label()
        # Find ranks; both should exist in the (effectively) unlimited list.
        dense_rank = next(
            (i for i, t in enumerate(labels) if dense_label in t),
            None,
        )
        sparse_rank = next(
            (i for i, t in enumerate(labels) if sparse_label in t),
            None,
        )
        assert dense_rank is not None
        assert sparse_rank is not None
        # The sparse tuple has fewer fragments → ranks higher (smaller index).
        assert sparse_rank < dense_rank

    def test_unclassified_fragment_does_not_count_against_any_tuple(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Fragments missing a dimension are ignored — they don't seed coverage."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-unclassified",
                title="Unclassified",
                phase=Phase.UNCLASSIFIED,
                voice_register=None,
            ),
            "Body.",
        )

        seeds = miner.mine_unexplored_ontology(vault, limit=3)

        # No tuple has any coverage; all seeds share the empty-corpus score.
        assert seeds
        assert len({seed.score for seed in seeds}) == 1

    def test_mine_all_includes_unexplored_ontology_seeds(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """The strategy participates in ``mine_all`` by default."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-anchor",
                title="Anchor",
            ),
            "Body.",
        )

        seeds = miner.mine_all(vault, current_phase=Phase.UNCLASSIFIED)

        assert any(s.strategy is MiningStrategy.UNEXPLORED_ONTOLOGY for s in seeds)

    def test_limit_caps_results(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A positive ``limit`` caps the surfaced seed count."""
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=2,
            require_corpus=False,
        )
        assert len(seeds) == 2

    def test_zero_limit_uses_default(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """``limit == 0`` returns the default cap (not zero, not everything)."""
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=0,
            require_corpus=False,
        )
        # Default is DEFAULT_UNEXPLORED_LIMIT.
        assert len(seeds) == DEFAULT_UNEXPLORED_LIMIT

    def test_source_fragments_is_empty(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Unexplored-ontology seeds reference absence — no source fragments."""
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=1,
            require_corpus=False,
        )
        assert not seeds[0].source_fragments

    def test_frequency_affinity_matches_tuple_frequency(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Each seed's frequency_affinity reflects the tuple's frequency dim."""
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=5,
            require_corpus=False,
        )
        for seed in seeds:
            # Exactly one frequency, never UNCLASSIFIED.
            assert len(seed.frequency_affinity) == 1
            assert seed.frequency_affinity[0] != Frequency.UNCLASSIFIED

    def test_empty_corpus_returns_no_seeds_by_default(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """An empty vault yields no unexplored-ontology seeds (require_corpus default).

        Surfacing "every combination is unexplored" against an empty
        corpus is uninformative — every dimension is equally rare. The
        guard preserves the historical "no seeds surfaced" experience
        for first-time users with no fragments yet.
        """
        seeds = miner.mine_unexplored_ontology(vault, limit=5)
        assert not seeds

    def test_unclassified_only_corpus_surfaces_max_score_seeds(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A vault of only fully-unclassified fragments still surfaces rarest tuples.

        The require_corpus guard cares about the *load* of any fragment
        at all (the user has indeed begun the work), not about how many
        of them are classified yet. With at least one fragment present
        the strategy returns its rarest-tuple list, even if every
        fragment is in the unclassified column.
        """
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-1",
                title="Anchor",
                voice_register=None,
            ),
            "Body.",
        )
        seeds = miner.mine_unexplored_ontology(vault, limit=3)
        # Corpus is non-empty so the strategy runs; the unclassified
        # fragment contributes zero coverage, so the rarest tuples
        # surface at the maximal score.
        assert len(seeds) == 3

    def test_negative_limit_returns_no_seeds(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """``limit < 0`` returns an empty list, matching the docstring.

        The docstring guarantees that negative limits clamp to zero —
        callers passing a negative value (often from arithmetic that
        underflowed) get an honest empty result rather than the
        :data:`DEFAULT_UNEXPLORED_LIMIT` fallback that ``limit == 0``
        triggers.
        """
        seeds = miner.mine_unexplored_ontology(
            vault,
            limit=-1,
            require_corpus=False,
        )
        assert seeds == []


# ---------------------------------------------------------------------------
# FEAT-007: phase_filtered_seeds edge cases
# ---------------------------------------------------------------------------


class TestPhaseFilteredSeedsEdgeCases:
    """Edge-case contract pinning for ``phase_filtered_seeds`` (FEAT-007)."""

    def test_zero_n_returns_empty_list(self, vault: Path) -> None:
        """``n == 0`` short-circuits to an empty list without scanning the vault."""
        seeds = phase_filtered_seeds(vault, Phase.RISING, n=0)
        assert seeds == []

    def test_negative_n_clamps_to_empty(self, vault: Path) -> None:
        """A negative ``n`` is clamped to zero (no negative-slice surprises)."""
        seeds = phase_filtered_seeds(vault, Phase.RISING, n=-5)
        assert seeds == []

    def test_unrecognised_phase_string_falls_through_permissively(
        self,
        vault: Path,
    ) -> None:
        """A garbage phase string is treated as UNCLASSIFIED → no strategy filter.

        The fallthrough is the documented contract — see the
        ``_PHASE_AWARE_STRATEGIES.get(...)`` call in mining.py. Pinning
        it here makes the permissive default a load-bearing test.
        """
        thread = _build_thread(
            thread_id="thread-active",
            title="Active thread",
            fragment_count=DEFAULT_MIN_THREAD_FRAGMENTS + 5,
        )
        _write_thread(vault, thread)

        seeds = phase_filtered_seeds(vault, "not-a-real-phase", n=5)
        # The unclassified fallthrough means every strategy contributes —
        # the thread-terminus seed should be present.
        assert any(s.strategy == MiningStrategy.THREAD_TERMINUS for s in seeds), (
            "unrecognised phase must fall through to all strategies"
        )


# ---------------------------------------------------------------------------
# Issue #340: honest diagnostics for a zero-seed mining run
# ---------------------------------------------------------------------------


class TestMiningRunReport:
    """``IdeaMiner.mine_all_with_report`` exposes per-strategy diagnostics.

    The bug we're patching: a 188-fragment vault returns ``No idea seeds
    surfaced`` with no explanation. The miner must expose enough state
    to tell the operator *why* — top score, threshold, fallback reason.
    """

    def test_report_records_one_diagnostic_per_strategy(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Every strategy contributes exactly one diagnostic to the report."""
        report = miner.mine_all_with_report(vault, current_phase=Phase.RISING)

        strategies = {diag.strategy for diag in report.diagnostics}
        assert strategies == set(MiningStrategy)

    def test_report_seeds_match_mine_all(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """``report.seeds`` equals ``mine_all`` output (no drift)."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Unique thread title",
                fragment_count=15,
            ),
        )
        legacy = miner.mine_all(vault, current_phase=Phase.UNCLASSIFIED)
        report = miner.mine_all_with_report(vault, current_phase=Phase.UNCLASSIFIED)

        assert list(report.seeds) == legacy

    def test_empty_vault_yields_per_strategy_diagnostics_and_no_seeds(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Empty vault produces one diagnostic per strategy, all with zero kept.

        Replaces a previous ``top_score`` aggregate check. Per-strategy
        scores are reported in different units (fragment counts vs.
        Jaccard similarity vs. binary 1.0 gates), so ``max()`` across
        them is meaningless and the aggregate property was removed.
        """
        empty_report = miner.mine_all_with_report(vault, current_phase=Phase.RISING)
        assert empty_report.seeds == ()
        assert {diag.strategy for diag in empty_report.diagnostics} == set(
            MiningStrategy,
        )
        assert all(diag.candidates_kept == 0 for diag in empty_report.diagnostics)

    def test_diagnostic_counts_threshold_and_candidates_for_thread_terminus(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Thread terminus reports candidates considered and the threshold."""
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-big",
                title="Big thread",
                fragment_count=15,
            ),
        )
        _write_thread(
            vault,
            _build_thread(
                thread_id="thread-tiny",
                title="Tiny thread",
                fragment_count=2,
            ),
        )
        report = miner.mine_all_with_report(vault, current_phase=Phase.UNCLASSIFIED)
        diag = next(
            d
            for d in report.diagnostics
            if d.strategy is MiningStrategy.THREAD_TERMINUS
        )

        assert diag.candidates_considered == 2
        assert diag.candidates_kept == 1
        assert diag.threshold == float(miner.min_thread_fragments)
        assert diag.top_score == 15.0

    def test_wavelength_top_score_reports_phase_matches_not_seeds(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Wavelength ``top_score`` is the pre-threshold phase-match count.

        Regression for the issue #340 review on PR #346: when 10 fragments
        match the current phase but every one is dropped by the
        ``praxis_potential == EXPLICIT`` gate, the diagnostic must report
        ``top_score=10.0`` (the pre-gate count) — not ``0.0``, which would
        be indistinguishable from "no phase matches at all".
        """
        n_phase_matches = 10
        for idx in range(n_phase_matches):
            frag = _build_fragment(
                frag_id=f"frag-rising-{idx}",
                title=f"Rising fragment {idx}",
                phase=Phase.RISING,
                praxis=PraxisPotential.LATENT,  # gated out by EXPLICIT filter
            )
            _write_fragment(vault, frag, f"Body {idx}")

        report = miner.mine_all_with_report(vault, current_phase=Phase.RISING)
        diag = next(
            d
            for d in report.diagnostics
            if d.strategy is MiningStrategy.WAVELENGTH_WINDOW
        )

        assert diag.candidates_considered == n_phase_matches
        assert diag.candidates_kept == 0
        # The bug: top_score was len(seeds)=0, making it indistinguishable
        # from "no phase matches at all". The fix reports len(phase_matches)=10.
        assert diag.top_score == float(n_phase_matches)
        assert diag.threshold == 1.0
        # And the praxis-gate-drop case names itself explicitly, instead
        # of leaving fallback_reason=None (which silenced the failure
        # mode in the original PR #346 review).
        assert diag.fallback_reason is not None
        assert "praxis_potential=EXPLICIT" in diag.fallback_reason

    def test_resonance_chain_zero_synchronicities_records_fallback_reason(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """When no synchronicity records exist, the diagnostic explains the miss."""
        report = miner.mine_all_with_report(vault, current_phase=Phase.UNCLASSIFIED)
        diag = next(
            d
            for d in report.diagnostics
            if d.strategy is MiningStrategy.RESONANCE_CHAIN
        )
        assert diag.candidates_considered == 0
        assert diag.fallback_reason is not None
        assert "synchron" in diag.fallback_reason.lower()

    def test_resonance_chain_short_components_records_fallback_reason(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """Symmetric to the no-synchronicities case: when synchronicities
        exist but every connected component is below ``min_chain_length``,
        the diagnostic must name the gap (largest component size vs.
        threshold) instead of leaving ``fallback_reason=None``. Closes the
        ``almost kept`` blind spot raised in the PR #346 re-review.
        """
        # Build a 2-fragment component (below default min_chain_length=3).
        _write_fragment(
            vault,
            _build_fragment(frag_id="a", title="A"),
            "Body A.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="b",
                title="B",
                platform=SourcePlatform.CLAUDE,
            ),
            "Body B.",
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

        report = miner.mine_all_with_report(vault, current_phase=Phase.UNCLASSIFIED)
        diag = next(
            d
            for d in report.diagnostics
            if d.strategy is MiningStrategy.RESONANCE_CHAIN
        )

        assert diag.candidates_considered == 1  # one component
        assert diag.candidates_kept == 0  # below min_chain_length
        assert diag.top_score == 2.0  # largest component size
        assert diag.fallback_reason is not None
        assert "min_chain_length" in diag.fallback_reason
        assert "2" in diag.fallback_reason  # actual largest component size


class TestMiningDiagnosticLogLines:
    """Each strategy emits a single INFO log line with diagnostic numbers."""

    def test_every_strategy_emits_log_line(
        self,
        vault: Path,
        miner: IdeaMiner,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All four strategies log a ``[mine] <strategy>: ...`` line at INFO."""
        with caplog.at_level("INFO", logger="creek.generate.mining"):
            miner.mine_all_with_report(vault, current_phase=Phase.RISING)

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        for strategy in MiningStrategy:
            assert f"[mine] {strategy.value}" in log_text, (
                f"missing diagnostic line for {strategy.value}: {log_text!r}"
            )

    def test_log_line_names_threshold(
        self,
        vault: Path,
        miner: IdeaMiner,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The diagnostic line includes the active threshold value."""
        with caplog.at_level("INFO", logger="creek.generate.mining"):
            miner.mine_all_with_report(vault, current_phase=Phase.RISING)

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "threshold=" in log_text


class TestMiningFallbackGapLog:
    """Strategies that hit a fallback append to ``compile-gaps.jsonl``.

    The wavelength-window and thread-terminus paths already log
    compile-needed entries when their compiled pages are missing. The
    resonance-chain strategy now also records a fallback entry when it
    has no synchronicity records to walk — without that the operator
    has no breadcrumb explaining the zero-seed outcome.
    """

    def test_resonance_chain_logs_missing_synchronicities(
        self,
        vault: Path,
        miner: IdeaMiner,
    ) -> None:
        """A run with zero synchronicities appends one resonance-chain gap entry."""
        miner.mine_all_with_report(vault, current_phase=Phase.UNCLASSIFIED)

        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert gaps_log.exists()
        lines = gaps_log.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        assert any(e["surfaced_by"] == "mine.resonance_chain" for e in entries), entries

    def test_resonance_chain_bypass_does_not_log(
        self,
        vault: Path,
    ) -> None:
        """Bypass mode suppresses the resonance-chain fallback log."""
        IdeaMiner(bypass_compiled=True).mine_all_with_report(
            vault,
            current_phase=Phase.UNCLASSIFIED,
        )

        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()


# ---------------------------------------------------------------------------
# Issue #340: MiningConfig knobs exposed via creek_config.yaml
# ---------------------------------------------------------------------------


class TestIdeaMinerConfigKnobs:
    """The four mining thresholds are individually overridable.

    ``creek_config.yaml`` exposes these knobs so an operator can calibrate
    a small-N corpus without monkey-patching defaults. The CLI passes
    them through; the constructor must accept any subset.
    """

    def test_constructor_accepts_overridden_thresholds(self) -> None:
        """All four mining knobs are individually overridable."""
        custom = IdeaMiner(
            min_thread_fragments=5,
            min_chain_length=2,
            similarity_liminal=0.2,
            similarity_resonance=0.4,
        )
        assert custom.min_thread_fragments == 5
        assert custom.min_chain_length == 2
        assert custom.similarity_liminal == pytest.approx(0.2)
        assert custom.similarity_resonance == pytest.approx(0.4)
