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
- **Rotation needs no cutover** (#895) — a consumer may hold an *ordered set* of
  currently-valid tokens, so the old and the new secret both authenticate for as
  long as the operator leaves the window open, and dropping the old one revokes
  it on the very next request. The configuration is refused outright for a
  repeated consumer name (previously a silent last-wins), for a token value
  reused anywhere in the configuration, and for a bare ``str`` map value —
  ``str`` is itself a ``Sequence[str]``, so one would otherwise be read as one
  single-character token per letter, each of which ``compare_digest`` accepts.
  ``rotation_notice()`` tells the operator which consumers are mid-rotation, by
  name and count only, so it cannot echo a value by construction.

No real/production token material is hardcoded — every token is a test literal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest
import uvicorn
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from creek_mcp import remote_auth as remote_auth_mod
from creek_mcp import server as server_mod
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.policy import Transport
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    REMOTE_SCOPE,
    ConsumerTokenVerifier,
    load_consumer_tokens,
)
from creek_mcp.server import build_server, main

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
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


def _window_token(tag: str) -> str:
    """Return a distinct, floor-clearing test token tagged with *tag* (#895).

    The rotation-window tests need several tokens that differ from each other
    and from :data:`_STRONG_TOKEN`, and all of which clear the 32-char floor so
    a rotation test never trips the length gate by accident. Spelled as a
    concatenation, matching the convention above, so a secret scanner reads a
    constructed string rather than a credential.

    Args:
        tag: A short word distinguishing this token from its siblings.

    Returns:
        A constructed test string of at least 32 characters.
    """
    return f"unit-test-{tag}-token-" + "x" * 24


# The rotation-window cast. Every one is a test literal, not a real credential.
_OLD_TOKEN = _window_token("old")  # the secret being retired
_NEW_TOKEN = _window_token("new")  # the secret replacing it
_THIRD_TOKEN = _window_token("third")
_FOURTH_TOKEN = _window_token("fourth")
_FIFTH_TOKEN = _window_token("fifth")

# 7 chars, well under the 32-char floor. Test literal, not a real credential.
_WEAK_TOKEN = "hunter2"

_ALL_TEST_TOKENS = (
    _STRONG_TOKEN,
    _OLD_TOKEN,
    _NEW_TOKEN,
    _THIRD_TOKEN,
    _FOURTH_TOKEN,
    _FIFTH_TOKEN,
    _WEAK_TOKEN,
)
"""Every token literal a refusal message or a notice must never echo."""


# --------------------------------------------------------------------------- #
# load_consumer_tokens — parsing
# --------------------------------------------------------------------------- #


def test_load_consumer_tokens_parses_pairs() -> None:
    """Well-formed ``consumer=token`` pairs parse into a map.

    Today's single-token format is unchanged on the wire and now lands as a
    one-element tuple (#895), so an operator who has never rotated anything
    parses exactly as before.
    """
    adepthood_token = "adepthood-strong-token-" + "a" * 20  # 43 chars, test literal
    crawdad_token = "crawdad-strong-token-" + "b" * 20  # 41 chars, test literal
    env = {CONSUMER_TOKENS_ENV: f"adepthood={adepthood_token};crawdad={crawdad_token}"}
    assert load_consumer_tokens(env) == {
        "adepthood": (adepthood_token,),
        "crawdad": (crawdad_token,),
    }


def test_load_consumer_tokens_skips_blank_and_tokenless_entries() -> None:
    """Blank segments and ``name=`` entries with no token are dropped."""
    env = {CONSUMER_TOKENS_ENV: f" adepthood = {_STRONG_TOKEN} ; ; missing= ;=orphan"}
    assert load_consumer_tokens(env) == {"adepthood": (_STRONG_TOKEN,)}


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
    assert load_consumer_tokens(env) == {"adepthood": (_STRONG_TOKEN,)}


def test_load_consumer_tokens_accepts_exact_boundary_token() -> None:
    """A token of exactly 32 chars sits on the floor and is accepted."""
    boundary_token = "a" * 32
    env = {CONSUMER_TOKENS_ENV: f"adepthood={boundary_token}"}
    assert load_consumer_tokens(env) == {"adepthood": (boundary_token,)}


def test_load_consumer_tokens_skips_blanks_without_raising() -> None:
    """Blank and bare ``name=`` entries stay silently skipped, not length-checked."""
    env = {CONSUMER_TOKENS_ENV: f";;missing=;=orphan;adepthood={_STRONG_TOKEN}"}
    assert load_consumer_tokens(env) == {"adepthood": (_STRONG_TOKEN,)}


def test_load_consumer_tokens_empty_when_unset() -> None:
    """An unset env var yields an empty map (caller treats as 'network denied')."""
    assert load_consumer_tokens({}) == {}


