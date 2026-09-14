"""Ownership-bound public routing to private managed vaults (#1807)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from creek_mcp.httpapi.routing import AllocationRouter, build_routing_app
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import (
    CustodyMode,
    FailureReason,
    RoutableAllocation,
)
from creek_mcp.provisioning.routing import PrivateVaultTarget, RoutingPrincipal
from creek_mcp.provisioning.store import ProvisioningStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

_TOKEN = "managed-vault-routing-token-" + "a" * 32
_OTHER_TOKEN = "managed-vault-routing-token-" + "b" * 32
_HEADERS = {
    "Authorization": f"Bearer {_TOKEN}",
    "X-Creek-Contract-Version": "0.16",
    "X-Creek-Tier-Ceiling": "personal",
}


@dataclass
class FakeRoutingVerifier:
    """Resolve one synthetic bearer without retaining it in response state."""

    principal: RoutingPrincipal

    async def verify_credential(self, credential: str) -> RoutingPrincipal | None:
        """Return the configured principal for the one valid credential."""
        return self.principal if credential == _TOKEN else None


class FakeRoutingProvider:
    """Record store-derived allocations and return one private provider target."""

    def __init__(self) -> None:
        self.calls: list[RoutableAllocation] = []
        self.before_return: Callable[[], None] | None = None
        self.failure: ProviderError | None = None

    def prepare_route(self, allocation: RoutableAllocation) -> PrivateVaultTarget:
        """Return a private target after an optional deterministic race hook."""
        self.calls.append(allocation)
        if self.failure is not None:
            raise self.failure
        if self.before_return is not None:
            self.before_return()
        return PrivateVaultTarget("https://machine-secret.vm.app-secret.internal:8823")


class GatedRoutingProvider(FakeRoutingProvider):
    """Hold one provider preparation so concurrency and cancellation are exact."""

    def __init__(self) -> None:
        """Create the deterministic entry and release signals."""
        super().__init__()
        self._calls_lock = threading.Lock()
        self.entered = threading.Event()
        self.second_entered = threading.Event()
        self.release = threading.Event()

    def prepare_route(self, allocation: RoutableAllocation) -> PrivateVaultTarget:
        """Wait until the test releases the one in-flight preparation."""
        with self._calls_lock:
            self.calls.append(allocation)
            call_count = len(self.calls)
        self.entered.set()
        if call_count == 2:
            self.second_entered.set()
        assert self.release.wait(3), "routing preparation was never released"
        return PrivateVaultTarget("https://machine-secret.vm.app-secret.internal:8823")


class TrackingResponseStream(httpx.AsyncByteStream):
    """Expose whether proxy completion closes the private response stream."""

    def __init__(self) -> None:
        """Start with an open two-chunk response."""
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield chunks so the proxy cannot rely on buffered content."""
        yield b'{"streamed":'
        yield b'"response-canary"}'

    async def aclose(self) -> None:
        """Record transport cleanup."""
        self.closed = True


def _ready_allocation(
    store: ProvisioningStore,
    *,
    requester: str = "adepthood",
    consumer: str = "user-001",
) -> None:
    """Create one provider-managed allocation ready for routing tests."""
    job = store.submit(
        "activation-route-001",
        consumer,
        requester_identity=requester,
    )
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        "fly-provider-allocation-canary",
        handoff=lambda: None,
        custody_mode=CustodyMode.PROVIDER_MANAGED,
    )


def test_archived_artifact_only_allocation_is_not_routable(tmp_path: Path) -> None:
    """Historical ceremony rows cannot enter the provider-managed data plane."""
    database = tmp_path / "routing.sqlite3"
    store = ProvisioningStore(database)
    job = store.submit(
        "activation-legacy-route",
        "user-legacy",
        requester_identity="adepthood",
    )
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        "fly-provider-legacy-canary",
        handoff=lambda: None,
        custody_mode=CustodyMode.WRAPPED_ARTIFACT_ONLY,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET state = 'ready' WHERE job_id = ?",
            (job.job_id,),
        )

    assert store.get_routable_allocation("adepthood", "user-legacy") is None


def _client(
    tmp_path: Path,
    upstream: Callable[[httpx.Request], httpx.Response],
    *,
    max_body_bytes: int = 1024 * 1024,
) -> tuple[TestClient, ProvisioningStore, FakeRoutingProvider]:
    """Build one public router over a real ownership store and mock private wire."""
    database = tmp_path / "routing.sqlite3"
    store = ProvisioningStore(database)
    _ready_allocation(store)
    provider = FakeRoutingProvider()
    private_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    verifier = FakeRoutingVerifier(RoutingPrincipal("adepthood", "user-001"))
    app = build_routing_app(
        store,
        verifier,
        provider,
        private_client,
        max_body_bytes=max_body_bytes,
    )
    return TestClient(app), store, provider


