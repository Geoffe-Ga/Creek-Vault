"""Tests for creek.decide module — decision support layer.

Tests cover DecisionDetector, ContextGatherer, DecisionContext,
DecisionResult, DecisionPipeline orchestration, and package re-exports.
"""

from datetime import date

from creek.config import DecisionConfig
from creek.decide import (
    ContextGatherer,
    DecisionContext,
    DecisionDetector,
    DecisionPipeline,
    DecisionResult,
)
from creek.decide.context import ContextGatherer as ContextGathererDirect
from creek.decide.context import DecisionContext as DecisionContextDirect
from creek.decide.detector import (
    BODY_WEIGHT,
    DECISION_SIGNALS,
    DEFAULT_THRESHOLD,
    FIRST_PARA_WEIGHT,
    TITLE_WEIGHT,
    _count_signal_matches,
    _split_content_sections,
)
from creek.decide.detector import (
    DecisionDetector as DecisionDetectorDirect,
)
from creek.decide.pipeline import DecisionPipeline as DecisionPipelineDirect
from creek.decide.pipeline import DecisionResult as DecisionResultDirect
from creek.models import (
    Decision,
    DecisionStatus,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    Praxis,
    PraxisType,
    SourcePlatform,
    Thread,
    WavelengthClassification,
    WavelengthObservation,
)


def _make_fragment(
    title: str = "Test Fragment",
    tags: list[str] | None = None,
    primary_freq: Frequency = Frequency.UNCLASSIFIED,
    phase: Phase = Phase.UNCLASSIFIED,
) -> Fragment:
    """Create a minimal Fragment for testing."""
    return Fragment(
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        frequency=FrequencyClassification(primary=primary_freq),
        wavelength=WavelengthClassification(phase=phase),
        tags=tags or [],
    )


def _make_thread(
    title: str = "Test Thread",
    tags: list[str] | None = None,
) -> Thread:
    """Create a minimal Thread for testing."""
    return Thread(title=title, tags=tags or [])


def _make_decision(
    title: str = "Test Decision",
    tags: list[str] | None = None,
    frequency_context: list[Frequency] | None = None,
    status: DecisionStatus = DecisionStatus.SENSING,
) -> Decision:
    """Create a minimal Decision for testing."""
    return Decision(
        title=title,
        tags=tags or [],
        frequency_context=frequency_context or [],
        status=status,
    )


def _make_praxis(
    title: str = "Test Praxis",
    tags: list[str] | None = None,
    frequency: list[Frequency] | None = None,
) -> Praxis:
    """Create a minimal Praxis for testing."""
    return Praxis(
        title=title,
        tags=tags or [],
        frequency=frequency or [],
        praxis_type=PraxisType.INSIGHT,
    )


def _make_observation(
    obs_date: date | None = None,
    phase: Phase = Phase.RISING,
) -> WavelengthObservation:
    """Create a minimal WavelengthObservation for testing."""
    return WavelengthObservation(
        date=obs_date or date.today(),
        phase=phase,
    )


# ---- Package __init__ re-exports ----


class TestPackageExports:
    """Tests that creek.decide.__init__ re-exports all public classes."""

    def test_decision_detector_reexported(self) -> None:
        """DecisionDetector should be importable from creek.decide."""
        assert DecisionDetector is DecisionDetectorDirect

    def test_context_gatherer_reexported(self) -> None:
        """ContextGatherer should be importable from creek.decide."""
        assert ContextGatherer is ContextGathererDirect

    def test_decision_context_reexported(self) -> None:
        """DecisionContext should be importable from creek.decide."""
        assert DecisionContext is DecisionContextDirect

    def test_decision_result_reexported(self) -> None:
        """DecisionResult should be importable from creek.decide."""
        assert DecisionResult is DecisionResultDirect

    def test_decision_pipeline_reexported(self) -> None:
        """DecisionPipeline should be importable from creek.decide."""
        assert DecisionPipeline is DecisionPipelineDirect


# ---- Detector Helper Tests ----


