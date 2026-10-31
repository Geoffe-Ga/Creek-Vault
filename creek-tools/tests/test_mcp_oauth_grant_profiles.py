"""``vault:journal`` and ``vault:pipeline`` are grantable end to end (#2006, R3-5).

The owner's decision R3-5 keeps both scopes advertised and makes them
grantable rather than removing them. "Grantable" is proved here, over the
served ``/mcp`` OAuth path on a managed-vault-shaped runtime (the
:data:`~creek.classify.llm.consent.MANAGED_VAULT_ENV` marker set, cloud consent
latched off, the JWT subject pinned): an agent a person granted exactly one of
the two scopes sees every tool in that profile and reaches it, and an agent
without the grant is refused before dispatch with
:data:`~creek_mcp.policy.TOOL_NOT_GRANTED_REASON`.

One tool cannot serve a remote agent: ``creek.ingest`` reads a caller-named
*vault* path, so over the network it is an existence oracle over every
slugified fragment filename (``input path not found: <path>``) and re-ingests
content the agent's ceiling never admitted. It stays in the profile (moving it
would change the grant partition, which is the owner's call) and is refused at
dispatch with :data:`~creek_mcp.policy.TOOL_NOT_SERVED_REMOTELY_REASON`, a
constant that names no path and no tier. Its documented posture lives in
:data:`creek_mcp.read_gate.TOOL_POSTURES` and ``docs/mcp.md``.

Every test is hermetic: tokens are minted against a local JWKS fixture and no
call leaves the process.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.testclient import TestClient

from creek.classify.llm.consent import MANAGED_VAULT_ENV, managed_local_only
from creek_mcp.audit import AUDIT_OUTCOME_REFUSED, MCP_AUDIT_RELPATH
from creek_mcp.policy import (
    REMOTE_CEILING_REFUSAL_REASON,
    REMOTE_UNSERVED_TOOLS,
    TOOL_GRANTS,
    TOOL_NOT_GRANTED_REASON,
    TOOL_NOT_SERVED_REMOTELY_REASON,
    UNGATED,
    Admission,
    CallerIdentity,
    Grant,
    Refusal,
    Transport,
    served_remotely,
)
from creek_mcp.read_gate import TOOL_POSTURES
from creek_mcp.server import build_server
from tests.agent_token_support import (
    AGENT_CLIENT_ID,
    AGENT_SUBJECT,
    DEFAULT_SCOPE,
    agent_verifier,
    mint,
)
from tests.helpers import write_raw_fragment_file
from tests.mcp_http_support import call_tool, listed_tools, open_session
from tests.v1_api_support import STRONG_TOKEN, build_app, seed_vault, snapshot

if TYPE_CHECKING:
    from collections.abc import Iterator


_OPT_IN_GRANTS: Final[frozenset[Grant]] = frozenset({Grant.JOURNAL, Grant.PIPELINE})
"""The two grants R3-5 keeps advertised and makes grantable."""

_PROFILE: Final[tuple[tuple[str, Grant], ...]] = tuple(
    sorted(
        (tool, grant) for tool, grant in TOOL_GRANTS.items() if grant in _OPT_IN_GRANTS
    )
)
"""Every ``vault:journal`` / ``vault:pipeline`` tool, derived from the authority."""

_EXPECTED_PROFILE: Final[dict[str, Grant]] = {
    "creek.journal": Grant.JOURNAL,
    "creek.upload": Grant.JOURNAL,
    "creek.classify": Grant.PIPELINE,
    "creek.link": Grant.PIPELINE,
    "creek.compile": Grant.PIPELINE,
    "creek.report": Grant.PIPELINE,
    "creek.skills.refresh": Grant.PIPELINE,
    "creek.ingest": Grant.PIPELINE,
    "creek.redact.scan": Grant.PIPELINE,
    "creek.state.render": Grant.PIPELINE,
}
"""The issue's ten tools, spelled out so a silent repartition fails here."""

_CEILING: Final[str] = "personal"
"""The highest ceiling a remote agent may ask for, used on every call."""

_SECRET: Final[str] = "intimate-body-marker-2006"
"""Text only the seeded intimate fragment carries."""

