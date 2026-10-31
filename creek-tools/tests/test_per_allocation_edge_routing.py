"""The edge routes ``/a/<slug>/mcp`` and binds each token's ``aud`` to it (#2003, R3-1).

Owner decision R3-1: an agent token minted for one managed vault must be
refused at every other vault, at the edge as well as at the vault. #2002 gave
each new allocation its own resource, ``<edge>/a/<slug>/mcp``, in its encrypted
record. This suite pins the edge half:

* a token is verified against the audience the **request path** names, then
  the allocation whose record names that exact resource is found, and its
  owner must be the token's ``sub``. All three must agree;
* an unknown slug, a token for another vault and a shared-audience token are
  refused with the same ``401`` and no route is prepared, so the path is no
  existence oracle;
* the RFC 9728 metadata for a per-allocation resource is answered from the
  path alone, for any slug of the minted grammar, so it is no oracle either;
* the shared ``/mcp`` audience stays accepted until #2004 flips
  ``shared_audience`` off, and both states are tested;
* the vault serves its own ``/a/<slug>/mcp`` — the path a Fly replay carries —
  and no other allocation's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.testclient import TestClient

from creek_mcp.agent_tokens import JwksCache
from creek_mcp.httpapi.context import bind_context
from creek_mcp.httpapi.mcp_mount import McpMountMiddleware
from creek_mcp.httpapi.routing import AgentRouting, build_fly_replay_routing_app
from creek_mcp.mcp_path import (
    ALLOCATION_SLUG_CHARS,
    MCP_PATH,
    PROTECTED_RESOURCE_ROOT,
    allocation_mcp_path,
    allocation_metadata_path,
    allocation_path_of_metadata,
    is_allocation_metadata_path,
)
from creek_mcp.oauth_resource import ResourceServerConfig
from creek_mcp.provisioning import pilot_cli, routing_cli
from creek_mcp.provisioning.models import McpOAuthSettings
from creek_mcp.provisioning.pilot_control import build_edge_application
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    EncryptedFileRoutingCredentialVerifier,
)
from creek_mcp.provisioning.routing import FlyReplayTarget, RoutingPrincipal
from creek_mcp.provisioning.store import ProvisioningStore
from tests.agent_token_support import (
    ISSUER,
    RSA_KID,
    StaticJwks,
    agent_verifier,
    mint,
)
from tests.mcp_http_support import initialize_body, mcp_headers
from tests.test_provisioning_production_adapters import _NOW, _ca_files, _owner_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.models import RoutableAllocation

_EDGE: Final[str] = "https://creek-control-pilot.fly.dev"
_EDGE_MCP_URL: Final[str] = f"{_EDGE}{MCP_PATH}"
_SLUG_X: Final[str] = "X" * ALLOCATION_SLUG_CHARS
_SLUG_Y: Final[str] = "Y" * ALLOCATION_SLUG_CHARS
_SLUG_UNKNOWN: Final[str] = "Z" * ALLOCATION_SLUG_CHARS
_PATH_X: Final[str] = allocation_mcp_path(_SLUG_X)
_PATH_Y: Final[str] = allocation_mcp_path(_SLUG_Y)
_PATH_UNKNOWN: Final[str] = allocation_mcp_path(_SLUG_UNKNOWN)
_RESOURCE_X: Final[str] = f"{_EDGE}{_PATH_X}"
_RESOURCE_Y: Final[str] = f"{_EDGE}{_PATH_Y}"
_RESOURCE_UNKNOWN: Final[str] = f"{_EDGE}{_PATH_UNKNOWN}"
_REQUESTER: Final[str] = "adepthood"
_CONSUMER_X: Final[str] = "user-001"
_CONSUMER_Y: Final[str] = "user-002"
_OWNER_X: Final[RoutingPrincipal] = RoutingPrincipal(_REQUESTER, _CONSUMER_X, "s" * 64)
_OWNER_Y: Final[RoutingPrincipal] = RoutingPrincipal(_REQUESTER, _CONSUMER_Y, "t" * 64)
_STATIC_TOKEN: Final[str] = "managed-vault-routing-token-" + "a" * 32
_UNAUTHENTICATED: Final[int] = 401
_REPLAY: Final[int] = 307
_OK: Final[int] = 200


# --------------------------------------------------------------------------- #
# The metadata path grammar
# --------------------------------------------------------------------------- #


def test_the_allocation_metadata_path_is_the_rfc_9728_insertion() -> None:
    """The well-known segment goes between the origin and ``/a/<slug>/mcp``."""
    path = allocation_metadata_path(_SLUG_X)

    assert path == f"{PROTECTED_RESOURCE_ROOT}/a/{_SLUG_X}/mcp"
    assert is_allocation_metadata_path(path)
    assert allocation_path_of_metadata(path) == _PATH_X


@pytest.mark.parametrize(
    "path",
    [
        f"{PROTECTED_RESOURCE_ROOT}/a/{'X' * 31}/mcp",
        f"{PROTECTED_RESOURCE_ROOT}/a/{_SLUG_X}/mcp/",
        f"{PROTECTED_RESOURCE_ROOT}/a/{_SLUG_X}/mcp/x",
        f"{PROTECTED_RESOURCE_ROOT}{MCP_PATH}",
        f"/x{PROTECTED_RESOURCE_ROOT}/a/{_SLUG_X}/mcp",
        _PATH_X,
        None,
    ],
)
def test_lookalike_metadata_paths_are_not_allocation_metadata(path: object) -> None:
    """Exact grammar only: no subtree, no other length, no prefix."""
    assert not is_allocation_metadata_path(path)
    assert allocation_path_of_metadata(path) is None


# --------------------------------------------------------------------------- #
# A verifier scoped to one resource, sharing the issuer's key cache
# --------------------------------------------------------------------------- #


def test_a_config_scoped_to_another_resource_keeps_issuer_and_keys() -> None:
    """Only the resource changes; the issuer and JWKS URL are the edge's own."""
    edge = ResourceServerConfig.build(
        resource_url=_EDGE_MCP_URL, authorization_server=ISSUER
    )

    scoped = edge.with_resource(_RESOURCE_X)

    assert scoped.resource_url == _RESOURCE_X
    assert scoped.authorization_server == edge.authorization_server
    assert scoped.jwks_url == edge.jwks_url
    with pytest.raises(ValueError, match="CREEK_MCP_RESOURCE_URL"):
        edge.with_resource(f"{_EDGE}/a/short/mcp")