class TestDetectorHelpers:
    """Tests for detector module helper functions."""

    def test_count_signal_matches_finds_exact(self) -> None:
        """Signal matching should find exact word-boundary matches."""
        count = _count_signal_matches("should I go?", ["should I"])
        assert count == 1

    def test_count_signal_matches_case_insensitive(self) -> None:
        """Signal matching should be case-insensitive."""
        count = _count_signal_matches("Should I go?", ["should i"])
        assert count == 1

    def test_count_signal_matches_no_partial(self) -> None:
        """Signal matching should not match partial words."""
        count = _count_signal_matches("crossbow", ["cross"])
        assert count == 0

    def test_count_signal_matches_empty_text(self) -> None:
        """Signal matching on empty text should return zero."""
        count = _count_signal_matches("", ["should I"])
        assert count == 0

    def test_count_signal_matches_multiple(self) -> None:
        """Signal matching should count multiple occurrences."""
        text = "should I stay or should I go"
        count = _count_signal_matches(text, ["should I"])
        assert count == 2

    def test_split_content_sections_with_blank_line(self) -> None:
        """Splitting should separate at the first blank line."""
        content = "first paragraph\n\nsecond paragraph"
        first, body = _split_content_sections(content)
        assert first == "first paragraph"
        assert body == "second paragraph"

    def test_split_content_sections_no_blank_line(self) -> None:
        """Splitting content with no blank line should return empty body."""
        content = "only one section"
        first, body = _split_content_sections(content)
        assert first == "only one section"
        assert body == ""

    def test_decision_signals_not_empty(self) -> None:
        """DECISION_SIGNALS should contain signal phrases."""
        assert len(DECISION_SIGNALS) > 0

    def test_weight_constants(self) -> None:
        """Positional weight constants should have expected values."""
        assert TITLE_WEIGHT == 3
        assert FIRST_PARA_WEIGHT == 2
        assert BODY_WEIGHT == 1
        assert DEFAULT_THRESHOLD == 2


# ---- DecisionDetector Tests ----


class TestDecisionDetector:
    """Tests for the DecisionDetector class."""

    def test_init_stores_threshold(self) -> None:
        """Detector should store the configured threshold."""
        detector = DecisionDetector(threshold=5)
        assert detector.threshold == 5

    def test_init_default_threshold(self) -> None:
        """Detector should use DEFAULT_THRESHOLD by default."""
        detector = DecisionDetector()
        assert detector.threshold == DEFAULT_THRESHOLD

    def test_score_fragment_title_only(self) -> None:
        """Scoring should detect signals in fragment title."""
        fragment = _make_fragment(title="Should I move to Portland?")
        detector = DecisionDetector()
        score = detector.score_fragment(fragment)
        assert score >= TITLE_WEIGHT

    def test_score_fragment_content_first_para(self) -> None:
        """Scoring should detect signals in first paragraph."""
        fragment = _make_fragment(title="Reflections")
        content = "I'm trying to decide about this.\n\nMore text here."
        detector = DecisionDetector()
        score = detector.score_fragment(fragment, content)
        assert score >= FIRST_PARA_WEIGHT

    def test_score_fragment_content_body(self) -> None:
        """Scoring should detect signals in body text."""
        fragment = _make_fragment(title="Notes")
        content = "Introduction text.\n\nI'm weighing options carefully."
        detector = DecisionDetector()
        score = detector.score_fragment(fragment, content)
        assert score >= BODY_WEIGHT

    def test_score_fragment_no_signals(self) -> None:
        """Scoring a fragment with no decision signals should return 0."""
        fragment = _make_fragment(title="Shopping list")
        detector = DecisionDetector()
        score = detector.score_fragment(fragment, "eggs, milk, bread")
        assert score == 0

    def test_is_decision_relevant_true(self) -> None:
        """Fragment with sufficient signals should be relevant."""
        fragment = _make_fragment(title="Should I quit my job?")
        detector = DecisionDetector(threshold=2)
        assert detector.is_decision_relevant(fragment) is True

    def test_is_decision_relevant_false(self) -> None:
        """Fragment without signals should not be relevant."""
        fragment = _make_fragment(title="Today's weather")
        detector = DecisionDetector()
        assert detector.is_decision_relevant(fragment) is False

    def test_detect_filters_relevant(self) -> None:
        """Detect should return only decision-relevant fragments."""
        fragments = [
            _make_fragment(title="Should I move?"),
            _make_fragment(title="Grocery list"),
            _make_fragment(title="Torn between two options"),
        ]
        detector = DecisionDetector(threshold=2)
        relevant = detector.detect(fragments)
        assert len(relevant) == 2
        assert relevant[0].title == "Should I move?"
        assert relevant[1].title == "Torn between two options"

    def test_detect_with_contents(self) -> None:
        """Detect should use content strings when provided."""
        fragments = [_make_fragment(title="Journal entry")]
        contents = ["I'm trying to decide whether to accept the offer."]
        detector = DecisionDetector(threshold=2)
        relevant = detector.detect(fragments, contents)
        assert len(relevant) == 1

    def test_detect_empty_list(self) -> None:
        """Detect should handle empty fragment list."""
        detector = DecisionDetector()
        relevant = detector.detect([])
        assert relevant == []

    def test_detect_logs_count(self) -> None:
        """Detect should log the number of flagged fragments."""
        fragments = [_make_fragment(title="Should I go?")]
        detector = DecisionDetector(threshold=2)
        detector.detect(fragments)

    def test_create_decision_from_fragment(self) -> None:
        """Creating a decision should set title and sensing status."""
        fragment = _make_fragment(
            title="Career change",
            tags=["career", "life"],
            primary_freq=Frequency.F5,
        )
        detector = DecisionDetector()
        decision = detector.create_decision(fragment)
        assert decision.title == "Decision: Career change"
        assert decision.status == DecisionStatus.SENSING
        assert "F5" in decision.frequency_context
        assert decision.tags == ["career", "life"]

    def test_create_decision_unclassified_freq(self) -> None:
        """Creating a decision from unclassified fragment omits frequency."""
        fragment = _make_fragment(title="Something")
        detector = DecisionDetector()
        decision = detector.create_decision(fragment)
        assert decision.frequency_context == []

    def test_create_decision_preserves_threads(self) -> None:
        """Created decision should preserve fragment thread references."""
        fragment = _make_fragment(title="Choice")
        fragment = fragment.model_copy(update={"threads": ["thread-abc", "thread-def"]})
        detector = DecisionDetector()
        decision = detector.create_decision(fragment)
        assert decision.relevant_threads == ["thread-abc", "thread-def"]

    def test_create_decision_wavelength_phase(self) -> None:
        """Created decision should record wavelength phase at opening."""
        fragment = _make_fragment(title="Hmm", phase=Phase.PEAKING)
        detector = DecisionDetector()
        decision = detector.create_decision(fragment)
        assert decision.wavelength_phase_at_opening == "peaking"