# --------------------------------------------------------------------------- #
# load_consumer_tokens — the rotation window (#895)
#
# One consumer, several currently-valid tokens, comma-separated inside its own
# ``consumer=`` segment. The ``;`` separator still means "next consumer"; the
# ``,`` separator means "another token this same consumer may present".
# --------------------------------------------------------------------------- #


def test_load_consumer_tokens_parses_an_ordered_token_set() -> None:
    """``adepthood=old,new`` yields both tokens, in configured order.

    Order is asserted with a tuple rather than a set: the configured order is
    the operator's statement of which secret is the incumbent and which is the
    replacement, and it is what a notice or a future report would read.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN},{_NEW_TOKEN}"}
    assert load_consumer_tokens(env) == {"adepthood": (_OLD_TOKEN, _NEW_TOKEN)}


def test_load_consumer_tokens_strips_whitespace_around_comma_segments() -> None:
    """Whitespace around a comma is operator formatting, never token material.

    The single-token form already strips, so a multi-token form that did not
    would silently produce a token nobody can present.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood= {_OLD_TOKEN} , {_NEW_TOKEN} "}
    assert load_consumer_tokens(env) == {"adepthood": (_OLD_TOKEN, _NEW_TOKEN)}


def test_load_consumer_tokens_drops_empty_comma_segments() -> None:
    """A doubled or trailing comma leaves no empty token behind.

    Dropping has to happen *before* the length floor runs, or a trailing comma
    would be reported to the operator as a zero-character token.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN},,{_NEW_TOKEN},"}
    assert load_consumer_tokens(env) == {"adepthood": (_OLD_TOKEN, _NEW_TOKEN)}


def test_load_consumer_tokens_skips_a_consumer_whose_segments_are_all_empty() -> None:
    """``missing=,,`` is the multi-token spelling of ``missing=``: skipped, not raised.

    The single-token parser already skips ``name=`` silently; the comma form
    must reach the same verdict rather than registering a consumer with no
    tokens (which the verifier refuses outright).
    """
    env = {CONSUMER_TOKENS_ENV: f"missing=,,;adepthood={_STRONG_TOKEN}"}
    assert load_consumer_tokens(env) == {"adepthood": (_STRONG_TOKEN,)}


def test_load_consumer_tokens_applies_the_floor_to_every_token() -> None:
    """The 32-char floor is per comma segment — one weak token in a set still raises.

    A rotation is exactly when a fresh secret gets typed in, so the floor has to
    hold on the *new* token and not only on whichever one happens to be first.
    The message names the consumer, the observed and required lengths and the
    rotation recipe, and echoes neither of the two configured values.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_STRONG_TOKEN},{_WEAK_TOKEN}"}
    with pytest.raises(ValueError, match="adepthood") as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert "'adepthood'" in message  # consumer named via repr
    assert "7" in message  # observed length of the rejected token
    assert "32" in message  # the enforced minimum
    assert "secrets.token_urlsafe(32)" in message  # the rotation recipe
    assert _WEAK_TOKEN not in message  # NEVER echo the token value
    assert _STRONG_TOKEN not in message  # nor the compliant one beside it


def test_load_consumer_tokens_rejects_a_repeated_consumer_name() -> None:
    """``adepthood=a;adepthood=b`` is a loud error, never a silent last-wins.

    The pre-#895 parser wrote both entries into one dict key, so the first
    token vanished without a word. Now that a consumer can legitimately hold
    several tokens, either silent reading is indefensible: overwriting throws
    away a credential the operator believes is live, and accumulating quietly
    invents a rotation window they never asked for. The message names the
    consumer and points at the comma form that expresses the intent properly.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN};adepthood={_NEW_TOKEN}"}
    with pytest.raises(ValueError, match="adepthood") as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert "adepthood" in message
    assert "comma" in message.lower()  # the supported spelling is named
    assert _OLD_TOKEN not in message  # NEVER echo the token value
    assert _NEW_TOKEN not in message


def test_load_consumer_tokens_rejects_a_value_shared_by_two_consumers() -> None:
    """One secret configured for two consumers destroys attribution, so it is refused.

    ``verify_token`` scans every consumer without breaking, so a shared value
    resolves to whichever consumer the scan saw last — every audit line that
    call produced would then name the wrong caller. The message names both
    consumers and never the value they share.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN};crawdad={_OLD_TOKEN}"}
    with pytest.raises(ValueError) as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert "adepthood" in message
    assert "crawdad" in message
    assert _OLD_TOKEN not in message  # NEVER echo the token value


def test_load_consumer_tokens_rejects_a_value_repeated_within_one_consumer() -> None:
    """A duplicate inside one set is a rotation that rotated nothing.

    Same rule and same message as the cross-consumer case: every configured
    token value is globally unique, stated once so the two cannot drift.
    """
    env = {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN},{_OLD_TOKEN}"}
    with pytest.raises(ValueError, match="adepthood") as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert "adepthood" in message
    assert _OLD_TOKEN not in message  # NEVER echo the token value


