"""Durable, idempotent provisioning job storage for issue #1768."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from creek_mcp.provisioning.ceremony import (
    KEY_CEREMONY_TTL,
    CeremonyExpiredError,
    CeremonySubmission,
)
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    FailureReason,
    JobOperation,
    JobState,
    ReceiptOutcome,
    ResourceClass,
)
from creek_mcp.provisioning.store import (
    _OWNERSHIP_INDEXES,
    _SCHEMA,
    MAX_ACTIVATION_ALIASES_PER_CONSUMER,
    ActivationConflictError,
    InvalidJobTransitionError,
    LostJobLeaseError,
    ProvisioningStore,
)

_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
_OUTCOME = DeletionOutcome(
    "fake",
    (
        ResourceClass.CREDENTIAL,
        ResourceClass.MACHINE,
        ResourceClass.VOLUME,
        ResourceClass.APP,
    ),
)
_HTTPAPI = Path(__file__).resolve().parents[1] / "creek_mcp" / "httpapi"
_CEREMONY_VECTORS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "provisioning-v1"
    / "key-ceremony-test-vectors.json"
)
_FLEET_TABLES = (
    "provisioning_deletion_receipts",
    "provisioning_counters",
    "provisioning_machine_running",
    "provisioning_machine_running_months",
    "provisioning_budget_months",
)


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return an initialized store backed by a real SQLite file."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


def test_repeating_an_activation_returns_the_same_durable_job(
    store: ProvisioningStore,
) -> None:
    """One activation id is an idempotency key, not a job factory."""
    first = store.submit("activation-001", "adepthood", now=_NOW)
    second = store.submit("activation-001", "adepthood", now=_NOW + timedelta(days=1))

    assert second == first
    assert first.state is JobState.PENDING


def test_one_consumer_cannot_gain_two_live_allocations_under_concurrency(
    store: ProvisioningStore,
) -> None:
    """Distinct concurrent activations alias one consumer's live job."""

    def submit(number: int) -> str:
        job = store.submit(f"activation-{number:03d}", "adepthood", now=_NOW)
        return job.job_id

    with ThreadPoolExecutor(max_workers=12) as executor:
        job_ids = set(executor.map(submit, range(24)))

    assert len(job_ids) == 1
    assert store.count_jobs() == 1
    assert store.count_activation_ids() == 24
    assert store.duplicate_allocation_attempts() == 23


def test_one_requester_can_own_distinct_consumer_allocations(
    store: ProvisioningStore,
) -> None:
    """A backend service can provision one isolated vault per activated user."""
    first = store.submit(
        "activation-user-001",
        "user-001",
        requester_identity="adepthood",
        now=_NOW,
    )
    second = store.submit(
        "activation-user-002",
        "user-002",
        requester_identity="adepthood",
        now=_NOW,
    )

    assert first.job_id != second.job_id
    assert first.requester_identity == "adepthood"
    assert second.requester_identity == "adepthood"
    assert store.count_jobs() == 2


def test_subject_identity_is_scoped_to_the_authenticated_requester(
    store: ProvisioningStore,
) -> None:
    """Two service consumers may use the same opaque local subject safely."""
    first = store.submit(
        "activation-service-a",
        "user-001",
        requester_identity="service-a",
        now=_NOW,
    )
    second = store.submit(
        "activation-service-b",
        "user-001",
        requester_identity="service-b",
        now=_NOW,
    )

    assert first.job_id != second.job_id
    assert store.get(first.job_id, "service-a") == first
    assert store.get(first.job_id, "service-b") is None


def test_an_activation_id_cannot_be_replayed_as_another_consumer(
    store: ProvisioningStore,
) -> None:
    """Cross-consumer idempotency collisions fail without exposing the owner."""
    store.submit("activation-shared", "adepthood", now=_NOW)

    with pytest.raises(ActivationConflictError, match="activation cannot be accepted"):
        store.submit("activation-shared", "other-consumer", now=_NOW)


