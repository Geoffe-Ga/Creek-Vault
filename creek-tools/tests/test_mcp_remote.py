"""Network transport + per-consumer bearer-token auth for the MCP server (#759).

Security properties under test:

- **No anonymous access** — the network transport refuses to start without
  ``CREEK_MCP_CONSUMER_TOKENS``, and an unauthenticated / bad-token request over
  the wire gets ``401`` before any tool runs.
- **INTIMATE is never reachable remotely** — an authenticated remote caller that
  requests a ceiling above ``personal`` (``intimate`` / ``all``, or any
  unrecognised value) is *refused before dispatch*, so intimate content is never
  even read for a network consumer. ``open`` / ``personal`` pass through.
- **Per-consumer identity** — a remote call is audited under the consumer its
  bearer token identifies, not the process-env default.
- **Stdio is unchanged** — with no verifier wired, ``get_access_token()`` is
  ``None`` so the cap never engages and a local ``intimate`` call dispatches.
- **Tokens expire** (#837) — a verified bearer carries a finite ``expires_at``
  (default TTL 3600s; ``CREEK_MCP_TOKEN_TTL_SECONDS`` overrides; invalid values
  fall back to the default), never ``None``.
- **No plaintext on routable interfaces** (#837) — a non-loopback ``--host``
  refuses to serve unless ``--tls-cert``/``--tls-key`` are configured; loopback
  binds and stdio keep working without TLS.
- **Weak tokens are refused at startup** (#838) — a configured consumer token
  shorter than 32 chars fails ``load_consumer_tokens`` (and the network
  transport, via ``parser.error``) with a rotation recipe that names the
  consumer and lengths but never echoes the token value.

No real/production token material is hardcoded — every token is a test literal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from creek_mcp import remote_auth as remote_auth_mod
from creek_mcp import server as server_mod
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    REMOTE_SCOPE,
    ConsumerTokenVerifier,
    load_consumer_tokens,
)
from creek_mcp.server import build_server, main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the remote-transport tests."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "creek-skills",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _structured(result: object) -> dict[str, object]:
    """Pull the structured-content dict out of a FastMCP ``call_tool`` result."""
    return result[1] if isinstance(result, tuple) else result  # type: ignore[return-value, index]


def _fake_token(consumer: str) -> AccessToken:
    """Build an :class:`AccessToken` as the verifier would for a valid bearer."""
    return AccessToken(
        token="opaque",  # test literal, not a real credential
        client_id=consumer,
        scopes=[REMOTE_SCOPE],
        expires_at=None,
    )


# 43 chars — clears the 32-char minimum (#838). Low-entropy test literal,
# not a real credential.
_STRONG_TOKEN = "unit-test-strong-token-" + "a" * 20


# --------------------------------------------------------------------------- #
# load_consumer_tokens — parsing
# --------------------------------------------------------------------------- #


def test_load_consumer_tokens_parses_pairs() -> None:
    """Well-formed ``consumer=token`` pairs parse into a map."""
    adepthood_token = "adepthood-strong-token-" + "a" * 20  # 43 chars, test literal
    crawdad_token = "crawdad-strong-token-" + "b" * 20  # 41 chars, test literal
    env = {CONSUMER_TOKENS_ENV: f"adepthood={adepthood_token};crawdad={crawdad_token}"}
    assert load_consumer_tokens(env) == {
        "adepthood": adepthood_token,
        "crawdad": crawdad_token,
    }


def test_load_consumer_tokens_skips_blank_and_tokenless_entries() -> None:
    """Blank segments and ``name=`` entries with no token are dropped."""
    env = {CONSUMER_TOKENS_ENV: f" adepthood = {_STRONG_TOKEN} ; ; missing= ;=orphan"}
    assert load_consumer_tokens(env) == {"adepthood": _STRONG_TOKEN}


def test_load_consumer_tokens_rejects_sub_minimum_token() -> None:
    """A present token shorter than 32 chars raises, naming everything but the token."""
    env = {CONSUMER_TOKENS_ENV: "adepthood=hunter2"}  # 7 chars, test literal
    with pytest.raises(ValueError, match="adepthood") as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert "'adepthood'" in message  # consumer named via repr
    assert "7" in message  # observed length of the rejected token
    assert "32" in message  # the enforced minimum
    assert "secrets.token_urlsafe(32)" in message  # the rotation recipe
    assert "hunter2" not in message  # NEVER echo the token value


def test_load_consumer_tokens_accepts_minimum_length_token() -> None:
    """A token at or above the 32-char minimum is accepted and returned intact."""
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_STRONG_TOKEN}"}
    assert load_consumer_tokens(env) == {"adepthood": _STRONG_TOKEN}


def test_load_consumer_tokens_accepts_exact_boundary_token() -> None:
    """A token of exactly 32 chars sits on the floor and is accepted."""
    boundary_token = "a" * 32
    env = {CONSUMER_TOKENS_ENV: f"adepthood={boundary_token}"}
    assert load_consumer_tokens(env) == {"adepthood": boundary_token}


def test_load_consumer_tokens_skips_blanks_without_raising() -> None:
    """Blank and bare ``name=`` entries stay silently skipped, not length-checked."""
    env = {CONSUMER_TOKENS_ENV: f";;missing=;=orphan;adepthood={_STRONG_TOKEN}"}
    assert load_consumer_tokens(env) == {"adepthood": _STRONG_TOKEN}


def test_load_consumer_tokens_empty_when_unset() -> None:
    """An unset env var yields an empty map (caller treats as 'network denied')."""
    assert load_consumer_tokens({}) == {}


# --------------------------------------------------------------------------- #
# ConsumerTokenVerifier
# --------------------------------------------------------------------------- #


def test_verify_token_returns_access_token_for_valid_bearer() -> None:
    """A known token resolves to its consumer's :class:`AccessToken`."""
    verifier = ConsumerTokenVerifier({"adepthood": "secret123"})
    token = asyncio.run(verifier.verify_token("secret123"))
    assert token is not None
    assert token.client_id == "adepthood"
    assert token.scopes == [REMOTE_SCOPE]


