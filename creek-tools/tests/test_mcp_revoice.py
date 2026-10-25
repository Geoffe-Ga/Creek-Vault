"""Revoice transforms only supplied text, with privacy-routed voice conditioning."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken

from creek.author.client import AuthorLLMClient
from creek.author.voice import split_revoice_prompt
from creek.care.guardrail import CARE_POLICY
from creek.classify.llm.completion import Completion
from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier
from creek.progress import CompositionStage
from creek_mcp import server as server_mod
from creek_mcp.audit import MCP_AUDIT_RELPATH, verify_mcp_audit_chain
from creek_mcp.policy import TOOL_GRANTS, Grant, Transport
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.server import build_server
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.revoice import (
    MAX_REVOICE_CHARS,
    MAX_REVOICE_OUTPUT_CHARS,
    revoice_tool,
)

INPUT = "Thanks for the invitation. I cannot make Thursday; could we try Friday?"
OUTPUT = "Thank you for inviting me. Thursday won't work for me. Could we try Friday?"


class _Provider:
    """A fully local, controllable completion seam that records prompts."""

    is_cloud = False
    model = "hermetic-voice"
    available = True

    def __init__(self, *, text: str = OUTPUT, stop_reason: str = "end_turn") -> None:
        self.text = text
        self.stop_reason = stop_reason
        self.prompts: list[tuple[str | None, str]] = []
        self.tiers: list[PrivacyTier | None] = []
        self.gate: threading.Event | None = None

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, system: str | None = None
    ) -> Completion:
        self.prompts.append((system, prompt))
        if self.gate is not None:
            assert self.gate.wait(10)
        return Completion(
            self.text, self.stop_reason, {"input_tokens": 42, "output_tokens": 17}
        )

    def factory(self, tier: PrivacyTier | None) -> AuthorLLMClient:
        self.tiers.append(tier)
        return AuthorLLMClient(self)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A bare vault with no corpus or voice skills unless a test supplies them."""
    (tmp_path / "00-Creek-Meta").mkdir()
    return tmp_path


def _skills(vault: Path) -> None:
    """Install the three native skill locations the voice stack resolves."""
    for relative, body in {
        "meta/voice-core.SKILL.md": "VOICE_CORE_SENTINEL: short sentences.",
        "phases/rising.SKILL.md": "PHASE_SENTINEL: rising energy.",
        "registers/conversational.SKILL.md": "REGISTER_SENTINEL: speak plainly.",
    }.items():
        path = vault / "creek-skills" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def test_prompt_caches_only_skills_and_preserves_meaning(vault: Path) -> None:
    """The changing text never enters the cacheable skill prefix."""
    _skills(vault)
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    static, dynamic = prompt.static, prompt.dynamic
    assert CARE_POLICY in static
    assert all(
        word in static
        for word in ("VOICE_CORE_SENTINEL", "PHASE_SENTINEL", "REGISTER_SENTINEL")
    )
    assert INPUT not in static
    assert INPUT in dynamic
    assert "preserve the meaning" in dynamic.lower()
    assert "do not add claims" in dynamic.lower()
    assert "biographical" in dynamic.lower()
    assert "untrusted" in dynamic.lower()


def test_happy_path_uses_skills_and_audits_only_character_counts(vault: Path) -> None:
    """The adapter reads no fragments and writes neither a draft nor plaintext audit."""
    _skills(vault)
    private = vault / "01-Fragments/Notes/private.md"
    private.parent.mkdir(parents=True)
    private.write_text(
        "---\nprivacy_tier: intimate\n---\nCORPUS_SENTINEL_NEVER_RETRIEVED"
    )
    provider = _Provider()
    stages: list[CompositionStage] = []
    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=provider.factory,
        register="conversational",
        phase="rising",
        consumer="agent-one",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        on_stage=stages.append,
    )
    assert response["status"] == "ok"
    assert response["text"] == OUTPUT
    assert response["voice_skills"] == [
        "voice-core",
        "phase-rising",
        "register-conversational",
    ]
    assert response["usage"] == {"input_tokens": 42, "output_tokens": 17}
    assert provider.tiers == [PrivacyTier.PERSONAL]
    assert "CORPUS_SENTINEL_NEVER_RETRIEVED" not in repr(provider.prompts)
    assert "derived_tier" not in response
    assert stages == [CompositionStage.SKILLS, CompositionStage.COMPOSED]
    entries = [
        json.loads(line)
        for line in (vault / MCP_AUDIT_RELPATH).read_text().splitlines()
    ]
    assert len(entries) == 1
    assert entries[0]["consumer"] == "agent-one"
    assert entries[0]["args_summary"] == {
        "input_chars": len(INPUT),
        "output_chars": len(OUTPUT),
    }
    assert INPUT not in json.dumps(entries)
    assert OUTPUT not in json.dumps(entries)
    assert not (vault / "07-Voice").exists()
    verify_mcp_audit_chain(vault)


