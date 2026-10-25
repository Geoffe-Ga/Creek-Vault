"""Per-agent tool profiles: grants decided in policy, enforced by MCP (#1964).

A remote consumer holds a *grant set*. The server answers ``tools/list`` with
only the tools that set admits, and refuses a ``tools/call`` outside it before
dispatch, with one constant reason, audited as refused under the tool name
only. The decision is :func:`creek_mcp.policy.admitted_tool`, beside the tier
cap, so ``/v1`` can reuse the same verdict.

Groups, in order:

1. The grant vocabulary and the four profiles (policy, no MCP in scope).
2. :func:`~creek_mcp.policy.admitted_tool` — local never restricted, a remote
   identity with no grants refused for everything, an ungated static consumer
   unrestricted as before #1964.
3. :func:`~creek_mcp.policy.granted_by_scopes` — the one identity -> grants
   seam, which C2's JWTs and static tokens both feed.
4. Drift gates: every registered non-purge tool is in ``TOOL_GRANTS``, and the
   ``read_gate`` posture manifest's grant column agrees with it.
5. The MCP adapter: ``list_tools`` filtered per call, ``call_tool`` refused
   before dispatch and audited, cross-consumer isolation, local unaffected.
6. Static-token grant suffixes in ``CREEK_MCP_CONSUMER_TOKENS`` and the
   "ungated" startup announcement.
7. One over-the-wire pin through the real bearer middleware.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Final, cast

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from creek_mcp import server as server_mod
from creek_mcp.audit import AUDIT_OUTCOME_REFUSED, MCP_AUDIT_RELPATH
from creek_mcp.httpapi.auth import build_verifier
from creek_mcp.policy import (
    DEFAULT_GRANTS,
    TOOL_GRANTS,
    TOOL_NOT_GRANTED_REASON,
    UNGATED,
    UNGATED_SCOPE,
    Admission,
    CallerIdentity,
    Grant,
    Refusal,
    Transport,
    admitted_tool,
    granted_by_scopes,
)
from creek_mcp.read_gate import TOOL_POSTURES, ReadPosture
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    REMOTE_SCOPE,
    ConsumerTokenVerifier,
    announce_ungated_consumers,
    load_consumer_grants,
    load_consumer_tokens,
)
from creek_mcp.server import UNREGISTERED_TOOL_AUDIT_NAME, build_server
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP


# --------------------------------------------------------------------------- #
# Shared fixtures and helpers
# --------------------------------------------------------------------------- #

# The exact refusal text. Spelled out rather than imported so a reword cannot
# sail through: this is the one sentence an out-of-profile caller is told.
_NOT_GRANTED_LITERAL: Final[str] = "this credential is not granted the requested tool"

_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "creek.handshake",
        "creek.state.read",
        "creek.wheel",
        "creek.mine",
        "creek.classify.entry",
        "creek.lint",
    }
)
_DESK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "creek.draft",
        "creek.author",
        "creek.revoice",
        "creek.reflect",
        "creek.save",
        "creek.jobs.read",
    }
)
_JOURNAL_TOOLS: Final[frozenset[str]] = frozenset({"creek.journal", "creek.upload"})
_PIPELINE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "creek.classify",
        "creek.link",
        "creek.compile",
        "creek.report",
        "creek.skills.refresh",
        "creek.ingest",
        "creek.redact.scan",
        "creek.state.render",
    }
)
_PURGE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "creek.purge.fragment",
        "creek.purge.source",
        "creek.purge.classifications",
        "creek.purge.daterange",
        "creek.purge.vault",
    }
)
_DEFAULT_PROFILE_TOOL_COUNT: Final[int] = 12

# Floor-clearing (>= 32 chars) fake test literals, never real credentials.
_GATED_TOKEN: Final[str] = "gated-consumer-test-token-" + "g" * 20
_UNGATED_TOKEN: Final[str] = "ungated-consumer-test-token-" + "u" * 20
_ROTATING_TOKEN: Final[str] = "rotating-consumer-test-token-" + "r" * 20


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault root for the adapter tests."""
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


