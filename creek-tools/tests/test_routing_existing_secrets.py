"""Production routing must attest existing secrets without issuance authority."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from creek_mcp.provisioning import routing_cli
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import FlyProviderDriver, FlyProviderPolicy
from creek_mcp.provisioning.models import CustodyMode
from creek_mcp.provisioning.production_secrets import ReadOnlyFlySecretManager
from creek_mcp.provisioning.store import ProvisioningStore
from tests.fly_support import FakeFlyAPI
from tests.test_provisioning_production_adapters import _secret_manager
from tests.test_provisioning_routing_cli import _NOW, _arguments

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("replay", [False, True])
def test_production_router_wakes_an_existing_allocation(
    tmp_path: Path, replay: bool
) -> None:
    """Use actual composition and encrypted bundles across the cold-start boundary."""
    issuer = _secret_manager(tmp_path)
    # Argument construction has its own filesystem fixture.
    arguments_root = tmp_path / "arguments"
    arguments_root.mkdir()
    parser = routing_cli.build_parser()
    args = parser.parse_args(_arguments(arguments_root))
    args.secret_state_directory = tmp_path / "runtime-secrets"
    args.secret_master_key_file = tmp_path / "master-key"
    api = FakeFlyAPI()
    api.provider_token = "fly-routing-token"
    transport = httpx.MockTransport(api.handle)
    bundle = (
        routing_cli.compose_replay(
            args, parser, clock=lambda: _NOW, provider_transport=transport
        )
        if replay
        else routing_cli.compose(
            args,
            parser,
            clock=lambda: _NOW,
            provider_transport=transport,
            private_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )
    )
    policy = FlyProviderPolicy(
        organization=args.fly_organization,
        image=args.fly_image,
        region=args.fly_region,
        routing_public_url="https://vault-router.example.com",
        fly_replay_enabled=replay,
    )
    with httpx.Client(base_url=args.fly_api_base_url, transport=transport) as client:
        writer = FlyProviderDriver(policy, bundle.driver._credential, issuer, client)
        store = ProvisioningStore(args.database)
        job = store.submit(
            "routing-activation", "consumer", requester_identity="requester"
        )
        created = writer.provision(job)
        claim = store.claim_next()
        assert claim is not None
        store.complete_create(
            job.job_id,
            claim.lease_token,
            created.allocation_id,
            handoff=lambda: None,
            custody_mode=CustodyMode.PROVIDER_MANAGED,
        )
        secret = issuer.issue(
            job.activation_id,
            job.consumer_identity,
            requester_identity=job.requester_identity,
        )
        before = {p.name: p.read_bytes() for p in args.secret_state_directory.iterdir()}
        try:
            with TestClient(bundle.app) as http:
                response = http.get(
                    "/v1/health",
                    headers={"Authorization": "Bearer " + secret.consumer_credential},
                )
            assert response.status_code == (307 if replay else 200)
            assert ("fly-replay" in response.headers) is replay
            assert any(
                path.endswith("/start")
                for method, path in api.requests
                if method == "POST"
            )
            assert before == {
                p.name: p.read_bytes() for p in args.secret_state_directory.iterdir()
            }
        finally:
            if isinstance(bundle, routing_cli.ReplayRoutingBundle):
                bundle.close()
            else:
                asyncio.run(bundle.aclose())


@pytest.mark.parametrize(
    "failure",
    ["missing", "revoked", "corrupt", "requester", "consumer", "activation", "symlink"],
)
def test_read_only_routing_secrets_fail_closed_without_writing(
    tmp_path: Path, failure: str
) -> None:
    """Absent, revoked, unreadable and differently owned state cannot be repaired."""
    issuer = _secret_manager(tmp_path)
    issuer.issue("activation", "consumer", requester_identity="requester")
    state = tmp_path / "runtime-secrets"
    bundle = next(state.glob("*.bundle"))
    if failure == "missing":
        bundle.unlink()
    elif failure == "revoked":
        bundle.with_suffix(".revoked").touch()
    elif failure == "corrupt":
        bundle.write_bytes(b"corrupt ciphertext")
    elif failure == "symlink":
        destination = tmp_path / "moved.bundle"
        bundle.rename(destination)
        bundle.symlink_to(destination)
    reader = ReadOnlyFlySecretManager(state, master_key_file=tmp_path / "master-key")
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    with pytest.raises(ProviderError):
        reader.issue(
            "other" if failure == "activation" else "activation",
            "other" if failure == "consumer" else "consumer",
            requester_identity="other" if failure == "requester" else "requester",
        )
    assert before == {p.name: p.read_bytes() for p in state.iterdir()}


def test_read_only_routing_adapter_cannot_revoke_or_sign(tmp_path: Path) -> None:
    """Routing still works without the CA key and refuses revocation."""
    issuer = _secret_manager(tmp_path)
    expected = issuer.issue("activation", "consumer", requester_identity="requester")
    (tmp_path / "ca.key").unlink()
    state = tmp_path / "runtime-secrets"
    reader = ReadOnlyFlySecretManager(state, master_key_file=tmp_path / "master-key")
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    with pytest.raises(ProviderError):
        reader.revoke("activation")
    assert (
        reader.issue("activation", "consumer", requester_identity="requester")
        == expected
    )
    assert before == {p.name: p.read_bytes() for p in state.iterdir()}
