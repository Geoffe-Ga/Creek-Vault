"""Shipped positive examples (AI-authored snippets) for calibration.

Drawn from the Wikipedia field guide's quoted examples. This framework
issue (FEAT-040.1) ships only the seed tell (placeholder dates), so the
examples here are the ones that seed can detect; later issues
(FEAT-040.3 through .7) extend both the tell catalog and this list so the
detection rate measured by :func:`creek.generate.ai_style.calibration.calibrate`
covers the full catalog.

The negative set is NOT shipped: it is the *user's own* writing, supplied
by the caller (issue #419's profiler). The headline calibration metric is
that the negative set must not flag.
"""

from __future__ import annotations

AI_EXAMPLES: list[str] = [
    # Unfilled citation access-date placeholders (guide: Markup §).
    "Canadian Screen Music Awards 2025 Winners and Nominees, "
    "access-date 2025-XX-XX, retrieved from the awards site.",
    "The platform launched in 2022-11-XX according to the deputy report, "
    "with the access-date recorded as 2024-xx-xx in the citation.",
]
"""AI-authored snippets the current tell catalog should flag."""