# ---- ContextGatherer Tests ----


class TestContextGatherer:
    """Tests for the ContextGatherer class."""

    def test_init_stores_min_tag_overlap(self) -> None:
        """Gatherer should store min_tag_overlap."""
        gatherer = ContextGatherer(min_tag_overlap=3)
        assert gatherer.min_tag_overlap == 3

    def test_gather_empty_inputs(self) -> None:
        """Gathering with no available data should return empty context."""
        decision = _make_decision(title="Test")
        gatherer = ContextGatherer()
        context = gatherer.gather(decision)
        assert context.decision_id == decision.id
        assert context.related_threads == []
        assert context.related_decisions == []
        assert context.related_praxis == []
        assert context.wavelength_phase == ""

    def test_gather_finds_related_threads(self) -> None:
        """Gatherer should find threads with overlapping tags."""
        decision = _make_decision(tags=["career", "life"])
        threads = [
            _make_thread(title="Career thread", tags=["career"]),
            _make_thread(title="Cooking thread", tags=["food"]),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, threads=threads)
        assert len(context.related_threads) == 1
        assert context.related_threads[0] == threads[0].id

    def test_gather_finds_related_decisions(self) -> None:
        """Gatherer should find past decisions with overlapping tags."""
        decision = _make_decision(tags=["career"])
        past = [
            _make_decision(title="Old career decision", tags=["career"]),
            _make_decision(title="Unrelated", tags=["food"]),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, decisions=past)
        assert len(context.related_decisions) == 1

    def test_gather_excludes_self(self) -> None:
        """Gatherer should not include the current decision in related."""
        decision = _make_decision(tags=["career"])
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, decisions=[decision])
        assert decision.id not in context.related_decisions

    def test_gather_finds_decisions_by_frequency(self) -> None:
        """Gatherer should match past decisions by frequency overlap."""
        decision = _make_decision(tags=[], frequency_context=[Frequency.F5])
        past = [
            _make_decision(
                title="Past F5",
                tags=[],
                frequency_context=[Frequency.F5],
            ),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, decisions=past)
        assert len(context.related_decisions) == 1

    def test_gather_finds_related_praxis(self) -> None:
        """Gatherer should find praxis notes with overlapping tags."""
        decision = _make_decision(tags=["meditation"])
        praxis = [
            _make_praxis(title="Meditation practice", tags=["meditation"]),
            _make_praxis(title="Running", tags=["fitness"]),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, praxis_notes=praxis)
        assert len(context.related_praxis) == 1

    def test_gather_finds_praxis_by_frequency(self) -> None:
        """Gatherer should match praxis by frequency overlap."""
        decision = _make_decision(frequency_context=[Frequency.F7])
        praxis = [
            _make_praxis(
                title="Systems thinking",
                frequency=[Frequency.F7],
            ),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, praxis_notes=praxis)
        assert len(context.related_praxis) == 1

    def test_gather_gets_current_phase(self) -> None:
        """Gatherer should return the most recent wavelength phase."""
        decision = _make_decision()
        observations = [
            _make_observation(date(2024, 1, 1), Phase.BOTTOMING_OUT),
            _make_observation(date(2024, 6, 1), Phase.PEAKING),
        ]
        gatherer = ContextGatherer()
        context = gatherer.gather(decision, observations=observations)
        assert context.wavelength_phase == "peaking"

    def test_gather_frequency_affinity(self) -> None:
        """Gathered context should include decision's frequency context."""
        decision = _make_decision(frequency_context=[Frequency.F3, Frequency.F7])
        gatherer = ContextGatherer()
        context = gatherer.gather(decision)
        assert "F3" in context.frequency_affinity
        assert "F7" in context.frequency_affinity

    def test_gather_min_tag_overlap_respected(self) -> None:
        """Higher min_tag_overlap should require more shared tags."""
        decision = _make_decision(tags=["career"])
        threads = [_make_thread(tags=["career"])]
        gatherer = ContextGatherer(min_tag_overlap=2)
        context = gatherer.gather(decision, threads=threads)
        assert len(context.related_threads) == 0


