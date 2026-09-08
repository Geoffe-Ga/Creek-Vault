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
from typing import TYPE_CHECKING, cast

import pytest

from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderDriver,
)
from creek_mcp.provisioning.inventory import (
    ProviderInventory,
    ProviderResource,
    ProviderResourceClass,
)
from creek_mcp.provisioning.models import JobOperation, JobState
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
from tests.provisioning_secret_support import (
    FORBIDDEN_FIELD_NAMES,
    assert_content_free,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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

    def list_resources(self) -> Sequence[ProviderResource]:
        """Return one resource carrying a class no Creek allocation can hold."""
        return [
            ProviderResource(
                resource_class=cast("ProviderResourceClass", "gpu"),
                provider_id="gpu-1",
                provider_allocation_id=self._surrogate,
                state="stopped",
            )
        ]


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

    resources = driver.list_resources()
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
        for resource in driver.list_resources()
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

    report = _reconcile(store, driver)

    assert report.inventory_complete is False
    assert report.divergences == ()


def test_a_vanished_app_is_reported_missing_once_the_inventory_is_complete(
    tmp_path: Path,
) -> None:
    """A live allocation the provider no longer shows is a divergence."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    api.apps.clear()

    report = _reconcile(store, driver)

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

    report = _reconcile(store, driver)

    assert {divergence.kind for divergence in report.divergences} == {
        DivergenceKind.DUPLICATE_ALLOCATION
    }
    assert {divergence.provider_id for divergence in report.divergences} == {
        "vol-1",
        "vol-2",
    }


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
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_a_machine_the_provider_gives_no_timestamp_for_is_not_guessed_at(
    tmp_path: Path,
) -> None:
    """Fly's proxy can start a Machine unobserved; a duration is never invented."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    api.machines[f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"][0]["state"] = "started"

    report = _reconcile(store, driver)

    assert report.divergences == ()


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


def test_the_reconciler_has_no_repair_path_anywhere_in_the_module() -> None:
    """Report-only is a ruling, so the mutating calls must not exist at all.

    Decision 6 binds resource removal to revoking the consumer credential, and
    ``FlySecretManager.revoke`` is keyed on the activation id — a SHA-256
    preimage of the only name an orphan has. An automatic repair would destroy
    billable resources while leaving a live credential issued, which is
    strictly worse than the orphan it was cleaning up.

    The check walks the module's syntax tree rather than its text, so prose
    that *names* the forbidden operations cannot satisfy or break it.
    """
    tree = ast.parse(
        (
            Path(__file__).resolve().parents[1]
            / "creek_mcp"
            / "provisioning"
            / "reconcile.py"
        ).read_text(encoding="utf-8")
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert list(ReconcileMode) == [ReconcileMode.REPORT_ONLY]
    assert called.isdisjoint({"provision", "delete", "start", "stop"})
    assert "delete_orphan" not in called | referenced


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
