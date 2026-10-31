"""``/mcp`` as an OAuth 2.1 resource server, end to end (#1963).

Every test drives :func:`creek_mcp.httpapi.app.create_app` — the factory both
container entry points serve — so what is asserted is what a client reaching a
vault's MCP URL meets: the RFC 9728 metadata, the ``401`` challenge that
points at it, a JWT issued for this vault accepted and dispatched as its
consumer under the remote tier cap and its grants, every other JWT refused, and
static consumer tokens still accepted on the same endpoint. The standalone
``creek-tools-mcp --transport network`` server is covered at the end.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from creek_mcp import server as server_mod
from creek_mcp.agent_tokens import mcp_token_verifier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.mcp_path import (
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    PROTECTED_RESOURCE_ROOT,
)
from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV, RESOURCE_URL_ENV
from creek_mcp.policy import (
    REMOTE_CEILING_REFUSAL_REASON,
    TOOL_NOT_GRANTED_REASON,
    Transport,
)
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV, remote_auth_settings
from creek_mcp.server import build_server
from creek_mcp.tools.handshake import AuthMode
from tests.agent_token_support import (
    AGENT_CLIENT_ID,
    AGENT_SUBJECT,
    ISSUER,
    OTHER_VAULT_URL,
    RESOURCE_URL,
    FakeTokenStatus,
    agent_verifier,
    mint,
    resource_config,
    revocation_cache,
)
from tests.mcp_http_support import (
    call_handshake,
    call_tool,
    initialize_body,
    listed_tools,
    mcp_headers,
    open_session,
)
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    STRONG_TOKEN,
    build_app,
    headers,
    seed_vault,
    verifier,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.applications import Starlette

_OK: Final[int] = 200
_UNAUTHENTICATED: Final[int] = 401
_FORBIDDEN: Final[int] = 403
_ROUTING_MISS: Final[int] = 404


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault.

    Args:
        tmp_path: Pytest's per-test directory.

    Yields:
        The vault root.
    """
    yield seed_vault(tmp_path)


def _oauth_app(vault: Path, **kwargs: Any) -> Starlette:
    """Return the served app with OAuth configured against the local JWKS.

    Args:
        vault: The vault to serve.
        kwargs: Passed to :func:`tests.agent_token_support.agent_verifier`.

    Returns:
        The app.
    """
    return build_app(vault_path=vault, agent_verifier=agent_verifier(**kwargs))


def _challenges(response: Any) -> list[str]:
    """Return every ``WWW-Authenticate`` value on *response*.

    Args:
        response: A test-client response.

    Returns:
        The challenge values.
    """
    return [
        value
        for name, value in response.headers.multi_items()
        if name == "www-authenticate"
    ]


def _audited_consumers(vault: Path, tool: str) -> list[str]:
    """Return the consumer of every audit entry for *tool*.

    Args:
        vault: The served vault.
        tool: The tool name.

    Returns:
        The audited consumers, in order.
    """
    log = vault / MCP_AUDIT_RELPATH
    entries = [json.loads(line) for line in log.read_text().splitlines() if line]
    return [entry["consumer"] for entry in entries if entry["tool"] == tool]


# --------------------------------------------------------------------------- #
# RFC 9728 metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [PROTECTED_RESOURCE_PATH, PROTECTED_RESOURCE_ROOT])
def test_the_metadata_is_served_without_a_credential(vault: Path, path: str) -> None:
    """Both metadata paths answer the issue's document, unauthenticated.

    Args:
        vault: A seeded vault.
        path: A metadata path.
    """
    with TestClient(_oauth_app(vault)) as client:
        response = client.get(path)

    assert response.status_code == _OK
    assert response.json() == {
        "resource": RESOURCE_URL,
        "authorization_servers": [ISSUER],
        "scopes_supported": [
            "vault:read",
            "vault:desk",
            "vault:journal",
            "vault:pipeline",
        ],
        "bearer_methods_supported": ["header"],
    }
    assert str(vault) not in response.text


@pytest.mark.parametrize("path", [PROTECTED_RESOURCE_PATH, PROTECTED_RESOURCE_ROOT])
def test_a_bearer_only_vault_serves_no_metadata(vault: Path, path: str) -> None:
    """No issuer configured: the paths are ordinary ``/v1`` misses, as before.

    Args:
        vault: A seeded vault.
        path: A metadata path.
    """
    with TestClient(build_app(vault_path=vault)) as client:
        anonymous = client.get(path)
        authenticated = client.get(path, headers=headers())

    assert anonymous.status_code == _UNAUTHENTICATED
    assert "resource" not in anonymous.text
    assert authenticated.status_code == _ROUTING_MISS


