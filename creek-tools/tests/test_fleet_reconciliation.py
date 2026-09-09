"""Report-only fleet reconciliation contract for issue #1769.

ADR-0013 Decision 6 requires that every provider resource Creek pays for is
reconciled against the durable control plane, and that a deletion the provider
never confirmed stays visible until it does. Nothing in the control plane could
observe that before this module: :class:`~creek_mcp.provisioning.driver.ProviderDriver`
declares only ``provision`` and ``delete``, and Fly app names are
``sha256(activation_id)[:24]`` — a one-way derivation — so an app with no store
row cannot be attributed by inverting the digest.

The suite therefore pins three things at once: an org-wide *enumeration*
capability that is deliberately separate from the provisioning capability, a
reconciler that is a pure function of (store snapshot, inventory snapshot,
clock), and the content-freedom of everything it emits. The plaintext
activation id that ``FlyProviderDriver._machine_request`` writes into Machine
metadata must never re-enter a report, so several assertions below exist only
to catch an implementation that reads it back.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from creek_mcp.provisioning.ceremony import KEY_CEREMONY_TTL
from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.inventory import (
    InventorySnapshot,
    MetricQuality,
    ProviderInventory,
    ProviderResource,
    ProviderResourceClass,
)
from creek_mcp.provisioning.models import FailureReason, JobOperation, JobState
from creek_mcp.provisioning.reconcile import (
    DivergenceKind,
    FleetDivergence,
    FleetReconcilePolicy,
    FleetReconciler,
    FleetReconciliationError,
    FleetReconciliationReport,
    ReconcileMode,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_api_support import FakeFlyAPI, build_driver
from tests.provisioning_report_only_support import (
    DYNAMIC_DISPATCH as _DYNAMIC_DISPATCH,
)
from tests.provisioning_report_only_support import (
    MUTATING_OPERATIONS as _MUTATING_OPERATIONS,
)
from tests.provisioning_secret_support import (
    FORBIDDEN_FIELD_NAMES,
    assert_content_free,
)

_RECONCILE_SOURCE: Final[str] = (
    Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning" / "reconcile.py"
).read_text(encoding="utf-8")

_NOW = datetime(2026, 9, 8, 4, tzinfo=UTC)
_ORG = "creek-vaults"
_LIVE_ACTIVATION = "activation-A"
_ORPHAN_ACTIVATION = "activation-B"
_POLICY = FleetReconcilePolicy(
    unconfirmed_deletion_after=timedelta(minutes=15),
    max_continuous_running=timedelta(hours=6),
)


class _UnclassifiableInventory:
    """Serve one resource whose class is outside the closed enum."""

    def __init__(self, surrogate: str) -> None:
        """Attribute the bogus resource to an allocation the store knows."""
        self._surrogate = surrogate

    def list_resources(self) -> InventorySnapshot:
        """Return one resource carrying a class no Creek allocation can hold."""
        return InventorySnapshot(
            resources=(
                ProviderResource(
                    resource_class=cast("ProviderResourceClass", "gpu"),
                    provider_id="gpu-1",
                    provider_allocation_id=self._surrogate,
                    state="stopped",
                ),
            ),
            complete=True,
        )


def _surrogate(activation_id: str) -> str:
    """Return the allocation surrogate Fly app names are derived from."""
    return hashlib.sha256(activation_id.encode()).hexdigest()[:24]


def _plant_orphan(api: FakeFlyAPI, activation_id: str) -> str:
    """Create provider state the durable store has never seen, and return it."""
    suffix = _surrogate(activation_id)
    app_name = f"creek-vault-{suffix}"
    allocation_id = f"fly-{suffix}"
    api.apps[app_name] = {
        "id": "app-orphan",
        "name": app_name,
        "organization": {"slug": _ORG},
        "network": allocation_id,
    }
    api.volumes[app_name].append(
        {
            "id": "vol-orphan",
            "name": f"{allocation_id}-vault",
            "region": "iad",
            "size_gb": 5,
            "encrypted": True,
            "state": "created",
        }
    )
    api.machines[app_name].append(
        {
            "id": "machine-orphan",
            "name": f"{allocation_id}-machine",
            "region": "iad",
            "state": "started",
            "config": {},
        }
    )
    return allocation_id


def test_reconciler_reports_a_provider_app_with_no_live_allocation_record(
    tmp_path: Path,
) -> None:
    """An unrecorded Fly app is reported, idempotently, without any mutation."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    live = f"fly-{_surrogate(_LIVE_ACTIVATION)}"
    baseline = len(api.requests)

    reconciler = FleetReconciler(store, driver, _POLICY, clock=lambda: _NOW)
    first = reconciler.reconcile()
    second = reconciler.reconcile()

    assert first.mode is ReconcileMode.REPORT_ONLY
    assert first.inventory_complete is True
    assert {divergence.kind for divergence in first.divergences} == {
        DivergenceKind.ORPHAN_PROVIDER_RESOURCE
    }
    assert {divergence.subject for divergence in first.divergences} == {orphan}
    assert {divergence.resource_class for divergence in first.divergences} >= {
        ProviderResourceClass.APP,
        ProviderResourceClass.MACHINE,
        ProviderResourceClass.VOLUME,
    }
    assert live not in repr(first)
    assert_content_free(repr(first))
    rendered = repr(first) + repr(
        [dataclasses.asdict(divergence) for divergence in first.divergences]
    )
    assert _LIVE_ACTIVATION not in rendered
    assert _ORPHAN_ACTIVATION not in rendered
    assert second == first
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def _provisioned(tmp_path: Path) -> tuple[ProvisioningStore, FakeFlyAPI, str]:
    """Drive one activation through the real worker and return its context."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    return store, api, job.job_id


def _reconcile(
    store: ProvisioningStore,
    inventory: ProviderInventory,
    *,
    now: datetime = _NOW,
) -> FleetReconciliationReport:
    """Run one report-only pass at a fixed instant."""
    return FleetReconciler(store, inventory, _POLICY, clock=lambda: now).reconcile()


def test_reported_models_carry_no_credential_or_activation_field() -> None:
    """Content-freedom is structural: no field can hold a secret or a preimage."""
    declared = {
        field.name
        for model in (ProviderResource, FleetDivergence, FleetReconciliationReport)
        for field in dataclasses.fields(model)
    }

    assert declared.isdisjoint(FORBIDDEN_FIELD_NAMES)
    assert not any("activation" in name for name in declared)


def test_apps_outside_the_prefix_or_organization_never_enter_the_inventory(
    tmp_path: Path,
) -> None:
    """Enumeration is scoped by organization and by Creek's own app prefix."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    api.apps["someone-elses-app"] = {
        "id": "app-foreign-name",
        "name": "someone-elses-app",
        "organization": {"slug": _ORG},
        "network": "n/a",
    }
    api.apps["creek-vault-" + "f" * 24] = {
        "id": "app-foreign-org",
        "name": "creek-vault-" + "f" * 24,
        "organization": {"slug": "someone-elses-org"},
        "network": "n/a",
    }

    resources = driver.list_resources().resources
    report = _reconcile(store, driver)

    assert {resource.provider_id for resource in resources} == {
        f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}",
        "vol-1",
        "machine-1",
    }
    assert report.divergences == ()


