"""Fly Machines lifecycle contract for issue #1770."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.provisioning.driver import FakeOneTimeHandoff, ProviderError
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    FailureReason,
    ResourceClass,
    ResourceState,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_support import (
    CONSUMER_TOKEN,
    PROVIDER_TOKEN,
    TLS_KEY,
    FakeFlyAPI,
    FakeSecretManager,
    fly_driver,
    fly_job,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_NOW = datetime(2026, 9, 7, 4, tzinfo=UTC)
_RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "provisioning-control-plane.md"
)


def _only_app(api: FakeFlyAPI) -> str:
    assert len(api.apps) == 1
    return next(iter(api.apps))


def _only_machine(api: FakeFlyAPI) -> dict[str, Any]:
    machines = api.machines[_only_app(api)]
    assert len(machines) == 1
    return machines[0]


def test_reference_policy_creates_one_private_scale_to_zero_allocation() -> None:
    """Reference defaults remain config while the request stays private."""
    api = FakeFlyAPI()
    driver = fly_driver(api)

    allocation = driver.provision(fly_job())

    app_name = _only_app(api)
    volume = api.volumes[app_name][0]
    machine = _only_machine(api)
    config = machine["config"]
    assert allocation.allocation_id == config["metadata"]["creek_allocation_id"]
    assert config["metadata"]["creek_activation_id"] == "activation-fly-001"
    assert api.apps[app_name]["network"] == allocation.allocation_id
    assert volume == {
        "id": "vol-1",
        "name": f"{allocation.allocation_id}-vault",
        "region": "iad",
        "size_gb": 5,
        "encrypted": True,
        "state": "created",
    }
    assert machine["region"] == "iad"
    assert machine["state"] == "stopped"
    assert config["guest"] == {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024}
    assert config["rootfs"] == {"size_gb": 1, "persist": "never"}
    assert config["mounts"] == [
        {"volume": "vol-1", "path": "/vault", "encrypted": True}
    ]
    assert config["restart"] == {"policy": "no"}
    assert config["services"] == [
        {
            "protocol": "tcp",
            "internal_port": 8823,
            "ports": [],
            "autostart": True,
            "autostop": "stop",
            "min_machines_running": 0,
        }
    ]
    assert all("ips" not in path for _, path in api.requests)
    assert allocation.vault_url.endswith(".internal:8823/v1")


def test_fly_runbook_pins_scope_reconciliation_and_background_ownership() -> None:
    """Operators receive the security and lifecycle rules the adapter enforces."""
    text = " ".join(_RUNBOOK.read_text(encoding="utf-8").lower().split())

    for phrase in (
        "org-scoped deploy token",
        "never a personal access token",
        "partial create",
        "partial delete",
        "no dedicated ipv4",
        "drain, commit, close, and stop",
        "ordinary fly machine does not advertise intimate",
    ):
        assert phrase in text


def test_provider_and_runtime_secrets_are_repr_safe_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider bodies and every credential stay out of exceptions and logs."""
    api = FakeFlyAPI()
    api.failure_body = f"echo {PROVIDER_TOKEN} {CONSUMER_TOKEN} {TLS_KEY}"
    api.fail_once("POST", "/machines")
    driver = fly_driver(api)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.provision(fly_job())

    rendered = caplog.text + repr(driver) + str(raised.value)
    assert PROVIDER_TOKEN not in rendered
    assert CONSUMER_TOKEN not in rendered
    assert TLS_KEY not in rendered


def test_credential_file_requires_an_org_deploy_scope_and_hides_token(
    tmp_path: Path,
) -> None:
    """A personal token cannot accidentally become the fleet credential."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(f"{PROVIDER_TOKEN}\n", encoding="utf-8")
    token_file.chmod(0o600)

    credential = FlyCredential.from_file(
        token_file,
        organization="creek-vaults",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )

    assert PROVIDER_TOKEN not in repr(credential)
    with pytest.raises(ValueError, match="org-scoped deploy"):
        FlyCredential(
            token=PROVIDER_TOKEN,
            organization="creek-vaults",
            scope=FlyCredentialScope.PERSONAL,
            expires_at=_NOW + timedelta(days=1),
        )


def test_credential_file_rejects_unsafe_permissions(tmp_path: Path) -> None:
    """Provider credentials are never loaded from a group-readable file."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(PROVIDER_TOKEN, encoding="utf-8")
    token_file.chmod(0o640)

    with pytest.raises(ValueError, match="owner-only and regular"):
        FlyCredential.from_file(
            token_file,
            organization="creek-vaults",
            scope=FlyCredentialScope.ORG_DEPLOY,
            expires_at=_NOW + timedelta(days=1),
        )


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/creek:latest",
        "registry.example/creek@sha256:abc123",
        "registry.example/creek@sha256:" + "z" * 64,
    ],
)
def test_policy_rejects_mutable_or_malformed_image_references(image: str) -> None:
    """Only an exact OCI sha256 digest can reach a Fly Machine request."""
    with pytest.raises(ValueError, match="immutable sha256 digest"):
        FlyProviderPolicy(
            organization="creek-vaults",
            image=image,
        )


