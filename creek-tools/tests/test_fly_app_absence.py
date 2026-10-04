"""Deleted Fly apps remain provably absent when their direct lookup times out."""

from __future__ import annotations

import httpx
import pytest

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import FailureReason
from tests.fly_support import FakeFlyAPI, FakeSecretManager, fly_driver, fly_job


class MissingAppTimeoutAPI(FakeFlyAPI):
    """Model the live API's timeout only after an app has been deleted."""

    fail_missing_app = False

    def _app_request(
        self, request: httpx.Request, segments: list[str]
    ) -> httpx.Response:
        """Keep the complete org inventory available while absent lookups fail."""
        if (
            self.fail_missing_app
            and request.method == "GET"
            and len(segments) == 1
            and segments[0] not in self.apps
        ):
            raise httpx.ReadTimeout("synthetic missing-app timeout", request=request)
        return super()._app_request(request, segments)


def test_deletion_uses_complete_org_inventory_for_absence() -> None:
    """A confirmed app DELETE needs no hanging lookup, including on replay."""
    api = MissingAppTimeoutAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)
    api.fail_missing_app = True

    outcome = driver.delete(job, allocation.allocation_id)

    assert api.apps == {}
    assert secrets.revoked == {job.activation_id}
    assert driver.delete(job, allocation.allocation_id) == outcome


def test_fleet_discovery_does_not_query_absent_known_or_injected_apps() -> None:
    """Pending deletion cannot kill fleet health through a missing app lookup."""
    api = MissingAppTimeoutAPI()
    driver = fly_driver(api, discover_organization_apps=True)
    job = fly_job()
    driver.provision(job)
    app = next(iter(api.apps))
    api.apps.clear()
    api.requests.clear()
    api.fail_missing_app = True

    assert driver.list_resources([job.activation_id], app_names=[app]) == ()
    assert api.requests == [("GET", "/v1/apps")]


@pytest.mark.parametrize(
    "body",
    [
        '{"total_apps":1,"apps":[]}',
        '{"total_apps":0,"apps":[{}]}',
        '{"total_apps":1,"apps":[{"name":""}]}',
        '{"total_apps":1,"apps":[{"name":" invalid "}]}',
        '{"total_apps":2,"apps":[{"name":"same"},{"name":"same"}]}',
    ],
)
def test_invalid_org_inventory_cannot_confirm_deletion(body: str) -> None:
    """Incomplete or malformed inventory never proves that resources are gone."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    api.malformed_once("GET", "/v1/apps", body)

    with pytest.raises(ProviderError) as raised:
        driver.delete(fly_job(), None)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable


def test_unavailable_org_inventory_cannot_confirm_deletion() -> None:
    """An inventory outage remains retryable even if the app may be absent."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    api.fail_once("GET", "/v1/apps", status=503)

    with pytest.raises(ProviderError) as raised:
        driver.delete(fly_job(), None)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable


def test_accepted_delete_cannot_confirm_an_app_still_in_inventory() -> None:
    """The provider must actually remove the app, not merely accept DELETE."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    app = next(iter(api.apps))
    api.fail_once("DELETE", f"/apps/{app}", status=202)

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, allocation.allocation_id)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert app in api.apps