@pytest.mark.asyncio
async def test_a_scoped_verifier_binds_aud_and_shares_the_key_cache() -> None:
    """One JWKS fetch serves every allocation; each accepts only its own ``aud``."""
    fetcher = StaticJwks()
    edge = agent_verifier(fetcher, resource_url=_EDGE_MCP_URL)

    scoped_x = edge.for_resource(_RESOURCE_X)
    scoped_y = edge.for_resource(_RESOURCE_Y)
    token_x = mint(aud=_RESOURCE_X, sub=_CONSUMER_X)

    accepted = await scoped_x.verify_token(token_x)
    refused_elsewhere = await scoped_y.verify_token(token_x)
    refused_shared = await edge.verify_token(token_x)

    assert accepted is not None
    assert accepted.resource == _RESOURCE_X
    assert refused_elsewhere is None
    assert refused_shared is None
    assert scoped_x.config.authorization_server == ISSUER
    assert fetcher.calls == 1


# --------------------------------------------------------------------------- #
# The edge router: fakes
# --------------------------------------------------------------------------- #


class _FakeAllocations:
    """Records keyed by their stored resource, as the encrypted records are."""

    def __init__(self, records: dict[str, RoutingPrincipal]) -> None:
        """Hold *records* and start with no lookups.

        Args:
            records: Each stored resource URL and the owner its record names.
        """
        self.records = records
        self.lookups: list[str] = []

    def shared_audience_activations(self, shared: McpOAuthSettings) -> tuple[str, ...]:
        """Answer #2004's fleet check: every record here is per-allocation.

        Args:
            shared: The edge's shared settings.

        Returns:
            No activation.
        """
        del shared
        return ()

    async def principal_for_resource(
        self,
        resource_url: str,
        *,
        authorization_server: str,
        consumer_identity: str,
    ) -> RoutingPrincipal | None:
        """Return the owner when the record, issuer and subject all agree.

        Args:
            resource_url: The resource the request path names.
            authorization_server: The issuer the token was verified against.
            consumer_identity: The verified token's subject.

        Returns:
            The owner, or ``None``.
        """
        self.lookups.append(resource_url)
        owner = self.records.get(resource_url)
        if (
            owner is None
            or authorization_server != ISSUER
            or owner.consumer_identity != consumer_identity
        ):
            return None
        return owner