def test_policy_rejects_plaintext_provider_transport() -> None:
    """The deploy token can never be sent over a plaintext API connection."""
    with pytest.raises(ValueError, match="HTTPS"):
        FlyProviderPolicy(
            organization="creek-vaults",
            image="registry.example/creek@sha256:" + "a" * 64,
            api_base_url="http://fly.example.test",
        )


def test_driver_rejects_cross_organization_credentials() -> None:
    """A rollout cannot silently cross provider tenant boundaries."""
    policy = FlyProviderPolicy(
        organization="creek-vaults",
        image="registry.example/creek@sha256:" + "a" * 64,
    )
    credential = FlyCredential(
        token=PROVIDER_TOKEN,
        organization="another-organization",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="organization does not match"):
        FlyProviderDriver(policy, credential, FakeSecretManager(set()))


def test_provision_start_stop_and_delete_are_idempotent() -> None:
    """Every lifecycle call is safe to replay after an unknown outcome."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()

    first = driver.provision(job)
    second = driver.provision(job)
    driver.start(job.activation_id)
    driver.start(job.activation_id)
    driver.stop(job.activation_id)
    driver.stop(job.activation_id)
    driver.delete(job, first.allocation_id)
    driver.delete(job, first.allocation_id)

    assert first == second
    assert api.apps == {}
    assert not any(api.volumes.values())
    assert not any(api.machines.values())
    assert secrets.revoked == {job.activation_id}


def test_stopped_machine_restarts_with_the_same_encrypted_volume() -> None:
    """Scale-to-zero preserves the durable volume across demand starts."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    original_volume = _only_machine(api)["config"]["mounts"][0]["volume"]

    driver.start(job.activation_id)
    driver.stop(job.activation_id)
    driver.start(job.activation_id)

    machine = _only_machine(api)
    assert machine["state"] == "started"
    assert machine["config"]["mounts"][0] == {
        "volume": original_volume,
        "path": "/vault",
        "encrypted": True,
    }


def test_background_work_owns_commit_close_and_shutdown_order() -> None:
    """Returning from initiation cannot stop work; the durable worker does."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)

    driver.start(job.activation_id)
    assert _only_machine(api)["state"] == "started"
    events: list[str] = []

    def record(event: str) -> Callable[[], None]:
        return lambda: events.append(event)

    driver.run_background_job(
        job.activation_id,
        drain=record("drain"),
        commit=record("commit"),
        close_vault=record("close"),
    )

    assert events == ["drain", "commit", "close"]
    assert _only_machine(api)["state"] == "stopped"


def test_background_failure_still_closes_and_stops_the_vault() -> None:
    """Failed durable work cannot strand an unlocked, billable Machine."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    events: list[str] = []

    def fail_drain() -> None:
        events.append("drain")
        raise RuntimeError("synthetic durable-work failure")

    with pytest.raises(RuntimeError, match="durable-work failure"):
        driver.run_background_job(
            job.activation_id,
            drain=fail_drain,
            commit=lambda: events.append("commit"),
            close_vault=lambda: events.append("close"),
        )

    assert events == ["drain", "close"]
    assert _only_machine(api)["state"] == "stopped"


def test_delete_rejects_a_cross_activation_allocation_before_revocation() -> None:
    """A stale queue record cannot delete or revoke another allocation."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, "fly-allocation-for-a-different-activation")

    assert raised.value.retryable is False
    assert allocation.allocation_id != "fly-allocation-for-a-different-activation"
    assert len(api.apps) == 1
    assert secrets.revoked == set()


def test_reconciliation_rejects_colliding_app_or_machine_identity() -> None:
    """Deterministic names cannot make foreign Fly resources adoptable."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    app_name = _only_app(api)
    api.apps[app_name]["organization"] = {"slug": "foreign-organization"}

    with pytest.raises(ProviderError) as app_error:
        driver.provision(job)
    assert app_error.value.retryable is False

    api.apps[app_name]["organization"] = {"slug": "creek-vaults"}
    api.machines[app_name].append(dict(_only_machine(api)))
    with pytest.raises(ProviderError) as machine_error:
        driver.provision(job)
    assert machine_error.value.retryable is False


