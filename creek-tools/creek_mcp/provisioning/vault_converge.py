"""Operator batch converge of vault Machines after an upgrade (#1869).

Publishing a new vault image, or changing the runtime config the control plane
writes, leaves every existing vault Machine behind policy.  Routing refuses
such a Machine retryably and never changes it; this module is the single
mutator, run by an operator through ``--converge-vault-machines`` on the
worker or the pilot (owner decision R2-1, 2026-10-27).

The safety rule is the driver's: a Machine is converged only when its
identity is exact and only policy-owned fields differ, only while it is
stopped, never touching its volume, and only with secret files read from its
existing record (the run's records are read-only, so the consumer credential
cannot rotate).  On top of it this module adds:

* a dry run by default, listing each allocation's drifted field names;
* ``apply``, bounded to ``limit`` Machine updates per run;
* a write-ahead journal recording each Machine's prior config (secret file
  values only as digests) before it is updated, so a re-run after any
  interruption resumes — finishing what was started and no-oping what is
  done — and a rollback run can restore the prior config exactly;
* post-update verification: the driver re-verifies the Machine exactly, and
  this module then proves readiness the way a route does;
* a halt at the first Machine whose update or verification failed, so a bad
  image stops the batch at one vault.

The printed document names allocation ids, outcomes and field names only —
never an identity, image, address or secret.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any, Final, Protocol

from creek._fsio import atomic_write_text
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import config_fingerprint
from creek_mcp.provisioning.production_secrets import read_owner_only_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from creek_mcp.provisioning.fly import FlyProviderDriver, VaultMachinePlan
    from creek_mcp.provisioning.models import RoutableAllocation

_LOGGER = logging.getLogger(__name__)

EXIT_CURRENT: Final[int] = 0
"""Nothing left to do: every allocation is current (or restored)."""

EXIT_REFUSED: Final[int] = 2
"""An identity refusal, an unusable journal, or a non-retryable failure."""

EXIT_PENDING: Final[int] = 3
"""Work remains: a dry run found some, or the batch limit was reached."""

EXIT_RETRY: Final[int] = 75
"""A Machine was running or a provider call failed retryably (``EX_TEMPFAIL``)."""

_JOURNAL_VERSION: Final[int] = 1
_MAX_JOURNAL_BYTES: Final[int] = 4 * 1024 * 1024
_JOURNAL_REASON: Final[str] = "journal"
_READINESS_DRIFT: Final[str] = "readiness"


@unique
class ConvergeOutcome(StrEnum):
    """What one allocation's pass did, or would do."""

    WOULD_CONVERGE = "would-converge"
    CONVERGED = "converged"
    WOULD_RESTORE = "would-restore"
    RESTORED = "restored"
    ALREADY_CURRENT = "already-current"
    SKIPPED_RUNNING = "skipped-running"
    REFUSED_IDENTITY = "refused-identity"
    FAILED_RETRYABLE = "failed-retryable"
    FAILED = "failed"


_RETRY_OUTCOMES: Final[frozenset[ConvergeOutcome]] = frozenset(
    {ConvergeOutcome.SKIPPED_RUNNING, ConvergeOutcome.FAILED_RETRYABLE}
)
_REFUSED_OUTCOMES: Final[frozenset[ConvergeOutcome]] = frozenset(
    {ConvergeOutcome.REFUSED_IDENTITY, ConvergeOutcome.FAILED}
)
_PENDING_OUTCOMES: Final[frozenset[ConvergeOutcome]] = frozenset(
    {ConvergeOutcome.WOULD_CONVERGE, ConvergeOutcome.WOULD_RESTORE}
)


@dataclass(frozen=True, slots=True)
class AllocationReport:
    """One allocation's content-free outcome.

    Attributes:
        allocation_id: The provider allocation id (a digest, not an identity).
        outcome: What happened.
        reason: For a failure, the provider reason value or ``journal``.
        drift: The drifted field names the plan found.
    """

    allocation_id: str
    outcome: ConvergeOutcome
    reason: str | None = None
    drift: tuple[str, ...] = ()

    def document(self) -> dict[str, object]:
        """Return the JSON-ready form."""
        entry: dict[str, object] = {
            "provider_allocation_id": self.allocation_id,
            "outcome": self.outcome.value,
        }
        if self.reason is not None:
            entry["reason"] = self.reason
        if self.drift:
            entry["drift"] = list(self.drift)
        return entry