def test_snapshots_are_enumerated_because_fly_bills_them_separately(
    tmp_path: Path,
) -> None:
    """A snapshot bills on its own and is invisible to every other endpoint."""
    _, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.snapshots[(app_name, "vol-1")].append(
        {"id": "snap-1", "size": 4096, "created_at": "2026-09-08T03:00:00Z"}
    )

    snapshots = [
        resource
        for resource in driver.list_resources().resources
        if resource.resource_class is ProviderResourceClass.SNAPSHOT
    ]

    assert len(snapshots) == 1
    assert snapshots[0].provider_id == "snap-1"
    assert snapshots[0].size_bytes == 4096
    assert snapshots[0].provider_allocation_id == f"fly-{_surrogate(_LIVE_ACTIVATION)}"


def test_an_incomplete_inventory_never_reports_every_allocation_as_missing(
    tmp_path: Path,
) -> None:
    """A rate-limited org listing is not evidence that the fleet vanished."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    api.fail_once("GET", "/v1/apps")
    baseline = len(api.requests)

    report = _reconcile(store, driver)

    assert {method for method, _ in api.requests[baseline:]} == {"GET"}
    assert report.inventory_complete is False
    assert report.divergences == ()


def test_a_vanished_app_is_reported_missing_once_the_inventory_is_complete(
    tmp_path: Path,
) -> None:
    """A live allocation the provider no longer shows is a divergence."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    api.apps.clear()
    baseline = len(api.requests)

    report = _reconcile(store, driver)

    assert {method for method, _ in api.requests[baseline:]} == {"GET"}
    assert report.inventory_complete is True
    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.MISSING_PROVIDER_RESOURCE,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )


