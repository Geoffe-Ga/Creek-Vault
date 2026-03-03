"""Tests for creek.clean.quality — content quality scorer.

Tests cover:
- QualityResult dataclass structure and constraints
- QualityScorer scoring of normal, empty, URL-only, emoji-only content
- Shannon entropy calculation for repetitive vs. varied text
- Stop-word ratio detection for filler content
- Alphanumeric presence check
- Configurable thresholds via constructor parameters
- Edge cases: unicode-only, mixed content, whitespace-only
"""

from creek.clean.quality import QualityResult, QualityScorer

# ---------------------------------------------------------------------------
# QualityResult
# ---------------------------------------------------------------------------


class TestQualityResult:
    """Tests for the QualityResult model."""

    def test_required_fields(self) -> None:
        """QualityResult should have score, action, and reasons fields."""
        result = QualityResult(score=0.8, action="accept", reasons=["good"])
        assert result.score == 0.8
        assert result.action == "accept"
        assert result.reasons == ["good"]

    def test_score_bounds_valid(self) -> None:
        """Score should accept values in [0.0, 1.0]."""
        low = QualityResult(score=0.0, action="skip", reasons=["empty"])
        high = QualityResult(score=1.0, action="accept", reasons=["perfect"])
        assert low.score == 0.0
        assert high.score == 1.0

    def test_action_values(self) -> None:
        """Action should accept 'accept', 'review', and 'skip'."""
        for action in ("accept", "review", "skip"):
            result = QualityResult(score=0.5, action=action, reasons=["test"])
            assert result.action == action

    def test_reasons_is_list(self) -> None:
        """Reasons should be a list of strings."""
        result = QualityResult(
            score=0.5,
            action="review",
            reasons=["low entropy", "high stop-word ratio"],
        )
        assert isinstance(result.reasons, list)
        assert len(result.reasons) == 2


# ---------------------------------------------------------------------------
# QualityScorer — Normal content
# ---------------------------------------------------------------------------


class TestQualityScorerNormal:
    """Tests for scoring normal, acceptable content."""

    def test_normal_content_accept(self) -> None:
        """Well-formed natural language content should score as accept."""
        scorer = QualityScorer()
        content = (
            "The quantum computing revolution represents a fundamental shift "
            "in computational paradigms. Unlike classical computers that process "
            "bits as zeros or ones, quantum computers leverage superposition "
            "and entanglement to explore multiple solutions simultaneously. "
            "This breakthrough enables solving problems previously considered "
            "intractable, from drug discovery to cryptographic analysis."
        )
        result = scorer.score(content)
        assert result.action == "accept"
        assert result.score > 0.5

    def test_normal_content_has_no_negative_reasons(self) -> None:
        """Well-formed content should have no skip or review reasons."""
        scorer = QualityScorer()
        content = (
            "Distributed systems require careful coordination between nodes. "
            "Consensus algorithms like Raft and Paxos provide mechanisms for "
            "achieving agreement despite partial failures. The CAP theorem "
            "establishes fundamental trade-offs between consistency, "
            "availability, and partition tolerance."
        )
        result = scorer.score(content)
        assert result.action == "accept"

    def test_score_returns_quality_result(self) -> None:
        """score() should return a QualityResult instance."""
        scorer = QualityScorer()
        result = scorer.score("Some reasonable content here.")
        assert isinstance(result, QualityResult)


# ---------------------------------------------------------------------------
# QualityScorer — Empty and whitespace-only
# ---------------------------------------------------------------------------


class TestQualityScorerEmpty:
    """Tests for scoring empty or whitespace-only content."""

    def test_empty_string_skip(self) -> None:
        """Empty string should be skipped."""
        scorer = QualityScorer()
        result = scorer.score("")
        assert result.action == "skip"
        assert result.score == 0.0

    def test_whitespace_only_skip(self) -> None:
        """Whitespace-only content should be skipped."""
        scorer = QualityScorer()
        result = scorer.score("   \t\n  \n\n  ")
        assert result.action == "skip"
        assert result.score == 0.0

    def test_empty_has_reason(self) -> None:
        """Empty content should include a reason explaining the skip."""
        scorer = QualityScorer()
        result = scorer.score("")
        assert len(result.reasons) >= 1
        lower_reasons = [r.lower() for r in result.reasons]
        assert any("empty" in r or "content" in r for r in lower_reasons)


# ---------------------------------------------------------------------------
# QualityScorer — URL-only content
# ---------------------------------------------------------------------------


