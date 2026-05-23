"""Tests for the FEAT-023 confidence-driven re-atomization orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creek.atomize.aggregate import AggregationConfig
from creek.classify.reatomize import (
    ClassificationTree,
    Classifier,
    ReatomizeConfig,
    choose_direction,
    classify_reatomize,
    classify_reatomize_stream,
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

    def test_from_classification_config_accepts_aggregation_override(self) -> None:
        """Callers can wire a tuned AggregationConfig for burst runs."""
        agg = AggregationConfig(burst_similarity_threshold=0.9)
        cls_cfg = ClassificationConfig()
        cfg = ReatomizeConfig.from_classification_config(
            cls_cfg,
            aggregation_config=agg,
        )
        assert cfg.aggregation_config is agg


# ---- Direction heuristic ---------------------------------------------------


class TestChooseDirection:
    """The FEAT-023 direction-choice heuristic on (platform, level)."""

    @pytest.mark.parametrize(
        ("platform", "level", "expected"),
        [
            # Chat sources at chat-level: zoom out.
            (SourcePlatform.DISCORD, "document", "aggregate"),
            (SourcePlatform.CLAUDE, "document", "aggregate"),
            (SourcePlatform.CHATGPT, "exchange", "aggregate"),
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
        ingested = _make_ingested(platform=platform, level=level)
        assert choose_direction(ingested.fragment) == expected

    @pytest.mark.parametrize(
        ("override", "platform"),
        [("split", SourcePlatform.DISCORD), ("aggregate", SourcePlatform.MARKDOWN)],
    )
    def test_explicit_override_wins(
        self,
        override: str,
        platform: SourcePlatform,
    ) -> None:
        """Non-``auto`` overrides bypass the heuristic entirely."""
        ingested = _make_ingested(platform=platform, level="document")
        assert choose_direction(ingested.fragment, override) == override  # type: ignore[arg-type]


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

    def test_stream_disabled_returns_per_input_leaves(self) -> None:
        ingested = [
            _make_ingested(
                frag_id="frag-a",
                body="hi",
                platform=SourcePlatform.DISCORD,
            ),
            _make_ingested(
                frag_id="frag-b",
                body="ok",
                platform=SourcePlatform.DISCORD,
            ),
        ]
        trees = classify_reatomize_stream(
            ingested,
            _accepting_classifier(),
            config=ReatomizeConfig(enabled=False),
        )
        assert [t.stop_reason for t in trees] == ["disabled", "disabled"]


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


class TestAggregateNoSiblings:
    """Single-fragment API can't aggregate; records that fact and stops."""

    def test_lone_chat_message_records_aggregate_no_siblings(self) -> None:
        ingested = _make_ingested(
            body="hi",
            level="document",
            platform=SourcePlatform.DISCORD,
        )
        tree = classify_reatomize(
            ingested,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9),
        )
        assert tree.stop_reason == "aggregate_no_siblings"
        assert tree.children == ()


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


# ---- Zoom-out integration --------------------------------------------------