def test_a_failed_delete_stays_visible_until_the_provider_confirms_removal(
    tmp_path: Path,
) -> None:
    """A delete that raised parks at state 'failed' with a live Fly bill.

    ``record_failure`` settles through ``_settle_claim(state=FAILED)``, which
    never sets ``deleted_at``, and nothing re-queues that row. Reconciling only
    stale ``deleting`` rows would miss the single most expensive divergence
    ADR-0013 Decision 6 exists to prevent.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    api.fail_once("DELETE", "/machines/machine-1")
    worker.run_once(now=_NOW)
    baseline = len(api.requests)

    settled = store.get(job.job_id, "adepthood")
    report = _reconcile(store, driver, now=_NOW + timedelta(hours=1))

    assert settled is not None
    assert settled.state is JobState.FAILED
    assert settled.operation is JobOperation.DELETE
    assert store.count_allocations(active_only=True) == 1
    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.DELETION_UNCONFIRMED,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_second_billable_volume_under_one_allocation_is_reported(
    tmp_path: Path,
) -> None:
    """Provider-side duplication is observed, so no failure reason is added."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.volumes[app_name].append(
        {
            "id": "vol-2",
            "name": "stray",
            "region": "iad",
            "size_gb": 5,
            "encrypted": True,
            "state": "created",
        }
    )
    baseline = len(api.requests)

    report = _reconcile(store, driver)

    assert {divergence.kind for divergence in report.divergences} == {
        DivergenceKind.DUPLICATE_ALLOCATION
    }
    assert {divergence.provider_id for divergence in report.divergences} == {
        "vol-1",
        "vol-2",
    }
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_machine_running_past_the_window_is_reported_but_never_stopped(
    tmp_path: Path,
) -> None:
    """Scale-to-zero drift is measured; the reconciler still mutates nothing."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    machine = api.machines[app_name][0]
    machine["state"] = "started"
    machine["updated_at"] = (_NOW - timedelta(days=1)).isoformat()
    baseline = len(api.requests)

    report = _reconcile(store, driver)

    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.RUNNING_BEYOND_POLICY,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
            resource_class=ProviderResourceClass.MACHINE,
            provider_id="machine-1",
        ),
    )
    assert report.unmetered_running == ()
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_machine_with_no_readable_meter_is_surfaced_not_assumed_compliant(
    tmp_path: Path,
) -> None:
    """An unreadable uptime meter is reported as unreadable, never as compliant.

    Fly's proxy can start a Machine with no Creek call to observe, so a
    duration is never invented. Silently omitting the Machine would be the
    other failure: an operator would see a clean report and have no way to tell
    a compliant Machine from one nobody could measure.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    machine = api.machines[f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"][0]
    machine["state"] = "started"
    baseline = len(api.requests)

    observed = driver.list_resources()
    report = _reconcile(store, driver)

    assert [
        resource.last_modified_quality
        for resource in observed.resources
        if resource.resource_class is ProviderResourceClass.MACHINE
    ] == [MetricQuality.UNAVAILABLE]
    assert report.divergences == ()
    assert report.unmetered_running == (f"fly-{_surrogate(_LIVE_ACTIVATION)}",)
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_the_fake_driver_also_satisfies_the_inventory_capability(
    tmp_path: Path,
) -> None:
    """Every criterion is provable without a provider credential or spend."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    driver.adopt_orphan("fake-orphaned-allocation")

    report = _reconcile(store, driver)

    assert {divergence.kind for divergence in report.divergences} == {
        DivergenceKind.ORPHAN_PROVIDER_RESOURCE
    }
    assert {divergence.subject for divergence in report.divergences} == {
        "fake-orphaned-allocation"
    }
    assert store.get_allocation(job.job_id, "adepthood") is not None
    assert driver.delete_count == 0
    assert driver.allocation_count == 1


def test_an_unclassifiable_resource_class_raises_instead_of_escaping_the_report(
    tmp_path: Path,
) -> None:
    """The closed enum fails loudly rather than letting a resource go unreported."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    surrogate = store.live_allocations()[0].provider_allocation_id

    with pytest.raises(FleetReconciliationError):
        _reconcile(store, _UnclassifiableInventory(surrogate))


def test_reconciliation_thresholds_must_make_a_divergence_observable() -> None:
    """Injected policy is validated the way FlyProviderPolicy validates its own."""
    with pytest.raises(ValueError, match="unconfirmed_deletion_after"):
        FleetReconcilePolicy(
            unconfirmed_deletion_after=timedelta(seconds=-1),
            max_continuous_running=timedelta(hours=6),
        )
    with pytest.raises(ValueError, match="max_continuous_running"):
        FleetReconcilePolicy(
            unconfirmed_deletion_after=timedelta(minutes=15),
            max_continuous_running=timedelta(0),
        )


_RECONCILE_IMPORTS: Final[frozenset[str]] = frozenset(
    {
        "annotations",
        "defaultdict",
        "dataclass",
        "field",
        "timedelta",
        "StrEnum",
        "unique",
        "TYPE_CHECKING",
        "Final",
        "ProviderError",
        "MetricQuality",
        "ProviderResourceClass",
        "JobOperation",
        "Callable",
        "Iterator",
        "Sequence",
        "datetime",
        "ProviderInventory",
        "ProviderResource",
        "OperatorAllocationView",
        "ProvisioningStore",
    }
)
"""Every name reconcile.py may import. All of them are read-only or inert."""


def test_the_reconciler_module_trips_on_the_spellings_of_a_repair_path() -> None:
    """A tripwire over the common spellings, not a proof, and not presented as one.

    What it actually enforces: no attribute call, bare-name call or attribute
    reference in ``reconcile.py`` names a mutating operation on **either**
    seam — the shared set in ``tests/provisioning_report_only_support.py``
    covers the provider driver and the durable store alike, and this suite's
    own copy used to cover only the provider half; no dynamic-dispatch builtin
    is called; and the module's import set is exactly the listed read-only
    names, so a mutating helper cannot be reached from another module without
    failing this test first.

    What it cannot enforce: attribute access has spellings this does not
    enumerate (``__getattribute__`` reached through a variable, an operator
    dunder, a string fed to a callable obtained some other way), and no
    syntax-level check closes that set. Treating this as a proof would be the
    overclaim the review caught.

    The behavioural guarantee lives on the wire instead: every reconcile test
    in this module asserts the pass issued nothing but GETs, and the fake
    driver's teardown counter stays at zero. That is what actually holds the
    report-only constraint; this test is the cheap early warning in front of it.
    """
    tree = ast.parse(_RECONCILE_SOURCE)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_attributes = {node.attr for node in calls if isinstance(node, ast.Attribute)}
    called_names = {node.id for node in calls if isinstance(node, ast.Name)}
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert list(ReconcileMode) == [ReconcileMode.REPORT_ONLY]
    assert imported == _RECONCILE_IMPORTS
    assert called_attributes.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_MUTATING_OPERATIONS)
    assert referenced.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_DYNAMIC_DISPATCH)
    assert referenced.isdisjoint(
        _DYNAMIC_DISPATCH | {"__getattr__", "__getattribute__"}
    )


