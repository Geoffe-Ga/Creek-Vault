"""Confidence-driven re-atomization orchestrator (FEAT-023).

Ties together the FEAT-021 zoom-in splitter and the FEAT-022 zoom-out
aggregator: when a classifier returns ``unclassified`` or scores below
the configured confidence floor, this orchestrator chooses a direction
based on the fragment's source and structural level, re-atomizes
accordingly, classifies the new units, and recurses. Recursion stops
when a unit clears the threshold, hits a terminal level (sentence on
the small end, session on the large end), or reaches the per-config
max-depth ceiling.

A leaf that still won't classify is honestly recorded as
``unclassified`` — exactly the philosophy the rest of the pipeline
already honours, expressed structurally instead of as a single failed
verdict on a too-large or too-small unit.

Pure transform: the orchestrator returns a :class:`ClassificationTree`
and never touches disk. Callers (the CLI, batch scripts) walk the tree
to persist its nodes; that separation keeps the unit tests fast and
the persistence concern reusable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from creek.atomize.aggregate import AggregateLevel, AggregationConfig, aggregate
from creek.atomize.split import SentenceTokenizer, default_sentence_tokenizer, split
from creek.ingest.base import IngestedFragment
from creek.models import (
    Fragment,
    FragmentLevel,
    Frequency,
    Phase,
    SourcePlatform,
)

if TYPE_CHECKING:
    from creek.config import ClassificationConfig

__all__ = [
    "ClassificationTree",
    "Classifier",
    "Direction",
    "ReatomizeConfig",
    "StopReason",
    "bubble_up_weighted",
    "choose_direction",
    "choose_intelligent_unit",
    "classify_reatomize",
    "classify_reatomize_stream",
]

Direction = Literal["split", "aggregate"]
DirectionOverride = Literal["auto", "split", "aggregate"]

StopReason = Literal[
    "accepted",
    "terminal",
    "max_depth",
    "disabled",
    "no_decomposition",
    "aggregate_no_siblings",
    "passthrough",
    "split",
    "aggregated",
    "aggregated_weak",
]
"""Why the orchestrator stopped recursing on a given subtree.

Leaf reasons (``children`` is empty):

``accepted``: classifier cleared :attr:`ReatomizeConfig.threshold`.
``terminal``: at a level the operators refuse to decompose
(sentence / session).
``max_depth``: hit :attr:`ReatomizeConfig.max_depth`.
``disabled``: the run was opt-out; no recursion attempted.
``no_decomposition``: the chosen operator returned no children.
``aggregate_no_siblings``: zoom-out chosen on a lone fragment with no
siblings; see :func:`classify_reatomize_stream`.
``passthrough``: a stream member the FEAT-022 aggregator declined to
combine (wrong level for the target transition) AND that didn't
clear the threshold on its own — distinct from ``accepted`` so
downstream gates don't conflate "the orchestrator did nothing" with
"the classifier was confident".

Internal reasons (``children`` is non-empty):

``split``: parent was decomposed by the FEAT-021 zoom-in splitter.
``aggregated``: parent was assembled by the FEAT-022 zoom-out
aggregator AND cleared the threshold (only produced by
:func:`classify_reatomize_stream`).
``aggregated_weak``: parent was assembled by the aggregator but the
re-classified parent still failed the threshold. Symmetric with
``aggregated`` for downstream consumers that want to find the
"aggregation happened but didn't resolve uncertainty" subset.
"""

Classifier = Callable[[IngestedFragment], tuple[IngestedFragment, float]]
"""Callable contract for FEAT-023 classification.

Takes an :class:`IngestedFragment` (fragment metadata + body) and
returns the classified fragment paired with a 0..1 confidence score.
The function shape stays simple so tests can mock it with a closure
and production code can adapt :class:`~creek.classify.rules.RuleClassifier`
or :class:`~creek.classify.llm.LLMClassifier` via a thin wrapper.
"""

_CHAT_PLATFORMS: frozenset[SourcePlatform] = frozenset(
    {
        SourcePlatform.DISCORD,
        SourcePlatform.CLAUDE,
        SourcePlatform.CHATGPT,
    },
)
"""Source platforms whose ``document``-level units are chat messages.