_INTIMATE_ID: Final[str] = "frag-intimate00001"
"""The seeded intimate fragment's id."""

_INTIMATE_RELPATH: Final[str] = f"01-Fragments/Notes/{_INTIMATE_ID}.md"
"""Where the seeded intimate fragment lives, vault-relative."""

_UPLOAD_BYTES: Final[bytes] = b"# A note\n\nSomething the writer chose to upload.\n"

_MINIMAL_ARGS: Final[dict[str, dict[str, Any]]] = {
    "creek.journal": {
        "content": "A morning page the agent filed.",
        "external_id": "agent-entry-1",
        "tier": _CEILING,
    },
    "creek.upload": {
        "filename": "note.md",
        "content_base64": base64.b64encode(_UPLOAD_BYTES).decode("ascii"),
        "external_id": "agent-upload-1",
        "tier": _CEILING,
    },
    "creek.classify": {"method": "rules"},
    "creek.link": {"method": "temporal"},
    "creek.compile": {
        "fragment_ids": [_INTIMATE_ID],
        "target_kind": "thread",
        "target_id": "agent-thread",
        "target_title": "Agent thread",
    },
    "creek.report": {"report_type": "tags"},
    "creek.skills.refresh": {},
    "creek.ingest": {"source_type": "markdown", "input_path": _INTIMATE_RELPATH},
    "creek.redact.scan": {"input_path": "00-Creek-Meta/Inbound"},
    "creek.state.render": {},
}
"""The smallest call each tool accepts; every one asks for ``personal``."""

_POLICY_REASONS: Final[frozenset[str]] = frozenset(
    {
        TOOL_NOT_GRANTED_REASON,
        TOOL_NOT_SERVED_REMOTELY_REASON,
        REMOTE_CEILING_REFUSAL_REASON,
    }
)
"""Every pre-dispatch refusal; a served tool's answer is none of these."""


# --------------------------------------------------------------------------- #
# The managed-vault-shaped runtime
# --------------------------------------------------------------------------- #


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a seeded vault holding one intimate fragment and an Inbound dir.

    Args:
        tmp_path: Pytest's per-test directory.

    Returns:
        The vault root.
    """
    root = seed_vault(tmp_path)
    (root / "00-Creek-Meta" / "Inbound").mkdir(parents=True)
    write_raw_fragment_file(
        root,
        "01-Fragments/Notes",
        _INTIMATE_ID,
        "Held close",
        body=_SECRET,
        privacy_tier="intimate",
    )
    return root


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Mark the runtime as an Adepthood-managed vault, cloud consent latched off.

    Args:
        monkeypatch: Pytest's monkeypatch.

    Yields:
        Control, inside :func:`managed_local_only`.
    """
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    with managed_local_only():
        yield


def _client(vault: Path) -> TestClient:
    """Return a client over the served app, pinned to the agent's subject.

    Args:
        vault: The vault to serve.

    Returns:
        A test client for the ``/mcp`` OAuth mount.
    """
    verifier = agent_verifier(expected_subject=AGENT_SUBJECT)
    return TestClient(build_app(vault_path=vault, agent_verifier=verifier))


def _args(tool: str) -> dict[str, Any]:
    """Return *tool*'s minimal arguments at the ``personal`` ceiling.

    Args:
        tool: The tool name.

    Returns:
        A fresh argument dict.
    """
    return {**_MINIMAL_ARGS[tool], "privacy_tier_ceiling": _CEILING}


def _audit(vault: Path) -> list[dict[str, Any]]:
    """Return every MCP audit entry written to *vault*.

    Args:
        vault: The served vault.

    Returns:
        The parsed entries, in order.
    """
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _dispatched(vault: Path, tool: str) -> list[dict[str, Any]]:
    """Return the audit entries *tool* itself wrote, i.e. after dispatch.

    Args:
        vault: The served vault.
        tool: The tool name.

    Returns:
        Entries for *tool* that are not the adapter's pre-dispatch refusal.
    """
    return [
        entry
        for entry in _audit(vault)
        if entry["tool"] == tool and entry.get("outcome") != AUDIT_OUTCOME_REFUSED
    ]