def test_authenticated_route_forwards_only_store_owned_v1_contract(
    tmp_path: Path,
) -> None:
    """The bearer selects ownership; caller data never selects a provider target."""
    observed: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Creek-Contract-Version": "0.16.0",
            },
            content=b'{"status":"ok","synthetic":"response-body-canary"}',
            request=request,
        )

    client, _store, provider = _client(tmp_path, upstream)
    response = client.get("/v1/capabilities?probe=1", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["synthetic"] == "response-body-canary"
    assert response.headers["X-Creek-Contract-Version"] == "0.16.0"
    assert len(provider.calls) == 1
    assert provider.calls[0].requester_identity == "adepthood"
    assert provider.calls[0].consumer_identity == "user-001"
    assert observed[0].url == (
        "https://machine-secret.vm.app-secret.internal:8823/v1/capabilities?probe=1"
    )
    assert observed[0].headers["Authorization"] == f"Bearer {_TOKEN}"
    assert observed[0].headers["X-Creek-Contract-Version"] == "0.16"
    assert observed[0].headers["X-Creek-Tier-Ceiling"] == "personal"


@pytest.mark.asyncio
async def test_concurrent_and_cancelled_callers_share_one_provider_start(
    tmp_path: Path,
) -> None:
    """Cancellation never releases a duplicate start beside an in-flight one."""
    database = tmp_path / "routing.sqlite3"
    store = ProvisioningStore(database)
    _ready_allocation(store)
    allocation = store.get_routable_allocation("adepthood", "user-001")
    assert allocation is not None
    provider = GatedRoutingProvider()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request)
        )
    ) as private_client:
        router = AllocationRouter(store, provider, private_client)
        cancelled = asyncio.create_task(router._prepare(allocation))
        entered = await asyncio.to_thread(provider.entered.wait, 3)
        assert entered, "routing preparation never started"
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        survivor = asyncio.create_task(router._prepare(allocation))
        await asyncio.sleep(0)
        provider.release.set()
        target = await survivor

    assert target == PrivateVaultTarget(
        "https://machine-secret.vm.app-secret.internal:8823"
    )
    assert provider.calls == [allocation]
    assert provider.second_entered.is_set() is False


@pytest.mark.asyncio
async def test_recreated_owner_pair_never_reuses_prior_generation_preparation(
    tmp_path: Path,
) -> None:
    """Distinct durable allocations cannot share an old in-flight provider call."""
    old = RoutableAllocation(
        "old-job",
        "old-activation",
        "adepthood",
        "user-001",
        "old-provider-allocation",
    )
    replacement = RoutableAllocation(
        "replacement-job",
        "replacement-activation",
        "adepthood",
        "user-001",
        "replacement-provider-allocation",
    )
    store = ProvisioningStore(tmp_path / "routing.sqlite3")
    provider = GatedRoutingProvider()
    async with httpx.AsyncClient() as private_client:
        router = AllocationRouter(store, provider, private_client)
        old_route = asyncio.create_task(router._prepare(old))
        entered = await asyncio.to_thread(provider.entered.wait, 3)
        assert entered, "old routing preparation never started"
        replacement_route = asyncio.create_task(router._prepare(replacement))
        both_entered = await asyncio.to_thread(provider.second_entered.wait, 3)
        assert both_entered, "replacement reused the old preparation"
        provider.release.set()
        await asyncio.gather(old_route, replacement_route)

    assert provider.calls == [old, replacement]


def test_proxy_streams_bodies_closes_upstream_and_strips_connection_options(
    tmp_path: Path,
) -> None:
    """End-to-end bodies survive without forwarding hop-scoped assertions."""
    stream = TrackingResponseStream()
    observed: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Connection": "X-Upstream-Secret",
                "X-Upstream-Secret": "private-response-hop",
            },
            stream=stream,
            request=request,
        )

    client, _store, _provider = _client(tmp_path, upstream)
    response = client.put(
        "/v1/voice-drafts/body-stream-canary",
        headers={
            **_HEADERS,
            "Connection": "X-Route-Secret",
            "X-Route-Secret": "private-request-hop",
            "Content-Type": "application/json",
        },
        content=b'{"content":"request-body-canary","tier":"open"}',
    )

    assert response.status_code == 200
    assert response.json() == {"streamed": "response-canary"}
    assert observed[0].content == b'{"content":"request-body-canary","tier":"open"}'
    assert "X-Route-Secret" not in observed[0].headers
    assert "X-Upstream-Secret" not in response.headers
    assert stream.closed is True


def test_unpublished_upstream_status_cannot_leak_a_private_redirect(
    tmp_path: Path,
) -> None:
    """A private runtime contract fault becomes one content-free public refusal."""
    client, _store, _provider = _client(
        tmp_path,
        lambda request: httpx.Response(
            307,
            headers={"Location": "https://machine-secret.vm.app.internal:8823/v1"},
            request=request,
        ),
    )

    response = client.get("/v1/capabilities", headers=_HEADERS)

    assert response.status_code == 503
    assert response.json()["code"] == "temporarily_unavailable"
    assert "machine-secret" not in response.text
    assert "Location" not in response.headers


