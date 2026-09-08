"""Fly Machines lifecycle contract for issue #1770."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning.driver import FakeOneTimeHandoff, ProviderError
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker

# The double and its canaries moved to tests/fly_api_support.py so fleet
# reconciliation (#1769) drives the same Fly fake rather than a second copy.
# They are re-bound to this module's original private spellings so that every
# assertion below stays byte-identical to the pre-move revision (#1769).
from tests.fly_api_support import (
    CONSUMER_TOKEN as _CONSUMER_TOKEN,
)
from tests.fly_api_support import (
    NOW as _NOW,
)
from tests.fly_api_support import (
    PROVIDER_TOKEN as _PROVIDER_TOKEN,
)
from tests.fly_api_support import (
    TLS_KEY as _TLS_KEY,
)
from tests.fly_api_support import (
    FakeFlyAPI as _FakeFlyAPI,
)
from tests.fly_api_support import (
    FakeSecretManager as _FakeSecretManager,
)
from tests.fly_api_support import (
    build_driver as _driver,
)
from tests.fly_api_support import (
    build_job as _job,
)
from tests.fly_api_support import (
    only_app as _only_app,
)
from tests.fly_api_support import (
    only_machine as _only_machine,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "provisioning-control-plane.md"
)


def test_reference_policy_creates_one_private_scale_to_zero_allocation() -> None:
    """Reference defaults remain config while the request stays private."""
    api = _FakeFlyAPI()
    driver = _driver(api)

    allocation = driver.provision(_job())

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
    api = _FakeFlyAPI()
    api.failure_body = f"echo {_PROVIDER_TOKEN} {_CONSUMER_TOKEN} {_TLS_KEY}"
    api.fail_once("POST", "/machines")
    driver = _driver(api)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.provision(_job())

    rendered = caplog.text + repr(driver) + str(raised.value)
    assert _PROVIDER_TOKEN not in rendered
    assert _CONSUMER_TOKEN not in rendered
    assert _TLS_KEY not in rendered


def test_credential_file_requires_an_org_deploy_scope_and_hides_token(
    tmp_path: Path,
) -> None:
    """A personal token cannot accidentally become the fleet credential."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(f"{_PROVIDER_TOKEN}\n", encoding="utf-8")
    token_file.chmod(0o600)

    credential = FlyCredential.from_file(
        token_file,
        organization="creek-vaults",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )

    assert _PROVIDER_TOKEN not in repr(credential)
    with pytest.raises(ValueError, match="org-scoped deploy"):
        FlyCredential(
            token=_PROVIDER_TOKEN,
            organization="creek-vaults",
            scope=FlyCredentialScope.PERSONAL,
            expires_at=_NOW + timedelta(days=1),
        )


def test_credential_file_rejects_unsafe_permissions(tmp_path: Path) -> None:
    """Provider credentials are never loaded from a group-readable file."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(_PROVIDER_TOKEN, encoding="utf-8")
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
        token=_PROVIDER_TOKEN,
        organization="another-organization",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="organization does not match"):
        FlyProviderDriver(policy, credential, _FakeSecretManager(set()))


def test_provision_start_stop_and_delete_are_idempotent() -> None:
    """Every lifecycle call is safe to replay after an unknown outcome."""
    api = _FakeFlyAPI()
    secrets = _FakeSecretManager(set())
    driver = _driver(api, secrets)
    job = _job()

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
    api = _FakeFlyAPI()
    driver = _driver(api)
    job = _job()
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
    api = _FakeFlyAPI()
    driver = _driver(api)
    job = _job()
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
    api = _FakeFlyAPI()
    driver = _driver(api)
    job = _job()
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
    api = _FakeFlyAPI()
    secrets = _FakeSecretManager(set())
    driver = _driver(api, secrets)
    job = _job()
    allocation = driver.provision(job)

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, "fly-allocation-for-a-different-activation")

    assert raised.value.retryable is False
    assert allocation.allocation_id != "fly-allocation-for-a-different-activation"
    assert len(api.apps) == 1
    assert secrets.revoked == set()


def test_reconciliation_rejects_colliding_app_or_machine_identity() -> None:
    """Deterministic names cannot make foreign Fly resources adoptable."""
    api = _FakeFlyAPI()
    driver = _driver(api)
    job = _job()
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
    api = _FakeFlyAPI()
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    job = store.submit("activation-from-worker", "adepthood", now=_NOW)
    worker = ProvisioningWorker(store, _driver(api), FakeOneTimeHandoff())

    assert worker.run_once(now=_NOW) is True

    assert _only_machine(api)["config"]["metadata"]["creek_activation_id"] == (
        job.activation_id
    )


@pytest.mark.integration
def test_fake_fly_api_reconciles_partial_creation() -> None:
    """A retry adopts one app and volume left by a failed Machine create."""
    api = _FakeFlyAPI()
    api.fail_once("POST", "/machines")
    driver = _driver(api)
    job = _job("activation-partial-create")

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
    api = _FakeFlyAPI()
    secrets = _FakeSecretManager(set())
    driver = _driver(api, secrets)
    job = _job("activation-partial-delete")
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
