"""Per-activation pool runtime bundle: identity, URL and credential (#1900).

ADR-0016 Decision 1 items 1 to 3 and Decision 6 put the pool's manifest,
``CREEK_POOL_URL`` and credential in the *activation's* stored runtime bundle
rather than in the fleet policy, so a policy flip cannot make
``_verify_machine`` reject every not-yet-rolled Machine.  These tests own
T11 (lifecycle), T12 (exact config) and T16 (cost line), plus the mutation
check the issue requires and a credential-leak sweep.

Every key, token and URL here is a synthetic canary.  Nothing dials a vendor
and nothing bills: the vendor admin API is the local fake below (Decision 4,
"no spend").
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.tokens import (
    Surface,
    SurfaceTokens,
    TokensPerReflection,
    unmeasured_tokens,
)
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.provisioning.budget import (
    AlertKind,
    FleetPolicy,
    InjectedUsage,
    PolicyFile,
    estimate_monthly_cost,
    evaluate_alerts,
    load_policy_file,
)
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import (
    FlyProviderDriver,
    FlyRuntimeSecrets,
)
from creek_mcp.provisioning.models import (
    Disposition,
    Divergence,
    DivergenceKind,
    FailureReason,
    FleetTelemetry,
    ResourceClass,
    ResourceState,
    RoutableAllocation,
)
from creek_mcp.provisioning.pool_bundle import (
    POOL_BUNDLE_VERSION_ENV,
    POOL_CREDENTIAL_KIND_ENV,
    POOL_MANIFEST_GUEST_PATH,
    POOL_VENDOR_KEY_GUEST_PATH,
    PoolCredential,
    PoolCredentialKind,
    PoolCredentialRecord,
    PoolRuntimeBundle,
    PoolSpendCap,
    pool_machine_files,
    pool_runtime_env,
    pool_secret_files,
    size_pool_caps,
)
from creek_mcp.provisioning.production_secrets import EncryptedFileFlySecretManager
from tests.fly_support import (
    NOW,
    POOL_MANIFEST_JSON,
    POOL_URL,
    POOL_VENDOR_KEY,
    FakeFlyAPI,
    FakeSecretManager,
    ProviderNormalizedFlyAPI,
    fly_driver,
    fly_job,
    pool_bundle,
)
from tests.test_provisioning_production_adapters import (
    _NOW,
    _ca_files,
    _owner_file,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek_mcp.provisioning.models import ProvisioningJob

_TODAY = date(2026, 10, 21)


# --------------------------------------------------------------------------
# A fake vendor admin API.  Decision 3.4's surface only: create, update, delete.
# --------------------------------------------------------------------------


@dataclass
class FakeVendorAdminAPI:
    """The vendor key API of Decision 3.4, with no network and no billing."""

    keys: dict[str, PoolSpendCap] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    delete_fails: bool = False
    minted: int = 0

    def create(self, allocation_id: str, caps: PoolSpendCap) -> PoolCredential:
        """Mint one per-activation vendor key bound to *allocation_id*."""
        self.minted += 1
        value = f"{POOL_VENDOR_KEY}-{allocation_id}-{self.minted}"
        self.keys[value] = caps
        self.created.append(value)
        return PoolCredential.vendor_key(value)

    def delete(self, credential: PoolCredential) -> None:
        """Delete a key by its full value, as the vendor's API requires."""
        if self.delete_fails:
            raise RuntimeError("vendor admin API is unavailable")
        value = credential.vendor_key_value()
        self.keys.pop(value, None)
        self.deleted.append(value)

    def accepts(self, credential: PoolCredential) -> bool:
        """Return whether the pool would still admit *credential*."""
        return credential.vendor_key_value() in self.keys


@dataclass
class FakePoolBundleIssuer:
    """The fleet-current pool bundle, plus per-activation minting.

    ``fleet_version`` and ``fleet_manifest`` stand for the fleet policy a
    re-pin flips.  Nothing here is read by ``FlyProviderDriver``: a stored
    activation keeps the bundle it was issued (Decision 1 item 1).
    """

    vendor: FakeVendorAdminAPI
    fleet_version: int = 1
    fleet_url: str = POOL_URL
    fleet_model: str = "pool-model-a"

    def issue(self, allocation_id: str) -> PoolRuntimeBundle:
        """Mint this activation's bundle at the fleet-current identity."""
        return PoolRuntimeBundle(
            bundle_version=self.fleet_version,
            pool_url=self.fleet_url,
            manifest_json=POOL_MANIFEST_JSON.replace("pool-model-a", self.fleet_model),
            credential=self.vendor.create(allocation_id, _caps()),
        )

    def revoke(self, bundle: PoolRuntimeBundle) -> None:
        """Revoke at the vendor, confirming before any receipt is issued."""
        self.vendor.delete(bundle.credential)
        if self.vendor.accepts(bundle.credential):
            raise RuntimeError("vendor still admits the revoked credential")


