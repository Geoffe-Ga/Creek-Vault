"""Compiled regex patterns for detecting sensitive data.

Each pattern is keyed by a human-readable name and compiled with
:func:`re.compile` for efficient repeated use.  The patterns are
intentionally conservative stubs — they cover the most common formats
and are designed to be extended via :pyattr:`RedactionConfig.custom_patterns`.
"""

import re

REDACTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "api_key": re.compile(
        r"(?:"
        # AWS access key IDs (always start with AKIA)
        r"AKIA[0-9A-Z]{16}"
        r"|"
        # OpenAI / Anthropic / Stripe style: sk-<prefix>-<alphanum20+>
        r"sk[-_][a-zA-Z0-9_-]{20,}"
        r")",
    ),
    "password": re.compile(
        r"(?i)(?:password|passwd)\s*=\s*\S+",
    ),
    "ssn": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b",
    ),
    "email": re.compile(
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
    ),
}