class _FakeSharedPrincipals:
    """The shared-audience resolver: ``sub`` to owner."""

    def __init__(self) -> None:
        """Start with no lookups."""
        self.lookups: list[str] = []

    async def principal_for_consumer(
        self, consumer_identity: str
    ) -> RoutingPrincipal | None:
        """Return the owner of ``user-001`` only.

        Args:
            consumer_identity: A verified subject.

        Returns:
            The owner, or ``None``.
        """
        self.lookups.append(consumer_identity)
        return _OWNER_X if consumer_identity == _CONSUMER_X else None


class _StaticVerifier:
    """The static routing credential, owned by X."""

    async def verify_credential(self, credential: str) -> RoutingPrincipal | None:
        """Return X's owner for the one static credential.

        Args:
            credential: The presented bearer.

        Returns:
            The owner, or ``None``.
        """
        return _OWNER_X if credential == _STATIC_TOKEN else None


class _ReplaySpy:
    """Count every replay the router prepares; none may follow a refusal."""

    def __init__(self) -> None:
        """Start with no preparations."""
        self.prepared: list[RoutableAllocation] = []

    def prepare_replay(self, allocation: RoutableAllocation) -> FlyReplayTarget:
        """Record *allocation* and return fixed replay coordinates.

        Args:
            allocation: The store-resolved allocation.

        Returns:
            The replay target.
        """
        self.prepared.append(allocation)
        return FlyReplayTarget("creek-vault-a1b2", "0" * 14)


def _ready(store: ProvisioningStore, activation: str, consumer: str) -> None:
    """Make one provider-managed allocation routable for *consumer*."""
    from creek_mcp.provisioning.models import CustodyMode

    job = store.submit(activation, consumer, requester_identity=_REQUESTER)
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        f"fly-allocation-{consumer}",
        handoff=lambda: None,
        custody_mode=CustodyMode.PROVIDER_MANAGED,
    )


def _edge(
    tmp_path: Path,
    *,
    records: dict[str, RoutingPrincipal] | None = None,
    per_allocation: bool = True,
    shared_audience: bool = True,
) -> tuple[TestClient, _ReplaySpy, _FakeAllocations]:
    """Return the replay router for X and Y, with per-allocation routing.

    Args:
        tmp_path: Pytest's per-test directory.
        records: The stored resources, X's and Y's by default.
        per_allocation: Whether ``/a/<slug>/mcp`` is routed.
        shared_audience: Whether the shared ``/mcp`` audience is accepted.

    Returns:
        The client, the replay spy and the allocation resolver.
    """
    store = ProvisioningStore(tmp_path / "routing.sqlite3")
    _ready(store, "activation-x", _CONSUMER_X)
    _ready(store, "activation-y", _CONSUMER_Y)
    allocations = _FakeAllocations(
        {_RESOURCE_X: _OWNER_X, _RESOURCE_Y: _OWNER_Y} if records is None else records
    )
    agent = AgentRouting(
        agent_verifier(resource_url=_EDGE_MCP_URL),
        _FakeSharedPrincipals(),
        allocations=allocations if per_allocation else None,
        shared_audience=shared_audience,
    )
    spy = _ReplaySpy()
    app = build_fly_replay_routing_app(store, _StaticVerifier(), spy, agent=agent)
    return TestClient(app), spy, allocations


def _bearer(token: str) -> dict[str, str]:
    """Return the ``Authorization`` header for *token*."""
    return {"Authorization": f"Bearer {token}"}


def _refusal_shape(response: Any) -> tuple[int, Any, list[str]]:
    """Return what a caller can observe of a refusal, minus per-request ids."""
    body = response.json()
    body.pop("request_id", None)
    challenges = [
        value
        for name, value in response.headers.multi_items()
        if name == "www-authenticate"
    ]
    return response.status_code, body, challenges


