"""A rolled-away pool credential is never stranded (#1918, #1917).

``roll_pool_bundle`` replaces an activation's vendor key.  The previous key
stays spendable at the vendor until it is revoked, so the roll records that
revocation as a durable pending obligation in the same atomic write that
stores the new bundle.  A vendor outage, or a crash, between that write and
the revocation therefore leaves the old key *owned*: ``revoke`` and
``retry_pending_revocations`` drain it, a later roll retries it before it
mints again, and the fleet report shows it and alerts once it outlives the
policy's bound.

Every key here is a synthetic canary over a local fake vendor: nothing dials a
vendor and nothing bills (ADR-0016 Decision 4).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning import production_secrets
from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.driver import FakeOneTimeHandoff, FakeProviderDriver
from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    Disposition,
    Divergence,
    DivergenceKind,
    ResourceClass,
    ResourceState,
)
from creek_mcp.provisioning.pool_bundle import (
    PendingPoolRevocation,
    PendingRevocationSource,
    PoolRuntimeBundle,
)
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    ReadOnlyFlySecretManager,
    _BundleDocument,
    _load_document,
    _routing_cipher,
)
from creek_mcp.provisioning.reconcile import (
    FleetReconciler,
    FleetReport,
    ReconcileUnavailableError,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.test_pool_runtime_bundle import FakePoolBundleIssuer, FakeVendorAdminAPI
from tests.test_provisioning_production_adapters import (
    _NOW,
    _ca_files,
    _owner_file,
)
from tests.test_provisioning_reconcile import _policy

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from creek_mcp.provisioning.pool_bundle import PoolCredential

_ACTIVATION = "activation-pending-revocation-001"
_CONSUMER = "adepthood-user-001"
_REQUESTER = "adepthood"
_BOUND = timedelta(seconds=1800)
"""``stuck_deletion_seconds`` in the reconcile tests' reference policy."""
_RECONCILE_SOURCE = (
    Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning" / "reconcile.py"
)


class _Crash(BaseException):
    """Stands for the process dying: not an ``Exception``, so nothing catches it."""


@dataclass
class _CrashingIssuer(FakePoolBundleIssuer):
    """Mints normally; the process dies the moment it tries to revoke."""

    def revoke(self, bundle: PoolRuntimeBundle) -> None:
        """Die between the durable write and the vendor delete."""
        del bundle
        raise _Crash


@dataclass(frozen=True, slots=True)
class _StateFiles:
    """One encrypted state directory and its key material, reopened at will."""

    state: Path
    master: Path
    certificate: Path
    key: Path

    def manager(
        self,
        issuer: FakePoolBundleIssuer | None,
        *,
        now: datetime = _NOW,
    ) -> EncryptedFileFlySecretManager:
        """Return a manager over these files: a fresh one is a restart."""
        return EncryptedFileFlySecretManager(
            self.state,
            master_key_file=self.master,
            ca_certificate_file=self.certificate,
            ca_private_key_file=self.key,
            app_prefix="creek-vault",
            clock=lambda: now,
            pool_bundles=issuer,
        )

    def read_only(self) -> ReadOnlyFlySecretManager:
        """Return the routing process's read-only view of the same state."""
        return ReadOnlyFlySecretManager(self.state, master_key_file=self.master)


@pytest.fixture
def files(tmp_path: Path) -> _StateFiles:
    """Create one owner-only state directory, master key and CA."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    certificate, key = _ca_files(tmp_path)
    return _StateFiles(
        state,
        _owner_file(tmp_path / "master-key", b"m" * 32),
        certificate,
        key,
    )


def _issuer(vendor: FakeVendorAdminAPI | None = None) -> FakePoolBundleIssuer:
    """Return an issuer over *vendor*, or over a fresh fake vendor."""
    return FakePoolBundleIssuer(vendor or FakeVendorAdminAPI())


def _issue(manager: EncryptedFileFlySecretManager) -> PoolRuntimeBundle:
    """Issue the activation and return its stored pool bundle."""
    pool = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER).pool
    assert pool is not None
    return pool


def _failed_roll(
    manager: EncryptedFileFlySecretManager,
    issuer: FakePoolBundleIssuer,
) -> tuple[PoolRuntimeBundle, PoolRuntimeBundle]:
    """Roll while the vendor's delete is down; return the old and new bundles."""
    old = _issue(manager)
    issuer.fleet_version = 2
    issuer.vendor.delete_fails = True
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        manager.roll_pool_bundle(_ACTIVATION)
    return old, _issue(manager)


