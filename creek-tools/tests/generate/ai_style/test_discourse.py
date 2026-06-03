"""Tests for the vault-relative discourse/structure detectors (FEAT-040.7)."""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.tells import get_tells

_CONFIG = AIStyleConfig()

# Every fingerprint-gated discourse feature (knowledge_cutoff is deliberately
# absent: it must fire regardless of the fingerprint).
_GATED_KEYS = (
    "negative_parallelism_density",
    "rule_of_three_rate",
    "challenges_section_density",
    "list_title_lead_density",
    "didactic_disclaimer_density",
    "section_summary_density",
    "comm_boilerplate_density",
)
_PLAIN = VoiceFingerprint(
    features={k: FeatureStat(rate=0.0, support=50) for k in _GATED_KEYS},
    fragment_count=50,
)
_ORNATE = VoiceFingerprint(
    features={k: FeatureStat(rate=900.0, support=50) for k in _GATED_KEYS},
    fragment_count=50,
)


def _fired(report, tell_id: str) -> bool:
    return any(f.tell_id == tell_id for f in report.findings)


def _message(report, tell_id: str) -> str:
    return next(f.message for f in report.findings if f.tell_id == tell_id)


def test_discourse_tells_registered_and_surface() -> None:
    """All eight discourse tells are registered and surface-handled."""
    tells = {t.id: t for t in get_tells(["discourse"])}
    assert {
        "negative_parallelism",
        "rule_of_three_padding",
        "challenges_section",
        "list_title_lead",
        "knowledge_cutoff",
        "didactic_disclaimer",
        "section_summary",
        "comm_boilerplate",
    } <= set(tells)
    assert all(tells[t].handling == "surface" for t in tells)


class TestVaultRelative:
    """Each gated tell fires for a plain user and is suppressed for an ornate one."""

    def test_negative_parallelism(self) -> None:
        """not-only/it's-not parallelisms flag for plain, not ornate."""
        text = "It's not only fast, but also cheap. It's not a bug, it's a feature."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "negative_parallelism"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "negative_parallelism"
        )

    def test_rule_of_three_padding(self) -> None:
        """Triad padding flags for a non-triad user, not a triad-loving one."""
        text = "Red, white, and blue. Cats, dogs, and birds. One, two, and three."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "rule_of_three_padding"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "rule_of_three_padding"
        )

    def test_challenges_section(self) -> None:
        """The 'Despite its X, Y faces challenges' formula flags for plain."""
        text = (
            "Despite its impressive scale, the Panama Canal faces challenges "
            "in the years ahead."
        )
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "challenges_section"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "challenges_section"
        )

    def test_list_title_lead(self) -> None:
        """A list/non-proper-noun title defined as an entity flags for plain."""
        text = "List of songs recorded by the band is a list of recordings."
        assert _fired(scan(text, fingerprint=_PLAIN, config=_CONFIG), "list_title_lead")
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "list_title_lead"
        )

    def test_didactic_disclaimer(self) -> None:
        """Didactic 'important to note / may vary' disclaimers flag for plain."""
        text = "It's important to note that results may vary."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "didactic_disclaimer"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "didactic_disclaimer"
        )

    def test_section_summary(self) -> None:
        """Sentence-initial In summary/In conclusion/Overall flags for plain."""
        text = "The work was hard. In summary, it succeeded."
        assert _fired(scan(text, fingerprint=_PLAIN, config=_CONFIG), "section_summary")
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "section_summary"
        )


class TestKnowledgeCutoffIsFingerprintIndependent:
    """Cutoff/'not documented' disclaimers fire regardless of the fingerprint."""

    def test_fires_for_plain_and_ornate(self) -> None:
        """It fires for an ornate user too; it is never desirable in prose."""
        text = "As of my last knowledge update, the subject maintains a low profile."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "knowledge_cutoff"
        )
        assert _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG), "knowledge_cutoff"
        )

    def test_finding_carries_fabrication_risk_hint(self) -> None:
        """The finding message warns about fabrication risk."""
        text = "While specific details are limited in the available sources, it grew."
        report = scan(text, fingerprint=_PLAIN, config=_CONFIG)
        assert "fabricat" in _message(report, "knowledge_cutoff").lower()


class TestCommentContext:
    """Collaborative-comm boilerplate is comment-context only."""

    def test_not_in_article(self) -> None:
        """Boilerplate in article prose does not fire."""
        text = "I hope this helps! Would you like me to expand it? Certainly!"
        report = scan(text, fingerprint=_PLAIN, config=_CONFIG, context="article")
        assert not _fired(report, "comm_boilerplate")

    def test_fires_in_comment(self) -> None:
        """Boilerplate fires in comment context for a plain user."""
        text = "I hope this helps! Would you like me to expand it? Certainly!"
        report = scan(text, fingerprint=_PLAIN, config=_CONFIG, context="comment")
        assert _fired(report, "comm_boilerplate")

    def test_suppressed_for_boilerplate_using_user(self) -> None:
        """The same comment is suppressed when the user's baseline is high."""
        text = "I hope this helps! Would you like me to expand it? Certainly!"
        report = scan(text, fingerprint=_ORNATE, config=_CONFIG, context="comment")
        assert not _fired(report, "comm_boilerplate")


def test_single_triad_below_margin_no_false_positive() -> None:
    """One triad in a long human text stays under the strongly-gated margin."""
    text = "Red, white, and blue forever. " + " ".join(["plain"] * 999)
    assert not _fired(
        scan(text, fingerprint=_PLAIN, config=_CONFIG), "rule_of_three_padding"
    )


def test_single_antithesis_below_margin_no_false_positive() -> None:
    """One deliberate antithesis in a long clean paragraph stays under the margin.

    The owner uses antithesis occasionally; a single rhetorical turn must not
    trip the surface margin (issue #516 calibration).
    """
    text = "We win not through force but through patience. " + " ".join(["plain"] * 999)
    assert not _fired(
        scan(text, fingerprint=_PLAIN, config=_CONFIG), "negative_parallelism"
    )


def test_saturated_antithesis_fires_for_plain_user() -> None:
    """A cluster of `not X but Y` antithesis flags for a plain user (issue #516)."""
    text = (
        "Not through bliss, necessarily, but through outrage. "
        "The parables weren't instruction manuals. They were field reports. "
        "The work isn't to save everyone. The work is to be findable. "
        "It doesn't make you easier to manipulate—it makes you ungovernable. "
        "It is not about winning, it is about showing up."
    )
    assert _fired(
        scan(text, fingerprint=_PLAIN, config=_CONFIG), "negative_parallelism"
    )


def test_findings_carry_a_span_and_message() -> None:
    """A fired discourse finding locates the offending text and hints why."""
    text = "The work was hard. In summary, it succeeded."
    report = scan(text, fingerprint=_PLAIN, config=_CONFIG)
    finding = next(f for f in report.findings if f.tell_id == "section_summary")
    assert finding.span.end > finding.span.start
    assert text[finding.span.start : finding.span.end].lower().startswith("in summary")