def _caps() -> PoolSpendCap:
    """Return a synthetic cap triple; sizing itself is tested separately."""
    return PoolSpendCap(
        lifetime_usd=Decimal("1.00"),
        input_tokens=100_000,
        output_tokens=10_000,
        expires_at=NOW + timedelta(days=30),
    )


# --------------------------------------------------------------------------
# T12 exact config, and the mutation check.
# --------------------------------------------------------------------------


def _provisioned(
    *,
    fly_replay_enabled: bool,
    issuer: FakePoolBundleIssuer | None = None,
) -> tuple[FakeFlyAPI, FlyProviderDriver, FakeSecretManager]:
    """Provision one allocation whose bundle carries a pool."""
    api = ProviderNormalizedFlyAPI()
    secrets = FakeSecretManager(set(), issuer=issuer or _issuer())
    driver = fly_driver(api, secrets, fly_replay_enabled=fly_replay_enabled)
    driver.provision(fly_job())
    return api, driver, secrets


def _issuer() -> FakePoolBundleIssuer:
    """Return an issuer over a fresh fake vendor admin API."""
    return FakePoolBundleIssuer(FakeVendorAdminAPI())


def _machine(api: FakeFlyAPI) -> dict[str, Any]:
    """Return the single provisioned Machine."""
    machines = next(iter(api.machines.values()))
    assert len(machines) == 1
    return machines[0]


def _route(driver: FlyProviderDriver, job: ProvisioningJob, *, replay: bool) -> None:
    """Route *job*'s allocation the way the edge does on its runtime."""
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        driver.expected_allocation_id(job.activation_id),
    )
    if replay:
        driver.prepare_replay(routed)
    else:
        driver.prepare_route(routed)


@pytest.mark.parametrize("fly_replay_enabled", [False, True])
def test_pool_bundle_reaches_the_machine_on_both_runtimes(
    *,
    fly_replay_enabled: bool,
) -> None:
    """T12: the URL, manifest and credential are installed on either runtime."""
    api, _, secrets = _provisioned(fly_replay_enabled=fly_replay_enabled)
    config = _machine(api)["config"]
    bundle = secrets.issued["activation-fly-001"].pool
    assert bundle is not None
    assert config["env"]["CREEK_POOL_URL"] == POOL_URL
    assert config["env"]["CREEK_MODEL_PACKAGE_FILE"] == POOL_MANIFEST_GUEST_PATH
    assert config["env"][POOL_BUNDLE_VERSION_ENV] == "1"
    assert config["env"][POOL_CREDENTIAL_KIND_ENV] == PoolCredentialKind.VENDOR_KEY
    paths = {entry["guest_path"] for entry in config["files"]}
    assert POOL_MANIFEST_GUEST_PATH in paths
    assert POOL_VENDOR_KEY_GUEST_PATH in paths


_STALE = FailureReason.PROVIDER_UNAVAILABLE
_FOREIGN = FailureReason.PROVIDER_REJECTED


@pytest.mark.parametrize("fly_replay_enabled", [False, True])
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(
            lambda c: c["env"].__setitem__("CREEK_POOL_URL", "https://x.test"),
            _STALE,
            id="pool-url",
        ),
        pytest.param(
            lambda c: c["env"].pop("CREEK_POOL_URL"), _STALE, id="pool-url-absent"
        ),
        pytest.param(
            lambda c: c["env"].pop("CREEK_MODEL_PACKAGE_FILE"),
            _STALE,
            id="manifest-env-absent",
        ),
        pytest.param(
            lambda c: c["env"].__setitem__(POOL_BUNDLE_VERSION_ENV, "2"),
            _STALE,
            id="bundle-version",
        ),
        pytest.param(lambda c: c["files"].pop(), _STALE, id="secret-file-missing"),
        pytest.param(
            lambda c: c["files"][-1].__setitem__("raw_value", "dGFtcGVyZWQ="),
            _FOREIGN,
            id="secret-file-tampered",
        ),
    ],
)
def test_verify_machine_rejects_a_machine_that_drifts_from_its_bundle(
    *,
    fly_replay_enabled: bool,
    mutate: Any,
    reason: FailureReason,
) -> None:
    """T12: env or files differing from the activation's bundle are refused.

    A route never adopts or rewrites the drift.  Drift the bundle itself can
    repair is refused retryably for the operator's converge; a secret value
    the bundle does not hold is a permanent rejection (#1869).
    """
    api, driver, _ = _provisioned(fly_replay_enabled=fly_replay_enabled)
    mutate(_machine(api)["config"])
    with pytest.raises(ProviderError) as caught:
        _route(driver, fly_job(), replay=fly_replay_enabled)
    assert caught.value.reason is reason
    assert api.updates == []


@pytest.mark.parametrize("fly_replay_enabled", [False, True])
def test_a_provisioning_retry_rewrites_pool_drift_to_its_bundle(
    *,
    fly_replay_enabled: bool,
) -> None:
    """T12 on the worker: a stopped Machine is put back to exactly its bundle."""
    api, driver, secrets = _provisioned(fly_replay_enabled=fly_replay_enabled)
    _machine(api)["config"]["env"]["CREEK_POOL_URL"] = "https://x.test"

    driver.provision(fly_job())

    bundle = secrets.issued["activation-fly-001"].pool
    assert bundle is not None
    assert _machine(api)["config"]["env"]["CREEK_POOL_URL"] == bundle.pool_url
    assert len(api.updates) == 1


