"""Authenticated, allocation-specific agent MCP discovery for Adepthood#3545."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlunsplit

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek_mcp.api.models import AGENT_MCP_URL_MAX_CHARS, AgentMcpDiscovery
from creek_mcp.httpapi import app as app_module
from creek_mcp.httpapi.agent_mcp import mounted_agent_mcp
from creek_mcp.mcp_path import PROTECTED_RESOURCE_PATH
from creek_mcp.oauth_resource import ProtectedResourceMetadataMiddleware
from tests.agent_token_support import ISSUER, RESOURCE_URL, agent_verifier
from tests.v1_api_support import CAPABILITIES_PATH, build_app, headers, seed_vault

if TYPE_CHECKING:
    from pathlib import Path

_DISCOVERY = {"resource": RESOURCE_URL, "authorization_server": ISSUER}
_REPLAY_STATE = "r" * 64


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return an initialized, content-free vault for the real HTTP adapter."""
    return seed_vault(tmp_path)


@pytest.mark.parametrize("minor", [None, "0.24"])
def test_agent_mcp_discovery_reports_the_served_configuration(
    vault: Path, minor: str | None
) -> None:
    """Current discovery names exactly the resource and issuer verified by MCP."""
    app = build_app(vault_path=vault, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=minor))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["agent_mcp"] == _DISCOVERY
    assert str(vault) not in response.text


@pytest.mark.parametrize("minor", ["0.22", "0.23"])
def test_agent_mcp_discovery_is_omitted_before_its_introduction(
    vault: Path, minor: str
) -> None:
    """Existing closed-schema clients receive no new field at their pinned minor."""
    app = build_app(vault_path=vault, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=minor))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "agent_mcp" not in response.json()