# ---- DecisionContext Rendering Tests ----


class TestDecisionContextRendering:
    """Tests for context rendering to markdown."""

    def test_render_empty_context(self) -> None:
        """Empty context should render placeholder message."""
        context = DecisionContext(decision_id="decision-test")
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "## Context" in rendered
        assert "*No related context found yet.*" in rendered

    def test_render_with_threads(self) -> None:
        """Context with threads should render wikilinks."""
        context = DecisionContext(
            decision_id="decision-test",
            related_threads=["thread-abc"],
        )
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "### Related Threads" in rendered
        assert "[[thread-abc]]" in rendered

    def test_render_with_decisions(self) -> None:
        """Context with past decisions should render wikilinks."""
        context = DecisionContext(
            decision_id="decision-test",
            related_decisions=["decision-old"],
        )
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "### Similar Past Decisions" in rendered
        assert "[[decision-old]]" in rendered

    def test_render_with_praxis(self) -> None:
        """Context with praxis notes should render wikilinks."""
        context = DecisionContext(
            decision_id="decision-test",
            related_praxis=["praxis-abc"],
        )
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "### Relevant Praxis" in rendered
        assert "[[praxis-abc]]" in rendered

    def test_render_with_wavelength(self) -> None:
        """Context with wavelength should render phase info."""
        context = DecisionContext(
            decision_id="decision-test",
            wavelength_phase="peaking",
        )
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "**Current Wavelength Phase:** peaking" in rendered

    def test_render_with_frequency_affinity(self) -> None:
        """Context with frequency affinity should render comma-separated."""
        context = DecisionContext(
            decision_id="decision-test",
            frequency_affinity=["F3", "F7"],
        )
        gatherer = ContextGatherer()
        rendered = gatherer.render_context_section(context)
        assert "**Frequency Affinity:** F3, F7" in rendered


# ---- DecisionResult Tests ----


class TestDecisionResult:
    """Tests for the DecisionResult Pydantic model."""

    def test_default_values(self) -> None:
        """DecisionResult should default all counts to zero."""
        result = DecisionResult()
        assert result.fragments_scanned == 0
        assert result.decisions_detected == 0
        assert result.decisions_created == 0
        assert result.contexts_gathered == 0

    def test_custom_values(self) -> None:
        """DecisionResult should store custom count values."""
        result = DecisionResult(
            fragments_scanned=10,
            decisions_detected=3,
            decisions_created=3,
            contexts_gathered=3,
        )
        assert result.fragments_scanned == 10
        assert result.decisions_detected == 3