def test_verify_token_rejects_unknown_bearer() -> None:
    """An unknown token yields ``None`` (401 at the middleware)."""
    verifier = ConsumerTokenVerifier({"adepthood": "secret123"})
    assert asyncio.run(verifier.verify_token("wrong")) is None


def test_verify_token_rejects_against_empty_map() -> None:
    """With no configured tokens, every bearer is rejected."""
    verifier = ConsumerTokenVerifier({})
    assert asyncio.run(verifier.verify_token("anything")) is None


def test_verify_token_rejects_non_ascii_bearer_cleanly() -> None:
    """A non-ASCII bearer is rejected (``None``), never a ``TypeError`` (#776)."""
    verifier = ConsumerTokenVerifier({"adepthood": "secret123"})
    # hmac.compare_digest raises TypeError on a non-ASCII str; the byte-compare
    # must reject cleanly instead of crashing verification.
    assert asyncio.run(verifier.verify_token("nön-ascii-tökén")) is None


# --------------------------------------------------------------------------- #
# _effective_consumer — per-consumer identity
# --------------------------------------------------------------------------- #


def test_effective_consumer_uses_token_client_id_when_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live access token overrides the default consumer with its client_id."""
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _fake_token("adepthood")
    )
    assert server_mod._effective_consumer("env-default") == "adepthood"


def test_effective_consumer_falls_back_to_default_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no token (stdio), the caller-supplied default is used."""
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: None)
    assert server_mod._effective_consumer("env-default") == "env-default"