def test_unknown_credential_and_unpublished_route_never_touch_provider(
    tmp_path: Path,
) -> None:
    """Authentication and the published route allowlist both precede wake-up."""
    client, _store, provider = _client(
        tmp_path,
        lambda request: httpx.Response(500, request=request),
    )

    unknown = client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {_OTHER_TOKEN}"},
    )
    missing = client.get("/v1/not-published", headers=_HEADERS)
    implicit_head = client.head("/v1/capabilities", headers=_HEADERS)

    assert unknown.status_code == 401
    assert unknown.json()["code"] == "unauthenticated"
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"
    assert implicit_head.status_code == 404
    assert provider.calls == []


def test_stale_and_cross_owner_allocations_are_indistinguishable(
    tmp_path: Path,
) -> None:
    """A valid credential cannot use or enumerate a non-routable allocation."""
    database = tmp_path / "routing.sqlite3"
    store = ProvisioningStore(database)
    _ready_allocation(store, requester="another-service")
    provider = FakeRoutingProvider()
    private_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )
    verifier = FakeRoutingVerifier(RoutingPrincipal("adepthood", "user-001"))
    client = TestClient(build_routing_app(store, verifier, provider, private_client))

    response = client.get("/v1/capabilities", headers=_HEADERS)

    assert response.status_code == 403
    assert response.json()["code"] == "privacy_refused"
    assert provider.calls == []


def test_deletion_race_revalidates_ownership_before_private_dial(
    tmp_path: Path,
) -> None:
    """A deletion begun during wake-up fences the private request."""
    dialled = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal dialled
        dialled = True
        return httpx.Response(200, request=request)

    client, store, provider = _client(tmp_path, upstream)
    job_id = provider_job_id(store)
    provider.before_return = lambda: store.request_delete(job_id, "adepthood")

    response = client.get("/v1/capabilities", headers=_HEADERS)

    assert response.status_code == 403
    assert response.json()["code"] == "privacy_refused"
    assert dialled is False


def provider_job_id(store: ProvisioningStore) -> str:
    """Return the only synthetic job id without exposing an operator listing in app."""
    jobs = store.list_fleet_jobs()
    assert len(jobs) == 1
    return jobs[0].job.job_id


@pytest.mark.parametrize(
    "failure",
    [
        ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True),
        ProviderError(
            FailureReason.PROVIDER_REJECTED,
            retryable=False,
            private_detail="machine-secret.vm.app-secret.internal",
        ),
    ],
)
def test_start_and_readiness_failures_are_bounded_secret_free_refusals(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: ProviderError,
) -> None:
    """Provider failures reveal neither topology nor the caller's credential."""
    client, _store, provider = _client(
        tmp_path,
        lambda request: httpx.Response(500, request=request),
    )
    provider.failure = failure

    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/capabilities", headers=_HEADERS)

    assert response.status_code == 503
    assert response.json()["code"] == "temporarily_unavailable"
    rendered = response.text + caplog.text
    assert _TOKEN not in rendered
    assert "machine-secret" not in rendered


def test_request_body_limit_is_enforced_before_private_dial(tmp_path: Path) -> None:
    """The public hop cannot buffer more than the published route cap."""
    client, _store, provider = _client(
        tmp_path,
        lambda request: httpx.Response(500, request=request),
        max_body_bytes=32,
    )
    response = client.post(
        "/v1/reflections",
        headers=_HEADERS,
        content=b"x" * 33,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert provider.calls == []


def test_routing_access_log_contains_no_target_credential_or_body(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The routing log is content-free and identifies only the service requester."""

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"secret": "response-body-canary"}).encode(),
            request=request,
        )

    client, _store, _provider = _client(tmp_path, upstream)
    with caplog.at_level(logging.INFO, logger="creek_mcp.httpapi.access"):
        response = client.get("/v1/capabilities", headers=_HEADERS)

    assert response.status_code == 200
    assert "consumer=adepthood" in caplog.text
    for secret in (
        _TOKEN,
        "user-001",
        "machine-secret",
        "response-body-canary",
    ):
        assert secret not in caplog.text


def test_private_target_type_refuses_public_or_caller_shaped_destinations() -> None:
    """Only HTTPS Fly-private hostnames can cross the final dial boundary."""
    for url in (
        "http://machine.vm.app.internal:8823",
        "https://127.0.0.1:8823",
        "https://example.com",
        "https://arbitrary.internal:8823",
        "https://machine.vm.app.internal:8823/v1",
        "https://user@machine.vm.app.internal:8823",
    ):
        with pytest.raises(ValueError, match="private HTTPS origin"):
            PrivateVaultTarget(url)
