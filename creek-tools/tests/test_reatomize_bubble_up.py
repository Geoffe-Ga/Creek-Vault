"""Tests for the split-path weighted bubble-up (issue #368).

Covers :func:`creek.classify.reatomize.bubble_up_weighted` and
:func:`creek.classify.reatomize.choose_intelligent_unit` — the
integration points that take an LLM-classified
:class:`ClassificationTree` (PR #366 atoms) and the holonic combiner
(#367) and produce a tree whose internal nodes carry a parent
``weighted`` profile derived from their children. The user's load-
bearing assertion — a single confident-short F3 atom beats four
hedged-long F5 atoms in the parent's top frequency — is anchored
here as the central guarantee of the integration.

The tests build :class:`ClassificationTree` fixtures directly rather
than going through :func:`classify_reatomize` so each invariant gets
a controlled, deterministic input; the bubble-up function is pure
over the tree, so this is the right granularity.
"""

from __future__ import annotations

import pytest

from creek.classify.reatomize import (
    ClassificationTree,
    bubble_up_weighted,
    choose_intelligent_unit,
)
from creek.classify.weighted import (
    WeightedDimension,
    WeightedFragmentClassification,
)
from creek.ingest.base import IngestedFragment
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    Phase,
    SourcePlatform,
)

# ---- Fixture helpers -------------------------------------------------------


def _wfc(
    *,
    frequencies: tuple[tuple[Frequency, float], ...] = (),
    phases: tuple[tuple[Phase, float], ...] = (),
    overall_confidence: float = 0.7,
) -> WeightedFragmentClassification:
    """Build a :class:`WeightedFragmentClassification` from concise tuples."""
    return WeightedFragmentClassification(
        frequencies=tuple(WeightedDimension(value=v, weight=w) for v, w in frequencies),
        phases=tuple(WeightedDimension(value=v, weight=w) for v, w in phases),
        overall_confidence=overall_confidence,
    )


def _make_node(
    *,
    fragment_id: str,
    weighted: WeightedFragmentClassification | None = None,
    children: tuple[ClassificationTree, ...] = (),
    confidence: float = 0.7,
    depth: int = 0,
    level: str = "paragraph",
) -> ClassificationTree:
    """Build a :class:`ClassificationTree` node with a fragment + weighted."""
    fragment = Fragment(
        id=fragment_id,
        title=fragment_id,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        level=level,  # type: ignore[arg-type]
        weighted=weighted,
    )
    ingested = IngestedFragment(fragment=fragment, body="(test body)")
    stop = "accepted" if not children else "split"
    return ClassificationTree(
        fragment=ingested,
        confidence=confidence,
        stop_reason=stop,  # type: ignore[arg-type]
        children=children,
        depth=depth,
    )


# ---- bubble_up_weighted edge cases -----------------------------------------


class TestBubbleUpEdgeCases:
    """Leaves and missing-weighted children are honoured."""

    def test_leaf_passes_through_unchanged(self) -> None:
        """A leaf (no children) is returned unchanged."""
        leaf = _make_node(
            fragment_id="frag-leaf0000001",
            weighted=_wfc(frequencies=((Frequency.F3, 0.8),)),
        )
        bubbled = bubble_up_weighted(leaf)
        assert bubbled is leaf

    def test_all_children_unweighted_leaves_parent_untouched(self) -> None:
        """When every child has ``weighted=None`` the parent stays unchanged."""
        parent = _make_node(
            fragment_id="frag-parent00001",
            children=(
                _make_node(fragment_id="frag-child0000001", weighted=None),
                _make_node(fragment_id="frag-child0000002", weighted=None),
            ),
        )
        bubbled = bubble_up_weighted(parent)
        assert bubbled.fragment.fragment.weighted is None


# ---- bubble_up_weighted: agreement and divergence --------------------------