# --------------------------------------------------------------------------- #
# ConsumerTokenVerifier
# --------------------------------------------------------------------------- #


def test_verify_token_returns_access_token_for_valid_bearer() -> None:
    """A known token resolves to its consumer's :class:`AccessToken`."""
    verifier = ConsumerTokenVerifier({"adepthood": ("secret123",)})
    token = asyncio.run(verifier.verify_token("secret123"))
    assert token is not None
    assert token.client_id == "adepthood"
    assert token.scopes == [REMOTE_SCOPE]


def test_verify_token_rejects_unknown_bearer() -> None:
    """An unknown token yields ``None`` (401 at the middleware)."""
    verifier = ConsumerTokenVerifier({"adepthood": ("secret123",)})
    assert asyncio.run(verifier.verify_token("wrong")) is None


def test_verify_token_rejects_against_empty_map() -> None:
    """With no configured tokens, every bearer is rejected."""
    verifier = ConsumerTokenVerifier({})
    assert asyncio.run(verifier.verify_token("anything")) is None


def test_verify_token_rejects_non_ascii_bearer_cleanly() -> None:
    """A non-ASCII bearer is rejected (``None``), never a ``TypeError`` (#776)."""
    verifier = ConsumerTokenVerifier({"adepthood": ("secret123",)})
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
        transport=Transport.STDIO,
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
        transport=Transport.STDIO,
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
        transport=Transport.STDIO,
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
        transport=Transport.STDIO,
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
        transport=Transport.NETWORK,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        token_verifier=ConsumerTokenVerifier({"adepthood": ("secret123",)}),
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
        transport=Transport.NETWORK,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        token_verifier=ConsumerTokenVerifier({"adepthood": (token,)}),
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
    verifier = ConsumerTokenVerifier({"adepthood": ("secret123",)})
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
# ConsumerTokenVerifier — the rotation window (#895)
#
# The #837 TTL bounds a *captured* AccessToken object; it does nothing to the
# wire credential, which the SDK's bearer middleware re-verifies fresh on every
# request. Rotating that credential used to mean a hard cutover: swap the env
# value, restart, and break every consumer still holding the old secret. A
# consumer may now hold an ordered set of currently-valid tokens instead, so the
# operator opens a window, lets consumers redeploy, and then closes it.
#
# The window is only a window if closing it works, so the revocation test below
# is load-bearing: without it this feature is a permanent widening of the
# credential surface wearing a rotation's clothes.
# --------------------------------------------------------------------------- #


def test_every_token_in_the_window_verifies_as_the_same_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old and new both authenticate, as one identity, each with a finite expiry.

    Same ``client_id`` for both is the point: a window that changed the caller's
    name for its duration would rewrite every audit line written while it was
    open. ``expires_at`` is asserted exactly rather than as "not ``None``", so
    the multi-token path cannot quietly issue the never-expiring token #837
    removed.
    """
    monkeypatch.delenv(_TOKEN_TTL_ENV, raising=False)
    monkeypatch.setattr(remote_auth_mod, "_now", lambda: _FIXED_NOW)
    verifier = ConsumerTokenVerifier({"adepthood": (_OLD_TOKEN, _NEW_TOKEN)})
    for presented in (_OLD_TOKEN, _NEW_TOKEN):
        token = asyncio.run(verifier.verify_token(presented))
        assert token is not None
        assert token.client_id == "adepthood"
        assert token.scopes == [REMOTE_SCOPE]
        assert token.token == presented
        assert token.expires_at == 1_000_000 + _DEFAULT_TTL_SECONDS


def test_retiring_a_token_revokes_it_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the old token from the set is what closes the window.

    This is the assertion that makes the feature a *bounded* rotation rather
    than a permanent widening of the credential surface: the retired secret
    authenticates while it is configured and stops the moment it is not.
    """
    monkeypatch.setattr(remote_auth_mod, "_now", lambda: _FIXED_NOW)

    during = ConsumerTokenVerifier({"adepthood": (_OLD_TOKEN, _NEW_TOKEN)})
    assert asyncio.run(during.verify_token(_OLD_TOKEN)) is not None

    after = ConsumerTokenVerifier({"adepthood": (_NEW_TOKEN,)})
    assert asyncio.run(after.verify_token(_OLD_TOKEN)) is None
    assert asyncio.run(after.verify_token(_NEW_TOKEN)) is not None


