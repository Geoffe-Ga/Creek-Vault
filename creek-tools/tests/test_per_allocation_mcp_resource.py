"""A per-allocation MCP resource URL for every new managed vault (#2002, R3-1).

Owner decision R3-1 requires each managed vault to verify a token audience of
its own.  The control plane mints one random, opaque slug per allocation at
create, persists it in the activation's encrypted record as the resource URL
``https://<edge>/a/<slug>/mcp``, writes that as the Machine's
``CREEK_MCP_RESOURCE_URL``, and the vault's ``agent_mcp`` handshake reports it.

The edge does not route ``/a/<slug>/mcp`` until #2003, so the behavior sits
behind ``--per-allocation-mcp-resource`` (off by default): with it off every new
allocation keeps the shared edge resource, exactly as before.
"""

from __future__ import annotations

import base64
from string import ascii_letters, digits
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from starlette.testclient import TestClient

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek_mcp.agent_tokens import JwksCache, JwtAgentTokenVerifier
from creek_mcp.api.models import AgentMcpDiscovery
from creek_mcp.mcp_path import (
    ALLOCATION_SLUG_CHARS,
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    PROTECTED_RESOURCE_ROOT,
    allocation_mcp_path,
    is_resource_path,
)
from creek_mcp.oauth_resource import (
    RESOURCE_URL_ENV,
    ProtectedResourceMetadataMiddleware,
    ResourceServerConfig,
    load_resource_server_config,
)
from creek_mcp.provisioning import pilot_cli, worker_cli
from creek_mcp.provisioning.models import (
    ALLOCATION_SLUG_BYTES,
    McpOAuthSettings,
    RoutableAllocation,
    allocation_mcp_resource_url,
    mcp_resource_url,
    new_allocation_slug,
    public_vault_url,
)
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    ReadOnlyFlySecretManager,
)
from tests.agent_token_support import StaticJwks
from tests.fly_support import (
    MCP_ISSUER,
    MCP_RESOURCE_URL,
    ROUTING_PUBLIC_URL,
    FakeFlyAPI,
    fly_driver,
    fly_job,
    mcp_oauth_settings,
)
from tests.test_provisioning_production_adapters import _NOW, _ca_files, _owner_file
from tests.v1_api_support import CAPABILITIES_PATH, build_app, headers, seed_vault

if TYPE_CHECKING:
    from pathlib import Path

_SLUG: Final[str] = "Xk3" + "q" * (ALLOCATION_SLUG_CHARS - 3)
_REQUESTER: Final[str] = "adepthood"
_CONSUMER: Final[str] = "adepthood-user-001"
_ACTIVATION: Final[str] = "activation-fly-001"
_OTHER_ACTIVATION: Final[str] = "activation-fly-002"
_OTHER_CONSUMER: Final[str] = "adepthood-user-002"
_REPLAY_STATE: Final[str] = "r" * 64
_URL_SAFE: Final[frozenset[str]] = frozenset(f"{ascii_letters}{digits}-_")
_MIN_SLUG_BITS: Final[int] = 128
_SAMPLES: Final[int] = 256


def _slug_of(resource_url: str) -> str:
    """Return the slug segment of a per-allocation resource URL."""
    prefix = f"{ROUTING_PUBLIC_URL}/a/"
    assert resource_url.startswith(prefix)
    assert resource_url.endswith(MCP_PATH)
    return resource_url.removeprefix(prefix).removesuffix(MCP_PATH)


# --------------------------------------------------------------------------- #
# The path grammar every boundary shares
# --------------------------------------------------------------------------- #


def test_an_allocation_path_puts_the_slug_between_the_root_and_mcp() -> None:
    """``/a/<slug>/mcp`` is the one per-allocation resource path."""
    assert allocation_mcp_path(_SLUG) == f"/a/{_SLUG}{MCP_PATH}"
    assert is_resource_path(allocation_mcp_path(_SLUG))
    assert is_resource_path(MCP_PATH)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "q" * (ALLOCATION_SLUG_CHARS - 1),
        "q" * (ALLOCATION_SLUG_CHARS + 1),
        "q" * (ALLOCATION_SLUG_CHARS - 1) + "/",
        "q" * (ALLOCATION_SLUG_CHARS - 1) + ".",
        "q" * (ALLOCATION_SLUG_CHARS - 1) + "=",
        "q" * (ALLOCATION_SLUG_CHARS - 3) + "%2F",
        "q" * (ALLOCATION_SLUG_CHARS - 1) + "é",
    ],
)
def test_a_slug_outside_the_minted_grammar_is_refused(slug: str) -> None:
    """Only the exact URL-safe shape the minter produces names an allocation."""
    with pytest.raises(ValueError, match=r"^allocation slug is invalid$"):
        allocation_mcp_path(slug)


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "/",
        "/mcpx",
        "/mcp/",
        "/a/mcp",
        "/a//mcp",
        f"/a/{_SLUG}/mcp/",
        f"/a/{_SLUG}/v1",
        f"/b/{_SLUG}/mcp",
        f"/a/{_SLUG}/a/{_SLUG}/mcp",
    ],
)
def test_no_other_path_is_a_resource_path(path: object) -> None:
    """Lookalikes, subtrees and nested scopes are not resource paths."""
    assert not is_resource_path(path)


