"""Confidence-driven re-atomization orchestrator (FEAT-023).

Drives the FEAT-021 zoom-in splitter: when a classifier returns
``unclassified`` or scores below the configured confidence floor, this
orchestrator decides whether carving the unit finer can help, splits
it, classifies the new units, and recurses. Recursion stops when a unit
clears the threshold, hits a terminal level (``sentence`` or
``session``), reaches the per-config max-depth ceiling, or lands on a
fragment the splitter is the wrong tool for.

That last case is what the zoom-out twin left behind. Chat messages at
message/exchange grain were once meant to be stitched into coarser
parents by FEAT-022; issue #1342 retired that operator (ADR-0011)
because no production path ever invoked it, so those fragments now stop
with ``no_operator`` — see :data:`StopReason`. Issue #1457 tracks
bringing coarsening back together with a caller that uses it.

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
]

Direction = Literal["split", "none"]
DirectionOverride = Literal["auto", "split"]

StopReason = Literal[
    "accepted",
    "terminal",
    "max_depth",
    "disabled",
    "no_decomposition",
    "no_operator",
    "split",
]
"""Why the orchestrator stopped recursing on a given subtree.

Every token here must have a producer a production run can actually
reach. That is a standing requirement, not an observation: downstream
dashboards and export queues gate on this string, so a token nothing
emits is a promise the pipeline cannot keep. Three tokens named
zoom-out outcomes whose only emitters were themselves uncalled; issue
#1342 removed them along with the operator.

Leaf reasons (``children`` is empty):

