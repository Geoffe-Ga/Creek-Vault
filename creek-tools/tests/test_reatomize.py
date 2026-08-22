"""Tests for the FEAT-023 confidence-driven re-atomization orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest

from creek.classify.reatomize import (
    ClassificationTree,
    Classifier,
    Direction,
    DirectionOverride,
    ReatomizeConfig,
    StopReason,
    choose_direction,
    classify_reatomize,
)
from creek.config import ClassificationConfig
from creek.ingest.base import IngestedFragment
from creek.models import (
    Authorship,
    Fragment,
    FragmentLevel,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)

# ---- Helpers ---------------------------------------------------------------


def _make_ingested(
    *,
    body: str = "",
    level: FragmentLevel = "document",
    platform: SourcePlatform = SourcePlatform.MARKDOWN,
    title: str = "Root doc",
    frag_id: str = "frag-rootroot0000",
    minute: int = 0,
    interlocutor: str | None = None,
    author: Authorship = Authorship.SELF,
) -> IngestedFragment:
    """Construct an :class:`IngestedFragment` for orchestrator tests."""
    when = datetime(2025, 5, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)
    fragment = Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(
            platform=platform,
            interlocutor=interlocutor,
            channel="general",
            conversation_id="conv-1",
            author=author,
        ),
        created=when,
        level=level,
    )
    return IngestedFragment(fragment=fragment, body=body)


def _classified(
    fragment: Fragment,
    *,
    frequency: Frequency = Frequency.F5,
    phase: Phase = Phase.RISING,
) -> Fragment:
    """Return a copy of ``fragment`` with the given primary axes set."""
    return fragment.model_copy(
        update={
            "frequency": FrequencyClassification(primary=frequency),
            "wavelength": WavelengthClassification(phase=phase),
        },
    )


def _accepting_classifier(confidence: float = 0.95) -> Classifier:
    """A classifier that always confidently classifies its input."""

    def _classify(ingested: IngestedFragment) -> tuple[IngestedFragment, float]:
        return (
            IngestedFragment(
                fragment=_classified(ingested.fragment),
                body=ingested.body,
            ),
            confidence,
        )

    return _classify


def _rejecting_classifier(confidence: float = 0.2) -> Classifier:
    """A classifier that returns ``unclassified`` results below threshold."""

    def _classify(ingested: IngestedFragment) -> tuple[IngestedFragment, float]:
        return ingested, confidence

    return _classify


# ---- ReatomizeConfig defaults & projection ---------------------------------


class TestReatomizeConfig:
    """Defaults, factory, and projection from :class:`ClassificationConfig`."""

    def test_defaults_mirror_spec(self) -> None:
        """Defaults match the FEAT-023 spec (disabled / 0.7 / 4 / auto)."""
        cfg = ReatomizeConfig()
        assert cfg.enabled is False
        assert cfg.threshold == pytest.approx(0.7)
        assert cfg.max_depth == 4
        assert cfg.direction == "auto"

    def test_from_classification_config_inherits_confidence_threshold(self) -> None:
        """A missing reatomize_threshold falls back to confidence_threshold."""
        cls_cfg = ClassificationConfig(confidence_threshold=0.8, reatomize=True)
        cfg = ReatomizeConfig.from_classification_config(cls_cfg)
        assert cfg.threshold == pytest.approx(0.8)
        assert cfg.enabled is True

    def test_from_classification_config_uses_explicit_reatomize_threshold(
        self,
    ) -> None:
        """An explicit reatomize_threshold overrides confidence_threshold."""
        cls_cfg = ClassificationConfig(
            confidence_threshold=0.8,
            reatomize_threshold=0.4,
        )
        cfg = ReatomizeConfig.from_classification_config(cls_cfg)
        assert cfg.threshold == pytest.approx(0.4)


# ---- Re-atomization vocabulary ---------------------------------------------


class TestReatomizeVocabulary:
    """The ``Direction`` / ``StopReason`` token sets are a public contract."""

    def test_stop_reason_tokens_are_exactly_the_seven_live_reasons(self) -> None:
        """Every ``StopReason`` token must have a producer in the orchestrator.

        The vocabulary is pinned literally, not derived, because a stray
        token is precisely how ``aggregated_weak`` outlived its producer:
        the FEAT-022 zoom-out aggregator was retired (ADR-0011, issue
        #1342) but its stop reasons stayed in the ``Literal``, where no
        type checker, linter or coverage gate could notice that nothing
        emits them any more. Downstream consumers gate dashboards and
        export queues on this string, so a phantom token is a live
        promise the pipeline cannot keep.
        """
        assert set(get_args(StopReason)) == {
            "accepted",
            "terminal",
            "max_depth",
            "disabled",
            "no_decomposition",
            "no_operator",
            "split",
        }

    def test_direction_offers_only_split_and_none(self) -> None:
        """``choose_direction`` may route to the splitter or to nothing at all."""
        assert set(get_args(Direction)) == {"split", "none"}

    def test_direction_override_offers_only_auto_and_split(self) -> None:
        """The only operator-selectable direction left is the zoom-in splitter."""
        assert set(get_args(DirectionOverride)) == {"auto", "split"}


# ---- Direction heuristic ---------------------------------------------------


class TestChooseDirection:
    """The FEAT-023 direction-choice heuristic on (platform, level)."""

    @pytest.mark.parametrize(
        ("platform", "level", "expected"),
        [
            # Chat sources at chat-level: no operator applies. Zoom-out
            # was retired with FEAT-022 (ADR-0011, issue #1342) and
            # splitting a lone chat message is not what the heuristic
            # ever meant, so the honest answer is "none".
            (SourcePlatform.DISCORD, "document", "none"),
            (SourcePlatform.CLAUDE, "document", "none"),
            (SourcePlatform.CHATGPT, "exchange", "none"),
            # Document sources at top-level: zoom in.
            (SourcePlatform.MARKDOWN, "document", "split"),
            (SourcePlatform.ESSAY, "section", "split"),
            (SourcePlatform.CODE, "document", "split"),
            # Finer carved levels → always zoom in.
            (SourcePlatform.DISCORD, "paragraph", "split"),
            (SourcePlatform.MARKDOWN, "subsection", "split"),
            # Unknown platform → conservative zoom in.
            (SourcePlatform.OTHER, "document", "split"),
        ],
    )
    def test_heuristic_routes_by_platform_and_level(
        self,
        platform: SourcePlatform,
        level: FragmentLevel,
        expected: str,
    ) -> None:
        """The (platform, level) pair alone decides the direction.

        Args:
            platform: Source platform stamped on the fragment.
            level: Structural level stamped on the fragment.
            expected: Direction the heuristic must return.
        """
        ingested = _make_ingested(platform=platform, level=level)
        assert choose_direction(ingested.fragment) == expected

    @pytest.mark.parametrize(
        ("override", "platform"),
        [("split", SourcePlatform.DISCORD)],
    )
    def test_explicit_override_wins(
        self,
        override: DirectionOverride,
        platform: SourcePlatform,
    ) -> None:
        """Non-``auto`` overrides bypass the heuristic entirely.

        Args:
            override: Explicit direction the caller forces.
            platform: A platform whose heuristic answer differs from
                ``override``, so a pass proves the override was honoured.
        """
        ingested = _make_ingested(platform=platform, level="document")
        assert choose_direction(ingested.fragment, override) == override


# ---- Disabled path ---------------------------------------------------------


class TestDisabled:
    """When ``enabled=False`` the orchestrator runs a single pass."""

    def test_returns_disabled_leaf(self) -> None:
        ingested = _make_ingested(body="some body", level="document")
        tree = classify_reatomize(
            ingested,
            _accepting_classifier(),
            config=ReatomizeConfig(enabled=False),
        )
        assert tree.stop_reason == "disabled"
        assert tree.children == ()
        assert tree.depth == 0

    def test_disabled_does_not_split_even_when_below_threshold(self) -> None:
        body = "# H1\n\nfirst para.\n\nsecond para."
        ingested = _make_ingested(body=body, level="document")
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=False, threshold=0.9),
        )
        assert tree.stop_reason == "disabled"
        assert tree.children == ()


# ---- Acceptance path -------------------------------------------------------


class TestAccepted:
    """Confidence ≥ threshold and required dimensions set → leaf accepted."""

    def test_accepts_when_classifier_returns_confident(self) -> None:
        ingested = _make_ingested(body="anything", level="document")
        tree = classify_reatomize(
            ingested,
            _accepting_classifier(0.95),
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert tree.stop_reason == "accepted"
        assert tree.confidence == pytest.approx(0.95)
        assert tree.children == ()

    def test_rejects_when_required_dimension_unclassified(self) -> None:
        """Even a high score doesn't accept if frequency.primary is unset."""

        def _classifier(ig: IngestedFragment) -> tuple[IngestedFragment, float]:
            # High confidence but frequency stays UNCLASSIFIED.
            return ig, 0.99

        body = "Paragraph one.\n\nParagraph two."
        ingested = _make_ingested(body=body, level="document")
        tree = classify_reatomize(
            ingested,
            _classifier,
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert tree.stop_reason == "split"
        assert len(tree.children) >= 1


# ---- Stopping conditions ---------------------------------------------------


class TestTerminalLevels:
    """Sentence and session are terminal — orchestrator never decomposes them."""

    @pytest.mark.parametrize("level", ["sentence", "session"])
    def test_terminal_levels_stop(self, level: FragmentLevel) -> None:
        ingested = _make_ingested(body="x", level=level)
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9),
        )
        assert tree.stop_reason == "terminal"
        assert tree.children == ()