def test_verifier_refuses_a_bare_string_token_value() -> None:
    """A bare ``str`` map value is refused at runtime, not only by the type checker.

    The signature is ``Mapping[str, Sequence[str]]`` and **not**
    ``Mapping[str, str | Sequence[str]]``, because that union is unsound: ``str``
    already *is* a ``Sequence[str]``, so the union collapses and mypy would wave
    through ``{"adepthood": "secret123"}``. The runtime guard is the half that
    protects a caller who never runs mypy — notably ``tests/v1_api_support.py``,
    which builds verifiers from literal maps.
    """
    with pytest.raises(TypeError) as excinfo:
        ConsumerTokenVerifier({"adepthood": _OLD_TOKEN})
    message = str(excinfo.value)
    assert "adepthood" in message  # the offending key is named
    assert _OLD_TOKEN not in message  # NEVER echo the token value


def test_a_single_character_of_a_configured_token_never_authenticates() -> None:
    """The security complement of the guard above, observed rather than asserted.

    Iterating a bare ``str`` value yields one single-character "token" per
    letter, and ``compare_digest`` accepts each of them, so a 43-character
    secret would degrade into an alphabet of valid credentials. Refusing the
    bare string is the fix; this sweeps every distinct character of a correctly
    configured token to show the consequence is really gone.
    """
    verifier = ConsumerTokenVerifier({"adepthood": (_OLD_TOKEN,)})
    for character in sorted(set(_OLD_TOKEN)):
        assert asyncio.run(verifier.verify_token(character)) is None


def test_verifier_refuses_one_value_shared_by_two_consumers() -> None:
    """Global token uniqueness is the *verifier's* rule, not only the parser's.

    ``tests/v1_api_support.py`` and the wire tests in this module construct
    verifiers directly from literal maps, never going through
    ``load_consumer_tokens``, so the constructor is the narrowest ceiling this
    invariant can sit under. Asserting it here rather than only at the parser
    is what stops a direct caller from configuring an ambiguous identity.
    """
    with pytest.raises(ValueError) as excinfo:
        ConsumerTokenVerifier({"adepthood": (_OLD_TOKEN,), "crawdad": (_OLD_TOKEN,)})
    message = str(excinfo.value)
    assert "adepthood" in message
    assert "crawdad" in message
    assert _OLD_TOKEN not in message  # NEVER echo the token value


def test_verifier_refuses_an_empty_token_set_for_a_named_consumer() -> None:
    """A named consumer holding no tokens authenticates nobody — say so, loudly.

    Silently keeping the entry would leave a consumer in the configuration that
    can never connect, which reads to an operator as an auth outage rather than
    as the typo it is.
    """
    with pytest.raises(ValueError) as excinfo:
        ConsumerTokenVerifier({"adepthood": ()})
    assert "adepthood" in str(excinfo.value)


def test_verifier_refuses_an_empty_iterable_that_is_not_a_sequence() -> None:
    """The same refusal for an empty value that is *truthy* — pinning the ordering.

    Not a second reading of the empty-tuple case above: ``()`` is falsy both
    before and after materialisation, so it is refused under either ordering of
    ``_token_set``'s emptiness check and cannot tell the two apart. An empty
    iterable that is *not* a ``Sequence`` — a spent iterator, an exhausted
    generator — is **truthy**, so it is only caught when the value is
    materialised *before* it is tested. Under the reversed order it would slip
    past the refusal and land as an empty tuple, leaving the named consumer
    able to authenticate nobody: a silent auth outage in place of the loud
    "can never authenticate" error. This pins that ordering, not the branch.
    """
    with pytest.raises(ValueError) as excinfo:
        ConsumerTokenVerifier({"adepthood": iter(())})
    assert "adepthood" in str(excinfo.value)


def test_verifier_over_no_consumers_at_all_stays_legal() -> None:
    """``ConsumerTokenVerifier({})`` is still constructible, and refuses everything.

    "No consumers configured" is a different statement from "this consumer has
    no tokens": the callers treat the empty map as *network mode denied* and
    refuse to serve on it, so making the constructor raise would turn a handled
    posture into a crash.
    """
    verifier = ConsumerTokenVerifier({})
    assert verifier.rotation_notice() is None
    assert asyncio.run(verifier.verify_token(_OLD_TOKEN)) is None