# --------------------------------------------------------------------------- #
# _BoundedFastMCP.call_tool — the remote tier-ceiling boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", ["intimate", "all", "bogus-tier"])
def test_remote_call_above_personal_is_refused_before_dispatch(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """A remote caller cannot request intimate/all (or a garbage tier)."""
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _fake_token("adepthood")
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = _structured(
        asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": ceiling}))
    )
    assert result["status"] == "refused"
    assert result["tool"] == "creek.wheel"
    assert "not reachable over the network" in result["reason"]
    # Refused before dispatch => nothing was audited for the tool call.
    assert not (vault / MCP_AUDIT_RELPATH).exists()


@pytest.mark.parametrize("ceiling", ["open", "personal"])
def test_remote_call_at_or_below_personal_dispatches(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """open/personal are admitted for remote callers and dispatch normally."""
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _fake_token("adepthood")
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = _structured(
        asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": ceiling}))
    )
    assert result["tool"] == "creek.wheel"
    assert result.get("status") != "refused"


def test_remote_call_is_audited_under_token_consumer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched remote call is audited under the bearer's consumer identity."""
    monkeypatch.setenv("CREEK_MCP_CONSUMER", "env-default")
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _fake_token("adepthood")
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": "open"}))

    entries = [
        json.loads(line)
        for line in (vault / MCP_AUDIT_RELPATH).read_text("utf-8").splitlines()
        if line.strip()
    ]
    wheel = [e for e in entries if e["tool"] == "creek.wheel"]
    assert wheel, "expected a creek.wheel audit entry"
    assert wheel[-1]["consumer"] == "adepthood"  # token identity, not env default


def test_local_stdio_call_is_not_capped(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no token (stdio), an ``intimate`` ceiling is *not* refused."""
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: None)
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = _structured(
        asyncio.run(
            server.call_tool("creek.wheel", {"privacy_tier_ceiling": "intimate"})
        )
    )
    assert result["tool"] == "creek.wheel"
    assert result.get("status") != "refused"


# --------------------------------------------------------------------------- #
# main() — network transport preconditions
# --------------------------------------------------------------------------- #


def test_network_transport_refuses_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--transport network`` with no configured tokens exits (no anon access)."""
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        main(["--transport", "network"])
    assert excinfo.value.code != 0


# --------------------------------------------------------------------------- #
# Over-the-wire: unauthenticated / bad-token requests get 401
# --------------------------------------------------------------------------- #


def test_streamable_http_rejects_unauthenticated_request(vault: Path) -> None:
    """A POST with no / wrong bearer is refused 401 before any tool runs."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        token_verifier=ConsumerTokenVerifier({"adepthood": "secret123"}),
    )
    client = TestClient(server.streamable_http_app())
    path = server.settings.streamable_http_path
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}

    no_auth = client.post(path, json=body, headers=headers)
    assert no_auth.status_code == 401

    bad_token = client.post(
        path,
        json=body,
        headers={**headers, "Authorization": "Bearer wrong"},
    )
    assert bad_token.status_code == 401


# --------------------------------------------------------------------------- #
# Over-the-wire happy path: a valid bearer drives a full tools/call through the
# REAL streamable-http + auth middleware (not a monkeypatched context var).
# --------------------------------------------------------------------------- #

_WIRE_TOKEN = "over-the-wire-consumer-secret"  # fake test literal, not a real key


def _network_server(vault: Path, token: str) -> FastMCP:
    """Build a network server; disable DNS-rebinding host-check for the in-test host.

    The host validation is a separate concern from the auth path under test here,
    and TestClient's synthetic ``testserver`` host would otherwise be rejected
    before the request reaches the auth middleware.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        token_verifier=ConsumerTokenVerifier({"adepthood": token}),
    )
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    return server


def _sse_result(body: str) -> dict[str, object]:
    """Extract the JSON-RPC ``result`` from a streamable-http SSE response body."""
    payloads = [
        line[len("data:") :].strip()
        for line in body.splitlines()
        if line.startswith("data:")
    ]
    return json.loads(payloads[-1])["result"]  # type: ignore[no-any-return]


def _open_authenticated_session(
    client: TestClient, path: str, token: str
) -> dict[str, str]:
    """Run the MCP ``initialize`` handshake; return headers carrying the session id."""
    base = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init = client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=base,
    )
    assert init.status_code == 200
    headers = {**base, "mcp-session-id": init.headers["mcp-session-id"]}
    ack = client.post(
        path,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert ack.status_code == 202
    return headers


def _wire_call(
    client: TestClient, path: str, headers: dict[str, str], ceiling: str
) -> dict[str, object]:
    """Invoke ``creek.wheel`` over the wire at *ceiling*; return the structured part."""
    resp = client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "creek.wheel",
                "arguments": {"privacy_tier_ceiling": ceiling},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200
    result = _sse_result(resp.text)
    return result["structuredContent"]  # type: ignore[index, return-value]


def test_authenticated_tools_call_dispatches_over_the_wire(vault: Path) -> None:
    """A valid bearer + open ceiling dispatches a real tools/call through middleware."""
    server = _network_server(vault, _WIRE_TOKEN)
    path = server.settings.streamable_http_path
    with TestClient(server.streamable_http_app()) as client:
        headers = _open_authenticated_session(client, path, _WIRE_TOKEN)
        structured = _wire_call(client, path, headers, "open")
    assert structured["tool"] == "creek.wheel"  # dispatched, not refused
    assert structured.get("status") != "refused"


def test_remote_intimate_is_refused_over_the_wire(vault: Path) -> None:
    """The ceiling cap engages on the *real* auth path: remote intimate is refused.

    ``get_access_token()`` is populated by the SDK's ``RequireAuthMiddleware`` from
    the verified bearer — no monkeypatch — so ``_BoundedFastMCP.call_tool`` refuses
    the over-ceiling request end-to-end, exactly as it would in production.
    """
    server = _network_server(vault, _WIRE_TOKEN)
    path = server.settings.streamable_http_path
    with TestClient(server.streamable_http_app()) as client:
        headers = _open_authenticated_session(client, path, _WIRE_TOKEN)
        structured = _wire_call(client, path, headers, "intimate")
    assert structured["status"] == "refused"
    assert "not reachable over the network" in structured["reason"]


# --------------------------------------------------------------------------- #
# Token TTL (#837): verify_token issues a finite expiry, never expires_at=None
# --------------------------------------------------------------------------- #

_TOKEN_TTL_ENV = "CREEK_MCP_TOKEN_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 3600
_FIXED_NOW = 1_000_000.5  # arbitrary fixed clock so expiry math is exact


def _verified_token_at_fixed_now(monkeypatch: pytest.MonkeyPatch) -> AccessToken:
    """Verify a known bearer with ``remote_auth._now`` pinned to :data:`_FIXED_NOW`.

    ``_now`` is the design contract's monkeypatchable module-level clock alias
    (``_now = time.time``); referencing it here is the RED trigger until #837
    lands.
    """
    monkeypatch.setattr(remote_auth_mod, "_now", lambda: _FIXED_NOW)
    verifier = ConsumerTokenVerifier({"adepthood": "secret123"})
    token = asyncio.run(verifier.verify_token("secret123"))
    assert token is not None
    return token


def test_verify_token_sets_finite_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified token expires at ``int(now) + 3600`` by default — never ``None``."""
    monkeypatch.delenv(_TOKEN_TTL_ENV, raising=False)
    token = _verified_token_at_fixed_now(monkeypatch)
    assert isinstance(token.expires_at, int)
    assert token.expires_at == 1_000_000 + _DEFAULT_TTL_SECONDS


def test_token_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CREEK_MCP_TOKEN_TTL_SECONDS`` overrides the default token lifetime."""
    monkeypatch.setenv(_TOKEN_TTL_ENV, "900")
    token = _verified_token_at_fixed_now(monkeypatch)
    assert token.expires_at == 1_000_000 + 900


@pytest.mark.parametrize("raw_ttl", ["abc", "-5", "0"])
def test_token_ttl_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw_ttl: str
) -> None:
    """A non-int or non-positive TTL env value falls back to 3600 without raising."""
    monkeypatch.setenv(_TOKEN_TTL_ENV, raw_ttl)
    token = _verified_token_at_fixed_now(monkeypatch)
    assert token.expires_at == 1_000_000 + _DEFAULT_TTL_SECONDS


# --------------------------------------------------------------------------- #
# Transport confidentiality (#837): loopback classification + the TLS guard
# --------------------------------------------------------------------------- #


class _StubBuiltServer:
    """Socket-free stand-in for a built ``FastMCP`` server.

    Routed in via ``build_server`` so a guard bug fails the test as
    ``DID NOT RAISE`` instead of binding a real port and hanging the run.
    """

    def __init__(self) -> None:
        """Start with mutable settings and an empty run log."""
        self.settings = SimpleNamespace(host=None, port=None)
        self.run_calls: list[str] = []

    def run(self, transport: str) -> None:
        """Record the requested transport instead of serving."""
        self.run_calls.append(transport)


def _stub_build_server(monkeypatch: pytest.MonkeyPatch) -> _StubBuiltServer:
    """Route ``server.build_server`` to a :class:`_StubBuiltServer`; return it."""
    stub = _StubBuiltServer()
    monkeypatch.setattr(server_mod, "build_server", lambda **_kwargs: stub)
    return stub


def _parsed_network_args(
    *, host: str, port: int, tls_cert: Path | None, tls_key: Path | None
) -> argparse.Namespace:
    """Build the parsed-args namespace ``main`` hands to ``_serve_network``."""
    return argparse.Namespace(
        config=None,
        transport="network",
        host=host,
        port=port,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.5", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback_helper(host: str, expected: bool) -> None:
    """``_is_loopback`` classifies loopback binds True and routable hosts False."""
    assert server_mod._is_loopback(host) is expected


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_network_transport_refuses_routable_host_without_tls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """A routable ``--host`` without TLS exits before serving, pointing at the fix."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_TOKEN}")
    _stub_build_server(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["--transport", "network", "--host", host])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "--tls-cert" in err or "TLS" in err


def test_network_transport_rejects_short_token_with_recipe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sub-minimum token exits 2 with the rotation recipe, never the token."""
    # 7-char test literal, well under the 32-char minimum.
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, "adepthood=hunter2")
    _stub_build_server(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["--transport", "network", "--host", "127.0.0.1"])
    assert excinfo.value.code == 2  # argparse parser.error convention
    err = capsys.readouterr().err
    assert "adepthood" in err  # the offending consumer is named
    assert "secrets.token_urlsafe(32)" in err  # the rotation recipe
    assert "hunter2" not in err  # NEVER echo the token value


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_network_transport_allows_loopback_without_tls(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """A loopback bind still serves plaintext — local dev is not broken by #837."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_TOKEN}")
    stub = _stub_build_server(monkeypatch)
    served: list[tuple[object, argparse.Namespace]] = []
    monkeypatch.setattr(
        server_mod,
        "_serve_network",
        lambda server, args: served.append((server, args)),
    )
    main(["--transport", "network", "--host", host])
    assert len(served) == 1
    assert served[0][0] is stub


def test_network_transport_allows_routable_host_with_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A routable bind with cert + key serves, and the TLS paths reach the args."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_TOKEN}")
    _stub_build_server(monkeypatch)
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")
    served: list[tuple[object, argparse.Namespace]] = []
    monkeypatch.setattr(
        server_mod,
        "_serve_network",
        lambda server, args: served.append((server, args)),
    )
    main(
        [
            "--transport",
            "network",
            "--host",
            "0.0.0.0",
            "--tls-cert",
            str(cert),
            "--tls-key",
            str(key),
        ]
    )
    assert len(served) == 1
    args = served[0][1]
    assert args.tls_cert == cert
    assert args.tls_key == key


def test_network_transport_rejects_tls_cert_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--tls-cert`` without ``--tls-key`` is refused (both-or-neither)."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_TOKEN}")
    _stub_build_server(monkeypatch)
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["--transport", "network", "--host", "127.0.0.1", "--tls-cert", str(cert)],
        )
    assert excinfo.value.code != 0
    assert "--tls-key" in capsys.readouterr().err


def test_network_transport_rejects_missing_tls_cert_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """TLS flags pointing at nonexistent files exit with a 'file not found' error."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_TOKEN}")
    _stub_build_server(monkeypatch)
    missing_cert = tmp_path / "no-cert.pem"
    missing_key = tmp_path / "no-key.pem"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--transport",
                "network",
                "--host",
                "127.0.0.1",
                "--tls-cert",
                str(missing_cert),
                "--tls-key",
                str(missing_key),
            ]
        )
    assert excinfo.value.code != 0
    assert "file not found" in capsys.readouterr().err.lower()