# --------------------------------------------------------------------------- #
# Routing a per-allocation token
# --------------------------------------------------------------------------- #


def test_a_token_for_this_allocation_is_replayed_to_its_owner(tmp_path: Path) -> None:
    """``aud`` = the path's resource, the record names it, ``sub`` owns it."""
    client, spy, allocations = _edge(tmp_path)

    response = client.post(
        _PATH_X,
        headers=_bearer(mint(aud=_RESOURCE_X, sub=_CONSUMER_X)),
        content=b"{}",
    )

    assert response.status_code == _REPLAY
    assert response.headers["Fly-Replay"] == (
        f"app=creek-vault-a1b2;instance={'0' * 14};state={'s' * 64}"
    )
    assert allocations.lookups == [_RESOURCE_X]
    assert [allocation.consumer_identity for allocation in spy.prepared] == [
        _CONSUMER_X
    ]


def test_a_cross_vault_replay_is_refused_before_any_route(tmp_path: Path) -> None:
    """X's token at Y's path is ``401`` even though X's ``sub`` is a real owner."""
    client, spy, allocations = _edge(tmp_path)

    response = client.post(
        _PATH_Y,
        headers=_bearer(mint(aud=_RESOURCE_X, sub=_CONSUMER_X)),
        content=b"{}",
    )

    assert response.status_code == _UNAUTHENTICATED
    assert "Fly-Replay" not in response.headers
    assert spy.prepared == []
    # Refused on the audience, before any encrypted record is read.
    assert allocations.lookups == []


def test_a_token_naming_the_path_but_another_owner_is_refused(tmp_path: Path) -> None:
    """``aud`` matches Y's path, but ``sub`` is X: the record's owner disagrees."""
    client, spy, allocations = _edge(tmp_path)

    response = client.post(
        _PATH_Y,
        headers=_bearer(mint(aud=_RESOURCE_Y, sub=_CONSUMER_X)),
        content=b"{}",
    )

    assert response.status_code == _UNAUTHENTICATED
    assert spy.prepared == []
    assert allocations.lookups == [_RESOURCE_Y]


def test_an_unknown_slug_and_a_wrong_audience_are_indistinguishable(
    tmp_path: Path,
) -> None:
    """The same path answers the same whether or not an allocation lives there."""
    for root in (tmp_path / "known", tmp_path / "unknown"):
        root.mkdir()
    known, known_spy, _ = _edge(tmp_path / "known")
    unknown, unknown_spy, _ = _edge(
        tmp_path / "unknown", records={_RESOURCE_X: _OWNER_X}
    )

    # Y exists: a token for X is refused on its audience.
    wrong_audience = known.post(
        _PATH_Y,
        headers=_bearer(mint(aud=_RESOURCE_X, sub=_CONSUMER_X)),
        content=b"{}",
    )
    # Y does not exist: even a token minted for exactly that path is refused.
    no_such_slug = unknown.post(
        _PATH_Y,
        headers=_bearer(mint(aud=_RESOURCE_Y, sub=_CONSUMER_Y)),
        content=b"{}",
    )
    anonymous = unknown.post(_PATH_Y, content=b"{}")

    assert _refusal_shape(wrong_audience) == _refusal_shape(no_such_slug)
    assert _refusal_shape(no_such_slug) == _refusal_shape(anonymous)
    assert _refusal_shape(anonymous)[2] == [
        ResourceServerConfig.build(
            resource_url=_RESOURCE_Y, authorization_server=ISSUER
        ).challenge(_UNAUTHENTICATED)
    ]
    assert known_spy.prepared == unknown_spy.prepared == []


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(aud=_EDGE_MCP_URL, sub=_CONSUMER_X), id="shared"),
        pytest.param(lambda: _STATIC_TOKEN, id="static-routing-credential"),
        pytest.param(
            lambda: mint(aud=_RESOURCE_X, sub=_CONSUMER_X, iss="https://evil.example"),
            id="foreign-issuer",
        ),
        pytest.param(
            lambda: mint(aud=_RESOURCE_X, sub=_CONSUMER_X, exp=1_000), id="expired"
        ),
    ],
)
def test_only_a_per_allocation_agent_token_routes_a_slug(
    tmp_path: Path, token: Any
) -> None:
    """A shared-audience token or the static credential never selects a slug."""
    client, spy, _allocations = _edge(tmp_path)
    reference = client.post(_PATH_X, content=b"{}")

    response = client.post(_PATH_X, headers=_bearer(token()), content=b"{}")

    assert _refusal_shape(response) == _refusal_shape(reference)
    assert spy.prepared == []


