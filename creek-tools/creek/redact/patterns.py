"""Compiled regex patterns for detecting sensitive data.

Each pattern is keyed by a human-readable name and compiled with
:func:`re.compile` for efficient repeated use.  The patterns are
intentionally conservative — they cover the most common formats
and are designed to be extended via :pyattr:`RedactionConfig.custom_patterns`.

Pattern metadata (severity, description, false-positive notes) is available
in :data:`PATTERN_METADATA` for reporting and documentation.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PatternInfo:
    """Metadata for a redaction pattern.

    Attributes:
        pattern: The compiled regex pattern.
        description: Human-readable description of what the pattern detects.
        severity: Risk level — ``critical``, ``high``, ``medium``, or ``low``.
        false_positive_notes: Notes about common false positives.
    """

    pattern: re.Pattern[str]
    description: str
    severity: str
    false_positive_notes: str

    def __post_init__(self) -> None:
        """Validate severity is one of the allowed values.

        Raises:
            ValueError: If severity is not in ``_VALID_SEVERITIES``.
        """
        if self.severity not in _VALID_SEVERITIES:
            msg = (
                f"Invalid severity {self.severity!r}; "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )
            raise ValueError(msg)


_VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})

PATTERN_METADATA: dict[str, PatternInfo] = {
    "api_key": PatternInfo(
        pattern=re.compile(
            r"(?:" r"AKIA[0-9A-Z]{16}" r"|" r"sk[-_][a-zA-Z0-9_-]{20,}" r")",
        ),
        description="API keys: AWS access key IDs (AKIA...) and sk-style tokens.",
        severity="critical",
        false_positive_notes="Short sk- prefixed strings in non-code contexts.",
    ),
    "password": PatternInfo(
        pattern=re.compile(
            r"(?i)(?:password|passwd)\s*=\s*\S+",
        ),
        description="Password assignments in configuration or code.",
        severity="critical",
        false_positive_notes=("Documentation examples with placeholder passwords."),
    ),
    "ssn": PatternInfo(
        pattern=re.compile(
            r"\b\d{3}-\d{2}-\d{4}\b",
        ),
        description="US Social Security Numbers in XXX-XX-XXXX format.",
        severity="critical",
        false_positive_notes=(
            "Date-like strings or version numbers with similar format."
        ),
    ),
    "email": PatternInfo(
        pattern=re.compile(
            r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
        ),
        description="Email addresses.",
        severity="medium",
        false_positive_notes=("Decorative @ symbols or scoped package names."),
    ),
    "credit_card": PatternInfo(
        pattern=re.compile(
            r"\b(?:"
            r"4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"
            r"|"
            r"5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"
            r"|"
            r"3[47]\d{2}[-\s]?\d{6}[-\s]?\d{5}"
            r"|"
            r"6(?:011|5\d{2}|4[4-9]\d)[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"
            r")\b",
        ),
        description=("Credit card numbers: Visa, Mastercard, Amex, Discover."),
        severity="critical",
        false_positive_notes=("Random 16-digit numbers; test card numbers (4111...)."),
    ),
    "email_password_combo": PatternInfo(
        pattern=re.compile(
            r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}:[^\s]+",
        ),
        description="Email:password credential pairs (leaked format).",
        severity="critical",
        false_positive_notes=("URLs with @ and : in authority section."),
    ),
    "aws_secret_key": PatternInfo(
        pattern=re.compile(
            r"(?i)"
            r"(?:aws_secret_access_key|aws_secret)"
            r"\s*=\s*[A-Za-z0-9/+=]{40}",
        ),
        description="AWS secret access keys in config assignments.",
        severity="critical",
        false_positive_notes=("Base64-encoded strings of exactly 40 characters."),
    ),
    "private_key": PatternInfo(
        pattern=re.compile(
            r"-----BEGIN\s+"
            r"(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?"
            r"PRIVATE\s+KEY-----",
        ),
        description="PEM private key header lines.",
        severity="critical",
        false_positive_notes=("Documentation or examples showing key formats."),
    ),
    "bearer_token": PatternInfo(
        pattern=re.compile(
            r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]{8,}=*",
        ),
        description="Bearer tokens in Authorization headers.",
        severity="high",
        false_positive_notes=(
            "The word 'Bearer' in documentation without a real token."
        ),
    ),
    "env_secret": PatternInfo(
        pattern=re.compile(
            r"(?i)"
            r"(?:export\s+)?"
            r"(?:SECRET|TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY)"
            r"[A-Z_]*\s*=\s*[^\s]+",
        ),
        description="Environment variable secrets (export KEY=value).",
        severity="high",
        false_positive_notes=(
            "Shell scripts setting non-secret env vars with similar names."
        ),
    ),
    "slack_token": PatternInfo(
        pattern=re.compile(
            r"xox[bpsa]-[0-9a-zA-Z\-]{10,}",
        ),
        description="Slack API tokens (xoxb-, xoxp-, xoxs-, xoxa-).",
        severity="critical",
        false_positive_notes="Rare; format is distinctive.",
    ),
    "phone_number": PatternInfo(
        pattern=re.compile(
            r"(?<!\d)"
            r"(?:"
            r"\(\d{3}\)\s?\d{3}[-.\s]\d{4}"
            r"|"
            r"\d{3}[-.\s]\d{3}[-.\s]\d{4}"
            r")"
            r"(?!\d)",
        ),
        description="US phone numbers in common formats.",
        severity="medium",
        false_positive_notes=("SSN-like patterns; random digit sequences."),
    ),
    "github_token": PatternInfo(
        pattern=re.compile(
            r"gh[pousr]_[A-Za-z0-9_]{36,}",
        ),
        description="GitHub personal access tokens and app tokens.",
        severity="critical",
        false_positive_notes="Rare; prefix format is distinctive.",
    ),
    "jwt": PatternInfo(
        pattern=re.compile(
            r"eyJ[A-Za-z0-9_-]{10,}" r"\.eyJ[A-Za-z0-9_-]{10,}" r"\.[A-Za-z0-9_-]{10,}",
        ),
        description="JSON Web Tokens (three base64url-encoded segments).",
        severity="high",
        false_positive_notes="Long base64-encoded strings with dots.",
    ),
}

REDACTION_PATTERNS: dict[str, re.Pattern[str]] = {
    name: info.pattern for name, info in PATTERN_METADATA.items()
}
"""Flat mapping of pattern name to compiled regex, for backward compatibility."""