def test_activation_alias_growth_is_bounded_without_forgetting_existing_ids(
    store: ProvisioningStore,
) -> None:
    """A consumer cannot grow the idempotency table past its published cap."""
    first = store.submit("activation-cap-000", "adepthood", now=_NOW)
    for number in range(1, MAX_ACTIVATION_ALIASES_PER_CONSUMER):
        assert (
            store.submit(f"activation-cap-{number:03d}", "adepthood", now=_NOW) == first
        )

    assert store.submit("activation-cap-000", "adepthood", now=_NOW) == first
    with pytest.raises(ActivationConflictError, match="activation cannot be accepted"):
        store.submit("activation-over-cap", "adepthood", now=_NOW)
    assert store.count_activation_ids() == MAX_ACTIVATION_ALIASES_PER_CONSUMER


def test_database_enforces_activation_and_live_consumer_uniqueness(
    tmp_path: Path,
) -> None:
    """The invariants exist in SQLite, not only in Python pre-checks."""
    database = tmp_path / "provisioning.sqlite3"
    ProvisioningStore(database)

    with closing(sqlite3.connect(database)) as connection:
        activation_indexes = connection.execute(
            "PRAGMA index_list(provisioning_activation_ids)"
        ).fetchall()
        live_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("uq_provisioning_live_requester_consumer",),
        ).fetchone()
        allocation_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("uq_provisioning_active_allocation_requester_consumer",),
        ).fetchone()

    assert any(row[2] == 1 for row in activation_indexes)
    assert live_index is not None
    assert "UNIQUE" in live_index[0]
    assert "requester_identity, consumer_identity" in live_index[0]
    assert "WHERE state != 'deleted'" in live_index[0]
    assert allocation_index is not None
    assert "UNIQUE" in allocation_index[0]
    assert "requester_identity, consumer_identity" in allocation_index[0]
    assert "WHERE deleted_at IS NULL" in allocation_index[0]