# --------------------------------------------------------------------------- #
# The 401 challenge
# --------------------------------------------------------------------------- #


def test_an_unauthenticated_call_is_challenged_toward_the_metadata(vault: Path) -> None:
    """``401`` with exactly one challenge naming the metadata and the scope.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        response = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(None)
        )

    assert response.status_code == _UNAUTHENTICATED
    assert _challenges(response) == [resource_config().challenge(_UNAUTHENTICATED)]
    assert (
        'resource_metadata="https://v-7q2k.creek.example/.well-known/'
        'oauth-protected-resource/mcp"'
    ) in response.headers["www-authenticate"]
    assert 'scope="vault:read vault:desk"' in response.headers["www-authenticate"]
    assert str(vault) not in response.text
    assert vault.name not in response.text


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(aud=OTHER_VAULT_URL), id="another-vault"),
        pytest.param(lambda: mint(exp=1_000), id="expired"),
        pytest.param(lambda: mint(iss="https://evil.example"), id="another-issuer"),
        pytest.param(lambda: "not-a-configured-token-" + "z" * 20, id="unknown"),
    ],
)
def test_a_refused_token_is_challenged_like_no_token(vault: Path, token: Any) -> None:
    """Another vault's, an expired, a foreign and an unknown token: all ``401``.

    Args:
        vault: A seeded vault.
        token: A thunk returning the refused bearer.
    """
    with TestClient(_oauth_app(vault)) as client:
        response = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(token())
        )

    assert response.status_code == _UNAUTHENTICATED
    assert _challenges(response) == [resource_config().challenge(_UNAUTHENTICATED)]
    assert "mcp-session-id" not in response.headers


def test_the_skew_holds_at_the_transport_too(vault: Path) -> None:
    """A token that expired 10 s ago still opens a session; 40 s ago does not.

    Args:
        vault: A seeded vault.
    """
    now = int(time.time())
    with TestClient(_oauth_app(vault)) as client:
        recent = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(mint(exp=now - 10))
        )
        stale = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(mint(exp=now - 40))
        )

    assert recent.status_code == _OK
    assert stale.status_code == _UNAUTHENTICATED


def test_a_token_with_no_grant_is_403_insufficient_scope(vault: Path) -> None:
    """Accepted, but granting nothing this vault knows: refused before dispatch.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        response = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(mint(scope="openid"))
        )

    assert response.status_code == _FORBIDDEN
    assert _challenges(response) == [resource_config().challenge(_FORBIDDEN)]
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert "mcp-session-id" not in response.headers


def test_a_bearer_only_challenge_names_no_placeholder(vault: Path) -> None:
    """The retired ``creek.invalid`` URL never reaches the wire.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as client:
        response = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(None)
        )

    assert response.status_code == _UNAUTHENTICATED
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "invalid/" not in response.headers["www-authenticate"]
    assert "resource_metadata" not in response.headers["www-authenticate"]


# --------------------------------------------------------------------------- #
# An accepted JWT
# --------------------------------------------------------------------------- #


def test_a_jwt_for_this_vault_is_dispatched_as_its_consumer(vault: Path) -> None:
    """Dispatched as the registered OAuth client, remote, and audited under it.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        session = open_session(client, mint())
        handshake = call_handshake(client, session["headers"])

    assert handshake["status"] == "ok"
    assert handshake["transport"] == Transport.NETWORK.value
    assert handshake["auth"] == AuthMode.OAUTH.value == "oauth"
    assert _audited_consumers(vault, "creek.handshake") == [AGENT_CLIENT_ID]


def test_a_jwt_caller_sees_and_reaches_only_its_grants(vault: Path) -> None:
    """The default profile lists read and desk tools, and refuses the pipeline.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        session = open_session(client, mint())
        tools = listed_tools(client, session["headers"])
        ingest = call_tool(client, session["headers"], "creek.ingest", {})

    assert "creek.mine" in tools
    assert "creek.draft" in tools
    assert "creek.ingest" not in tools
    assert "creek.journal" not in tools
    assert ingest["status"] == "refused"
    assert ingest["reason"] == TOOL_NOT_GRANTED_REASON


@pytest.mark.parametrize("ceiling", ["intimate", "all"])
def test_a_jwt_caller_asking_for_intimate_is_refused(vault: Path, ceiling: str) -> None:
    """The remote tier cap applies to JWT callers, with the existing reason.

    Args:
        vault: A seeded vault.
        ceiling: A ceiling no remote identity may request.
    """
    with TestClient(_oauth_app(vault)) as client:
        session = open_session(client, mint())
        refused = call_tool(
            client,
            session["headers"],
            "creek.wheel",
            {"privacy_tier_ceiling": ceiling},
        )

    assert refused["status"] == "refused"
    assert refused["reason"] == REMOTE_CEILING_REFUSAL_REASON


def test_a_pinned_vault_refuses_another_persons_token(vault: Path) -> None:
    """A managed vault pinned to its consumer refuses any other subject.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault, expected_subject=AGENT_SUBJECT)) as client:
        mine = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(mint())
        )
        theirs = client.post(
            MCP_PATH,
            json=initialize_body(),
            headers=mcp_headers(mint(sub="acct_other")),
        )

    assert mine.status_code == _OK
    assert theirs.status_code == _UNAUTHENTICATED