def _protocol_methods(protocol: type) -> set[str]:
    """Return the public method names one Protocol declares."""
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    }


def test_the_durable_worker_gains_no_enumeration_capability() -> None:
    """Provisioning and enumeration stay narrow in opposite directions."""
    assert _protocol_methods(ProviderDriver) == {"provision", "delete"}
    assert _protocol_methods(ProviderInventory) == {"list_resources"}


def test_a_negative_unconfirmed_window_is_refused(tmp_path: Path) -> None:
    """A negative window would silently select rows from the future."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")

    with pytest.raises(ValueError, match="older_than"):
        store.unconfirmed_deletions(timedelta(seconds=-1))


class _MalformedAppListing(FakeFlyAPI):
    """Serve an org listing that answers 200 with the wrong body shape."""

    @classmethod
    def of(cls, api: FakeFlyAPI) -> _MalformedAppListing:
        """Wrap an already-provisioned account so its state is preserved."""
        corrupted = cls()
        corrupted.apps = api.apps
        corrupted.volumes = api.volumes
        corrupted.machines = api.machines
        corrupted.snapshots = api.snapshots
        corrupted.requests = api.requests
        return corrupted

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Corrupt only the org listing, leaving every other route intact."""
        if request.url.path == "/v1/apps" and request.method == "GET":
            self.requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"apps": "not-a-list"}, request=request)
        return super().handle(request)


