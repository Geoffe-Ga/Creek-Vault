"""Audit log for purge operations.

Records every purge operation — including dry runs — to a JSON log at
``00-Creek-Meta/Processing-Log/purge-log.json``. Each entry captures the
timestamp, operation, target, affected count, operator, and dry-run flag.

The log is a JSON array; new entries are appended. If the file does not
exist it is created. Malformed existing logs are rebuilt from scratch
rather than losing new entries.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_OPERATOR = "human via CLI"


class PurgeAuditEntry(BaseModel):
    """A single purge audit log entry.

    Attributes:
        timestamp: UTC ISO-format datetime of the purge.
        operation: Operation name (``fragment``, ``source``,
            ``classifications``, ``daterange``, ``vault``).
        target: The purge target (fragment ID, source type, etc.).
        count: Number of affected files or fragments.
        operator: Who performed the purge (default ``"human via CLI"``).
        dry_run: Whether the purge was a dry-run preview.
    """

    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
    )
    operation: str
    target: str
    count: int
    operator: str = _DEFAULT_OPERATOR
    dry_run: bool = False


class PurgeAuditLog:
    """Append-only JSON audit log for purge operations.

    Args:
        vault_path: Root of the Obsidian vault. The log file lives at
            ``{vault_path}/00-Creek-Meta/Processing-Log/purge-log.json``.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialise the audit log with the target vault path.

        Args:
            vault_path: Root of the Obsidian vault.
        """
        self.vault_path = vault_path
        self.log_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
        )

    def append(self, entry: PurgeAuditEntry) -> None:
        """Append a new entry to the audit log.

        Creates the log directory and file if missing. If the existing
        log is malformed, it is replaced with a fresh log containing
        only the new entry.

        Args:
            entry: The audit entry to append.
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._read_entries()
        entries.append(entry.model_dump(mode="json"))
        self.log_path.write_text(
            json.dumps(entries, indent=2),
            encoding="utf-8",
        )

    def read(self) -> list[PurgeAuditEntry]:
        """Read all entries from the audit log.

        Returns:
            List of parsed :class:`PurgeAuditEntry` objects. Empty list
            if the log does not exist or is malformed.
        """
        entries = self._read_entries()
        return [PurgeAuditEntry.model_validate(e) for e in entries]

    def _read_entries(self) -> list[dict[str, object]]:
        """Read raw entry dicts from the log file.

        Returns:
            List of raw entry dicts. Empty list if the log is missing
            or unreadable.
        """
        if not self.log_path.exists():
            return []
        try:
            raw = self.log_path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Purge log at %s is unreadable — starting fresh.",
                self.log_path,
            )
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]