@pytest.mark.parametrize("path", [f"{_PATH_X}/", f"{_PATH_X}/x", "/a/short/mcp"])
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(aud=_RESOURCE_X, sub=_CONSUMER_X), id="agent"),
        pytest.param(lambda: _STATIC_TOKEN, id="static-routing-credential"),
    ],
)
def test_lookalike_allocation_paths_never_route(
    tmp_path: Path, path: str, token: Any
) -> None:
    """Only the exact minted grammar is a per-allocation route, for any bearer."""
    client, spy, _allocations = _edge(tmp_path)

    response = client.post(path, headers=_bearer(token()), content=b"{}")

    assert response.status_code == _UNAUTHENTICATED
    assert "Fly-Replay" not in response.headers
    assert spy.prepared == []


def test_a_refusal_logs_nothing_that_names_a_vault_or_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No token, no ``kid``, no slug and no other vault's URL in any log line."""
    client, _spy, _allocations = _edge(tmp_path)
    token = mint(aud=_RESOURCE_X, sub=_CONSUMER_X)

    with caplog.at_level(logging.DEBUG):
        client.post(_PATH_Y, headers=_bearer(token), content=b"{}")
        client.post(_PATH_UNKNOWN, headers=_bearer(token), content=b"{}")

    # The test client's own httpx logger records the URL it requested; that
    # is the caller's side, not the edge's.
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    )
    for secret in (token, RSA_KID, _SLUG_X, _SLUG_Y, _SLUG_UNKNOWN, _RESOURCE_X):
        assert secret not in logged


# --------------------------------------------------------------------------- #
# Per-allocation RFC 9728 metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slug", [_SLUG_X, _SLUG_UNKNOWN])
def test_per_allocation_metadata_names_that_resource_for_any_slug(
    tmp_path: Path, slug: str
) -> None:
    """Answered from the path alone: a live slug and a dead one look the same."""
    client, spy, allocations = _edge(tmp_path)

    response = client.get(allocation_metadata_path(slug))

    resource = f"{_EDGE}{allocation_mcp_path(slug)}"
    assert response.status_code == _OK
    assert response.json() == (
        ResourceServerConfig.build(
            resource_url=resource, authorization_server=ISSUER
        ).metadata_document()
    )
    assert response.headers["access-control-allow-origin"] == "*"
    assert allocations.lookups == []
    assert spy.prepared == []


def test_an_anonymous_slug_call_is_challenged_toward_its_own_metadata(
    tmp_path: Path,
) -> None:
    """The challenge names ``/.well-known/oauth-protected-resource/a/<slug>/mcp``."""
    client, _spy, _allocations = _edge(tmp_path)

    response = client.post(_PATH_X, content=b"{}")

    assert response.status_code == _UNAUTHENTICATED
    assert (
        f'resource_metadata="{_EDGE}{allocation_metadata_path(_SLUG_X)}"'
        in response.headers["www-authenticate"]
    )


# --------------------------------------------------------------------------- #
# Both configuration states
# --------------------------------------------------------------------------- #


def test_with_per_allocation_routing_off_a_slug_is_not_routed(tmp_path: Path) -> None:
    """The default: no slug route, no slug metadata, exactly as before #2003."""
    client, spy, allocations = _edge(tmp_path, per_allocation=False)

    routed = client.post(
        _PATH_X,
        headers=_bearer(mint(aud=_RESOURCE_X, sub=_CONSUMER_X)),
        content=b"{}",
    )
    metadata = client.get(allocation_metadata_path(_SLUG_X))

    assert routed.status_code == _UNAUTHENTICATED
    assert metadata.status_code == _UNAUTHENTICATED
    assert allocations.lookups == []
    assert spy.prepared == []