class TestMaxDepth:
    """Recursion stops at the configured ``max_depth``."""

    def test_max_depth_zero_short_circuits_immediately(self) -> None:
        body = "# H1\n\nfirst.\n\nsecond."
        ingested = _make_ingested(body=body, level="document")
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9, max_depth=0),
        )
        assert tree.stop_reason == "max_depth"
        assert tree.children == ()

    def test_max_depth_bounds_recursion(self) -> None:
        """At max_depth=1, root may split but its children must not."""
        body = "# A\n\npara one.\n\npara two.\n\n# B\n\npara three.\n\npara four."
        ingested = _make_ingested(body=body, level="document")
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9, max_depth=1),
        )
        assert tree.stop_reason == "split"
        assert tree.children
        for child in tree.children:
            assert child.stop_reason in {"max_depth", "no_decomposition", "terminal"}
            assert child.children == ()


class TestNoDecomposition:
    """A splitter that returns no children produces an honest leaf."""

    def test_paragraph_with_one_sentence_yields_no_decomposition(self) -> None:
        ingested = _make_ingested(body="Just one sentence.", level="paragraph")
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9),
        )
        assert tree.stop_reason == "no_decomposition"
        assert tree.children == ()


class TestNoOperator:
    """A chat message at chat level has no operator left; it stops honestly."""

    def test_lone_chat_message_records_no_operator(self) -> None:
        """A weak Discord ``document`` becomes a ``no_operator`` leaf.

        Zoom-out was the only operator this branch ever had, and FEAT-022
        was retired by ADR-0011 (issue #1342). The orchestrator must
        therefore say so in one word — ``no_operator`` — rather than
        borrowing a reason that implies work was attempted. The whole
        leaf contract is pinned (reason, no children, root depth, and
        that the returned fragment is the classified *input*, not a
        substitute), because a downstream export queue reads all four.
        """
        ingested = _make_ingested(
            body="hi",
            level="document",
            platform=SourcePlatform.DISCORD,
            frag_id="frag-lonechat001",
        )
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9),
        )
        assert tree.stop_reason == "no_operator"
        assert tree.children == ()
        assert tree.depth == 0
        assert tree.fragment.fragment.id == "frag-lonechat001"


