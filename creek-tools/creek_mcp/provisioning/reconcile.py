"""Fleet reconciler and telemetry for the provisioning control plane (#1769).

One pass compares the store's desired state - live jobs plus deletions the
provider has not confirmed - with the provider inventory of that bounded set,
and returns a deterministic, content-free ``FleetReport``.  The only repairs
are stopping an overrunning Machine of a live allocation and requeueing a
retryable failed delete; the reconciler is typed against
``FleetInventorySource`` and ``FleetStopper`` and can call no delete.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from creek_mcp.provisioning.budget import (
    estimate_monthly_cost,
    evaluate_alerts,
    evaluate_review_checkpoint,
)
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fleet_schema import month_key
from creek_mcp.provisioning.models import (
    Disposition,
    Divergence,
    DivergenceKind,
    FleetTelemetry,
    JobOperation,
    JobState,
    ResourceClass,
    ResourceState,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from creek_mcp.provisioning.budget import (
        Alert,
        CostEstimate,
        FleetPolicy,
        InjectedUsage,
        ReviewTrigger,
    )
    from creek_mcp.provisioning.inventory import (
        FleetInventorySource,
        FleetStopper,
        ProviderResource,
    )
    from creek_mcp.provisioning.models import FleetJob
    from creek_mcp.provisioning.store import ProvisioningStore

_LOGGER = logging.getLogger(__name__)
_BYTES_PER_GB: Final[int] = 1024**3
_LIVE_STATES: Final[frozenset[JobState]] = frozenset(
    {
        JobState.PENDING,
        JobState.PROVISIONING,
        JobState.AWAITING_KEY_CEREMONY,
        JobState.READY,
    }
)
_INSPECTED_STATES: Final[frozenset[JobState]] = frozenset(
    {JobState.AWAITING_KEY_CEREMONY, JobState.READY}
)
"""Live states whose provider resources must exist exactly once per class."""
_DELETION_KINDS: Final[frozenset[DivergenceKind]] = frozenset(
    {DivergenceKind.UNCONFIRMED_DELETION, DivergenceKind.STUCK_DELETION}
)
_SOURCE_STORE: Final[str] = "store"
_SOURCE_PROVIDER: Final[str] = "provider"
_SOURCE_INJECTED: Final[str] = "injected"
_SOURCE_UNAVAILABLE: Final[str] = "unavailable"
_STORE_FIELDS: Final[tuple[str, ...]] = (
    "activated_allocations",
    "allocations_by_state",
    "running_machine_seconds_by_allocation",
    "running_machine_seconds_fleet",
    "duplicate_allocation_attempts",
    "unconfirmed_deletions",
    "oldest_unconfirmed_deletion_seconds",
)
_PROVIDER_FIELDS: Final[tuple[str, ...]] = (
    "provisioned_volumes",
    "volume_bytes",
    "stopped_rootfs_gb",
    "machines_without_rootfs_size",
    "orphan_resources",
)
_INJECTED_FIELDS: Final[dict[str, str]] = {
    "running_seconds_injected": "running_seconds",
    "snapshot_bytes": "snapshot_bytes",
    "egress_bytes": "egress_bytes",
}
"""Telemetry field -> ``InjectedUsage`` attribute it is copied from."""


class ReconcileUnavailableError(RuntimeError):
    """The provider inventory could not be read; the pass wrote and repaired nothing."""


@dataclass(frozen=True, slots=True)
class FleetReport:
    """The content-free result of one reconciliation pass."""

    observed_at: datetime
    telemetry: FleetTelemetry
    divergences: tuple[Divergence, ...]
    alerts: tuple[Alert, ...]
    estimate: CostEstimate
    review_triggers: tuple[ReviewTrigger, ...]
    inventory_mode: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON document: Decimal -> str, datetime -> ISO, None -> null."""
        document = _jsonable(asdict(self))
        assert isinstance(document, dict)
        return document


