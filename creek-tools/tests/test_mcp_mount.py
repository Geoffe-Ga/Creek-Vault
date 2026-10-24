"""The MCP streamable-HTTP surface at ``/mcp`` inside the ``/v1`` process (#1962).

Every test here drives :func:`creek_mcp.httpapi.app.create_app` — the factory the
container entry points serve — rather than the SDK's ``streamable_http_app()``
on its own, which is all ``tests/test_mcp_remote.py`` ever reached. The point of
#1962 is that the two adapters share one process, one port and one verifier, so
the properties are asserted on the combined app or not at all.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from creek_mcp.contract import CONTRACT_VERSION
from creek_mcp.httpapi import app as app_module
from creek_mcp.httpapi import mcp_mount
from creek_mcp.httpapi.middleware.access_log import (
    ACCESS_LOGGER_NAME,
    AccessLogMiddleware,
)
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.httpapi.middleware.fly_replay import FlyReplayStateMiddleware
from creek_mcp.httpapi.middleware.limits import ConcurrencyLimitMiddleware
from creek_mcp.mcp_path import MCP_PATH, in_mcp_realm
from creek_mcp.policy import Transport
from creek_mcp.server import SERVER_NAME
from tests.mcp_http_support import (
    call_handshake,
    initialize_body,
    mcp_headers,
    open_session,
    sse_payload,
)
from tests.v1_api_support import (
    ANONYMOUS_CONSUMER,
    CAPABILITIES_PATH,
    CONSUMER,
    HEALTH_PATH,
    STRONG_TOKEN,
    UNKNOWN_TOKEN,
    build_app,
    headers,
    seed_vault,
    verifier,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from creek_mcp.remote_auth import ConsumerTokenVerifier


_REPLAY_STATE: Final[str] = "s" * 64
"""A closed-shape Fly replay state, as the provisioning plane mounts one."""

_UNAUTHENTICATED: Final[int] = 401
_OK: Final[int] = 200
_ROUTING_MISS: Final[int] = 404
_INTERNAL_ERROR: Final[int] = 500


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault.

    Args:
        tmp_path: Pytest's per-test directory.

    Yields:
        The vault root.
    """
    yield seed_vault(tmp_path)


# --------------------------------------------------------------------------- #
# The path predicate the vault and the edge share
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp", True),
        ("/mcp/", True),
        ("/mcp/anything", True),
        ("/mcpx", False),
        ("/mc", False),
        ("/v1/mcp", False),
        ("/", False),
        ("", False),
        (None, False),
        (b"/mcp", False),
    ],
)
def test_the_mcp_realm_is_the_exact_path_and_its_subtree(
    path: object, expected: bool
) -> None:
    """``/mcp`` and ``/mcp/...`` are the realm; a lookalike prefix is not.

    Args:
        path: A candidate ASGI ``scope["path"]`` value.
        expected: Whether it is in the MCP realm.
    """
    assert in_mcp_realm(path) is expected


def test_the_mcp_path_is_the_sdk_default_and_carries_no_version_segment() -> None:
    """Client docs say ``/mcp``; the SDK says ``/mcp``; so does this server."""
    assert MCP_PATH == "/mcp"


# --------------------------------------------------------------------------- #
# The handshake through create_app()
# --------------------------------------------------------------------------- #


