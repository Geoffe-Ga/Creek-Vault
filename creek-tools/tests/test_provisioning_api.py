"""Authenticated, secret-free asynchronous control-plane API tests (#1768)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from creek_mcp.httpapi.provisioning import build_provisioning_app
from creek_mcp.provisioning.api import CONTRACT_VERSION
from creek_mcp.provisioning.models import FailureReason, JobState
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.remote_auth import ConsumerTokenVerifier

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import Response

_TOKEN = "api-test-consumer-token-" + "a" * 32
_OTHER_TOKEN = "api-test-other-token-" + "b" * 32
_UNKNOWN_TOKEN = "api-test-unknown-token-" + "c" * 32


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return a real durable store for API tests."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


@pytest.fixture
def app_client(store: ProvisioningStore) -> TestClient:
    """Return an authenticated control-plane test client."""
    verifier = ConsumerTokenVerifier(
        {"adepthood": (_TOKEN,), "other-consumer": (_OTHER_TOKEN,)}
    )
    return TestClient(build_provisioning_app(store, verifier))


def _headers(token: str = _TOKEN) -> dict[str, str]:
    """Return the one accepted bearer spelling."""
    return {"Authorization": f"Bearer {token}"}


def _submit(
    client: TestClient,
    activation_id: str,
    *,
    consumer_identity: str = "adepthood",
    token: str = _TOKEN,
) -> Response:
    """Submit one activation request through the public API."""
    return client.post(
        "/control/v1/activations",
        headers=_headers(token),
        json={
            "activation_id": activation_id,
            "consumer_identity": consumer_identity,
        },
    )


def test_submit_is_immediate_idempotent_and_contains_no_provider_result(
    app_client: TestClient,
    store: ProvisioningStore,
) -> None:
    """The request commits only durable queue state and returns one safe handle."""
    first = _submit(app_client, "activation-api")
    second = _submit(app_client, "activation-api")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == first.json()
    assert first.headers["Creek-Provisioning-Version"] == CONTRACT_VERSION
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["state"] == JobState.PENDING.value
    assert store.count_jobs() == 1
    serialized = first.text.lower()
    assert "credential" not in serialized
    assert "provider" not in serialized
    assert "vault_url" not in serialized


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/control/v1/activations"),
        ("GET", "/control/v1/jobs/not-a-job"),
        ("DELETE", "/control/v1/nonsense"),
        ("GET", "/"),
    ],
)
def test_missing_and_unknown_credentials_fail_before_route_disclosure(
    app_client: TestClient,
    method: str,
    path: str,
) -> None:
    """No anonymous probe learns the control-plane route table or version."""
    absent = app_client.request(method, path)
    unknown = app_client.request(method, path, headers=_headers(_UNKNOWN_TOKEN))

    assert absent.status_code == 401
    assert unknown.status_code == 401
    assert absent.json()["code"] == "unauthenticated"
    assert unknown.json()["code"] == "unauthenticated"
    assert "Creek-Provisioning-Version" not in absent.headers
    assert "Creek-Provisioning-Version" not in unknown.headers


def test_authenticated_consumer_identity_must_match_the_request(
    app_client: TestClient,
) -> None:
    """A bearer cannot ask the control plane to allocate for another identity."""
    response = _submit(
        app_client,
        "activation-wrong-consumer",
        consumer_identity="other-consumer",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "consumer_mismatch"


@pytest.mark.parametrize(
    ("activation_id", "consumer_identity"),
    [("   ", "adepthood"), ("activation-whitespace", "   ")],
)
def test_whitespace_only_identifiers_are_bounded_request_errors(
    app_client: TestClient,
    store: ProvisioningStore,
    activation_id: str,
    consumer_identity: str,
) -> None:
    """Blank identifiers are rejected at the wire boundary, never as server faults."""
    response = app_client.post(
        "/control/v1/activations",
        headers=_headers(),
        json={
            "activation_id": activation_id,
            "consumer_identity": consumer_identity,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert store.count_jobs() == 0


def test_job_status_is_scoped_to_the_authenticated_consumer(
    app_client: TestClient,
) -> None:
    """A valid bearer cannot enumerate another consumer's job handles."""
    created = _submit(app_client, "activation-status").json()

    own = app_client.get(created["status_url"], headers=_headers())
    other = app_client.get(created["status_url"], headers=_headers(_OTHER_TOKEN))

    assert own.status_code == 200
    assert own.json()["job_id"] == created["job_id"]
    assert other.status_code == 403
    assert other.json()["code"] == "job_unavailable"


