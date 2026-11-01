"""The vault's private owed-alert listener, and that no public route reaches it.

#2049: the control plane pulls owed vault alerts from a two-route listener
served on the Machine's 6PN address.  It must demand exactly what a replayed
request carries - the allocation's replay state and the consumer bearer - and
neither the edge's routers nor the vault's published ``/v1`` app may serve it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from creek.vault_alerts import (
    SCHEMA,
    VAULT_ALERTS_ACK_PATH,
    VAULT_ALERTS_PATH,
    Acknowledgement,
    VaultAlertOutbox,
)
from creek_mcp.httpapi.routing import build_fly_replay_routing_app, build_routing_app
from creek_mcp.httpapi.vault_alerts import build_vault_alerts_app
from creek_mcp.provisioning.models import CustodyMode
from creek_mcp.provisioning.routing import (
    FlyReplayTarget,
    PrivateVaultTarget,
    RoutingPrincipal,
)
from creek_mcp.provisioning.store import ProvisioningStore
from tests.v1_api_support import STRONG_TOKEN, client, headers, seed_vault, verifier

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.models import RoutableAllocation

_STATE = "s" * 64
_EPOCH = "e" * 32
_SPEND = "monthly_budget_departure"
_EDGE_TOKEN = "managed-vault-routing-token-" + "a" * 32


def _alerts_client(vault: Path) -> tuple[TestClient, VaultAlertOutbox]:
    """Return a client over the listener and the outbox it serves."""
    outbox = VaultAlertOutbox(vault, epoch_factory=lambda: _EPOCH)
    app = build_vault_alerts_app(
        outbox=outbox, verifier=verifier(), fly_replay_state=_STATE
    )
    return TestClient(app), outbox


def _private_headers(
    *, state: str = _STATE, token: str = STRONG_TOKEN
) -> dict[str, str]:
    """Return the two secrets the control plane presents."""
    return {"Fly-Replay-Src": f"state={state}", "Authorization": f"Bearer {token}"}


def test_the_summary_is_the_closed_counts_only_document(tmp_path: Path) -> None:
    """An authenticated read returns the outbox, or the bare schema when empty."""
    app_client, outbox = _alerts_client(tmp_path)

    empty = app_client.get(VAULT_ALERTS_PATH, headers=_private_headers())
    outbox.owe(_SPEND)
    owed = app_client.get(VAULT_ALERTS_PATH, headers=_private_headers())

    assert empty.status_code == 200
    assert empty.json() == {"schema": SCHEMA}
    assert owed.json() == {
        "schema": SCHEMA,
        "epoch": _EPOCH,
        "raised": {_SPEND: 1},
        "acknowledged": {},
    }


@pytest.mark.parametrize(
    "presented",
    [
        {},
        {"Authorization": f"Bearer {STRONG_TOKEN}"},
        {"Fly-Replay-Src": f"state={_STATE}"},
        _private_headers(state="t" * 64),
        _private_headers(token="unit-test-unknown-token-" + "c" * 20),
    ],
)
def test_both_the_replay_state_and_the_bearer_are_required(
    tmp_path: Path, presented: dict[str, str]
) -> None:
    """Either secret alone is a published 401 that names nothing."""
    app_client, outbox = _alerts_client(tmp_path)
    outbox.owe(_SPEND)

    read = app_client.get(VAULT_ALERTS_PATH, headers=presented)
    ack = app_client.post(
        VAULT_ALERTS_ACK_PATH,
        headers=presented,
        json=Acknowledgement(_EPOCH, {_SPEND: 1}).to_document(),
    )

    assert read.status_code == 401
    assert ack.status_code == 401
    assert _SPEND not in read.text
    state = outbox.summary()
    assert state is not None
    assert state.owed == {_SPEND: 1}


def test_an_acknowledgement_clears_what_it_names(tmp_path: Path) -> None:
    """The ack answers the outbox after it: nothing left owed."""
    app_client, outbox = _alerts_client(tmp_path)
    outbox.owe(_SPEND)

    response = app_client.post(
        VAULT_ALERTS_ACK_PATH,
        headers=_private_headers(),
        json=Acknowledgement(_EPOCH, {_SPEND: 1}).to_document(),
    )

    assert response.status_code == 200
    assert response.json()["acknowledged"] == {_SPEND: 1}
    state = outbox.summary()
    assert state is not None
    assert state.owed == {}


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"schema": "creek_vault_alerts_v1", "epoch": "x", "through": {}}',
        b"[" * 5000,
        b'{"schema": "creek_vault_alerts_v1", "through": {"note": "free text"}}',
    ],
)
def test_a_malformed_or_oversized_acknowledgement_changes_nothing(
    tmp_path: Path, body: bytes
) -> None:
    """Anything outside the closed shape, or over the cap, is refused."""
    app_client, outbox = _alerts_client(tmp_path)
    outbox.owe(_SPEND)

    response = app_client.post(
        VAULT_ALERTS_ACK_PATH,
        headers={**_private_headers(), "Content-Type": "application/json"},
        content=body,
    )

    assert 400 <= response.status_code < 500
    state = outbox.summary()
    assert state is not None
    assert state.owed == {_SPEND: 1}


def test_an_unreadable_outbox_is_a_published_refusal(tmp_path: Path) -> None:
    """A damaged document answers 503 on both routes, never a reset."""
    app_client, outbox = _alerts_client(tmp_path)
    outbox.owe(_SPEND)
    path = tmp_path / "00-Creek-Meta" / "alerts" / "owed-alerts.json"
    path.write_text("damaged", encoding="utf-8")

    read = app_client.get(VAULT_ALERTS_PATH, headers=_private_headers())
    ack = app_client.post(
        VAULT_ALERTS_ACK_PATH,
        headers=_private_headers(),
        json=Acknowledgement(_EPOCH, {_SPEND: 1}).to_document(),
    )

    assert read.status_code == 503
    assert read.json()["code"] == "unavailable"
    assert ack.status_code == 503
    assert path.read_text(encoding="utf-8") == "damaged"


def test_a_held_lock_is_a_transient_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that cannot be taken is retryable, not a damaged outbox."""
    app_client, outbox = _alerts_client(tmp_path)

    def locked() -> None:
        raise TimeoutError

    monkeypatch.setattr(outbox, "summary", locked)
    response = app_client.get(VAULT_ALERTS_PATH, headers=_private_headers())

    assert response.status_code == 503
    assert response.json()["code"] == "temporarily_unavailable"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", VAULT_ALERTS_PATH),
        ("GET", VAULT_ALERTS_ACK_PATH),
        ("GET", "/v1/health"),
        ("GET", f"{VAULT_ALERTS_PATH}/"),
    ],
)
def test_the_listener_serves_its_two_routes_and_nothing_else(
    tmp_path: Path, method: str, path: str
) -> None:
    """A wrong verb, a stray slash or a ``/v1`` path is the published routing miss."""
    app_client, _ = _alerts_client(tmp_path)

    response = app_client.request(method, path, headers=_private_headers())

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_the_published_vault_app_does_not_serve_the_alert_routes(
    tmp_path: Path,
) -> None:
    """The replayed ``/v1`` listener answers the alert paths as a routing miss."""
    app_client = client(vault_path=seed_vault(tmp_path), fly_replay_state=_STATE)
    replayed = {**headers(), "Fly-Replay-Src": f"state={_STATE}"}

    read = app_client.get(VAULT_ALERTS_PATH, headers=replayed)
    ack = app_client.post(VAULT_ALERTS_ACK_PATH, headers=replayed, json={})

    assert read.status_code == 404
    assert ack.status_code == 404