@pytest.mark.parametrize("per_allocation", [True, False])
def test_the_shared_audience_is_accepted_until_it_is_switched_off(
    tmp_path: Path, per_allocation: bool
) -> None:
    """Before #2004 rolls every allocation, ``/mcp`` keeps its shared audience."""
    client, spy, _allocations = _edge(tmp_path, per_allocation=per_allocation)

    response = client.post(
        MCP_PATH,
        headers=_bearer(mint(aud=_EDGE_MCP_URL, sub=_CONSUMER_X)),
        content=b"{}",
    )

    assert response.status_code == _REPLAY
    assert len(spy.prepared) == 1


def test_switching_the_shared_audience_off_refuses_it_everywhere(
    tmp_path: Path,
) -> None:
    """#2004's flip: a shared-audience token is refused; slugs and static stay."""
    client, spy, _allocations = _edge(tmp_path, shared_audience=False)

    shared = client.post(
        MCP_PATH,
        headers=_bearer(mint(aud=_EDGE_MCP_URL, sub=_CONSUMER_X)),
        content=b"{}",
    )
    assert shared.status_code == _UNAUTHENTICATED
    assert spy.prepared == []

    scoped = client.post(
        _PATH_X,
        headers=_bearer(mint(aud=_RESOURCE_X, sub=_CONSUMER_X)),
        content=b"{}",
    )
    static = client.post(MCP_PATH, headers=_bearer(_STATIC_TOKEN), content=b"{}")
    assert scoped.status_code == _REPLAY
    assert static.status_code == _REPLAY


def test_the_shared_audience_cannot_be_refused_without_slug_routing() -> None:
    """Refusing ``/mcp``'s audience with no per-allocation route serves nobody."""
    with pytest.raises(ValueError, match="per-allocation"):
        AgentRouting(
            agent_verifier(resource_url=_EDGE_MCP_URL),
            _FakeSharedPrincipals(),
            shared_audience=False,
        )


def test_slug_routing_needs_the_shared_edge_resource_as_its_base() -> None:
    """The edge's own resource is ``<origin>/mcp``; slugs hang off that origin."""
    with pytest.raises(ValueError, match="shared edge"):
        AgentRouting(
            agent_verifier(resource_url=_RESOURCE_X),
            _FakeSharedPrincipals(),
            allocations=_FakeAllocations({}),
        )


# --------------------------------------------------------------------------- #
# The encrypted records: the production resolver
# --------------------------------------------------------------------------- #


def _records(tmp_path: Path) -> tuple[str, str, EncryptedFileRoutingCredentialVerifier]:
    """Issue X and Y with per-allocation resources; return them and the reader."""
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
        mcp_oauth=McpOAuthSettings(_EDGE_MCP_URL, ISSUER),
        per_allocation_mcp_resource=True,
    )
    resources = []
    for activation, consumer in (("act-x", _CONSUMER_X), ("act-y", _CONSUMER_Y)):
        issued = manager.issue(activation, consumer, requester_identity=_REQUESTER)
        assert issued.mcp_oauth is not None
        resources.append(issued.mcp_oauth.resource_url)
    reader = EncryptedFileRoutingCredentialVerifier(
        state, master_key_file=tmp_path / "master-key"
    )
    return resources[0], resources[1], reader


@pytest.mark.asyncio
async def test_the_record_reader_finds_the_owner_by_exact_resource(
    tmp_path: Path,
) -> None:
    """Resource, issuer and subject must all match one record."""
    resource_x, resource_y, reader = _records(tmp_path)

    found = await reader.principal_for_resource(
        resource_x, authorization_server=ISSUER, consumer_identity=_CONSUMER_X
    )
    crossed = await reader.principal_for_resource(
        resource_y, authorization_server=ISSUER, consumer_identity=_CONSUMER_X
    )
    unknown = await reader.principal_for_resource(
        _RESOURCE_UNKNOWN, authorization_server=ISSUER, consumer_identity=_CONSUMER_X
    )
    foreign = await reader.principal_for_resource(
        resource_x,
        authorization_server="https://evil.example",
        consumer_identity=_CONSUMER_X,
    )
    shared = await reader.principal_for_resource(
        _EDGE_MCP_URL, authorization_server=ISSUER, consumer_identity=_CONSUMER_X
    )

    assert found is not None
    assert found.consumer_identity == _CONSUMER_X
    assert found.requester_identity == _REQUESTER
    assert crossed is None
    assert unknown is None
    assert foreign is None
    assert shared is None


