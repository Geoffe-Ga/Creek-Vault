"""Audit log for ``creek redact --apply`` operations.

Every committed (or dry-run-previewed) redaction writes one entry per
touched file to the tamper-evident JSONL log at
``<vault>/00-Creek-Meta/audit/redact.jsonl``. The log is backed by
:class:`creek.audit.AuditLog`, so the same hash-chain integrity that
guards purge and privacy-override events also guards redactions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from creek.audit import AuditLog

REDACT_AUDIT_RELPATH = Path("00-Creek-Meta/audit/redact.jsonl")
"""Canonical redaction audit log location under the vault root."""


class RedactionAuditEntry(BaseModel):
    """One audit record for a single redacted file.

    Attributes:
        timestamp: UTC ISO-format datetime of the apply event.
        source_path: The file path whose contents were rewritten,
            stored as a string for portability.
        pattern_names: Pattern names that matched in the file.
        match_counts: Per-pattern hit count.
        replacement_template: Marker template used to replace each hit
            (``[REDACTED:{name}]``).
        operator: Who initiated the apply.
        dry_run: ``True`` when the file would have been rewritten but
            was preserved by ``--dry-run``.
    """

    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
    )
    source_path: str
    pattern_names: list[str] = Field(default_factory=list)
    match_counts: dict[str, int] = Field(default_factory=dict)
    replacement_template: str = "[REDACTED:{name}]"
    operator: str = "human via CLI"
    dry_run: bool = False


class RedactionAuditLog:
    """Append-only redaction audit log rooted at a vault path.

    Args:
        vault_path: Vault root under which the audit JSONL lives.
    """

    def __init__(self, vault_path: Path) -> None:
        """Resolve the canonical audit path under *vault_path*."""
        self.vault_path = vault_path
        self.log_path = vault_path / REDACT_AUDIT_RELPATH
        self._audit = AuditLog(self.log_path)

    def append(self, entry: RedactionAuditEntry) -> None:
        """Append a :class:`RedactionAuditEntry` to the chained log."""
        self._audit.append(entry.model_dump(mode="json"))

    def read(self) -> list[RedactionAuditEntry]:
        """Return every audit entry parsed back into the model."""
        entries: list[RedactionAuditEntry] = []
        for raw in self._audit.read():
            payload: dict[str, Any] = {k: v for k, v in raw.items() if k != "prev_hash"}
            entries.append(RedactionAuditEntry.model_validate(payload))
        return entries

    def verify(self) -> None:
        """Verify the audit log's hash chain.

        Raises:
            creek.audit.AuditChainBroken: When the chain is broken.
        """
        self._audit.verify()