# --------------------------------------------------------------------------- #
# Minting the slug
# --------------------------------------------------------------------------- #


def test_a_minted_slug_is_random_url_safe_and_at_least_128_bits() -> None:
    """Every slug is fresh CSPRNG output in the URL-safe base64 alphabet."""
    slugs = {new_allocation_slug() for _ in range(_SAMPLES)}

    assert len(slugs) == _SAMPLES
    assert ALLOCATION_SLUG_BYTES * 8 >= _MIN_SLUG_BITS
    for slug in slugs:
        assert len(slug) == ALLOCATION_SLUG_CHARS
        assert set(slug) <= _URL_SAFE
        padded = slug + "=" * (-len(slug) % 4)
        assert len(base64.urlsafe_b64decode(padded)) == ALLOCATION_SLUG_BYTES
        assert is_resource_path(allocation_mcp_path(slug))


def test_a_minted_slug_decodes_to_random_bytes_not_text() -> None:
    """A slug is not an encoding of any identity string."""
    for _ in range(_SAMPLES):
        raw = base64.urlsafe_b64decode(new_allocation_slug())
        assert _REQUESTER.encode() not in raw
        assert _CONSUMER.encode() not in raw


# --------------------------------------------------------------------------- #
# The per-allocation resource and its settings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("origin", [ROUTING_PUBLIC_URL, f"{ROUTING_PUBLIC_URL}/"])
def test_an_allocation_resource_is_the_edge_origin_plus_its_path(origin: str) -> None:
    """The resource is the shared edge origin with the allocation's own path."""
    assert (
        allocation_mcp_resource_url(origin, _SLUG)
        == f"{ROUTING_PUBLIC_URL}/a/{_SLUG}{MCP_PATH}"
    )


def test_settings_for_an_allocation_name_its_own_resource() -> None:
    """``for_allocation`` builds verbatim, storable, repr-safe settings."""
    settings = McpOAuthSettings.for_allocation(ROUTING_PUBLIC_URL, _SLUG, MCP_ISSUER)

    assert settings.resource_url == f"{ROUTING_PUBLIC_URL}/a/{_SLUG}/mcp"
    assert settings.authorization_server == MCP_ISSUER
    assert settings.runtime_env()[RESOURCE_URL_ENV] == settings.resource_url
    assert McpOAuthSettings.decode(settings.encode()) == settings
    assert _SLUG not in repr(settings)
    assert ROUTING_PUBLIC_URL not in repr(settings)


def test_settings_for_an_allocation_refuse_a_malformed_slug() -> None:
    """A slug outside the grammar never becomes a stored resource."""
    with pytest.raises(ValueError, match="allocation slug"):
        McpOAuthSettings.for_allocation(ROUTING_PUBLIC_URL, "../mcp", MCP_ISSUER)


def test_shared_settings_scope_to_a_fresh_allocation_on_the_same_origin() -> None:
    """Each scoping keeps the origin and issuer and mints a different slug."""
    shared = mcp_oauth_settings()

    first = shared.for_new_allocation()
    second = shared.for_new_allocation()

    assert first.authorization_server == second.authorization_server == MCP_ISSUER
    assert _slug_of(first.resource_url) != _slug_of(second.resource_url)
    assert shared.resource_url == MCP_RESOURCE_URL


def test_settings_already_scoped_to_an_allocation_are_not_rescoped() -> None:
    """Only the shared edge resource is a base for a per-allocation one."""
    scoped = McpOAuthSettings.for_allocation(ROUTING_PUBLIC_URL, _SLUG, MCP_ISSUER)

    with pytest.raises(ValueError, match="shared"):
        scoped.for_new_allocation()


