"""Durable control-plane bookkeeping for vault-side alerts (#2049).

Two tables, both content-free: closed alert kinds, counts, an allocation id, a
random outbox epoch, a closed outcome and a timestamp.

* ``provisioning_vault_alert_marks`` - per allocation and outbox epoch, the
  cumulative per-kind count the control plane has already handed to its alert
  relay.  It is what makes a vault alert delivered once: a later pass hands on
  only what lies above the mark, whether or not the vault heard the ack.
* ``provisioning_vault_alert_status`` - per allocation, the outcome of the last
  attempt to collect and the alerts it was last seen to owe.  An unreachable
  vault keeps its last-known owed counts here, which is how the fleet report
  shows an alert still owed by a vault it cannot currently read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.provisioning.fleet_schema import timestamp

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

VAULT_ALERT_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS provisioning_vault_alert_marks (
    provider_allocation_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    kind TEXT NOT NULL,
    mark INTEGER NOT NULL CHECK (mark > 0),
    PRIMARY KEY (provider_allocation_id, epoch, kind)
);

CREATE TABLE IF NOT EXISTS provisioning_vault_alert_status (
    provider_allocation_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('collected', 'unreachable', 'refused')),
    checked_at TEXT NOT NULL,
    owed TEXT NOT NULL DEFAULT '{}'
);
"""

_RAISE_MARK: Final[str] = (
    "INSERT INTO provisioning_vault_alert_marks "
    "(provider_allocation_id, epoch, kind, mark) VALUES (?, ?, ?, ?) "
    "ON CONFLICT(provider_allocation_id, epoch, kind) DO UPDATE SET "
    "mark = MAX(mark, excluded.mark)"
)
_UPSERT_COLLECTED: Final[str] = (
    "INSERT INTO provisioning_vault_alert_status "
    "(provider_allocation_id, outcome, checked_at, owed) VALUES (?, ?, ?, ?) "
    "ON CONFLICT(provider_allocation_id) DO UPDATE SET "
    "outcome = excluded.outcome, checked_at = excluded.checked_at, "
    "owed = excluded.owed"
)
_UPSERT_MISSED: Final[str] = (
    "INSERT INTO provisioning_vault_alert_status "
    "(provider_allocation_id, outcome, checked_at) VALUES (?, ?, ?) "
    "ON CONFLICT(provider_allocation_id) DO UPDATE SET "
    "outcome = excluded.outcome, checked_at = excluded.checked_at"
)


@unique
class CollectionOutcome(StrEnum):
    """What the last attempt to read one vault's owed alerts found."""

    COLLECTED = "collected"
    UNREACHABLE = "unreachable"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class VaultAlertStatus:
    """One allocation's last collection attempt and its last-known owed alerts.

    Attributes:
        provider_allocation_id: The allocation the vault belongs to.
        outcome: What the last attempt found.
        checked_at: When it was made.
        owed: Closed alert-kind counts the vault was last seen to owe; kept
            from the last successful read while the vault is unreachable.
    """

    provider_allocation_id: str
    outcome: CollectionOutcome
    checked_at: datetime
    owed: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        """Return the content-free fleet-report row."""
        return {
            "provider_allocation_id": self.provider_allocation_id,
            "outcome": self.outcome.value,
            "checked_at": self.checked_at.isoformat(),
            "owed": dict(self.owed),
        }


def vault_alert_marks(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    epoch: str,
) -> dict[str, int]:
    """Return the per-kind counts already handed on for one outbox epoch."""
    rows = connection.execute(
        "SELECT kind, mark FROM provisioning_vault_alert_marks "
        "WHERE provider_allocation_id = ? AND epoch = ?",
        (provider_allocation_id, epoch),
    ).fetchall()
    return {str(kind): int(mark) for kind, mark in rows}


def record_vault_alert_marks(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    epoch: str,
    marks: Mapping[str, int],
) -> None:
    """Raise one epoch's marks monotonically and forget the allocation's others.

    A vault holds one outbox at a time, so once a new epoch is handed on the
    old one's marks can never be consulted again.
    """
    connection.execute(
        "DELETE FROM provisioning_vault_alert_marks "
        "WHERE provider_allocation_id = ? AND epoch != ?",
        (provider_allocation_id, epoch),
    )
    connection.executemany(
        _RAISE_MARK,
        [
            (provider_allocation_id, epoch, kind, mark)
            for kind, mark in sorted(marks.items())
            if mark > 0
        ],
    )


def record_vault_alert_check(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    outcome: CollectionOutcome,
    now: datetime,
    owed: Mapping[str, int] | None,
) -> None:
    """Record one attempt; *owed* ``None`` keeps the last-known owed counts."""
    if owed is None:
        connection.execute(
            _UPSERT_MISSED, (provider_allocation_id, outcome.value, timestamp(now))
        )
        return
    connection.execute(
        _UPSERT_COLLECTED,
        (
            provider_allocation_id,
            outcome.value,
            timestamp(now),
            json.dumps(dict(sorted(owed.items())), separators=(",", ":")),
        ),
    )


def list_vault_alert_status(
    connection: sqlite3.Connection,
) -> tuple[VaultAlertStatus, ...]:
    """Return every allocation's last collection attempt, by allocation id."""
    rows = connection.execute(
        "SELECT provider_allocation_id, outcome, checked_at, owed "
        "FROM provisioning_vault_alert_status ORDER BY provider_allocation_id"
    ).fetchall()
    return tuple(
        VaultAlertStatus(
            provider_allocation_id=str(row["provider_allocation_id"]),
            outcome=CollectionOutcome(str(row["outcome"])),
            checked_at=datetime.fromisoformat(str(row["checked_at"])),
            owed=MappingProxyType(_owed_counts(str(row["owed"]))),
        )
        for row in rows
    )


def _owed_counts(stored: str) -> dict[str, int]:
    """Decode one stored owed-count column."""
    decoded: dict[str, Any] = json.loads(stored)
    return {kind: int(count) for kind, count in decoded.items()}


def running_allocation_ids(connection: sqlite3.Connection) -> frozenset[str]:
    """Return the allocations the latest fleet pass observed running."""
    rows = connection.execute(
        "SELECT provider_allocation_id FROM provisioning_machine_running "
        "WHERE running_since IS NOT NULL"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)
