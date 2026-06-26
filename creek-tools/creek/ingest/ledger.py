"""Per-source ingest ledger (issue #672 / SPEC R1).

Maps a stable ``source_key`` — the vault-relative path of a *mutable* source
unit (e.g. a journal ``.md``) — to the fragment it produced, the content hash
at last ingest, and a timestamp. Persisted as append-only JSONL under
``00-Creek-Meta/State/ingest/<source>.jsonl`` so a later ingest can decide
**unchanged / changed / gone** for that unit.

This module is the skeleton seam: it only *records* and *reads back*. The
load-bearing decisions — update-in-place on a changed unit, soft-tomb on a
vanished one — land in issues #673 and #674. Append-only event sources
(Discord/chat exports) keep their content-hashed ids and never touch the
ledger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_LEDGER_RELPARTS: tuple[str, ...] = ("00-Creek-Meta", "State", "ingest")


@dataclass(frozen=True)
class LedgerRecord:
    """One source unit's last-known ingest state.

    Attributes:
        source_key: Stable vault-relative identity of the source unit.
        fragment_id: The fragment id that unit currently maps to.
        content_hash: SHA-256 of the source content at last ingest.
        last_seen: ISO-8601 timestamp of the last ingest of this unit.
    """

    source_key: str
    fragment_id: str
    content_hash: str
    last_seen: str


class SourceLedger:
    """Append-only JSONL ledger of source-unit -> fragment mappings.

    Loaded once per ingest run via :meth:`load`; :meth:`record` appends a
    line (crash-safe) and updates the in-memory view. On reload the latest
    line per ``source_key`` wins, so re-ingests accumulate harmlessly and
    resolve to current state.
    """

    def __init__(self, path: Path, records: dict[str, LedgerRecord]) -> None:
        """Bind a ledger to its backing file and pre-loaded records."""
        self._path = path
        self._records = records

    @staticmethod
    def path_for(vault_path: Path, source: str) -> Path:
        """Return the ledger file path for *source* under the vault meta dir."""
        return vault_path.joinpath(*_LEDGER_RELPARTS, f"{source}.jsonl")

    @staticmethod
    def content_hash(content: str) -> str:
        """Return a stable SHA-256 hex digest of *content*."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, vault_path: Path, *, source: str) -> SourceLedger:
        """Load the ledger for *source*, resolving last-write-wins per key.

        A missing file yields an empty ledger. Corrupt or non-object lines
        are skipped so one bad line never aborts the load.

        Args:
            vault_path: Vault root containing ``00-Creek-Meta/``.
            source: Source/ingestor key (e.g. ``"markdown"``).

        Returns:
            A :class:`SourceLedger` bound to the resolved records.
        """
        path = cls.path_for(vault_path, source)
        records: dict[str, LedgerRecord] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                record = cls._parse_line(line)
                if record is not None:
                    records[record.source_key] = record
        return cls(path, records)

    @staticmethod
    def _nonempty_str(value: object) -> str | None:
        """Return *value* when it is a non-empty string, else ``None``."""
        return value if isinstance(value, str) and value else None

    @classmethod
    def _record_from_dict(cls, data: dict[str, object]) -> LedgerRecord | None:
        """Build a record from a parsed object, requiring every field.

        A record is only accepted when ``source_key``, ``fragment_id``,
        ``content_hash`` and ``last_seen`` are all present, non-empty
        strings. Rejecting partial rows keeps a truncated or hand-edited
        line from masquerading as a valid mapping — an empty ``content_hash``
        would otherwise make the changed-check in #673 read every re-ingest
        as "changed".
        """
        source_key = cls._nonempty_str(data.get("source_key"))
        fragment_id = cls._nonempty_str(data.get("fragment_id"))
        content_hash = cls._nonempty_str(data.get("content_hash"))
        last_seen = cls._nonempty_str(data.get("last_seen"))
        if (
            source_key is None
            or fragment_id is None
            or content_hash is None
            or last_seen is None
        ):
            return None
        return LedgerRecord(
            source_key=source_key,
            fragment_id=fragment_id,
            content_hash=content_hash,
            last_seen=last_seen,
        )

    @classmethod
    def _parse_line(cls, line: str) -> LedgerRecord | None:
        """Parse one JSONL line into a record, or ``None`` if malformed."""
        text = line.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return cls._record_from_dict(data)

    def get(self, source_key: str) -> LedgerRecord | None:
        """Return the current record for *source_key*, or ``None``."""
        return self._records.get(source_key)

    def __len__(self) -> int:
        """Return the number of distinct source keys tracked."""
        return len(self._records)

    def __contains__(self, source_key: object) -> bool:
        """Return whether *source_key* is tracked."""
        return source_key in self._records

    def record(
        self,
        source_key: str,
        fragment_id: str,
        content_hash: str,
        *,
        last_seen: str | None = None,
    ) -> LedgerRecord:
        """Upsert a source unit's state, appending a line and updating memory.

        Args:
            source_key: Stable vault-relative identity of the source unit.
            fragment_id: The fragment id it currently maps to.
            content_hash: SHA-256 of the source content (see
                :meth:`content_hash`).
            last_seen: ISO timestamp; defaults to the current UTC time.

        Returns:
            The :class:`LedgerRecord` that was written.
        """
        record = LedgerRecord(
            source_key=source_key,
            fragment_id=fragment_id,
            content_hash=content_hash,
            last_seen=last_seen or datetime.now(UTC).isoformat(),
        )
        self._records[source_key] = record
        self._append(record)
        return record

    def _append(self, record: LedgerRecord) -> None:
        """Append one record as a JSONL line, creating parents as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "source_key": record.source_key,
                "fragment_id": record.fragment_id,
                "content_hash": record.content_hash,
                "last_seen": record.last_seen,
            },
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