# --------------------------------------------------------------------------- #
# The Fly edge listener admits the slug paths only when they are routed
# --------------------------------------------------------------------------- #


def _edge_listener(*, per_allocation: bool) -> TestClient:
    """Return the attested Fly listener over a labelled router double."""

    async def label(scope: Any, receive: Any, send: Any) -> None:
        del receive
        body = b"router" if scope["type"] == "http" else b""
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    application = build_edge_application(
        label,
        label,
        expected_host="creek-control-pilot.fly.dev",
        fly_app_name="creek-control-pilot",
        fly_machine_id="machine-001",
        routing_miss_status=404,
        per_allocation_mcp=per_allocation,
    )
    client = TestClient(application)
    client.headers.update(
        {
            "Host": "creek-control-pilot.fly.dev",
            "X-Forwarded-Proto": "https",
            "Fly-Forwarded-Port": "443",
        }
    )
    return client


@pytest.mark.parametrize("path", [_PATH_X, allocation_metadata_path(_SLUG_X)])
def test_the_listener_hands_slug_paths_to_the_router_when_routed(path: str) -> None:
    """On: the vault realm includes ``/a/<slug>/mcp`` and its metadata path."""
    assert _edge_listener(per_allocation=True).get(path).text == "router"
    assert _edge_listener(per_allocation=False).get(path).status_code == 404


@pytest.mark.parametrize(
    "path", [f"{_PATH_X}/", "/a/short/mcp", f"/a/{_SLUG_X}/v1", "/a/"]
)
def test_the_listener_misses_lookalike_slug_paths(path: str) -> None:
    """Off-grammar paths are the content-free routing miss, flag or not."""
    response = _edge_listener(per_allocation=True).get(path)

    assert response.status_code == 404
    assert response.content == b""


# --------------------------------------------------------------------------- #
# The vault serves its own /a/<slug>/mcp — the path a replay carries
# --------------------------------------------------------------------------- #


