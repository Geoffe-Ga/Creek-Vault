"""Audit log for ``creek redact --apply`` operations.

Every ``creek redact --apply`` run — committed or ``--dry-run`` — writes
a three-phase record to the tamper-evident JSONL log at
``<vault>/00-Creek-Meta/audit/redact.jsonl``:

* one ``intent`` entry naming every candidate file, written *before* the
  first byte is rewritten;
* one ``file`` entry per file, appended immediately after that file's
  own atomic write;
* one ``outcome`` entry closing the run, ``status="complete"`` when the
  batch finished and ``status="partial"`` when it aborted.

All three share one ``operation_id`` so a reader can group them. The log
is backed by :class:`creek.audit.AuditLog`, so the same hash-chain
integrity that guards purge and privacy-override events also guards
redactions.

What a reader may conclude, and no more (#1308): every file carrying a
``file`` entry *was* rewritten; at most ONE further file may have been
rewritten without its record (the one in flight when the run died); and
nothing outside the ``intent`` entry's ``files`` list was touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from creek.audit import AuditLog

REDACT_AUDIT_RELPATH = Path("00-Creek-Meta/audit/redact.jsonl")
"""Canonical redaction audit log location under the vault root."""

RedactionPhase = Literal["intent", "file", "outcome"]
"""Which of the three record kinds an entry is."""

RedactionOutcomeStatus = Literal["complete", "partial"]
"""Whether a run's batch finished or aborted partway."""


class RedactionAuditEntry(BaseModel):
    """One record in the redaction audit log.

    Attributes:
        timestamp: UTC ISO-format datetime of the apply event.
        source_path: The file path whose contents were rewritten,
            stored as a string for portability. ``None`` on ``intent``
            and ``outcome`` entries, which describe a run rather than a
            file; required on ``file`` entries and enforced by
            :meth:`_file_entries_must_name_their_file`.
        pattern_names: Pattern names that matched in the file.
        match_counts: Per-pattern hit count **as the scan found it** —
            not a count of substitutions actually performed. Scan/apply
            parity is a separate open gap (#900, #946).
        replacement_template: Marker template used to replace each hit
            (``[REDACTED:{name}]``).
        operator: Who initiated the apply.
        dry_run: ``True`` when the file would have been rewritten but
            was preserved by ``--dry-run``.
        phase: Record kind. ``"intent"`` is written before any file is
            rewritten, ``"file"`` immediately after each rewrite,
            ``"outcome"`` last. Pre-#1308 entries default to ``"file"``
            because that is exactly what they always were — note this
            differs from :mod:`creek.purge.audit`, whose legacy lines
            were outcomes and which therefore defaults to ``"outcome"``.
        operation_id: UUID4 shared by the ``intent``, every ``file`` and
            the ``outcome`` entry of one run, so a reader can attribute
            per-file entries to their run and pair intent with outcome.
            Empty string for pre-#1308 entries.
        status: For ``outcome`` entries only — ``"complete"`` when the
            batch ran to the end, ``"partial"`` when it aborted.
            ``None`` elsewhere.
        failure_reason: The exception **type name only** (e.g.
            ``"OSError"``) when ``status="partial"``; ``None``
            otherwise. The message is deliberately excluded: ``str`` of
            an ``OSError`` embeds the offending path, and in a redaction
            workflow filenames routinely carry the very secrets being
            redacted. The type is the forensic value; the message is the
            leak — the same argument :meth:`creek.purge.engine.
            PurgeEngine._run_audited` already makes.
        files: On an ``intent`` entry, every candidate path the run is
            about to rewrite. Empty elsewhere. This is the containment
            bound a compliance reader relies on when the run died before
            its outcome line.
    """

    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
    )
    source_path: str | None = None
    pattern_names: list[str] = Field(default_factory=list)
    match_counts: dict[str, int] = Field(default_factory=dict)
    replacement_template: str = "[REDACTED:{name}]"
    operator: str = "human via CLI"
    dry_run: bool = False

    phase: RedactionPhase = "file"
    operation_id: str = ""
    status: RedactionOutcomeStatus | None = None
    failure_reason: str | None = None
    files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _file_entries_must_name_their_file(self) -> RedactionAuditEntry:
        """Keep the invariant a required ``source_path`` used to carry.

        ``source_path`` had to become optional so ``intent`` and
        ``outcome`` entries — which describe a run, not a file — can
        omit it. Without this check that change would silently permit a
        per-file record naming no file, which certifies nothing.

        Returns:
            The validated entry.

        Raises:
            ValueError: When a ``phase="file"`` entry has no
                ``source_path``.
        """
        if self.phase == "file" and self.source_path is None:
            msg = "a phase='file' audit entry must name its source_path"
            raise ValueError(msg)
        return self


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
        """Append a :class:`RedactionAuditEntry` to the chained log.

        ``None`` fields are omitted rather than serialised as ``null``,
        matching :meth:`creek.purge.audit.PurgeAuditLog.append`. Without
        it every per-file line would carry ``"status": null`` and
        ``"failure_reason": null``, inviting a reader to think those
        fields mean something for a file record. Omission is also how
        pre-#1308 lines already look, so old and new entries stay
        directly comparable.

        Args:
            entry: The record to append.
        """
        self._audit.append(entry.model_dump(mode="json", exclude_none=True))

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
