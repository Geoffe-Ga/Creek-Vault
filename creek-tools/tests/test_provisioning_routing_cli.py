"""Production managed-vault routing composition tests for issue #1806."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from starlette.testclient import TestClient

from creek_mcp.provisioning import routing_cli
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import FlyProviderDriver
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileRoutingCredentialVerifier,
)
from creek_mcp.provisioning.routing import FlyReplayTarget, PrivateVaultTarget
from tests.test_provisioning_production_adapters import _ca_files, _owner_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.models import RoutableAllocation

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


def _arguments(tmp_path: Path) -> list[str]:
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    ca_certificate, _ = _ca_files(tmp_path)
    return [
        "--database",
        str(tmp_path / "jobs.sqlite3"),
        "--fly-token-file",
        str(_owner_file(tmp_path / "fly-token", b"fly-routing-token")),
        "--fly-organization",
        "creek-vaults",
        "--fly-image",
        "registry.example/creek@sha256:" + "a" * 64,
        "--fly-token-expires-at",
        "2026-09-15T12:00:00+00:00",
        "--secret-state-directory",
        str(state),
        "--secret-master-key-file",
        str(_owner_file(tmp_path / "master-key", b"m" * 32)),
        "--private-tls-ca-certificate-file",
        str(ca_certificate),
    ]


def test_composition_uses_real_verifier_driver_and_private_tls(
    tmp_path: Path,
) -> None:
    """The installed router has no fake verifier/provider or plaintext dial path."""
    parser = routing_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    bundle = routing_cli.compose(
        args,
        parser,
        clock=lambda: _NOW,
        provider_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
        private_transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request)
        ),
    )

    assert isinstance(bundle.driver, FlyProviderDriver)
    assert isinstance(bundle.verifier, EncryptedFileRoutingCredentialVerifier)
    assert bundle.fly_token_expires_at == _NOW + timedelta(days=1)
    assert bundle.minimum_route_validity == timedelta(seconds=290)
    assert "fly-routing-token" not in repr(bundle)

    with TestClient(bundle.app) as client:
        refusal = client.get("/v1/capabilities")
    assert refusal.status_code == 401
    asyncio.run(bundle.aclose())


def test_fly_edge_composition_uses_replay_without_a_private_tls_client(
    tmp_path: Path,
) -> None:
    """The managed pilot routes cross-network through Fly Proxy, not DNS."""
    parser = routing_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    bundle = routing_cli.compose_replay(
        args,
        parser,
        clock=lambda: _NOW,
        provider_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )

    assert isinstance(bundle.driver, FlyProviderDriver)
    assert isinstance(bundle.verifier, EncryptedFileRoutingCredentialVerifier)
    assert not hasattr(bundle, "_private_client")
    with TestClient(bundle.app) as client:
        refusal = client.get("/v1/capabilities")
    assert refusal.status_code == 401
    bundle.close()


def test_router_refuses_to_start_with_an_unsafe_fly_token_window(
    tmp_path: Path,
) -> None:
    """Startup cannot admit a token too near expiry for one bounded cold start."""
    parser = routing_cli.build_parser()
    arguments = _arguments(tmp_path)
    expiry_index = arguments.index("--fly-token-expires-at") + 1
    arguments[expiry_index] = "2026-09-14T12:04:20+00:00"

    with pytest.raises(SystemExit) as raised:
        routing_cli.compose(
            parser.parse_args(arguments),
            parser,
            clock=lambda: _NOW,
        )

    assert raised.value.code == 2


def test_runtime_expiry_fence_refuses_before_any_provider_call() -> None:
    """A long-running router drains instead of using an expired mounted token."""

    class RecordingProvider:
        calls = 0

        def prepare_route(
            self,
            allocation: RoutableAllocation,
        ) -> PrivateVaultTarget:
            del allocation
            self.calls += 1
            return PrivateVaultTarget("https://machine.vm.app.internal:8823")

    provider = RecordingProvider()
    guarded = routing_cli.ExpiringRoutingProvider(
        provider,
        expires_at=_NOW + timedelta(minutes=4),
        minimum_validity=timedelta(minutes=5),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError):
        guarded.prepare_route(cast("RoutableAllocation", object()))

    assert provider.calls == 0


def test_runtime_expiry_fence_delegates_while_the_bounded_window_is_safe() -> None:
    """A healthy short-lived token still reaches the real routing provider."""

    class RecordingProvider:
        calls = 0

        def prepare_route(
            self,
            allocation: RoutableAllocation,
        ) -> PrivateVaultTarget:
            del allocation
            self.calls += 1
            return PrivateVaultTarget("https://machine.vm.app.internal:8823")

    provider = RecordingProvider()
    guarded = routing_cli.ExpiringRoutingProvider(
        provider,
        expires_at=_NOW + timedelta(minutes=6),
        minimum_validity=timedelta(minutes=5),
        clock=lambda: _NOW,
    )

    target = guarded.prepare_route(cast("RoutableAllocation", object()))

    assert target == PrivateVaultTarget("https://machine.vm.app.internal:8823")
    assert provider.calls == 1


def test_replay_expiry_fence_refuses_before_any_provider_call() -> None:
    """A near-expiry token cannot emit stale cross-network replay coordinates."""

    class RecordingProvider:
        calls = 0

        def prepare_replay(
            self,
            allocation: RoutableAllocation,
        ) -> FlyReplayTarget:
            del allocation
            self.calls += 1
            return FlyReplayTarget("creek-vault", "machine-001")

    provider = RecordingProvider()
    guarded = routing_cli.ExpiringReplayRoutingProvider(
        provider,
        expires_at=_NOW + timedelta(minutes=4),
        minimum_validity=timedelta(minutes=5),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError):
        guarded.prepare_replay(cast("RoutableAllocation", object()))

    assert provider.calls == 0


@pytest.mark.parametrize(
    "value",
    [
        "not-a-timestamp",
        "2026-09-15T12:00:00",
        "2026-09-21T12:00:01+00:00",
    ],
)
def test_router_refuses_unbounded_or_malformed_token_expiry(
    tmp_path: Path,
    value: str,
) -> None:
    """The production router admits only a bounded timezone-aware token."""
    parser = routing_cli.build_parser()
    arguments = _arguments(tmp_path)
    expiry_index = arguments.index("--fly-token-expires-at") + 1
    arguments[expiry_index] = value

    with pytest.raises(SystemExit) as raised:
        routing_cli.compose(
            parser.parse_args(arguments),
            parser,
            clock=lambda: _NOW,
        )

    assert raised.value.code == 2


def test_routable_plaintext_bind_is_refused_before_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public bearer route cannot bind beyond loopback without TLS."""
    served = False

    def fake_serve(*args: object, **kwargs: object) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(routing_cli, "serve", fake_serve)
    with pytest.raises(SystemExit) as raised:
        routing_cli.main(
            [
                "--database",
                str(tmp_path / "jobs.sqlite3"),
                "--fly-token-file",
                str(tmp_path / "missing"),
                "--fly-organization",
                "creek-vaults",
                "--fly-image",
                "registry.example/creek@sha256:" + "a" * 64,
                "--fly-token-expires-at",
                "2026-09-15T12:00:00+00:00",
                "--secret-state-directory",
                str(tmp_path / "missing-state"),
                "--secret-master-key-file",
                str(tmp_path / "missing-master"),
                "--private-tls-ca-certificate-file",
                str(tmp_path / "missing-ca"),
                "--host",
                "0.0.0.0",
            ]
        )

    assert raised.value.code == 2
    assert served is False