def test_serve_network_uses_ssl_when_tls_configured(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TLS configured, ``_serve_network`` hands uvicorn the cert + key files."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")

    captured: dict[str, object] = {}

    class _CapturingConfig:
        """Fake ``uvicorn.Config`` recording every keyword it is built with."""

        # Attributes uvicorn.run() consults post-construction, so an
        # implementation built on uvicorn.run(...) also completes cleanly.
        reload = False
        should_reload = False
        workers = 1
        uds = None

        def __init__(self, app: object, **kwargs: object) -> None:
            """Record the app and the keyword arguments uvicorn received."""
            captured["app"] = app
            captured.update(kwargs)

    class _IdleServer:
        """Fake ``uvicorn.Server`` that never opens a socket."""

        started = True  # uvicorn.run() exits nonzero when this is False

        def __init__(self, config: object) -> None:
            """Hold the config without acting on it."""
            self.config = config

        def run(self) -> None:
            """No-op stand-in for the blocking serve loop."""

        async def serve(self) -> None:
            """No-op stand-in for the async serve loop."""

    monkeypatch.setattr(uvicorn, "Config", _CapturingConfig)
    monkeypatch.setattr(uvicorn, "Server", _IdleServer)

    server = _network_server(vault, _WIRE_TOKEN)
    server_mod._serve_network(
        server,
        _parsed_network_args(host="0.0.0.0", port=8443, tls_cert=cert, tls_key=key),
    )

    assert str(captured["ssl_certfile"]) == str(cert)
    assert str(captured["ssl_keyfile"]) == str(key)