def _remote(grants: frozenset[Grant]) -> CallerIdentity:
    """Return a remote identity holding exactly *grants*.

    Args:
        grants: The grant set the identity holds.

    Returns:
        A remote :class:`CallerIdentity` named ``acct_91``.
    """
    return CallerIdentity(consumer="acct_91", is_remote=True, grants=grants)


def _token(consumer: str, scopes: list[str]) -> AccessToken:
    """Build an :class:`AccessToken` carrying *scopes*, as a verifier would.

    Args:
        consumer: The ``client_id`` the token names.
        scopes: The scopes the token carries.

    Returns:
        The access token.
    """
    return AccessToken(
        token="opaque",  # test literal, not a real credential
        client_id=consumer,
        scopes=scopes,
        expires_at=None,
    )


def _gated_scopes(*grants: Grant) -> list[str]:
    """Return the scopes a gated static token carries for *grants*.

    Args:
        *grants: The grants the token holds.

    Returns:
        ``creek:remote`` plus each grant's value.
    """
    return [REMOTE_SCOPE, *(grant.value for grant in grants)]


def _present(monkeypatch: pytest.MonkeyPatch, token: AccessToken | None) -> None:
    """Make every later call arrive with *token* (``None`` = local stdio).

    Args:
        monkeypatch: The active monkeypatch fixture.
        token: The access token the request carries, or ``None``.
    """
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: token)


def _server(vault: Path) -> FastMCP:
    """Build a stdio-built server whose identity the tests drive per call.

    Args:
        vault: The seeded vault root.

    Returns:
        The built server, with a stub draft LLM so no provider is needed.
    """
    return build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )


def _listed(server: FastMCP) -> set[str]:
    """Return the tool names ``list_tools`` answers with right now.

    Args:
        server: The built server.

    Returns:
        The listed tool names.
    """
    return {tool.name for tool in asyncio.run(server.list_tools())}


def _structured(result: object) -> object:
    """Pull the structured payload out of a FastMCP ``call_tool`` result.

    Args:
        result: What ``call_tool`` returned.

    Returns:
        The structured half of a ``(content, structured)`` pair, else *result*.
    """
    if isinstance(result, tuple):
        return result[1]
    return result


def _call(server: FastMCP, tool: str, arguments: dict[str, object]) -> object:
    """Invoke *tool* through the public boundary; return its structured payload.

    Args:
        server: The built server.
        tool: The tool name to call.
        arguments: The raw arguments.

    Returns:
        The structured payload.
    """
    return _structured(asyncio.run(server.call_tool(tool, arguments)))


def _audit_entries(vault: Path) -> list[dict[str, object]]:
    """Return every MCP audit entry written under *vault*, in write order.

    Args:
        vault: The vault root.

    Returns:
        The parsed entries; empty when nothing was audited.
    """
    path = vault / MCP_AUDIT_RELPATH
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _not_granted(tool: str) -> dict[str, object]:
    """Return the exact refusal payload an out-of-profile call receives.

    Args:
        tool: The refused tool's name.

    Returns:
        The canonical refusal payload, with the transport-default ceiling.
    """
    return refusal_response(
        tool=tool, ceiling=TierCeiling.OPEN, reason=_NOT_GRANTED_LITERAL
    )


# --------------------------------------------------------------------------- #
# Group 1 -- the grant vocabulary and the four profiles
# --------------------------------------------------------------------------- #


def test_grant_values_are_the_four_published_scope_names() -> None:
    """The four grants spell the OAuth scope names a consent screen shows."""
    assert [grant.value for grant in Grant] == [
        "vault:read",
        "vault:desk",
        "vault:journal",
        "vault:pipeline",
    ]


def test_default_profile_for_a_new_agent_is_read_and_desk() -> None:
    """A new agent drafts and reflects; it cannot journal or run corpus passes."""
    assert frozenset({Grant.READ, Grant.DESK}) == DEFAULT_GRANTS


