"""Fleet tables, statements, and row converters for the durable store (#1769).

``ProvisioningStore`` stays the sole owner of the SQLite file: every function
here receives the store's already-configured connection inside the store's own
transaction and never opens a connection of its own.  Splitting the fleet
surface out keeps ``store.py`` under pylint's module-length ceiling without
introducing a second writer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

from creek_mcp.provisioning.models import (
    DeletionReceipt,
    FailureReason,
    FleetJob,
    JobOperation,
    JobState,
    ReceiptOutcome,
    ResourceClass,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from decimal import Decimal

    from creek_mcp.provisioning.models import DeletionOutcome, ProvisioningJob

DUPLICATE_ALLOCATION_ATTEMPTS: Final[str] = "duplicate_allocation_attempts"
_MONTH_ROLLOVER: Final[timedelta] = timedelta(days=32)

FLEET_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS provisioning_deletion_receipts (
    job_id TEXT PRIMARY KEY REFERENCES provisioning_jobs(job_id),
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    provider TEXT,
    provider_allocation_id TEXT,
    resource_classes TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL,
    confirmed_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'confirmed', 'failed')),
    last_failure_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    backfilled INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_provisioning_receipts_open
ON provisioning_deletion_receipts(confirmed_at, requested_at);

CREATE TABLE IF NOT EXISTS provisioning_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS provisioning_machine_running (
    provider_allocation_id TEXT PRIMARY KEY,
    running_since TEXT,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provisioning_machine_running_months (
    provider_allocation_id TEXT NOT NULL,
    month TEXT NOT NULL,
    running_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider_allocation_id, month)
);

CREATE TABLE IF NOT EXISTS provisioning_budget_months (
    month TEXT PRIMARY KEY,
    estimated TEXT NOT NULL,
    budget TEXT NOT NULL,
    currency TEXT NOT NULL,
    over INTEGER NOT NULL CHECK (over IN (0, 1)),
    recorded_at TEXT NOT NULL
);
"""

_BACKFILL_RECEIPTS: Final[str] = (
    "INSERT OR IGNORE INTO provisioning_deletion_receipts "
    "(job_id, requester_identity, consumer_identity, requested_at, outcome, "
    "backfilled) "
    "SELECT job_id, requester_identity, consumer_identity, updated_at, 'pending', 1 "
    "FROM provisioning_jobs WHERE state = 'deleting' "
    "OR (state = 'failed' AND operation = 'delete')"
)
_OPEN_RECEIPT: Final[str] = (
    "INSERT INTO provisioning_deletion_receipts "
    "(job_id, requester_identity, consumer_identity, requested_at, outcome) "
    "VALUES (?, ?, ?, ?, 'pending') "
    "ON CONFLICT(job_id) DO UPDATE SET outcome = 'pending', confirmed_at = NULL"
)
_OPEN_EXPIRED_RECEIPTS: Final[str] = (
    "INSERT OR IGNORE INTO provisioning_deletion_receipts "
    "(job_id, requester_identity, consumer_identity, requested_at, outcome) "
    "SELECT job_id, requester_identity, consumer_identity, ?, 'pending' "
    "FROM provisioning_jobs WHERE state = 'awaiting_key_ceremony' AND job_id IN ("
    "SELECT job_id FROM provisioning_key_ceremonies "
    "WHERE completed_at IS NULL AND expires_at <= ?)"
)
_CONFIRM_RECEIPT: Final[str] = (
    "UPDATE provisioning_deletion_receipts SET outcome = 'confirmed', "
    "confirmed_at = ?, provider = ?, resource_classes = ?, "
    "provider_allocation_id = (SELECT provider_allocation_id "
    "FROM provisioning_allocations "
    "WHERE provisioning_allocations.job_id = provisioning_deletion_receipts.job_id) "
    "WHERE job_id = ?"
)
_RECORD_ATTEMPT: Final[str] = (
    "UPDATE provisioning_deletion_receipts SET attempts = attempts + 1, "
    "last_failure_reason = ?, outcome = ? WHERE job_id = ?"
)
_LAZY_BACKFILL_RECEIPT: Final[str] = (
    "INSERT OR IGNORE INTO provisioning_deletion_receipts "
    "(job_id, requester_identity, consumer_identity, requested_at, outcome, "
    "backfilled) VALUES (?, ?, ?, ?, 'pending', 1)"
)
_FLEET_JOBS: Final[str] = (
    "SELECT provisioning_jobs.*, "
    "provisioning_allocations.provider_allocation_id AS fleet_provider_allocation_id, "
    "provisioning_allocations.deleted_at AS fleet_allocation_deleted_at, "
    "provisioning_deletion_receipts.requested_at AS fleet_delete_requested_at, "
    "provisioning_deletion_receipts.outcome AS fleet_receipt_outcome "
    "FROM provisioning_jobs "
    "LEFT JOIN provisioning_allocations USING (job_id) "
    "LEFT JOIN provisioning_deletion_receipts USING (job_id) "
    "ORDER BY provisioning_jobs.created_at, provisioning_jobs.job_id"
)
_UPSERT_RUNNING: Final[str] = (
    "INSERT INTO provisioning_machine_running "
    "(provider_allocation_id, running_since, observed_at) VALUES (?, ?, ?) "
    "ON CONFLICT(provider_allocation_id) DO UPDATE SET "
    "running_since = excluded.running_since, observed_at = excluded.observed_at"
)
_ACCRUE_MONTH: Final[str] = (
    "INSERT INTO provisioning_machine_running_months "
    "(provider_allocation_id, month, running_seconds) VALUES (?, ?, ?) "
    "ON CONFLICT(provider_allocation_id, month) DO UPDATE SET "
    "running_seconds = running_seconds + excluded.running_seconds"
)


