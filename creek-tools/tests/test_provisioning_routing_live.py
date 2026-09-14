"""External-network smoke for the deployed managed-vault route (#1807)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
import pytest

from creek_mcp.api.models import CONTRACT_MINOR

_REQUIRED_ENV = (
    "CREEK_ROUTING_LIVE_URL",
    "CREEK_ROUTING_LIVE_CREDENTIAL",
    "CREEK_ROUTING_LIVE_FLY_TOKEN",
    "CREEK_ROUTING_LIVE_FLY_APP",
    "CREEK_ROUTING_LIVE_FLY_MACHINE",
)


@dataclass(frozen=True, slots=True)
class _LiveRoute:
    """Mounted values for one disposable stopped test allocation."""

    route_url: str
    credential: str
    fly_token: str
    fly_app: str
    fly_machine: str


def _configuration() -> _LiveRoute:
    """Load the live-only mounted configuration or skip without partial use."""
    values = {name: os.environ.get(name) for name in _REQUIRED_ENV}
    if any(value is None for value in values.values()):
        pytest.skip("managed-vault routing live configuration is not mounted")
    return _LiveRoute(*(str(values[name]) for name in _REQUIRED_ENV))


def _wait_for(
    fly: httpx.Client,
    config: _LiveRoute,
    state: str,
    *,
    instance_id: str | None = None,
) -> None:
    """Wait at the provider for a bounded Machine state transition."""
    params = {"state": state, "timeout": 60}
    if instance_id is not None:
        params["instance_id"] = instance_id
    response = fly.get(
        f"/v1/apps/{config.fly_app}/machines/{config.fly_machine}/wait",
        params=params,
    )
    assert response.status_code == 200


@pytest.mark.live
def test_stopped_fly_allocation_recovers_through_the_public_route() -> None:
    """Write/read synthetic data externally, then return the test Machine to zero."""
    config = _configuration()
    fly_headers = {"Authorization": f"Bearer {config.fly_token}"}
    vault_headers = {
        "Authorization": f"Bearer {config.credential}",
        "X-Creek-Contract-Version": CONTRACT_MINOR,
        "X-Creek-Tier-Ceiling": "open",
    }
    external_id = "creek-routing-live-synthetic"
    draft_path = f"/voice-drafts/{external_id}"
    draft_present = False
    stopped_status: int | None = None
    with (
        httpx.Client(
            base_url="https://api.machines.dev",
            headers=fly_headers,
            timeout=70,
        ) as fly,
        httpx.Client(
            base_url=config.route_url.rstrip("/"),
            headers=vault_headers,
            timeout=70,
        ) as vault,
    ):
        machine = fly.get(f"/v1/apps/{config.fly_app}/machines/{config.fly_machine}")
        assert machine.status_code == 200
        instance_id = str(machine.json()["instance_id"])
        try:
            stopped = fly.post(
                f"/v1/apps/{config.fly_app}/machines/{config.fly_machine}/stop"
            )
            assert stopped.status_code in {200, 201, 204}
            _wait_for(fly, config, "stopped", instance_id=instance_id)

            capabilities = vault.get("/capabilities")
            assert capabilities.status_code == 200
            written = vault.put(
                draft_path,
                json={
                    "content": "Synthetic managed-routing live proof.",
                    "title": "Managed routing live proof",
                    "tier": "open",
                },
            )
            assert written.status_code == 200
            draft_present = True
            recalled = vault.get(draft_path)
            assert recalled.status_code == 200
            assert recalled.json()["content"] == (
                "Synthetic managed-routing live proof."
            )
            deleted = vault.delete(draft_path)
            assert deleted.status_code == 200
            draft_present = False

            recovered = fly.get(
                f"/v1/apps/{config.fly_app}/machines/{config.fly_machine}"
            )
            assert recovered.status_code == 200
            assert recovered.json()["state"] in {"started", "starting"}
        finally:
            if draft_present:
                vault.delete(draft_path)
            stopped_again = fly.post(
                f"/v1/apps/{config.fly_app}/machines/{config.fly_machine}/stop"
            )
            stopped_status = stopped_again.status_code
            if stopped_status in {200, 201, 204}:
                _wait_for(fly, config, "stopped")
    assert stopped_status in {200, 201, 204}