Slack is named in the FEAT-023 spec but is not yet enumerated in
:class:`SourcePlatform`; it will join this set once added.
"""

_TERMINAL_LEVELS: frozenset[FragmentLevel] = frozenset({"sentence", "session"})
"""Levels that resist further re-atomization in either direction."""

_AGGREGATE_NEXT_LEVEL: dict[FragmentLevel, AggregateLevel] = {
    "document": "exchange",
    "exchange": "burst",
    "burst": "session",
}
"""For each source level, the FEAT-022 target one step coarser."""


@dataclass(frozen=True)
class ClassificationTree:
    """Recursive output of :func:`classify_reatomize`.

    Each node carries the classified fragment, the per-node confidence
    score, the reason recursion stopped (or ``"split"`` / ``"aggregated"``
    when the node has children), and the sub-trees produced.
    ``frozen=True`` so callers can hash or set-membership-test nodes
    without surprise.

    Attributes:
        fragment: Classified fragment (frontmatter + body).
        confidence: Score the classifier returned for this fragment.
        stop_reason: Why recursion ended at this node, or how it
            continued (``split`` / ``aggregated``) when ``children``
            is non-empty.
        children: Sub-trees produced by re-atomization.
        depth: Distance from the root the caller invoked the
            orchestrator at (root is 0). Recorded so a downstream
            renderer or test can assert depth-bounded recursion
            without re-walking the tree.
    """

    fragment: IngestedFragment
    confidence: float
    stop_reason: StopReason
    children: tuple[ClassificationTree, ...] = ()
    depth: int = 0


@dataclass
class ReatomizeConfig:
    """Runtime knobs for :func:`classify_reatomize`.

    Mirrors the four ``classification.reatomize_*`` YAML fields but
    keeps runtime-only fields (operator hooks, tokenizer) out of the
    user-facing config schema.

    Attributes:
        enabled: Master switch. ``False`` short-circuits to a single
            classify pass per fragment (mirrors the legacy engine).
            Default ``False`` per FEAT-023's "opt-in for v1" framing.
        threshold: Confidence floor that triggers re-atomization.
            Default ``0.7`` matches
            :class:`creek.config.ClassificationConfig.confidence_threshold`.
        max_depth: Recursion ceiling counted from the root (depth 0).
        direction: ``"auto"`` (heuristic), ``"split"``, or
            ``"aggregate"`` — overrides :func:`choose_direction`.
        sentence_tokenizer: Forwarded into the FEAT-021 splitter for
            paragraph→sentence transitions.
        aggregation_config: Forwarded into FEAT-022; defaults to stock
            thresholds. Callers wire an embedder when burst runs are
            in play.
    """

    enabled: bool = False
    threshold: float = 0.7
    # Deliberately not validated with ge=1 (unlike
    # ``ClassificationConfig.reatomize_max_depth``): the runtime knob
    # accepts 0 so tests can short-circuit recursion at the root, and
    # the CLI path projects from ``ClassificationConfig`` which already
    # enforces the user-facing floor.
    max_depth: int = 4
    direction: DirectionOverride = "auto"
    sentence_tokenizer: SentenceTokenizer = field(
        default=default_sentence_tokenizer,
    )
    aggregation_config: AggregationConfig = field(default_factory=AggregationConfig)

    @classmethod
    def from_classification_config(
        cls,
        config: ClassificationConfig,
        *,
        aggregation_config: AggregationConfig | None = None,
    ) -> ReatomizeConfig:
        """Project the FEAT-023 knobs out of a :class:`ClassificationConfig`.

        Centralises the ``reatomize_threshold is None`` fallback so
        the CLI and tests don't each re-derive it.

        Args:
            config: Loaded vault classification config.
            aggregation_config: Optional pre-built FEAT-022 config —
                callers that want a calibrated embedder wire it here.

        Returns:
            A ready-to-use :class:`ReatomizeConfig`.
        """
        threshold = (
            config.reatomize_threshold
            if config.reatomize_threshold is not None
            else config.confidence_threshold
        )
        return cls(
            enabled=config.reatomize,
            threshold=threshold,
            max_depth=config.reatomize_max_depth,
            direction=config.reatomize_direction,
            aggregation_config=aggregation_config or AggregationConfig(),
        )


def classify_reatomize(
    fragment: IngestedFragment,
    classifier: Classifier,
    *,
    config: ReatomizeConfig,
    _depth: int = 0,
) -> ClassificationTree:
    """Classify ``fragment`` and recursively re-atomize when uncertain.

    Implements the FEAT-023 algorithm:

    1. Classify the fragment.
    2. If accepted (confidence ≥ threshold AND no required dimension
       is ``unclassified``), return a leaf.
    3. Otherwise pick a direction (split / aggregate) via
       :func:`choose_direction`, decompose, recurse on the children.
    4. Stop on terminal levels, ``max_depth``, empty decomposition,
       or lone-fragment aggregate requests.

    Idempotency: re-running on byte-identical input yields a tree of
    identical shape because both operators are deterministic and the
    classifier-driven branches collapse to the same shape when the
    fragments are unchanged.

    Args:
        fragment: Root fragment to classify.
        classifier: Callable returning ``(classified, confidence)``.
        config: Per-run knobs.
        _depth: Internal recursion counter; callers should not pass.

    Returns:
        A :class:`ClassificationTree` rooted at ``fragment``.
    """
    classified, confidence = classifier(fragment)

    if not config.enabled:
        return _leaf(classified, confidence, "disabled", _depth)
    if _is_accepted(classified.fragment, confidence, config.threshold):
        return _leaf(classified, confidence, "accepted", _depth)
    if classified.fragment.level in _TERMINAL_LEVELS:
        return _leaf(classified, confidence, "terminal", _depth)
    if _depth >= config.max_depth:
        return _leaf(classified, confidence, "max_depth", _depth)

    direction = choose_direction(classified.fragment, config.direction)
    if direction == "aggregate":
        return _leaf(classified, confidence, "aggregate_no_siblings", _depth)
    return _zoom_in(classified, confidence, classifier, config, _depth)


def classify_reatomize_stream(
    fragments: list[IngestedFragment],
    classifier: Classifier,
    *,
    config: ReatomizeConfig,
) -> list[ClassificationTree]:
    """Batch entry point that supports zoom-out via FEAT-022 aggregation.

    For chat-style inputs (short, low-content fragments) the single-
    fragment API cannot aggregate — there are no siblings to combine
    with. This helper accepts the full stream, classifies each member,
    and when zoom-out is the chosen direction for the weak members,
    rolls them up via FEAT-022, classifies the parents, and returns
    trees whose ``children`` are the original (now-classified) leaves.

    When no fragment is weak, every input becomes an ``accepted``
    single-node tree. When the direction comes back as ``split``, the
    stream falls through to the single-fragment orchestrator on each
    input, preserving FEAT-023's zoom-in algorithm verbatim.

    Args:
        fragments: Stream to classify. The aggregator groups by
            :class:`creek.models.FragmentSource` key per FEAT-022.
        classifier: Same callable as :func:`classify_reatomize`.
        config: Per-run knobs.

    Returns:
        A list of :class:`ClassificationTree` — one per parent
        produced (or per pass-through fragment when zoom-out wasn't
        triggered).
    """
    if not fragments:
        return []

    classified_leaves = [classifier(frag) for frag in fragments]
    return _route_stream(fragments, classified_leaves, classifier, config)


def choose_direction(
    fragment: Fragment,
    override: DirectionOverride = "auto",
) -> Direction:
    """Pick a re-atomization direction for ``fragment``.

    Honours an explicit ``override`` first, then routes by
    ``source.platform`` and structural ``level`` per the FEAT-023
    heuristic: chat sources (``discord`` / ``claude`` / ``chatgpt``)
    at a chat-level (``document`` = message, ``exchange``) zoom
    **out** to stitch sparse context into the level where meaning
    lives; every other combination — document sources at top levels,
    finer carved levels at any source, the unknown-platform fallback —
    zooms **in**. Aggregate without a clear chat-stream signal risks
    combining unrelated material; ``split`` can at worst return no
    children and become an honest leaf.

    Args:
        fragment: Fragment whose source + level drive the heuristic.
        override: ``"auto"`` (use heuristic) or one of
            ``"split"`` / ``"aggregate"`` to bypass it.

    Returns:
        ``"split"`` or ``"aggregate"``.
    """
    if override != "auto":
        return override

    # FragmentSource uses ``use_enum_values=True``, so ``platform`` is the
    # enum's string value at this point — feed it back through the enum
    # to compare set-membership without leaning on string literals.
    platform = SourcePlatform(fragment.source.platform)
    level = fragment.level

    if platform in _CHAT_PLATFORMS and level in {"document", "exchange"}:
        return "aggregate"
    # Document-source top levels, paragraph/subsection at any source, and
    # the unknown-platform fallback all default to ``split``. Aggregate
    # without a clear chat-stream signal risks combining unrelated
    # material; ``split`` can at worst return no children and become an
    # honest leaf.
    return "split"


# ---- Internals --------------------------------------------------------------


def _leaf(
    fragment: IngestedFragment,
    confidence: float,
    stop_reason: StopReason,
    depth: int,
) -> ClassificationTree:
    """Build a leaf node — exists to keep the ``classify_reatomize`` body terse."""
    return ClassificationTree(
        fragment=fragment,
        confidence=confidence,
        stop_reason=stop_reason,
        depth=depth,
    )


def _route_stream(
    fragments: list[IngestedFragment],
    classified_leaves: list[tuple[IngestedFragment, float]],
    classifier: Classifier,
    config: ReatomizeConfig,
) -> list[ClassificationTree]:
    """Pick a stream-handling strategy after the initial pass.

    Factored out of :func:`classify_reatomize_stream` so neither
    function exceeds the per-function complexity ceiling (Xenon B).
    """
    if not config.enabled:
        return [_leaf(cl, conf, "disabled", 0) for cl, conf in classified_leaves]

    weak = [
        (cl, conf)
        for cl, conf in classified_leaves
        if not _is_accepted(cl.fragment, conf, config.threshold)
    ]
    if not weak:
        return [_leaf(cl, conf, "accepted", 0) for cl, conf in classified_leaves]

    # Pin the direction off the first weak fragment. The stream API
    # assumes a homogeneous source (one Discord conversation, one
    # document corpus) — the FEAT-022 aggregator itself groups by
    # FragmentSource key, so mixed streams already split cleanly at
    # that layer. A caller mixing chat + document sources in the same
    # batch should either segment up-front or invoke
    # :func:`classify_reatomize` per fragment.
    direction = choose_direction(weak[0][0].fragment, config.direction)
    if direction != "aggregate":
        return [
            classify_reatomize(frag, classifier, config=config) for frag in fragments
        ]
    return _zoom_out_stream(classified_leaves, classifier, config)


def _is_accepted(fragment: Fragment, confidence: float, threshold: float) -> bool:
    """Return ``True`` when ``fragment`` clears the FEAT-023 accept bar.

    A fragment is accepted iff every required dimension is classified
    (frequency primary + wavelength phase — the two dimensions the
    pipeline downstream depends on most) AND the per-fragment score
    meets ``threshold``. Other dimensions (mode, orientation, dosage)
    may remain ``unclassified``; they are governed by the LLM's own
    per-dimension floor (see
    :attr:`creek.config.LLMConfig.unclassified_threshold`), and forcing
    them through re-atomization would over-fire.
    """
    if confidence < threshold:
        return False
    if fragment.frequency.primary == Frequency.UNCLASSIFIED.value:
        return False
    return fragment.wavelength.phase != Phase.UNCLASSIFIED.value


def _zoom_in(
    classified: IngestedFragment,
    confidence: float,
    classifier: Classifier,
    config: ReatomizeConfig,
    depth: int,
) -> ClassificationTree:
    """Run the FEAT-021 splitter and recurse on every child."""
    children = split(classified, sentence_tokenizer=config.sentence_tokenizer)
    if not children:
        return _leaf(classified, confidence, "no_decomposition", depth)
    sub_trees = tuple(
        classify_reatomize(child, classifier, config=config, _depth=depth + 1)
        for child in children
    )
    return ClassificationTree(
        fragment=classified,
        confidence=confidence,
        stop_reason="split",
        children=sub_trees,
        depth=depth,
    )


def _zoom_out_stream(
    classified_leaves: list[tuple[IngestedFragment, float]],
    classifier: Classifier,
    config: ReatomizeConfig,
) -> list[ClassificationTree]:
    """Aggregate the stream up one level and classify each parent.

    The aggregator (FEAT-022) is fragment-centric. We strip the bodies,
    ask it to roll the stream to the next coarser level, and rebuild
    :class:`IngestedFragment` envelopes whose bodies are the joiner-
    concatenated child bodies. That keeps the parent classifier-readable
    without re-implementing FEAT-022's joining rules.
    """
    base_level = classified_leaves[0][0].fragment.level
    target = _AGGREGATE_NEXT_LEVEL.get(base_level)
    if target is None:
        return [_leaf(cl, conf, "terminal", 0) for cl, conf in classified_leaves]

    leaf_lookup: dict[str, tuple[IngestedFragment, float]] = {
        cl.fragment.id: (cl, conf) for cl, conf in classified_leaves
    }
    parents = aggregate(
        [cl.fragment for cl, _ in classified_leaves],
        level=target,
        config=config.aggregation_config,
    )
    return [
        _tree_for_parent(parent, target, leaf_lookup, classifier, config)
        for parent in parents
    ]


def _tree_for_parent(
    parent: Fragment,
    target: AggregateLevel,
    leaf_lookup: dict[str, tuple[IngestedFragment, float]],
    classifier: Classifier,
    config: ReatomizeConfig,
) -> ClassificationTree:
    """Classify ``parent`` and wrap its leaves as a sub-tree."""
    if parent.level != target:
        # Pass-through: FEAT-022 declined to aggregate this fragment
        # (wrong source level for the target transition). Distinguish
        # the "classifier was confident" case from "we did nothing
        # further" so a downstream gate on ``stop_reason == "accepted"``
        # doesn't pick up weak fragments by accident.
        cl, conf = leaf_lookup[parent.id]
        reason: StopReason = (
            "accepted"
            if _is_accepted(cl.fragment, conf, config.threshold)
            else "passthrough"
        )
        return _leaf(cl, conf, reason, 0)

    body = config.aggregation_config.joiner.join(
        leaf_lookup[c_id][0].body for c_id in parent.child_ids
    )
    parent_ingested = IngestedFragment(fragment=parent, body=body)
    classified_parent, parent_conf = classifier(parent_ingested)
    child_trees = tuple(
        ClassificationTree(
            fragment=leaf_lookup[c_id][0],
            confidence=leaf_lookup[c_id][1],
            stop_reason="terminal",
            depth=1,
        )
        for c_id in parent.child_ids
    )
    # Children are non-empty here, so ``no_decomposition`` (defined as
    # "operator returned no children") would be a lie. Use the dedicated
    # ``aggregated_weak`` for the unconfident-parent case instead.
    stop: StopReason = (
        "aggregated"
        if _is_accepted(classified_parent.fragment, parent_conf, config.threshold)
        else "aggregated_weak"
    )
    return ClassificationTree(
        fragment=classified_parent,
        confidence=parent_conf,
        stop_reason=stop,
        children=child_trees,
    )


# ---- Weighted bubble-up ----------------------------------------------------


def bubble_up_weighted(tree: ClassificationTree) -> ClassificationTree:
    """Recursively roll up children's weighted classifications onto parents.

    Walks the tree depth-first, combining each non-leaf node's
    children via :func:`creek.classify.holonic.combine` (which
    enforces the conviction x confidence contract — length is never
    the default mass) and stamping the combined
    :class:`~creek.classify.weighted.WeightedFragmentClassification`
    onto the parent fragment's :attr:`weighted` field. Leaves flow
    through unchanged.

    Children whose :attr:`Fragment.weighted` is ``None`` are excluded
    from the combine; they do not pollute the parent's profile. When
    every child's weighted is ``None``, the parent's weighted stays
    ``None`` too (honest "no signal" rather than fabricating one).

    Args:
        tree: The classification tree produced by
            :func:`classify_reatomize`. Each leaf is expected to
            already carry a :attr:`Fragment.weighted` populated by
            the LLM classifier; missing ``weighted`` is treated as
            "no signal" and skipped.

    Returns:
        A new tree with every internal node's
        :attr:`Fragment.weighted` derived from its children's
        weighted profiles. Leaves are returned unchanged.
    """
    # Local import dodges the cycle that would otherwise form via
    # creek.models -> creek.classify.weighted -> creek.classify.holonic
    # at module load; reatomize itself is loaded after models so this is fine.
    from creek.classify.holonic import combine

    if not tree.children:
        return tree

    bubbled_children = tuple(bubble_up_weighted(child) for child in tree.children)

    weighted_inputs = [
        child.fragment.fragment.weighted
        for child in bubbled_children
        if child.fragment.fragment.weighted is not None
    ]
    if not weighted_inputs:
        # No contributing children — leave the parent's ``weighted``
        # untouched (honest "no signal").
        return ClassificationTree(
            fragment=tree.fragment,
            confidence=tree.confidence,
            stop_reason=tree.stop_reason,
            children=bubbled_children,
            depth=tree.depth,
        )

    combined = combine(weighted_inputs)
    new_parent_fragment = tree.fragment.fragment.model_copy(
        update={"weighted": combined},
    )
    new_parent_ingested = IngestedFragment(
        fragment=new_parent_fragment,
        body=tree.fragment.body,
    )
    return ClassificationTree(
        fragment=new_parent_ingested,
        confidence=tree.confidence,
        stop_reason=tree.stop_reason,
        children=bubbled_children,
        depth=tree.depth,
    )


def choose_intelligent_unit(
    tree: ClassificationTree,
    *,
    threshold: float = 0.6,
    material_delta: float = 0.05,
) -> ClassificationTree:
    """Pick the holarchy level whose weighted profile is the right grain.

    Walks the (bubbled) tree and surfaces the deepest descendant
    whose ``Fragment.weighted.overall_confidence`` reaches
    ``threshold`` and whose immediate ancestor does not improve
    confidence by more than ``material_delta``. Falls back to the
    root when no descendant clears the bar — the operator still gets
    a usable answer, just at coarser grain.

    Args:
        tree: A bubbled :class:`ClassificationTree`. Should be the
            output of :func:`bubble_up_weighted`; raw orchestrator
            output without bubbling will return the root since no
            parent has a ``weighted`` set.
        threshold: Confidence floor — a node must reach this to be
            considered a candidate.
        material_delta: A child's confidence improves on its parent
            "materially" when the parent's confidence is at most
            ``material_delta`` higher. When the parent fails this
            test, the child is preferred as a finer-grain unit.

    Returns:
        The chosen :class:`ClassificationTree` node. The caller can
        compare ``chosen.depth`` against ``tree.depth`` to learn the
        recommended grain.
    """
    return _choose_intelligent_unit_recursive(
        tree,
        threshold=threshold,
        material_delta=material_delta,
    )


def _choose_intelligent_unit_recursive(
    node: ClassificationTree,
    *,
    threshold: float,
    material_delta: float,
) -> ClassificationTree:
    """Recursion body for :func:`choose_intelligent_unit`.

    Considers ``node`` plus the result of recursing into each child.
    When a child's confidence reaches the threshold and the parent
    does not materially improve on the child, the child wins. Ties
    (no child clears) fall back to ``node`` itself.
    """
    node_confidence = _node_confidence_or_zero(node)

    best_descendant: ClassificationTree | None = None
    for child in node.children:
        descendant = _choose_intelligent_unit_recursive(
            child,
            threshold=threshold,
            material_delta=material_delta,
        )
        desc_confidence = _node_confidence_or_zero(descendant)
        if desc_confidence < threshold:
            continue
        if node_confidence - desc_confidence > material_delta:
            # Parent materially outperforms — prefer the parent
            # (this iteration), so do not promote the descendant.
            continue
        if best_descendant is None or desc_confidence > _node_confidence_or_zero(
            best_descendant
        ):
            best_descendant = descendant

    if best_descendant is not None:
        return best_descendant
    return node


def _node_confidence_or_zero(node: ClassificationTree) -> float:
    """Return ``node``'s weighted overall_confidence, defaulting to 0.0."""
    weighted = node.fragment.fragment.weighted
    if weighted is None:
        return 0.0
    return weighted.overall_confidence