def _jsonable(value: object) -> object:
    """Convert report values to JSON primitives without changing their meaning."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _is_live(job: FleetJob) -> bool:
    """Return whether *job* still owns (or is acquiring) provider resources."""
    return job.job.state in _LIVE_STATES or (
        job.job.state is JobState.FAILED and job.job.operation is JobOperation.CREATE
    )


def _is_pending_delete(job: FleetJob) -> bool:
    """Return whether *job* is a deletion the provider has not confirmed."""
    return job.job.state is JobState.DELETING or (
        job.job.state is JobState.FAILED and job.job.operation is JobOperation.DELETE
    )


def _divergence_key(divergence: Divergence) -> tuple[str, str, str, str]:
    """Order divergences deterministically."""
    return (
        divergence.kind.value,
        divergence.provider_allocation_id or "",
        "" if divergence.resource_class is None else divergence.resource_class.value,
        divergence.job_id or "",
    )


def _classify_group(
    provider_allocation_id: str,
    resources: Sequence[ProviderResource],
    desired: FleetJob | None,
) -> Iterator[Divergence]:
    """Compare one allocation's resources with the store's expectation.

    No desired row means every resource is an orphan.  A live row whose
    create is still in flight (or failed) and a pending deletion expect
    nothing in particular.  An inspected row must own exactly one Machine and
    one live volume: more is a duplicate per class, none is missing per class.
    """
    if desired is None:
        yield from _orphans_for(provider_allocation_id, resources)
        return
    if desired.job.state not in _INSPECTED_STATES:
        return
    for resource_class, count in _class_counts(resources):
        if count == 1:
            continue
        kind = (
            DivergenceKind.DUPLICATE_RESOURCE
            if count
            else DivergenceKind.MISSING_RESOURCE
        )
        yield Divergence(
            kind,
            Disposition.REPORTED,
            provider_allocation_id,
            resource_class,
            desired.job.job_id,
            None,
        )


def _orphans_for(
    provider_allocation_id: str,
    resources: Sequence[ProviderResource],
) -> Iterator[Divergence]:
    """Report every resource under an allocation the store does not want."""
    for resource in resources:
        yield Divergence(
            DivergenceKind.ORPHAN_RESOURCE,
            Disposition.REPORTED,
            provider_allocation_id,
            resource.resource_class,
            None,
            None,
        )


def _class_counts(
    resources: Sequence[ProviderResource],
) -> tuple[tuple[ResourceClass, int], ...]:
    """Count the Machines and live volumes in one allocation group."""
    return (
        (ResourceClass.MACHINE, sum(1 for r in resources if _is_machine(r))),
        (ResourceClass.VOLUME, sum(1 for r in resources if _is_live_volume(r))),
    )


def _is_machine(resource: ProviderResource) -> bool:
    """Return whether *resource* is a Machine."""
    return resource.resource_class is ResourceClass.MACHINE


def _is_live_volume(resource: ProviderResource) -> bool:
    """Return whether *resource* is a volume that still exists."""
    return (
        resource.resource_class is ResourceClass.VOLUME
        and resource.state is not ResourceState.DESTROYED
    )


class FleetReconciler:
    """Compare store and provider, report every divergence, repair two of them."""

    def __init__(
        self,
        store: ProvisioningStore,
        inventory: FleetInventorySource,
        stopper: FleetStopper,
        policy: FleetPolicy,
        *,
        extra_app_names: Sequence[str] = (),
        usage: InjectedUsage | None = None,
    ) -> None:
        """Bind the durable store, narrow provider seams, and operator policy."""
        self._store = store
        self._inventory = inventory
        self._stopper = stopper
        self._policy = policy
        self._extra_app_names = tuple(extra_app_names)
        self._usage = usage

    def run_once(
        self,
        *,
        now: datetime,
        repair: bool,
        confidential_compute_changed: bool = False,
    ) -> FleetReport:
        """Run one pass at *now*; repair only when *repair* is set."""
        self._store.expire_key_ceremonies(now=now)
        fleet = self._store.list_fleet_jobs()
        pending_deletes = [job for job in fleet if _is_pending_delete(job)]
        desired = self._desired(
            [job for job in fleet if _is_live(job)] + pending_deletes
        )
        resources = self._list_resources(desired)
        groups: defaultdict[str, list[ProviderResource]] = defaultdict(list)
        for resource in resources:
            groups[resource.provider_allocation_id].append(resource)
        divergences: list[Divergence] = []
        for provider_allocation_id in sorted(set(groups) | set(desired)):
            divergences.extend(
                _classify_group(
                    provider_allocation_id,
                    groups.get(provider_allocation_id, ()),
                    desired.get(provider_allocation_id),
                )
            )
        divergences.extend(
            self._classify_deletions(pending_deletes, now, repair=repair)
        )
        divergences.extend(
            self._observe_machines(resources, desired, now, repair=repair)
        )
        divergences.sort(key=_divergence_key)
        telemetry = self._telemetry(fleet, resources, pending_deletes, divergences, now)
        estimate = estimate_monthly_cost(telemetry, self._policy, now=now)
        return FleetReport(
            observed_at=now,
            telemetry=telemetry,
            divergences=tuple(divergences),
            alerts=evaluate_alerts(divergences, estimate, self._policy),
            estimate=estimate,
            review_triggers=evaluate_review_checkpoint(
                telemetry,
                self._store.months_over_budget(self._policy.review_months_over_budget),
                confidential_compute_changed=confidential_compute_changed,
                policy=self._policy,
            ),
            inventory_mode="derived+injected" if self._extra_app_names else "derived",
        )

    def _desired(self, jobs: Sequence[FleetJob]) -> dict[str, FleetJob]:
        """Index the bounded known set by provider allocation id."""
        return {
            job.provider_allocation_id
            or self._inventory.expected_allocation_id(job.job.activation_id): job
            for job in jobs
        }

    def _list_resources(
        self, desired: Mapping[str, FleetJob]
    ) -> tuple[ProviderResource, ...]:
        """Read the provider inventory or abort the pass with no writes."""
        known_ids = sorted(job.job.activation_id for job in desired.values())
        try:
            return self._inventory.list_resources(
                known_ids,
                app_names=self._extra_app_names,
            )
        except ProviderError as exc:
            raise ReconcileUnavailableError(
                "provider inventory is unavailable"
            ) from exc

    def _classify_deletions(
        self,
        pending_deletes: Sequence[FleetJob],
        now: datetime,
        *,
        repair: bool,
    ) -> list[Divergence]:
        """Age every unconfirmed deletion; requeue retryable failures under repair."""
        stuck_after = self._policy.stuck_deletion_after.total_seconds()
        divergences: list[Divergence] = []
        for entry in pending_deletes:
            requested = entry.delete_requested_at or entry.job.updated_at
            age = max(0, int((now - requested).total_seconds()))
            kind = (
                DivergenceKind.STUCK_DELETION
                if age >= stuck_after
                else DivergenceKind.UNCONFIRMED_DELETION
            )
            disposition = Disposition.REPORTED
            if repair and entry.job.state is JobState.FAILED and entry.job.retryable:
                self._store.requeue_failed_delete(entry.job.job_id, now=now)
                disposition = Disposition.REPAIRED
            divergences.append(
                Divergence(
                    kind,
                    disposition,
                    entry.provider_allocation_id,
                    None,
                    entry.job.job_id,
                    age,
                )
            )
        return divergences

    def _observe_machines(
        self,
        resources: Sequence[ProviderResource],
        desired: Mapping[str, FleetJob],
        now: datetime,
        *,
        repair: bool,
    ) -> list[Divergence]:
        """Sample every Machine; stop an overrunning live one only under repair."""
        limit = self._policy.max_continuous_running.total_seconds()
        divergences: list[Divergence] = []
        for resource in filter(_is_machine, resources):
            continuous, _ = self._store.record_machine_state(
                resource.provider_allocation_id,
                running=resource.state is ResourceState.RUNNING,
                now=now,
            )
            if continuous < limit:
                continue
            entry = desired.get(resource.provider_allocation_id)
            disposition = Disposition.REPORTED
            if repair and entry is not None and _is_live(entry):
                disposition = self._stop(entry.job.activation_id, resource)
            divergences.append(
                Divergence(
                    DivergenceKind.CONTINUOUS_RUNNING,
                    disposition,
                    resource.provider_allocation_id,
                    ResourceClass.MACHINE,
                    None if entry is None else entry.job.job_id,
                    continuous,
                )
            )
        return divergences

    def _stop(self, activation_id: str, resource: ProviderResource) -> Disposition:
        """Stop one Machine; a provider refusal leaves the divergence reported."""
        try:
            self._stopper.stop(activation_id)
        except ProviderError as exc:
            _LOGGER.info(
                "fleet repair refused kind=continuous_running subject=%s reason=%s",
                resource.provider_allocation_id,
                exc.reason.value,
            )
            return Disposition.REPORTED
        return Disposition.REPAIRED

    def _telemetry(
        self,
        fleet: Sequence[FleetJob],
        resources: Sequence[ProviderResource],
        pending_deletes: Sequence[FleetJob],
        divergences: Sequence[Divergence],
        now: datetime,
    ) -> FleetTelemetry:
        """Assemble the seven ADR-0013 D4 measurements with their provenance."""
        measures = _ProviderMeasures.of(resources)
        running = self._store.running_seconds_by_allocation(month_key(now))
        usage = self._usage
        return FleetTelemetry(
            activated_allocations=self._store.count_allocations(active_only=True),
            allocations_by_state=dict(
                sorted(Counter(job.job.state.value for job in fleet).items())
            ),
            provisioned_volumes=measures.provisioned_volumes,
            volume_bytes=measures.volume_gb * _BYTES_PER_GB,
            stopped_rootfs_gb=measures.stopped_rootfs_gb,
            machines_without_rootfs_size=measures.machines_without_rootfs_size,
            running_machine_seconds_by_allocation=running,
            running_machine_seconds_fleet=sum(running.values()),
            running_seconds_injected=_injected(usage, "running_seconds"),
            snapshot_bytes=_injected(usage, "snapshot_bytes"),
            egress_bytes=_injected(usage, "egress_bytes"),
            duplicate_allocation_attempts=self._store.duplicate_allocation_attempts(),
            orphan_resources=_count_kind(divergences, DivergenceKind.ORPHAN_RESOURCE),
            unconfirmed_deletions=len(pending_deletes),
            oldest_unconfirmed_deletion_seconds=_oldest_deletion_age(divergences),
            sources=_sources(usage),
        )


@dataclass(frozen=True, slots=True)
class _ProviderMeasures:
    """Provider-side telemetry summed from one inventory listing."""

    provisioned_volumes: int
    volume_gb: int
    stopped_rootfs_gb: int
    machines_without_rootfs_size: int

    @classmethod
    def of(cls, resources: Sequence[ProviderResource]) -> _ProviderMeasures:
        """Sum live volumes and Machine root filesystems from *resources*."""
        volumes = list(filter(_is_live_volume, resources))
        machines = list(filter(_is_machine, resources))
        return cls(
            provisioned_volumes=len(volumes),
            volume_gb=_gb_total(volumes),
            stopped_rootfs_gb=_gb_total(list(filter(_is_stopped, machines))),
            machines_without_rootfs_size=len(machines)
            - len(list(filter(_has_size, machines))),
        )


def _is_stopped(resource: ProviderResource) -> bool:
    """Return whether a Machine is stopped (its root filesystem is billed idle)."""
    return resource.state is ResourceState.STOPPED


def _has_size(resource: ProviderResource) -> bool:
    """Return whether the provider reported a size for *resource*."""
    return resource.size_gb is not None


def _gb_total(resources: Sequence[ProviderResource]) -> int:
    """Sum the known sizes of *resources* in GB."""
    return sum(r.size_gb or 0 for r in resources)


def _injected(usage: InjectedUsage | None, attribute: str) -> int | None:
    """Return one injected usage figure, or None when nothing was injected."""
    if usage is None:
        return None
    value: int | None = getattr(usage, attribute)
    return value


def _count_kind(divergences: Sequence[Divergence], kind: DivergenceKind) -> int:
    """Count divergences of one kind."""
    return sum(1 for d in divergences if d.kind is kind)


def _oldest_deletion_age(divergences: Sequence[Divergence]) -> int | None:
    """Return the oldest unconfirmed-deletion age, or None when there is none."""
    ages = [
        d.age_seconds
        for d in divergences
        if d.kind in _DELETION_KINDS and d.age_seconds is not None
    ]
    return max(ages) if ages else None


def _sources(usage: InjectedUsage | None) -> dict[str, str]:
    """Label every telemetry field with where its value came from."""
    sources = dict.fromkeys(_STORE_FIELDS, _SOURCE_STORE)
    sources.update(dict.fromkeys(_PROVIDER_FIELDS, _SOURCE_PROVIDER))
    for name, attribute in _INJECTED_FIELDS.items():
        injected = usage is not None and getattr(usage, attribute) is not None
        sources[name] = _SOURCE_INJECTED if injected else _SOURCE_UNAVAILABLE
    return sources