# --------------------------------------------------------------------------- #
# Revocation, checked on every call (#2005, R3-2)
# --------------------------------------------------------------------------- #


def _post_rpc(client: TestClient, session_headers: dict[str, str], method: str) -> Any:
    """Send one JSON-RPC request and return the raw response.

    Args:
        client: A client whose lifespan is running.
        session_headers: An open session's headers.
        method: The JSON-RPC method.

    Returns:
        The response, whatever its status.
    """
    params = (
        {"name": "creek.handshake", "arguments": {}} if method == "tools/call" else {}
    )
    return client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers=session_headers,
    )


def test_a_revoked_agent_is_refused_on_its_next_call(vault: Path) -> None:
    """Stop in Settings: the next call is the generic ``401``; nothing runs.

    Args:
        vault: A seeded vault.
    """
    status = FakeTokenStatus()
    app = _oauth_app(vault, revocation=revocation_cache(status, ttl_seconds=0))
    with TestClient(app) as client:
        session = open_session(client, mint())
        assert "creek.handshake" in listed_tools(client, session["headers"])
        status.revoke()
        listed = _post_rpc(client, session["headers"], "tools/list")
        called = _post_rpc(client, session["headers"], "tools/call")

    for response in (listed, called):
        assert response.status_code == _UNAUTHENTICATED
        assert _challenges(response) == [resource_config().challenge(_UNAUTHENTICATED)]
        assert "creek.handshake" not in response.text
    audit = vault / MCP_AUDIT_RELPATH
    assert not audit.exists() or _audited_consumers(vault, "creek.handshake") == []


def test_an_unreachable_issuer_refuses_with_the_generic_challenge(vault: Path) -> None:
    """Fail closed: no answer and no fresh ``active`` is an invalid token.

    Args:
        vault: A seeded vault.
    """
    status = FakeTokenStatus()
    status.failing = True
    app = _oauth_app(vault, revocation=revocation_cache(status))
    with TestClient(app) as client:
        response = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(mint())
        )

    assert response.status_code == _UNAUTHENTICATED
    assert _challenges(response) == [resource_config().challenge(_UNAUTHENTICATED)]
    assert "mcp-session-id" not in response.headers


# --------------------------------------------------------------------------- #
# Static tokens beside OAuth
# --------------------------------------------------------------------------- #


def test_a_static_token_still_reaches_mcp_on_an_oauth_vault(vault: Path) -> None:
    """The adepthood service bearer keeps working on the same endpoint.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        session = open_session(client, STRONG_TOKEN)
        handshake = call_handshake(client, session["headers"])

    assert handshake["status"] == "ok"
    assert handshake["auth"] == AuthMode.OAUTH.value


def test_a_jwt_never_reaches_v1(vault: Path) -> None:
    """``/v1`` stays the static registry's: an agent JWT is ``401`` there.

    Args:
        vault: A seeded vault.
    """
    with TestClient(_oauth_app(vault)) as client:
        response = client.get(CAPABILITIES_PATH, headers=headers(token=mint()))

    assert response.status_code == _UNAUTHENTICATED


def test_a_bearer_only_vault_reports_bearer(vault: Path) -> None:
    """No issuer: the handshake says ``bearer``.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as client:
        session = open_session(client)
        handshake = call_handshake(client, session["headers"])

    assert handshake["auth"] == AuthMode.BEARER.value == "bearer"


# --------------------------------------------------------------------------- #
# The SDK settings, and the standalone network server
# --------------------------------------------------------------------------- #


def test_the_sdk_settings_carry_the_real_urls() -> None:
    """``issuer_url`` / ``resource_server_url`` are the configured values."""
    settings = remote_auth_settings(resource_config())

    assert str(settings.resource_server_url) == RESOURCE_URL
    assert str(settings.issuer_url).rstrip("/") == ISSUER
    assert settings.validate_token_resource is False


def test_bearer_only_sdk_settings_publish_no_resource() -> None:
    """Without an issuer the SDK has no resource URL to put in a challenge."""
    settings = remote_auth_settings()

    assert settings.resource_server_url is None
    assert "creek.invalid" not in settings.model_dump_json()


