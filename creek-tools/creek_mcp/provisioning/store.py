"""SQLite-backed durable queue and uniqueness boundary for provisioning (#1768)."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

from creek_mcp.provisioning.ceremony import (
    KEY_CEREMONY_TTL,
    CeremonyConflictError,
    CeremonyExpiredError,
    CeremonySubmission,
    CeremonyUnavailableError,
    KeyCeremonyChallenge,
    WrappedKeyArtifact,
)
from creek_mcp.provisioning.models import (
    ClaimedJob,
    FailureReason,
    JobOperation,
    JobState,
    OperatorAllocationView,
    ProvisioningAllocation,
    ProvisioningJob,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_SCHEMA_VERSION: Final[int] = 4
_DEFAULT_LEASE: Final[timedelta] = timedelta(minutes=1)
_MAX_IDENTIFIER_LENGTH: Final[int] = 200
MAX_ACTIVATION_ALIASES_PER_CONSUMER: Final[int] = 256

_REQUESTER_MIGRATIONS: Final[dict[str, tuple[str, str]]] = {
    "provisioning_jobs": (
        "ALTER TABLE provisioning_jobs ADD COLUMN requester_identity TEXT",
        "UPDATE provisioning_jobs SET requester_identity = consumer_identity",
    ),
    "provisioning_allocations": (
        "ALTER TABLE provisioning_allocations ADD COLUMN requester_identity TEXT",
        "UPDATE provisioning_allocations SET requester_identity = consumer_identity",
    ),
    "provisioning_activation_ids": (
        "ALTER TABLE provisioning_activation_ids ADD COLUMN requester_identity TEXT",
        "UPDATE provisioning_activation_ids SET requester_identity = consumer_identity",
    ),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provisioning_jobs (
    job_id TEXT PRIMARY KEY,
    canonical_activation_id TEXT NOT NULL,
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    state TEXT NOT NULL,
    operation TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    attested_confidential INTEGER,
    delete_requested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state IN (
        'pending', 'provisioning', 'awaiting_key_ceremony', 'ready',
        'failed', 'deleting', 'deleted'
    )),
    CHECK (operation IN ('create', 'delete')),
    CHECK (retryable IN (0, 1)),
    CHECK (attested_confidential IS NULL OR attested_confidential IN (0, 1))
);

CREATE TABLE IF NOT EXISTS provisioning_allocations (
    allocation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES provisioning_jobs(job_id),
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    provider_allocation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS provisioning_activation_ids (
    activation_id TEXT PRIMARY KEY,
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES provisioning_jobs(job_id)
);

CREATE INDEX IF NOT EXISTS ix_provisioning_claimable
ON provisioning_jobs(state, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS provisioning_key_ceremonies (
    job_id TEXT PRIMARY KEY REFERENCES provisioning_jobs(job_id),
    ceremony_id TEXT NOT NULL UNIQUE,
    server_nonce TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    wrapped_artifact_json TEXT,
    completion_fingerprint TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_provisioning_key_ceremony_expiry
ON provisioning_key_ceremonies(expires_at);
"""

_OWNERSHIP_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_live_requester_consumer
ON provisioning_jobs(requester_identity, consumer_identity)
WHERE state != 'deleted';

CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_active_allocation_requester_consumer
ON provisioning_allocations(requester_identity, consumer_identity)
WHERE deleted_at IS NULL;
"""


class ProvisioningStoreError(RuntimeError):
    """Base class for content-free store failures."""


class ActivationConflictError(ProvisioningStoreError):
    """An activation id already belongs to another requester or subject."""


class InvalidJobTransitionError(ProvisioningStoreError):
    """A requested lifecycle transition is not valid for the current state."""


class LostJobLeaseError(ProvisioningStoreError):
    """A worker attempted to settle a claim it no longer owns."""


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


def _timestamp(value: datetime) -> str:
    """Return a stable UTC database representation for *value*."""
    if value.tzinfo is None:
        raise ValueError("provisioning timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _validate_identifier(value: str, *, field: str) -> str:
    """Return one bounded non-blank public identifier."""
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must contain 1-{_MAX_IDENTIFIER_LENGTH} characters")
    return normalized


class ProvisioningStore:
    """Own durable idempotency, state transitions, and worker leases in SQLite."""

    def __init__(self, database: Path) -> None:
        """Initialize *database* and its uniqueness constraints idempotently.

        Three phases, and the middle one is a transaction on purpose. Table and
        trigger creation are ``IF NOT EXISTS`` and re-run harmlessly on every
        open, so they self-heal after a crash. The migration does not: it
        rewrites existing rows, and a crash between an ALTER and its backfill
        would strand them. So the ALTER, the backfill and the version stamp
        commit together — the stamp is what tells the next open the backfill is
        done, and it must never land without it. ``sqlite3.executescript``
        commits any open transaction before running, so the schema and index
        scripts stay outside that fence rather than silently breaking it.
        """
        self._database = database.resolve()
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        with self._connect(write=True) as connection:
            self._migrate_schema(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        with self._connect() as connection:
            self._install_requester_guards(connection)
            connection.executescript(_OWNERSHIP_INDEXES)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """Upgrade an older database without weakening its ownership boundary.

        Column additions are gated on the column being absent, because an ALTER
        cannot be repeated. Backfills are gated on ``PRAGMA user_version``
        instead, and the difference is what makes a half-applied upgrade heal.
        Gating a backfill on column absence makes a crash between the ALTER and
        the UPDATE permanent: the column exists forever after, so the backfill
        can never run again and every pre-existing row keeps its NULL. The
        version stamp is written by the caller inside this same transaction, so
        a crash leaves it at the old value and the next open finishes the job.
        Every backfill is additionally written to be idempotent, so re-running
        one after a rollback costs nothing.
        """
        version_row = connection.execute("PRAGMA user_version").fetchone()
        version = 0 if version_row is None else int(version_row[0])
        table_columns = {
            table: {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for table in (
                "provisioning_jobs",
                "provisioning_allocations",
                "provisioning_activation_ids",
            )
        }
        if "attested_confidential" not in table_columns["provisioning_jobs"]:
            connection.execute(
                "ALTER TABLE provisioning_jobs "
                "ADD COLUMN attested_confidential INTEGER "
                "CHECK (attested_confidential IS NULL "
                "OR attested_confidential IN (0, 1))"
            )
        if "delete_requested_at" not in table_columns["provisioning_jobs"]:
            connection.execute(
                "ALTER TABLE provisioning_jobs ADD COLUMN delete_requested_at TEXT"
            )
        if version < _SCHEMA_VERSION:
            # Backfill from updated_at rather than leaving NULL: a delete that
            # was already stuck when this column landed must not become
            # invisible to reconciliation because of the upgrade itself. Gated
            # on the version rather than on the ALTER above, so a crash between
            # the two is repaired on the next open instead of made permanent.
            connection.execute(
                "UPDATE provisioning_jobs SET delete_requested_at = updated_at "
                "WHERE operation = 'delete' AND delete_requested_at IS NULL"
            )
        for table, migration in _REQUESTER_MIGRATIONS.items():
            if "requester_identity" not in table_columns[table]:
                add_requester_column, backfill_requester = migration
                connection.execute(add_requester_column)
                connection.execute(backfill_requester)
        connection.execute("DROP INDEX IF EXISTS uq_provisioning_live_consumer")
        connection.execute(
            "DROP INDEX IF EXISTS uq_provisioning_active_allocation_consumer"
        )

    @staticmethod
    def _install_requester_guards(connection: sqlite3.Connection) -> None:
        """Restore the v3 NOT NULL/identifier invariant on ALTERed SQLite tables."""
        for table in (
            "provisioning_jobs",
            "provisioning_allocations",
            "provisioning_activation_ids",
        ):
            connection.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS ck_{table}_requester_insert
                BEFORE INSERT ON {table}
                WHEN NEW.requester_identity IS NULL
                  OR length(trim(NEW.requester_identity)) = 0
                  OR length(NEW.requester_identity) > {_MAX_IDENTIFIER_LENGTH}
                BEGIN
                    SELECT RAISE(ABORT, 'invalid requester identity');
                END;
                CREATE TRIGGER IF NOT EXISTS ck_{table}_requester_update
                BEFORE UPDATE OF requester_identity ON {table}
                WHEN NEW.requester_identity IS NULL
                  OR length(trim(NEW.requester_identity)) = 0
                  OR length(NEW.requester_identity) > {_MAX_IDENTIFIER_LENGTH}
                BEGIN
                    SELECT RAISE(ABORT, 'invalid requester identity');
                END;
                """
            )

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection, optionally holding an immediate write lock."""
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        assert connection.row_factory is sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def submit(
        self,
        activation_id: str,
        consumer_identity: str,
        requester_identity: str | None = None,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Return one live subject job owned by the authenticated requester."""
        activation = _validate_identifier(activation_id, field="activation_id")
        consumer = _validate_identifier(consumer_identity, field="consumer_identity")
        requester = _validate_identifier(
            requester_identity or consumer,
            field="requester_identity",
        )
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            existing = connection.execute(
                "SELECT requester_identity, consumer_identity, job_id "
                "FROM provisioning_activation_ids "
                "WHERE activation_id = ?",
                (activation,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["requester_identity"] != requester
                    or existing["consumer_identity"] != consumer
                ):
                    raise ActivationConflictError("activation cannot be accepted")
                return self._job_by_id(connection, str(existing["job_id"]))

            live = connection.execute(
                "SELECT * FROM provisioning_jobs "
                "WHERE requester_identity = ? AND consumer_identity = ? "
                "AND state != 'deleted'",
                (requester, consumer),
            ).fetchone()
            if live is None:
                job_id = str(uuid4())
                stamp = _timestamp(instant)
                connection.execute(
                    "INSERT INTO provisioning_jobs "
                    "(job_id, canonical_activation_id, requester_identity, "
                    "consumer_identity, state, operation, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        activation,
                        requester,
                        consumer,
                        JobState.PENDING.value,
                        JobOperation.CREATE.value,
                        stamp,
                        stamp,
                    ),
                )
            else:
                job_id = str(live["job_id"])
            alias_count = connection.execute(
                "SELECT COUNT(*) FROM provisioning_activation_ids "
                "WHERE requester_identity = ? AND consumer_identity = ?",
                (requester, consumer),
            ).fetchone()
            assert alias_count is not None
            if int(alias_count[0]) >= MAX_ACTIVATION_ALIASES_PER_CONSUMER:
                raise ActivationConflictError("activation cannot be accepted")
            connection.execute(
                "INSERT INTO provisioning_activation_ids "
                "(activation_id, requester_identity, consumer_identity, job_id) "
                "VALUES (?, ?, ?, ?)",
                (activation, requester, consumer, job_id),
            )
            return self._job_by_id(connection, job_id)

    def get(self, job_id: str, requester_identity: str) -> ProvisioningJob | None:
        """Return *job_id* only when its requester owns it."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_jobs "
                "WHERE job_id = ? AND requester_identity = ?",
                (job_id, requester_identity),
            ).fetchone()
            return None if row is None else self._from_row(row)

    def claim_next(
        self,
        *,
        now: datetime | None = None,
        lease_for: timedelta = _DEFAULT_LEASE,
    ) -> ClaimedJob | None:
        """Lease the oldest claimable job, reclaiming expired crash residue first."""
        instant = now or _utc_now()
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        stamp = _timestamp(instant)
        with self._connect(write=True) as connection:
            self._release_expired_claims(connection, stamp)
            row = connection.execute(
                "SELECT provisioning_jobs.*, "
                "provisioning_allocations.provider_allocation_id "
                "AS claimed_provider_allocation_id FROM provisioning_jobs "
                "LEFT JOIN provisioning_allocations USING (job_id) "
                "WHERE state IN ('pending', 'deleting') AND lease_token IS NULL "
                "ORDER BY created_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            state = (
                JobState.PROVISIONING
                if row["state"] == JobState.PENDING.value
                else JobState.DELETING
            )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, attempts = attempts + 1, "
                "lease_token = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE job_id = ?",
                (
                    state.value,
                    token,
                    _timestamp(instant + lease_for),
                    stamp,
                    row["job_id"],
                ),
            )
            job = self._job_by_id(connection, str(row["job_id"]))
            allocation_id = row["claimed_provider_allocation_id"]
            return ClaimedJob(
                job=job,
                lease_token=token,
                provider_allocation_id=(
                    None if allocation_id is None else str(allocation_id)
                ),
            )

    def record_failure(
        self,
        job_id: str,
        lease_token: str,
        reason: FailureReason,
        *,
        retryable: bool,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Settle one owned claim as a stable, optionally retryable failure."""
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.FAILED,
            retryable=retryable,
            failure_reason=reason,
            now=now,
        )

    def complete_create(
        self,
        job_id: str,
        lease_token: str,
        provider_allocation_id: str,
        *,
        handoff: Callable[[], None],
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Handoff and settle a create claim inside one lease-valid write fence."""
        allocation = _validate_identifier(
            provider_allocation_id,
            field="provider_allocation_id",
        )
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.AWAITING_KEY_CEREMONY,
            provider_allocation_id=allocation,
            before_settle=handoff,
            create_key_ceremony=True,
            now=now,
        )

    def complete_delete(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Settle a delete claim only after the provider confirms removal."""
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.DELETED,
            now=now,
        )

    def retry(
        self,
        job_id: str,
        requester_identity: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Requeue a failed operation only when its recorded policy permits retry."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            row = self._owned_job(connection, job_id, requester_identity)
            if row["state"] != JobState.FAILED.value:
                # The counter is scoped to the current operation. An operation
                # change resets it, so only replays of the retry that already
                # moved this same operation out of FAILED are idempotent here.
                if int(row["retry_count"]) > 0 and row["state"] in {
                    JobState.PENDING.value,
                    JobState.PROVISIONING.value,
                    JobState.DELETING.value,
                }:
                    return self._from_row(row)
                raise InvalidJobTransitionError("job is not retryable")
            if not bool(row["retryable"]):
                raise InvalidJobTransitionError("job is not retryable")
            target = (
                JobState.DELETING
                if row["operation"] == JobOperation.DELETE.value
                else JobState.PENDING
            )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, "
                "retry_count = retry_count + 1, "
                "retryable = 0, failure_reason = NULL, updated_at = ? WHERE job_id = ?",
                (target.value, _timestamp(instant), job_id),
            )
            return self._job_by_id(connection, job_id)

    def request_delete(
        self,
        job_id: str,
        requester_identity: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Idempotently enqueue deletion for one requester-owned allocation."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            row = self._owned_job(connection, job_id, requester_identity)
            if row["state"] in {JobState.DELETING.value, JobState.DELETED.value}:
                return self._from_row(row)
            # delete_requested_at is the only clock reconciliation can trust:
            # updated_at is rewritten by every claim_next lease and by every
            # _settle_claim, so a delete that hangs, loses its lease and is
            # re-claimed would keep pushing its own staleness deadline forward
            # and never age past the window. COALESCE is what makes the column
            # write-once, and that is deliberately not left to the early return
            # above: it only covers 'deleting' and 'deleted', so a delete
            # parked at 'failed' reaches this line again and must not restart
            # its own clock. Every other site setting operation='delete'
            # stamps it the same way.
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, operation = ?, "
                "retry_count = 0, retryable = 0, failure_reason = NULL, "
                "attested_confidential = NULL, lease_token = NULL, "
                "delete_requested_at = COALESCE(delete_requested_at, ?), "
                "lease_expires_at = NULL, updated_at = ? WHERE job_id = ?",
                (
                    JobState.DELETING.value,
                    JobOperation.DELETE.value,
                    _timestamp(instant),
                    _timestamp(instant),
                    job_id,
                ),
            )
            return self._job_by_id(connection, job_id)

    def get_key_ceremony(
        self,
        job_id: str,
        requester_identity: str,
    ) -> KeyCeremonyChallenge:
        """Return the public challenge for one requester-owned allocation."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT provisioning_jobs.canonical_activation_id, "
                "provisioning_key_ceremonies.* FROM provisioning_jobs "
                "JOIN provisioning_key_ceremonies USING (job_id) "
                "WHERE job_id = ? AND requester_identity = ?",
                (job_id, requester_identity),
            ).fetchone()
        if row is None:
            raise CeremonyUnavailableError("key ceremony is unavailable")
        return KeyCeremonyChallenge(
            job_id=job_id,
            activation_id=str(row["canonical_activation_id"]),
            ceremony_id=str(row["ceremony_id"]),
            server_nonce=str(row["server_nonce"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def get_wrapped_key_artifact(
        self,
        job_id: str,
        requester_identity: str,
    ) -> WrappedKeyArtifact | None:
        """Return only the ciphertext artifact for one completed owned ceremony."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT wrapped_artifact_json FROM provisioning_key_ceremonies "
                "JOIN provisioning_jobs USING (job_id) "
                "WHERE job_id = ? AND requester_identity = ?",
                (job_id, requester_identity),
            ).fetchone()
        if row is None or row["wrapped_artifact_json"] is None:
            return None
        return WrappedKeyArtifact.model_validate_json(str(row["wrapped_artifact_json"]))

    def complete_key_ceremony(
        self,
        job_id: str,
        requester_identity: str,
        submission: CeremonySubmission,
        *,
        attested_confidential: bool,
        before_settle: Callable[[], None] | None,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Persist ciphertext and settle one valid, unexpired ceremony."""
        instant = now or _utc_now()
        canonical = submission.canonical_json()
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        artifact_json = submission.wrapped_artifact.model_dump_json()
        expired = False
        completed: ProvisioningJob | None = None
        with self._connect(write=True) as connection:
            job_row = self._owned_job(connection, job_id, requester_identity)
            ceremony = connection.execute(
                "SELECT * FROM provisioning_key_ceremonies WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if ceremony is None:
                raise CeremonyUnavailableError("key ceremony is unavailable")
            prior = ceremony["completion_fingerprint"]
            if job_row["state"] == JobState.READY.value:
                if prior != fingerprint:
                    raise CeremonyConflictError(
                        "key ceremony conflicts with prior completion"
                    )
                return self._from_row(job_row)
            if job_row["state"] != JobState.AWAITING_KEY_CEREMONY.value:
                raise CeremonyUnavailableError("key ceremony is unavailable")
            if instant >= datetime.fromisoformat(str(ceremony["expires_at"])):
                # This transition queues a teardown, so it dates one; see
                # request_delete for why updated_at cannot be that clock.
                connection.execute(
                    "UPDATE provisioning_jobs SET state = ?, operation = ?, "
                    "retry_count = 0, retryable = 0, failure_reason = NULL, "
                    "attested_confidential = NULL, lease_token = NULL, "
                    "delete_requested_at = COALESCE(delete_requested_at, ?), "
                    "lease_expires_at = NULL, updated_at = ? WHERE job_id = ?",
                    (
                        JobState.DELETING.value,
                        JobOperation.DELETE.value,
                        _timestamp(instant),
                        _timestamp(instant),
                        job_id,
                    ),
                )
                expired = True
            else:
                self._validate_ceremony_binding(job_row, ceremony, submission)
                if before_settle is not None:
                    before_settle()
                stamp = _timestamp(instant)
                connection.execute(
                    "UPDATE provisioning_key_ceremonies SET "
                    "wrapped_artifact_json = ?, completion_fingerprint = ?, "
                    "completed_at = ? WHERE job_id = ?",
                    (artifact_json, fingerprint, stamp, job_id),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET state = ?, "
                    "attested_confidential = ?, updated_at = ? WHERE job_id = ?",
                    (
                        JobState.READY.value,
                        int(attested_confidential),
                        stamp,
                        job_id,
                    ),
                )
                completed = self._job_by_id(connection, job_id)
        if expired:
            raise CeremonyExpiredError("key ceremony expired")
        assert completed is not None
        return completed

    def expire_key_ceremonies(self, *, now: datetime | None = None) -> int:
        """Idempotently queue teardown for every incomplete expired ceremony.

        This is the *unattended* teardown path: it runs on every worker tick,
        for a consumer who never came back, by which point the Machine, the
        encrypted volume and the allocation row all exist. It therefore dates
        the teardown it queues, exactly as request_delete does. A row left with
        a NULL clock would fall back onto updated_at, which every re-claim
        resets, and neither of reconciliation's other detectors can cover it:
        _missing skips delete operations and _orphans cannot fire while the
        allocation row is live.
        """
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE provisioning_jobs SET state = ?, operation = ?, "
                "retry_count = 0, retryable = 0, failure_reason = NULL, "
                "attested_confidential = NULL, lease_token = NULL, "
                "delete_requested_at = COALESCE(delete_requested_at, ?), "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE state = ? AND job_id IN ("
                "SELECT job_id FROM provisioning_key_ceremonies "
                "WHERE completed_at IS NULL AND expires_at <= ?)",
                (
                    JobState.DELETING.value,
                    JobOperation.DELETE.value,
                    _timestamp(instant),
                    _timestamp(instant),
                    JobState.AWAITING_KEY_CEREMONY.value,
                    _timestamp(instant),
                ),
            )
            return cursor.rowcount

    @staticmethod
    def _validate_ceremony_binding(
        job: sqlite3.Row,
        ceremony: sqlite3.Row,
        submission: CeremonySubmission,
    ) -> None:
        """Reject a replay whose public AEAD binding misses this job challenge."""
        binding = submission.wrapped_artifact.binding
        expected = (
            str(job["canonical_activation_id"]),
            str(ceremony["ceremony_id"]),
            str(ceremony["server_nonce"]),
        )
        received = (
            binding.activation_id,
            submission.ceremony_id,
            submission.server_nonce,
        )
        nested = (binding.activation_id, binding.ceremony_id, binding.server_nonce)
        if received != expected or nested != expected:
            raise CeremonyConflictError("key ceremony binding does not match challenge")

    def count_jobs(self) -> int:
        """Return the number of durable job records (test and telemetry seam)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM provisioning_jobs"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def count_activation_ids(self) -> int:
        """Return the number of durable activation aliases."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM provisioning_activation_ids"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def get_allocation(
        self,
        job_id: str,
        requester_identity: str,
    ) -> ProvisioningAllocation | None:
        """Return one allocation only inside its requester ownership boundary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_allocations "
                "WHERE job_id = ? AND requester_identity = ?",
                (job_id, requester_identity),
            ).fetchone()
        return None if row is None else self._allocation_from_row(row)

    def count_allocations(self, *, active_only: bool = False) -> int:
        """Return all or only active durable allocations (test/telemetry seam)."""
        query = "SELECT COUNT(*) FROM provisioning_allocations"
        if active_only:
            query += " WHERE deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # Operator-scoped fleet queries (#1769).
    #
    # These three are DELIBERATELY not fenced by requester_identity, in the same
    # style as claim_next above. Fleet reconciliation runs for the operator who
    # pays the provider invoice, not for a consumer: a requester fence would
    # hide precisely the divergences it exists to find, because an orphaned
    # resource has no owning requester left to ask on its behalf. All three
    # are read-only, none of them selects canonical_activation_id, and
    # _owned_job remains the only path every consumer-facing method takes.
    # ------------------------------------------------------------------

    _OPERATOR_COLUMNS: Final[str] = (
        "SELECT provisioning_allocations.provider_allocation_id, "
        "provisioning_allocations.job_id, "
        "provisioning_allocations.consumer_identity, "
        "provisioning_jobs.state, provisioning_jobs.operation, "
        "provisioning_jobs.updated_at, provisioning_jobs.delete_requested_at "
        "FROM provisioning_allocations JOIN provisioning_jobs USING (job_id) "
    )

    def live_allocations(self) -> list[OperatorAllocationView]:
        """Return every allocation the provider should still be billing for."""
        with self._connect() as connection:
            rows = connection.execute(
                self._OPERATOR_COLUMNS
                + "WHERE provisioning_allocations.deleted_at IS NULL "
                "ORDER BY provisioning_allocations.provider_allocation_id"
            ).fetchall()
        return [self._operator_view(row) for row in rows]

    def unconfirmed_deletions(
        self,
        older_than: timedelta,
        *,
        now: datetime | None = None,
    ) -> list[OperatorAllocationView]:
        """Return deletions the provider has not confirmed within *older_than*.

        Staleness is measured from ``delete_requested_at``. ``updated_at``
        cannot serve as that clock: every
        ``claim_next`` lease and every ``_settle_claim`` rewrites it, so a
        delete that hangs and is re-claimed pushes its own deadline forward
        indefinitely and never ages past the window — while the Machine it
        already destroyed removes the ``_missing`` backstop, leaving a billing
        volume nothing can see.

        All three transitions into ``operation='delete'`` stamp that column
        through ``COALESCE(delete_requested_at, ?)`` — ``request_delete``, the
        expired branch of ``complete_key_ceremony``, and
        ``expire_key_ceremonies``. The last two are the *unattended* path,
        taken for a consumer who never completed the ceremony and whose
        provider resources already exist, so they matter most. Write-once is
        therefore an SQL invariant rather than an argument about which states
        reach which line.

        A row whose clock is somehow still NULL — a v3 database whose upgrade
        has not yet run — falls back to ``updated_at`` through COALESCE. That
        is the weaker clock, but a weak clock reports a stuck delete late; a
        NULL would never report it at all.

        The predicate covers state ``failed`` as well as ``deleting``, and that
        arm is load-bearing rather than defensive. ``record_failure`` settles
        through ``_settle_claim(state=FAILED)``, which never reaches the branch
        that sets ``deleted_at``; ``_release_expired_claims`` only rescues rows
        still holding an expired lease, and ``retry`` is requester-fenced.
        A delete
        whose provider call raised therefore parks forever at
        ``state='failed'`` with a live allocation row — a Fly bill nobody is
        watching, which is exactly what ADR-0013 Decision 6 forbids.
        """
        if older_than < timedelta(0):
            raise ValueError("older_than must not be negative")
        threshold = _timestamp((now or _utc_now()) - older_than)
        with self._connect() as connection:
            rows = connection.execute(
                self._OPERATOR_COLUMNS
                + "WHERE provisioning_allocations.deleted_at IS NULL "
                "AND provisioning_jobs.operation = ? "
                "AND provisioning_jobs.state IN (?, ?) "
                "AND COALESCE(provisioning_jobs.delete_requested_at, "
                "provisioning_jobs.updated_at) <= ? "
                "ORDER BY provisioning_allocations.provider_allocation_id",
                (
                    JobOperation.DELETE.value,
                    JobState.DELETING.value,
                    JobState.FAILED.value,
                    threshold,
                ),
            ).fetchall()
        return [self._operator_view(row) for row in rows]

    def duplicate_activation_attempts(self) -> int:
        """Return how many activation aliases resolved onto an existing job.

        ADR-0013 Decision 4 requires the control plane to report duplicate
        allocations. They are already durable and need no new column:
        :meth:`submit` folds a second *distinct* activation id for one live
        requester/consumer pair onto the job that already exists while still
        inserting its alias row, so aliases beyond the first per job are
        exactly the attempts that would have created a second billable
        allocation had the fold not caught them.

        The figure is a **lifetime, monotonic** count rather than a rate: no
        ``DELETE FROM provisioning_activation_ids`` exists anywhere in this
        module, so a row once written is never removed and the count only ever
        rises. Two things it deliberately does not count:

        * A pure replay of an activation id the store already holds inserts
          nothing, because ``submit`` returns early on the existing row. That
          is the idempotency contract working, not a duplicate allocation.
        * An attempt the store *refused* — ``ActivationConflictError`` for a
          mismatched owner, or the
          :data:`MAX_ACTIVATION_ALIASES_PER_CONSUMER` cap — leaves no durable
          trace at all, so it is invisible here and must be reported as
          unavailable rather than folded in as a zero.

        Read-only and operator-scoped like its two neighbours above, and it
        selects no identifier: the count is the whole result.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT job_id) "
                "FROM provisioning_activation_ids"
            ).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _operator_view(row: sqlite3.Row) -> OperatorAllocationView:
        """Convert one operator-scoped row into its content-free projection."""
        return OperatorAllocationView(
            provider_allocation_id=str(row["provider_allocation_id"]),
            job_id=str(row["job_id"]),
            consumer_identity=str(row["consumer_identity"]),
            state=JobState(str(row["state"])),
            operation=JobOperation(str(row["operation"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _release_expired_claims(connection: sqlite3.Connection, stamp: str) -> None:
        """Make expired create/delete leases claimable after a worker crash."""
        connection.execute(
            "UPDATE provisioning_jobs SET "
            "state = CASE WHEN state = 'provisioning' THEN 'pending' ELSE state END, "
            "lease_token = NULL, lease_expires_at = NULL "
            "WHERE state IN ('provisioning', 'deleting') "
            "AND lease_token IS NOT NULL AND lease_expires_at <= ?",
            (stamp,),
        )

    def _settle_claim(
        self,
        job_id: str,
        lease_token: str,
        *,
        state: JobState,
        retryable: bool = False,
        failure_reason: FailureReason | None = None,
        provider_allocation_id: str | None = None,
        before_settle: Callable[[], None] | None = None,
        create_key_ceremony: bool = False,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Apply one terminal claim transition when *lease_token* still owns it."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            query = (
                "SELECT * FROM provisioning_jobs WHERE job_id = ? AND lease_token = ? "
                "AND state IN ('provisioning', 'deleting')"
            )
            parameters: tuple[str, ...] = (job_id, lease_token)
            if before_settle is not None:
                query += " AND lease_expires_at > ?"
                parameters += (_timestamp(instant),)
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                raise LostJobLeaseError("job lease is no longer owned")
            stamp = _timestamp(instant)
            if before_settle is not None:
                before_settle()
            if provider_allocation_id is not None:
                connection.execute(
                    "INSERT INTO provisioning_allocations "
                    "(allocation_id, job_id, requester_identity, consumer_identity, "
                    "provider_allocation_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        job_id,
                        row["requester_identity"],
                        row["consumer_identity"],
                        provider_allocation_id,
                        stamp,
                    ),
                )
            if create_key_ceremony:
                connection.execute(
                    "INSERT INTO provisioning_key_ceremonies "
                    "(job_id, ceremony_id, server_nonce, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        str(uuid4()),
                        secrets.token_urlsafe(32),
                        _timestamp(instant + KEY_CEREMONY_TTL),
                    ),
                )
            if state is JobState.DELETED:
                connection.execute(
                    "UPDATE provisioning_allocations SET deleted_at = ? "
                    "WHERE job_id = ? AND deleted_at IS NULL",
                    (stamp, job_id),
                )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, retryable = ?, "
                "failure_reason = ?, lease_token = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE job_id = ?",
                (
                    state.value,
                    int(retryable),
                    None if failure_reason is None else failure_reason.value,
                    stamp,
                    job_id,
                ),
            )
            return self._job_by_id(connection, job_id)

    @staticmethod
    def _owned_job(
        connection: sqlite3.Connection,
        job_id: str,
        requester_identity: str,
    ) -> sqlite3.Row:
        """Return a requester-owned row or a content-free not-found error."""
        row = connection.execute(
            "SELECT * FROM provisioning_jobs "
            "WHERE job_id = ? AND requester_identity = ?",
            (job_id, requester_identity),
        ).fetchone()
        if row is None:
            raise InvalidJobTransitionError("job is unavailable")
        return cast("sqlite3.Row", row)

    def _job_by_id(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> ProvisioningJob:
        """Return one known job row as a secret-free domain model."""
        row = connection.execute(
            "SELECT * FROM provisioning_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ProvisioningStoreError("job disappeared during transaction")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ProvisioningJob:
        """Convert one SQLite row into its immutable domain representation."""
        reason = row["failure_reason"]
        attested = row["attested_confidential"]
        return ProvisioningJob(
            job_id=str(row["job_id"]),
            activation_id=str(row["canonical_activation_id"]),
            requester_identity=str(row["requester_identity"]),
            consumer_identity=str(row["consumer_identity"]),
            state=JobState(str(row["state"])),
            operation=JobOperation(str(row["operation"])),
            attempts=int(row["attempts"]),
            retryable=bool(row["retryable"]),
            failure_reason=None if reason is None else FailureReason(str(reason)),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            attested_confidential=None if attested is None else bool(attested),
        )

    @staticmethod
    def _allocation_from_row(row: sqlite3.Row) -> ProvisioningAllocation:
        """Convert one allocation row into its immutable internal model."""
        deleted_at = row["deleted_at"]
        return ProvisioningAllocation(
            allocation_id=str(row["allocation_id"]),
            job_id=str(row["job_id"]),
            requester_identity=str(row["requester_identity"]),
            consumer_identity=str(row["consumer_identity"]),
            provider_allocation_id=str(row["provider_allocation_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            deleted_at=(
                None if deleted_at is None else datetime.fromisoformat(str(deleted_at))
            ),
        )