def _refused(vault: Path, tool: str) -> list[dict[str, Any]]:
    """Return the adapter's pre-dispatch refusal entries for *tool*.

    Args:
        vault: The served vault.
        tool: The tool name.

    Returns:
        The refused entries.
    """
    return [
        entry
        for entry in _audit(vault)
        if entry["tool"] == tool and entry.get("outcome") == AUDIT_OUTCOME_REFUSED
    ]


def _without(grant: Grant) -> str:
    """Return a scope holding every grant except *grant*.

    Args:
        grant: The grant to leave out.

    Returns:
        A space-separated scope string.
    """
    return " ".join(sorted(held.value for held in Grant if held is not grant))


# --------------------------------------------------------------------------- #
# The profile under test
# --------------------------------------------------------------------------- #


def test_the_opt_in_profiles_are_the_issues_ten_tools() -> None:
    """``vault:journal`` and ``vault:pipeline`` partition exactly these ten."""
    assert dict(_PROFILE) == _EXPECTED_PROFILE


def test_every_profile_tool_has_minimal_arguments() -> None:
    """The matrix drives every tool; a new one needs arguments here first."""
    assert set(_MINIMAL_ARGS) == set(_EXPECTED_PROFILE)


# --------------------------------------------------------------------------- #
# The matrix: granted -> listed and reached; not granted -> refused
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(("tool", "grant"), _PROFILE)
def test_a_granted_agent_sees_every_tool_in_its_profile(
    vault: Path, tool: str, grant: Grant
) -> None:
    """A token carrying exactly the grant lists the tool.

    Args:
        vault: A seeded vault.
        tool: A profile tool.
        grant: Its grant.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=grant.value))
        tools = listed_tools(client, session["headers"])

    assert tool in tools
    assert {name for name in tools if TOOL_GRANTS[name] is not grant} == set()


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(
    ("tool", "grant"),
    [pair for pair in _PROFILE if pair[0] not in REMOTE_UNSERVED_TOOLS],
)
def test_a_granted_agent_reaches_every_served_tool(
    vault: Path, tool: str, grant: Grant
) -> None:
    """Dispatched under the agent's identity, never refused by policy.

    The tool's own answer may still be a tool-domain refusal — ``creek.compile``
    is handed the intimate fragment's id, which its own source-tier gate
    refuses — but it is the tool that ran and said so: the tool's own audit
    entry is attributed to the agent, and the intimate body never comes back.

    Args:
        vault: A seeded vault.
        tool: A profile tool served to remote agents.
        grant: Its grant.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=grant.value))
        answer = call_tool(client, session["headers"], tool, _args(tool))

    assert answer.get("reason") not in _POLICY_REASONS
    assert answer["tool"] == tool
    assert _refused(vault, tool) == []
    assert [entry["consumer"] for entry in _dispatched(vault, tool)][:1] == [
        AGENT_CLIENT_ID
    ]
    assert _SECRET not in json.dumps(answer)


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(
    "tool",
    ["creek.journal", "creek.upload", "creek.classify", "creek.link"],
)
def test_the_write_tools_do_their_job_for_a_granted_agent(
    vault: Path, tool: str
) -> None:
    """The journal and corpus-pass tools answer ``ok``, not just "dispatched".

    Args:
        vault: A seeded vault.
        tool: A profile tool whose minimal call succeeds outright.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=TOOL_GRANTS[tool].value))
        answer = call_tool(client, session["headers"], tool, _args(tool))

    assert answer["status"] == "ok", answer


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(("tool", "grant"), _PROFILE)
def test_an_agent_without_the_grant_is_refused_before_dispatch(
    vault: Path, tool: str, grant: Grant
) -> None:
    """Every other grant held, this one not: unlisted and refused, nothing run.

    Args:
        vault: A seeded vault.
        tool: A profile tool.
        grant: The grant the token is denied.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=_without(grant)))
        tools = listed_tools(client, session["headers"])
        answer = call_tool(client, session["headers"], tool, _args(tool))

    assert tool not in tools
    assert answer["status"] == "refused"
    assert answer["reason"] == TOOL_NOT_GRANTED_REASON
    assert _dispatched(vault, tool) == []
    assert [entry["consumer"] for entry in _refused(vault, tool)] == [AGENT_CLIENT_ID]