class TestBubbleUpAgreement:
    """Three F3-leaning atoms produce an F3-dominant parent."""

    def test_three_f3_children_produce_f3_parent(self) -> None:
        """The parent's top frequency mirrors the children's agreement."""
        children = tuple(
            _make_node(
                fragment_id=f"frag-f3child0000{i}",
                weighted=_wfc(
                    frequencies=((Frequency.F3, 0.8),),
                    overall_confidence=0.85,
                ),
            )
            for i in range(3)
        )
        parent = _make_node(
            fragment_id="frag-agreed00001",
            children=children,
        )
        bubbled = bubble_up_weighted(parent)
        weighted = bubbled.fragment.fragment.weighted
        assert weighted is not None
        assert weighted.frequencies[0].value is Frequency.F3
        # Confidence is at or above the per-child mean (no divergence).
        assert weighted.overall_confidence >= 0.7


class TestBubbleUpDivergence:
    """Three confident-but-different children dampen confidence."""

    def test_three_divergent_children_dampens_confidence(self) -> None:
        """F3/F5/F7 each at 0.9 confidence → parent confidence below 0.9."""
        children = tuple(
            _make_node(
                fragment_id=f"frag-div0000000{i}",
                weighted=_wfc(
                    frequencies=((freq, 0.9),),
                    overall_confidence=0.9,
                ),
            )
            for i, freq in enumerate((Frequency.F3, Frequency.F5, Frequency.F7))
        )
        parent = _make_node(
            fragment_id="frag-divparent01",
            children=children,
        )
        bubbled = bubble_up_weighted(parent)
        weighted = bubbled.fragment.fragment.weighted
        assert weighted is not None
        assert weighted.overall_confidence < 0.9
        # All three frequencies surface.
        surfaced = {entry.value for entry in weighted.frequencies}
        assert Frequency.F3 in surfaced
        assert Frequency.F5 in surfaced
        assert Frequency.F7 in surfaced


# ---- The user's load-bearing case ------------------------------------------


class TestConvictionOverLength:
    """One confident-short F3 atom beats four hedged-long F5 atoms."""

    def test_confident_short_beats_hedged_long_in_split(self) -> None:
        """Load-bearing fixture for issue #368 — see PR conversation."""
        confident_short = _make_node(
            fragment_id="frag-short00001",
            weighted=_wfc(
                frequencies=((Frequency.F3, 0.9),),
                overall_confidence=0.9,
            ),
        )
        hedged_long = tuple(
            _make_node(
                fragment_id=f"frag-long000000{i}",
                weighted=_wfc(
                    frequencies=((Frequency.F5, 0.3),),
                    overall_confidence=0.2,
                ),
            )
            for i in range(4)
        )
        parent = _make_node(
            fragment_id="frag-essay00001",
            children=(confident_short, *hedged_long),
        )
        bubbled = bubble_up_weighted(parent)
        weighted = bubbled.fragment.fragment.weighted
        assert weighted is not None
        # Parent's top frequency is F3 (not F5) — length-free contract.
        assert weighted.frequencies[0].value is Frequency.F3


# ---- Soft-failed atoms -----------------------------------------------------


class TestSoftFailedAtoms:
    """Atoms whose classifier failed soft to empty contribute nothing."""

    def test_failed_atoms_yield_zero_confidence_parent(self) -> None:
        """A subtree of empty-weighted atoms produces an empty parent profile."""
        empty_children = tuple(
            _make_node(
                fragment_id=f"frag-fail000000{i}",
                weighted=WeightedFragmentClassification(),  # empty, zero conf
            )
            for i in range(3)
        )
        parent = _make_node(
            fragment_id="frag-failparent1",
            children=empty_children,
        )
        bubbled = bubble_up_weighted(parent)
        weighted = bubbled.fragment.fragment.weighted
        # Children contribute (they have weighted != None) but at zero
        # confidence, so the combiner returns the no-signal profile.
        assert weighted is not None
        assert weighted.frequencies == ()
        assert weighted.overall_confidence == 0.0