class _FailingInventory:
    """An injected inventory boundary that raises rather than reporting."""

    def list_resources(self) -> InventorySnapshot:
        """Refuse the read the way a third-party implementation might."""
        raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)


def test_a_malformed_org_listing_is_an_unavailable_provider_not_an_empty_fleet(
    tmp_path: Path,
) -> None:
    """A 200 whose body is the wrong shape must not read as "nothing exists"."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(_MalformedAppListing.of(api))
    baseline = len(api.requests)

    observed = driver.list_resources()
    report = _reconcile(store, driver)

    assert observed == InventorySnapshot(resources=(), complete=False)
    assert report.inventory_complete is False
    assert report.divergences == ()
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_fields_fly_omits_are_recorded_as_absent_rather_than_invented(
    tmp_path: Path,
) -> None:
    """Fly's listings are sparse; a missing or naive field is never guessed at."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"
    api.apps[app_name] = {
        "id": "app-sparse",
        "name": app_name,
        "organization": {"slug": _ORG},
    }
    api.machines[app_name].extend(
        [
            {
                "id": "machine-offsetless",
                "state": "started",
                "updated_at": "2026-09-07T04:00:00",
            },
            {"id": "machine-unparseable", "state": "started", "updated_at": "whenever"},
            {"id": "machine-bare"},
            {"id": "machine-no-rootfs", "state": "stopped", "config": {"image": "x"}},
        ]
    )
    api.volumes[app_name].append({"id": "vol-bare"})
    api.snapshots[(app_name, "vol-bare")].append({"id": "snap-bare", "size": "large"})
    baseline = len(api.requests)

    observed = driver.list_resources()
    resources = {resource.provider_id: resource for resource in observed.resources}
    report = _reconcile(store, driver)

    for machine in ("machine-offsetless", "machine-unparseable", "machine-bare"):
        assert resources[machine].last_modified_at is None
        assert resources[machine].last_modified_quality is MetricQuality.UNAVAILABLE
    assert resources["machine-bare"].state == "unknown"
    assert resources["machine-bare"].region is None
    # A Machine with no ``config`` at all, and one whose config omits the
    # ``rootfs`` key Creek itself writes, both record an absent size rather
    # than the policy's configured default — an assumption reported as an
    # observation is the defect this whole test exists to catch (#1769 PR2).
    assert resources["machine-bare"].size_gb is None
    assert resources["machine-no-rootfs"].size_gb is None
    assert resources["vol-bare"].size_gb is None
    assert resources["snap-bare"].size_bytes is None
    assert {divergence.kind for divergence in report.divergences} == {
        DivergenceKind.ORPHAN_PROVIDER_RESOURCE
    }
    assert observed.complete is True
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_repeatedly_reclaimed_stuck_delete_still_ages_past_the_window(
    tmp_path: Path,
) -> None:
    """A re-claim must not reset the clock the staleness window measures.

    ``claim_next`` rewrites ``updated_at`` on every claim, so a delete that
    hangs, loses its lease and is re-claimed keeps pushing that column forward.
    Predicating staleness on it means the most expensive divergence — a delete
    stuck mid-flight with the volume still billing — never ages past the
    window, and ``_missing``'s DELETE skip removes the only other backstop.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    for hour in (1, 2):
        assert store.claim_next(now=_NOW + timedelta(hours=hour)) is not None
        api.machines[app_name].clear()

    report = _reconcile(store, driver, now=_NOW + timedelta(hours=2, minutes=1))

    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.DELETION_UNCONFIRMED,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )


def test_a_destroyed_volume_left_by_a_disk_restore_is_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """Fly's own restore flow leaves a destroyed volume beside the live one.

    ``fly volumes destroy`` then ``fly volumes create --snapshot-id`` leaves two
    rows under one app and bills for one. Counting both raises a duplicate-cost
    divergence for a fleet that is costing exactly what it should, and a false
    positive on a cost alarm is how operators learn to ignore the alarm.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.volumes[app_name][0]["state"] = "destroyed"
    api.volumes[app_name].append(
        {
            "id": "vol-2",
            "name": f"fly-{_surrogate(_LIVE_ACTIVATION)}-vault",
            "region": "iad",
            "size_gb": 5,
            "encrypted": True,
            "state": "created",
        }
    )
    api.machines[app_name].append({"id": "machine-2", "state": "destroyed"})

    observed = driver.list_resources()
    report = _reconcile(store, driver)

    assert report.divergences == ()
    assert {
        resource.provider_id
        for resource in observed.resources
        if resource.resource_class
        in {ProviderResourceClass.VOLUME, ProviderResourceClass.MACHINE}
    } == {"vol-2", "machine-1"}