def test_serve_network_plain_loopback_uses_server_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without TLS, ``_serve_network`` falls back to ``run('streamable-http')``."""
    server = _network_server(vault, _WIRE_TOKEN)
    runs: list[str] = []
    monkeypatch.setattr(server, "run", lambda transport: runs.append(transport))
    server_mod._serve_network(
        server,
        _parsed_network_args(host="127.0.0.1", port=8000, tls_cert=None, tls_key=None),
    )
    assert runs == ["streamable-http"]


def test_default_stdio_transport_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main([])`` still runs plain stdio — the TLS guard never engages locally."""
    stub = _stub_build_server(monkeypatch)
    main([])
    assert stub.run_calls == ["stdio"]


# --------------------------------------------------------------------------- #
# CHARACTERIZATION PINS for the issue #1073 extraction
#
# Everything below this banner pins the *current* behaviour of the remote
# tier-ceiling cap (``_BoundedFastMCP.call_tool``) and of per-call consumer
# identity (``_effective_consumer``), exactly as they behave today, before any
# of that logic is extracted out of ``creek_mcp/server.py``.
#
# These tests PASS against the unmodified code. They are pins, not RED tests.
# Their whole job is to fail loudly if the extraction changes behaviour, so:
#
# * a diff to any test below across the extraction commit is itself a red flag
#   — the pins are meant to be byte-identical before and after the move;
# * every pin goes through the public boundary (``build_server`` +
#   ``server.call_tool``, or the module-level ``_effective_consumer``), never
#   through an internal the extraction is expected to relocate;
# * the refusal ``reason`` is asserted against a hard-coded literal rather than
#   an imported constant, so a refactor cannot silently reword the one sentence
#   a remote consumer is told when intimate content is withheld.
# --------------------------------------------------------------------------- #

# The exact text ``_BoundedFastMCP.call_tool`` refuses with today. Spelled out
# rather than imported on purpose: importing the constant would let a reword
# sail through unnoticed.
_CAP_REFUSAL_REASON = (
    "remote consumers may not request a ceiling above 'personal'; "
    "intimate content is not reachable over the network"
)

# The literal ``creek_mcp.tools.purge`` refuses with when the elevated gate
# denies. Also spelled out, for the same reason.
_PURGE_ELEVATED_REFUSAL_REASON = (
    "elevated authorization required: set CREEK_MCP_ELEVATED_TOKEN on the "
    "server and pass a matching auth_token"
)

_PURGE_VAULT_CONFIRM_REFUSAL_REASON = (
    "creek.purge.vault requires confirm_vault_path matching the "
    "target vault's absolute path"
)

_ELEVATED_TOKEN_ENV = "CREEK_MCP_ELEVATED_TOKEN"
_CONSUMER_ENV = "CREEK_MCP_CONSUMER"

_PURGE_TOOL_NAMES = (
    "creek.purge.fragment",
    "creek.purge.source",
    "creek.purge.classifications",
    "creek.purge.daterange",
    "creek.purge.vault",
)

# Every ``privacy_tier_ceiling`` value the cap refuses for a REMOTE caller.
# Only the exact strings ``open`` and ``personal`` (and an absent key, which
# defaults to ``open``) clear it: no case folding, no whitespace tolerance, no
# coercion of non-strings.
_REMOTE_REFUSED_CEILINGS: list[object] = [
    "intimate",
    "all",
    None,
    "",
    "OPEN",
    "Personal",
    " open",
    "open ",
    "bogus-tier",
    "unclassified",
    0,
    1,
    True,
    False,
    3.5,
    [],
    {},
]

_REMOTE_REFUSED_IDS = [
    "intimate",
    "all",
    "none",
    "empty-string",
    "upper-open",
    "title-personal",
    "leading-space",
    "trailing-space",
    "bogus-tier",
    "unclassified",
    "int-zero",
    "int-one",
    "bool-true",
    "bool-false",
    "float",
    "empty-list",
    "empty-dict",
]

# Every argument set that dispatches locally, including the absent key.
_VALID_CEILING_ARGUMENTS: list[dict[str, object]] = [
    {},
    {"privacy_tier_ceiling": "open"},
    {"privacy_tier_ceiling": "personal"},
    {"privacy_tier_ceiling": "intimate"},
    {"privacy_tier_ceiling": "all"},
]

_VALID_CEILING_IDS = ["absent", "open", "personal", "intimate", "all"]


def _pin_server(vault: Path) -> FastMCP:
    """Build the server every characterization pin drives through ``call_tool``.

    Args:
        vault: The seeded vault root the tools read and audit under.

    Returns:
        A ``build_server`` instance with a stub draft LLM, so no pin needs a
        provider configured.
    """
    return build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )


def _remote(monkeypatch: pytest.MonkeyPatch, consumer: str = "adepthood") -> None:
    """Make every subsequent call look like an authenticated remote request.

    Args:
        monkeypatch: The active monkeypatch fixture.
        consumer: The ``client_id`` the fake bearer identifies.
    """
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _fake_token(consumer)
    )


def _local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every subsequent call look like a local stdio request (no token).

    Args:
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: None)