def test_policy_flip_without_a_roll_rejects_nothing() -> None:
    """The mutation check: a re-pin must not reject a not-yet-rolled Machine.

    This is the defect the issue exists to prevent.  Two activations sit on
    different bundle versions; the fleet moves to a third identity; every
    stored activation still verifies against its own stored bundle.
    """
    issuer = _issuer()
    api = ProviderNormalizedFlyAPI()
    secrets = FakeSecretManager(set(), issuer=issuer)
    driver = fly_driver(api, secrets)
    first = fly_job("activation-pool-first")
    driver.provision(first)
    issuer.fleet_version = 2
    issuer.fleet_model = "pool-model-b"
    second = fly_job("activation-pool-second")
    driver.provision(second)
    issuer.fleet_version = 3
    issuer.fleet_model = "pool-model-c"
    # Re-converging both allocations re-runs _verify_machine on each.
    driver.provision(first)
    driver.provision(second)
    versions = {
        job.activation_id: secrets.issued[job.activation_id].pool
        for job in (first, second)
    }
    assert versions["activation-pool-first"] is not None
    assert versions["activation-pool-second"] is not None
    assert versions["activation-pool-first"].bundle_version == 1
    assert versions["activation-pool-second"].bundle_version == 2


def test_tampering_one_machine_rejects_exactly_that_machine() -> None:
    """The mutation check's other direction: drift is local to one Machine."""
    issuer = _issuer()
    api = ProviderNormalizedFlyAPI()
    secrets = FakeSecretManager(set(), issuer=issuer)
    driver = fly_driver(api, secrets)
    good = fly_job("activation-pool-good")
    bad = fly_job("activation-pool-bad")
    driver.provision(good)
    driver.provision(bad)
    bad_app = driver.expected_allocation_id(bad.activation_id).replace(
        "fly-", "creek-vault-"
    )
    api.machines[bad_app][0]["config"]["env"]["CREEK_POOL_URL"] = "https://other.test"
    with pytest.raises(ProviderError):
        _route(driver, bad, replay=False)
    _route(driver, good, replay=False)


def test_a_bundle_without_a_pool_installs_no_pool_fields() -> None:
    """The O3-fallback and pool-free paths must stay byte-identical (#1905)."""
    api = ProviderNormalizedFlyAPI()
    driver = fly_driver(api, FakeSecretManager(set()))
    driver.provision(fly_job())
    config = _machine(api)["config"]
    assert "CREEK_POOL_URL" not in config["env"]
    paths = {entry["guest_path"] for entry in config["files"]}
    assert POOL_MANIFEST_GUEST_PATH not in paths
    assert POOL_VENDOR_KEY_GUEST_PATH not in paths


# --------------------------------------------------------------------------
# T12's bootstrap half.
# --------------------------------------------------------------------------


def test_bootstrap_lists_the_pool_files_only_when_a_pool_is_configured() -> None:
    """T12: the replay bootstrap normalizes listed pool files, never a scan."""
    assert pool_secret_files({}) == ()
    configured = {
        "CREEK_POOL_URL": POOL_URL,
        POOL_CREDENTIAL_KIND_ENV: PoolCredentialKind.VENDOR_KEY.value,
    }
    assert pool_secret_files(configured) == (
        Path(POOL_MANIFEST_GUEST_PATH),
        Path(POOL_VENDOR_KEY_GUEST_PATH),
    )


def test_bootstrap_refuses_an_unknown_credential_kind() -> None:
    """An unrecognized kind fails closed rather than normalizing nothing."""
    with pytest.raises(ValueError, match="pool credential kind"):
        pool_secret_files(
            {"CREEK_POOL_URL": POOL_URL, POOL_CREDENTIAL_KIND_ENV: "invented"}
        )


def test_the_o3_successor_shape_carries_three_files() -> None:
    """Decision 1 item 3's bearer-plus-mTLS triple fits the same field."""
    credential = PoolCredential.bearer_and_client_tls(
        token=b"pool-token-secret-canary",
        certificate=b"pool-client-certificate",
        private_key=b"pool-client-private-key-secret-canary",
    )
    bundle = replace(pool_bundle(), credential=credential)
    paths = [path for path, _ in pool_machine_files(bundle)]
    assert paths == [
        POOL_MANIFEST_GUEST_PATH,
        "/run/secrets/creek_pool_token",
        "/run/secrets/creek_pool.crt",
        "/run/secrets/creek_pool.key",
    ]
    assert pool_runtime_env(bundle)[POOL_CREDENTIAL_KIND_ENV] == (
        PoolCredentialKind.BEARER_AND_CLIENT_TLS
    )


# --------------------------------------------------------------------------
# T11 lifecycle.
# --------------------------------------------------------------------------