def _accepts(issuer: FakePoolBundleIssuer, credential: PoolCredential) -> bool:
    """Return whether the fake vendor would still admit *credential*."""
    return issuer.vendor.accepts(credential)


# --------------------------------------------------------------------------
# #1918: a failed revoke inside a roll leaves the old key owned and retryable.
# --------------------------------------------------------------------------


def test_a_failed_revoke_during_a_roll_keeps_the_old_credential_owned(
    files: _StateFiles,
) -> None:
    """The vendor 5xx is loud, the new bundle is stored, the old key is owed."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, new = _failed_roll(manager, issuer)
    assert new.bundle_version == 2
    assert new.credential != old.credential
    assert _accepts(issuer, old.credential)
    assert manager.pending_pool_revocations() == (
        PendingPoolRevocation(activation_id=_ACTIVATION, superseded_at=_NOW),
    )


def test_a_retry_revokes_the_pending_credential_and_clears_it(
    files: _StateFiles,
) -> None:
    """Once the vendor recovers, one retry kills the old key and only that."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, new = _failed_roll(manager, issuer)
    issuer.vendor.delete_fails = False
    manager.retry_pending_revocations(_ACTIVATION)
    assert not _accepts(issuer, old.credential)
    assert _accepts(issuer, new.credential)
    assert manager.pending_pool_revocations() == ()
    assert _issue(manager) == new
    manager.retry_pending_revocations(_ACTIVATION)
    assert issuer.vendor.deleted == [old.credential.vendor_key_value()]


def test_a_retry_that_fails_again_keeps_the_obligation(files: _StateFiles) -> None:
    """A second outage is just as loud, and leaves the key just as owned."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, _ = _failed_roll(manager, issuer)
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        manager.retry_pending_revocations(_ACTIVATION)
    assert _accepts(issuer, old.credential)
    assert len(manager.pending_pool_revocations()) == 1


def test_a_crash_between_the_write_and_the_revoke_is_recoverable(
    files: _StateFiles,
) -> None:
    """The obligation is written with the new bundle, so a restart finds it."""
    vendor = FakeVendorAdminAPI()
    issuer = _issuer(vendor)
    old = _issue(files.manager(issuer))
    issuer.fleet_version = 2

    dying = _CrashingIssuer(vendor, fleet_version=2)
    with pytest.raises(_Crash):
        files.manager(dying).roll_pool_bundle(_ACTIVATION)
    restarted = files.manager(_issuer(vendor))
    assert len(restarted.pending_pool_revocations()) == 1
    assert vendor.accepts(old.credential)
    restarted.retry_pending_revocations(_ACTIVATION)
    assert not vendor.accepts(old.credential)
    assert restarted.pending_pool_revocations() == ()


def test_a_roll_retries_an_outstanding_revocation_before_minting_again(
    files: _StateFiles,
) -> None:
    """At most one superseded key is ever live: no roll mints over an IOU."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, new = _failed_roll(manager, issuer)
    minted = issuer.vendor.minted
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        manager.roll_pool_bundle(_ACTIVATION)
    assert issuer.vendor.minted == minted
    assert _issue(manager) == new
    issuer.vendor.delete_fails = False
    issuer.fleet_version = 3
    newest = manager.roll_pool_bundle(_ACTIVATION)
    assert newest.bundle_version == 3
    assert not _accepts(issuer, old.credential)
    assert not _accepts(issuer, new.credential)
    assert _accepts(issuer, newest.credential)
    assert manager.pending_pool_revocations() == ()


def test_deleting_the_allocation_drains_every_pending_revocation(
    files: _StateFiles,
) -> None:
    """Deletion revokes the superseded key as well as the current one."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, new = _failed_roll(manager, issuer)
    issuer.vendor.delete_fails = False
    manager.revoke(_ACTIVATION)
    assert not _accepts(issuer, old.credential)
    assert not _accepts(issuer, new.credential)
    assert manager.pending_pool_revocations() == ()
    with pytest.raises(ValueError, match="revoked"):
        _issue(manager)


def test_deletion_during_an_outage_keeps_every_credential_owned(
    files: _StateFiles,
) -> None:
    """No tombstone while any key is unconfirmed, so a retry still reaches both."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old, new = _failed_roll(manager, issuer)
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        manager.revoke(_ACTIVATION)
    assert _issue(manager) == new
    assert len(manager.pending_pool_revocations()) == 1
    issuer.vendor.delete_fails = False
    manager.revoke(_ACTIVATION)
    assert not _accepts(issuer, old.credential)
    assert not _accepts(issuer, new.credential)