def test_a_machine_running_inside_the_window_is_not_reported(
    tmp_path: Path,
) -> None:
    """The configured window is load-bearing, not decoration.

    Without this case the whole ``max_continuous_running`` threshold can be
    ignored — inverting the deadline arithmetic still leaves the positive test
    green, because a Machine started a day ago is on the far side of the
    comparison either way.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    machine = api.machines[f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"][0]
    machine["state"] = "started"
    machine["updated_at"] = (_NOW - timedelta(hours=1)).isoformat()

    report = _reconcile(store, driver)

    assert _POLICY.max_continuous_running == timedelta(hours=6)
    assert report.divergences == ()
    assert report.unmetered_running == ()


def test_a_delete_that_failed_inside_the_window_is_not_yet_unconfirmed(
    tmp_path: Path,
) -> None:
    """A delete gets its configured grace period before it is called stuck."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    api.fail_once("DELETE", "/machines/machine-1")
    worker.run_once(now=_NOW)

    report = _reconcile(store, driver, now=_NOW + timedelta(minutes=1))

    assert _POLICY.unconfirmed_deletion_after == timedelta(minutes=15)
    assert report.divergences == ()


def test_a_delete_still_in_flight_past_the_window_is_unconfirmed(
    tmp_path: Path,
) -> None:
    """The 'deleting' arm of the predicate carries its own weight.

    A delete claimed by a worker that then vanished parks in ``deleting`` with
    a live allocation row, and narrowing the predicate to ``failed`` alone
    leaves that case entirely unreported.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)

    settled = store.get(job.job_id, "adepthood")
    report = _reconcile(store, driver, now=_NOW + timedelta(hours=1))

    assert settled is not None
    assert settled.state is JobState.DELETING
    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.DELETION_UNCONFIRMED,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )


def test_a_confirmed_delete_leaves_nothing_for_reconciliation_to_find(
    tmp_path: Path,
) -> None:
    """``deleted_at IS NULL`` is the fence that retires a settled allocation.

    Every other test in this module reconciles a fleet that was never
    successfully torn down, so dropping the clause changes nothing they can
    see. Here the teardown succeeds: the allocation must leave
    ``live_allocations`` and the provider must show nothing left to bill.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    assert worker.run_once(now=_NOW) is True
    baseline = len(api.requests)

    settled = store.get(job.job_id, "adepthood")
    report = _reconcile(store, driver, now=_NOW + timedelta(days=7))

    assert settled is not None
    assert settled.state is JobState.DELETED
    assert store.live_allocations() == []
    assert store.count_allocations(active_only=True) == 0
    assert report.divergences == ()
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_two_passes_at_different_instants_still_compare_equal(
    tmp_path: Path,
) -> None:
    """``observed_at`` is excluded from equality, and that is worth proving.

    Drawing both passes from one frozen clock would make the idempotence claim
    true for the wrong reason: the timestamps would be equal anyway, so
    removing ``field(compare=False)`` would change nothing.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)

    first = _reconcile(store, driver, now=_NOW)
    second = _reconcile(store, driver, now=_NOW + timedelta(days=3))

    assert first.observed_at != second.observed_at
    assert first == second
    assert {divergence.subject for divergence in first.divergences} == {orphan}


def test_many_snapshots_under_one_allocation_are_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """Snapshots are unbounded by design; only Machines and volumes are not.

    ``_duplicates`` filters to live allocations before classifying, so without
    this case the SNAPSHOT arm of the closed classification is never reached
    at all and could return either answer unnoticed.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.snapshots[(app_name, "vol-1")].extend(
        [
            {"id": "snap-1", "size": 4096},
            {"id": "snap-2", "size": 8192},
            {"id": "snap-3", "size": 16384},
        ]
    )
    baseline = len(api.requests)

    observed = driver.list_resources()
    report = _reconcile(store, driver)

    assert [
        resource.provider_id
        for resource in observed.resources
        if resource.resource_class is ProviderResourceClass.SNAPSHOT
    ] == ["snap-1", "snap-2", "snap-3"]
    assert report.divergences == ()
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_partial_enumeration_still_reports_what_it_did_observe(
    tmp_path: Path,
) -> None:
    """A failure part-way through must not read as a clean fleet.

    Discarding everything on the first ``ProviderError`` turns a partial read
    into an empty one, and an empty report is indistinguishable from a fleet
    with no divergences at all — while the orphan that was already seen keeps
    billing.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    machines_path = f"/v1/apps/creek-vault-{_surrogate(_LIVE_ACTIVATION)}/machines"
    baseline = len(api.requests)

    api.fail_once("GET", machines_path)
    observed = driver.list_resources()
    api.fail_once("GET", machines_path)
    report = _reconcile(store, driver)

    assert observed.complete is False
    assert report.inventory_complete is False
    assert {divergence.subject for divergence in report.divergences} == {orphan}
    assert DivergenceKind.MISSING_PROVIDER_RESOURCE not in {
        divergence.kind for divergence in report.divergences
    }
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_an_inventory_boundary_that_raises_is_still_handled(
    tmp_path: Path,
) -> None:
    """The Protocol is injected, so a third party may raise instead of report."""
    store, _, _ = _provisioned(tmp_path)

    report = _reconcile(store, _FailingInventory())

    assert report.inventory_complete is False
    assert report.divergences == ()


def _hammer_claims(
    store: ProvisioningStore, since: datetime, minutes: tuple[int, ...]
) -> None:
    """Claim the queued teardown repeatedly, as a crash-looping worker would."""
    for minute in minutes:
        assert store.claim_next(now=since + timedelta(minutes=minute)) is not None


def test_an_expired_ceremony_teardown_gets_a_clock_a_reclaim_cannot_move(
    tmp_path: Path,
) -> None:
    """The unattended teardown path needs the same stable clock as request_delete.

    ``expire_key_ceremonies`` runs on every worker tick and moves a job to
    ``deleting``/``delete`` for a consumer who never completed the ceremony —
    by which point the Machine, the encrypted volume and the allocation row all
    exist. If that transition leaves ``delete_requested_at`` NULL the row falls
    back onto ``updated_at``, the very clock every re-claim resets, so a
    teardown that never converges is never reported. Neither other detector can
    cover it: ``_missing`` skips delete operations and ``_orphans`` cannot fire
    while the allocation row is live.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    expired_at = _NOW + KEY_CEREMONY_TTL + timedelta(seconds=1)

    assert store.expire_key_ceremonies(now=expired_at) == 1
    _hammer_claims(store, expired_at, (30, 40, 50))
    report = _reconcile(store, driver, now=expired_at + timedelta(minutes=51))

    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.DELETION_UNCONFIRMED,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )


def test_a_second_delete_request_does_not_restart_a_failed_deletes_clock(
    tmp_path: Path,
) -> None:
    """A repeatedly-requested delete must not keep resetting its own deadline.

    ``request_delete`` returns early only for ``deleting`` and ``deleted``. A
    delete parked at ``failed`` therefore accepts a fresh request, and if that
    rewrites the clock the row ages from the newest request rather than from
    the first — so a delete that fails, is re-requested, and fails again never
    reaches the window at all.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    job = store.submit(_LIVE_ACTIVATION, "adepthood-user-001", "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    api.fail_once("DELETE", "/machines/machine-1")
    worker.run_once(now=_NOW)
    settled = store.get(job.job_id, "adepthood")

    store.request_delete(job.job_id, "adepthood", now=_NOW + timedelta(hours=10))
    report = _reconcile(store, driver, now=_NOW + timedelta(hours=10, minutes=1))

    assert settled is not None
    assert settled.state is JobState.FAILED
    assert report.divergences == (
        FleetDivergence(
            kind=DivergenceKind.DELETION_UNCONFIRMED,
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
        ),
    )


def test_unmetered_machines_are_ordered_and_stable_across_passes(
    tmp_path: Path,
) -> None:
    """More than one unreadable meter must sort deterministically and repeat.

    A single-element tuple cannot distinguish a sorted result from an
    accidental one, and drawing both passes from one clock cannot show that the
    set survives a clock that moved.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    api = FakeFlyAPI()
    driver = build_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    surrogates = []
    for index, activation in enumerate((_LIVE_ACTIVATION, "activation-C")):
        store.submit(activation, f"adepthood-user-{index:03d}", "adepthood", now=_NOW)
        assert worker.run_once(now=_NOW) is True
        app_name = f"creek-vault-{_surrogate(activation)}"
        api.machines[app_name][0]["state"] = "started"
        surrogates.append(f"fly-{_surrogate(activation)}")

    first = _reconcile(store, driver, now=_NOW)
    second = _reconcile(store, driver, now=_NOW + timedelta(days=2))

    assert len(surrogates) == 2
    assert first.unmetered_running == tuple(sorted(surrogates))
    assert second.unmetered_running == first.unmetered_running
    assert first == second
    assert first.divergences == ()