def test_self_hosted_derivation_is_unchanged() -> None:
    """A self-hosted vault's resource is still its ``/v1`` URL's ``/mcp``."""
    settings = McpOAuthSettings.for_vault_url(
        public_vault_url(ROUTING_PUBLIC_URL), MCP_ISSUER
    )

    assert mcp_resource_url(public_vault_url(ROUTING_PUBLIC_URL)) == MCP_RESOURCE_URL
    assert settings == mcp_oauth_settings()
    config = load_resource_server_config(
        {
            RESOURCE_URL_ENV: MCP_RESOURCE_URL,
            "CREEK_MCP_AUTHORIZATION_SERVER": MCP_ISSUER,
        }
    )
    assert config is not None
    assert config.resource_url == MCP_RESOURCE_URL
    assert config.metadata_url == f"{ROUTING_PUBLIC_URL}{PROTECTED_RESOURCE_PATH}"


# --------------------------------------------------------------------------- #
# The vault accepts, publishes and advertises a per-allocation resource
# --------------------------------------------------------------------------- #


def test_a_vault_boots_with_a_per_allocation_resource() -> None:
    """The resource-server config admits the allocation path, not lookalikes."""
    resource = allocation_mcp_resource_url(ROUTING_PUBLIC_URL, _SLUG)

    config = ResourceServerConfig.build(
        resource_url=resource, authorization_server=MCP_ISSUER
    )

    assert config.resource_url == resource
    assert config.metadata_url == (
        f"{ROUTING_PUBLIC_URL}{PROTECTED_RESOURCE_ROOT}/a/{_SLUG}/mcp"
    )
    with pytest.raises(ValueError, match=RESOURCE_URL_ENV):
        ResourceServerConfig.build(
            resource_url=f"{ROUTING_PUBLIC_URL}/a/short/mcp",
            authorization_server=MCP_ISSUER,
        )


def test_a_vault_publishes_metadata_at_its_own_resources_path() -> None:
    """The metadata path a per-allocation challenge names is one it answers."""
    resource = allocation_mcp_resource_url(ROUTING_PUBLIC_URL, _SLUG)
    config = ResourceServerConfig.build(
        resource_url=resource, authorization_server=MCP_ISSUER
    )

    async def _below(scope: Any, receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # No ``with``: this bare ASGI double answers HTTP only, never lifespan.
    client = TestClient(ProtectedResourceMetadataMiddleware(_below, config=config))
    scoped = client.get(f"{PROTECTED_RESOURCE_ROOT}/a/{_SLUG}/mcp")
    root = client.get(PROTECTED_RESOURCE_ROOT)
    other = client.get(f"{PROTECTED_RESOURCE_ROOT}/a/{'z' * 32}/mcp")

    assert scoped.status_code == 200
    assert scoped.json()["resource"] == resource
    assert root.json()["resource"] == resource
    assert other.status_code == 404


def test_the_handshake_contract_admits_a_per_allocation_resource() -> None:
    """``agent_mcp.resource`` may name ``/a/<slug>/mcp`` and still nothing else."""
    resource = allocation_mcp_resource_url(ROUTING_PUBLIC_URL, _SLUG)

    discovery = AgentMcpDiscovery(resource=resource, authorization_server=MCP_ISSUER)

    assert discovery.resource == resource
    with pytest.raises(ValueError, match="/mcp"):
        AgentMcpDiscovery(
            resource=f"{ROUTING_PUBLIC_URL}/a/{_SLUG}/v1",
            authorization_server=MCP_ISSUER,
        )


# --------------------------------------------------------------------------- #
# Minting and persisting it in the encrypted per-activation record
# --------------------------------------------------------------------------- #


def _manager(
    tmp_path: Path,
    *,
    per_allocation: bool,
    mcp_oauth: McpOAuthSettings | None = None,
    state_name: str = "runtime-secrets",
) -> EncryptedFileFlySecretManager:
    """Return an encrypted manager over one state root under *tmp_path*."""
    state = tmp_path / state_name
    if not state.exists():
        state.mkdir(mode=0o700)
    if not (tmp_path / "master-key").exists():
        _owner_file(tmp_path / "master-key", b"m" * 32)
        _ca_files(tmp_path)
    return EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
        mcp_oauth=mcp_oauth_settings() if mcp_oauth is None else mcp_oauth,
        per_allocation_mcp_resource=per_allocation,
    )