@pytest.mark.parametrize(
    ("grant", "expected"),
    [
        (Grant.READ, _READ_TOOLS),
        (Grant.DESK, _DESK_TOOLS),
        (Grant.JOURNAL, _JOURNAL_TOOLS),
        (Grant.PIPELINE, _PIPELINE_TOOLS),
    ],
    ids=["read", "desk", "journal", "pipeline"],
)
def test_each_profile_names_exactly_its_tools(
    grant: Grant, expected: frozenset[str]
) -> None:
    """Each grant admits exactly the tools the issue's profile table lists."""
    actual = {name for name, required in TOOL_GRANTS.items() if required is grant}
    assert actual == expected


def test_no_purge_tool_is_in_any_profile() -> None:
    """Purge stays behind the elevated token alone; no grant reaches it."""
    assert _PURGE_TOOLS.isdisjoint(TOOL_GRANTS)


def test_tool_grants_is_read_only() -> None:
    """The table cannot be widened at runtime by a caller holding a reference."""
    # Cast to the mutable shape a careless caller would assume; the runtime
    # object is a read-only proxy, which is the property under test.
    table = cast("dict[str, Grant]", TOOL_GRANTS)
    with pytest.raises(TypeError):
        table["creek.purge.vault"] = Grant.READ


def test_not_granted_reason_is_the_exact_published_literal() -> None:
    """The refusal sentence is pinned against a hard-coded literal."""
    assert TOOL_NOT_GRANTED_REASON == _NOT_GRANTED_LITERAL


def test_not_granted_reason_names_no_tool_and_no_grant() -> None:
    """The reason is constant: no tool list, no grant held, no template marks."""
    for name in TOOL_GRANTS:
        assert name not in TOOL_NOT_GRANTED_REASON
    for grant in Grant:
        assert grant.value not in TOOL_NOT_GRANTED_REASON
    for mark in ("{", "}", "%"):
        assert mark not in TOOL_NOT_GRANTED_REASON


# --------------------------------------------------------------------------- #
# Group 2 -- admitted_tool
# --------------------------------------------------------------------------- #


def test_issue_example_default_profile_admits_draft() -> None:
    """``admitted_tool(identity, "creek.draft")`` -> Admission."""
    identity = _remote(frozenset({Grant.READ, Grant.DESK}))
    assert admitted_tool(identity, "creek.draft") == Admission(ceiling=None)


def test_issue_example_default_profile_refuses_link() -> None:
    """``admitted_tool(identity, "creek.link")`` -> Refusal(TOOL_NOT_GRANTED_REASON)."""
    identity = _remote(frozenset({Grant.READ, Grant.DESK}))
    assert admitted_tool(identity, "creek.link") == Refusal(reason=_NOT_GRANTED_LITERAL)


def test_issue_example_stdio_is_unchanged() -> None:
    """A local identity is never restricted, purge and pipeline included."""
    local = CallerIdentity(None, False)
    for name in (*TOOL_GRANTS, *_PURGE_TOOLS, "creek.not-registered"):
        assert admitted_tool(local, name) == Admission(ceiling=None)


def test_remote_identity_with_no_grants_is_refused_for_every_tool() -> None:
    """No grants is a credential that authenticates and can call nothing."""
    identity = _remote(frozenset())
    for name in (*TOOL_GRANTS, *_PURGE_TOOLS):
        assert admitted_tool(identity, name) == Refusal(reason=_NOT_GRANTED_LITERAL)


def test_remote_identity_defaults_to_no_grants() -> None:
    """Forgetting the grants fails closed: the default is the empty set."""
    identity = CallerIdentity(consumer="acct_91", is_remote=True)
    assert identity.grants == frozenset()
    assert isinstance(admitted_tool(identity, "creek.handshake"), Refusal)


@pytest.mark.parametrize("grant", list(Grant), ids=[g.value for g in Grant])
def test_a_single_grant_admits_its_tools_and_nothing_else(grant: Grant) -> None:
    """A grant admits its own profile and refuses every other tool."""
    identity = _remote(frozenset({grant}))
    for name, required in TOOL_GRANTS.items():
        verdict = admitted_tool(identity, name)
        if required is grant:
            assert verdict == Admission(ceiling=None)
        else:
            assert verdict == Refusal(reason=_NOT_GRANTED_LITERAL)