# ---- Deep tree associativity ----------------------------------------------


class TestDeepTreeAssociativity:
    """Combining is associative across the tree depth."""

    def test_deep_tree_bubbles_layer_by_layer(self) -> None:
        """document → section → paragraph → sentence; root reflects every leaf."""

        # Two sentences under a paragraph, two paragraphs under a section,
        # one section under the document. All sentences agree on F3.
        def _sentence(idx: int) -> ClassificationTree:
            return _make_node(
                fragment_id=f"frag-sent0000{idx:03d}",
                level="sentence",
                weighted=_wfc(
                    frequencies=((Frequency.F3, 0.8),),
                    overall_confidence=0.85,
                ),
            )

        paragraph_a = _make_node(
            fragment_id="frag-para0000001",
            level="paragraph",
            children=(_sentence(1), _sentence(2)),
        )
        paragraph_b = _make_node(
            fragment_id="frag-para0000002",
            level="paragraph",
            children=(_sentence(3), _sentence(4)),
        )
        section = _make_node(
            fragment_id="frag-section0001",
            level="section",
            children=(paragraph_a, paragraph_b),
        )
        document = _make_node(
            fragment_id="frag-document001",
            level="document",
            children=(section,),
        )
        bubbled = bubble_up_weighted(document)
        root_weighted = bubbled.fragment.fragment.weighted
        assert root_weighted is not None
        # Every level surfaces F3 as the top frequency.
        assert root_weighted.frequencies[0].value is Frequency.F3
        assert root_weighted.overall_confidence == pytest.approx(0.85, abs=0.05)


# ---- choose_intelligent_unit -----------------------------------------------


class TestChooseIntelligentUnit:
    """The walk selects the deepest sensibly-confident level."""

    def test_lone_confident_descendant_wins(self) -> None:
        """A single confident child wins over a confident-but-similar parent."""
        # Parent at confidence 0.8, child at confidence 0.78 — material
        # delta default is 0.05, so the parent doesn't materially beat
        # the child; the child is the finer-grain pick.
        child = _make_node(
            fragment_id="frag-child0000001",
            weighted=_wfc(overall_confidence=0.78),
        )
        parent = _make_node(
            fragment_id="frag-parent00001",
            weighted=_wfc(overall_confidence=0.8),
            children=(child,),
        )
        chosen = choose_intelligent_unit(parent, threshold=0.6)
        assert chosen.fragment.fragment.id == "frag-child0000001"

    def test_parent_materially_beats_child(self) -> None:
        """When the parent materially exceeds the child, the parent wins."""
        weak_child = _make_node(
            fragment_id="frag-weak00000001",
            weighted=_wfc(overall_confidence=0.5),
        )
        strong_parent = _make_node(
            fragment_id="frag-strong000001",
            weighted=_wfc(overall_confidence=0.85),
            children=(weak_child,),
        )
        chosen = choose_intelligent_unit(strong_parent, threshold=0.6)
        # Child at 0.5 is below threshold; the parent at 0.85 wins.
        assert chosen.fragment.fragment.id == "frag-strong000001"

    def test_no_descendant_clears_threshold(self) -> None:
        """When nothing reaches the threshold the root is returned."""
        child = _make_node(
            fragment_id="frag-low00000001",
            weighted=_wfc(overall_confidence=0.3),
        )
        root = _make_node(
            fragment_id="frag-rootlow0001",
            weighted=_wfc(overall_confidence=0.4),
            children=(child,),
        )
        chosen = choose_intelligent_unit(root, threshold=0.6)
        assert chosen.fragment.fragment.id == "frag-rootlow0001"

    def test_unweighted_root_returns_self(self) -> None:
        """A tree whose root has ``weighted=None`` returns the root itself."""
        root = _make_node(fragment_id="frag-noweighted01", weighted=None)
        chosen = choose_intelligent_unit(root, threshold=0.6)
        assert chosen.fragment.fragment.id == "frag-noweighted01"
