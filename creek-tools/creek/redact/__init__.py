"""Creek redaction module — scan, flag, and replace sensitive data.

Provides the :class:`RedactionScanner` for detecting PII and secrets in
text files, the :class:`Redactor` for replacing matches with safe markers,
and the :data:`REDACTION_PATTERNS` dictionary of compiled regex patterns.

Sensitive matched text is **never** stored — only salted SHA-256 hashes are
retained so that duplicate detections can be correlated without leaking data.
"""

from creek.redact.patterns import REDACTION_PATTERNS
from creek.redact.redactor import Redactor
from creek.redact.scanner import RedactionMatch, RedactionScanner

__all__ = [
    "REDACTION_PATTERNS",
    "RedactionMatch",
    "RedactionScanner",
    "Redactor",
]