def test_every_grant_still_refuses_purge_and_unknown_names() -> None:
    """Holding all four grants never reaches purge, nor an unregistered name."""
    identity = _remote(frozenset(Grant))
    for name in (*_PURGE_TOOLS, "creek.not-registered", ""):
        assert admitted_tool(identity, name) == Refusal(reason=_NOT_GRANTED_LITERAL)


def test_ungated_remote_identity_is_unrestricted_as_before() -> None:
    """A static consumer with no grant suffix keeps its pre-#1964 reach."""
    identity = CallerIdentity(consumer="adepthood", is_remote=True, grants=UNGATED)
    for name in (*TOOL_GRANTS, *_PURGE_TOOLS):
        assert admitted_tool(identity, name) == Admission(ceiling=None)


def test_every_refusal_carries_the_identical_reason() -> None:
    """Refused-because-ungranted and refused-because-unknown read the same."""
    identity = _remote(frozenset({Grant.READ}))
    reasons = {
        verdict.reason
        for name in ("creek.link", "creek.purge.vault", "creek.nope")
        if isinstance(verdict := admitted_tool(identity, name), Refusal)
    }
    assert reasons == {_NOT_GRANTED_LITERAL}


# --------------------------------------------------------------------------- #
# Group 3 -- granted_by_scopes: the identity -> grants seam
# --------------------------------------------------------------------------- #


def test_vault_scopes_become_grants_and_other_scopes_are_ignored() -> None:
    """``vault:*`` scopes are grants; ``creek:remote`` and strangers are not."""
    scopes = [REMOTE_SCOPE, "vault:read", "openid", "vault:desk", "vault:admin"]
    assert granted_by_scopes(scopes) == frozenset({Grant.READ, Grant.DESK})


def test_the_ungated_marker_alone_is_ungated() -> None:
    """Only the static verifier's explicit marker reads as ungated."""
    assert granted_by_scopes([REMOTE_SCOPE, UNGATED_SCOPE]) is UNGATED


def test_explicit_grants_win_over_the_ungated_marker() -> None:
    """A token carrying both is read restrictively, never as unrestricted."""
    scopes = [UNGATED_SCOPE, "vault:journal"]
    assert granted_by_scopes(scopes) == frozenset({Grant.JOURNAL})


@pytest.mark.parametrize(
    "scopes",
    [[], [REMOTE_SCOPE], ["openid", "profile"], ["VAULT:READ", " vault:read"]],
    ids=["empty", "remote-only", "foreign", "near-misses"],
)
def test_no_grant_scope_is_no_grants(scopes: list[str]) -> None:
    """A token with no grant scope holds no grants (exact matching only)."""
    assert granted_by_scopes(scopes) == frozenset()


# --------------------------------------------------------------------------- #
# Group 4 -- drift gates
# --------------------------------------------------------------------------- #


def test_every_registered_non_purge_tool_names_its_grant(vault: Path) -> None:
    """Registering a tool without deciding its grant turns this red."""
    registered = _listed(_server(vault))
    assert set(TOOL_GRANTS) == registered - _PURGE_TOOLS
    assert registered >= _PURGE_TOOLS


def test_posture_manifest_grant_column_matches_the_grant_table() -> None:
    """``read_gate.TOOL_POSTURES`` and ``policy.TOOL_GRANTS`` cannot disagree."""
    column = {name: posture.grant for name, posture in TOOL_POSTURES.items()}
    expected = {name: TOOL_GRANTS.get(name) for name in TOOL_POSTURES}
    assert column == expected


def test_only_elevated_token_tools_carry_no_grant() -> None:
    """A grantless posture entry is exactly an ``AUTH_TOKEN`` purge tool."""
    grantless = {name for name, p in TOOL_POSTURES.items() if p.grant is None}
    elevated = {
        name for name, p in TOOL_POSTURES.items() if p.posture is ReadPosture.AUTH_TOKEN
    }
    assert grantless == elevated == _PURGE_TOOLS