def test_verify_token_compares_against_every_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan never breaks early — not even when the very first token matches.

    Newly load-bearing now the loop is nested consumer → tokens: a ``break`` on
    either level would make verification time depend on *where* in the
    configuration the presented token sits, which is a positional oracle over
    the token set. Counted through ``remote_auth.hmac.compare_digest`` so the
    property is measured rather than read off the source.

    Args:
        monkeypatch: Installs the counting wrapper over the real compare.
    """
    comparisons: list[tuple[bytes, bytes]] = []
    real_compare = remote_auth_mod.hmac.compare_digest

    def _counting(left: bytes, right: bytes) -> bool:
        """Record the comparison, then delegate to the real constant-time one."""
        comparisons.append((left, right))
        return real_compare(left, right)

    # Built before the patch, so only verification's own comparisons are counted.
    verifier = ConsumerTokenVerifier(
        {"adepthood": (_OLD_TOKEN, _NEW_TOKEN), "crawdad": (_THIRD_TOKEN,)}
    )
    monkeypatch.setattr(remote_auth_mod.hmac, "compare_digest", _counting)
    token = asyncio.run(verifier.verify_token(_OLD_TOKEN))

    assert token is not None
    assert token.client_id == "adepthood"
    # Three configured tokens across two consumers => three comparisons, even
    # though the first one matched.
    assert len(comparisons) == 3


# --------------------------------------------------------------------------- #
# rotation_notice — telling the operator a window is open, without a secret
# --------------------------------------------------------------------------- #

_NO_WINDOW_MAPS: list[dict[str, tuple[str, ...]]] = [
    {},
    {"adepthood": (_STRONG_TOKEN,)},
    {"adepthood": (_STRONG_TOKEN,), "crawdad": (_OLD_TOKEN,)},
]

_NO_WINDOW_IDS = ["no-consumers", "one-consumer", "two-consumers"]


@pytest.mark.parametrize("tokens", _NO_WINDOW_MAPS, ids=_NO_WINDOW_IDS)
def test_rotation_notice_is_none_when_no_window_is_open(
    tokens: Mapping[str, Sequence[str]],
) -> None:
    """One token per consumer is the steady state, and the steady state is silent.

    A notice printed on every start is a notice operators stop reading, at which
    point the one that matters — "you left a window open three months ago" —
    goes unread too.

    Args:
        tokens: A configuration in which no consumer holds more than one token.
    """
    assert ConsumerTokenVerifier(tokens).rotation_notice() is None


def test_rotation_notice_names_every_consumer_in_a_window() -> None:
    """Each multi-token consumer is named with its count; settled ones are not.

    Naming only the consumers mid-rotation is what makes the notice actionable:
    it is a to-do list of windows still to close, not an inventory of the
    configuration.
    """
    verifier = ConsumerTokenVerifier(
        {
            "adepthood": (_OLD_TOKEN, _NEW_TOKEN),
            "crawdad": (_THIRD_TOKEN, _FOURTH_TOKEN, _FIFTH_TOKEN),
            "settled": (_STRONG_TOKEN,),
        }
    )
    notice = verifier.rotation_notice()
    assert notice is not None
    assert "adepthood" in notice
    assert "2" in notice  # adepthood's token count
    assert "crawdad" in notice
    assert "3" in notice  # crawdad's token count
    assert "settled" not in notice  # a single-token consumer is not in a window


def test_rotation_notice_carries_no_token_value() -> None:
    """The notice takes names and counts, so it cannot echo a secret by construction.

    It is printed at startup, which means it lands in logs, terminals and
    process supervisors — the same audience the #838 rejection message is
    written for, and the same reason it must stay value-free.
    """
    verifier = ConsumerTokenVerifier(
        {
            "adepthood": (_OLD_TOKEN, _NEW_TOKEN),
            "crawdad": (_THIRD_TOKEN, _FOURTH_TOKEN),
        }
    )
    notice = verifier.rotation_notice()
    assert notice is not None
    for configured in _ALL_TEST_TOKENS:
        assert configured not in notice


# --------------------------------------------------------------------------- #
# A credential that names nobody (#1100)
# --------------------------------------------------------------------------- #

_UNNAMED_CONSUMERS: Final[tuple[str, ...]] = ("", " ", "   ", "\t", "\n", " \t\n ")
"""Every spelling of "this credential identifies nobody"."""

_UNNAMED_IDS: Final[tuple[str, ...]] = (
    "empty",
    "one-space",
    "three-spaces",
    "tab",
    "newline",
    "mixed-whitespace",
)


@pytest.mark.parametrize("consumer", _UNNAMED_CONSUMERS, ids=_UNNAMED_IDS)
def test_verifier_refuses_a_consumer_name_that_identifies_nobody(
    consumer: str,
) -> None:
    """A blank or whitespace-only consumer key is refused at construction (#1100).

    ``_normalized_token_sets`` is the narrowest shared ceiling: both
    ``load_consumer_tokens`` and this constructor funnel through it, and the
    constructor is reachable *without* the environment parser — from
    ``tests/v1_api_support.py``, from the wire tests, and from
    ``creek_mcp.httpapi.app.create_app(verifier=...)``. A consumer whose name
    strips to nothing would be admitted as a real identity there and stamped
    on every audit line, access line and ceiling decision below as ``''``.

    Args:
        consumer: One spelling of "no name at all".
    """
    with pytest.raises(ValueError, match="names no consumer") as excinfo:
        ConsumerTokenVerifier({consumer: (_STRONG_TOKEN,)})
    message = str(excinfo.value)
    assert _STRONG_TOKEN not in message  # NEVER echo the token value


def test_unnamed_consumers_are_refused_rather_than_admitted_as_distinct_ones() -> None:
    """Three whitespace names are three *distinct* map keys, and one non-identity.

    This is the blast radius the issue understates. ``''``, ``'   '`` and
    ``'\\t'`` do not collapse: they are three separate keys, three separate
    verified identities, and — once per-consumer accounting exists — three
    separate quota buckets for a caller with no name at all. The refusal has
    to land before any of that, which means at construction.
    """
    unnamed = {"": (_OLD_TOKEN,), "   ": (_NEW_TOKEN,), "\t": (_THIRD_TOKEN,)}
    assert len(unnamed) == 3, "three distinct keys, all of them nobody"
    with pytest.raises(ValueError, match="names no consumer"):
        ConsumerTokenVerifier(unnamed)


def test_load_consumer_tokens_still_drops_a_blank_name_rather_than_failing_boot() -> (
    None
):
    """The env parser keeps *skipping* nameless entries; it does not now refuse.

    The new guard sits in ``_normalized_token_sets``, which
    ``load_consumer_tokens`` calls last — so a guard written carelessly would
    turn an operator's stray ``;`` or ``=orphan`` into a boot failure. It does
    not, because ``_parsed_entry`` has already dropped those before the
    normaliser ever sees them.
    """
    env = {CONSUMER_TOKENS_ENV: f" = {_OLD_TOKEN} ; adepthood={_STRONG_TOKEN} ; ="}
    assert load_consumer_tokens(env) == {"adepthood": (_STRONG_TOKEN,)}


class _UnnamedTokenVerifier(TokenVerifier):
    """A custom verifier that authenticates a bearer but names no consumer.

    The case the constructor guard cannot see: an arbitrary
    :class:`~mcp.server.auth.provider.TokenVerifier` handed to
    :func:`creek_mcp.server.build_server` never runs
    ``_normalized_token_sets`` at all.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token whose ``client_id`` names nobody.

        Args:
            token: The presented bearer.

        Returns:
            An :class:`AccessToken` with a blank ``client_id``.
        """
        return AccessToken(
            token=token, client_id="", scopes=[REMOTE_SCOPE], expires_at=None
        )


class _NamedTokenVerifier(TokenVerifier):
    """The non-vacuity twin: a custom verifier that does name its consumer."""

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token naming ``adepthood``.

        Args:
            token: The presented bearer.

        Returns:
            An :class:`AccessToken` with a usable ``client_id``.
        """
        return AccessToken(
            token=token, client_id="adepthood", scopes=[REMOTE_SCOPE], expires_at=None
        )


def test_the_verifier_publishes_its_configured_consumer_names() -> None:
    """``consumers`` is the public read a per-consumer limiter must build from (#1110).

    Eagerly, and from *this* — never lazily from whatever name arrives on a
    request. A bucket map that grew on first sight would hand a bucket to every
    distinct identity any future verifier let through, which turns a limit into
    an amplifier. Configured order is preserved so an operator reading a
    diagnostic sees their own configuration back.
    """
    configured = ConsumerTokenVerifier(
        {"adepthood": (_OLD_TOKEN, _NEW_TOKEN), "crawdad": (_THIRD_TOKEN,)}
    )
    assert configured.consumers == ("adepthood", "crawdad")


def test_the_published_consumer_names_carry_no_token_value() -> None:
    """The accessor exposes names only, so it cannot leak a secret by construction.

    A limiter, a diagnostic or a log line reading this must not be able to
    reach a credential through it.
    """
    configured = ConsumerTokenVerifier(
        {"adepthood": (_OLD_TOKEN, _NEW_TOKEN), "crawdad": (_THIRD_TOKEN,)}
    )
    rendered = repr(configured.consumers)
    for token in _ALL_TEST_TOKENS:
        assert token not in rendered


def test_build_server_refuses_a_token_verifier_that_names_no_consumer(
    vault: Path,
) -> None:
    """A custom verifier's blank ``client_id`` authenticates nobody, before dispatch.

    The SDK's ``RequireAuthMiddleware`` answers ``401`` for a verifier that
    returns ``None``, so mapping "authenticated but unnamed" onto ``None`` at
    the boundary is the 401-equivalent refusal AC#1 asks for — and it lands
    before any tool runs, before ``_effective_consumer`` is consulted and
    before an audit entry is written under an empty name.

    Args:
        vault: Seeded vault root.
    """
    server = build_server(
        transport=Transport.NETWORK,
        vault_path=vault,
        token_verifier=_UnnamedTokenVerifier(),
    )
    installed = server._token_verifier
    assert installed is not None
    assert asyncio.run(installed.verify_token(_STRONG_TOKEN)) is None


def test_build_server_still_admits_a_token_verifier_that_names_a_consumer(
    vault: Path,
) -> None:
    """The guard refuses *unnamed* credentials only, and passes the identity through.

    Without this twin, a guard that refused every custom verifier outright
    would satisfy the test above and break the network transport.

    Args:
        vault: Seeded vault root.
    """
    server = build_server(
        transport=Transport.NETWORK,
        vault_path=vault,
        token_verifier=_NamedTokenVerifier(),
    )
    installed = server._token_verifier
    assert installed is not None
    access = asyncio.run(installed.verify_token(_STRONG_TOKEN))
    assert access is not None
    assert access.client_id == "adepthood"
    assert access.scopes == [REMOTE_SCOPE]


# --------------------------------------------------------------------------- #
# Cross-cutting: no refusal on ANY of the new paths ever echoes a token
# --------------------------------------------------------------------------- #

_RAISING_CONFIGURATIONS: list[Callable[[], object]] = [
    lambda: load_consumer_tokens(
        {CONSUMER_TOKENS_ENV: f"adepthood={_STRONG_TOKEN},{_WEAK_TOKEN}"}
    ),
    lambda: load_consumer_tokens(
        {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN};adepthood={_NEW_TOKEN}"}
    ),
    lambda: load_consumer_tokens(
        {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN};crawdad={_OLD_TOKEN}"}
    ),
    lambda: load_consumer_tokens(
        {CONSUMER_TOKENS_ENV: f"adepthood={_OLD_TOKEN},{_OLD_TOKEN}"}
    ),
    lambda: ConsumerTokenVerifier({"": (_OLD_TOKEN,)}),
    lambda: ConsumerTokenVerifier({"   ": (_OLD_TOKEN,)}),
    lambda: ConsumerTokenVerifier({"adepthood": _OLD_TOKEN}),
    lambda: ConsumerTokenVerifier({"adepthood": ()}),
    lambda: ConsumerTokenVerifier(
        {"adepthood": (_OLD_TOKEN,), "crawdad": (_OLD_TOKEN,)}
    ),
]

_RAISING_IDS = [
    "parser-weak-token-inside-a-set",
    "parser-repeated-consumer-name",
    "parser-value-shared-across-consumers",
    "parser-value-repeated-within-one-set",
    "verifier-empty-consumer-name",
    "verifier-whitespace-consumer-name",
    "verifier-bare-string-value",
    "verifier-empty-token-set",
    "verifier-value-shared-across-consumers",
]


@pytest.mark.parametrize("configure", _RAISING_CONFIGURATIONS, ids=_RAISING_IDS)
def test_no_rejection_message_ever_echoes_a_token(
    configure: Callable[[], object],
) -> None:
    """Every refusal on the #895 surface names configuration, never credentials.

    Swept as one parametrize rather than left to each test's own assertion so a
    path added later is a path that has to be added here too. Startup errors
    land in logs, terminals and process supervisors, so a message that echoed
    the value it rejected would publish the secret it exists to protect.

    Args:
        configure: A zero-argument call that must raise.
    """
    with pytest.raises((TypeError, ValueError)) as excinfo:
        configure()
    message = str(excinfo.value)
    for configured in _ALL_TEST_TOKENS:
        assert configured not in message, message


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


_UVICORN_ACCESS_LOGGER: Final[str] = "uvicorn.access"
"""The logger uvicorn writes ``client_addr - "METHOD /path" status`` to.

Its handler list is what ``access_log=False`` actually empties, and
``h11_impl`` consults ``hasHandlers()`` to decide whether to log at all — so
"no handlers" is the observable form of the promise, not the flag.
"""

_UVICORN_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    _UVICORN_ACCESS_LOGGER,
    "uvicorn.asgi",
)
"""Every logger :data:`uvicorn.config.LOGGING_CONFIG` reconfigures.

``uvicorn.Config.__init__`` calls ``configure_logging()``, which calls
:func:`logging.config.dictConfig` — a mutation of *global* interpreter state
that would otherwise outlive the test and change what every later module in the
session can observe.
"""