# ---- Zoom-in integration ---------------------------------------------------


class TestZoomInIntegration:
    """Polyphonic document → confidently-classified leaves."""

    @staticmethod
    def _polyphonic_doc() -> IngestedFragment:
        body = (
            "# Section on Power\n\n"
            "power dominance control conquest force aggression "
            "bold fearless warrior rage impulsive rebellion\n\n"
            "# Section on Order\n\n"
            "order discipline rules duty authority morality "
            "structure hierarchy obedience law\n"
        )
        return _make_ingested(
            body=body,
            level="document",
            platform=SourcePlatform.ESSAY,
            title="Polyphonic essay",
        )

    @staticmethod
    def _mock_section_classifier() -> Classifier:
        """LLM mock: roots stay unclassified, sections classify by heading."""

        def _classify(ig: IngestedFragment) -> tuple[IngestedFragment, float]:
            level = ig.fragment.level
            title = ig.fragment.title.lower()
            if level == "document":
                return ig, 0.2
            if "power" in title:
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F3,
                            phase=Phase.PEAKING,
                        ),
                        body=ig.body,
                    ),
                    0.92,
                )
            if "order" in title:
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F4,
                            phase=Phase.WITHDRAWAL,
                        ),
                        body=ig.body,
                    ),
                    0.88,
                )
            return ig, 0.1

        return _classify

    def test_polyphonic_document_yields_classified_leaves(self) -> None:
        tree = classify_reatomize(
            self._polyphonic_doc(),
            self._mock_section_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert tree.stop_reason == "split"
        leaves = _collect_leaves(tree)
        assert len(leaves) == 2
        accepted = [leaf for leaf in leaves if leaf.stop_reason == "accepted"]
        assert len(accepted) == 2
        primaries = {leaf.fragment.fragment.frequency.primary for leaf in accepted}
        assert primaries == {Frequency.F3.value, Frequency.F4.value}

    def test_idempotent_reruns_yield_identical_tree(self) -> None:
        """Acceptance criterion: re-running produces no duplicate children."""
        classifier = self._mock_section_classifier()
        cfg = ReatomizeConfig(enabled=True, threshold=0.7)
        first = classify_reatomize(self._polyphonic_doc(), classifier, config=cfg)
        second = classify_reatomize(self._polyphonic_doc(), classifier, config=cfg)
        assert _shape_signature(first) == _shape_signature(second)
        assert _ids(first) == _ids(second)


# ---- Walk helpers ----------------------------------------------------------


def _collect_leaves(tree: ClassificationTree) -> list[ClassificationTree]:
    if not tree.children:
        return [tree]
    leaves: list[ClassificationTree] = []
    for child in tree.children:
        leaves.extend(_collect_leaves(child))
    return leaves


def _shape_signature(tree: ClassificationTree) -> tuple[object, ...]:
    return (
        tree.fragment.fragment.id,
        tree.stop_reason,
        tuple(_shape_signature(c) for c in tree.children),
    )


def _ids(tree: ClassificationTree) -> list[str]:
    out = [tree.fragment.fragment.id]
    for child in tree.children:
        out.extend(_ids(child))
    return out