def test_worker_supplies_activation_context_to_the_provider(tmp_path: Path) -> None:
    """Crash reconciliation is keyed by activation, not an opaque queue id."""
    api = FakeFlyAPI()
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    job = store.submit("activation-from-worker", "adepthood", now=_NOW)
    worker = ProvisioningWorker(store, fly_driver(api), FakeOneTimeHandoff())

    assert worker.run_once(now=_NOW) is True

    assert _only_machine(api)["config"]["metadata"]["creek_activation_id"] == (
        job.activation_id
    )


@pytest.mark.integration
def test_fake_fly_api_reconciles_partial_creation() -> None:
    """A retry adopts one app and volume left by a failed Machine create."""
    api = FakeFlyAPI()
    api.fail_once("POST", "/machines")
    driver = fly_driver(api)
    job = fly_job("activation-partial-create")

    with pytest.raises(ProviderError) as raised:
        driver.provision(job)
    assert raised.value.retryable is True
    app_name = _only_app(api)
    assert len(api.volumes[app_name]) == 1
    assert api.machines[app_name] == []

    driver.provision(job)

    assert len(api.apps) == 1
    assert len(api.volumes[app_name]) == 1
    assert len(api.machines[app_name]) == 1


@pytest.mark.integration
def test_fake_fly_api_reconciles_partial_deletion() -> None:
    """A failed teardown remains retryable until every billable resource is gone."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job("activation-partial-delete")
    allocation = driver.provision(job)
    api.fail_once("DELETE", "/volumes/vol-1")

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, allocation.allocation_id)
    assert raised.value.retryable is True
    app_name = _only_app(api)
    assert api.machines[app_name] == []
    assert len(api.volumes[app_name]) == 1

    driver.delete(job, allocation.allocation_id)

    assert api.apps == {}
    assert not any(api.volumes.values())
    assert not any(api.machines.values())
    assert secrets.revoked == {job.activation_id}


def test_delete_returns_a_provider_confirmed_outcome() -> None:
    """The outcome names provider and classes only after absence is verified."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)

    outcome = driver.delete(job, allocation.allocation_id)
    replay = driver.delete(job, allocation.allocation_id)

    assert outcome == DeletionOutcome(
        "fly",
        (
            ResourceClass.CREDENTIAL,
            ResourceClass.MACHINE,
            ResourceClass.VOLUME,
            ResourceClass.APP,
        ),
    )
    assert replay == outcome
    assert api.apps == {}
    for canary in (PROVIDER_TOKEN, CONSUMER_TOKEN, TLS_KEY):
        assert canary not in repr(outcome)


def test_list_resources_reads_only_per_app_get_calls_and_maps_states_and_sizes() -> (
    None
):
    """Inventory is three GETs per app, grouped by the app-derived allocation id."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    allocation = driver.provision(fly_job())
    app_name = _only_app(api)
    api.requests.clear()

    resources = driver.list_resources(["activation-fly-001"])

    pid = allocation.allocation_id
    assert resources == (
        ProviderResource(
            pid, ResourceClass.APP, ResourceState.OTHER, None, None, "app-1"
        ),
        ProviderResource(
            pid,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            1,
            "activation-fly-001",
            "machine-1",
        ),
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.OTHER, 5, None, "vol-1"
        ),
    )
    assert all(method == "GET" for method, _ in api.requests)
    assert {path for _, path in api.requests} == {
        f"/v1/apps/{app_name}",
        f"/v1/apps/{app_name}/machines",
        f"/v1/apps/{app_name}/volumes",
    }
    assert len(api.requests) == 3
    assert driver.expected_allocation_id("activation-fly-001") == pid

    machine = api.machines[app_name][0]
    for fly_state, expected in (
        ("started", ResourceState.RUNNING),
        ("starting", ResourceState.RUNNING),
        ("stopping", ResourceState.STOPPED),
        ("suspended", ResourceState.STOPPED),
        ("replacing", ResourceState.OTHER),
        ("destroyed", ResourceState.DESTROYED),
    ):
        machine["state"] = fly_state
        listed = driver.list_resources(["activation-fly-001"])
        assert [
            r.state for r in listed if r.resource_class is ResourceClass.MACHINE
        ] == [expected]
    del machine["config"]["rootfs"]
    api.volumes[app_name][0]["state"] = "destroyed"
    listed = driver.list_resources(["activation-fly-001"])
    assert [r.size_gb for r in listed if r.resource_class is ResourceClass.MACHINE] == [
        None
    ]
    assert [r.state for r in listed if r.resource_class is ResourceClass.VOLUME] == [
        ResourceState.DESTROYED
    ]


def test_list_resources_covers_injected_app_names_and_skips_missing_apps() -> None:
    """Discovery is bounded to derived and injected names under the app prefix."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    orphan_app = "creek-vault-" + "0" * 24
    api.apps[orphan_app] = {"id": "app-orphan", "name": orphan_app}
    api.volumes[orphan_app].append(
        {"id": "vol-orphan", "name": "left-behind", "size_gb": 3, "state": "created"}
    )
    api.apps["not-ours"] = {"id": "app-foreign", "name": "not-ours"}

    missing_app = "creek-vault-" + "f" * 24
    resources = driver.list_resources(
        [],
        app_names=[orphan_app, "not-ours", missing_app, "creek-vault-missing"],
    )
    nothing = driver.list_resources(["activation-never-provisioned"])

    pid = "fly-" + "0" * 24
    assert resources == (
        ProviderResource(
            pid, ResourceClass.APP, ResourceState.OTHER, None, None, "app-orphan"
        ),
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.OTHER, 3, None, "vol-orphan"
        ),
    )
    assert nothing == ()
    assert not any("not-ours" in path for _, path in api.requests)
    assert [path for _, path in api.requests if missing_app in path] == [
        f"/v1/apps/{missing_app}"
    ]
    assert not any("creek-vault-missing" in path for _, path in api.requests)
    assert ("GET", "/v1/apps") not in api.requests