def test_activation_mints_and_deletion_revokes_before_the_receipt() -> None:
    """T11: minted at activation, refused by the pool before the receipt."""
    issuer = _issuer()
    api = ProviderNormalizedFlyAPI()
    secrets = FakeSecretManager(set(), issuer=issuer, call_log=api.requests)
    driver = fly_driver(api, secrets)
    driver.provision(fly_job())
    bundle = secrets.issued["activation-fly-001"].pool
    assert bundle is not None
    assert issuer.vendor.accepts(bundle.credential)
    outcome = driver.delete(
        fly_job(), driver.expected_allocation_id("activation-fly-001")
    )
    assert not issuer.vendor.accepts(bundle.credential)
    assert ResourceClass.CREDENTIAL in outcome.resource_classes
    assert not any(api.machines.values())
    revoked_at = [
        index for index, (method, _) in enumerate(api.requests) if method == "REVOKE"
    ]
    destroyed_at = [
        index
        for index, (method, path) in enumerate(api.requests)
        if method == "DELETE" and "/machines/" in path
    ]
    assert revoked_at and destroyed_at
    assert max(revoked_at) < min(destroyed_at)


def test_a_vendor_that_cannot_revoke_blocks_the_receipt() -> None:
    """T11: an unconfirmed revocation must not yield a deletion receipt."""
    issuer = _issuer()
    _, driver, _ = _provisioned(fly_replay_enabled=False, issuer=issuer)
    issuer.vendor.delete_fails = True
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        driver.delete(fly_job(), None)


def test_a_surviving_credential_is_inventoried_as_an_orphan() -> None:
    """T11: the credential joins inventory, so it is never left behind."""
    issuer = _issuer()
    api, driver, secrets = _provisioned(fly_replay_enabled=False, issuer=issuer)
    allocation = driver.expected_allocation_id("activation-fly-001")
    bundle = secrets.issued["activation-fly-001"].pool
    assert bundle is not None
    survivors = (
        PoolCredentialRecord(
            provider_allocation_id=allocation,
            provider_ref="vendor-key-1",
        ),
    )
    resources = fly_driver(
        api,
        secrets,
        credentials=_FakeCredentialInventory(survivors),
    ).list_resources(["activation-fly-001"])
    credentials = [
        resource
        for resource in resources
        if resource.resource_class is ResourceClass.CREDENTIAL
    ]
    assert len(credentials) == 1
    assert credentials[0].provider_allocation_id == allocation
    assert credentials[0].state is ResourceState.OTHER
    assert credentials[0].provider_ref == "vendor-key-1"


@dataclass
class _FakeCredentialInventory:
    """A stand-in for the vendor key listing #1896's adapter will serve."""

    records: tuple[PoolCredentialRecord, ...]

    def list_pool_credentials(
        self,
        allocation_ids: Sequence[str],
    ) -> tuple[PoolCredentialRecord, ...]:
        """Return the recorded survivors for *allocation_ids*."""
        wanted = set(allocation_ids)
        return tuple(
            record for record in self.records if record.provider_allocation_id in wanted
        )


# --------------------------------------------------------------------------
# Secrets discipline: the credential reaches no log and no record.
# --------------------------------------------------------------------------


def test_the_credential_never_reaches_a_log_a_repr_or_a_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3/D1.12: ids, counts and versions only — never the key itself."""
    issuer = _issuer()
    caplog.set_level(logging.DEBUG)
    api, driver, secrets = _provisioned(fly_replay_enabled=True, issuer=issuer)
    bundle = secrets.issued["activation-fly-001"].pool
    assert bundle is not None
    secret = bundle.credential.vendor_key_value()
    driver.delete(fly_job(), None)
    haystacks = [
        caplog.text,
        repr(bundle),
        repr(bundle.credential),
        repr(secrets.issued["activation-fly-001"]),
        str(bundle),
        json.dumps(bundle.record(), sort_keys=True),
        " ".join(f"{method} {path}" for method, path in api.requests),
    ]
    for haystack in haystacks:
        assert secret not in haystack
    record = bundle.record()
    assert record["bundle_version"] == 1
    assert record["credential_kind"] == PoolCredentialKind.VENDOR_KEY.value
    assert not any("key" in str(value).lower() for value in [record.get("credential")])


def test_the_operational_record_carries_no_prompt_or_manifest_body() -> None:
    """D1.12: the record is a closed vocabulary, not the bundle's contents."""
    record = pool_bundle().record()
    assert set(record) == {
        "bundle_version",
        "credential_kind",
        "manifest_digest",
        "pool_url_host",
    }
    assert record["pool_url_host"] == "pool.vendor.example"


# --------------------------------------------------------------------------
# Cap sizing (Decision 3.4), against measured tokens only.
# --------------------------------------------------------------------------