def _call(
    server: FastMCP, tool: str, arguments: dict[str, object]
) -> dict[str, object] | None:
    """Invoke *tool* through the public boundary; return its structured payload.

    Args:
        server: The built server under test.
        tool: Dot-namespaced tool name.
        arguments: The raw argument mapping, deliberately untyped so a pin can
            hand the boundary a value FastMCP would never construct.

    Returns:
        The structured payload, or ``None`` when the call raised before
        producing one. ``None`` is what FastMCP's own pydantic argument
        coercion yields for a value that is not a ``TierCeiling``; that
        coercion is not the ceiling cap's business, so the local-path pins ask
        only whether the *cap's* refusal was produced.
    """
    try:
        return _structured(asyncio.run(server.call_tool(tool, arguments)))
    except Exception:
        return None


def _audit_entries(vault: Path) -> list[dict[str, object]]:
    """Return every parsed MCP audit entry under *vault*, in write order.

    Args:
        vault: The vault root.

    Returns:
        The decoded entries, or an empty list when the log does not exist.
    """
    log_path = vault / MCP_AUDIT_RELPATH
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def _entries_for(vault: Path, tool: str) -> list[dict[str, object]]:
    """Return the audit entries written for *tool*, in write order.

    Args:
        vault: The vault root.
        tool: Dot-namespaced tool name to filter on.

    Returns:
        The matching entries.
    """
    return [entry for entry in _audit_entries(vault) if entry["tool"] == tool]


def _consumers_for(vault: Path, tool: str) -> list[object]:
    """Return the ``consumer`` of every audit entry for *tool*, in write order.

    Args:
        vault: The vault root.
        tool: Dot-namespaced tool name to filter on.

    Returns:
        The consumer identifiers, one per matching entry.
    """
    return [entry["consumer"] for entry in _entries_for(vault, tool)]


