"""The vault as an OAuth 2.1 resource server: configuration and RFC 9728 (#1963).

Covers :mod:`creek_mcp.oauth_resource` on its own: the two settings the
control plane writes per allocation, the protected-resource metadata document
built from them, the ``WWW-Authenticate`` challenge that points at it, and the
two ASGI layers that publish both. The end-to-end behaviour on the served
``/mcp`` lives in ``tests/test_mcp_oauth.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from creek_mcp.mcp_path import (
    PROTECTED_RESOURCE_PATH,
    PROTECTED_RESOURCE_PATHS,
    PROTECTED_RESOURCE_ROOT,
    in_metadata_realm,
)
from creek_mcp.oauth_resource import (
    AUTHORIZATION_SERVER_ENV,
    JWKS_URL_ENV,
    RESOURCE_URL_ENV,
    ProtectedResourceMetadataMiddleware,
    ResourceChallengeMiddleware,
    ResourceServerConfig,
    load_resource_server_config,
)
from creek_mcp.policy import DEFAULT_GRANTS, Grant
from tests.agent_token_support import ISSUER, RESOURCE_URL, resource_config

if TYPE_CHECKING:
    from starlette.requests import Request

_METADATA_URL: Final[str] = (
    "https://v-7q2k.creek.example/.well-known/oauth-protected-resource/mcp"
)
_UNAUTHENTICATED: Final[int] = 401
_FORBIDDEN: Final[int] = 403
_OK: Final[int] = 200


def _environ(**values: str) -> dict[str, str]:
    """Return an environment holding exactly *values*, by setting name.

    Args:
        values: ``RESOURCE``, ``ISSUER`` and/or ``JWKS`` values.

    Returns:
        The mapping.
    """
    names = {
        "RESOURCE": RESOURCE_URL_ENV,
        "ISSUER": AUTHORIZATION_SERVER_ENV,
        "JWKS": JWKS_URL_ENV,
    }
    return {names[key]: value for key, value in values.items()}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_the_setting_names_are_the_ones_the_control_plane_writes() -> None:
    """The two per-allocation settings are named exactly as the issue fixes them."""
    assert RESOURCE_URL_ENV == "CREEK_MCP_RESOURCE_URL"
    assert AUTHORIZATION_SERVER_ENV == "CREEK_MCP_AUTHORIZATION_SERVER"
    assert JWKS_URL_ENV == "CREEK_MCP_JWKS_URL"


def test_no_issuer_configured_is_no_resource_server() -> None:
    """A self-hosted vault with neither setting is not an OAuth resource server."""
    assert load_resource_server_config({}) is None
    assert load_resource_server_config(_environ(RESOURCE="  ", ISSUER="")) is None


def test_both_settings_load_verbatim() -> None:
    """The URLs are kept exactly as configured: no slash added, none removed."""
    config = load_resource_server_config(_environ(RESOURCE=RESOURCE_URL, ISSUER=ISSUER))

    assert config is not None
    assert config.resource_url == RESOURCE_URL
    assert config.authorization_server == ISSUER
    assert config.jwks_url == f"{ISSUER}/.well-known/jwks.json"


def test_an_explicit_jwks_url_overrides_the_issuer_default() -> None:
    """An operator may point the key set somewhere other than the issuer root."""
    config = load_resource_server_config(
        _environ(
            RESOURCE=RESOURCE_URL,
            ISSUER=ISSUER,
            JWKS="https://keys.adepthood.example/jwks.json",
        )
    )

    assert config is not None
    assert config.jwks_url == "https://keys.adepthood.example/jwks.json"


def test_an_issuer_with_a_trailing_slash_keeps_one_slash_in_the_jwks_url() -> None:
    """The derived JWKS URL never doubles the separator."""
    config = ResourceServerConfig.build(
        resource_url=RESOURCE_URL, authorization_server=f"{ISSUER}/"
    )

    assert config.authorization_server == f"{ISSUER}/"
    assert config.jwks_url == f"{ISSUER}/.well-known/jwks.json"


@pytest.mark.parametrize(
    "values",
    [
        {"RESOURCE": RESOURCE_URL},
        {"ISSUER": ISSUER},
        {"JWKS": "https://keys.adepthood.example/jwks.json"},
        {"RESOURCE": RESOURCE_URL, "JWKS": "https://keys.adepthood.example/j"},
    ],
)
def test_a_half_configured_resource_server_is_refused(values: dict[str, str]) -> None:
    """One setting without its partner fails closed rather than serving half OAuth.

    Args:
        values: A partial configuration.
    """
    with pytest.raises(ValueError, match="set together") as refused:
        load_resource_server_config(_environ(**values))

    assert RESOURCE_URL_ENV in str(refused.value)
    assert AUTHORIZATION_SERVER_ENV in str(refused.value)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("RESOURCE", "http://v-7q2k.creek.example/mcp"),
        ("RESOURCE", "https://v-7q2k.creek.example/v1"),
        ("RESOURCE", "https://v-7q2k.creek.example/"),
        ("RESOURCE", "https://v-7q2k.creek.example/mcp?x=1"),
        ("RESOURCE", "https://v-7q2k.creek.example/mcp#frag"),
        ("RESOURCE", "https://user:" + "pw" + "@v-7q2k.creek.example/mcp"),
        ("RESOURCE", "https:///mcp"),
        ("RESOURCE", 'https://v-7q2k.creek.example/mcp"x'),
        ("RESOURCE", "https://v-7q2k.creek.example/m cp"),
        ("RESOURCE", "https://[::1/mcp"),
        ("ISSUER", "http://api.adepthood.example"),
        ("ISSUER", "https://api.adepthood.example?tenant=1"),
        ("ISSUER", "ftp://api.adepthood.example"),
        ("ISSUER", "not a url"),
        ("JWKS", "http://keys.adepthood.example/jwks.json"),
    ],
)
def test_an_unsafe_url_is_refused_without_echoing_it(setting: str, value: str) -> None:
    """Only clean HTTPS URLs are admitted, and the refusal names the setting only.

    Args:
        setting: Which setting carries the bad value.
        value: The bad value.
    """
    good = {"RESOURCE": RESOURCE_URL, "ISSUER": ISSUER}
    good[setting] = value

    with pytest.raises(ValueError, match="CREEK_MCP_") as refused:
        load_resource_server_config(_environ(**good))

    assert value not in str(refused.value)


@pytest.mark.parametrize(
    "resource",
    ["http://127.0.0.1:8000/mcp", "http://localhost:8000/mcp", "http://[::1]/mcp"],
)
def test_a_loopback_resource_may_be_plain_http(resource: str) -> None:
    """A self-hosted vault bound to loopback serves plain HTTP; its URL says so.

    Args:
        resource: A loopback resource URL.
    """
    config = ResourceServerConfig.build(
        resource_url=resource, authorization_server=ISSUER
    )

    assert config.resource_url == resource


def test_the_config_is_frozen() -> None:
    """Nothing may re-point a built configuration at another vault."""
    config = resource_config()

    with pytest.raises(AttributeError):
        config.resource_url = "https://elsewhere.example/mcp"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# The metadata document
# --------------------------------------------------------------------------- #


def test_the_metadata_paths_are_the_rfc_9728_insertion_and_the_root() -> None:
    """The well-known segment plus the resource path, and the bare root."""
    assert PROTECTED_RESOURCE_ROOT == "/.well-known/oauth-protected-resource"
    assert PROTECTED_RESOURCE_PATH == "/.well-known/oauth-protected-resource/mcp"
    assert {
        PROTECTED_RESOURCE_PATH,
        PROTECTED_RESOURCE_ROOT,
    } == PROTECTED_RESOURCE_PATHS


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (PROTECTED_RESOURCE_PATH, True),
        (PROTECTED_RESOURCE_ROOT, True),
        (f"{PROTECTED_RESOURCE_PATH}/", False),
        (f"{PROTECTED_RESOURCE_ROOT}/v1", False),
        ("/.well-known/oauth-authorization-server", False),
        (None, False),
        (b"/.well-known/oauth-protected-resource", False),
    ],
)
def test_the_metadata_realm_is_exact(path: object, expected: bool) -> None:
    """Only the two exact paths are metadata; a lookalike is not.

    Args:
        path: A candidate ASGI path.
        expected: Whether it is a metadata path.
    """
    assert in_metadata_realm(path) is expected


def test_the_metadata_url_inserts_the_well_known_segment() -> None:
    """RFC 9728 section 3.1: host, then the well-known segment, then the path."""
    assert resource_config().metadata_url == _METADATA_URL


def test_the_metadata_document_matches_the_issue_shape() -> None:
    """Resource, issuer, the four grants in declaration order, header bearers."""
    document = resource_config().metadata_document()

    assert document == {
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
    assert document["scopes_supported"] == [grant.value for grant in Grant]


def test_the_metadata_never_advertises_the_internal_scopes() -> None:
    """``creek:remote`` and ``creek:ungated`` are server-side markers, not grants."""
    text = json.dumps(resource_config().metadata_document())

    assert "creek:" not in text


# --------------------------------------------------------------------------- #
# The challenge
# --------------------------------------------------------------------------- #


def test_the_401_challenge_points_at_the_metadata_and_names_the_default_scope() -> None:
    """``Bearer resource_metadata="…", scope="…"`` with the RFC 6750 error code."""
    challenge = resource_config().challenge(_UNAUTHENTICATED)

    assert challenge == (
        'Bearer error="invalid_token", '
        f'resource_metadata="{_METADATA_URL}", '
        'scope="vault:read vault:desk"'
    )


def test_the_403_challenge_is_insufficient_scope() -> None:
    """An authenticated caller holding no grant is told which scope to ask for."""
    challenge = resource_config().challenge(_FORBIDDEN)

    assert challenge.startswith('Bearer error="insufficient_scope", ')
    assert f'resource_metadata="{_METADATA_URL}"' in challenge


def test_the_challenge_scope_is_the_default_profile() -> None:
    """The scope hint is the curated default, in grant declaration order."""
    challenge = resource_config().challenge(_UNAUTHENTICATED)
    default = " ".join(grant.value for grant in Grant if grant in DEFAULT_GRANTS)

    assert f'scope="{default}"' in challenge


# --------------------------------------------------------------------------- #
# The two ASGI layers
# --------------------------------------------------------------------------- #


async def _downstream(_request: Request) -> Response:
    """Answer whatever reaches the wrapped app, so a pass-through is visible.

    Args:
        _request: Unread.

    Returns:
        A marker response.
    """
    return JSONResponse({"reached": "downstream"})


def _metadata_app() -> Starlette:
    """Return an app with the metadata layer above a catch-all route.

    Returns:
        The app.
    """
    return Starlette(
        routes=[Route("/{path:path}", _downstream, methods=["GET", "POST"])],
        middleware=[
            Middleware(ProtectedResourceMetadataMiddleware, config=resource_config())
        ],
    )


@pytest.mark.parametrize("path", sorted(PROTECTED_RESOURCE_PATHS))
def test_the_metadata_is_served_unauthenticated_at_both_paths(path: str) -> None:
    """``GET`` on either metadata path answers the document, with no credential.

    Args:
        path: A metadata path.
    """
    with TestClient(_metadata_app()) as client:
        response = client.get(path)

    assert response.status_code == _OK
    assert response.headers["content-type"] == "application/json"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.json() == resource_config().metadata_document()


def test_other_methods_and_paths_pass_through() -> None:
    """Only ``GET`` on the exact paths is answered here; the rest goes below."""
    with TestClient(_metadata_app()) as client:
        posted = client.post(PROTECTED_RESOURCE_PATH)
        beside = client.get(f"{PROTECTED_RESOURCE_PATH}/x")
        mcp = client.get("/mcp")

    for response in (posted, beside, mcp):
        assert response.json() == {"reached": "downstream"}


async def _refuse(request: Request) -> Response:
    """Answer the status the path names, with the SDK's own challenge.

    Args:
        request: The request; its last path segment is the status.

    Returns:
        A refusal carrying a challenge to be rewritten.
    """
    status = int(request.path_params["status"])
    return JSONResponse(
        {"error": "x"},
        status_code=status,
        headers={"WWW-Authenticate": 'Bearer error="sdk"'},
    )


def _challenge_app(paths: frozenset[str] | None = None) -> Starlette:
    """Return an app with the challenge layer above a status-echoing route.

    Args:
        paths: The paths the layer is confined to, or ``None`` for all.

    Returns:
        The app.
    """
    return Starlette(
        routes=[Route("/{prefix}/{status:int}", _refuse)],
        middleware=[
            Middleware(
                ResourceChallengeMiddleware, config=resource_config(), paths=paths
            )
        ],
    )


@pytest.mark.parametrize("status", [_UNAUTHENTICATED, _FORBIDDEN])
def test_a_refusal_carries_exactly_one_rewritten_challenge(status: int) -> None:
    """The inner challenge is replaced, never duplicated beside ours.

    Args:
        status: A refusal status.
    """
    with TestClient(_challenge_app()) as client:
        response = client.get(f"/mcp/{status}")

    challenges = [
        value
        for name, value in response.headers.multi_items()
        if name == "www-authenticate"
    ]
    assert response.status_code == status
    assert challenges == [resource_config().challenge(status)]


def test_a_non_refusal_is_untouched() -> None:
    """A ``200`` keeps whatever headers it had."""
    with TestClient(_challenge_app()) as client:
        response = client.get(f"/mcp/{_OK}")

    assert response.headers["www-authenticate"] == 'Bearer error="sdk"'


def test_a_confined_layer_leaves_other_paths_alone() -> None:
    """On the router, only ``/mcp`` gets the OAuth challenge; ``/v1`` keeps its own."""
    with TestClient(_challenge_app(frozenset({"/mcp/401"}))) as client:
        mcp = client.get("/mcp/401")
        v1 = client.get("/v1/401")

    assert mcp.headers["www-authenticate"] == resource_config().challenge(
        _UNAUTHENTICATED
    )
    assert v1.headers["www-authenticate"] == 'Bearer error="sdk"'


def test_the_layers_pass_non_http_scopes_through() -> None:
    """A lifespan scope reaches the app below both layers unchanged."""
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])
        del receive, send

    metadata = ProtectedResourceMetadataMiddleware(inner, config=resource_config())
    challenge = ResourceChallengeMiddleware(inner, config=resource_config())

    async def drive() -> None:
        await metadata({"type": "lifespan"}, _never, _discard)
        await challenge({"type": "lifespan"}, _never, _discard)

    asyncio.run(drive())

    assert seen == ["lifespan", "lifespan"]


async def _never() -> dict[str, Any]:
    """Receive nothing.

    Returns:
        A disconnect.
    """
    return {"type": "http.disconnect"}


async def _discard(_message: Any) -> None:
    """Drop a sent message.

    Args:
        _message: Unread.
    """