def test_agent_mcp_discovery_is_null_without_a_jwt_resource_server(vault: Path) -> None:
    """A legacy allocation's static MCP endpoint does not imply OAuth support."""
    with TestClient(build_app(vault_path=vault)) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_is_null_without_the_actual_mount(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured verifier cannot advertise an MCP surface that was not mounted."""
    monkeypatch.setattr(app_module, "resolve_vault", lambda _path: None)
    app = build_app(vault_path=vault, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_is_null_for_managed_private_tls(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private TLS router does not proxy MCP even when OAuth env is installed."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    app = build_app(vault_path=vault, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_reports_a_managed_fly_replay_allocation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay runtime actually routes MCP to this OAuth-enabled allocation."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    app = build_app(
        vault_path=vault,
        agent_verifier=agent_verifier(),
        fly_replay_state=_REPLAY_STATE,
    )
    request_headers = {
        **headers(minor=None),
        "fly-replay-src": f"instance=test;region=ord;t=1;state={_REPLAY_STATE}",
    }
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=request_headers)
    assert response.status_code == 200
    assert response.json()["agent_mcp"] == _DISCOVERY


def test_agent_mcp_discovery_does_not_trust_shared_edge_metadata(vault: Path) -> None:
    """Shared metadata cannot attest that a legacy allocation serves OAuth."""
    app = ProtectedResourceMetadataMiddleware(
        build_app(vault_path=vault), config=agent_verifier().config
    )
    with TestClient(app) as client:
        metadata = client.get(PROTECTED_RESOURCE_PATH)
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == RESOURCE_URL
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_is_not_available_without_authentication(
    vault: Path,
) -> None:
    """The allocation-specific signal stays behind the existing service bearer."""
    app = build_app(vault_path=vault, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(
            CAPABILITIES_PATH, headers=headers(token=None, minor=None)
        )
    assert response.status_code == 401
    assert "agent_mcp" not in response.json()
    assert RESOURCE_URL not in response.text


def test_agent_mcp_discovery_is_null_for_an_uninitialized_vault(tmp_path: Path) -> None:
    """An app built with a verifier is insufficient without a usable vault."""
    app = build_app(vault_path=tmp_path, agent_verifier=agent_verifier())
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["status"] == "uninitialized"
    assert response.json()["agent_mcp"] is None


@pytest.mark.parametrize(
    "value",
    [
        "http://vault.example/mcp",
        urlunsplit(("https", "user:password@vault.example", "/mcp", "", "")),
        "https://vault.example/mcp?secret=value",
        "https://vault.example/mcp#fragment",
        "https://vault.example/mcp\n",
        'https://vault.example/"mcp',
        "https://vault.example/\\mcp",
        "https:///mcp",
        "https://vault.example:bad/mcp",
        "https://vault.example:0/mcp",
        "https://vault.example/not-mcp",
        "https://localhost/mcp",
        "https://localhost./mcp",
        "https://vault.local/mcp",
        "https://vault.internal./mcp",
        "https://vault.home.arpa/mcp",
        "https://vault/mcp",
        "https://127.0.0.1/mcp",
        "https://127.1/mcp",
        "https://0177.0.0.1/mcp",
        "https://0x7f000001/mcp",
        "https://10.0.0.1/mcp",
        "https://169.254.169.254/mcp",
        "https://[::1]/mcp",
        "https://[fc00::1]/mcp",
        "https://[2001:4860:4860::8888%25eth0]/mcp",
        "https://" + "a" * AGENT_MCP_URL_MAX_CHARS + "/mcp",
    ],
)
def test_agent_mcp_discovery_schema_refuses_unsafe_resources(value: str) -> None:
    """Published identifiers are bounded clean HTTPS values, never credentials."""
    with pytest.raises(ValidationError):
        AgentMcpDiscovery(resource=value, authorization_server=ISSUER)


def test_agent_mcp_discovery_schema_is_closed_and_preserves_url_spelling() -> None:
    """A client can compare OAuth identifiers exactly without URL normalization."""
    payload = {
        "resource": "https://VAULT.example:443/mcp",
        "authorization_server": "https://ISSUER.example/base/",
    }
    assert AgentMcpDiscovery.model_validate(payload).model_dump() == payload
    with pytest.raises(ValidationError):
        AgentMcpDiscovery.model_validate({**payload, "credential": "never-published"})


def test_agent_mcp_discovery_does_not_advertise_a_local_http_resource(
    vault: Path,
) -> None:
    """Local MCP development can work without advertising an external HTTPS address."""
    app = build_app(
        vault_path=vault,
        agent_verifier=agent_verifier(resource_url="http://127.0.0.1/mcp"),
    )
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["status"] == "ok"
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_does_not_advertise_a_local_https_resource(
    vault: Path,
) -> None:
    """A configured loopback HTTPS resource is not a public agent address either."""
    app = build_app(
        vault_path=vault,
        agent_verifier=agent_verifier(resource_url="https://127.0.0.1/mcp"),
    )
    with TestClient(app) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(minor=None))
    assert response.json()["status"] == "ok"
    assert response.json()["agent_mcp"] is None


def test_agent_mcp_discovery_accepts_global_ip_identifiers_verbatim() -> None:
    """IPv4 and IPv6 literal resources are allowed only when globally addressed."""
    for resource in ("https://1.1.1.1/mcp", "https://[2001:4860:4860::8888]/mcp"):
        assert (
            AgentMcpDiscovery(resource=resource, authorization_server=ISSUER).resource
            == resource
        )


def test_agent_mcp_discovery_refuses_an_empty_managed_replay_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty argument cannot make an unrouted managed app public."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    assert (
        mounted_agent_mcp(mounted=True, verifier=agent_verifier(), fly_replay_state="")
        is None
    )


def test_agent_mcp_discovery_bounds_the_issuer_without_truncation() -> None:
    """A maximum-length identifier stays exact; a longer one is refused."""
    prefix = "https://issuer.example/"
    issuer = prefix + "a" * (AGENT_MCP_URL_MAX_CHARS - len(prefix))
    assert (
        AgentMcpDiscovery(
            resource=RESOURCE_URL, authorization_server=issuer
        ).authorization_server
        == issuer
    )
    with pytest.raises(ValidationError):
        AgentMcpDiscovery(resource=RESOURCE_URL, authorization_server=issuer + "a")