# --------------------------------------------------------------------------- #
# Pin: the remote admission table, refused half
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", _REMOTE_REFUSED_CEILINGS, ids=_REMOTE_REFUSED_IDS)
def test_remote_cap_refuses_every_non_admitted_ceiling(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: object
) -> None:
    """Pin the whole refused half of the remote admission table.

    The cap admits the exact strings ``open`` and ``personal`` and nothing
    else. Everything here — the two over-ceiling tiers, wrong case, leading or
    trailing whitespace, ``None``, the empty string, unknown tier names, and
    every non-string scalar or container — is refused *before dispatch* with
    the one hard-coded reason, and leaves no audit entry behind.

    Args:
        vault: Seeded vault root.
        monkeypatch: Used to present an authenticated remote bearer.
        ceiling: The ``privacy_tier_ceiling`` value under test.
    """
    _remote(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", {"privacy_tier_ceiling": ceiling})
    assert result is not None
    assert result["status"] == "refused"
    assert result["tool"] == "creek.wheel"
    assert result["reason"] == _CAP_REFUSAL_REASON
    # Refused before dispatch => the tool never ran, so nothing was audited.
    assert not (vault / MCP_AUDIT_RELPATH).exists()


@pytest.mark.parametrize("ceiling", ["intimate", "all", "bogus-tier"])
def test_remote_refusal_payload_is_exact_and_does_not_echo_the_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """Pin the refusal payload key-for-key, including the ceiling it reports.

    ``tier_ceiling`` is always ``"open"`` — the cap's own floor — never the
    ceiling the caller asked for, so a client cannot read its own rejected
    request back out of the response. The dict equality also pins that the
    payload carries no key beyond ``status``/``tool``/``tier_ceiling``/
    ``reason``.

    Args:
        vault: Seeded vault root.
        monkeypatch: Used to present an authenticated remote bearer.
        ceiling: An over-ceiling or unrecognised request value.
    """
    _remote(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", {"privacy_tier_ceiling": ceiling})
    assert result == {
        "status": "refused",
        "tool": "creek.wheel",
        "tier_ceiling": "open",
        "reason": _CAP_REFUSAL_REASON,
    }


# --------------------------------------------------------------------------- #
# Pin: the remote admission table, admitted half
# --------------------------------------------------------------------------- #


def test_remote_call_without_the_ceiling_key_dispatches_and_audits(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent ``privacy_tier_ceiling`` defaults to ``open`` and is admitted.

    The cap reads ``arguments.get(...)`` with an ``open`` fallback, so omitting
    the key entirely is admitted rather than refused — and the call dispatches
    far enough to write its audit entry under the bearer's consumer.

    Args:
        vault: Seeded vault root.
        monkeypatch: Used to present an authenticated remote bearer.
    """
    _remote(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", {})
    assert result is not None
    assert result["tool"] == "creek.wheel"
    assert result.get("status") != "refused"
    assert _consumers_for(vault, "creek.wheel") == ["adepthood"]


# --------------------------------------------------------------------------- #
# Pin: stdio is outside the cap entirely
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", _REMOTE_REFUSED_CEILINGS, ids=_REMOTE_REFUSED_IDS)
def test_local_call_never_produces_the_cap_refusal(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: object
) -> None:
    """No local (stdio) call is refused *by the cap*, whatever the value is.

    The garbage values still fail FastMCP's own pydantic coercion locally,
    which raises instead of returning a payload. That is not this gate's
    business, so the pin is deliberately narrow: whatever else happens, the
    cap's refusal is never what comes back.

    Args:
        vault: Seeded vault root.
        monkeypatch: Used to make the call look local (no token).
        ceiling: The ``privacy_tier_ceiling`` value under test.
    """
    _local(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", {"privacy_tier_ceiling": ceiling})
    assert result is None or result.get("reason") != _CAP_REFUSAL_REASON


@pytest.mark.parametrize("arguments", _VALID_CEILING_ARGUMENTS, ids=_VALID_CEILING_IDS)
def test_local_call_dispatches_at_every_valid_ceiling(
    vault: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]
) -> None:
    """All four ceilings — plus an absent key — dispatch for a local caller.

    ``intimate`` and ``all`` are exactly the two the remote cap refuses, so
    this is the pin that stops the extraction from widening the cap onto the
    stdio path.

    Args:
        vault: Seeded vault root.
        monkeypatch: Used to make the call look local (no token).
        arguments: The argument mapping under test.
    """
    _local(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", arguments)
    assert result is not None
    assert result["tool"] == "creek.wheel"
    assert result.get("status") != "refused"


def test_local_call_is_audited_under_the_env_consumer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stdio half of the identity rule: no token means ``CREEK_MCP_CONSUMER``.

    The remote half is pinned by
    ``test_remote_call_is_audited_under_token_consumer``; this is its
    counterpart, so the extraction cannot make the env default win for a
    bearer-identified caller (or vice versa) without turning one of the two
    red.

    Args:
        vault: Seeded vault root.
        monkeypatch: Sets the process-default consumer and clears the token.
    """
    monkeypatch.setenv(_CONSUMER_ENV, "env-default")
    _local(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.wheel", {"privacy_tier_ceiling": "open"})
    assert result is not None
    assert _consumers_for(vault, "creek.wheel") == ["env-default"]


# --------------------------------------------------------------------------- #
# Pin: caller identity and remoteness are resolved PER CALL, never cached
#
# The single highest-consequence failure mode of the #1073 extraction is a
# caller identity built once at ``build_server`` time. Every call after the
# first would then be audited under the first caller's name — forging
# attribution — and the cap would be pinned to the first caller's remoteness,
# so one local call could unlock intimate content for every later remote one.
# --------------------------------------------------------------------------- #


def test_consumer_identity_is_resolved_per_call_not_once_per_server(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two calls on ONE server under two bearers audit under two consumers.

    Args:
        vault: Seeded vault root.
        monkeypatch: Sets the process default and swaps the live bearer between
            the two calls.
    """
    monkeypatch.setenv(_CONSUMER_ENV, "env-default")
    bearer: list[AccessToken | None] = [_fake_token("adepthood")]
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: bearer[0])
    server = _pin_server(vault)

    assert _call(server, "creek.wheel", {"privacy_tier_ceiling": "open"}) is not None
    bearer[0] = _fake_token("crawdad")
    assert _call(server, "creek.wheel", {"privacy_tier_ceiling": "open"}) is not None

    assert _consumers_for(vault, "creek.wheel") == ["adepthood", "crawdad"]


def test_cap_re_evaluates_remoteness_remote_then_local(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One server, remote then local: the cap engages on the first call only.

    Args:
        vault: Seeded vault root.
        monkeypatch: Sets the process default and drops the bearer between the
            two calls.
    """
    monkeypatch.setenv(_CONSUMER_ENV, "env-default")
    bearer: list[AccessToken | None] = [_fake_token("adepthood")]
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: bearer[0])
    server = _pin_server(vault)

    refused = _call(server, "creek.wheel", {"privacy_tier_ceiling": "intimate"})
    assert refused is not None
    assert refused["reason"] == _CAP_REFUSAL_REASON
    assert not (vault / MCP_AUDIT_RELPATH).exists()

    bearer[0] = None
    admitted = _call(server, "creek.wheel", {"privacy_tier_ceiling": "intimate"})
    assert admitted is not None
    assert admitted.get("status") != "refused"
    assert _consumers_for(vault, "creek.wheel") == ["env-default"]


def test_cap_re_evaluates_remoteness_local_then_remote(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One server, local then remote: an earlier local call does not unlock the cap.

    Args:
        vault: Seeded vault root.
        monkeypatch: Sets the process default and installs the bearer between
            the two calls.
    """
    monkeypatch.setenv(_CONSUMER_ENV, "env-default")
    bearer: list[AccessToken | None] = [None]
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: bearer[0])
    server = _pin_server(vault)

    admitted = _call(server, "creek.wheel", {"privacy_tier_ceiling": "intimate"})
    assert admitted is not None
    assert admitted.get("status") != "refused"
    assert _consumers_for(vault, "creek.wheel") == ["env-default"]

    bearer[0] = _fake_token("adepthood")
    refused = _call(server, "creek.wheel", {"privacy_tier_ceiling": "intimate"})
    assert refused is not None
    assert refused["reason"] == _CAP_REFUSAL_REASON
    # Refused before dispatch => no second entry joined the first.
    assert _consumers_for(vault, "creek.wheel") == ["env-default"]


# --------------------------------------------------------------------------- #
# Pin: the deliberate creek.purge.* carve-out (server.py:120-131)
#
# Purge tools declare no ``privacy_tier_ceiling``, so the remote cap always
# falls back to ``open`` for them and is a documented no-op. Their real gate is
# ``CREEK_MCP_ELEVATED_TOKEN`` via ``creek_mcp.auth.is_elevated``. These pins
# stop the extraction from either (a) accidentally routing purge through the
# ceiling cap, or (b) treating the cap as purge's protection and weakening the
# elevated gate.
# --------------------------------------------------------------------------- #


def test_purge_tools_declare_no_tier_ceiling_parameter(vault: Path) -> None:
    """The carve-out's premise: purge tools expose no ceiling to cap.

    ``creek.wheel`` declares ``privacy_tier_ceiling``; none of the five
    ``creek.purge.*`` tools do.

    Args:
        vault: Seeded vault root.
    """
    server = _pin_server(vault)
    listed = asyncio.run(server.list_tools())
    schemas = {tool.name: tool.inputSchema for tool in listed}
    assert "privacy_tier_ceiling" in schemas["creek.wheel"]["properties"]
    for name in _PURGE_TOOL_NAMES:
        assert "privacy_tier_ceiling" not in schemas[name]["properties"]


def test_remote_purge_is_refused_by_the_elevated_gate_not_the_cap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote purge call is refused by the ELEVATED gate, never by the ceiling cap.

    The distinguishing evidence is the reason string plus the payload shape:
    the purge refusal carries no ``tier_ceiling`` key at all, which the cap's
    refusal always does.

    Args:
        vault: Seeded vault root.
        monkeypatch: Clears the elevated token and presents a remote bearer.
    """
    monkeypatch.delenv(_ELEVATED_TOKEN_ENV, raising=False)
    _remote(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.purge.fragment", {"fragment_id": "frag-missing"})
    assert result == {
        "status": "refused",
        "tool": "creek.purge.fragment",
        "reason": _PURGE_ELEVATED_REFUSAL_REASON,
    }


def test_remote_purge_refusal_is_audited_under_the_bearer_consumer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The elevated gate audits its denial, attributed to the bearer's consumer.

    Also pins that the audit entry records ``tier_ceiling: "open"`` for a
    purge, and that the caller's ``auth_token`` never enters ``args_summary``.

    Args:
        vault: Seeded vault root.
        monkeypatch: Sets a process default, clears the elevated token, and
            presents a remote bearer.
    """
    monkeypatch.setenv(_CONSUMER_ENV, "env-default")
    monkeypatch.delenv(_ELEVATED_TOKEN_ENV, raising=False)
    _remote(monkeypatch)
    server = _pin_server(vault)
    _call(server, "creek.purge.fragment", {"fragment_id": "frag-missing"})

    entries = _entries_for(vault, "creek.purge.fragment")
    assert len(entries) == 1
    assert entries[0]["consumer"] == "adepthood"
    assert entries[0]["tier_ceiling"] == "open"
    assert entries[0]["args_summary"] == {
        "fragment_id": "frag-missing",
        "dry_run": False,
    }


@pytest.mark.parametrize(
    ("configured", "supplied"),
    [
        (None, None),
        (None, _STRONG_TOKEN),
        (_STRONG_TOKEN, None),
        (_STRONG_TOKEN, "wrong-token-value"),
    ],
    ids=[
        "unset-and-absent",
        "unset-but-supplied",
        "set-but-absent",
        "set-and-mismatched",
    ],
)
def test_elevated_gate_remains_the_real_purge_gate(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    supplied: str | None,
) -> None:
    """A remote bearer alone never satisfies the elevated gate, and denials audit.

    Four ways to fail closed: no server token and no client token, no server
    token with a client token, a server token with no client token, and a
    server token with a mismatched client token. All four refuse with the
    elevated reason and all four leave exactly one audit entry.

    Args:
        vault: Seeded vault root.
        monkeypatch: Configures (or clears) the elevated token and presents a
            remote bearer.
        configured: The server-side ``CREEK_MCP_ELEVATED_TOKEN``, or ``None``
            to leave it unset.
        supplied: The client-side ``auth_token``, or ``None`` to omit it.
    """
    if configured is None:
        monkeypatch.delenv(_ELEVATED_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(_ELEVATED_TOKEN_ENV, configured)
    _remote(monkeypatch)
    server = _pin_server(vault)

    arguments: dict[str, object] = {"fragment_id": "frag-missing"}
    if supplied is not None:
        arguments["auth_token"] = supplied
    result = _call(server, "creek.purge.fragment", arguments)

    assert result is not None
    assert result["status"] == "refused"
    assert result["reason"] == _PURGE_ELEVATED_REFUSAL_REASON
    assert len(_entries_for(vault, "creek.purge.fragment")) == 1


def test_remote_purge_with_valid_elevated_token_clears_both_gates(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the elevated token, a remote purge gets past the cap AND the gate.

    ``creek.purge.vault`` then stops on its own second factor — the missing
    ``confirm_vault_path`` — which is the proof that neither the ceiling cap
    nor the elevated gate refused it. Nothing is destroyed: the refusal lands
    before ``PurgeEngine`` is ever constructed.

    Args:
        vault: Seeded vault root.
        monkeypatch: Configures a floor-clearing elevated token and presents a
            remote bearer.
    """
    monkeypatch.setenv(_ELEVATED_TOKEN_ENV, _STRONG_TOKEN)
    _remote(monkeypatch)
    server = _pin_server(vault)
    result = _call(server, "creek.purge.vault", {"auth_token": _STRONG_TOKEN})

    assert result == {
        "status": "refused",
        "tool": "creek.purge.vault",
        "reason": _PURGE_VAULT_CONFIRM_REFUSAL_REASON,
    }
    entries = _entries_for(vault, "creek.purge.vault")
    assert len(entries) == 1
    assert entries[0]["consumer"] == "adepthood"
    # The elevated token never reaches the audit log.
    assert entries[0]["args_summary"] == {
        "confirm_vault_path": False,
        "dry_run": False,
    }