class MissingDeletionReceiptError(RuntimeError):
    """A deletion settled for a job that never opened its receipt."""


def timestamp(value: datetime) -> str:
    """Return a stable UTC database representation for *value*."""
    if value.tzinfo is None:
        raise ValueError("provisioning timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def month_key(value: datetime) -> str:
    """Return the ``YYYY-MM`` UTC calendar-month bucket holding *value*."""
    return value.astimezone(UTC).strftime("%Y-%m")


def split_by_month(start: datetime, end: datetime) -> Iterator[tuple[str, int]]:
    """Yield ``(month, seconds)`` chunks of the interval split at UTC month starts."""
    cursor = start.astimezone(UTC)
    finish = end.astimezone(UTC)
    while cursor < finish:
        month_start = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + _MONTH_ROLLOVER).replace(day=1)
        chunk_end = min(finish, next_month)
        yield month_key(cursor), int((chunk_end - cursor).total_seconds())
        cursor = chunk_end


def _optional_datetime(value: object) -> datetime | None:
    """Parse one nullable ISO-8601 column."""
    return None if value is None else datetime.fromisoformat(str(value))


def backfill_pending_receipts(connection: sqlite3.Connection) -> None:
    """Open flagged ``pending`` receipts for pre-v4 deletions lacking one."""
    connection.execute(_BACKFILL_RECEIPTS)


def open_deletion_receipt(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    now: datetime,
) -> None:
    """Open (or reopen as ``pending``) the receipt for one job entering deletion."""
    connection.execute(
        _OPEN_RECEIPT,
        (
            job["job_id"],
            job["requester_identity"],
            job["consumer_identity"],
            timestamp(now),
        ),
    )


def open_expired_ceremony_receipts(
    connection: sqlite3.Connection,
    now: datetime,
) -> None:
    """Open receipts for every ceremony the caller is about to expire at *now*."""
    stamp = timestamp(now)
    connection.execute(_OPEN_EXPIRED_RECEIPTS, (stamp, stamp))


def finalize_deletion_receipt(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    now: datetime,
    *,
    state: JobState,
    retryable: bool,
    failure_reason: FailureReason | None,
    outcome: DeletionOutcome | None,
) -> None:
    """Confirm or record an attempt on the receipt of a settling delete claim.

    A ``deleting`` row that somehow has no receipt is given a backfilled one
    first (requested_at := the row's updated_at) so a deletion can always
    settle; the post-condition then requires exactly one receipt row.
    """
    if state is JobState.DELETED and outcome is not None:
        classes = ",".join(resource.value for resource in outcome.resource_classes)
        statement = _CONFIRM_RECEIPT
        parameters: tuple[object, ...] = (timestamp(now), outcome.provider, classes)
    elif state is JobState.FAILED and job["operation"] == JobOperation.DELETE.value:
        result = ReceiptOutcome.PENDING if retryable else ReceiptOutcome.FAILED
        reason = None if failure_reason is None else failure_reason.value
        statement, parameters = _RECORD_ATTEMPT, (reason, result.value)
    else:
        return
    connection.execute(
        _LAZY_BACKFILL_RECEIPT,
        (
            job["job_id"],
            job["requester_identity"],
            job["consumer_identity"],
            job["updated_at"],
        ),
    )
    cursor = connection.execute(statement, (*parameters, job["job_id"]))
    if cursor.rowcount != 1:
        raise MissingDeletionReceiptError("deletion receipt is missing")