@dataclass
class _EdgeVerifier:
    """Resolve the one synthetic routing credential."""

    async def verify_credential(self, credential: str) -> RoutingPrincipal | None:
        """Return the owner for the valid credential only."""
        if credential != _EDGE_TOKEN:
            return None
        return RoutingPrincipal("adepthood", "user-001", _STATE)


class _CountingProvider:
    """Count every wake the edge asks for; the alert paths must cause none."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls = 0

    def prepare_route(self, allocation: RoutableAllocation) -> PrivateVaultTarget:
        """Record a private-route wake."""
        del allocation
        self.calls += 1
        return PrivateVaultTarget("https://machine.vm.app.internal:8823")

    def prepare_replay(self, allocation: RoutableAllocation) -> FlyReplayTarget:
        """Record a replay wake."""
        del allocation
        self.calls += 1
        return FlyReplayTarget("creek-vault-app", "machine-001")


def _ready_store(tmp_path: Path) -> ProvisioningStore:
    """Return a store holding one routable allocation the edge could wake."""
    store = ProvisioningStore(tmp_path / "edge.sqlite3")
    job = store.submit(
        "activation-edge-001", "user-001", requester_identity="adepthood"
    )
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        "fly-0123456789abcdef01234567",
        handoff=lambda: None,
        custody_mode=CustodyMode.PROVIDER_MANAGED,
    )
    return store


@pytest.mark.parametrize("router", ["replay", "private"])
@pytest.mark.parametrize(
    ("method", "path"), [("GET", VAULT_ALERTS_PATH), ("POST", VAULT_ALERTS_ACK_PATH)]
)
def test_the_public_edge_cannot_reach_the_alert_routes(
    tmp_path: Path, router: str, method: str, path: str
) -> None:
    """With a valid routing credential the edge still routes no alert path.

    Neither router wakes a vault or dials one: the paths are not published.
    """
    store = _ready_store(tmp_path)
    provider = _CountingProvider()
    dialed: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        dialed.append(request)
        return httpx.Response(200, json={"schema": SCHEMA})

    if router == "replay":
        app = build_fly_replay_routing_app(store, _EdgeVerifier(), provider)
    else:
        app = build_routing_app(
            store,
            _EdgeVerifier(),
            provider,
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
    response = TestClient(app).request(
        method,
        path,
        headers={
            "Authorization": f"Bearer {_EDGE_TOKEN}",
            "Fly-Replay-Src": f"state={_STATE}",
        },
        json=Acknowledgement(_EPOCH, {_SPEND: 1}).to_document(),
    )

    assert response.status_code == 404
    assert "fly-replay" not in {name.lower() for name in response.headers}
    assert provider.calls == 0
    assert dialed == []
