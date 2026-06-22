"""Per-user polarity derivation + one-line-fragment signature (#635).

The scrub must reinforce authentic signatures, not sand them off. These tests
pin: (1) polarity is derived per-user — a feature the user characteristically
uses becomes a ``signature`` so stripping it RAISES voice distance; (2) the new
one-line-fragment extractor; (3) existing avoid-tell behaviour and the
curly-quote/em-dash typography protection are preserved.
"""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.features import one_line_fragment_density
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.sanitize_typography import normalize_typography
from creek.generate.ai_style.tells import (
    PLACEHOLDER_DATE,
    TELL_REGISTRY,
    Tell,
    effective_polarity,
)

_KEY = "one_line_fragment_density"

# A punchy draft: short single-sentence paragraphs, the user's signature beat.
_PUNCHY = (
    "I went down to the water.\n\nIt was cold.\n\nI stayed anyway.\n\nThe light moved."
)
# The same content flattened into one dense block — the signature stripped out.
_FLATTENED = (
    "I went down to the water and it was cold but I stayed anyway while the "
    "light moved across the surface in the way it always does at that hour."
)


def _fingerprint(**rates: float) -> VoiceFingerprint:
    """Build a non-thin fingerprint with the given per-feature rates."""
    return VoiceFingerprint(
        features={k: FeatureStat(rate=v, support=50) for k, v in rates.items()},
        fragment_count=50,
    )


def _finding(report, tell_id: str):
    """Return the first finding for *tell_id*, or ``None``."""
    return next((f for f in report.findings if f.tell_id == tell_id), None)


# --- effective_polarity (unit) ---------------------------------------------


class TestEffectivePolarity:
    """Polarity is derived from the user's rate, with locked artifacts exempt."""

    def test_heavy_user_makes_signature(self) -> None:
        """A rate above threshold flips the concern to under-use."""
        tell = TELL_REGISTRY["one_line_fragment"]
        assert effective_polarity(tell, 9.0, signature_threshold=3.0) == "signature"

    def test_threshold_boundary_is_inclusive(self) -> None:
        """``user_rate == threshold`` counts as a signature (``>=``, not ``>``)."""
        tell = TELL_REGISTRY["one_line_fragment"]
        assert effective_polarity(tell, 3.0, signature_threshold=3.0) == "signature"

    def test_lock_polarity_flag_forces_declared(self) -> None:
        """An explicit ``lock_polarity`` keeps declared polarity below threshold.

        Covers the ``lock_polarity=True`` branch independently of the
        ``margin == 0.0`` branch: without the lock this signature tell would be
        inert (``None``) at a sub-threshold rate.
        """
        locked = Tell(
            id="_test_locked_signature",
            category="rhetorical",
            feature_key="_test_locked",
            handling="surface",
            polarity="signature",
            description="test",
            caveat="test",
            measure=lambda _text: 0.0,
            locate=lambda _text: [],
            lock_polarity=True,
        )
        assert locked.margin is None
        assert locked.polarity_is_locked()
        assert effective_polarity(locked, 0.0, signature_threshold=3.0) == "signature"

    def test_avoid_tell_never_reinforced(self) -> None:
        """An AI-tell stays ``avoid`` even when the user over-uses it."""
        tell = TELL_REGISTRY["negative_parallelism"]
        assert effective_polarity(tell, 0.0, signature_threshold=3.0) == "avoid"
        # Heavy use does NOT reinforce a catalog tell (no triad-padding revival).
        assert effective_polarity(tell, 900.0, signature_threshold=3.0) == "avoid"

    def test_signature_tell_inert_below_threshold(self) -> None:
        """A signature feature the user does not employ is inert (no firing)."""
        tell = TELL_REGISTRY["one_line_fragment"]
        assert effective_polarity(tell, 0.0, signature_threshold=3.0) is None

    def test_never_legitimate_tell_is_locked(self) -> None:
        """A margin-0 artifact stays ``avoid`` even at a huge user rate."""
        assert PLACEHOLDER_DATE.polarity_is_locked()
        assert (
            effective_polarity(PLACEHOLDER_DATE, 999.0, signature_threshold=3.0)
            == "avoid"
        )


# --- one_line_fragment_density extractor (acceptance #2) -------------------


class TestOneLineFragmentDensity:
    """The new signature extractor counts short single-line paragraphs."""

    def test_counts_short_one_liners(self) -> None:
        """Several short single-line paragraphs yield a positive rate."""
        assert one_line_fragment_density(_PUNCHY) > 0.0

    def test_flattened_block_scores_zero(self) -> None:
        """One dense multi-line-free block is not a one-line-fragment rhythm."""
        assert one_line_fragment_density(_FLATTENED) == 0.0

    def test_long_single_line_paragraph_not_counted(self) -> None:
        """A single line over the word ceiling is a paragraph, not a beat."""
        long_line = " ".join(["word"] * 40)
        assert one_line_fragment_density(long_line) == 0.0


# --- scanner regression (acceptance #1) ------------------------------------


class TestSignatureReinforcement:
    """Stripping a user's over-used signature raises voice distance."""

    def test_stripping_signature_raises_distance(self) -> None:
        """A flattened draft diverges *more* than a punchy one for a punchy user."""
        fp = _fingerprint(**{_KEY: 12.0})
        config = AIStyleConfig()

        punchy = scan(_PUNCHY, fingerprint=fp, config=config)
        flattened = scan(_FLATTENED, fingerprint=fp, config=config)

        # The flattened draft under-uses the signature, so it is the divergent
        # one — the whole point of #635.
        assert flattened.voice_distance > punchy.voice_distance
        under = _finding(flattened, "one_line_fragment")
        assert under is not None
        assert under.direction == "under"
        # And the matching punchy draft is not flagged for the signature.
        assert _finding(punchy, "one_line_fragment") is None

    def test_derivation_gated_by_threshold(self) -> None:
        """Raise the threshold above the user's rate and the flip does not happen."""
        fp = _fingerprint(**{_KEY: 12.0})
        # Threshold above the user's 12.0 rate => feature is not a signature,
        # so under-use is harmless and nothing fires (old avoid behaviour).
        config = AIStyleConfig(signature_polarity_threshold=100.0)
        flattened = scan(_FLATTENED, fingerprint=fp, config=config)
        assert _finding(flattened, "one_line_fragment") is None


# --- existing protections preserved (acceptance #3) ------------------------


class TestExistingProtectionsPreserved:
    """Avoid-tell firing and typography protection still work under derivation."""

    def test_avoid_tell_still_fires_for_non_signature_user(self) -> None:
        """A user who avoids a feature still gets over-use flagged."""
        fp = _fingerprint(negative_parallelism_density=0.0)
        text = "It's not only fast, but also cheap. It's not a bug, it's a feature."
        report = scan(text, fingerprint=fp, config=AIStyleConfig())
        firing = _finding(report, "negative_parallelism")
        assert firing is not None
        assert firing.direction == "over"

    def test_curly_quotes_straightened_when_user_avoids_them(self) -> None:
        """Typography normalisation still strips curly quotes off-grain."""
        fp = _fingerprint(curly_quote_density=0.0)
        out = normalize_typography(
            "She said “hello” to me.",
            fingerprint=fp,
            config=AIStyleConfig(),
        )
        assert "“" not in out and "”" not in out

    def test_curly_quotes_preserved_when_user_uses_them(self) -> None:
        """A user whose voice uses curly quotes keeps them untouched."""
        fp = _fingerprint(curly_quote_density=20.0)
        original = "She said “hello” to me."
        out = normalize_typography(original, fingerprint=fp, config=AIStyleConfig())
        assert out == original