def test_v2_database_migrates_authenticated_ownership_without_data_loss(
    tmp_path: Path,
) -> None:
    """Existing single-identity rows become requester-owned v3 rows in place."""
    database = tmp_path / "provisioning-v2.sqlite3"
    stamp = _NOW.isoformat(timespec="microseconds")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE provisioning_jobs (
                job_id TEXT PRIMARY KEY,
                canonical_activation_id TEXT NOT NULL,
                consumer_identity TEXT NOT NULL,
                state TEXT NOT NULL,
                operation TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retryable INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE provisioning_allocations (
                allocation_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE REFERENCES provisioning_jobs(job_id),
                consumer_identity TEXT NOT NULL,
                provider_allocation_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE provisioning_activation_ids (
                activation_id TEXT PRIMARY KEY,
                consumer_identity TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES provisioning_jobs(job_id)
            );
            CREATE UNIQUE INDEX uq_provisioning_live_consumer
            ON provisioning_jobs(consumer_identity) WHERE state != 'deleted';
            CREATE UNIQUE INDEX uq_provisioning_active_allocation_consumer
            ON provisioning_allocations(consumer_identity) WHERE deleted_at IS NULL;
            """
        )
        connection.execute(
            "INSERT INTO provisioning_jobs "
            "(job_id, canonical_activation_id, consumer_identity, state, operation, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "job-v2",
                "activation-v2",
                "adepthood",
                "pending",
                "create",
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO provisioning_activation_ids VALUES (?, ?, ?)",
            ("activation-v2", "adepthood", "job-v2"),
        )
        connection.execute(
            "INSERT INTO provisioning_allocations VALUES (?, ?, ?, ?, ?, ?)",
            (
                "allocation-v2",
                "job-v2",
                "adepthood",
                "provider-v2",
                stamp,
                None,
            ),
        )
        connection.commit()

    migrated = ProvisioningStore(database)
    job = migrated.get("job-v2", "adepthood")

    assert job is not None
    assert job.requester_identity == "adepthood"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT requester_identity FROM provisioning_activation_ids"
        ).fetchone() == ("adepthood",)
        for table in (
            "provisioning_jobs",
            "provisioning_allocations",
            "provisioning_activation_ids",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET requester_identity = NULL")


def test_allocation_is_a_distinct_durable_model_with_one_active_per_consumer(
    store: ProvisioningStore,
) -> None:
    """Provider allocation identity survives job completion under its own constraint."""
    first_job = store.submit("activation-allocation-1", "adepthood", now=_NOW)
    first_claim = store.claim_next(now=_NOW)
    assert first_claim is not None
    store.complete_create(
        first_job.job_id,
        first_claim.lease_token,
        "provider-allocation-1",
        handoff=lambda: None,
        now=_NOW,
    )

    first_allocation = store.get_allocation(first_job.job_id, "adepthood")
    assert first_allocation is not None
    assert first_allocation.provider_allocation_id == "provider-allocation-1"
    assert store.count_allocations(active_only=True) == 1

    store.request_delete(first_job.job_id, "adepthood", now=_NOW)
    delete_claim = store.claim_next(now=_NOW)
    assert delete_claim is not None
    store.complete_delete(
        first_job.job_id, delete_claim.lease_token, _OUTCOME, now=_NOW
    )
    assert store.count_allocations(active_only=True) == 0

    second_job = store.submit("activation-allocation-2", "adepthood", now=_NOW)
    second_claim = store.claim_next(now=_NOW)
    assert second_claim is not None
    store.complete_create(
        second_job.job_id,
        second_claim.lease_token,
        "provider-allocation-2",
        handoff=lambda: None,
        now=_NOW,
    )

    assert store.count_allocations() == 2
    assert store.count_allocations(active_only=True) == 1


def test_create_handoff_runs_inside_the_lease_settlement_write_fence(
    tmp_path: Path,
) -> None:
    """No deletion transaction can interleave after validation and before handoff."""
    database = tmp_path / "fenced-handoff.sqlite3"
    local_store = ProvisioningStore(database)
    job = local_store.submit("activation-fenced-handoff", "adepthood", now=_NOW)
    claimed = local_store.claim_next(now=_NOW)
    assert claimed is not None

    def assert_write_fenced() -> None:
        with (
            closing(
                sqlite3.connect(database, timeout=0, isolation_level=None)
            ) as contender,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            contender.execute("BEGIN IMMEDIATE")

    completed = local_store.complete_create(
        job.job_id,
        claimed.lease_token,
        "provider-fenced-handoff",
        handoff=assert_write_fenced,
        now=_NOW,
    )

    assert completed.state is JobState.AWAITING_KEY_CEREMONY


def test_an_expired_create_lease_cannot_handoff_or_complete(
    store: ProvisioningStore,
) -> None:
    """Lease expiry fences the handoff before another worker even claims."""
    job = store.submit("activation-expired-handoff", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW, lease_for=timedelta(seconds=1))
    assert claimed is not None
    handed_off = False

    def handoff() -> None:
        nonlocal handed_off
        handed_off = True

    with pytest.raises(LostJobLeaseError, match="no longer owned"):
        store.complete_create(
            job.job_id,
            claimed.lease_token,
            "provider-expired-handoff",
            handoff=handoff,
            now=_NOW + timedelta(seconds=2),
        )
    assert handed_off is False


def test_a_crashed_worker_lease_is_reclaimed_without_making_a_second_job(
    store: ProvisioningStore,
) -> None:
    """Expired claims retry the same durable work after a process crash."""
    submitted = store.submit("activation-lease", "adepthood", now=_NOW)
    first = store.claim_next(now=_NOW, lease_for=timedelta(seconds=30))

    assert first is not None
    assert first.job.job_id == submitted.job_id
    assert first.job.state is JobState.PROVISIONING
    assert store.claim_next(now=_NOW + timedelta(seconds=29)) is None

    reclaimed = store.claim_next(
        now=_NOW + timedelta(seconds=31),
        lease_for=timedelta(seconds=30),
    )

    assert reclaimed is not None
    assert reclaimed.job.job_id == submitted.job_id
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.job.attempts == 2
    assert store.count_jobs() == 1


def test_only_a_retryable_failure_can_return_to_pending(
    store: ProvisioningStore,
) -> None:
    """Retry is explicit and preserves a stable machine-readable reason."""
    retryable = store.submit("activation-retry", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    failed = store.record_failure(
        claimed.job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )

    assert failed.state is JobState.FAILED
    assert failed.failure_reason is FailureReason.PROVIDER_UNAVAILABLE
    retried = store.retry(retryable.job_id, "adepthood", now=_NOW)
    assert retried.state is JobState.PENDING

    claimed_again = store.claim_next(now=_NOW)
    assert claimed_again is not None
    permanent = store.record_failure(
        claimed_again.job.job_id,
        claimed_again.lease_token,
        FailureReason.PROVIDER_REJECTED,
        retryable=False,
        now=_NOW,
    )

    with pytest.raises(InvalidJobTransitionError, match="not retryable"):
        store.retry(permanent.job_id, "adepthood", now=_NOW)


def test_a_prior_create_retry_does_not_make_a_later_delete_retryable(
    store: ProvisioningStore,
) -> None:
    """Changing operations invalidates idempotency state from an earlier retry."""
    job = store.submit("activation-retry-then-delete", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    store.record_failure(
        job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )
    store.retry(job.job_id, "adepthood", now=_NOW)
    deleting = store.request_delete(job.job_id, "adepthood", now=_NOW)

    assert deleting.state is JobState.DELETING
    with pytest.raises(InvalidJobTransitionError, match="not retryable"):
        store.retry(job.job_id, "adepthood", now=_NOW)


def test_concurrent_retries_of_a_failed_delete_remain_idempotent(
    store: ProvisioningStore,
) -> None:
    """Resetting retry state at deletion still admits duplicates of its own retry."""
    job = store.submit("activation-delete-retry", "adepthood", now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    store.record_failure(
        job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        states = list(
            executor.map(
                lambda _: store.retry(job.job_id, "adepthood", now=_NOW).state,
                range(16),
            )
        )

    assert set(states) == {JobState.DELETING}


def test_delete_is_idempotent_and_remains_durable_until_a_worker_claims_it(
    store: ProvisioningStore,
) -> None:
    """Repeated deletes enqueue one teardown operation."""
    job = store.submit("activation-delete", "adepthood", now=_NOW)

    first = store.request_delete(job.job_id, "adepthood", now=_NOW)
    second = store.request_delete(job.job_id, "adepthood", now=_NOW)
    claim = store.claim_next(now=_NOW)

    assert first == second
    assert first.state is JobState.DELETING
    assert claim is not None
    assert claim.job.state is JobState.DELETING


def _ready_job(store: ProvisioningStore, activation_id: str, allocation: str) -> str:
    """Drive one activation (its own consumer subject) through create."""
    job = store.submit(
        activation_id,
        activation_id,
        requester_identity="adepthood",
        now=_NOW,
    )
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        allocation,
        handoff=lambda: None,
        now=_NOW,
    )
    return job.job_id


def _fail_delete(store: ProvisioningStore, job_id: str, *, retryable: bool) -> None:
    """Claim the queued delete for *job_id* and settle it as a failure."""
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    assert claim.job.job_id == job_id
    store.record_failure(
        job_id,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=retryable,
        now=_NOW,
    )


def _v3_database(path: Path) -> None:
    """Write a v3 file holding a deleting, a failed-delete, and a ready job."""
    stamp = _NOW.isoformat(timespec="microseconds")
    later = (_NOW + timedelta(hours=1)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA)
        connection.executescript(_OWNERSHIP_INDEXES)
        rows = (
            ("job-deleting", "act-deleting", "user-1", "deleting", "delete", later),
            ("job-failed", "act-failed", "user-2", "failed", "delete", later),
            ("job-ready", "act-ready", "user-3", "ready", "create", stamp),
        )
        for job_id, activation, consumer, state, operation, updated in rows:
            connection.execute(
                "INSERT INTO provisioning_jobs (job_id, canonical_activation_id, "
                "requester_identity, consumer_identity, state, operation, "
                "retryable, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    activation,
                    "adepthood",
                    consumer,
                    state,
                    operation,
                    int(state == "failed"),
                    stamp,
                    updated,
                ),
            )
            connection.execute(
                "INSERT INTO provisioning_activation_ids VALUES (?, ?, ?, ?)",
                (activation, "adepthood", consumer, job_id),
            )
            connection.execute(
                "INSERT INTO provisioning_allocations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"alloc-{job_id}",
                    job_id,
                    "adepthood",
                    consumer,
                    f"p-{job_id}",
                    stamp,
                    None,
                ),
            )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()


def test_v3_database_migrates_to_v4_keeping_rows_indexes_and_backfilling_receipts(
    tmp_path: Path,
) -> None:
    """Fleet tables arrive idempotently and pre-upgrade deletions get receipts."""
    database = tmp_path / "provisioning-v3.sqlite3"
    _v3_database(database)

    ProvisioningStore(database)
    first_pass = ProvisioningStore(database).list_deletion_receipts()
    ProvisioningStore(database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(_FLEET_TABLES) <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM provisioning_jobs"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM provisioning_allocations"
        ).fetchone() == (3,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO provisioning_jobs (job_id, canonical_activation_id, "
                "requester_identity, consumer_identity, state, operation, "
                "created_at, updated_at) VALUES ('dup', 'a', 'adepthood', 'user-3', "
                "'pending', 'create', '2026', '2026')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO provisioning_allocations VALUES "
                "('dup', 'job-ready', 'adepthood', 'user-3', 'p-dup', '2026', NULL)"
            )
    receipts = ProvisioningStore(database).list_deletion_receipts()
    assert receipts == first_pass
    assert {receipt.job_id for receipt in receipts} == {"job-deleting", "job-failed"}
    for receipt in receipts:
        assert receipt.outcome is ReceiptOutcome.PENDING
        assert receipt.backfilled is True
        assert receipt.requested_at == _NOW + timedelta(hours=1)
        assert receipt.provider is None
        assert receipt.resource_classes == ()


def test_duplicate_attempts_count_aliases_and_conflicts_but_not_replays(
    store: ProvisioningStore,
) -> None:
    """Idempotent polls cost nothing; every duplicate attempt is a durable count."""
    assert store.duplicate_allocation_attempts() == 0
    first = store.submit("activation-dup-000", "adepthood", now=_NOW)
    for number in (1, 2, 3):
        assert store.submit(f"activation-dup-{number:03d}", "adepthood", now=_NOW) == (
            first
        )
    assert store.duplicate_allocation_attempts() == 3

    assert store.submit("activation-dup-000", "adepthood", now=_NOW) == first
    assert store.duplicate_allocation_attempts() == 3

    with pytest.raises(ActivationConflictError):
        store.submit("activation-dup-000", "other-consumer", now=_NOW)
    assert store.duplicate_allocation_attempts() == 4
    assert store.count_jobs() == 1


def test_request_delete_and_both_ceremony_expiry_paths_open_exactly_one_pending_receipt(
    store: ProvisioningStore,
) -> None:
    """Every path into ``deleting`` opens one receipt; replays never add a second."""
    requested = _ready_job(store, "activation-receipt-request", "alloc-request")
    expired_by_sweep = _ready_job(store, "activation-receipt-sweep", "alloc-sweep")
    expired_by_put = _ready_job(store, "activation-receipt-put", "alloc-put")
    submission = CeremonySubmission.model_validate(
        json.loads(_CEREMONY_VECTORS.read_text(encoding="utf-8"))["submission"]
    )
    late = _NOW + KEY_CEREMONY_TTL

    store.request_delete(requested, "adepthood", now=_NOW)
    store.request_delete(requested, "adepthood", now=_NOW + timedelta(minutes=5))
    _fail_delete(store, requested, retryable=False)
    with pytest.raises(CeremonyExpiredError):
        store.complete_key_ceremony(
            expired_by_put,
            "adepthood",
            submission,
            attested_confidential=False,
            before_settle=None,
            now=late,
        )
    assert store.expire_key_ceremonies(now=late) == 1
    assert store.expire_key_ceremonies(now=late) == 0

    receipts = {receipt.job_id: receipt for receipt in store.list_deletion_receipts()}
    assert set(receipts) == {requested, expired_by_sweep, expired_by_put}
    assert receipts[requested].requested_at == _NOW
    assert receipts[expired_by_sweep].requested_at == late
    assert receipts[expired_by_put].requested_at == late
    for job_id in (expired_by_sweep, expired_by_put):
        assert receipts[job_id].outcome is ReceiptOutcome.PENDING
        assert receipts[job_id].backfilled is False
        assert receipts[job_id].requester_identity == "adepthood"
        assert receipts[job_id].consumer_identity.startswith("activation-receipt-")
        assert receipts[job_id].attempts == 0
        assert receipts[job_id].confirmed_at is None
    assert receipts[requested].outcome is ReceiptOutcome.FAILED
    assert receipts[requested].last_failure_reason is (
        FailureReason.PROVIDER_UNAVAILABLE
    )

    reset = store.request_delete(requested, "adepthood", now=_NOW + timedelta(days=1))
    assert reset.state is JobState.DELETING
    receipt = {r.job_id: r for r in store.list_deletion_receipts()}[requested]
    assert receipt.outcome is ReceiptOutcome.PENDING
    assert receipt.requested_at == _NOW
    assert receipt.attempts == 1
    assert len(store.list_deletion_receipts()) == 3


def test_complete_delete_confirms_the_receipt_only_inside_the_lease_fence(
    store: ProvisioningStore,
) -> None:
    """A lost lease cannot confirm; a held lease confirms atomically with deleted."""
    job_id = _ready_job(store, "activation-receipt-fence", "alloc-fence")
    store.request_delete(job_id, "adepthood", now=_NOW)
    claim = store.claim_next(now=_NOW, lease_for=timedelta(seconds=1))
    assert claim is not None
    store.request_delete(job_id, "adepthood", now=_NOW)  # idempotent, keeps lease

    with pytest.raises(LostJobLeaseError):
        store.complete_delete(job_id, "stale-token", _OUTCOME, now=_NOW)
    pending = store.list_deletion_receipts()[0]
    assert pending.outcome is ReceiptOutcome.PENDING
    assert pending.confirmed_at is None

    settled = store.complete_delete(
        job_id,
        claim.lease_token,
        _OUTCOME,
        now=_NOW + timedelta(seconds=30),
    )
    confirmed = store.list_deletion_receipts()[0]
    assert settled.state is JobState.DELETED
    assert confirmed.outcome is ReceiptOutcome.CONFIRMED
    assert confirmed.confirmed_at == _NOW + timedelta(seconds=30)
    assert confirmed.provider == "fake"
    assert confirmed.provider_allocation_id == "alloc-fence"
    assert confirmed.resource_classes == _OUTCOME.resource_classes
    assert confirmed.requested_at == _NOW
    with pytest.raises(LostJobLeaseError):
        store.complete_delete(job_id, claim.lease_token, _OUTCOME, now=_NOW)


def test_requeue_failed_delete_moves_only_a_retryable_failed_delete_back_to_deleting(
    store: ProvisioningStore,
) -> None:
    """The operator repair primitive is narrow, idempotent, and content-free."""
    retryable = _ready_job(store, "activation-requeue-ok", "alloc-requeue-ok")
    terminal = _ready_job(store, "activation-requeue-no", "alloc-requeue-no")
    ready = _ready_job(store, "activation-requeue-ready", "alloc-requeue-ready")
    store.request_delete(retryable, "adepthood", now=_NOW)
    _fail_delete(store, retryable, retryable=True)
    store.request_delete(terminal, "adepthood", now=_NOW)
    _fail_delete(store, terminal, retryable=False)

    requeued = store.requeue_failed_delete(retryable, now=_NOW + timedelta(hours=1))
    replay = store.requeue_failed_delete(retryable, now=_NOW + timedelta(hours=2))

    assert requeued.state is JobState.DELETING
    assert requeued.operation is JobOperation.DELETE
    assert requeued.retryable is False
    assert requeued.failure_reason is None
    assert requeued.updated_at == _NOW + timedelta(hours=1)
    assert replay == requeued
    for job_id in (terminal, ready, "job-that-does-not-exist"):
        with pytest.raises(InvalidJobTransitionError):
            store.requeue_failed_delete(job_id, now=_NOW)
    receipt = {r.job_id: r for r in store.list_deletion_receipts()}[retryable]
    assert receipt.outcome is ReceiptOutcome.PENDING
    assert receipt.attempts == 1
    assert receipt.last_failure_reason is FailureReason.PROVIDER_UNAVAILABLE


def test_record_machine_state_accrues_between_running_samples_and_splits_months(
    store: ProvisioningStore,
    tmp_path: Path,
) -> None:
    """Running seconds are sampled into bounded per-allocation month buckets."""
    start = datetime(2026, 9, 30, 23, tzinfo=UTC)
    crossed = datetime(2026, 10, 1, 1, tzinfo=UTC)

    first = store.record_machine_state("fly-sample", running=True, now=start)
    replay = store.record_machine_state("fly-sample", running=True, now=start)
    accrued = store.record_machine_state("fly-sample", running=True, now=crossed)
    stopped = store.record_machine_state(
        "fly-sample", running=False, now=crossed + timedelta(minutes=10)
    )
    restarted = store.record_machine_state(
        "fly-sample", running=True, now=crossed + timedelta(minutes=20)
    )
    other = store.record_machine_state("fly-other", running=False, now=crossed)

    assert first == (0, 0)
    assert replay == (0, 0)
    assert accrued == (7200, 3600)
    assert stopped == (0, 3600)
    assert restarted == (0, 3600)
    assert other == (0, 0)
    assert store.running_seconds_by_allocation("2026-09") == {"fly-sample": 3600}
    assert store.running_seconds_by_allocation("2026-10") == {"fly-sample": 3600}
    assert store.running_seconds_by_allocation("2026-11") == {}
    with closing(sqlite3.connect(tmp_path / "provisioning.sqlite3")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provisioning_machine_running"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM provisioning_machine_running_months"
        ).fetchone() == (2,)


def test_list_fleet_jobs_is_fleet_wide_and_requester_scoped_reads_are_unchanged(
    store: ProvisioningStore,
) -> None:
    """Operators see every job; the public API keeps its ownership boundary."""
    ready = _ready_job(store, "activation-fleet-ready", "alloc-fleet-ready")
    deleting = _ready_job(store, "activation-fleet-deleting", "alloc-fleet-deleting")
    store.request_delete(deleting, "adepthood", now=_NOW + timedelta(minutes=1))
    pending = store.submit(
        "activation-fleet-pending",
        "user-9",
        requester_identity="service-b",
        now=_NOW,
    ).job_id

    fleet = {entry.job.job_id: entry for entry in store.list_fleet_jobs()}

    assert set(fleet) == {ready, deleting, pending}
    assert fleet[ready].provider_allocation_id == "alloc-fleet-ready"
    assert fleet[ready].allocation_deleted_at is None
    assert fleet[ready].delete_requested_at is None
    assert fleet[ready].receipt_outcome is None
    assert fleet[deleting].job.state is JobState.DELETING
    assert fleet[deleting].delete_requested_at == _NOW + timedelta(minutes=1)
    assert fleet[deleting].receipt_outcome is ReceiptOutcome.PENDING
    assert fleet[pending].provider_allocation_id is None
    assert fleet[pending].job.requester_identity == "service-b"
    assert store.get(pending, "adepthood") is None
    assert store.get_allocation(ready, "service-b") is None
    assert store.get(ready, "adepthood") == fleet[ready].job
    httpapi_source = (_HTTPAPI / "provisioning.py").read_text(encoding="utf-8")
    for seam in (
        "list_fleet_jobs",
        "list_deletion_receipts",
        "duplicate_allocation_attempts",
        "record_machine_state",
        "running_seconds_by_allocation",
        "requeue_failed_delete",
        "record_budget_month",
        "months_over_budget",
    ):
        assert seam not in httpapi_source


def test_budget_months_record_and_report_most_recent_first(
    store: ProvisioningStore,
) -> None:
    """Durable month history lets the D7 rolling-months trigger be evaluated."""
    budget = Decimal("500.00")
    store.record_budget_month("2026-07", Decimal("512.10"), budget, "USD", now=_NOW)
    store.record_budget_month("2026-08", Decimal("499.99"), budget, "USD", now=_NOW)
    store.record_budget_month("2026-09", Decimal("100.00"), budget, "USD", now=_NOW)
    store.record_budget_month("2026-09", Decimal("500.00"), budget, "USD", now=_NOW)

    assert store.months_over_budget(2) == (True, False)
    assert store.months_over_budget(5) == (True, False, True)
    assert store.months_over_budget(0) == ()