def test_retrying_needs_the_revocation_authority(files: _StateFiles) -> None:
    """A manager with no issuer cannot pretend to have cleared an obligation."""
    issuer = _issuer()
    _failed_roll(files.manager(issuer), issuer)
    with pytest.raises(ValueError, match="no pool bundle issuer"):
        files.manager(None).retry_pending_revocations(_ACTIVATION)


def test_the_read_only_view_lists_what_is_owed_until_deletion_drains_it(
    files: _StateFiles,
) -> None:
    """The routing/fleet process can see an obligation it cannot discharge."""
    issuer = _issuer()
    manager = files.manager(issuer)
    _failed_roll(manager, issuer)
    other = "activation-pending-revocation-002"
    manager.issue(other, _CONSUMER, requester_identity=_REQUESTER)
    assert files.read_only().pending_pool_revocations() == (
        PendingPoolRevocation(activation_id=_ACTIVATION, superseded_at=_NOW),
    )
    issuer.vendor.delete_fails = False
    manager.revoke(_ACTIVATION)
    assert files.read_only().pending_pool_revocations() == ()


def test_an_unreadable_record_fails_the_listing_closed(files: _StateFiles) -> None:
    """A record it cannot read is not proof that nothing is owed."""
    issuer = _issuer()
    _issue(files.manager(issuer))
    (bundle,) = files.state.glob("*.bundle")
    bundle.write_bytes(b"x" * 64)
    with pytest.raises(ValueError, match="invalid"):
        files.read_only().pending_pool_revocations()
    (files.state / "not-a-digest.bundle").write_bytes(b"x")
    with pytest.raises(ValueError, match="invalid"):
        files.read_only().pending_pool_revocations()


def test_a_legacy_record_without_the_obligation_list_decodes_as_owing_nothing(
    files: _StateFiles,
) -> None:
    """Records written before #1918 carry no list; they owe nothing."""
    manager = files.manager(_issuer())
    _issue(manager)
    document = manager_document(files)
    encoded = json.loads(document.encode())
    assert encoded["pending_revocations"] == []
    del encoded["pending_revocations"]
    legacy = _BundleDocument.decode(json.dumps(encoded).encode())
    assert legacy.pending_revocations == ()
    encoded["pending_revocations"] = [{"superseded_at": "not-a-time"}]
    with pytest.raises(ValueError, match="invalid"):
        _BundleDocument.decode(json.dumps(encoded).encode())
    encoded["pending_revocations"] = {"superseded_at": _NOW.isoformat()}
    with pytest.raises(ValueError, match="invalid"):
        _BundleDocument.decode(json.dumps(encoded).encode())


@pytest.mark.parametrize(
    "entry",
    [
        "not-an-object",
        {"pool": None, "superseded_at": "2026-09-13T12:00:00+00:00"},
        {"pool": "<stored pool>", "superseded_at": "2026-09-13T12:00:00"},
    ],
    ids=["not-an-object", "no-pool", "naive-time"],
)
def test_a_malformed_obligation_fails_the_record_closed(
    files: _StateFiles,
    entry: object,
) -> None:
    """An obligation that cannot be revoked or aged is no obligation at all."""
    manager = files.manager(_issuer())
    _issue(manager)
    encoded = json.loads(manager_document(files).encode())
    if isinstance(entry, dict) and entry["pool"] == "<stored pool>":
        entry = {**entry, "pool": encoded["pool"]}
    encoded["pending_revocations"] = [entry]
    with pytest.raises(ValueError, match="invalid"):
        _BundleDocument.decode(json.dumps(encoded).encode())