def test_missing_skills_still_conditions_with_care(vault: Path) -> None:
    """Missing voice skills do not pretend the owner's voice was learned."""
    provider = _Provider()
    response = revoice_tool(vault_path=vault, text=INPUT, llm_factory=provider.factory)
    assert response["status"] == "ok"
    assert response["voice_skills"] == []
    assert provider.prompts[0][0] == CARE_POLICY


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": ""},
        {"text": "  \n"},
        {"text": "x" * (MAX_REVOICE_CHARS + 1)},
        {"register": "../other"},
        {"phase": "not-a-phase"},
    ],
)
def test_invalid_input_never_builds_a_model(
    vault: Path, arguments: dict[str, str]
) -> None:
    """Length and canonical slug validation happen before any provider sees input."""
    provider = _Provider()
    response = revoice_tool(
        vault_path=vault, llm_factory=provider.factory, **{"text": INPUT, **arguments}
    )
    assert response["status"] == "error"
    assert provider.tiers == []
    assert provider.prompts == []
    assert "text" not in response
    assert len((vault / MCP_AUDIT_RELPATH).read_text().splitlines()) == 1


def test_intimate_routing_refusal_is_content_free(vault: Path) -> None:
    """A local-routing refusal cannot echo a provider exception or either text."""

    def refuse(tier: PrivacyTier | None) -> AuthorLLMClient:
        assert tier is PrivacyTier.INTIMATE
        raise IntimateRoutingError(stage_provider="anthropic")

    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=refuse,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert response["status"] == "refused"
    assert response["tool"] == "creek.revoice"
    assert response["tier_ceiling"] == "intimate"
    assert INPUT not in json.dumps(response)


@pytest.mark.parametrize("client_available", [True, False])
def test_provider_failures_are_static_errors(
    vault: Path, client_available: bool
) -> None:
    """Neither an unavailable model nor an exception fabricates a successful rewrite."""

    def failed(tier: PrivacyTier | None) -> AuthorLLMClient | None:
        if client_available:
            raise RuntimeError(INPUT)
        return None

    response = revoice_tool(vault_path=vault, text=INPUT, llm_factory=failed)
    assert response["status"] == "error"
    assert INPUT not in json.dumps(response)


@pytest.mark.parametrize(
    "text, stop",
    [
        ("", "end_turn"),
        ("partial", "max_tokens"),
        ("x" * (MAX_REVOICE_OUTPUT_CHARS + 1), "end_turn"),
    ],
    ids=["empty", "truncated", "oversized"],
)
def test_incomplete_completion_is_not_an_ok_rewrite(
    vault: Path, text: str, stop: str
) -> None:
    """A truncated output cannot claim the meaning-preserving success contract."""
    provider = _Provider(text=text, stop_reason=stop)
    assert (
        revoice_tool(vault_path=vault, text=INPUT, llm_factory=provider.factory)[
            "status"
        ]
        == "error"
    )


def _structured(result: object) -> dict[str, Any]:
    """Unpack the SDK's registered-tool result."""
    payload = result[1] if isinstance(result, tuple) else result
    assert isinstance(payload, dict)
    return payload


def test_registered_tool_round_trip_and_desk_grant(vault: Path) -> None:
    """FastMCP advertises and dispatches the production revoice adapter."""
    provider = _Provider()
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )
    tools = asyncio.run(server.list_tools())
    assert "creek.revoice" in {tool.name for tool in tools}
    assert TOOL_GRANTS["creek.revoice"] is Grant.DESK
    response = _structured(
        asyncio.run(server.call_tool("creek.revoice", {"text": INPUT}))
    )
    assert response["text"] == OUTPUT
    assert provider.prompts