def test_retry_and_delete_paths_apply_only_durable_transitions(
    app_client: TestClient,
    store: ProvisioningStore,
) -> None:
    """Retry/delete mutate queue state without invoking any provider adapter."""
    created = _submit(app_client, "activation-transitions").json()
    claimed = store.claim_next()
    assert claimed is not None
    store.record_failure(
        claimed.job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
    )

    retried = app_client.post(
        f"/control/v1/jobs/{created['job_id']}/retry",
        headers=_headers(),
    )
    deleted = app_client.delete(created["status_url"], headers=_headers())
    deleted_again = app_client.delete(created["status_url"], headers=_headers())

    assert retried.status_code == 202
    assert retried.json()["state"] == JobState.PENDING.value
    assert deleted.status_code == 202
    assert deleted.json()["state"] == JobState.DELETING.value
    assert deleted_again.json() == deleted.json()


def test_concurrent_http_activations_share_one_job(
    store: ProvisioningStore,
) -> None:
    """The authenticated API preserves the database's one-allocation invariant."""
    verifier = ConsumerTokenVerifier({"adepthood": (_TOKEN,)})
    app = build_provisioning_app(store, verifier)

    def submit(number: int) -> str:
        with TestClient(app) as client:
            response = _submit(client, f"activation-http-{number}")
        assert response.status_code == 202
        return str(response.json()["job_id"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        job_ids = set(executor.map(submit, range(16)))

    assert len(job_ids) == 1
    assert store.count_jobs() == 1
    assert store.count_activation_ids() == 16


def test_invalid_payload_and_invalid_retry_return_stable_errors(
    app_client: TestClient,
) -> None:
    """Client mistakes receive bounded machine-readable reasons, never tracebacks."""
    malformed = app_client.post(
        "/control/v1/activations",
        headers=_headers(),
        content=b"not-json",
    )
    created = _submit(app_client, "activation-not-failed").json()
    invalid_retry = app_client.post(
        f"/control/v1/jobs/{created['job_id']}/retry",
        headers=_headers(),
    )

    assert malformed.status_code == 400
    assert malformed.json()["code"] == "invalid_request"
    assert invalid_retry.status_code == 409
    assert invalid_retry.json()["code"] == "invalid_transition"


def test_status_retry_and_delete_are_each_safe_under_concurrency(
    store: ProvisioningStore,
) -> None:
    """Every non-create path has a deterministic concurrent outcome."""
    verifier = ConsumerTokenVerifier({"adepthood": (_TOKEN,)})
    app = build_provisioning_app(store, verifier)
    job = store.submit("activation-all-paths", "adepthood")

    def request(method: str, path: str) -> tuple[int, str]:
        with TestClient(app) as client:
            response = client.request(method, path, headers=_headers())
        return response.status_code, str(response.json().get("state"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        status_results = list(
            executor.map(
                lambda _: request("GET", f"/control/v1/jobs/{job.job_id}"),
                range(16),
            )
        )
    assert set(status_results) == {(200, JobState.PENDING.value)}

    claimed = store.claim_next()
    assert claimed is not None
    store.record_failure(
        claimed.job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        retry_results = list(
            executor.map(
                lambda _: request(
                    "POST",
                    f"/control/v1/jobs/{job.job_id}/retry",
                ),
                range(16),
            )
        )
    assert set(retry_results) == {(202, JobState.PENDING.value)}

    with ThreadPoolExecutor(max_workers=8) as executor:
        delete_results = list(
            executor.map(
                lambda _: request("DELETE", f"/control/v1/jobs/{job.job_id}"),
                range(16),
            )
        )
    assert set(delete_results) == {(202, JobState.DELETING.value)}