class _AnyASGIApp:
    """A callable that satisfies :class:`uvicorn.Config` without being a server.

    uvicorn only needs something callable; nothing here is ever awaited, because
    no test in this module binds a socket.
    """

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        """Accept the ASGI three-tuple and do nothing with it."""


@pytest.fixture
def restored_uvicorn_logging() -> Iterator[None]:
    """Snapshot and restore the four ``uvicorn`` loggers around one test.

    Yields:
        ``None``. The restoration happens on the way back out.
    """
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in _UVICORN_LOGGERS
    }
    try:
        yield
    finally:
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Substitute uvicorn's ``Config`` and ``Server`` so nothing binds a socket.

    One helper rather than a copy per test: three tests now ask what
    ``_serve_network`` hands uvicorn, and three hand-rolled fakes are three
    chances for one of them to accept a keyword the real ``uvicorn.Config``
    would reject.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The dict the fake config records ``app`` and every keyword into.
    """
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
    return captured


def test_serve_network_uses_ssl_when_tls_configured(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TLS configured, ``_serve_network`` hands uvicorn the cert + key files."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")

    captured = _capture_uvicorn(monkeypatch)

    server = _network_server(vault, _WIRE_TOKEN)
    server_mod._serve_network(
        server,
        _parsed_network_args(host="0.0.0.0", port=8443, tls_cert=cert, tls_key=key),
    )

    assert str(captured["ssl_certfile"]) == str(cert)
    assert str(captured["ssl_keyfile"]) == str(key)


def test_serve_network_disables_uvicorns_own_access_log(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE INVARIANT (#1125) — the MCP adapter suppresses uvicorn's access logger too.

    ``creek_mcp/httpapi/cli.py`` has switched it off since #1117, and
    ``tests/test_v1_api_uvicorn_logging.py`` proves uvicorn honours the flag.
    This call site built its own :class:`uvicorn.Config` and kept the default,
    so the two adapters logged the client address of every request under two
    different postures — and a posture that differs between two surfaces of one
    server is one an operator cannot reason about at all.

    Args:
        vault: The seeded vault.
        tmp_path: Where the TLS fixture material is written.
        monkeypatch: Substitutes uvicorn's config and server so nothing binds.
    """
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")

    captured = _capture_uvicorn(monkeypatch)

    server = _network_server(vault, _WIRE_TOKEN)
    server_mod._serve_network(
        server,
        _parsed_network_args(host="0.0.0.0", port=8443, tls_cert=cert, tls_key=key),
    )

    assert captured["access_log"] is False