def _standalone(vault: Path, *, oauth: bool) -> TestClient:
    """Return a client over the standalone network server's own app.

    Args:
        vault: The vault to serve.
        oauth: Whether to configure the resource server.

    Returns:
        A test client.
    """
    agent = agent_verifier() if oauth else None
    server = build_server(
        transport=Transport.NETWORK,
        vault_path=vault,
        token_verifier=mcp_token_verifier(verifier(), agent),
        resource_server=None if agent is None else agent.config,
    )
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    return TestClient(server.streamable_http_app())


def test_the_standalone_server_publishes_this_resources_metadata(vault: Path) -> None:
    """The SDK's own metadata route is shadowed by the vault's document.

    Args:
        vault: A seeded vault.
    """
    with _standalone(vault, oauth=True) as client:
        metadata = client.get(PROTECTED_RESOURCE_PATH)
        refused = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(None)
        )
        session = open_session(client, mint())
        handshake = call_handshake(client, session["headers"])

    assert metadata.json() == resource_config().metadata_document()
    assert _challenges(refused) == [resource_config().challenge(_UNAUTHENTICATED)]
    assert handshake["auth"] == AuthMode.OAUTH.value


def test_the_standalone_bearer_server_is_unchanged(vault: Path) -> None:
    """No issuer: no metadata route, a plain challenge, and ``bearer``.

    Args:
        vault: A seeded vault.
    """
    with _standalone(vault, oauth=False) as client:
        metadata = client.get(PROTECTED_RESOURCE_PATH)
        refused = client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(None)
        )
        session = open_session(client)
        handshake = call_handshake(client, session["headers"])

    assert metadata.status_code == _ROUTING_MISS
    assert "resource_metadata" not in refused.headers["www-authenticate"]
    assert handshake["auth"] == AuthMode.BEARER.value


def test_a_resource_server_without_a_verifier_is_refused(vault: Path) -> None:
    """OAuth metadata for a server that authenticates nobody is a contradiction.

    Args:
        vault: A seeded vault.
    """
    with pytest.raises(ValueError, match="resource_server"):
        build_server(
            transport=Transport.NETWORK,
            vault_path=vault,
            resource_server=resource_config(),
        )


def test_a_local_server_reports_no_auth(vault: Path) -> None:
    """Stdio authenticates nobody, and says so rather than claiming ``bearer``.

    Args:
        vault: A seeded vault.
    """
    server = build_server(transport=Transport.STDIO, vault_path=vault)
    result = asyncio.run(server.call_tool("creek.handshake", {}))
    structured = result[1] if isinstance(result, tuple) else result

    assert structured["auth"] == AuthMode.NONE.value == "none"


class _Served:
    """Record what :func:`creek_mcp.server.main` would have served."""

    def __init__(self) -> None:
        """Start empty."""
        self.servers: list[Any] = []

    def __call__(self, server: Any, args: argparse.Namespace) -> None:
        """Capture *server* instead of binding a socket.

        Args:
            server: The built server.
            args: Unread.
        """
        del args
        self.servers.append(server)


def test_main_serves_oauth_from_the_environment(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network entry point reads the two settings and serves OAuth.

    An OAuth vault needs no static consumer at all: an agent's JWT is the
    credential.

    Args:
        vault: A seeded vault.
        monkeypatch: Pytest's monkeypatch.
    """
    served = _Served()
    monkeypatch.setattr(server_mod, "_serve_network", served)
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    monkeypatch.setenv(RESOURCE_URL_ENV, "http://127.0.0.1:8000/mcp")
    monkeypatch.setenv(AUTHORIZATION_SERVER_ENV, ISSUER)
    monkeypatch.setenv("CREEK_VAULT_PATH", str(vault))
    monkeypatch.setattr(server_mod, "_resolve_vault", lambda _path: vault)

    server_mod.main(["--transport", "network"])

    assert len(served.servers) == 1
    built = served.servers[0]
    assert built.settings.auth is not None
    assert str(built.settings.auth.resource_server_url) == "http://127.0.0.1:8000/mcp"


def test_main_refuses_a_half_configured_resource_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One setting without the other exits naming both settings.

    Args:
        monkeypatch: Pytest's monkeypatch.
        capsys: Pytest's output capture.
    """
    monkeypatch.setattr(server_mod, "_serve_network", _Served())
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={STRONG_TOKEN}")
    monkeypatch.setenv(RESOURCE_URL_ENV, RESOURCE_URL)
    monkeypatch.delenv(AUTHORIZATION_SERVER_ENV, raising=False)

    with pytest.raises(SystemExit):
        server_mod.main(["--transport", "network"])

    assert AUTHORIZATION_SERVER_ENV in capsys.readouterr().err