def bump_counter(connection: sqlite3.Connection, name: str) -> None:
    """Increment one named durable counter."""
    connection.execute(
        "INSERT INTO provisioning_counters (name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (name,),
    )


def counter_value(connection: sqlite3.Connection, name: str) -> int:
    """Return one named durable counter, zero when never bumped."""
    row = connection.execute(
        "SELECT value FROM provisioning_counters WHERE name = ?",
        (name,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def receipt_from_row(row: sqlite3.Row) -> DeletionReceipt:
    """Convert one receipt row into its immutable content-free model."""
    reason = row["last_failure_reason"]
    classes = str(row["resource_classes"])
    return DeletionReceipt(
        job_id=str(row["job_id"]),
        requester_identity=str(row["requester_identity"]),
        consumer_identity=str(row["consumer_identity"]),
        provider=None if row["provider"] is None else str(row["provider"]),
        provider_allocation_id=(
            None
            if row["provider_allocation_id"] is None
            else str(row["provider_allocation_id"])
        ),
        resource_classes=tuple(
            ResourceClass(value) for value in classes.split(",") if value
        ),
        requested_at=datetime.fromisoformat(str(row["requested_at"])),
        confirmed_at=_optional_datetime(row["confirmed_at"]),
        outcome=ReceiptOutcome(str(row["outcome"])),
        last_failure_reason=None if reason is None else FailureReason(str(reason)),
        attempts=int(row["attempts"]),
        backfilled=bool(row["backfilled"]),
    )


def list_deletion_receipts(
    connection: sqlite3.Connection,
) -> tuple[DeletionReceipt, ...]:
    """Return every receipt, oldest request first."""
    rows = connection.execute(
        "SELECT * FROM provisioning_deletion_receipts ORDER BY requested_at, job_id"
    ).fetchall()
    return tuple(receipt_from_row(row) for row in rows)


def list_fleet_jobs(
    connection: sqlite3.Connection,
    job_from_row: Callable[[sqlite3.Row], ProvisioningJob],
) -> tuple[FleetJob, ...]:
    """Return every job joined with its allocation and receipt, oldest first."""
    rows = connection.execute(_FLEET_JOBS).fetchall()
    return tuple(_fleet_job_from_row(row, job_from_row(row)) for row in rows)


def _fleet_job_from_row(row: sqlite3.Row, job: ProvisioningJob) -> FleetJob:
    """Attach the joined allocation and receipt columns to *job*."""
    allocation = row["fleet_provider_allocation_id"]
    outcome = row["fleet_receipt_outcome"]
    return FleetJob(
        job=job,
        provider_allocation_id=None if allocation is None else str(allocation),
        allocation_deleted_at=_optional_datetime(row["fleet_allocation_deleted_at"]),
        delete_requested_at=_optional_datetime(row["fleet_delete_requested_at"]),
        receipt_outcome=None if outcome is None else ReceiptOutcome(str(outcome)),
    )


def record_machine_state(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    *,
    running: bool,
    now: datetime,
) -> tuple[int, int]:
    """Sample one allocation and return ``(continuous_seconds, month_to_date_seconds)``.

    Seconds accrue only between two consecutive *running* observations, split
    across UTC month buckets; a stop resets the continuous run; a replay at the
    same instant accrues nothing; a sample older than the stored observation is
    ignored (nothing is written) so out-of-order passes cannot double count.
    Storage is one row per allocation plus one per allocation-month.
    """
    row = connection.execute(
        "SELECT running_since, observed_at FROM provisioning_machine_running "
        "WHERE provider_allocation_id = ?",
        (provider_allocation_id,),
    ).fetchone()
    since = None if row is None else _optional_datetime(row["running_since"])
    observed = None if row is None else datetime.fromisoformat(str(row["observed_at"]))
    if observed is not None and now < observed:
        return _continuous(since, observed), _bucket(
            connection, provider_allocation_id, observed
        )
    continuous = 0
    if running and since is not None and observed is not None:
        _accrue(connection, provider_allocation_id, observed, now)
        continuous = _continuous(since, now)
    running_since = timestamp(since or now) if running else None
    connection.execute(
        _UPSERT_RUNNING,
        (provider_allocation_id, running_since, timestamp(now)),
    )
    return continuous, _bucket(connection, provider_allocation_id, now)


def _continuous(since: datetime | None, until: datetime) -> int:
    """Return whole seconds of the current continuous run ending at *until*."""
    return 0 if since is None else max(0, int((until - since).total_seconds()))


def _bucket(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    instant: datetime,
) -> int:
    """Return the allocation's accrued seconds for the month holding *instant*."""
    bucket = connection.execute(
        "SELECT running_seconds FROM provisioning_machine_running_months "
        "WHERE provider_allocation_id = ? AND month = ?",
        (provider_allocation_id, month_key(instant)),
    ).fetchone()
    return 0 if bucket is None else int(bucket[0])


def _accrue(
    connection: sqlite3.Connection,
    provider_allocation_id: str,
    last: datetime,
    now: datetime,
) -> None:
    """Add the seconds between the previous running observation and *now*."""
    for month, seconds in split_by_month(last, now):
        connection.execute(_ACCRUE_MONTH, (provider_allocation_id, month, seconds))


def running_seconds_by_allocation(
    connection: sqlite3.Connection,
    month: str,
) -> dict[str, int]:
    """Return sampled running seconds per allocation for one ``YYYY-MM`` bucket."""
    rows = connection.execute(
        "SELECT provider_allocation_id, running_seconds "
        "FROM provisioning_machine_running_months WHERE month = ? "
        "ORDER BY provider_allocation_id",
        (month,),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def record_budget_month(
    connection: sqlite3.Connection,
    month: str,
    estimated: Decimal,
    budget: Decimal,
    currency: str,
    now: datetime,
) -> None:
    """Durably record whether *month*'s estimate reached the operator budget."""
    connection.execute(
        "INSERT INTO provisioning_budget_months "
        "(month, estimated, budget, currency, over, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(month) DO UPDATE SET "
        "estimated = excluded.estimated, budget = excluded.budget, "
        "currency = excluded.currency, over = excluded.over, "
        "recorded_at = excluded.recorded_at",
        (
            month,
            str(estimated),
            str(budget),
            currency,
            int(estimated >= budget),
            timestamp(now),
        ),
    )


def requeue_failed_delete(
    connection: sqlite3.Connection,
    job_id: str,
    now: datetime,
) -> sqlite3.Row | None:
    """Requeue a retryable failed delete; return the row, or None when not allowed.

    A delete-operation row already ``deleting`` or ``deleted`` - whether from
    an earlier requeue, a consumer's re-issued DELETE, or the worker settling
    it - is an idempotent no-op and returns the row unchanged.
    """
    row = _job_row(connection, job_id)
    if row is None or row["operation"] != JobOperation.DELETE.value:
        return None
    if row["state"] in {JobState.DELETING.value, JobState.DELETED.value}:
        return row
    if row["state"] != JobState.FAILED.value or not bool(row["retryable"]):
        return None
    connection.execute(
        "UPDATE provisioning_jobs SET state = ?, retry_count = retry_count + 1, "
        "retryable = 0, failure_reason = NULL, updated_at = ? WHERE job_id = ?",
        (JobState.DELETING.value, timestamp(now), job_id),
    )
    return _job_row(connection, job_id)


def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Return one job row by id, or None."""
    row = connection.execute(
        "SELECT * FROM provisioning_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def months_over_budget(connection: sqlite3.Connection, limit: int) -> tuple[bool, ...]:
    """Return over-budget flags for the latest recorded months, most recent first.

    The window ends at the latest recorded month and stops at the first gap in
    calendar-consecutive months, so a missing month can never be skipped over
    when the ADR-0013 D7 rolling-months trigger is evaluated.
    """
    if limit <= 0:
        return ()
    rows = connection.execute(
        "SELECT month, over FROM provisioning_budget_months "
        "ORDER BY month DESC LIMIT ?",
        (limit,),
    ).fetchall()
    flags: list[bool] = []
    expected: str | None = None
    for row in rows:
        month = str(row[0])
        if expected is not None and month != expected:
            break
        flags.append(bool(row[1]))
        expected = previous_month(month)
    return tuple(flags)


def month_range(month: str) -> tuple[datetime, datetime]:
    """Return the UTC start of ``YYYY-MM`` *month* and the start of the next month."""
    year, month_number = (int(part) for part in month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=UTC)
    return start, (start + _MONTH_ROLLOVER).replace(day=1)


def previous_month(month: str) -> str:
    """Return the ``YYYY-MM`` key of the month before *month*."""
    start, _ = month_range(month)
    return month_key(start - timedelta(days=1))