def test_detached_rewrite_is_readable_but_never_persisted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner obtains a detached body without any artifact containing it."""
    monkeypatch.setenv("CREEK_MCP_SYNC_DEADLINE_SECONDS", "0.01")
    provider = _Provider()
    provider.gate = threading.Event()
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )

    async def run() -> dict[str, Any]:
        accepted = _structured(await server.call_tool("creek.revoice", {"text": INPUT}))
        assert accepted["status"] == "accepted"
        provider.gate.set()
        for _ in range(200):
            result = _structured(
                await server.call_tool(
                    "creek.jobs.read", {"job_id": accepted["job_id"]}
                )
            )
            if result.get("state") == "succeeded":
                return result
            await asyncio.sleep(0.01)
        pytest.fail("detached rewrite never finished")

    result = asyncio.run(run())
    assert result["result"]["text"] == OUTPUT
    assert not (vault / "07-Voice").exists()
    for path in vault.rglob("*"):
        if path.is_file():
            stored = path.read_text()
            assert INPUT not in stored
            assert OUTPUT not in stored


def test_detached_read_is_owner_and_ceiling_bound_and_lost_on_restart(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handle alone cannot read text; restart cannot recover it from disk."""
    monkeypatch.setenv("CREEK_MCP_SYNC_DEADLINE_SECONDS", "0.01")
    active = [
        AccessToken(
            token="test",
            client_id="agent-one",
            scopes=["creek:remote", "vault:desk", "vault:read"],
        )
    ]
    monkeypatch.setattr(server_mod, "get_access_token", lambda: active[0])
    provider = _Provider()
    provider.gate = threading.Event()
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )

    async def run() -> str:
        accepted = _structured(
            await server.call_tool(
                "creek.revoice", {"text": INPUT, "privacy_tier_ceiling": "personal"}
            )
        )
        assert accepted["status"] == "accepted"
        job_id = accepted["job_id"]
        provider.gate.set()
        for _ in range(200):
            result = _structured(
                await server.call_tool(
                    "creek.jobs.read",
                    {"job_id": job_id, "privacy_tier_ceiling": "personal"},
                )
            )
            if result.get("state") == "succeeded":
                assert result["result"]["text"] == OUTPUT
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("detached result never became readable")
        low = _structured(await server.call_tool("creek.jobs.read", {"job_id": job_id}))
        assert low["status"] == "refused"
        active[0] = AccessToken(
            token="test",
            client_id="agent-two",
            scopes=["creek:remote", "vault:desk", "vault:read"],
        )
        other = _structured(
            await server.call_tool(
                "creek.jobs.read",
                {"job_id": job_id, "privacy_tier_ceiling": "personal"},
            )
        )
        assert other["status"] == "refused"
        active[0] = AccessToken(
            token="test",
            client_id="agent-one",
            scopes=["creek:remote", "vault:desk", "vault:read"],
        )
        return str(job_id)

    job_id = asyncio.run(run())
    restarted = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )
    missing = _structured(
        asyncio.run(
            restarted.call_tool(
                "creek.jobs.read",
                {"job_id": job_id, "privacy_tier_ceiling": "personal"},
            )
        )
    )
    assert missing["status"] == "refused"
    assert OUTPUT not in json.dumps(missing)


def test_no_core_ignores_orphan_phase_and_register_skills(vault: Path) -> None:
    """Auxiliary skills alone never claim that the owner's voice is conditioned."""
    _skills(vault)
    (vault / "creek-skills/meta/voice-core.SKILL.md").unlink()
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    assert prompt.static == CARE_POLICY
    assert prompt.voice_skills == ()


def test_declared_all_routes_intimate_and_provenance_is_preserved(vault: Path) -> None:
    """Broad caller sensitivity cannot win cloud routing through unclassified text."""
    provider = _Provider()
    provenance = InferenceProvenance()
    provenance.record("local")
    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=provider.factory,
        privacy_tier_ceiling=TierCeiling.ALL,
        provenance=provenance,
    )
    assert provider.tiers == [PrivacyTier.INTIMATE]
    assert response["inference_source"] == "local"


def test_default_factory_absence_is_an_honest_error(vault: Path) -> None:
    """No model means no rewrite, rather than an unchanged-text success."""
    response = revoice_tool(vault_path=vault, text=INPUT)
    assert response["status"] == "error"
    assert "text" not in response


def test_read_grant_cannot_spend_model_budget(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only OAuth caller is refused before constructing the voice client."""
    provider = _Provider()
    token = AccessToken(
        token="test", client_id="reader", scopes=["creek:remote", "vault:read"]
    )
    monkeypatch.setattr(server_mod, "get_access_token", lambda: token)
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )
    response = _structured(
        asyncio.run(server.call_tool("creek.revoice", {"text": INPUT}))
    )
    assert response["status"] == "refused"
    assert provider.tiers == []
    assert provider.prompts == []