class TestQualityScorerUrlOnly:
    """Tests for scoring content that is exclusively URLs."""

    def test_single_url_skip(self) -> None:
        """A single URL with no other content should be skipped."""
        scorer = QualityScorer()
        result = scorer.score("https://example.com/some/path")
        assert result.action == "skip"

    def test_multiple_urls_skip(self) -> None:
        """Multiple URLs with no prose should be skipped."""
        scorer = QualityScorer()
        content = (
            "https://example.com/page1\n"
            "https://example.com/page2\n"
            "http://another-site.org/resource"
        )
        result = scorer.score(content)
        assert result.action == "skip"

    def test_url_with_prose_not_skip(self) -> None:
        """URL accompanied by substantial prose should not be skipped as URL-only."""
        scorer = QualityScorer()
        content = (
            "Check out this fascinating article about machine learning "
            "architectures and transformer models: https://example.com/article "
            "It provides detailed analysis of attention mechanisms and their "
            "applications in natural language processing."
        )
        result = scorer.score(content)
        assert result.action != "skip"

    def test_url_only_has_reason(self) -> None:
        """URL-only content should include a reason in the result."""
        scorer = QualityScorer()
        result = scorer.score("https://example.com")
        assert any("url" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# QualityScorer — Emoji-only content
# ---------------------------------------------------------------------------


class TestQualityScorerEmojiOnly:
    """Tests for scoring content that is exclusively emoji."""

    def test_emoji_only_skip(self) -> None:
        """Content that is only emoji should be skipped."""
        scorer = QualityScorer()
        result = scorer.score("\U0001f600\U0001f601\U0001f602\U0001f603\U0001f604")
        assert result.action == "skip"

    def test_emoji_with_text_not_skip(self) -> None:
        """Emoji mixed with substantial prose should not be skipped."""
        scorer = QualityScorer()
        content = (
            "Great progress on the quantum computing project today! "
            "\U0001f680 The team demonstrated entanglement fidelity above "
            "99 percent, which represents a significant milestone."
        )
        result = scorer.score(content)
        assert result.action != "skip"

    def test_emoji_only_has_reason(self) -> None:
        """Emoji-only content should include a reason in the result."""
        scorer = QualityScorer()
        result = scorer.score("\U0001f600\U0001f601\U0001f602")
        assert any("emoji" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# QualityScorer — Stop-word ratio
# ---------------------------------------------------------------------------


class TestQualityScorerStopWords:
    """Tests for scoring content with high stop-word ratios."""

    def test_high_stop_word_ratio_review(self) -> None:
        """Content dominated by stop words should trigger review."""
        scorer = QualityScorer()
        # Extremely filler-heavy content
        content = (
            "I am the one who is the one that is in the and of the "
            "to a for it is the of and to be or not is to the with "
            "an at by the in a the to of and is it that this for the"
        )
        result = scorer.score(content)
        assert result.action in ("review", "skip")

    def test_normal_stop_word_ratio_accept(self) -> None:
        """Content with a balanced stop-word ratio should be accepted."""
        scorer = QualityScorer()
        content = (
            "Neural network architectures leverage gradient descent "
            "optimization across multiple hidden layers. Backpropagation "
            "computes partial derivatives efficiently through computational "
            "graph traversal, enabling parameter updates."
        )
        result = scorer.score(content)
        assert result.action == "accept"


# ---------------------------------------------------------------------------
# QualityScorer — Low entropy (repetitive content)
# ---------------------------------------------------------------------------


class TestQualityScorerEntropy:
    """Tests for scoring repetitive (low-entropy) content."""

    def test_highly_repetitive_review_or_skip(self) -> None:
        """Repetitive content should trigger review or skip."""
        scorer = QualityScorer()
        content = "hello " * 50
        result = scorer.score(content)
        assert result.action in ("review", "skip")

    def test_single_char_repeated_review_or_skip(self) -> None:
        """Single character repeated should trigger review or skip."""
        scorer = QualityScorer()
        content = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        result = scorer.score(content)
        assert result.action in ("review", "skip")

    def test_varied_content_good_entropy(self) -> None:
        """Varied natural language should have good entropy."""
        scorer = QualityScorer()
        content = (
            "Philosophy explores fundamental questions about existence, "
            "knowledge, values, reason, mind, and language. Epistemology "
            "examines justified belief while metaphysics investigates "
            "the nature of reality itself."
        )
        result = scorer.score(content)
        assert result.action == "accept"

    def test_entropy_reason_present(self) -> None:
        """Low-entropy content should include an entropy-related reason."""
        scorer = QualityScorer()
        content = "abc " * 100
        result = scorer.score(content)
        lower_reasons = [r.lower() for r in result.reasons]
        assert any("entropy" in r or "repetitive" in r for r in lower_reasons)


# ---------------------------------------------------------------------------
# QualityScorer — Alphanumeric presence
# ---------------------------------------------------------------------------


class TestQualityScorerAlphanumeric:
    """Tests for alphanumeric presence check."""

    def test_no_alphanumeric_skip(self) -> None:
        """Content with no alphanumeric characters should be skipped."""
        scorer = QualityScorer()
        result = scorer.score("!@#$%^&*()_+-=[]{}|;':\",./<>?")
        assert result.action == "skip"

    def test_has_alphanumeric_not_skip_for_that_reason(self) -> None:
        """Content with alphanumeric chars should not be skipped for lack of them."""
        scorer = QualityScorer()
        result = scorer.score(
            "This content has alphanumeric characters and good structure."
        )
        assert not any("alphanumeric" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# QualityScorer — Configurable thresholds
# ---------------------------------------------------------------------------


class TestQualityScorerThresholds:
    """Tests for custom threshold configuration."""

    def test_custom_min_length(self) -> None:
        """Custom min_length should affect scoring."""
        strict = QualityScorer(min_length=100)
        lenient = QualityScorer(min_length=5)

        short_content = "Hello world this is short."

        strict_result = strict.score(short_content)
        lenient_result = lenient.score(short_content)

        # Strict scorer should penalize short content more
        assert strict_result.score <= lenient_result.score

    def test_custom_min_words(self) -> None:
        """Custom min_words should affect scoring."""
        strict = QualityScorer(min_words=50)
        lenient = QualityScorer(min_words=3)

        short_content = "A brief but meaningful note about architecture."

        strict_result = strict.score(short_content)
        lenient_result = lenient.score(short_content)

        assert strict_result.score <= lenient_result.score

    def test_custom_entropy_threshold(self) -> None:
        """Custom entropy_threshold should affect scoring."""
        strict = QualityScorer(entropy_threshold=4.0)
        lenient = QualityScorer(entropy_threshold=1.0)

        content = "moderate content variety here today now then words"

        strict_result = strict.score(content)
        lenient_result = lenient.score(content)

        assert strict_result.score <= lenient_result.score

    def test_custom_stop_word_threshold(self) -> None:
        """Custom stop_word_threshold should affect scoring."""
        strict = QualityScorer(stop_word_threshold=0.3)
        lenient = QualityScorer(stop_word_threshold=0.9)

        # Content with moderate stop-word ratio
        content = (
            "The analysis of the data in the experiment showed that "
            "the results were consistent with predictions."
        )

        strict_result = strict.score(content)
        lenient_result = lenient.score(content)

        assert strict_result.score <= lenient_result.score

    def test_default_thresholds_produce_reasonable_results(self) -> None:
        """Default thresholds should produce sensible results for typical content."""
        scorer = QualityScorer()
        good = scorer.score(
            "Functional programming emphasizes immutability and pure functions. "
            "Languages like Haskell and Clojure demonstrate these principles "
            "through algebraic data types and persistent data structures."
        )
        bad = scorer.score("")
        assert good.score > bad.score


# ---------------------------------------------------------------------------
# QualityScorer — Edge cases
# ---------------------------------------------------------------------------


class TestQualityScorerEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_unicode_only_content(self) -> None:
        """Unicode-only content (no ASCII alphanumeric) should be handled."""
        scorer = QualityScorer()
        # Chinese characters (has meaning but no ASCII alphanumeric)
        result = scorer.score("\u4f60\u597d\u4e16\u754c")
        assert isinstance(result, QualityResult)
        # Should not crash; action depends on implementation

    def test_mixed_content_with_urls_and_text(self) -> None:
        """Mixed content with URLs and substantial text should be accepted."""
        scorer = QualityScorer()
        content = (
            "This research paper explores distributed consensus algorithms. "
            "See https://example.com/paper for the full text. "
            "The key contribution is a novel approach to Byzantine fault "
            "tolerance that reduces message complexity significantly."
        )
        result = scorer.score(content)
        assert result.action == "accept"

    def test_very_long_content(self) -> None:
        """Very long content should still be scored without errors."""
        scorer = QualityScorer()
        content = "Exploring various aspects of distributed computing. " * 200
        result = scorer.score(content)
        assert isinstance(result, QualityResult)
        assert 0.0 <= result.score <= 1.0

    def test_single_word(self) -> None:
        """A single word should produce a valid result (likely review or skip)."""
        scorer = QualityScorer()
        result = scorer.score("hello")
        assert isinstance(result, QualityResult)
        assert result.action in ("review", "skip")

    def test_newlines_and_tabs(self) -> None:
        """Content with many newlines and tabs should be handled."""
        scorer = QualityScorer()
        content = "Line one.\n\n\tLine two.\n\n\tLine three with content."
        result = scorer.score(content)
        assert isinstance(result, QualityResult)

    def test_score_always_in_range(self) -> None:
        """Score should always be between 0.0 and 1.0 for any input."""
        scorer = QualityScorer()
        test_inputs = [
            "",
            "a",
            "hello world",
            "x" * 10000,
            "\U0001f600" * 50,
            "https://example.com",
            "!@#$%^&*()",
            "Normal natural language content with good variety.",
        ]
        for content in test_inputs:
            result = scorer.score(content)
            assert 0.0 <= result.score <= 1.0, (
                f"Score {result.score} out of range for: {content!r}"
            )

    def test_reasons_always_non_empty_for_non_accept(self) -> None:
        """Non-accept results should always have at least one reason."""
        scorer = QualityScorer()
        test_inputs = [
            "",
            "https://example.com",
            "\U0001f600\U0001f601\U0001f602",
            "!@#$%^&*()",
            "a " * 100,
        ]
        for content in test_inputs:
            result = scorer.score(content)
            if result.action != "accept":
                assert len(result.reasons) >= 1, (
                    f"No reasons for {result.action}: {content!r}"
                )