def _mount(allocation_path: str | None) -> tuple[TestClient, list[str]]:
    """Return the ``/mcp`` mount over a recording MCP app and a 404 below."""
    seen: list[str] = []

    async def mcp_app(scope: Any, receive: Any, send: Any) -> None:
        del receive
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})

    async def below(scope: Any, receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = McpMountMiddleware(
        below,
        mcp_app=mcp_app,
        max_concurrency=4,
        fly_replay_state=None,
        allocation_path=allocation_path,
    )

    async def outermost(scope: Any, receive: Any, send: Any) -> None:
        # The access log binds the request context in the real stack.
        bind_context(scope)
        await middleware(scope, receive, send)

    return TestClient(outermost), seen


def test_the_vault_serves_its_own_allocation_path_as_mcp() -> None:
    """The replayed ``/a/<own slug>/mcp`` reaches the SDK at ``/mcp``."""
    client, seen = _mount(_PATH_X)

    own = client.post(_PATH_X, content=b"{}")
    other = client.post(_PATH_Y, content=b"{}")
    plain = client.post(MCP_PATH, content=b"{}")

    assert own.text == "mcp"
    assert other.status_code == 404
    assert plain.text == "mcp"
    assert seen == [MCP_PATH, MCP_PATH]


def test_a_vault_without_an_allocation_path_serves_only_mcp() -> None:
    """A self-hosted or shared-resource vault is unchanged."""
    client, seen = _mount(None)

    assert client.post(_PATH_X, content=b"{}").status_code == 404
    assert seen == []


def test_an_allocation_path_must_be_the_minted_grammar() -> None:
    """The mount refuses to alias anything but one exact ``/a/<slug>/mcp``."""
    with pytest.raises(ValueError, match="allocation"):
        _mount("/v1/capabilities")


def test_a_per_allocation_vault_answers_mcp_at_its_replayed_path(
    tmp_path: Path,
) -> None:
    """End to end on the vault: the replayed path initializes an MCP session."""
    from tests.v1_api_support import build_app, seed_vault

    vault = seed_vault(tmp_path)
    verifier = agent_verifier(resource_url=_RESOURCE_X)
    app = build_app(vault_path=vault, agent_verifier=verifier)
    token = mint(aud=_RESOURCE_X)

    with TestClient(app) as client:
        own = client.post(_PATH_X, json=initialize_body(), headers=mcp_headers(token))
        other = client.post(
            _PATH_Y,
            json=initialize_body(),
            headers=mcp_headers(mint(aud=_RESOURCE_Y)),
        )

    assert own.status_code == _OK, own.text
    assert "mcp-session-id" in own.headers
    assert other.status_code in {_UNAUTHENTICATED, 404}


# --------------------------------------------------------------------------- #
# The pilot exposes the flag, off by default
# --------------------------------------------------------------------------- #


def test_the_pilot_flag_is_off_by_default_and_can_be_turned_on(tmp_path: Path) -> None:
    """``--per-allocation-mcp-resource`` drives both worker and edge."""
    from tests.test_provisioning_pilot_cli import _arguments

    parser = pilot_cli.build_parser()

    default = parser.parse_args(_arguments(tmp_path))
    enabled = parser.parse_args(
        [*_arguments(tmp_path), "--per-allocation-mcp-resource"]
    )
    refusing = parser.parse_args(
        [
            *_arguments(tmp_path),
            "--per-allocation-mcp-resource",
            "--refuse-shared-mcp-audience",
        ]
    )

    assert default.per_allocation_mcp_resource is False
    assert default.refuse_shared_mcp_audience is False
    assert enabled.per_allocation_mcp_resource is True
    assert refusing.refuse_shared_mcp_audience is True


def _routing_args(*extra: str) -> Any:
    """Parse the router's OAuth arguments over a minimal namespace."""
    parser = routing_cli.build_parser()
    namespace = parser.parse_args(
        [
            "--database",
            "db",
            "--fly-token-file",
            "t",
            "--fly-organization",
            "o",
            "--fly-image",
            "i",
            "--fly-token-expires-at",
            "2026-09-28T12:00:00+00:00",
            "--secret-state-directory",
            "s",
            "--secret-master-key-file",
            "k",
            "--private-tls-ca-certificate-file",
            "c",
            *extra,
        ]
    )
    namespace.oauth_resource_url = _EDGE_MCP_URL
    return namespace


def test_the_edge_composition_wires_both_flags() -> None:
    """``agent_routing`` turns the flags into the route's two settings."""
    principals = _FakeAllocations({})
    base = ("--oauth-authorization-server", ISSUER)

    off = routing_cli.agent_routing(_routing_args(*base), principals)
    on = routing_cli.agent_routing(
        _routing_args(*base, "--per-allocation-mcp-resource"), principals
    )
    flipped = routing_cli.agent_routing(
        _routing_args(
            *base, "--per-allocation-mcp-resource", "--refuse-shared-mcp-audience"
        ),
        principals,
    )

    assert off is not None
    assert off.allocations is None
    assert off.shared_audience is True
    assert on is not None
    assert on.allocations is principals
    assert on.shared_audience is True
    assert flipped is not None
    assert flipped.shared_audience is False


@pytest.mark.parametrize(
    "extra",
    [
        ("--per-allocation-mcp-resource",),
        ("--refuse-shared-mcp-audience",),
        ("--oauth-authorization-server", ISSUER, "--refuse-shared-mcp-audience"),
    ],
)
def test_the_edge_composition_refuses_an_incoherent_flag_set(
    extra: tuple[str, ...],
) -> None:
    """No issuer, or refusing ``/mcp`` with nothing routed in its place."""
    with pytest.raises(ValueError, match="per-allocation"):
        routing_cli.agent_routing(_routing_args(*extra), _FakeAllocations({}))


def test_the_scoped_verifier_shares_one_key_cache_across_allocations() -> None:
    """A fresh scope never builds a second cache that would refetch the JWKS."""
    keys = JwksCache(StaticJwks())
    edge = agent_verifier(resource_url=_EDGE_MCP_URL)
    rebuilt = type(edge)(edge.config, keys=keys)

    assert rebuilt.for_resource(_RESOURCE_X).keys is keys
