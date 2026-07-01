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

No real/production token material is hardcoded — every token is a test literal.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

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


# --------------------------------------------------------------------------- #
# load_consumer_tokens — parsing
# --------------------------------------------------------------------------- #


def test_load_consumer_tokens_parses_pairs() -> None:
    """Well-formed ``consumer=token`` pairs parse into a map."""
    env = {CONSUMER_TOKENS_ENV: "adepthood=secret123;crawdad=tok456"}
    assert load_consumer_tokens(env) == {
        "adepthood": "secret123",
        "crawdad": "tok456",
    }


def test_load_consumer_tokens_skips_blank_and_tokenless_entries() -> None:
    """Blank segments and ``name=`` entries with no token are dropped."""
    env = {CONSUMER_TOKENS_ENV: " adepthood = secret123 ; ; missing= ;=orphan"}
    assert load_consumer_tokens(env) == {"adepthood": "secret123"}


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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