def test_create_app_completes_an_mcp_handshake_at_mcp(vault: Path) -> None:
    """``initialize`` then ``creek.handshake`` succeed against the combined app.

    The bearer is the suite's ``/v1`` consumer token, the request carries no
    ``/v1`` contract-version header, and the handshake reports the network
    transport — the three things #1962's example promises.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as test_client:
        session = open_session(test_client)
        handshake = call_handshake(test_client, session["headers"])

    assert session["initialize"]["serverInfo"]["name"] == SERVER_NAME
    assert handshake["transport"] == Transport.NETWORK.value
    assert handshake["contract_version"] == CONTRACT_VERSION


def test_an_unauthenticated_mcp_request_is_401_and_names_no_vault(
    vault: Path,
) -> None:
    """No bearer and a wrong bearer are both ``401``, and say nothing of the vault.

    This is the response C2 extends with RFC 9728 metadata, so it is pinned at
    the status and at what it must never carry — not at its exact bytes.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as test_client:
        missing = test_client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(None)
        )
        wrong = test_client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers(UNKNOWN_TOKEN)
        )

    for response in (missing, wrong):
        assert response.status_code == _UNAUTHENTICATED
        assert str(vault) not in response.text
        assert vault.name not in response.text
        assert "mcp-session-id" not in response.headers
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_the_v1_version_gate_does_not_apply_at_mcp_but_still_applies_at_v1(
    vault: Path,
) -> None:
    """MCP negotiates its own version; ``/v1`` still refuses a silent minor.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as test_client:
        mcp = test_client.post(MCP_PATH, json=initialize_body(), headers=mcp_headers())
        silent = test_client.get(CAPABILITIES_PATH, headers=headers(minor=None))
        health = test_client.get(HEALTH_PATH, headers=headers(minor=None))

    assert mcp.status_code == _OK
    assert silent.status_code == _OK
    assert health.status_code == _OK


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/extra", "/mcpx"])
def test_paths_beside_mcp_answer_the_existing_v1_routing_miss(
    vault: Path, path: str
) -> None:
    """Only the exact path is MCP inside the vault; the rest is ``/v1``'s miss.

    Unauthenticated, the ``/v1`` bearer gate answers first, so a caller cannot
    read the route table without a credential; authenticated, it is the one
    published ``404 not_found``.

    Args:
        vault: A seeded vault.
        path: A path beside, but not equal to, ``/mcp``.
    """
    with TestClient(build_app(vault_path=vault)) as test_client:
        anonymous = test_client.post(path, json={}, headers=headers(token=None))
        authenticated = test_client.post(path, json={}, headers=headers())

    assert anonymous.status_code == _UNAUTHENTICATED
    assert anonymous.json()["code"] == "unauthenticated"
    assert authenticated.status_code == _ROUTING_MISS
    assert authenticated.json()["code"] == "not_found"


# --------------------------------------------------------------------------- #
# One verifier instance, never two registries
# --------------------------------------------------------------------------- #


def test_the_mcp_surface_is_built_with_the_v1_verifier_instance(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier handed to ``create_app`` is the one ``build_server`` gets.

    Identity, not equality: a second verifier over the same map would still be
    a second registry, free to be rotated without the first.

    Args:
        vault: A seeded vault.
        monkeypatch: Wraps the build seam to record what it was handed.
    """
    seen: list[object] = []
    real = mcp_mount.build_mcp_server

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs["verifier"])
        return real(**kwargs)

    monkeypatch.setattr(app_module, "build_mcp_server", recording)
    shared = verifier()

    app_module.create_app(vault_path=vault, verifier=shared)

    assert len(seen) == 1
    assert seen[0] is shared


