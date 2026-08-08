"""Audit log for purge operations.

Records every purge operation — including dry runs — to a tamper-
evident JSONL log at ``<vault>/00-Creek-Meta/audit/purge.jsonl``. Each
entry captures the timestamp, operation, criteria dict, affected
fragment IDs, deletion counts, references scrubbed, embeddings removed,
operator, and dry-run flag.

Phase pairing (GAP-002): every purge operation emits **two** entries —
an ``intent`` line written *before* the first destructive op, then an
``outcome`` line written *after* the destructive section completes
(``status="complete"``) or after an exception aborts it
(``status="partial"``). Both entries share a UUID4 ``operation_id`` so
a recovery tool can pair them. Pre-GAP-002 entries have no ``phase``
field; they read back as ``phase="outcome"`` with an empty
``operation_id`` and ``status=None``.

Timezone (BUG-002): purge-audit entries deliberately stamp ``UTC``
rather than ``America/Los_Angeles`` (the rest of the pipeline). The
audit chain is forensic infrastructure — its consumers are operators
investigating an incident, log-aggregation tooling, and downstream
pipelines that may run on hosts in any timezone. UTC is the portable
default for that audience and is what every other tamper-evident log
in the repo (vault-writer provenance, redaction audit, privacy
elevation audit) also uses. Fragment / thread / eddy timestamps stay
on LA per ontology §8.3 because they describe the user's lived
experience; audit timestamps describe a machine event.

Backward compatibility: an existing legacy
``<vault>/00-Creek-Meta/Processing-Log/purge-log.json`` file is migrated
into the new JSONL log on first read or write, then removed. The
migration writes one entry per legacy record plus a final
``purge.audit.migration`` marker, so the chain captures the move.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from creek.audit import AuditLog

logger = logging.getLogger(__name__)

_DEFAULT_OPERATOR = "human via CLI"

LEGACY_PURGE_LOG_RELPATH = Path("00-Creek-Meta/Processing-Log/purge-log.json")
"""Pre-Batch-C purge log location used for one-time migration."""

PURGE_AUDIT_RELPATH = Path("00-Creek-Meta/audit/purge.jsonl")
"""Canonical Batch-C purge audit log location."""

PurgePhase = Literal["intent", "outcome"]
"""GAP-002 phase discriminator on each purge audit entry."""

PurgeOutcomeStatus = Literal["complete", "partial"]
"""``outcome`` entries set this to record whether the body ran to completion."""


class PurgeAuditEntry(BaseModel):
    """A single purge audit log entry.

    Attributes:
        timestamp: UTC ISO-format datetime of the purge.
        operation: Operation name (``fragment``, ``source``,
            ``classifications``, ``daterange``, ``vault``).
        criteria: Structured input that drove the purge (e.g. fragment
            ID, source platform, date range).
        affected_fragments: Fragment IDs touched by the operation.
        fragments_deleted: Number of fragment files removed from disk.
        references_scrubbed: Number of wiki-link references removed.
        embeddings_removed: Real number of rows dropped from
            ``<vault>/00-Creek-Meta/embeddings.parquet`` by the purge
            (GAP-001). Zero when the cache had not been built yet or
            when no rows matched; the actual row delta otherwise.
        provenance_scrubbed: Number of bare fragment-ID mentions
            replaced with ``[purged]`` across derived content (GAP-004).
            Counts YAML provenance lists (e.g. ``source_fragments`` in
            drafts) plus body-text mentions of the ID. Wiki-link
            removals stay on ``references_scrubbed``.
        intimate_stubs_removed: Number of intimate-body stub files
            deleted under ``10-Liminal/Compost/intimate-stubs/`` because
            a purged note pointed at them via
            ``saved_from.intimate_body_pointer`` (GAP-012). Zero for
            notes that carry no pointer, dry-runs count what *would* be
            removed, and an already-missing stub does not increment it.
        journal_staged_removed: Number of staged Adepthood source files
            deleted under ``00-Creek-Meta/adepthood/journal/`` or
            ``00-Creek-Meta/adepthood/uploads/`` because the fragment
            they produced was purged, or because a whole-vault purge
            swept the staging dirs (issues #845, #1023). Zero for
            fragments with no ``source.origin_key``; dry-runs count
            what *would* be removed. The field keeps its journal-era
            name because it is serialised into the append-only
            ``purge.jsonl``, where a rename would break every existing
            log.
        operator: Who performed the purge.
        dry_run: Whether the purge was a dry-run preview.
        phase: GAP-002 discriminator. ``"intent"`` is written before
            any destructive op; ``"outcome"`` is written after. Pre-
            GAP-002 entries default to ``"outcome"`` since that's what
            they always semantically were.
        operation_id: UUID4 that pairs an intent entry with its
            matching outcome. Empty string for pre-GAP-002 entries.
        status: For outcome entries only — ``"complete"`` if the body
            ran without raising, ``"partial"`` if it aborted partway.
            ``None`` for intent entries and for pre-GAP-002 outcomes.
        failure_reason: The exception **type name only** (e.g.
            ``"OSError"``) when ``status="partial"``; ``None``
            otherwise. The message is deliberately excluded: this log
            is preserved by every purge, so vault-derived text quoted
            in an exception would outlive the right-to-be-forgotten
            request that produced it.
        target: Legacy field preserved for backward compatibility on
            read; populated only when reading pre-Batch-C entries.
        count: Legacy fragment count preserved for backward compatibility
            on read.
    """

    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
    )
    operation: str
    criteria: dict[str, Any] = Field(default_factory=dict)
    affected_fragments: list[str] = Field(default_factory=list)
    fragments_deleted: int = 0
    references_scrubbed: int = 0
    embeddings_removed: int = 0
    provenance_scrubbed: int = 0
    intimate_stubs_removed: int = 0
    journal_staged_removed: int = 0
    operator: str = _DEFAULT_OPERATOR
    dry_run: bool = False

    phase: PurgePhase = "outcome"
    operation_id: str = ""
    status: PurgeOutcomeStatus | None = None
    failure_reason: str | None = None

    target: str | None = None
    count: int | None = None


def _coerce_legacy_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a pre-Batch-C entry shape into the current schema.

    Pre-Batch-C entries carried ``target`` (str) and ``count`` (int).
    The new schema uses ``criteria`` (dict) and ``fragments_deleted``.
    Both old fields are preserved on the resulting entry so a downstream
    consumer that still expects them does not break.
    """
    if "criteria" in raw and "affected_fragments" in raw:
        return raw
    upgraded = raw.copy()
    target = raw.get("target")
    if target is not None and "criteria" not in raw:
        upgraded["criteria"] = {"target": target}
    if "count" in raw and "fragments_deleted" not in raw:
        upgraded["fragments_deleted"] = int(raw.get("count", 0) or 0)
    upgraded.setdefault("affected_fragments", [])
    upgraded.setdefault("references_scrubbed", 0)
    upgraded.setdefault("embeddings_removed", 0)
    upgraded.setdefault("provenance_scrubbed", 0)
    upgraded.setdefault("intimate_stubs_removed", 0)
    upgraded.setdefault("journal_staged_removed", 0)
    # GAP-002 fields take their schema defaults via Pydantic when
    # absent, so we leave them out here rather than fabricating a
    # phase / operation_id that doesn't correspond to anything on disk.
    return upgraded


class PurgeAuditLog:
    """Tamper-evident JSONL audit log for purge operations.

    The log lives at :data:`PURGE_AUDIT_RELPATH` under the vault root
    and is backed by :class:`creek.audit.AuditLog`, so every append
    extends a sha256 hash chain that :meth:`creek.audit.AuditLog.verify`
    can validate.

    Args:
        vault_path: Root of the Obsidian vault.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialise the audit log with the target vault path.

        Args:
            vault_path: Root of the Obsidian vault.
        """
        self.vault_path = vault_path
        self.log_path = vault_path / PURGE_AUDIT_RELPATH
        self._legacy_path = vault_path / LEGACY_PURGE_LOG_RELPATH
        self._audit = AuditLog(self.log_path)
        # Flip to True after a successful migration (or when there is
        # nothing to migrate) so subsequent append/read calls skip the
        # filesystem stat that the legacy-detection path would
        # otherwise perform on every operation. Stays False until the
        # first append/read so callers that construct the wrapper
        # without ever touching it pay no migration cost.
        self._migration_settled = False

    def append(self, entry: PurgeAuditEntry) -> None:
        """Append a new entry to the audit log.

        Args:
            entry: The audit entry to append.
        """
        self._migrate_legacy_if_needed()
        payload = entry.model_dump(mode="json", exclude_none=True)
        self._audit.append(payload)

    def read(self) -> list[PurgeAuditEntry]:
        """Read all entries from the audit log.

        Returns:
            List of parsed :class:`PurgeAuditEntry` objects in append
            order.
        """
        self._migrate_legacy_if_needed()
        entries: list[PurgeAuditEntry] = []
        for raw in self._audit.read():
            entry_data = {k: v for k, v in raw.items() if k != "prev_hash"}
            upgraded = _coerce_legacy_entry(entry_data)
            entries.append(PurgeAuditEntry.model_validate(upgraded))
        return entries

    def verify(self) -> None:
        """Verify the audit log's hash chain.

        Raises:
            creek.audit.AuditChainBroken: If the chain is broken.
        """
        self._audit.verify()

    def _migrate_legacy_if_needed(self) -> None:
        """One-shot migration of the pre-Batch-C JSON-array log.

        If the legacy file exists and the new JSONL log is empty, every
        legacy entry is replayed into the new chain in order, a
        ``purge.audit.migration`` marker is appended, and the legacy file
        is unlinked. After the first call the instance flips
        :attr:`_migration_settled` so subsequent calls skip the legacy
        stat altogether — important for the vault-writer ingest path
        where ``append`` is hot.
        """
        if self._migration_settled:
            return
        if not self._legacy_path.exists():
            self._migration_settled = True
            return
        if self.log_path.exists() and self.log_path.stat().st_size > 0:
            # JSONL already populated AND legacy still on disk: a
            # previous attempt wrote some entries then crashed before
            # unlinking, or an operator restored the legacy file. The
            # state is half-migrated; silently doing nothing would let
            # the inconsistency drift indefinitely, so surface it
            # explicitly the first time we see it.
            logger.warning(
                "Purge audit migration: %s exists and %s also exists with "
                "content; skipping migration. Inspect both files and remove "
                "%s by hand once you have confirmed every legacy entry is in "
                "the new log.",
                self._legacy_path,
                self.log_path,
                self._legacy_path,
            )
            self._migration_settled = True
            return
        legacy_entries = self._load_legacy_entries()
        if legacy_entries is None:
            self._migration_settled = True
            return
        try:
            for entry in legacy_entries:
                # Strip prev_hash defensively: AuditLog.append rejects
                # payloads carrying the reserved chain key, and a legacy
                # log written by an earlier audit substrate (or hand-edited
                # by an operator) could include one. Without this guard the
                # whole migration aborts with ValueError.
                sanitised = {k: v for k, v in entry.items() if k != "prev_hash"}
                self._audit.append(_coerce_legacy_entry(sanitised))
            self._audit.append(self._migration_marker(len(legacy_entries)))
        except OSError:
            # Mid-migration failure (disk full, permission flip). The
            # new log now has partial content so the size guard above
            # will short-circuit subsequent attempts; the legacy file
            # is left in place deliberately so no entries are lost.
            # Warn loudly so the operator can reconcile before the
            # next run silently treats the partial state as "already
            # migrated".
            logger.exception(
                "Purge audit migration of %s into %s failed mid-write; legacy "
                "file left intact for manual reconciliation.",
                self._legacy_path,
                self.log_path,
            )
            raise
        self._legacy_path.unlink(missing_ok=True)
        self._migration_settled = True

    def _load_legacy_entries(self) -> list[dict[str, Any]] | None:
        """Return parsed legacy entries, or ``None`` when unreadable."""
        try:
            raw = self._legacy_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read legacy purge log %s", self._legacy_path)
            return None
        if not raw.strip():
            return []
        try:  # noqa: TRY101  # Separate failure modes: file IO vs JSON parsing each have distinct logging contexts.
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Legacy purge log %s is not valid JSON; skipping migration",
                self._legacy_path,
            )
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _migration_marker(self, migrated_count: int) -> dict[str, Any]:
        """Build the ``purge.audit.migration`` chain marker payload."""
        return {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": "purge.audit.migration",
            "criteria": {"legacy_path": str(self._legacy_path)},
            "affected_fragments": [],
            "fragments_deleted": 0,
            "references_scrubbed": 0,
            "embeddings_removed": 0,
            "provenance_scrubbed": 0,
            "intimate_stubs_removed": 0,
            "journal_staged_removed": 0,
            "operator": "system",
            "dry_run": False,
            "migrated_entries": migrated_count,
        }