# --------------------------------------------------------------------------- #
# Group 5 -- the MCP adapter
# --------------------------------------------------------------------------- #


def test_list_tools_for_the_default_profile_is_twelve_tools(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default profile includes revoice (#1981), but neither link nor purge."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ, Grant.DESK)))
    listed = _listed(server)
    assert listed == _READ_TOOLS | _DESK_TOOLS
    assert len(listed) == _DEFAULT_PROFILE_TOOL_COUNT


def test_list_tools_for_a_grantless_remote_is_empty(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token carrying no grant scope is shown no tools at all."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", [REMOTE_SCOPE]))
    assert _listed(server) == set()


def test_list_tools_is_unchanged_for_local_and_ungated_callers(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stdio and an ungated static consumer both still see every tool."""
    server = _server(vault)
    _present(monkeypatch, None)
    local = _listed(server)
    _present(monkeypatch, _token("adepthood", [REMOTE_SCOPE, UNGATED_SCOPE]))
    ungated = _listed(server)
    assert local == ungated == set(TOOL_GRANTS) | _PURGE_TOOLS


def test_list_tools_is_decided_per_call_never_hoisted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two bearers through one built server each get their own list."""
    server = _server(vault)
    bearer: list[AccessToken] = [_token("writer", _gated_scopes(Grant.JOURNAL))]
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: bearer[0])
    first = _listed(server)
    bearer[0] = _token("reader", _gated_scopes(Grant.READ))
    second = _listed(server)
    assert first == _JOURNAL_TOOLS
    assert second == _READ_TOOLS


def test_out_of_profile_call_is_refused_and_audited_by_name_only(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``creek.link`` is refused before dispatch and audited as refused."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ, Grant.DESK)))
    result = _call(server, "creek.link", {"privacy_tier_ceiling": "personal"})
    assert result == _not_granted("creek.link")
    entries = _audit_entries(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.link"
    assert entry["outcome"] == AUDIT_OUTCOME_REFUSED
    assert entry["args_summary"] == {}
    assert entry["consumer"] == "acct_91"
    assert entry["tier_ceiling"] == TierCeiling.OPEN.value


def test_grant_refusal_comes_before_the_ceiling_cap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ungranted tool at ``intimate`` gets the grant reason, not the cap's."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ)))
    result = _call(server, "creek.journal", {"privacy_tier_ceiling": "intimate"})
    assert result == _not_granted("creek.journal")


def test_gated_purge_is_refused_before_the_elevated_gate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gated remote never reaches purge: no elevated-gate entry is written."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(*Grant)))
    result = _call(server, "creek.purge.fragment", {"fragment_id": "frag-x"})
    assert result == _not_granted("creek.purge.fragment")
    entries = _audit_entries(vault)
    assert [entry.get("outcome") for entry in entries] == [AUDIT_OUTCOME_REFUSED]
    assert entries[0]["args_summary"] == {}


def test_unregistered_name_is_refused_and_audited_under_a_placeholder(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe for a name that does not exist writes no caller text to the log."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ)))
    probe = "creek." + "x" * 200
    result = _call(server, probe, {})
    assert result == _not_granted(probe)
    entries = _audit_entries(vault)
    assert [entry["tool"] for entry in entries] == [UNREGISTERED_TOOL_AUDIT_NAME]


def test_in_profile_call_dispatches(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A granted tool dispatches and audits as the tool itself does."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ)))
    result = _call(server, "creek.wheel", {"privacy_tier_ceiling": "open"})
    assert isinstance(result, dict)
    assert result.get("reason") != _NOT_GRANTED_LITERAL
    entries = _audit_entries(vault)
    assert [entry["tool"] for entry in entries] == ["creek.wheel"]
    assert "outcome" not in entries[0]


def test_cross_consumer_grants_never_bleed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One consumer's grant admits nothing for another through one server."""
    server = _server(vault)
    bearer: list[AccessToken] = [_token("reader", _gated_scopes(Grant.READ))]
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: bearer[0])
    admitted = _call(server, "creek.wheel", {})
    bearer[0] = _token("writer", _gated_scopes(Grant.JOURNAL))
    refused = _call(server, "creek.wheel", {})
    assert isinstance(admitted, dict)
    assert admitted.get("reason") != _NOT_GRANTED_LITERAL
    assert refused == _not_granted("creek.wheel")
    consumers = [(e["consumer"], e.get("outcome")) for e in _audit_entries(vault)]
    assert consumers == [("reader", None), ("writer", AUDIT_OUTCOME_REFUSED)]


def test_local_pipeline_call_is_unaffected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stdio reaches ``creek.link`` exactly as before: no grant refusal."""
    server = _server(vault)
    _present(monkeypatch, None)
    result = _call(server, "creek.link", {})
    assert isinstance(result, dict)
    assert result.get("reason") != _NOT_GRANTED_LITERAL


def test_handshake_advertises_only_the_admitted_tools(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handshake's capability list is the caller's own profile."""
    server = _server(vault)
    _present(monkeypatch, _token("acct_91", _gated_scopes(Grant.READ, Grant.DESK)))
    result = _call(server, "creek.handshake", {})
    assert isinstance(result, dict)
    assert set(result["capabilities"]) == _READ_TOOLS | _DESK_TOOLS


# --------------------------------------------------------------------------- #
# Group 6 -- static-token grant suffixes and the ungated announcement
# --------------------------------------------------------------------------- #


def _env(value: str) -> dict[str, str]:
    """Return an environment mapping configuring *value* as the registry.

    Args:
        value: The ``CREEK_MCP_CONSUMER_TOKENS`` value.

    Returns:
        The environment mapping.
    """
    return {CONSUMER_TOKENS_ENV: value}


def test_grant_suffix_is_stripped_from_the_token() -> None:
    """``openclaw=<token>@vault:read+vault:desk`` still authenticates the token."""
    env = _env(f"openclaw={_GATED_TOKEN}@vault:read+vault:desk")
    assert load_consumer_tokens(env) == {"openclaw": (_GATED_TOKEN,)}


def test_grant_suffix_configures_the_consumer_grants() -> None:
    """The suffix names the consumer's grants; no suffix means ungated."""
    env = _env(
        f"openclaw={_GATED_TOKEN}@vault:read+vault:desk;adepthood={_UNGATED_TOKEN}"
    )
    assert load_consumer_grants(env) == {
        "openclaw": frozenset({Grant.READ, Grant.DESK}),
    }


def test_grant_suffix_applies_to_a_rotating_token_set() -> None:
    """The suffix belongs to the entry, so every token in the window shares it."""
    env = _env(f"openclaw={_GATED_TOKEN} , {_ROTATING_TOKEN} @ vault:journal")
    assert load_consumer_tokens(env) == {"openclaw": (_GATED_TOKEN, _ROTATING_TOKEN)}
    assert load_consumer_grants(env) == {"openclaw": frozenset({Grant.JOURNAL})}


def test_grant_suffix_tolerates_whitespace_and_stray_separators() -> None:
    """Operator formatting around ``+`` is absorbed, never read as a grant."""
    env = _env(f"openclaw={_GATED_TOKEN}@ vault:read ++ vault:pipeline +")
    assert load_consumer_grants(env) == {
        "openclaw": frozenset({Grant.READ, Grant.PIPELINE}),
    }


def test_unknown_grant_is_refused_without_echoing_the_entry() -> None:
    """A typo'd grant fails startup naming the consumer and the valid grants."""
    env = _env(f"openclaw={_GATED_TOKEN}@vault:raed")
    with pytest.raises(ValueError, match="openclaw") as excinfo:
        load_consumer_tokens(env)
    message = str(excinfo.value)
    assert _GATED_TOKEN not in message
    assert "vault:raed" not in message
    for grant in Grant:
        assert grant.value in message


def test_empty_grant_suffix_is_refused() -> None:
    """``name=<token>@`` grants nothing, which is a typo, not a posture."""
    with pytest.raises(ValueError, match="openclaw") as excinfo:
        load_consumer_grants(_env(f"openclaw={_GATED_TOKEN}@ + "))
    assert _GATED_TOKEN not in str(excinfo.value)


def test_gated_verifier_mints_grant_scopes() -> None:
    """A gated consumer's token carries ``creek:remote`` plus its grants."""
    verifier = ConsumerTokenVerifier(
        {"openclaw": (_GATED_TOKEN,)},
        grants={"openclaw": frozenset({Grant.DESK, Grant.READ})},
    )
    access = asyncio.run(verifier.verify_token(_GATED_TOKEN))
    assert access is not None
    assert access.scopes == [REMOTE_SCOPE, "vault:read", "vault:desk"]


def test_ungated_verifier_mints_the_ungated_marker() -> None:
    """A consumer with no configured grants carries the explicit marker."""
    verifier = ConsumerTokenVerifier({"adepthood": (_UNGATED_TOKEN,)})
    access = asyncio.run(verifier.verify_token(_UNGATED_TOKEN))
    assert access is not None
    assert access.scopes == [REMOTE_SCOPE, UNGATED_SCOPE]
    assert granted_by_scopes(access.scopes) is UNGATED


def test_verifier_refuses_grants_for_an_unknown_consumer() -> None:
    """Grants for a consumer that holds no token are a configuration error."""
    with pytest.raises(ValueError, match="ghost"):
        ConsumerTokenVerifier(
            {"openclaw": (_GATED_TOKEN,)},
            grants={"ghost": frozenset({Grant.READ})},
        )


def test_verifier_refuses_an_empty_grant_set() -> None:
    """A constructed empty grant set is refused like an empty suffix."""
    with pytest.raises(ValueError, match="openclaw"):
        ConsumerTokenVerifier(
            {"openclaw": (_GATED_TOKEN,)}, grants={"openclaw": frozenset()}
        )


def test_ungated_notice_names_ungated_consumers_only() -> None:
    """The notice names who is ungated and how to gate them, never a token."""
    verifier = ConsumerTokenVerifier(
        {"openclaw": (_GATED_TOKEN,), "adepthood": (_UNGATED_TOKEN,)},
        grants={"openclaw": DEFAULT_GRANTS},
    )
    notice = verifier.ungated_notice()
    assert notice is not None
    assert "ungated" in notice
    assert "adepthood" in notice
    assert "openclaw" not in notice
    assert "@vault:read+vault:desk" in notice
    assert _UNGATED_TOKEN not in notice
    assert _GATED_TOKEN not in notice


def test_ungated_notice_is_silent_when_every_consumer_is_gated() -> None:
    """Nothing is announced once every consumer carries a grant suffix."""
    verifier = ConsumerTokenVerifier(
        {"openclaw": (_GATED_TOKEN,)}, grants={"openclaw": DEFAULT_GRANTS}
    )
    assert verifier.ungated_notice() is None


def test_announce_ungated_consumers_writes_stderr_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdout belongs to stdio JSON-RPC framing; the notice goes to stderr."""
    announce_ungated_consumers(ConsumerTokenVerifier({"adepthood": (_UNGATED_TOKEN,)}))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "adepthood" in captured.err
    assert "ungated" in captured.err


def test_announce_ungated_consumers_is_silent_when_all_gated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ungated consumer, no output at all."""
    announce_ungated_consumers(
        ConsumerTokenVerifier(
            {"openclaw": (_GATED_TOKEN,)}, grants={"openclaw": DEFAULT_GRANTS}
        )
    )
    assert capsys.readouterr() == ("", "")


def test_network_main_serves_suffixed_grants_and_announces_ungated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    vault: Path,
) -> None:
    """``main`` wires the suffix grants into the verifier it serves behind."""
    monkeypatch.setenv(
        CONSUMER_TOKENS_ENV,
        f"openclaw={_GATED_TOKEN}@vault:read+vault:desk;adepthood={_UNGATED_TOKEN}",
    )
    monkeypatch.delenv("CREEK_MCP_ELEVATED_TOKEN", raising=False)
    served: list[FastMCP] = []
    monkeypatch.setattr(
        server_mod, "_serve_network", lambda server, args: served.append(server)
    )
    monkeypatch.setattr(server_mod, "_resolve_vault", lambda path: vault)
    server_mod.main(["--transport", "network"])
    assert "adepthood" in capsys.readouterr().err
    verifier = served[0]._token_verifier
    assert verifier is not None
    access = asyncio.run(verifier.verify_token(_GATED_TOKEN))
    assert access is not None
    assert granted_by_scopes(access.scopes) == DEFAULT_GRANTS


def test_network_main_refuses_an_unknown_grant_at_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad suffix exits before any socket opens, never echoing the token."""
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"openclaw={_GATED_TOKEN}@vault:all")
    monkeypatch.delenv("CREEK_MCP_ELEVATED_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        server_mod.main(["--transport", "network"])
    err = capsys.readouterr().err
    assert "openclaw" in err
    assert _GATED_TOKEN not in err


# --------------------------------------------------------------------------- #
# Group 7 -- over the wire, through the real bearer middleware
# --------------------------------------------------------------------------- #


def _wire_server(vault: Path) -> FastMCP:
    """Build a network server whose one consumer holds the default profile.

    Args:
        vault: The seeded vault root.

    Returns:
        The built server with DNS-rebinding checks off for ``testserver``.
    """
    server = build_server(
        transport=Transport.NETWORK,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        token_verifier=ConsumerTokenVerifier(
            {"openclaw": (_GATED_TOKEN,)}, grants={"openclaw": DEFAULT_GRANTS}
        ),
    )
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    return server


def _rpc_result(body: str) -> dict[str, object]:
    """Extract the JSON-RPC ``result`` from a streamable-http SSE body.

    Args:
        body: The SSE response text.

    Returns:
        The last event's ``result`` member.
    """
    payloads = [
        line[len("data:") :].strip()
        for line in body.splitlines()
        if line.startswith("data:")
    ]
    result: dict[str, object] = json.loads(payloads[-1])["result"]
    return result


def _open_session(client: TestClient, path: str) -> dict[str, str]:
    """Run the MCP ``initialize`` handshake as the gated consumer.

    Args:
        client: The test client.
        path: The streamable-http path.

    Returns:
        Request headers carrying the bearer and the session id.
    """
    base = {
        "Authorization": f"Bearer {_GATED_TOKEN}",
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
    headers = {**base, "mcp-session-id": init.headers["mcp-session-id"]}
    client.post(
        path,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    return headers


def test_wire_tools_list_and_call_honour_the_grant_suffix(vault: Path) -> None:
    """The real middleware path lists 10 tools and refuses ``creek.link``."""
    server = _wire_server(vault)
    path = server.settings.streamable_http_path
    with TestClient(server.streamable_http_app()) as client:
        headers = _open_session(client, path)
        listed = client.post(
            path,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        called = client.post(
            path,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "creek.link", "arguments": {}},
            },
            headers=headers,
        )
    tools = _rpc_result(listed.text)["tools"]
    assert isinstance(tools, list)
    assert {tool["name"] for tool in tools} == _READ_TOOLS | _DESK_TOOLS
    assert _rpc_result(called.text)["structuredContent"] == _not_granted("creek.link")


def test_v1_verifier_carries_the_same_grants() -> None:
    """``/v1``'s registry loader keeps the suffix, so no surface reads it ungated."""
    verifier = build_verifier(
        _env(f"openclaw={_GATED_TOKEN}@vault:journal;adepthood={_UNGATED_TOKEN}")
    )
    gated = asyncio.run(verifier.verify_token(_GATED_TOKEN))
    ungated = asyncio.run(verifier.verify_token(_UNGATED_TOKEN))
    assert gated is not None
    assert ungated is not None
    assert granted_by_scopes(gated.scopes) == frozenset({Grant.JOURNAL})
    assert granted_by_scopes(ungated.scopes) is UNGATED