def test_main_serves_the_composed_app_and_closes_it_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed entry point always releases both owned client pools."""

    class FakeBundle:
        app = object()
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    bundle = FakeBundle()
    served: list[object] = []

    def fake_compose(*args: object, **kwargs: object) -> routing_cli.RoutingBundle:
        del args, kwargs
        return cast("routing_cli.RoutingBundle", bundle)

    def fake_serve(app: object, args: object) -> None:
        del args
        served.append(app)

    monkeypatch.setattr(routing_cli, "compose", fake_compose)
    monkeypatch.setattr(routing_cli, "serve", fake_serve)

    routing_cli.main(_arguments(tmp_path))

    assert served == [bundle.app]
    assert bundle.closed is True


def _replay_bundle(tmp_path: Path, *extra: str) -> routing_cli.ReplayRoutingBundle:
    """Compose the Fly replay router with *extra* arguments.

    Args:
        tmp_path: Pytest's per-test directory.
        extra: Further command-line arguments.

    Returns:
        The bundle.
    """
    parser = routing_cli.build_parser()
    args = parser.parse_args([*_arguments(tmp_path), *extra])
    return routing_cli.compose_replay(
        args,
        parser,
        clock=lambda: _NOW,
        provider_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )


def test_the_replay_router_routes_agents_once_an_issuer_is_configured(
    tmp_path: Path,
) -> None:
    """``--oauth-*`` turns on the edge's resource-server answers (#1963)."""
    bundle = _replay_bundle(
        tmp_path,
        "--oauth-authorization-server",
        "https://api.adepthood.example",
        "--oauth-resource-url",
        "https://creek-control-pilot.fly.dev/mcp",
    )
    with TestClient(bundle.app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        refused = client.post("/mcp", content=b"{}")
    bundle.close()

    assert metadata.json()["resource"] == "https://creek-control-pilot.fly.dev/mcp"
    assert metadata.json()["authorization_servers"] == ["https://api.adepthood.example"]
    assert "resource_metadata=" in refused.headers["www-authenticate"]


def test_the_replay_router_without_an_issuer_routes_no_agents(tmp_path: Path) -> None:
    """No issuer: the router is exactly the pre-#1963 router."""
    bundle = _replay_bundle(tmp_path)
    with TestClient(bundle.app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    bundle.close()

    assert metadata.status_code == 401


def test_an_issuer_without_a_resource_url_refuses_to_start(tmp_path: Path) -> None:
    """Agent routing with no audience to verify against is a startup refusal."""
    with pytest.raises(SystemExit):
        _replay_bundle(
            tmp_path, "--oauth-authorization-server", "https://api.adepthood.example"
        )


_STATUS_URL = (
    "https://api.adepthood.example/internal/vault-provisioning/agent-token-status"
)
_OAUTH = (
    "--oauth-authorization-server",
    "https://api.adepthood.example",
    "--oauth-resource-url",
    "https://creek-control-pilot.fly.dev/mcp",
)


def _status_flags(tmp_path: Path) -> tuple[str, ...]:
    """Return the edge's revocation-check flags over a mounted bearer (#2005).

    Args:
        tmp_path: Pytest's per-test directory.

    Returns:
        The two flags and their values.
    """
    bearer = _owner_file(tmp_path / "handoff-bearer", b"unit-test-handoff-" + b"h" * 20)
    return (
        "--oauth-agent-token-status-url",
        _STATUS_URL,
        "--oauth-agent-token-status-bearer-file",
        str(bearer),
    )


def test_the_edge_enforces_revocation_once_the_status_flags_are_set(
    tmp_path: Path,
) -> None:
    """Both status flags turn the per-call check on at the edge."""
    parser = routing_cli.build_parser()
    args = parser.parse_args([*_arguments(tmp_path), *_OAUTH, *_status_flags(tmp_path)])

    agent = routing_cli.agent_routing(
        args, cast("EncryptedFileRoutingCredentialVerifier", object())
    )

    assert agent is not None
    assert agent.verifier.revocation_enforced is True


def test_the_edge_without_status_flags_warns_revocation_is_not_enforced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default off, and said so at startup, never silent."""
    parser = routing_cli.build_parser()
    args = parser.parse_args([*_arguments(tmp_path), *_OAUTH])

    with caplog.at_level(logging.WARNING):
        agent = routing_cli.agent_routing(
            args, cast("EncryptedFileRoutingCredentialVerifier", object())
        )

    assert agent is not None
    assert agent.verifier.revocation_enforced is False
    assert "revocation is not enforced" in caplog.text


@pytest.mark.parametrize("keep", [0, 2])
def test_half_the_edge_status_flags_refuse_to_start(tmp_path: Path, keep: int) -> None:
    """A status URL without its bearer (or the reverse) is a startup refusal."""
    flags = _status_flags(tmp_path)[keep : keep + 2]

    with pytest.raises(SystemExit):
        _replay_bundle(tmp_path, *_OAUTH, *flags)


def test_status_flags_without_an_issuer_refuse_to_start(tmp_path: Path) -> None:
    """A revocation check with no agent route to guard is a misconfiguration."""
    with pytest.raises(SystemExit):
        _replay_bundle(tmp_path, *_status_flags(tmp_path))