@pytest.mark.usefixtures("managed")
def test_the_default_profile_reaches_neither_opt_in_profile(vault: Path) -> None:
    """``vault:read vault:desk`` — the consent default — lists none of the ten.

    Args:
        vault: A seeded vault.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=DEFAULT_SCOPE))
        tools = listed_tools(client, session["headers"])

    assert tools.isdisjoint(_EXPECTED_PROFILE)


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(("tool", "grant"), _PROFILE)
def test_a_granted_agent_still_cannot_ask_for_intimate(
    vault: Path, tool: str, grant: Grant
) -> None:
    """The grant opens the tool, never the ceiling: ``intimate`` stays refused.

    Args:
        vault: A seeded vault.
        tool: A profile tool.
        grant: Its grant.
    """
    with _client(vault) as client:
        session = open_session(client, mint(scope=grant.value))
        answer = call_tool(
            client,
            session["headers"],
            tool,
            {**_args(tool), "privacy_tier_ceiling": "intimate"},
        )

    assert answer["status"] == "refused"
    assert answer["reason"] in {
        REMOTE_CEILING_REFUSAL_REASON,
        TOOL_NOT_SERVED_REMOTELY_REASON,
    }
    assert _dispatched(vault, tool) == []


# --------------------------------------------------------------------------- #
# creek.ingest: in the profile, refused to a remote agent
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("managed")
@pytest.mark.parametrize(
    "input_path",
    [
        pytest.param(_INTIMATE_RELPATH, id="an-intimate-fragment"),
        pytest.param("01-Fragments/Notes/nothing-here.md", id="a-missing-path"),
        pytest.param("00-Creek-Meta/Inbound", id="the-staging-subtree"),
        pytest.param("/etc/passwd", id="outside-the-vault"),
    ],
)
def test_ingest_is_refused_to_a_granted_agent_without_an_oracle(
    vault: Path, input_path: str
) -> None:
    """One constant reason whatever the path; nothing read, nothing written.

    Args:
        vault: A seeded vault.
        input_path: A path the agent names.
    """
    before = snapshot(vault / "01-Fragments")
    with _client(vault) as client:
        session = open_session(client, mint(scope=Grant.PIPELINE.value))
        answer = call_tool(
            client,
            session["headers"],
            "creek.ingest",
            {
                "source_type": "markdown",
                "input_path": input_path,
                "privacy_tier_ceiling": _CEILING,
            },
        )

    assert answer == {
        "status": "refused",
        "tool": "creek.ingest",
        "tier_ceiling": "open",
        "reason": TOOL_NOT_SERVED_REMOTELY_REASON,
    }
    assert snapshot(vault / "01-Fragments") == before
    assert _dispatched(vault, "creek.ingest") == []
    assert [entry["consumer"] for entry in _refused(vault, "creek.ingest")] == [
        AGENT_CLIENT_ID
    ]


def test_the_not_served_reason_is_a_constant_naming_nothing() -> None:
    """No template markers, no path, no tier, and distinct from the grant reason."""
    reason = TOOL_NOT_SERVED_REMOTELY_REASON

    assert reason == "this tool is not served to a remote agent"
    assert not {"{", "}", "%", "/"} & set(reason)
    assert reason != TOOL_NOT_GRANTED_REASON


def test_only_ingest_is_withheld_from_remote_agents() -> None:
    """The withheld set is ``creek.ingest`` alone, and stays inside a profile."""
    assert frozenset({"creek.ingest"}) == REMOTE_UNSERVED_TOOLS
    assert set(TOOL_GRANTS) >= REMOTE_UNSERVED_TOOLS


def test_the_posture_manifest_records_the_remote_refusal() -> None:
    """``TOOL_POSTURES`` carries a ``served_remotely`` column that agrees."""
    withheld = {
        name for name, posture in TOOL_POSTURES.items() if not posture.served_remotely
    }

    assert withheld == REMOTE_UNSERVED_TOOLS
    assert "remote agent" in TOOL_POSTURES["creek.ingest"].rationale


@pytest.mark.parametrize(
    ("identity", "served"),
    [
        pytest.param(CallerIdentity(consumer=None, is_remote=False), True, id="local"),
        pytest.param(
            CallerIdentity(consumer="adepthood", is_remote=True, grants=UNGATED),
            True,
            id="ungated-static",
        ),
        pytest.param(
            CallerIdentity(
                consumer=AGENT_CLIENT_ID,
                is_remote=True,
                grants=frozenset({Grant.PIPELINE}),
            ),
            False,
            id="gated-agent",
        ),
    ],
)
def test_served_remotely_withholds_ingest_from_gated_remote_callers_only(
    identity: CallerIdentity, served: bool
) -> None:
    """Local and ungated callers keep ``creek.ingest``; a gated agent does not.

    Args:
        identity: The caller.
        served: Whether ``creek.ingest`` is served to it.
    """
    verdict = served_remotely(identity, "creek.ingest")

    if served:
        assert verdict == Admission(ceiling=None)
    else:
        assert verdict == Refusal(reason=TOOL_NOT_SERVED_REMOTELY_REASON)


def test_served_remotely_admits_every_other_tool_for_a_gated_agent() -> None:
    """Withholding is a list of one, never a default-deny."""
    agent = CallerIdentity(
        consumer=AGENT_CLIENT_ID, is_remote=True, grants=frozenset(Grant)
    )

    verdicts = {
        tool: served_remotely(agent, tool)
        for tool in TOOL_GRANTS
        if tool not in REMOTE_UNSERVED_TOOLS
    }

    assert set(verdicts.values()) == {Admission(ceiling=None)}


def test_a_local_caller_still_runs_ingest(vault: Path) -> None:
    """Stdio is unchanged: the operator's own ingest still runs.

    Args:
        vault: A seeded vault.
    """
    staged = vault / "00-Creek-Meta" / "Inbound" / "note.md"
    staged.write_bytes(_UPLOAD_BYTES)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    result = _structured(
        server.call_tool(
            "creek.ingest",
            {
                "source_type": "markdown",
                "input_path": "00-Creek-Meta/Inbound/note.md",
                "privacy_tier_ceiling": _CEILING,
            },
        )
    )

    assert result["status"] == "ok"
    assert result["written"] == 1


def test_an_ungated_static_consumer_still_reaches_ingest(vault: Path) -> None:
    """The adepthood service bearer keeps the reach it had before grants.

    Args:
        vault: A seeded vault.
    """
    staged = vault / "00-Creek-Meta" / "Inbound" / "note.md"
    staged.write_bytes(_UPLOAD_BYTES)
    with _client(vault) as client:
        session = open_session(client, STRONG_TOKEN)
        answer = call_tool(
            client,
            session["headers"],
            "creek.ingest",
            {
                "source_type": "markdown",
                "input_path": "00-Creek-Meta/Inbound/note.md",
                "privacy_tier_ceiling": _CEILING,
            },
        )

    assert answer["status"] == "ok"


def _structured(coroutine: Any) -> dict[str, Any]:
    """Run a ``FastMCP.call_tool`` coroutine and return its structured result.

    Args:
        coroutine: The pending call.

    Returns:
        The structured payload.
    """
    result = asyncio.run(coroutine)
    structured = result[1] if isinstance(result, tuple) else result
    assert isinstance(structured, dict)
    return structured


# --------------------------------------------------------------------------- #
# The opt-in recipe is published
# --------------------------------------------------------------------------- #

_MCP_DOC: Final[Path] = Path(__file__).resolve().parents[1] / "docs" / "mcp.md"


def test_the_docs_publish_the_opt_in_recipe_and_the_behaviour_table() -> None:
    """``docs/mcp.md`` names both scopes in a recipe, R3-5, and all ten tools."""
    text = _MCP_DOC.read_text(encoding="utf-8")

    assert "R3-5" in text
    assert "vault:read vault:desk vault:journal vault:pipeline" in text
    assert TOOL_NOT_SERVED_REMOTELY_REASON in text
    for tool in _EXPECTED_PROFILE:
        assert f"`{tool}`" in text