def test_machine_metadata_claiming_another_allocation_stays_under_its_own_app() -> None:
    """Metadata is informational: a rogue Machine cannot be adopted by its claim."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    first = driver.provision(fly_job("activation-fly-001"))
    second = driver.provision(fly_job("activation-fly-002"))
    app_of_first = next(
        name for name, app in api.apps.items() if app["network"] == first.allocation_id
    )
    api.machines[app_of_first].append(
        {
            "id": "machine-rogue",
            "name": "rogue",
            "region": "iad",
            "state": "stopped",
            "config": {
                "rootfs": {"size_gb": 1},
                "metadata": {
                    "creek_allocation_id": second.allocation_id,
                    "creek_activation_id": "activation-fly-002",
                },
            },
        }
    )

    resources = driver.list_resources(["activation-fly-001", "activation-fly-002"])

    rogue = [r for r in resources if r.provider_ref == "machine-rogue"]
    assert rogue == [
        ProviderResource(
            first.allocation_id,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            1,
            "activation-fly-002",
            "machine-rogue",
        )
    ]
    second_machines = [
        r
        for r in resources
        if r.provider_allocation_id == second.allocation_id
        and r.resource_class is ResourceClass.MACHINE
    ]
    assert len(second_machines) == 1


def test_malformed_inventory_bodies_fail_closed_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-list inventory body is a retryable provider fault with no echo."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    driver.provision(fly_job())
    body_canary = "inventory-body-canary-" + PROVIDER_TOKEN
    api.malformed_once("GET", "/machines", f'{{"echo": "{body_canary}"}}')

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.list_resources(["activation-fly-001"])

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable is True
    rendered = caplog.text + str(raised.value) + repr(raised.value) + repr(driver)
    assert body_canary not in rendered
    assert PROVIDER_TOKEN not in rendered


def test_injected_app_names_outside_the_strict_pattern_never_reach_the_provider() -> (
    None
):
    """Only ``<prefix>-<24 hex>`` names are inventoried; nothing escapes the prefix."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    valid = "creek-vault-" + "a" * 24
    api.apps[valid] = {"id": "app-valid", "name": valid}

    resources = driver.list_resources(
        [],
        app_names=[
            "creek-vault-x/../other-app",
            "creek-vault-" + "A" * 24,
            "creek-vault-" + "a" * 23,
            "creek-vault-" + "a" * 25,
            "creek-vault-../../v1/apps",
            valid,
        ],
    )

    assert [r.provider_ref for r in resources] == ["app-valid"]
    assert [path for _, path in api.requests] == [
        f"/v1/apps/{valid}",
        f"/v1/apps/{valid}/machines",
        f"/v1/apps/{valid}/volumes",
    ]
    assert all(path.startswith("/v1/apps/creek-vault-") for _, path in api.requests)
    assert not any(".." in path or "other-app" in path for _, path in api.requests)


def test_fly_provision_and_delete_leave_no_secret_in_the_durable_database(
    tmp_path: Path,
) -> None:
    """The SQLite file and the receipt carry no provider, consumer or TLS secret."""
    api = FakeFlyAPI()
    database = tmp_path / "provisioning.sqlite3"
    store = ProvisioningStore(database)
    worker = ProvisioningWorker(store, fly_driver(api), FakeOneTimeHandoff())
    job = store.submit("activation-fly-bytes", "adepthood", now=_NOW)
    assert worker.run_once(now=_NOW) is True
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    assert worker.run_once(now=_NOW) is True

    persisted = database.read_bytes()
    receipt = store.list_deletion_receipts()[0]

    assert receipt.outcome.value == "confirmed"
    assert receipt.provider == "fly"
    for canary in (PROVIDER_TOKEN, CONSUMER_TOKEN, TLS_KEY):
        assert canary.encode() not in persisted
        assert canary not in repr(receipt)
