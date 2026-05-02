"""Shared frontmatter keys for fragment classification provenance.

These string literals live on every fragment's YAML frontmatter and are
read or written by the classification engine, the review-queue runner,
and any future consumer that needs to know how a fragment's
classification was last produced. Keeping them in a single module
prevents the keys from drifting apart silently — a rename in one
writer that the other writer (or any reader) doesn't pick up would
make manual decisions silently re-enter the review queue.
"""

from __future__ import annotations

from typing import Final

CLASSIFICATION_METHOD_KEY: Final[str] = "classification_method"
"""Frontmatter key carrying ``rules | llm | manual``."""

CLASSIFIED_AT_KEY: Final[str] = "classified_at"
"""Frontmatter key carrying the ISO-8601 classification timestamp."""

MANUAL_METHOD: Final[str] = "manual"
"""Sentinel ``classification_method`` value reserved for human review."""

RULES_METHOD: Final[str] = "rules"
"""``classification_method`` value emitted by the rule classifier."""

LLM_METHOD: Final[str] = "llm"
"""``classification_method`` value emitted by the LLM classifier."""
