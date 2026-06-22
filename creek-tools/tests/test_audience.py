"""Tests for the AudienceClassifier (#634).

The audience axis tags whether a fragment was written *for an audience*
(``audience-facing``), kept *private*, or is ambiguous (``mixed``). These tests
pin the two acceptance cases — a casual OPEN chat is NOT audience-facing, a
Substack essay IS — plus the layered signals, the fail-closed default, the
optional LLM seam, and idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from creek.classify.audience import AudienceClassifier, AudienceLLMAssist
from creek.models import (
    Audience,
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourceKind,
    SourcePlatform,
    synthetic_fragment_id,
)

_CASUAL_CHAT = "lol yeah that works, see you then"
_ESSAY_BODY = (
    "# On Equanimity\n\n"
    "There is a particular quiet that arrives only after the storm has had "
    "its say. I have learned to wait for it rather than chase it.\n\n"
    "> The wound is the place where the Light enters you.\n~ Rumi\n\n"
) + ("Each paragraph turns the same stone to a new face. " * 60)


def _make_fragment(
    *,
    platform: SourcePlatform = SourcePlatform.OTHER,
    kind: SourceKind = SourceKind.UNCLASSIFIED,
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
    channel: str | None = None,
    author: Authorship = Authorship.SELF,
) -> Fragment:
    """Build a deterministic Fragment for audience tests."""
    return Fragment(
        id=synthetic_fragment_id(),
        title="A note",
        source=FragmentSource(
            platform=platform,
            kind=kind,
            channel=channel,
            author=author,
        ),
        privacy_tier=privacy_tier,
        created=datetime(2026, 4, 1, tzinfo=UTC),
        ingested=datetime(2026, 4, 1, tzinfo=UTC),
    )


class TestClassifyAudience:
    """``classify_audience`` maps layered signals to the audience axis."""

    def test_substack_essay_is_audience_facing(self) -> None:
        """A published Substack essay is the canonical audience-facing case."""
        classifier = AudienceClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.SUBSTACK,
            kind=SourceKind.WRITING,
            privacy_tier=PrivacyTier.OPEN,
        )
        assert classifier.classify_audience(frag, _ESSAY_BODY) == "audience-facing"

    def test_casual_open_chat_is_not_audience_facing(self) -> None:
        """A short OPEN Discord blip is private, not audience-facing."""
        classifier = AudienceClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.DISCORD,
            privacy_tier=PrivacyTier.OPEN,
            channel="general",
        )
        verdict = classifier.classify_audience(frag, _CASUAL_CHAT)
        assert verdict == "private"
        assert verdict != "audience-facing"

    def test_journal_is_private(self) -> None:
        """An intimate journal entry is firmly private."""
        classifier = AudienceClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.JOURNAL,
            privacy_tier=PrivacyTier.INTIMATE,
        )
        assert classifier.classify_audience(frag, "today I felt raw") == "private"

    def test_essay_platform_alone_is_audience_facing(self) -> None:
        """The essay platform is a strong enough signal on its own."""
        classifier = AudienceClassifier()
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        assert classifier.classify_audience(frag, "short but published") == (
            "audience-facing"
        )

    def test_long_structured_note_is_audience_facing(self) -> None:
        """Long-form with headings + quotation reads as audience-facing."""
        classifier = AudienceClassifier()
        frag = _make_fragment(platform=SourcePlatform.MARKDOWN)
        assert classifier.classify_audience(frag, _ESSAY_BODY) == "audience-facing"

    def test_neutral_medium_length_note_is_mixed(self) -> None:
        """An unmapped platform with no strong signal stays mixed."""
        classifier = AudienceClassifier()
        frag = _make_fragment(platform=SourcePlatform.MARKDOWN)
        body = "A handful of ordinary sentences. " * 12
        assert classifier.classify_audience(frag, body) == "mixed"


class TestLLMSeam:
    """The optional LLM seam refines only the ambiguous ``mixed`` verdict."""

    def test_seam_consulted_on_mixed(self) -> None:
        """A configured assist can resolve a ``mixed`` heuristic verdict."""

        class _Assist:
            def refine(
                self,
                fragment: Fragment,
                content: str,
                heuristic: Audience,
            ) -> Audience:
                assert heuristic == "mixed"
                return "audience-facing"

        assist: AudienceLLMAssist = _Assist()
        classifier = AudienceClassifier(llm_assist=assist)
        frag = _make_fragment(platform=SourcePlatform.MARKDOWN)
        body = "A handful of ordinary sentences. " * 12
        assert classifier.classify_audience(frag, body) == "audience-facing"

    def test_seam_not_consulted_when_confident(self) -> None:
        """A confident heuristic verdict never spends an LLM call."""

        class _Boom:
            def refine(
                self,
                fragment: Fragment,
                content: str,
                heuristic: Audience,
            ) -> Audience:
                raise AssertionError("seam must not be called when confident")

        classifier = AudienceClassifier(llm_assist=_Boom())
        frag = _make_fragment(
            platform=SourcePlatform.SUBSTACK,
            kind=SourceKind.WRITING,
        )
        assert classifier.classify_audience(frag, _ESSAY_BODY) == "audience-facing"


class TestEnforceAndIdempotency:
    """``classify_and_enforce`` stamps the axis without mutating the input."""

    def test_classify_and_enforce_sets_axis(self) -> None:
        """The returned copy carries the verdict; the original is untouched."""
        classifier = AudienceClassifier()
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        assert frag.audience == "mixed"
        result = classifier.classify_and_enforce(frag, "published")
        assert result.audience == "audience-facing"
        assert frag.audience == "mixed"

    def test_idempotent(self) -> None:
        """Re-running on the stamped fragment yields the same axis."""
        classifier = AudienceClassifier()
        frag = _make_fragment(platform=SourcePlatform.JOURNAL)
        once = classifier.classify_and_enforce(frag, "private musing")
        twice = classifier.classify_and_enforce(once, "private musing")
        assert once.audience == twice.audience == "private"