def test_caps_refuse_to_be_sized_from_an_unmeasured_token_block() -> None:
    """Never price from a planning assumption or a byte upper bound."""
    with pytest.raises(ValueError, match="measured"):
        size_pool_caps(
            tokens=unmeasured_tokens(),
            monthly_reflection_cap=20,
            input_tokens_spent=0,
            output_tokens_spent=0,
            lifetime_usd=None,
            expires_at=NOW + timedelta(days=30),
        )


def test_caps_are_sized_from_spent_plus_the_allowance_doubled() -> None:
    """Decision 3.4: spent + monthly cap x measured tokens x 2, per direction."""
    tokens = _measured_tokens(prompt=1_000, output=100)
    caps = size_pool_caps(
        tokens=tokens,
        monthly_reflection_cap=20,
        input_tokens_spent=5_000,
        output_tokens_spent=500,
        lifetime_usd=Decimal("0.50"),
        expires_at=NOW + timedelta(days=30),
    )
    assert caps.input_tokens == 5_000 + 20 * 1_000 * 2
    assert caps.output_tokens == 500 + 20 * 100 * 2
    assert caps.lifetime_usd == Decimal("0.50")


def _measured_tokens(*, prompt: int, output: int) -> TokensPerReflection:
    """Return a provider-reported token block with the worst-case surface."""
    block = unmeasured_tokens()
    return TokensPerReflection(
        basis=TokenBasis.PROVIDER_REPORTED,
        per_surface={
            Surface.REFLECTION_GROUNDED: SurfaceTokens(
                surface=Surface.REFLECTION_GROUNDED,
                prompt_tokens=prompt,
                output_tokens=output,
                max_output_tokens=128,
                output_cap_exceeded=False,
                outcome=Outcome.OK,
            )
        },
        prompt_overhead_tokens=10,
        prompt_overhead_bound=block.prompt_overhead_bound,
        prompt_overhead_within_bound=True,
        assumed_prompt_tokens=block.assumed_prompt_tokens,
        assumed_output_tokens=block.assumed_output_tokens,
    )


# --------------------------------------------------------------------------
# T16 cost line.
# --------------------------------------------------------------------------

_BASE_POLICY = """
[budget]
currency = "USD"
monthly_budget = "25.00"
volume_gb_month_rate = "0.15"
stopped_rootfs_gb_month_rate = "0.15"
running_hour_rate = "0.0000008"
[policy]
max_continuous_running_seconds = 3600
stuck_deletion_seconds = 900
[review]
activated_vaults = 25
provisioned_volumes = 25
months_over_budget = 2
"""

_POOL_RATES = """
pool_input_mtoken_rate = "0.10"
pool_output_mtoken_rate = "0.50"
"""


def _policy_path(tmp_path: Path, body: str) -> Path:
    """Write one operator policy TOML and return its path."""
    path = tmp_path / "fleet-policy.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_priced_pool_requires_a_dated_sheet(tmp_path: Path) -> None:
    """T16: pool rates without ``pool_rate_observed_on`` are refused."""
    body = _BASE_POLICY.replace("[policy]", _POOL_RATES + "[policy]")
    with pytest.raises(ValueError, match="pool_rate_observed_on"):
        load_policy_file(_policy_path(tmp_path, body), today=_TODAY)


def test_a_stale_pool_sheet_is_refused(tmp_path: Path) -> None:
    """T16: the staleness refusal ``bench.sh cost`` applies, now in budget.py."""
    dated = _POOL_RATES + "pool_rate_observed_on = 2025-01-01\n"
    body = _BASE_POLICY.replace("[policy]", dated + "[policy]")
    with pytest.raises(ValueError, match="stale"):
        load_policy_file(_policy_path(tmp_path, body), today=_TODAY)


def test_a_future_pool_sheet_is_refused(tmp_path: Path) -> None:
    """A sheet dated after today is as unusable as a stale one."""
    dated = _POOL_RATES + "pool_rate_observed_on = 2027-01-01\n"
    body = _BASE_POLICY.replace("[policy]", dated + "[policy]")
    with pytest.raises(ValueError, match="future"):
        load_policy_file(_policy_path(tmp_path, body), today=_TODAY)


def test_a_fresh_pool_sheet_prices_the_pool_line(tmp_path: Path) -> None:
    """T16: the fleet report grows a pool cost line from the dated sheet."""
    dated = _POOL_RATES + "pool_rate_observed_on = 2026-10-01\n"
    body = _BASE_POLICY.replace("[policy]", dated + "[policy]")
    body += "[usage]\npool_input_tokens = 2000000\npool_output_tokens = 1000000\n"
    loaded = load_policy_file(_policy_path(tmp_path, body), today=_TODAY)
    assert loaded.policy.pool_input_mtoken_rate == Decimal("0.10")
    assert loaded.usage.pool_input_tokens == 2_000_000
    estimate = _estimate(loaded)
    assert estimate.components["pool"] == Decimal("0.70")
    assert "pool" not in estimate.unpriced


def test_an_unpriced_pool_is_listed_not_zeroed(tmp_path: Path) -> None:
    """A policy with no pool rate keeps working and says the pool is unpriced."""
    loaded = load_policy_file(_policy_path(tmp_path, _BASE_POLICY), today=_TODAY)
    assert loaded.policy.pool_input_mtoken_rate is None
    assert "pool" in _estimate(loaded).unpriced


