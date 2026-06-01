"""Tests for the vault-relative rhetorical detectors (FEAT-040.6)."""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.tells import get_tells

_CONFIG = AIStyleConfig()

_KEYS = (
    "significance_phrase_density",
    "superficial_ing_density",
    "peacock_density",
    "vague_attribution_density",
)
_PLAIN = VoiceFingerprint(
    features={k: FeatureStat(rate=0.0, support=50) for k in _KEYS},
    fragment_count=50,
)
_ORNATE = VoiceFingerprint(
    features={k: FeatureStat(rate=900.0, support=50) for k in _KEYS},
    fragment_count=50,
)


def _fired(report, tell_id: str) -> bool:
    return any(f.tell_id == tell_id for f in report.findings)


def test_rhetorical_tells_registered_and_surface() -> None:
    """All four rhetorical tells are registered and are surface-handled."""
    tells = {t.id: t for t in get_tells(["rhetorical"])}
    assert {
        "significance_puffery",
        "superficial_ing",
        "peacock_language",
        "vague_attribution",
    } <= set(tells)
    assert all(tells[t].handling == "surface" for t in tells)


class TestVaultRelative:
    """Each tell fires above the user's baseline and is suppressed at/above it."""

    def test_significance_puffery(self) -> None:
        """Significance/legacy phrasing flags for a plain user, not an ornate one."""
        text = (
            "This marks a pivotal moment, contributing to the broader movement "
            "and standing as an enduring legacy of the work."
        )
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "significance_puffery"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "significance_puffery",
        )

    def test_superficial_ing(self) -> None:
        """Trailing -ing analysis clauses flag for plain, not ornate."""
        text = (
            "The town grew, highlighting its role, reflecting its values, "
            "and underscoring its reach."
        )
        assert _fired(scan(text, fingerprint=_PLAIN, config=_CONFIG), "superficial_ing")
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "superficial_ing",
        )

    def test_peacock(self) -> None:
        """Peacock/travel-guide language flags for plain, not ornate."""
        text = "A vibrant, bustling town nestled in the heart of breathtaking hills."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "peacock_language"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "peacock_language",
        )

    def test_vague_attribution(self) -> None:
        """Weasel attribution flags for plain, not ornate."""
        text = "Experts argue this. Some scholars note that. Several sources agree."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG), "vague_attribution"
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "vague_attribution",
        )


def test_single_significance_phrase_below_margin_no_false_positive() -> None:
    """One significance phrase in a long human text stays under the margin."""
    text = "This was a pivotal moment. " + " ".join(["plain"] * 999)
    assert not _fired(
        scan(text, fingerprint=_PLAIN, config=_CONFIG),
        "significance_puffery",
    )


def test_findings_carry_a_span_and_message() -> None:
    """A fired rhetorical finding locates the offending text and hints why."""
    text = "A vibrant, bustling town nestled in the heart of breathtaking hills."
    report = scan(text, fingerprint=_PLAIN, config=_CONFIG)
    peacock = next(f for f in report.findings if f.tell_id == "peacock_language")
    assert peacock.span.end > peacock.span.start
    assert (
        "do not strip" in peacock.message or "relative to your own" in peacock.message
    )
