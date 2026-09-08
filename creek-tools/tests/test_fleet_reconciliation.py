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

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from creek_mcp.provisioning.inventory import ProviderResourceClass
from creek_mcp.provisioning.reconcile import (
    DivergenceKind,
    FleetReconcilePolicy,
    FleetReconciler,
    ReconcileMode,
)

from creek_mcp.provisioning.driver import FakeOneTimeHandoff
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_api_support import FakeFlyAPI, build_driver
from tests.provisioning_secret_support import assert_content_free

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 8, 4, tzinfo=UTC)
_ORG = "creek-vaults"
_LIVE_ACTIVATION = "activation-A"
_ORPHAN_ACTIVATION = "activation-B"
_POLICY = FleetReconcilePolicy(
    unconfirmed_deletion_after=timedelta(minutes=15),
    max_continuous_running=timedelta(hours=6),
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
    assert {method for method, _, _ in api.requests[baseline:]} == {"GET"}