def _estimate(loaded: Any) -> Any:
    """Estimate one month from an otherwise-empty fleet."""
    telemetry = FleetTelemetry(
        activated_allocations=0,
        allocations_by_state={},
        provisioned_volumes=0,
        volume_bytes=0,
        stopped_rootfs_gb=0,
        machines_without_rootfs_size=0,
        running_machine_seconds_by_allocation={},
        running_machine_seconds_fleet=0,
        running_seconds_injected=0,
        snapshot_bytes=None,
        egress_bytes=None,
        pool_input_tokens=loaded.usage.pool_input_tokens,
        pool_output_tokens=loaded.usage.pool_output_tokens,
        duplicate_allocation_attempts=0,
        orphan_resources=0,
        unconfirmed_deletions=0,
        oldest_unconfirmed_deletion_seconds=None,
        sources={},
    )
    return estimate_monthly_cost(
        telemetry,
        loaded.policy,
        now=datetime(2026, 10, 21, tzinfo=UTC),
    )


def test_a_surviving_credential_raises_its_own_alert_kind() -> None:
    """D1.12: a leftover credential is not hidden inside ``orphan_resource``.

    It is still spendable up to its cap until revoked, which an orphaned
    volume is not, so it carries its own closed-vocabulary kind.
    """
    divergences = [
        Divergence(
            DivergenceKind.ORPHAN_RESOURCE,
            Disposition.REPORTED,
            "fly-" + "a" * 24,
            resource_class,
            None,
            None,
        )
        for resource_class in (ResourceClass.CREDENTIAL, ResourceClass.VOLUME)
    ]
    policy = _bare_policy()
    estimate = _estimate_for(policy)
    kinds = {alert.kind for alert in evaluate_alerts(divergences, estimate, policy)}
    assert AlertKind.POOL_CREDENTIAL_ORPHAN in kinds
    assert AlertKind.ORPHAN_RESOURCE in kinds
    assert AlertKind.POOL_CREDENTIAL_ORPHAN.value == "pool_credential_orphan"


def _bare_policy() -> FleetPolicy:
    """Return a minimal valid policy with no pool rate."""
    return FleetPolicy(
        currency="USD",
        monthly_budget=Decimal("25"),
        volume_gb_month_rate=Decimal("0.15"),
        stopped_rootfs_gb_month_rate=Decimal("0.15"),
        running_hour_rate=Decimal("0.0001"),
        snapshot_gb_month_rate=None,
        egress_gb_rate=None,
        pool_input_mtoken_rate=None,
        pool_output_mtoken_rate=None,
        pool_rate_observed_on=None,
        max_continuous_running=timedelta(hours=1),
        stuck_deletion_after=timedelta(minutes=15),
        review_activated_vaults=25,
        review_provisioned_volumes=25,
        review_months_over_budget=2,
    )


def _estimate_for(policy: FleetPolicy) -> Any:
    """Estimate one month for *policy* over an empty fleet."""
    return _estimate(
        PolicyFile(
            policy=policy,
            usage=InjectedUsage(
                snapshot_bytes=None,
                egress_bytes=None,
                running_seconds=0,
                pool_input_tokens=None,
                pool_output_tokens=None,
            ),
        )
    )


def test_fleet_policy_rejects_a_non_positive_pool_rate() -> None:
    """A zero or negative rate is an operator error, not a free pool."""
    with pytest.raises(ValueError, match="pool_input_mtoken_rate"):
        FleetPolicy(
            currency="USD",
            monthly_budget=Decimal("25"),
            volume_gb_month_rate=Decimal("0.15"),
            stopped_rootfs_gb_month_rate=Decimal("0.15"),
            running_hour_rate=Decimal("0.0001"),
            snapshot_gb_month_rate=None,
            egress_gb_rate=None,
            pool_input_mtoken_rate=Decimal("0"),
            pool_output_mtoken_rate=None,
            pool_rate_observed_on=None,
            max_continuous_running=timedelta(hours=1),
            stuck_deletion_after=timedelta(minutes=15),
            review_activated_vaults=25,
            review_provisioned_volumes=25,
            review_months_over_budget=2,
        )


def test_the_runtime_bundle_still_refuses_an_incomplete_core() -> None:
    """The pool field is additive; the pre-existing invariant is unchanged."""
    with pytest.raises(ValueError, match="complete"):
        FlyRuntimeSecrets(
            consumer_credential="",
            replay_state="r" * 64,
            consumer_registry=b"x",
            tls_certificate=b"x",
            tls_private_key=b"x",
        )


# --------------------------------------------------------------------------
# The encrypted store: the bundle is stored per activation and rolled per
# activation, and revocation is confirmed before the receipt (T11, T12, T26).
# --------------------------------------------------------------------------