class TestZoomOutIntegration:
    """Stream of short chat messages → confidently-classified exchange parents."""

    @staticmethod
    def _short_chat_messages() -> list[IngestedFragment]:
        return [
            _make_ingested(
                frag_id=f"frag-msg{idx:08}",
                body=body,
                title=body,
                level="document",
                platform=SourcePlatform.DISCORD,
                minute=idx,
                interlocutor="alice",
                author=Authorship.SELF if idx % 2 == 0 else Authorship.OTHER,
            )
            for idx, body in enumerate(["hi", "hey", "ok", "thanks"])
        ]

    @staticmethod
    def _aggregating_classifier() -> Classifier:
        """LLM mock: messages stay unclassified, exchange parents classify."""

        def _classify(ig: IngestedFragment) -> tuple[IngestedFragment, float]:
            if ig.fragment.level == "exchange":
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F2,
                            phase=Phase.RESTORATION,
                        ),
                        body=ig.body,
                    ),
                    0.9,
                )
            return ig, 0.2

        return _classify

    def test_stream_zooms_out_into_exchange_parents(self) -> None:
        trees = classify_reatomize_stream(
            self._short_chat_messages(),
            self._aggregating_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert len(trees) == 1
        parent_tree = trees[0]
        assert parent_tree.stop_reason == "aggregated"
        assert parent_tree.fragment.fragment.level == "exchange"
        assert parent_tree.fragment.fragment.frequency.primary == Frequency.F2.value
        # All four messages live under the single exchange parent.
        assert len(parent_tree.children) == 4

    def test_stream_idempotent_under_rerun(self) -> None:
        classifier = self._aggregating_classifier()
        cfg = ReatomizeConfig(enabled=True, threshold=0.7)
        first = classify_reatomize_stream(
            self._short_chat_messages(),
            classifier,
            config=cfg,
        )
        second = classify_reatomize_stream(
            self._short_chat_messages(),
            classifier,
            config=cfg,
        )
        assert [_shape_signature(t) for t in first] == [
            _shape_signature(t) for t in second
        ]
        assert [_ids(t) for t in first] == [_ids(t) for t in second]

    def test_stream_returns_empty_for_empty_input(self) -> None:
        trees = classify_reatomize_stream(
            [],
            _accepting_classifier(),
            config=ReatomizeConfig(enabled=True),
        )
        assert trees == []

    def test_stream_skips_aggregation_when_all_accepted(self) -> None:
        messages = self._short_chat_messages()
        trees = classify_reatomize_stream(
            messages,
            _accepting_classifier(0.95),
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert len(trees) == len(messages)
        assert all(t.stop_reason == "accepted" for t in trees)

    def test_stream_falls_through_to_split_for_document_sources(self) -> None:
        """Document-source weak fragments use the single-fragment splitter."""
        body = "# H1\n\npara one.\n\npara two."
        docs = [
            _make_ingested(
                frag_id="frag-doc-a",
                body=body,
                level="document",
                platform=SourcePlatform.MARKDOWN,
            ),
        ]
        trees = classify_reatomize_stream(
            docs,
            _rejecting_classifier(),
            config=ReatomizeConfig(enabled=True, threshold=0.9),
        )
        assert len(trees) == 1
        assert trees[0].stop_reason == "split"

    def test_stream_passthrough_weak_fragment_marked_passthrough(self) -> None:
        """A weak pass-through fragment is labeled ``passthrough``, not ``accepted``.

        Mixing a ``document``-level message with a ``burst``-level fragment
        forces the aggregator's eligibility partition to emit the burst as
        pass-through. When that pass-through fragment failed the threshold
        on its own, ``stop_reason`` must NOT be ``accepted`` — downstream
        consumers gate dashboards and export queues on that field.
        """

        def _mixed_classifier(
            ig: IngestedFragment,
        ) -> tuple[IngestedFragment, float]:
            if ig.fragment.level == "exchange":
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F2,
                            phase=Phase.RISING,
                        ),
                        body=ig.body,
                    ),
                    0.9,
                )
            return ig, 0.2

        mixed = [
            _make_ingested(
                frag_id="frag-msg00000001",
                body="hi",
                level="document",
                platform=SourcePlatform.DISCORD,
                minute=0,
            ),
            _make_ingested(
                frag_id="frag-burst00001",
                body="burst body",
                level="burst",
                platform=SourcePlatform.DISCORD,
                minute=5,
            ),
        ]
        trees = classify_reatomize_stream(
            mixed,
            _mixed_classifier,
            config=ReatomizeConfig(
                enabled=True,
                threshold=0.7,
                direction="aggregate",
            ),
        )
        # The aggregator emits one exchange parent and one burst pass-through.
        stops = {t.stop_reason for t in trees}
        assert "aggregated" in stops
        # The weak burst pass-through must be flagged honestly, NOT "accepted":
        # confidence was 0.2 against a threshold of 0.7.
        assert "passthrough" in stops
        assert "accepted" not in stops

    def test_stream_passthrough_strong_fragment_marked_accepted(self) -> None:
        """A pass-through fragment that *does* pass the threshold stays ``accepted``."""

        def _strong_passthrough_classifier(
            ig: IngestedFragment,
        ) -> tuple[IngestedFragment, float]:
            if ig.fragment.level == "exchange":
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F2,
                            phase=Phase.RISING,
                        ),
                        body=ig.body,
                    ),
                    0.9,
                )
            if ig.fragment.level == "burst":
                # Confident pass-through — must keep its "accepted" label.
                return (
                    IngestedFragment(
                        fragment=_classified(
                            ig.fragment,
                            frequency=Frequency.F5,
                            phase=Phase.PEAKING,
                        ),
                        body=ig.body,
                    ),
                    0.95,
                )
            # The document-level message stays weak so aggregation fires.
            return ig, 0.2

        mixed = [
            _make_ingested(
                frag_id="frag-msg00000002",
                body="hi",
                level="document",
                platform=SourcePlatform.DISCORD,
                minute=0,
            ),
            _make_ingested(
                frag_id="frag-burst00002",
                body="burst body",
                level="burst",
                platform=SourcePlatform.DISCORD,
                minute=5,
            ),
        ]
        trees = classify_reatomize_stream(
            mixed,
            _strong_passthrough_classifier,
            config=ReatomizeConfig(
                enabled=True,
                threshold=0.7,
                direction="aggregate",
            ),
        )
        stops = {t.stop_reason for t in trees}
        assert "aggregated" in stops
        assert "accepted" in stops  # confident pass-through keeps "accepted"
        assert "passthrough" not in stops  # confident burst is NOT a weak passthrough

    def test_stream_aggregated_weak_parent_marked_aggregated_weak(self) -> None:
        """An aggregated parent that still fails the threshold gets ``aggregated_weak``.

        ``no_decomposition`` is reserved for "operator returned no children";
        a parent with non-empty children must not borrow that label.
        """

        def _weak_parent_classifier(
            _ig: IngestedFragment,
        ) -> tuple[IngestedFragment, float]:
            # Every fragment — leaf or aggregated parent — scores below threshold.
            return _ig, 0.2

        messages = [
            _make_ingested(
                frag_id=f"frag-weak{idx:08}",
                body=body,
                title=body,
                level="document",
                platform=SourcePlatform.DISCORD,
                minute=idx,
            )
            for idx, body in enumerate(["a", "b", "c"])
        ]
        trees = classify_reatomize_stream(
            messages,
            _weak_parent_classifier,
            config=ReatomizeConfig(enabled=True, threshold=0.7),
        )
        assert len(trees) == 1
        parent = trees[0]
        # Children were produced, so "no_decomposition" would be a lie.
        assert parent.children
        assert parent.stop_reason == "aggregated_weak"

    def test_stream_returns_terminal_when_base_level_is_session(self) -> None:
        """A stream already at the terminal level can't aggregate further."""
        sessions = [
            _make_ingested(
                frag_id=f"frag-sess{idx:08}",
                body="x",
                level="session",
                platform=SourcePlatform.DISCORD,
                minute=idx,
            )
            for idx in range(2)
        ]
        # Force aggregate direction so the stream takes the zoom-out branch.
        trees = classify_reatomize_stream(
            sessions,
            _rejecting_classifier(),
            config=ReatomizeConfig(
                enabled=True,
                threshold=0.9,
                direction="aggregate",
            ),
        )
        # Sessions hit the terminal guard in the zoom-out branch.
        assert all(t.stop_reason == "terminal" for t in trees)


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