def test_an_environment_built_verifier_is_built_once_and_shared(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no verifier injected, one is built — once — and both adapters use it.

    Args:
        vault: A seeded vault.
        monkeypatch: Counts builds of the verifier and records the mount's.
    """
    built: list[ConsumerTokenVerifier] = []
    seen: list[object] = []
    real = mcp_mount.build_mcp_server

    def building() -> ConsumerTokenVerifier:
        built.append(verifier())
        return built[-1]

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs["verifier"])
        return real(**kwargs)

    monkeypatch.setattr(app_module, "build_verifier", building)
    monkeypatch.setattr(app_module, "build_mcp_server", recording)

    app = app_module.create_app(vault_path=vault)

    assert len(built) == 1
    assert seen == built
    assert app.state.mcp_server is not None


def test_one_bearer_authenticates_both_adapters_in_one_process(vault: Path) -> None:
    """The ``/v1`` consumer token opens ``/v1`` and ``/mcp`` on the same app.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as test_client:
        health = test_client.get(HEALTH_PATH, headers=headers())
        session = open_session(test_client)

    assert health.status_code == _OK
    assert session["initialize"]["serverInfo"]["name"] == SERVER_NAME


# --------------------------------------------------------------------------- #
# Inside the access log and the error boundary
# --------------------------------------------------------------------------- #


def test_mcp_requests_are_access_logged_under_the_verified_consumer(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One access line per ``/mcp`` request, naming the route and the consumer.

    Args:
        vault: A seeded vault.
        caplog: Captures the access logger.
    """
    with (
        caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME),
        TestClient(build_app(vault_path=vault)) as test_client,
    ):
        test_client.post(MCP_PATH, json=initialize_body(), headers=mcp_headers())
        test_client.post(MCP_PATH, json=initialize_body(), headers=mcp_headers(None))

    lines = [
        record.__dict__
        for record in caplog.records
        if record.name == ACCESS_LOGGER_NAME
    ]
    assert [(line["route"], line["consumer"], line["status"]) for line in lines] == [
        (MCP_PATH, CONSUMER, _OK),
        (MCP_PATH, ANONYMOUS_CONSUMER, _UNAUTHENTICATED),
    ]


def test_a_fault_inside_the_mcp_surface_is_enveloped_by_the_error_boundary(
    vault: Path,
) -> None:
    """A raise below the mount reaches the boundary and leaves as the ``500``.

    The mount's own MCP app is swapped for a raiser before the stack is first
    built — Starlette assembles it lazily — so the fault crosses exactly the
    layers production wraps around the SDK.

    Args:
        vault: A seeded vault.
    """

    async def raiser(_scope: Scope, _receive: Receive, _send: Send) -> None:
        msg = "simulated fault inside the MCP surface"
        raise RuntimeError(msg)

    app = build_app(vault_path=vault)
    (mount,) = (
        entry
        for entry in app.user_middleware
        if entry.cls is mcp_mount.McpMountMiddleware
    )
    mount.kwargs["mcp_app"] = raiser
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers()
        )

    assert response.status_code == _INTERNAL_ERROR
    assert response.json()["code"] == "internal_error"
    assert "simulated fault" not in response.text


def test_the_mount_sits_between_the_boundary_and_the_v1_limits(vault: Path) -> None:
    """Below the access log and the boundary; above everything ``/v1``-specific.

    Args:
        vault: A seeded vault.
    """
    classes = [entry.cls for entry in build_app(vault_path=vault).user_middleware]

    assert classes[:3] == [
        AccessLogMiddleware,
        ErrorBoundaryMiddleware,
        mcp_mount.McpMountMiddleware,
    ]


# --------------------------------------------------------------------------- #
# The Fly replay guard covers /mcp too
# --------------------------------------------------------------------------- #


def test_a_fly_vault_refuses_a_direct_mcp_request_and_serves_a_replayed_one(
    vault: Path,
) -> None:
    """The bearer alone never bypasses the router, on ``/mcp`` as on ``/v1``.

    Args:
        vault: A seeded vault.
    """
    replay = f"instance=router-machine;region=iad;t=1727395200;state={_REPLAY_STATE}"
    with TestClient(
        build_app(vault_path=vault, fly_replay_state=_REPLAY_STATE)
    ) as test_client:
        direct = test_client.post(
            MCP_PATH, json=initialize_body(), headers=mcp_headers()
        )
        replayed = test_client.post(
            MCP_PATH,
            json=initialize_body(),
            headers={**mcp_headers(), "Fly-Replay-Src": replay},
        )

    assert direct.status_code == _UNAUTHENTICATED
    assert "mcp-session-id" not in direct.headers
    assert _REPLAY_STATE not in direct.text
    assert replayed.status_code == _OK
    assert sse_payload(replayed.text)["result"]["serverInfo"]["name"] == SERVER_NAME


# --------------------------------------------------------------------------- #
# The mount's own composition, and what it hands the SDK
# --------------------------------------------------------------------------- #


class _RecordingApp:
    """Stand in for the SDK app: record the scope, answer ``200``."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.scopes: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record *scope* and answer an empty ``200``.

        Args:
            scope: The ASGI scope the mount handed down.
            receive: The ASGI receive channel, unread.
            send: The ASGI send channel.
        """
        del receive
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": _OK, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _mounted(recorder: _RecordingApp, *, fly_replay_state: str | None) -> Starlette:
    """Return a minimal app: access log, boundary, then the mount over *recorder*.

    Args:
        recorder: The stand-in MCP app.
        fly_replay_state: The replay state to require, or ``None``.

    Returns:
        A Starlette app whose only ``/mcp`` answer is *recorder*'s.
    """
    from starlette.middleware import Middleware

    return Starlette(
        middleware=[
            Middleware(AccessLogMiddleware),
            Middleware(ErrorBoundaryMiddleware),
            Middleware(
                mcp_mount.McpMountMiddleware,
                mcp_app=recorder,
                max_concurrency=1,
                fly_replay_state=fly_replay_state,
            ),
        ]
    )


def test_client_headers_reach_the_mcp_surface_untouched() -> None:
    """Every header the agent sent is on the scope the SDK sees (C6's seam).

    ``X-Creek-Provider`` stands for the request-scoped BYOK headers #1967 reads
    from the MCP request: the mount must not filter, rewrite or reorder them.
    """
    recorder = _RecordingApp()
    sent = {"X-Creek-Provider": "anthropic", "Authorization": f"Bearer {STRONG_TOKEN}"}

    response = TestClient(_mounted(recorder, fly_replay_state=None)).post(
        MCP_PATH, headers=sent
    )

    assert response.status_code == _OK
    (scope,) = recorder.scopes
    received = {name.decode(): value.decode() for name, value in scope["headers"]}
    assert received["x-creek-provider"] == "anthropic"
    assert received["authorization"] == f"Bearer {STRONG_TOKEN}"
    assert scope["path"] == MCP_PATH


def test_the_trusted_replay_header_is_stripped_before_the_mcp_surface() -> None:
    """The Fly replay source is consumed by the guard, never shown to tools."""
    recorder = _RecordingApp()
    replay = f"instance=router-machine;region=iad;t=1727395200;state={_REPLAY_STATE}"

    response = TestClient(_mounted(recorder, fly_replay_state=_REPLAY_STATE)).post(
        MCP_PATH, headers={"Fly-Replay-Src": replay, "X-Creek-Provider": "openai"}
    )

    assert response.status_code == _OK
    (scope,) = recorder.scopes
    names = {name.decode() for name, _value in scope["headers"]}
    assert "fly-replay-src" not in names
    assert "x-creek-provider" in names


def test_the_mcp_surface_sheds_beyond_its_own_concurrency_pool() -> None:
    """The mount carries its own limiter, so ``/mcp`` cannot hold ``/v1``'s slots."""
    mount = mcp_mount.McpMountMiddleware(
        _RecordingApp(),
        mcp_app=_RecordingApp(),
        max_concurrency=1,
        fly_replay_state=None,
    )

    assert isinstance(mount.surface, ConcurrencyLimitMiddleware)


def test_the_fly_guard_sits_inside_the_mcp_concurrency_pool() -> None:
    """Shed first, then the replay check — the same order ``/v1`` uses."""
    mount = mcp_mount.McpMountMiddleware(
        _RecordingApp(),
        mcp_app=_RecordingApp(),
        max_concurrency=1,
        fly_replay_state=_REPLAY_STATE,
    )

    assert isinstance(mount.surface, ConcurrencyLimitMiddleware)
    assert isinstance(mount.surface.app, FlyReplayStateMiddleware)


# --------------------------------------------------------------------------- #
# /v1 is unchanged for a caller that never touches /mcp
# --------------------------------------------------------------------------- #


def test_capabilities_bytes_are_identical_with_and_without_mcp_traffic(
    vault: Path,
) -> None:
    """``GET /v1/capabilities`` is the same document either side of an MCP session.

    Args:
        vault: A seeded vault.
    """
    with TestClient(build_app(vault_path=vault)) as quiet:
        before = quiet.get(CAPABILITIES_PATH, headers=headers())
    with TestClient(build_app(vault_path=vault)) as busy:
        call_handshake(busy, open_session(busy)["headers"])
        after = busy.get(CAPABILITIES_PATH, headers=headers())

    assert before.status_code == after.status_code == _OK
    assert before.content == after.content
    assert "mcp" not in json.loads(before.content)["capabilities"]


# --------------------------------------------------------------------------- #
# A vault that cannot be resolved at boot leaves /v1 serving
# --------------------------------------------------------------------------- #


def test_an_unreadable_config_at_boot_leaves_v1_up_and_mcp_unmounted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``/v1`` keeps its "repair the config, retry" promise; ``/mcp`` is not served.

    The MCP surface binds its vault once, at build, as ``creek-tools-mcp``
    always has (#1409). A config nobody can read leaves it nothing to bind, so
    it is not mounted — and ``/mcp`` then answers exactly as an unrouted path.

    Args:
        monkeypatch: Makes configuration unreadable.
        caplog: Captures the operator warning.
    """
    from creek_mcp.httpapi import vault as vault_module

    def unreadable() -> None:
        msg = "config is unreadable"
        raise ValueError(msg)

    monkeypatch.setattr(vault_module, "load_config", unreadable)

    with caplog.at_level(logging.WARNING, logger=mcp_mount.MCP_MOUNT_LOGGER_NAME):
        app = build_app(vault_path=None)
    with TestClient(app) as test_client:
        capabilities = test_client.get(CAPABILITIES_PATH, headers=headers())
        mcp = test_client.post(MCP_PATH, json=initialize_body(), headers=mcp_headers())

    assert app.state.mcp_server is None
    assert mcp_mount.McpMountMiddleware not in [
        entry.cls for entry in app.user_middleware
    ]
    assert capabilities.status_code == _OK
    assert mcp.status_code == _ROUTING_MISS
    assert any("not mounted" in record.getMessage() for record in caplog.records)


def test_a_configured_vault_is_resolved_once_for_the_mcp_surface(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit vault, the MCP surface binds the one the config names.

    Args:
        vault: A seeded vault, named by the stubbed configuration.
        monkeypatch: Stubs configuration to name *vault*.
    """
    from types import SimpleNamespace

    from creek_mcp.httpapi import vault as vault_module

    monkeypatch.setattr(
        vault_module, "load_config", lambda: SimpleNamespace(vault_path=vault)
    )
    seen: list[object] = []
    real = mcp_mount.build_mcp_server

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs["vault_path"])
        return real(**kwargs)

    monkeypatch.setattr(app_module, "build_mcp_server", recording)

    app = build_app(vault_path=None)

    assert seen == [vault]
    assert app.state.vault_path is None