def _store_manager(
    tmp_path: Path,
    issuer: FakePoolBundleIssuer | None,
) -> EncryptedFileFlySecretManager:
    """Return an encrypted-file secret manager wired to *issuer*."""
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    master = _owner_file(tmp_path / "master-key", b"m" * 32)
    certificate, key = _ca_files(tmp_path)
    return EncryptedFileFlySecretManager(
        state,
        master_key_file=master,
        ca_certificate_file=certificate,
        ca_private_key_file=key,
        app_prefix="creek-vault",
        clock=lambda: _NOW,
        pool_bundles=issuer,
    )


_ACTIVATION = "activation-pool-store-001"
_CONSUMER = "adepthood-user-001"
_REQUESTER = "adepthood"


def _issued(manager: EncryptedFileFlySecretManager) -> Any:
    """Issue this activation's bundle through *manager*."""
    return manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)


def test_the_stored_bundle_survives_a_restart_and_holds_no_plaintext_key(
    tmp_path: Path,
) -> None:
    """The bundle is encrypted at rest and identical on every later issuance."""
    issuer = _issuer()
    manager = _store_manager(tmp_path, issuer)
    first = _issued(manager)
    second = _issued(manager)
    assert first.pool is not None
    assert first.pool == second.pool
    assert issuer.vendor.minted == 1
    secret = first.pool.credential.vendor_key_value()
    on_disk = b"".join(
        path.read_bytes() for path in (tmp_path / "runtime-secrets").iterdir()
    )
    assert secret.encode() not in on_disk


def test_a_policy_flip_changes_only_the_next_activation(tmp_path: Path) -> None:
    """T26: the stored bundle, not the fleet identity, is what a Machine matches."""
    issuer = _issuer()
    manager = _store_manager(tmp_path, issuer)
    first = _issued(manager)
    issuer.fleet_version = 7
    issuer.fleet_model = "pool-model-z"
    again = _issued(manager)
    assert first.pool is not None
    assert again.pool is not None
    assert again.pool.bundle_version == 1
    assert again.pool.manifest_digest() == first.pool.manifest_digest()


def test_rolling_one_activation_writes_the_next_version_and_revokes_the_old(
    tmp_path: Path,
) -> None:
    """Decision 1 item 1's "write the new bundle version" half of a roll."""
    issuer = _issuer()
    manager = _store_manager(tmp_path, issuer)
    first = _issued(manager)
    assert first.pool is not None
    issuer.fleet_version = 2
    issuer.fleet_model = "pool-model-b"
    rolled = manager.roll_pool_bundle(_ACTIVATION)
    assert rolled.bundle_version == 2
    assert rolled.manifest_digest() != first.pool.manifest_digest()
    assert not issuer.vendor.accepts(first.pool.credential)
    assert issuer.vendor.accepts(rolled.credential)
    reloaded = _issued(manager)
    assert reloaded.pool == rolled


def test_rolling_refuses_without_an_issuer_or_a_stored_pool(tmp_path: Path) -> None:
    """A roll is never silently a no-op."""
    issuer = _issuer()
    manager = _store_manager(tmp_path, issuer)
    with pytest.raises(ValueError, match="unreadable"):
        manager.roll_pool_bundle("activation-never-issued")
    _issued(manager)
    unconfigured = _store_manager(tmp_path / "pool-free", None)
    _issued(unconfigured)
    with pytest.raises(ValueError, match="no pool bundle issuer"):
        unconfigured.roll_pool_bundle(_ACTIVATION)


def test_revocation_is_confirmed_at_the_vendor_before_the_tombstone(
    tmp_path: Path,
) -> None:
    """T11: an unconfirmed revocation leaves the bundle, so a retry can revoke."""
    issuer = _issuer()
    manager = _store_manager(tmp_path, issuer)
    bundle = _issued(manager).pool
    assert bundle is not None
    issuer.vendor.delete_fails = True
    with pytest.raises(RuntimeError, match="vendor admin API is unavailable"):
        manager.revoke(_ACTIVATION)
    assert issuer.vendor.accepts(bundle.credential)
    assert _issued(manager).pool == bundle
    issuer.vendor.delete_fails = False
    manager.revoke(_ACTIVATION)
    assert not issuer.vendor.accepts(bundle.credential)
    with pytest.raises(ValueError, match="revoked"):
        _issued(manager)


def test_revocation_without_a_pool_issuer_is_unchanged(tmp_path: Path) -> None:
    """The pool-free path keeps the pre-existing tombstone-then-unlink order."""
    manager = _store_manager(tmp_path, None)
    manager.issue("activation-pool-free", _CONSUMER, requester_identity=_REQUESTER)
    manager.revoke("activation-pool-free")
    with pytest.raises(ValueError, match="revoked"):
        manager.issue("activation-pool-free", _CONSUMER, requester_identity=_REQUESTER)


# --------------------------------------------------------------------------
# Every refusal path on the bundle's own invariants.
# --------------------------------------------------------------------------