@dataclass(frozen=True, slots=True)
class FleetReport:
    """One run's per-allocation outcomes.

    Attributes:
        mode: ``converge`` or ``rollback``.
        applied: Whether the run could mutate (``--converge-apply``).
        allocations: Each reported allocation, in store order.
        limit_reached: Whether the run stopped at its update bound.
    """

    mode: str
    applied: bool
    allocations: tuple[AllocationReport, ...]
    limit_reached: bool = False

    @property
    def exit_status(self) -> int:
        """Return the process exit status this run maps to."""
        outcomes = {entry.outcome for entry in self.allocations}
        if outcomes & _REFUSED_OUTCOMES:
            return EXIT_REFUSED
        if outcomes & _RETRY_OUTCOMES:
            return EXIT_RETRY
        if self.limit_reached or outcomes & _PENDING_OUTCOMES:
            return EXIT_PENDING
        return EXIT_CURRENT

    def document(self) -> dict[str, object]:
        """Return the JSON-ready, content-free run document."""
        counts: dict[str, int] = {}
        for entry in self.allocations:
            counts[entry.outcome.value] = counts.get(entry.outcome.value, 0) + 1
        return {
            "mode": self.mode,
            "applied": self.applied,
            "limit_reached": self.limit_reached,
            "counts": counts,
            "allocations": [entry.document() for entry in self.allocations],
        }


class RoutableAllocationSet(Protocol):
    """The store's operator listing of every ownership-bound allocation."""

    def list_routable_allocations(self) -> tuple[RoutableAllocation, ...]:
        """Return every ready, provider-managed allocation, in a fixed order."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One Machine's recorded upgrade state.

    Attributes:
        machine_id: The Machine the prior was read from.
        prior: Its content-free config before the converge.
        prior_fingerprint: :func:`config_fingerprint` of *prior*.
        converged_fingerprint: The verified post-update fingerprint, once known.
        verified: Whether readiness was proven after the update.
    """

    machine_id: str
    prior: Mapping[str, Any] = field(repr=False)
    prior_fingerprint: str
    converged_fingerprint: str | None = None
    verified: bool = False

    def document(self) -> dict[str, object]:
        """Return the JSON-ready form."""
        return {
            "machine_id": self.machine_id,
            "prior": dict(self.prior),
            "prior_fingerprint": self.prior_fingerprint,
            "converged_fingerprint": self.converged_fingerprint,
            "verified": self.verified,
        }


class ConvergeJournal:
    """The owner-only write-ahead record of one upgrade's prior configs.

    One journal belongs to one upgrade: an entry is written once, before its
    Machine is first updated, and never overwritten with a later prior, so a
    rollback always restores the config from before this upgrade.
    """

    def __init__(self, path: Path) -> None:
        """Load *path*, or start empty when it does not exist yet.

        Args:
            path: The journal file; its directory must exist.

        Raises:
            ValueError: When the file is not owner-only and regular, or is not
                a journal whose every prior matches its recorded fingerprint.
        """
        self._path = path
        self._entries: dict[str, JournalEntry] = {}
        if os.path.lexists(path):
            self._entries = _load_entries(
                read_owner_only_file(path, maximum=_MAX_JOURNAL_BYTES)
            )

    def entry(self, allocation_id: str) -> JournalEntry | None:
        """Return the entry recorded for *allocation_id*, if any."""
        return self._entries.get(allocation_id)

    def record(self, allocation_id: str, entry: JournalEntry) -> None:
        """Durably write *entry* before the caller acts on it.

        Args:
            allocation_id: The provider allocation id.
            entry: Its new state.
        """
        self._entries[allocation_id] = entry
        document = {
            "version": _JOURNAL_VERSION,
            "entries": {
                key: value.document() for key, value in sorted(self._entries.items())
            },
        }
        atomic_write_text(self._path, json.dumps(document, indent=2, sort_keys=True))


