"""Fly soft-deleted volumes must not strand managed-vault teardown."""

from __future__ import annotations

import pytest

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import FailureReason, ResourceClass, ResourceState
from tests.fly_support import FakeFlyAPI, FakeSecretManager, fly_driver, fly_job

_DELETED_STATES = (
    "scheduling_destroy",
    "fork_cleanup",
    "waiting_for_detach",
    "pending_destroy",
    "destroying",
    "destroyed",
)


@pytest.mark.parametrize("volume_state", _DELETED_STATES)
def test_delete_converges_with_soft_deleted_volume(volume_state: str) -> None:
    """Fly retains volume tombstones after DELETE until its retention expires."""
    api = FakeFlyAPI()
    api.deleted_volume_state = volume_state
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)

    outcome = driver.delete(job, allocation.allocation_id)
    assert driver.delete(job, allocation.allocation_id) == outcome
    assert api.apps == {}
    assert secrets.revoked == {job.activation_id}
    assert (
        sum(method == "DELETE" and "/volumes/" in path for method, path in api.requests)
        == 1
    )


@pytest.mark.parametrize("volume_state", _DELETED_STATES)
def test_inventory_classifies_soft_deleted_volume(volume_state: str) -> None:
    """Retained tombstones cannot count as live encrypted storage."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    app = next(iter(api.apps))
    api.volumes[app][0]["state"] = volume_state

    volumes = [
        resource
        for resource in driver.list_resources([job.activation_id])
        if resource.resource_class is ResourceClass.VOLUME
    ]
    assert len(volumes) == 1
    assert volumes[0].state is ResourceState.DESTROYED


def test_soft_deleted_volume_does_not_confirm_an_existing_app() -> None:
    """A failed final app deletion still requires a retry and absence proof."""
    api = FakeFlyAPI()
    api.deleted_volume_state = "pending_destroy"
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    app = next(iter(api.apps))
    api.fail_once("DELETE", f"/apps/{app}")

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, allocation.allocation_id)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable
    assert app in api.apps
    driver.delete(job, allocation.allocation_id)
    assert api.apps == {}
    assert (
        sum(method == "DELETE" and "/volumes/" in path for method, path in api.requests)
        == 1
    )


@pytest.mark.parametrize("volume_state", _DELETED_STATES)
def test_provision_never_reuses_a_soft_deleted_volume(volume_state: str) -> None:
    """A retry must allocate usable storage instead of mounting a tombstone."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    app = next(iter(api.apps))
    old_volume = api.volumes[app][0]
    old_volume["state"] = volume_state
    api.machines[app].clear()

    driver.provision(job)

    assert len(api.volumes[app]) == 2
    mounted_id = api.machines[app][0]["config"]["mounts"][0]["volume"]
    assert mounted_id != old_volume["id"]
    assert mounted_id == api.volumes[app][1]["id"]