def test_a_credential_must_match_its_declared_kind_exactly() -> None:
    """A vendor key cannot be smuggled in under the mutual-TLS shape."""
    with pytest.raises(ValueError, match="do not match its kind"):
        PoolCredential(
            PoolCredentialKind.BEARER_AND_CLIENT_TLS,
            ((POOL_VENDOR_KEY_GUEST_PATH, b"k"),),
        )


def test_a_credential_refuses_an_empty_value() -> None:
    """An empty key would make the vault fail at the pool, not at activation."""
    with pytest.raises(ValueError, match="must not be empty"):
        PoolCredential.vendor_key("")


def test_only_a_vendor_key_yields_a_vendor_key_value() -> None:
    """The O3 triple has no single full-key value to hand the vendor's API."""
    credential = PoolCredential.bearer_and_client_tls(
        token=b"t", certificate=b"c", private_key=b"k"
    )
    with pytest.raises(ValueError, match="not a vendor key"):
        credential.vendor_key_value()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bundle_version": 0}, "version must be positive"),
        ({"manifest_json": "  "}, "manifest must not be blank"),
        ({"pool_url": "pool.vendor.example"}, "absolute HTTP"),
        ({"pool_url": "ftp://pool.vendor.example"}, "absolute HTTP"),
    ],
)
def test_a_bundle_refuses_an_unversioned_or_destinationless_shape(
    overrides: dict[str, Any],
    message: str,
) -> None:
    """Decision 1 items 1 and 2 are invariants of the stored bundle itself."""
    with pytest.raises(ValueError, match=message):
        replace(pool_bundle(), **overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"lifetime_usd": Decimal("0")}, "dollar cap must be positive"),
        ({"input_tokens": 0}, "token caps must be positive"),
        ({"output_tokens": -1}, "token caps must be positive"),
        (
            {"expires_at": datetime(2026, 10, 21)},
            "expiry must be timezone-aware",
        ),
    ],
)
def test_a_cap_refuses_an_unbounded_triple(
    overrides: dict[str, Any],
    message: str,
) -> None:
    """An unbounded cap is the one thing Decision 3.4 does not allow."""
    with pytest.raises(ValueError, match=message):
        replace(_caps(), **overrides)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"monthly_reflection_cap": 0}, "allowance must be positive"),
        ({"input_tokens_spent": -1}, "must not be negative"),
        ({"output_tokens_spent": -1}, "must not be negative"),
    ],
)
def test_cap_sizing_refuses_a_nonsense_allowance(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    """Sizing to the allowance means the allowance has to be real."""
    base: dict[str, Any] = {
        "tokens": _measured_tokens(prompt=10, output=5),
        "monthly_reflection_cap": 20,
        "input_tokens_spent": 0,
        "output_tokens_spent": 0,
        "lifetime_usd": None,
        "expires_at": NOW + timedelta(days=30),
    }
    with pytest.raises(ValueError, match=message):
        size_pool_caps(**{**base, **kwargs})


def test_cap_sizing_refuses_a_measured_block_that_reported_nothing() -> None:
    """``provider_reported`` with no counts is still nothing to size against."""
    tokens = TokensPerReflection(
        basis=TokenBasis.PROVIDER_REPORTED,
        per_surface={},
        prompt_overhead_tokens=None,
        prompt_overhead_bound=unmeasured_tokens().prompt_overhead_bound,
        prompt_overhead_within_bound=None,
        assumed_prompt_tokens=unmeasured_tokens().assumed_prompt_tokens,
        assumed_output_tokens=unmeasured_tokens().assumed_output_tokens,
    )
    with pytest.raises(ValueError, match="measured"):
        size_pool_caps(
            tokens=tokens,
            monthly_reflection_cap=20,
            input_tokens_spent=0,
            output_tokens_spent=0,
            lifetime_usd=None,
            expires_at=NOW + timedelta(days=30),
        )


def test_a_stored_bundle_that_is_not_a_bundle_is_refused(tmp_path: Path) -> None:
    """A tampered encrypted document fails closed rather than half-loading."""
    from creek_mcp.provisioning.production_secrets import _decode_pool

    assert _decode_pool(None) is None
    with pytest.raises(ValueError, match="invalid"):
        _decode_pool("not-a-bundle")
    with pytest.raises(ValueError, match="invalid"):
        _decode_pool({"credential_files": "not-a-list"})
    del tmp_path


def test_the_bootstrap_rejects_an_invalid_pool_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Machine declaring an unknown credential kind must not boot."""
    from creek_mcp.container_runtime import ContainerConfigurationError
    from creek_mcp.provisioning.fly_vault_bootstrap import _expected_secret_files

    monkeypatch.setenv("CREEK_POOL_URL", POOL_URL)
    monkeypatch.setenv(POOL_CREDENTIAL_KIND_ENV, "invented")
    with pytest.raises(ContainerConfigurationError, match="pool secret"):
        _expected_secret_files()
    monkeypatch.setenv(POOL_CREDENTIAL_KIND_ENV, PoolCredentialKind.VENDOR_KEY.value)
    assert Path(POOL_VENDOR_KEY_GUEST_PATH) in _expected_secret_files()