# ---- DecisionPipeline Tests ----


class TestDecisionPipeline:
    """Tests for the DecisionPipeline orchestrator."""

    def test_init_creates_detector_and_gatherer(self) -> None:
        """Pipeline should create detector and gatherer on init."""
        pipeline = DecisionPipeline(threshold=5, min_tag_overlap=2)
        assert pipeline.detector.threshold == 5
        assert pipeline.gatherer.min_tag_overlap == 2

    def test_run_empty_fragments(self) -> None:
        """Running pipeline with no fragments should return empty result."""
        pipeline = DecisionPipeline()
        result, decisions, contexts = pipeline.run([])
        assert result.fragments_scanned == 0
        assert result.decisions_detected == 0
        assert decisions == []
        assert contexts == []

    def test_run_no_decisions_detected(self) -> None:
        """Pipeline with irrelevant fragments should detect nothing."""
        pipeline = DecisionPipeline()
        fragments = [_make_fragment(title="Nice weather today")]
        result, decisions, _contexts = pipeline.run(fragments)
        assert result.fragments_scanned == 1
        assert result.decisions_detected == 0
        assert decisions == []

    def test_run_detects_and_creates(self) -> None:
        """Pipeline should detect and create decisions for relevant fragments."""
        pipeline = DecisionPipeline(threshold=2)
        fragments = [
            _make_fragment(title="Should I change careers?"),
            _make_fragment(title="Grocery list"),
        ]
        result, decisions, _contexts = pipeline.run(fragments)
        assert result.fragments_scanned == 2
        assert result.decisions_detected == 1
        assert result.decisions_created == 1
        assert len(decisions) == 1
        assert decisions[0].title == "Decision: Should I change careers?"

    def test_run_gathers_context(self) -> None:
        """Pipeline should gather context for each created decision."""
        pipeline = DecisionPipeline(threshold=2)
        fragments = [
            _make_fragment(
                title="Should I move?",
                tags=["life", "location"],
            ),
        ]
        threads = [_make_thread(tags=["life"])]
        result, _decisions, contexts = pipeline.run(fragments, threads=threads)
        assert result.contexts_gathered == 1
        assert len(contexts) == 1
        assert len(contexts[0].related_threads) == 1

    def test_run_with_contents(self) -> None:
        """Pipeline should pass contents to detector."""
        pipeline = DecisionPipeline(threshold=2)
        fragments = [_make_fragment(title="Journal")]
        contents = ["I'm trying to decide if I should move."]
        result, _decisions, _contexts = pipeline.run(fragments, contents=contents)
        assert result.decisions_detected == 1

    def test_run_includes_existing_decisions_in_context(self) -> None:
        """Pipeline should include existing decisions when gathering context."""
        pipeline = DecisionPipeline(threshold=2)
        fragments = [
            _make_fragment(
                title="Should I quit?",
                tags=["career"],
                primary_freq=Frequency.F5,
            ),
        ]
        existing = [
            _make_decision(
                title="Past career choice",
                tags=["career"],
                frequency_context=[Frequency.F5],
            ),
        ]
        _result, _decisions, contexts = pipeline.run(fragments, decisions=existing)
        assert len(contexts[0].related_decisions) >= 1

    def test_run_with_observations(self) -> None:
        """Pipeline should pass observations for wavelength context."""
        pipeline = DecisionPipeline(threshold=2)
        fragments = [_make_fragment(title="Should I go?")]
        observations = [_make_observation(phase=Phase.DIMINISHING)]
        _result, _decisions, contexts = pipeline.run(
            fragments, observations=observations
        )
        assert contexts[0].wavelength_phase == "diminishing"


# ---- DecisionConfig Tests ----


class TestDecisionConfig:
    """Tests for the DecisionConfig Pydantic model."""

    def test_default_values(self) -> None:
        """DecisionConfig should have sensible defaults."""
        config = DecisionConfig()
        assert config.detection_threshold == 2
        assert config.min_tag_overlap == 1

    def test_custom_values(self) -> None:
        """DecisionConfig should accept custom values."""
        config = DecisionConfig(detection_threshold=5, min_tag_overlap=3)
        assert config.detection_threshold == 5
        assert config.min_tag_overlap == 3
