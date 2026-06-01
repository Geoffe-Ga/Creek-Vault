"""FEAT-040 AI-style / voice-fidelity subsystem.

Measures how far generated text diverges from the *user's own* writing
(their voice fingerprint) rather than from a generic baseline, and scores
that divergence as a bounded ``voice_distance``. This package ships the
framework (model, tell registry, scan engine, distance score, calibration
harness) plus a single seed tell; later FEAT-040 issues register the real
catalogs and add the sanitizer, prompt prevention, guard, and lint check.
"""

from __future__ import annotations

from creek.generate.ai_style.distance import (
    FeatureContribution,
    bad_direction_magnitude,
    voice_distance,
)
from creek.generate.ai_style.features import FINGERPRINT_FEATURES
from creek.generate.ai_style.fingerprint import (
    build_fingerprint,
    extract_user_turns,
    load_fingerprint,
    save_fingerprint,
)
from creek.generate.ai_style.lexical import (
    AI_VOCABULARY,
    CONCRETE_COMMENT,
    COPULATIVE_AVOIDANCE,
    TRANSITION_OPENER,
)
from creek.generate.ai_style.model import (
    Category,
    Direction,
    FeatureStat,
    Finding,
    ScanReport,
    Span,
    VoiceFingerprint,
)
from creek.generate.ai_style.sanitize_structure import sanitize_structure
from creek.generate.ai_style.sanitize_typography import (
    normalize_typography,
    sanitize,
    strip_markup_artifacts,
)
from creek.generate.ai_style.scanner import scan
from creek.generate.ai_style.tells import (
    TELL_REGISTRY,
    Tell,
    get_tells,
    rate_per_kwords,
    register,
    word_count,
)

__all__ = [
    "AI_VOCABULARY",
    "CONCRETE_COMMENT",
    "COPULATIVE_AVOIDANCE",
    "FINGERPRINT_FEATURES",
    "TELL_REGISTRY",
    "TRANSITION_OPENER",
    "Category",
    "Direction",
    "FeatureContribution",
    "FeatureStat",
    "Finding",
    "ScanReport",
    "Span",
    "Tell",
    "VoiceFingerprint",
    "bad_direction_magnitude",
    "build_fingerprint",
    "extract_user_turns",
    "get_tells",
    "load_fingerprint",
    "normalize_typography",
    "rate_per_kwords",
    "register",
    "sanitize",
    "sanitize_structure",
    "save_fingerprint",
    "scan",
    "strip_markup_artifacts",
    "voice_distance",
    "word_count",
]