def test_the_mcp_config_leaves_uvicorns_access_logger_unhandled(
    tmp_path: Path, restored_uvicorn_logging: None
) -> None:
    """A real config built the MCP way installs no handler on ``uvicorn.access``.

    A flag is not a behaviour. ``uvicorn/config.py`` implements ``access_log=False``
    by emptying ``logging.getLogger("uvicorn.access").handlers`` and setting
    ``propagate = False``, and ``h11_impl`` then decides whether to log at all
    with ``hasHandlers()`` — so an unhandled logger is the state that actually
    keeps the client address out of the operator's stream. Built directly rather
    than through ``_serve_network`` because the point is what uvicorn does with
    the configuration, and binding a socket for it would be a second copy of
    ``tests/test_v1_api_uvicorn_logging.py``.

    Args:
        tmp_path: Where the TLS fixture material is written.
        restored_uvicorn_logging: Puts the four uvicorn loggers back afterwards;
            constructing a config calls :func:`logging.config.dictConfig`, which
            is a mutation of global interpreter state.
    """
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")
    config = server_mod.build_network_uvicorn_config(
        _AnyASGIApp(),
        _parsed_network_args(host="0.0.0.0", port=8443, tls_cert=cert, tls_key=key),
        log_level="info",
    )
    assert config.access_log is False
    assert logging.getLogger(_UVICORN_ACCESS_LOGGER).handlers == []