def test_a_record_deleted_mid_listing_is_skipped_not_fatal(
    files: _StateFiles,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deletion racing the listing removed a record that then owed nothing."""
    issuer = _issuer()
    _failed_roll(files.manager(issuer), issuer)
    real = production_secrets._load_document

    def vanish(cipher: AESGCM, path: Path, digest: str) -> _BundleDocument:
        path.unlink()
        return real(cipher, path, digest)

    monkeypatch.setattr(production_secrets, "_load_document", vanish)
    assert files.read_only().pending_pool_revocations() == ()


def manager_document(files: _StateFiles) -> _BundleDocument:
    """Decrypt the single stored record through the manager's own loader."""
    (bundle,) = files.state.glob("*.bundle")
    return _load_document(_routing_cipher(files.master), bundle, bundle.stem)


# --------------------------------------------------------------------------
# #1917: a roll always replaces the credential, and says so.
# --------------------------------------------------------------------------


@dataclass
class _RepinningIssuer(FakePoolBundleIssuer):
    """Breaks the contract: re-pins the manifest but hands back the old key."""

    kept: PoolRuntimeBundle | None = None

    def issue(self, allocation_id: str) -> PoolRuntimeBundle:
        """Mint once, then return that same credential on every later call."""
        if self.kept is None:
            self.kept = super().issue(allocation_id)
            return self.kept
        return replace(
            self.kept,
            bundle_version=self.fleet_version,
            manifest_json=self.kept.manifest_json.replace("pool-model-a", "z"),
        )


def test_every_roll_revokes_exactly_the_credential_it_replaced(
    files: _StateFiles,
) -> None:
    """``issue`` always mints, so the replaced key is always revoked."""
    issuer = _issuer()
    manager = files.manager(issuer)
    old = _issue(manager)
    issuer.fleet_version = 2
    rolled = manager.roll_pool_bundle(_ACTIVATION)
    assert issuer.vendor.deleted == [old.credential.vendor_key_value()]
    assert _accepts(issuer, rolled.credential)
    assert manager.pending_pool_revocations() == ()


def test_an_issuer_that_keeps_the_credential_is_refused_before_any_write(
    files: _StateFiles,
) -> None:
    """Revoking a kept key would revoke the live one; storing it would lie."""
    issuer = _RepinningIssuer(FakeVendorAdminAPI())
    manager = files.manager(issuer)
    old = _issue(manager)
    issuer.fleet_version = 2
    with pytest.raises(ValueError, match="returned the credential it replaces"):
        manager.roll_pool_bundle(_ACTIVATION)
    assert issuer.vendor.deleted == []
    assert _accepts(issuer, old.credential)
    assert _issue(manager) == old
    assert manager.pending_pool_revocations() == ()


# --------------------------------------------------------------------------
# Visibility: the fleet report shows every owed key and alerts past the bound.
# --------------------------------------------------------------------------


@dataclass
class _Pending:
    """A listing-only pending-revocation source, as the fleet process holds."""

    records: tuple[PendingPoolRevocation, ...]

    def pending_pool_revocations(self) -> tuple[PendingPoolRevocation, ...]:
        """Return the recorded obligations."""
        return self.records


def _ready(store: ProvisioningStore, driver: FakeProviderDriver) -> str:
    """Provision one live allocation and return its provider allocation id."""
    store.submit(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER, now=_NOW)
    assert ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    return driver.expected_allocation_id(_ACTIVATION)


def _report(
    store: ProvisioningStore,
    driver: FakeProviderDriver,
    source: PendingRevocationSource | None,
    *,
    now: datetime = _NOW,
) -> FleetReport:
    """Run one report-only pass with *source* wired in."""
    reconciler = FleetReconciler(
        store, driver, driver, _policy(), pending_revocations=source
    )
    return reconciler.run_once(now=now, repair=False)


def _of_kind(report: FleetReport, kind: DivergenceKind) -> list[Divergence]:
    """Return the report's divergences of one kind."""
    return [d for d in report.divergences if d.kind is kind]


def test_a_pending_revocation_is_reported_and_alerts_only_past_the_bound(
    tmp_path: Path,
) -> None:
    """Visible from the first pass; an alert once it outlives the policy bound."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    allocation = _ready(store, driver)
    source = _Pending((PendingPoolRevocation(_ACTIVATION, _NOW),))
    young = _report(store, driver, source, now=_NOW + _BOUND - timedelta(seconds=1))
    (divergence,) = young.divergences
    assert divergence.kind is DivergenceKind.PENDING_REVOCATION
    assert divergence.disposition is Disposition.REPORTED
    assert divergence.provider_allocation_id == allocation
    assert divergence.resource_class is ResourceClass.CREDENTIAL
    assert divergence.age_seconds == _BOUND.total_seconds() - 1
    assert young.alerts == ()
    aged = _report(store, driver, source, now=_NOW + _BOUND)
    (alert,) = aged.alerts
    assert alert.kind is AlertKind.POOL_CREDENTIAL_ORPHAN
    assert alert.subject == allocation
    assert alert.age_seconds == int(_BOUND.total_seconds())
    assert alert.threshold == str(int(_BOUND.total_seconds()))


def test_without_a_source_the_report_is_unchanged(tmp_path: Path) -> None:
    """Wiring is opt-in: a pass with no source reports what it always did."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    _ready(store, driver)
    report = _report(store, driver, None, now=_NOW + _BOUND)
    assert report.divergences == ()
    assert report.alerts == ()


def test_an_unreadable_pending_source_aborts_before_any_repair(
    tmp_path: Path,
) -> None:
    """Fail closed and early: no partial report claims nothing is owed."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    _ready(store, driver)

    class _Broken:
        def pending_pool_revocations(self) -> tuple[PendingPoolRevocation, ...]:
            raise ValueError("runtime secret state is invalid")

    with pytest.raises(ReconcileUnavailableError, match="pending revocation"):
        _report(store, driver, _Broken())


def _credential(allocation: str, ref: str) -> ProviderResource:
    """Return one vendor-listed credential under *allocation*."""
    return ProviderResource(
        allocation, ResourceClass.CREDENTIAL, ResourceState.OTHER, None, None, ref
    )


@pytest.mark.parametrize(
    ("listed", "owed", "duplicate"),
    [(0, 0, False), (1, 0, False), (2, 0, True), (2, 1, False), (3, 1, True)],
)
def test_inventory_flags_more_live_credentials_than_the_store_accounts_for(
    tmp_path: Path,
    listed: int,
    owed: int,
    duplicate: bool,
) -> None:
    """Two live keys under one allocation are visible unless one is owed."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    allocation = _ready(store, driver)
    for index in range(listed):
        driver.seed_resource(_credential(allocation, f"vendor-ref-{index}"))
    source = _Pending(
        tuple(PendingPoolRevocation(_ACTIVATION, _NOW) for _ in range(owed))
    )
    report = _report(store, driver, source)
    duplicates = [
        d
        for d in _of_kind(report, DivergenceKind.DUPLICATE_RESOURCE)
        if d.resource_class is ResourceClass.CREDENTIAL
    ]
    assert bool(duplicates) is duplicate
    assert not [
        d
        for d in _of_kind(report, DivergenceKind.MISSING_RESOURCE)
        if d.resource_class is ResourceClass.CREDENTIAL
    ]
    kinds = {alert.kind for alert in report.alerts}
    assert (AlertKind.DUPLICATE_RESOURCE in kinds) is duplicate


def test_the_reconciler_still_holds_no_revocation_authority() -> None:
    """Retrying is the issuer-holder's job; the fleet pass only observes."""
    source = _RECONCILE_SOURCE.read_text(encoding="utf-8")
    assert re.search(r"\.revoke\(", source) is None
    assert "retry_pending_revocations" not in source
    assert not hasattr(PendingRevocationSource, "revoke")
    assert not hasattr(PendingRevocationSource, "retry_pending_revocations")


# --------------------------------------------------------------------------
# Secrets discipline: no key value in a log, an error, a report or on disk.
# --------------------------------------------------------------------------


def test_no_credential_value_reaches_a_log_an_error_a_report_or_the_disk(
    files: _StateFiles,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only counts, ages and opaque ids leave the encrypted record."""
    caplog.set_level(logging.DEBUG)
    issuer = _issuer()
    manager = files.manager(issuer)
    old = _issue(manager)
    issuer.fleet_version = 2
    issuer.vendor.delete_fails = True
    with pytest.raises(RuntimeError) as failure:
        manager.roll_pool_bundle(_ACTIVATION)
    new = _issue(manager)
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    _ready(store, driver)
    report = _report(store, driver, files.read_only(), now=_NOW + _BOUND)
    assert report.alerts
    rendered = "\n".join(
        (
            caplog.text,
            str(failure.value),
            repr(failure.value),
            json.dumps(report.to_dict()),
            repr(manager.pending_pool_revocations()),
            repr(report),
        )
    )
    on_disk = b"".join(path.read_bytes() for path in files.state.iterdir())
    for bundle in (old, new):
        secret = bundle.credential.vendor_key_value()
        assert secret not in rendered
        assert secret.encode() not in on_disk