def _resource(
    manager: EncryptedFileFlySecretManager | ReadOnlyFlySecretManager,
    activation: str = _ACTIVATION,
    consumer: str = _CONSUMER,
) -> str:
    """Issue or load *activation*'s record and return its stored resource."""
    issued = manager.issue(activation, consumer, requester_identity=_REQUESTER)
    assert issued.mcp_oauth is not None
    return issued.mcp_oauth.resource_url


def test_by_default_a_new_record_keeps_the_shared_resource(tmp_path: Path) -> None:
    """The flag defaults off, so nothing changes until the edge routes slugs."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    _owner_file(tmp_path / "master-key", b"m" * 32)
    _ca_files(tmp_path)
    manager = EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
        mcp_oauth=mcp_oauth_settings(),
    )

    assert _resource(manager) == MCP_RESOURCE_URL
    assert _resource(manager, _OTHER_ACTIVATION, _OTHER_CONSUMER) == MCP_RESOURCE_URL


def test_two_allocations_get_different_stable_resources(tmp_path: Path) -> None:
    """Each allocation's resource is its own, and stays put across reloads."""
    manager = _manager(tmp_path, per_allocation=True)

    first = _resource(manager)
    second = _resource(manager, _OTHER_ACTIVATION, _OTHER_CONSUMER)

    assert first != second
    assert _slug_of(first) != _slug_of(second)
    assert first == allocation_mcp_resource_url(ROUTING_PUBLIC_URL, _slug_of(first))
    restarted = _manager(tmp_path, per_allocation=True)
    reader = ReadOnlyFlySecretManager(
        tmp_path / "runtime-secrets", master_key_file=tmp_path / "master-key"
    )
    assert _resource(manager) == first
    assert _resource(restarted) == first
    assert _resource(reader) == first
    assert _resource(reader, _OTHER_ACTIVATION, _OTHER_CONSUMER) == second


def test_a_create_retry_keeps_the_slug_it_minted(tmp_path: Path) -> None:
    """A retried create re-reads the record; it never mints a second slug."""
    manager = _manager(tmp_path, per_allocation=True)
    first = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    retried = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    assert retried == first
    assert len(list((tmp_path / "runtime-secrets").glob("*.bundle"))) == 1


def test_the_slug_is_not_derived_from_any_identity(tmp_path: Path) -> None:
    """The same identities in two state roots mint unrelated slugs."""
    one = _resource(_manager(tmp_path, per_allocation=True, state_name="one"))
    two = _resource(_manager(tmp_path, per_allocation=True, state_name="two"))

    assert _slug_of(one) != _slug_of(two)
    for resource in (one, two):
        slug = _slug_of(resource)
        for identity in (_REQUESTER, _CONSUMER, _ACTIVATION):
            assert identity not in slug
            assert identity not in resource
            encoded = base64.urlsafe_b64encode(identity.encode()).decode()
            assert encoded.rstrip("=") not in slug


def test_a_record_issued_before_the_flag_keeps_its_shared_resource(
    tmp_path: Path,
) -> None:
    """Turning the flag on changes no existing allocation (that is #2004)."""
    assert _resource(_manager(tmp_path, per_allocation=False)) == MCP_RESOURCE_URL

    assert _resource(_manager(tmp_path, per_allocation=True)) == MCP_RESOURCE_URL


def test_a_legacy_roll_still_writes_the_shared_resource(tmp_path: Path) -> None:
    """The #1974 roll is unchanged: legacy records move to the shared settings."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    _owner_file(tmp_path / "master-key", b"m" * 32)
    _ca_files(tmp_path)
    legacy = EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    )
    legacy.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    rolled = _manager(tmp_path, per_allocation=True).roll_mcp_oauth(_ACTIVATION)

    assert rolled == mcp_oauth_settings()


def test_the_flag_without_settings_writes_no_settings(tmp_path: Path) -> None:
    """With no issuer configured there is no resource to scope."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    _owner_file(tmp_path / "master-key", b"m" * 32)
    _ca_files(tmp_path)
    manager = EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
        per_allocation_mcp_resource=True,
    )

    issued = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    assert issued.mcp_oauth is None


# --------------------------------------------------------------------------- #
# The Machine and the handshake carry exactly the stored resource
# --------------------------------------------------------------------------- #


def _routable(allocation_id: str) -> RoutableAllocation:
    """Return the store-owned routing record for the default job."""
    return RoutableAllocation(
        "job-fly-001", _ACTIVATION, _REQUESTER, _CONSUMER, allocation_id
    )