def test_the_unhandled_access_logger_assertion_is_not_vacuous(
    tmp_path: Path, restored_uvicorn_logging: None
) -> None:
    """THE NON-VACUITY TWIN — with the flag on, uvicorn *does* install a handler.

    Without this, the assertion above would pass on any uvicorn that stopped
    configuring the logger at all, and the leak would come back green.

    Args:
        tmp_path: Unused beyond keeping the two tests symmetrical.
        restored_uvicorn_logging: Restores the mutated loggers.
    """
    assert tmp_path.exists()
    uvicorn.Config(_AnyASGIApp(), access_log=True)
    assert logging.getLogger(_UVICORN_ACCESS_LOGGER).handlers != []


def test_serve_network_still_carries_the_bind_and_tls_material(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extracting the factory must not drop what ``_serve_network`` already passed.

    A factory that silenced the access log and lost the certificate would
    satisfy the invariant above and serve bearer tokens in cleartext.

    Args:
        vault: The seeded vault.
        tmp_path: Where the TLS fixture material is written.
        monkeypatch: Substitutes uvicorn's config and server so nothing binds.
    """
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")

    captured = _capture_uvicorn(monkeypatch)

    server = _network_server(vault, _WIRE_TOKEN)
    server_mod._serve_network(
        server,
        _parsed_network_args(host="0.0.0.0", port=8443, tls_cert=cert, tls_key=key),
    )

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8443
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
        transport=Transport.STDIO,
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
