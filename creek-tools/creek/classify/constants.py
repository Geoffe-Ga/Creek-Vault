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

CLASSIFICATION_PROVIDER_KEY: Final[str] = "classification_provider"
"""Frontmatter key naming the LLM provider behind an ``llm`` classification
(e.g. ``anthropic`` / ``ollama``). Lets a quality-aware re-run tell a local-LLM
classification apart from a cloud-LLM one; absent for ``rules`` / ``manual``."""

CLASSIFICATION_REASONING_KEY: Final[str] = "classification_reasoning"
"""Frontmatter key carrying the truncated LLM reasoning trace (FEAT-017).

Populated only by ``--method llm`` runs. ``open`` and ``personal``
fragments store a 400-char truncation here for auditability; ``intimate``
fragments persist an empty string and route the full trace to the
gitignored trace log instead (see :data:`CLASSIFY_TRACE_LOG_FILENAME`).
"""

CLASSIFICATION_REASONING_MAX_CHARS: Final[int] = 400
"""Maximum characters of LLM reasoning persisted to fragment frontmatter.

Longer traces are routed to the trace log instead so neither the
fragment file nor downstream renderers (Obsidian preview, MCP
endpoints) have to handle multi-kilobyte provenance strings inline.
"""

CLASSIFY_TRACE_LOG_FILENAME: Final[str] = "classify-llm-trace.jsonl"
"""Per-vault trace log filename (under ``00-Creek-Meta/Processing-Log/``).

Newline-delimited JSON, one record per LLM classification, carrying
``{id, tier, reasoning}``. For ``intimate`` fragments this is the
**only** place the reasoning trace lives — it never enters the
fragment's frontmatter. The log is gitignored (FEAT-019) so vault
sharing does not leak intimate-tier model traces.
"""