``accepted``: classifier cleared :attr:`ReatomizeConfig.threshold`.
``terminal``: at a level the splitter refuses to decompose
(sentence / session).
``max_depth``: hit :attr:`ReatomizeConfig.max_depth`.
``disabled``: the run was opt-out; no recursion attempted.
``no_decomposition``: the splitter ran and returned no children.
``no_operator``: :func:`choose_direction` answered ``"none"`` — the
fragment's source and level call for zoom-out, and Creek has no
zoom-out operator (retired, issue #1342). An honest leaf, not a
failure: nothing was attempted, because nothing could be. This is the
only producer of the reason, and ``"none"`` is its only cause, so the
two names move together or not at all.

Internal reasons (``children`` is non-empty):

``split``: parent was decomposed by the FEAT-021 zoom-in splitter.
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


@dataclass(frozen=True)
class ClassificationTree:
    """Recursive output of :func:`classify_reatomize`.

    Each node carries the classified fragment, the per-node confidence
    score, the reason recursion stopped (or ``"split"`` when the node
    has children), and the sub-trees produced.
    ``frozen=True`` so callers can hash or set-membership-test nodes
    without surprise.

    Attributes:
        fragment: Classified fragment (frontmatter + body).
        confidence: Score the classifier returned for this fragment.
        stop_reason: Why recursion ended at this node, or how it
            continued (``split``) when ``children`` is non-empty.
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
        direction: ``"auto"`` (heuristic) or ``"split"`` — overrides
            :func:`choose_direction`.
        sentence_tokenizer: Forwarded into the FEAT-021 splitter for
            paragraph→sentence transitions.
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

    @classmethod
    def from_classification_config(
        cls,
        config: ClassificationConfig,
    ) -> ReatomizeConfig:
        """Project the FEAT-023 knobs out of a :class:`ClassificationConfig`.

        Centralises the ``reatomize_threshold is None`` fallback so
        the CLI and tests don't each re-derive it.

        Args:
            config: Loaded vault classification config.

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
    3. Otherwise ask :func:`choose_direction` whether the splitter
       applies (``split``) or no operator does (``none``); on ``split``,
       decompose and recurse on the children.
    4. Stop on terminal levels, ``max_depth``, empty decomposition, or
       a fragment no surviving operator fits (``no_operator``).

    Idempotency: re-running on byte-identical input yields a tree of
    identical shape because the splitter is deterministic and the
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
    if direction == "none":
        return _leaf(classified, confidence, "no_operator", _depth)
    return _zoom_in(classified, confidence, classifier, config, _depth)


def choose_direction(
    fragment: Fragment,
    override: DirectionOverride = "auto",
) -> Direction:
    """Pick a re-atomization direction for ``fragment``.

    Honours an explicit ``override`` first, then routes by
    ``source.platform`` and structural ``level`` per the FEAT-023
    heuristic: chat sources (``discord`` / ``claude`` / ``chatgpt``) at
    a chat-level (``document`` = message, ``exchange``) get ``"none"``;
    every other combination — document sources at top levels, finer
    carved levels at any source, the unknown-platform fallback — zooms
    **in** with ``"split"``.

    Why ``"none"`` rather than falling through to ``"split"``: the
    chat-level branch was the zoom-out branch, and the FEAT-022
    zoom-out operator was retired by issue #1342 (ADR-0011) for having
    no production caller. Creek now has no operator for that case, and
    saying so is the behaviour-preserving answer. Splitting instead
    would change what the pipeline does on real vaults in two ways: a
    chat one-liner carves into nothing, which is merely a differently
    named leaf; but a long chat turn carves into paragraphs, and every
    one of those children is a new file the vault did not have before.
    Where the retired branch persisted nothing, ``"split"`` would start
    minting fragments. ``"none"`` keeps the fragment exactly as it is
    and lets the caller record an honest ``no_operator`` leaf.

    Args:
        fragment: Fragment whose source + level drive the heuristic.
        override: ``"auto"`` (use heuristic) or ``"split"`` to bypass
            it. There is no override for ``"none"``: it is a statement
            about which operators exist, not a strategy to select.

    Returns:
        ``"split"`` or ``"none"``.
    """
    if override != "auto":
        return override

    # FragmentSource uses ``use_enum_values=True``, so ``platform`` is the
    # enum's string value at this point — feed it back through the enum
    # to compare set-membership without leaning on string literals.
    platform = SourcePlatform(fragment.source.platform)
    level = fragment.level

    if platform in _CHAT_PLATFORMS and level in {"document", "exchange"}:
        return "none"
    # Document-source top levels, paragraph/subsection at any source, and
    # the unknown-platform fallback all default to ``split``: carving
    # material that really is carvable can at worst return no children
    # and become an honest leaf.
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

    **Provenance: an inherited dimension counts, and that is deliberate
    (issue #1422).** A split child is a ``parent.model_copy`` overriding
    only identity and structure (``creek/atomize/split.py``), so both
    required dimensions usually arrive already populated, from the
    parent, before any classifier reads the child's own text. This
    predicate cannot tell that apart from a value the pass just derived,
    and it is not being taught to. Three reasons, in the order they
    decided it:

    1. **The bar is a conjunction, and its other half is
       child-specific.** The score comes from
       :meth:`~creek.classify.rules.RuleClassifier.confidence_score`,
       which reads the fragment's title and body and nothing stored on
       it. A child that understood nothing of itself scores low and is
       rejected however complete its inherited block looks, so
       provenance-blindness here does not make acceptance
       evidence-free.
    2. **Inheritance is evidence, just not evidence derived from the
       child.** Phase and primary frequency describe the register a
       passage was written in; a paragraph carved out of a rising-phase
       document is, absent a signal of its own, still rising-phase.
    3. **Requiring a derived value would invert the operator's intent.**
       The rule pass derives phase only from keyword hits, which short
       child atoms routinely lack, so a derived-only bar keeps splitting
       until the splitter itself runs out of material. Measured on a
       fixed three-paragraph document in
       ``tests/test_reatomize.py::TestInheritedDimensionsBoundRecursion``
       — everything held constant but this rule — one accepted node
       becomes six, at depth two, and every leaf stops on
       ``no_decomposition`` rather than on anything learned about the
       text. The recursion buys no information and costs a 6x fragment
       count.

    Tracking provenance would need a marker that survives a whole-fragment
    ``model_copy``, i.e. a new persisted ``Fragment`` field and its
    frontmatter round-trip, spent on a knob that is off by default
    (``ClassificationConfig.reatomize``). The decision above means no
    such mechanism is required, and none is added.
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