def _env(api: FakeFlyAPI) -> dict[str, Any]:
    """Return the one Machine's environment."""
    (machines,) = api.machines.values()
    (machine,) = machines
    env = machine["config"]["env"]
    assert isinstance(env, dict)
    return env


def test_a_new_machine_and_its_handshake_name_the_stored_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record, Machine env and ``agent_mcp`` agree byte for byte."""
    api = FakeFlyAPI()
    manager = _manager(tmp_path, per_allocation=True)
    driver = fly_driver(api, manager, fly_replay_enabled=True)

    allocation = driver.provision(fly_job())
    driver.prepare_replay(_routable(allocation.allocation_id))

    stored = _resource(manager)
    env = _env(api)
    assert env[RESOURCE_URL_ENV] == stored
    assert stored != MCP_RESOURCE_URL
    assert allocation.vault_url == public_vault_url(ROUTING_PUBLIC_URL)
    config = load_resource_server_config(env)
    assert config is not None
    verifier = JwtAgentTokenVerifier(config, keys=JwksCache(StaticJwks()))
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    app = build_app(
        vault_path=seed_vault(tmp_path / "vault-root"),
        agent_verifier=verifier,
        fly_replay_state=_REPLAY_STATE,
    )
    request_headers = {
        **headers(minor=None),
        "fly-replay-src": f"instance=test;region=ord;t=1;state={_REPLAY_STATE}",
    }
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=request_headers)

    assert response.status_code == 200
    assert response.json()["agent_mcp"]["resource"] == env[RESOURCE_URL_ENV]
    assert response.json()["agent_mcp"]["authorization_server"] == MCP_ISSUER


def test_a_per_allocation_machine_is_already_current_for_the_legacy_roll(
    tmp_path: Path,
) -> None:
    """The #1974 roll leaves a per-allocation allocation exactly as it is."""
    api = FakeFlyAPI()
    manager = _manager(tmp_path, per_allocation=True)
    driver = fly_driver(api, manager, fly_replay_enabled=True)
    allocation = driver.provision(fly_job())
    before = _env(api)[RESOURCE_URL_ENV]

    assert driver.roll_mcp_oauth(_routable(allocation.allocation_id), manager) is False
    assert api.updates == []
    assert _env(api)[RESOURCE_URL_ENV] == before


# --------------------------------------------------------------------------- #
# Composition: the flag and its default
# --------------------------------------------------------------------------- #


def _worker_bundle(tmp_path: Path, *extra: str) -> worker_cli.WorkerBundle:
    """Compose the production worker over synthetic mounts."""
    from tests.test_provisioning_worker_cli import _NOW as WORKER_NOW
    from tests.test_provisioning_worker_cli import _arguments

    parser = worker_cli.build_parser()
    args = parser.parse_args([*_arguments(tmp_path), *extra])
    return worker_cli.compose(
        args,
        parser,
        clock=lambda: WORKER_NOW,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )


def test_the_worker_keeps_the_shared_resource_by_default(tmp_path: Path) -> None:
    """Without the flag, a new activation names the shared edge resource."""
    bundle = _worker_bundle(tmp_path, "--oauth-authorization-server", MCP_ISSUER)
    try:
        assert _resource(bundle.secrets) == MCP_RESOURCE_URL
    finally:
        bundle.close()


def test_the_worker_flag_mints_a_per_allocation_resource(tmp_path: Path) -> None:
    """``--per-allocation-mcp-resource`` scopes every new activation."""
    bundle = _worker_bundle(
        tmp_path,
        "--oauth-authorization-server",
        MCP_ISSUER,
        "--per-allocation-mcp-resource",
    )
    try:
        resource = _resource(bundle.secrets)
    finally:
        bundle.close()

    assert is_resource_path(allocation_mcp_path(_slug_of(resource)))


def test_the_pilot_cannot_turn_the_flag_on_before_its_edge_routes_slugs(
    tmp_path: Path,
) -> None:
    """The deployed pilot composes the worker with the flag off (#2003 flips)."""
    from tests.test_provisioning_pilot_cli import _arguments

    parser = pilot_cli.build_parser()

    assert parser.parse_args(_arguments(tmp_path)).per_allocation_mcp_resource is False
    with pytest.raises(SystemExit):
        parser.parse_args([*_arguments(tmp_path), "--per-allocation-mcp-resource"])