def _load_entries(raw: bytes) -> dict[str, JournalEntry]:
    """Parse and integrity-check one journal document.

    Raises:
        ValueError: When the document is not a valid journal.
    """
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("converge journal is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("version") != _JOURNAL_VERSION:
        raise ValueError("converge journal has no supported version")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("converge journal has no entries")
    return {str(key): _load_entry(value) for key, value in entries.items()}


def _load_entry(value: object) -> JournalEntry:
    """Parse one entry, refusing a prior that no longer matches its digest."""
    if not isinstance(value, dict):
        raise ValueError("converge journal entry is invalid")
    prior = value.get("prior")
    converged = value.get("converged_fingerprint")
    entry = JournalEntry(
        machine_id=str(value.get("machine_id")),
        prior=prior if isinstance(prior, dict) else {},
        prior_fingerprint=str(value.get("prior_fingerprint")),
        converged_fingerprint=None if converged is None else str(converged),
        verified=value.get("verified") is True,
    )
    if not isinstance(prior, dict) or config_fingerprint(prior) != (
        entry.prior_fingerprint
    ):
        raise ValueError("converge journal prior does not match its fingerprint")
    return entry


@dataclass
class _Batch:
    """Mutable state of one bounded run."""

    driver: FlyProviderDriver
    journal: ConvergeJournal | None
    apply: bool
    limit: int
    updates: int = 0
    reports: list[AllocationReport] = field(default_factory=list)
    halted: bool = False
    limit_reached: bool = False

    def admit_update(self) -> bool:
        """Return whether one more update fits, marking the limit otherwise."""
        if self.updates >= self.limit:
            self.limit_reached = True
            return False
        self.updates += 1
        return True

    def require_journal(self) -> ConvergeJournal:
        """Return the journal every mutation records into."""
        if self.journal is None:
            raise ValueError("an applied converge or rollback needs a journal")
        return self.journal


def converge_fleet(
    allocations: RoutableAllocationSet,
    driver: FlyProviderDriver,
    journal: ConvergeJournal | None,
    *,
    apply: bool,
    limit: int,
) -> FleetReport:
    """Plan, or converge at most *limit* of, every allocation's Machine.

    Args:
        allocations: The store's ownership-bound allocation set.
        driver: A Fly driver over read-only records.
        journal: The upgrade's journal; required when *apply* is set.
        apply: Whether to update Machines; otherwise only plan.
        limit: The most Machine updates this run may make.

    Returns:
        The run's report; a refusal or failure is reported, never raised.

    Raises:
        ValueError: When *apply* is set without a journal.
    """
    batch = _Batch(driver, journal, apply, limit)
    if apply:
        batch.require_journal()
    return _run(batch, allocations, "converge", _converge_one)


def rollback_fleet(
    allocations: RoutableAllocationSet,
    driver: FlyProviderDriver,
    journal: ConvergeJournal,
    *,
    apply: bool,
    limit: int,
) -> FleetReport:
    """Plan, or restore at most *limit* of, every journaled Machine.

    Args:
        allocations: The store's ownership-bound allocation set.
        driver: A Fly driver over read-only records.
        journal: The journal of the upgrade to roll back.
        apply: Whether to update Machines; otherwise only plan.
        limit: The most Machine updates this run may make.

    Returns:
        The run's report; allocations the journal never recorded are omitted.
    """
    batch = _Batch(driver, journal, apply, limit)
    return _run(batch, allocations, "rollback", _rollback_one)


def _run(
    batch: _Batch,
    allocations: RoutableAllocationSet,
    mode: str,
    step: Callable[[_Batch, RoutableAllocation], AllocationReport | None],
) -> FleetReport:
    """Run *step* over the allocation set until it ends, halts or is bounded."""
    for allocation in allocations.list_routable_allocations():
        if batch.halted or batch.limit_reached:
            break
        try:
            report = step(batch, allocation)
        except ProviderError as exc:
            report = _failure(allocation.provider_allocation_id, exc)
        if report is not None:
            batch.reports.append(report)
    return FleetReport(mode, batch.apply, tuple(batch.reports), batch.limit_reached)


def _failure(allocation_id: str, exc: ProviderError) -> AllocationReport:
    """Report one provider failure by its public reason only."""
    outcome = (
        ConvergeOutcome.FAILED_RETRYABLE if exc.retryable else ConvergeOutcome.FAILED
    )
    return AllocationReport(allocation_id, outcome, exc.reason.value)


def _converge_one(
    batch: _Batch, allocation: RoutableAllocation
) -> AllocationReport | None:
    """Plan one allocation and, when applying, converge and verify it."""
    allocation_id = allocation.provider_allocation_id
    plan = batch.driver.plan_vault_machine(allocation)
    if not plan.identity_exact:
        return AllocationReport(allocation_id, ConvergeOutcome.REFUSED_IDENTITY)
    entry = None if batch.journal is None else batch.journal.entry(allocation_id)
    if not plan.drift:
        return _finish_current(batch, allocation, plan, entry)
    if not plan.stopped:
        return AllocationReport(
            allocation_id, ConvergeOutcome.SKIPPED_RUNNING, drift=plan.drift
        )
    if not batch.apply:
        return AllocationReport(
            allocation_id, ConvergeOutcome.WOULD_CONVERGE, drift=plan.drift
        )
    if entry is not None and (
        entry.machine_id != plan.machine_id
        or entry.prior_fingerprint != plan.fingerprint
    ):
        return AllocationReport(
            allocation_id, ConvergeOutcome.FAILED, _JOURNAL_REASON, plan.drift
        )
    if not batch.admit_update():
        return None
    return _apply_converge(batch, allocation, plan)


def _finish_current(
    batch: _Batch,
    allocation: RoutableAllocation,
    plan: VaultMachinePlan,
    entry: JournalEntry | None,
) -> AllocationReport:
    """Report a current Machine, finishing a converge an interruption left."""
    allocation_id = allocation.provider_allocation_id
    if entry is None or entry.verified:
        return AllocationReport(allocation_id, ConvergeOutcome.ALREADY_CURRENT)
    if not batch.apply:
        return AllocationReport(
            allocation_id, ConvergeOutcome.WOULD_CONVERGE, drift=(_READINESS_DRIFT,)
        )
    if entry.machine_id != plan.machine_id:
        return AllocationReport(allocation_id, ConvergeOutcome.FAILED, _JOURNAL_REASON)
    journal = batch.require_journal()
    pending = JournalEntry(
        entry.machine_id, entry.prior, entry.prior_fingerprint, plan.fingerprint
    )
    journal.record(allocation_id, pending)
    return _prove_ready(batch, allocation, pending, plan.drift)


def _apply_converge(
    batch: _Batch,
    allocation: RoutableAllocation,
    plan: VaultMachinePlan,
) -> AllocationReport:
    """Record the prior, update the Machine, then prove it ready."""
    allocation_id = allocation.provider_allocation_id
    journal = batch.require_journal()
    entry = JournalEntry(plan.machine_id, plan.config, plan.fingerprint)
    journal.record(allocation_id, entry)
    try:
        converged = batch.driver.converge_vault_machine(allocation, plan)
    except ProviderError as exc:
        batch.halted = True
        return _failure(allocation_id, exc)
    pending = JournalEntry(
        entry.machine_id, entry.prior, entry.prior_fingerprint, converged.fingerprint
    )
    journal.record(allocation_id, pending)
    return _prove_ready(batch, allocation, pending, plan.drift)


def _prove_ready(
    batch: _Batch,
    allocation: RoutableAllocation,
    entry: JournalEntry,
    drift: tuple[str, ...],
) -> AllocationReport:
    """Run the route's readiness proof and record that it held."""
    allocation_id = allocation.provider_allocation_id
    try:
        batch.driver.verify_vault_machine_ready(allocation)
    except ProviderError as exc:
        batch.halted = True
        return _failure(allocation_id, exc)
    batch.require_journal().record(
        allocation_id,
        JournalEntry(
            entry.machine_id,
            entry.prior,
            entry.prior_fingerprint,
            entry.converged_fingerprint,
            verified=True,
        ),
    )
    return AllocationReport(allocation_id, ConvergeOutcome.CONVERGED, drift=drift)


def _rollback_one(
    batch: _Batch, allocation: RoutableAllocation
) -> AllocationReport | None:
    """Plan one journaled allocation's rollback and, when applying, restore it."""
    allocation_id = allocation.provider_allocation_id
    journal = batch.require_journal()
    entry = journal.entry(allocation_id)
    if entry is None:
        return None
    plan = batch.driver.plan_vault_machine(allocation)
    if plan.fingerprint == entry.prior_fingerprint:
        return AllocationReport(allocation_id, ConvergeOutcome.ALREADY_CURRENT)
    if entry.converged_fingerprint is None:
        return AllocationReport(allocation_id, ConvergeOutcome.FAILED, _JOURNAL_REASON)
    if (
        plan.machine_id != entry.machine_id
        or plan.fingerprint != entry.converged_fingerprint
    ):
        return AllocationReport(allocation_id, ConvergeOutcome.REFUSED_IDENTITY)
    if not plan.stopped:
        return AllocationReport(allocation_id, ConvergeOutcome.SKIPPED_RUNNING)
    if not batch.apply:
        return AllocationReport(allocation_id, ConvergeOutcome.WOULD_RESTORE)
    if not batch.admit_update():
        return None
    return _apply_restore(batch, allocation, entry)


def _apply_restore(
    batch: _Batch,
    allocation: RoutableAllocation,
    entry: JournalEntry,
) -> AllocationReport:
    """Restore one Machine and mark its entry as no longer converged."""
    allocation_id = allocation.provider_allocation_id
    try:
        batch.driver.restore_vault_machine(
            allocation,
            entry.prior,
            prior_fingerprint=entry.prior_fingerprint,
            converged_fingerprint=str(entry.converged_fingerprint),
        )
    except ProviderError as exc:
        batch.halted = True
        return _failure(allocation_id, exc)
    batch.require_journal().record(
        allocation_id,
        JournalEntry(entry.machine_id, entry.prior, entry.prior_fingerprint),
    )
    return AllocationReport(allocation_id, ConvergeOutcome.RESTORED)


@dataclass(frozen=True, slots=True)
class ConvergeRequest:
    """What one operator run asked for.

    Attributes:
        journal_path: The upgrade's journal file, if any.
        rollback: Whether to roll back instead of converging.
        apply: Whether to update Machines; otherwise a dry run.
        limit: The most Machine updates this run may make.
    """

    journal_path: Path | None
    rollback: bool = False
    apply: bool = False
    limit: int = 1


def run(
    allocations: RoutableAllocationSet,
    driver: FlyProviderDriver,
    request: ConvergeRequest,
) -> int:
    """Run one converge or rollback pass and print its content-free document.

    Args:
        allocations: The store's ownership-bound allocation set.
        driver: A Fly driver over read-only records.
        request: The operator's options.

    Returns:
        One of :data:`EXIT_CURRENT`, :data:`EXIT_REFUSED`,
        :data:`EXIT_PENDING` or :data:`EXIT_RETRY`.
    """
    try:
        report = _report(allocations, driver, request)
    except ValueError:
        _LOGGER.warning("vault converge: refused(journal)")
        print(json.dumps({"error": "converge journal is unusable"}))
        return EXIT_REFUSED
    document = report.document()
    _LOGGER.info("vault converge: %s %s", report.mode, document["counts"])
    print(json.dumps(document, indent=2, sort_keys=True))
    return report.exit_status


def _report(
    allocations: RoutableAllocationSet,
    driver: FlyProviderDriver,
    request: ConvergeRequest,
) -> FleetReport:
    """Load the journal and run the pass *request* selects.

    Raises:
        ValueError: When the journal is unusable or a mutation lacks one.
    """
    path = request.journal_path
    journal = None if path is None else ConvergeJournal(path)
    if not request.rollback:
        return converge_fleet(
            allocations, driver, journal, apply=request.apply, limit=request.limit
        )
    if journal is None:
        raise ValueError("a rollback needs the upgrade's journal")
    return rollback_fleet(
        allocations, driver, journal, apply=request.apply, limit=request.limit
    )
