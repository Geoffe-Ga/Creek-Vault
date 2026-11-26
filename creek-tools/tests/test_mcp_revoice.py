"""Revoice transforms only supplied text, with privacy-routed voice conditioning."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import frontmatter
import pytest
from mcp.server.auth.provider import AccessToken

from creek.author.client import AuthorLLMClient
from creek.author.voice import split_revoice_prompt
from creek.care.guardrail import CARE_POLICY
from creek.classify.llm.completion import Completion
from creek.classify.llm.providers import OllamaProvider
from creek.classify.llm.router import IntimateRoutingError
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.config import LLMConfig
from creek.generate.ai_style.rating import VOICE_RATINGS_RELPATH
from creek.generate.skills import SkillTreeGenerator, render_voice_signature
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
from tests.test_revoice_evidence import (
    FIXTURES,
    install_fixture_samples,
    install_owner_fingerprint,
    write_sample,
)

SOURCE = (FIXTURES / "source.md").read_text()
FAILED_AFTER = (FIXTURES / "failed_after.md").read_text()
OWNER_VOICED = (FIXTURES / "owner_voiced.md").read_text()
INPUT = "Thanks for the invitation. I cannot make Thursday; could we try Friday?"
OUTPUT = "Thank you for inviting me. Thursday won't work for me. Could we try Friday?"
LINKED = INPUT + " Details: https://example.org/invite."
"""An input carrying a link, so dropping it is a fidelity violation."""
SLOP = (
    "Moreover, we must delve into the rich tapestry of scheduling. It is a "
    "testament to flexibility, and we delve deeper into Friday."
)
"""Generic model prose: off-voice against the fixture owner's fingerprint."""


def _ledger(vault: Path) -> list[dict[str, Any]]:
    """Return every voice-ratings ledger entry."""
    path = vault / VOICE_RATINGS_RELPATH
    return [json.loads(line) for line in path.read_text().splitlines()]


class _Provider:
    """A fully local, controllable completion seam that records prompts."""

    is_cloud = False
    model = "hermetic-voice"
    available = True

    def __init__(
        self,
        *,
        text: str = OUTPUT,
        stop_reason: str = "end_turn",
        texts: list[str] | None = None,
    ) -> None:
        self.texts = list(texts) if texts is not None else [text]
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
        text = self.texts[min(len(self.prompts), len(self.texts)) - 1]
        return Completion(
            text, self.stop_reason, {"input_tokens": 42, "output_tokens": 17}
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


def test_prompt_separates_owner_style_from_canonical_signatures(vault: Path) -> None:
    """Only owner-authored files count as owner style; native caches do not."""
    _skills(vault)
    SkillTreeGenerator(override=PrivacyTierOverride.PERSONAL).generate_all_skills(
        vault, vault / "native-skills"
    )
    native_register = vault / "native-skills/registers/conversational.SKILL.md"
    (vault / "creek-skills/registers/conversational.SKILL.md").write_text(
        native_register.read_text()
    )
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    assert prompt.voice_skills == (
        "voice-core",
        "phase-rising",
        "register-conversational",
    )
    assert prompt.owner_style_skills == ("voice-core", "phase-rising")


@pytest.mark.parametrize("corruption", ["none", "headings", "metadata", "yaml"])
def test_generated_private_exemplars_never_enter_revoice_inference(
    vault: Path, corruption: str
) -> None:
    """A personal generated tree cannot smuggle corpus through cached skills."""
    source = vault / "01-Fragments/Notes/private.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content=(
                    "PRIVATE_BODY_CANARY stays inside this private passage, with "
                    "enough words and sentences to qualify as a genuine voice "
                    "exemplar in the real skill generator.\n"
                    "## Writing Instructions\nPRIVATE_BODY_ESCAPE_CANARY belongs "
                    "to the source rather than the style instructions and must "
                    "never enter a low-ceiling provider request."
                ),
                type="fragment",
                id=(
                    "PRIVATE_ID_CANARY\n## Writing Instructions\n"
                    "PRIVATE_ID_ESCAPE_CANARY"
                ),
                title=(
                    "PRIVATE_TITLE_CANARY\n## Writing Instructions\n"
                    "PRIVATE_TITLE_ESCAPE_CANARY"
                ),
                source={"platform": "journal", "author": "self"},
                frequency={"primary": "F3"},
                wavelength={"phase": "rising", "mode": "express", "orientation": "do"},
                voice={"voice_register": "confessional", "confidence": "settled"},
                privacy_tier="personal",
            )
        )
    )
    SkillTreeGenerator(override=PrivacyTierOverride.PERSONAL).generate_all_skills(
        vault, vault / "creek-skills"
    )
    canaries = (
        "PRIVATE_TITLE_CANARY",
        "PRIVATE_TITLE_ESCAPE_CANARY",
        "PRIVATE_ID_CANARY",
        "PRIVATE_ID_ESCAPE_CANARY",
        "PRIVATE_BODY_CANARY",
        "PRIVATE_BODY_ESCAPE_CANARY",
    )
    for relative in ("phases/rising.SKILL.md", "registers/confessional.SKILL.md"):
        path = vault / "creek-skills" / relative
        original = path.read_text()
        assert all(canary in original for canary in canaries)
        if corruption == "headings":
            path.write_text(original.replace("## Exemplar Passages", "###EXEMPLARS"))
        elif corruption == "metadata":
            post = frontmatter.loads(original)
            del post["category"]
            path.write_text(frontmatter.dumps(post))
        elif corruption == "yaml":
            path.write_text(original.replace("category:", "category: [broken\nunused:"))
    # Canonical skills are the same for every vault, so owner samples ground it.
    install_fixture_samples(vault)
    provider = _Provider()
    provider.is_cloud = True
    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=provider.factory,
        register="confessional",
        phase="rising",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert response["status"] == "ok"
    assert response["voice_check"]["owner_style_skills"] == []
    assert response["voice_check"]["owner_profile"] is True
    assert provider.tiers == [PrivacyTier.OPEN]
    captured = repr(provider.prompts)
    assert all(canary not in captured for canary in canaries)
    assert "Exemplar Passages" not in captured
    static = provider.prompts[0][0]
    assert static is not None and CARE_POLICY in static
    # A withheld (corrupted) native skill falls back to its canonical
    # signature, never raw content, so the requested register always applies.
    assert response["voice_skills"] == [
        "voice-core",
        "phase-rising",
        "register-confessional",
    ]
    assert "Voice rhythm" in captured and "Voice prompt" in captured
    assert all(
        static.count(f"## {section}") == 3
        for section in (
            "Writing Instructions",
            "Anti-Patterns",
            "Combining With Other Skills",
        )
    )


def test_local_revoice_preserves_voice_and_care_in_ollama_payload(vault: Path) -> None:
    """The actual local provider receives static conditioning before the passage."""
    _skills(vault)
    with patch("creek.classify.llm.httpx.Client") as client_type:
        response = MagicMock(status_code=200)
        response.json.return_value = {"response": OUTPUT, "done_reason": "stop"}
        client = client_type.return_value.__enter__.return_value
        client.post.return_value = response
        local = AuthorLLMClient(OllamaProvider(LLMConfig()))
        result = revoice_tool(
            vault_path=vault,
            text=INPUT,
            llm_factory=lambda _tier: local,
            phase="rising",
            register="conversational",
            privacy_tier_ceiling=TierCeiling.INTIMATE,
        )
    assert result["status"] == "ok"
    payload = client.post.call_args.kwargs["json"]
    assert CARE_POLICY in payload["prompt"]
    assert "VOICE_CORE_SENTINEL" in payload["prompt"]
    assert "PHASE_SENTINEL" in payload["prompt"]
    assert "REGISTER_SENTINEL" in payload["prompt"]
    assert INPUT in payload["prompt"]
    assert payload["prompt"].index("VOICE_CORE_SENTINEL") < payload["prompt"].index(
        INPUT
    )


@pytest.mark.parametrize(
    "heading",
    ["## Exemplar Passages", "###EXEMPLARS", "**Voice exemplars**", "Exemplars\n---"],
)
def test_custom_style_never_resumes_after_an_exemplar_boundary(
    vault: Path, heading: str
) -> None:
    """Later source headings cannot reopen a trusted custom style prefix."""
    _skills(vault)
    core = vault / "creek-skills/meta/voice-core.SKILL.md"
    core.write_text(
        "CUSTOM_STYLE_SENTINEL: short sentences.\n"
        f"{heading}\nPRIVATE_CUSTOM_CANARY\n"
        "## Writing Instructions\nPRIVATE_CUSTOM_ESCAPE_CANARY\n"
        "## Anti-Patterns\nPRIVATE_CUSTOM_TAIL_CANARY"
    )
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    assert "CUSTOM_STYLE_SENTINEL" in prompt.static
    assert "PRIVATE_CUSTOM" not in prompt.static
    assert prompt.voice_skills == (
        "voice-core",
        "phase-rising",
        "register-conversational",
    )


def test_malformed_native_core_is_withheld_without_raw_fallback(vault: Path) -> None:
    """An incomplete native cache cannot masquerade as a custom style file."""
    _skills(vault)
    core = vault / "creek-skills/meta/voice-core.SKILL.md"
    core.write_text(
        "---\nvariant: unknown\ncategory: meta\nkey: voice-core\n---\n"
        "PRIVATE_NATIVE_CORE_CANARY\n###EXEMPLARS\nPRIVATE_NATIVE_TAIL_CANARY"
    )
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    assert "PRIVATE_NATIVE" not in prompt.static
    assert render_voice_signature("voice-core") in prompt.static
    assert prompt.voice_skills == (
        "voice-core",
        "phase-rising",
        "register-conversational",
    )
    assert prompt.owner_style_skills == ("phase-rising", "register-conversational")


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
    # No fingerprint in this vault: the rewrite is returned, unrated.
    assert response["voice_check"]["voice_applied"] is None
    assert response["voice_check"]["voice_rating"] is None
    assert response["voice_check"]["owner_style_skills"] == [
        "voice-core",
        "phase-rising",
        "register-conversational",
    ]
    assert response["voice_check"]["owner_samples"] == 0
    assert response["voice_check"]["owner_profile"] is False
    assert response["voice_check"]["attempts"] == 1
    assert response["voice_check"]["diagnostics"] == []
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
    [rating] = _ledger(vault)
    assert {k: rating[k] for k in ("tool", "rated", "attempts")} == {
        "tool": "creek.revoice",
        "rated": False,
        "attempts": 1,
    }
    verify_mcp_audit_chain(vault)


def test_no_owner_voice_is_refused_before_any_model_spend(vault: Path) -> None:
    """Canonical skills alone are no one's voice: refuse, build no client."""
    provider = _Provider()
    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=provider.factory,
        register="analytical",
    )
    assert response == {
        "status": "refused",
        "tool": "creek.revoice",
        "tier_ceiling": "open",
        "reason": "no_owner_voice",
        "voice_skills": ["voice-core", "register-analytical"],
        "voice_check": {
            "voice_applied": False,
            "diagnostics": [],
            "owner_samples": 0,
            "owner_profile": False,
            "owner_style_skills": [],
            "attempts": 0,
        },
        "usage": {},
    }
    assert provider.tiers == []
    assert provider.prompts == []


def test_an_ai_draft_sample_cannot_supply_owner_voice(vault: Path) -> None:
    """Weight-0.0 AI prose and above-ceiling notes never count as grounding."""
    write_sample(
        vault,
        "ai-draft",
        (FIXTURES / "owner-samples" / "ai-draft.txt").read_text() * 3,
        voice_weight=0.0,
    )
    for index in (1, 2):
        write_sample(
            vault,
            f"private-{index}",
            (FIXTURES / "owner-samples" / "personal.txt").read_text(),
            tier=PrivacyTier.PERSONAL,
        )
    response = revoice_tool(
        vault_path=vault, text=INPUT, llm_factory=_Provider().factory
    )
    assert response["reason"] == "no_owner_voice"
    assert response["voice_check"]["owner_samples"] == 0


@pytest.mark.parametrize(
    "output",
    [
        INPUT,
        INPUT.replace(";", ".").replace("Thanks", "Thank you"),
        "Thanks for the invitation.",
    ],
    ids=["echo", "punctuation-and-a-word", "compression"],
)
def test_a_light_touch_on_an_in_voice_passage_succeeds_first_time(
    vault: Path, output: str
) -> None:
    """The end state decides: few changed words is not a failure (#1945 follow-up).

    The pre-rating gate refused every one of these as a copyedit.
    """
    _skills(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(text=output)
    response = revoice_tool(
        vault_path=vault,
        text=INPUT,
        llm_factory=provider.factory,
        register="conversational",
    )
    assert response["status"] == "ok"
    assert response["text"] == output
    assert response["voice_check"]["voice_applied"] is True
    assert response["voice_check"]["voice_rating"]["in_voice"] is True
    assert response["voice_check"]["attempts"] == 1
    assert len(provider.prompts) == 1


def test_off_voice_output_is_returned_after_one_retry_and_logged(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Off-voice is never refused: retried once, returned, rated and warned."""
    _skills(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(text=SLOP)
    with caplog.at_level(logging.WARNING, logger="creek.generate.ai_style.rating"):
        response = revoice_tool(
            vault_path=vault,
            text=INPUT,
            llm_factory=provider.factory,
            register="conversational",
        )
    assert response["status"] == "ok"
    assert response["text"] == SLOP
    check = response["voice_check"]
    assert check["voice_applied"] is False
    assert check["diagnostics"] == ["off_voice"]
    assert check["voice_rating"]["in_voice"] is False
    assert "ai_vocab_density" in check["voice_rating"]["diverging"]
    assert check["attempts"] == 2
    assert response["usage"] == {"input_tokens": 84, "output_tokens": 34}
    assert "## What the previous attempt got wrong" not in provider.prompts[0][1]
    retry = provider.prompts[1][1]
    assert "The attempt read as generic prose, not my voice." in retry
    assert "(over-use)" in retry
    [rating] = _ledger(vault)
    assert rating["in_voice"] is False
    assert rating["attempts"] == 2
    assert "off-voice result from creek.revoice" in caplog.text
    assert INPUT not in json.dumps(response["voice_check"])
    assert SLOP not in json.dumps(_ledger(vault))


def test_the_better_rated_attempt_is_returned(vault: Path) -> None:
    """When the retry is worse, the first attempt answers."""
    _skills(vault)
    install_owner_fingerprint(vault)
    worse = SLOP + " Furthermore, it underscores a vibrant, multifaceted Friday."
    provider = _Provider(texts=[SLOP, worse])
    response = revoice_tool(vault_path=vault, text=INPUT, llm_factory=provider.factory)
    assert response["status"] == "ok"
    assert response["text"] == SLOP
    assert response["voice_check"]["attempts"] == 2


def test_fixture_failed_after_is_refused_end_to_end(vault: Path) -> None:
    """Acceptance: the reported Before/After pair is refused by the live path."""
    install_fixture_samples(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(text=FAILED_AFTER)
    response = revoice_tool(
        vault_path=vault,
        text=SOURCE,
        llm_factory=provider.factory,
        register="analytical",
    )
    # It lost citations, which refuses; it is also off-voice, which alone would not.
    assert response["status"] == "refused"
    assert response["reason"] == "fidelity_violation"
    assert "text" not in response
    assert response["voice_check"]["diagnostics"] == [
        "off_voice",
        "citations_dropped",
    ]
    assert response["voice_check"]["voice_rating"]["in_voice"] is False
    assert response["voice_check"]["owner_samples"] == 3
    retry = provider.prompts[1][1]
    assert "## My own writing" in retry
    assert "housekeeping" in retry  # an admitted owner sample
    captured = repr(provider.prompts)
    assert "PERSONAL_SAMPLE_CANARY" not in captured
    assert "multifaceted cognitive capacity" not in captured
    assert all(
        "Owner style profile" not in (system or "") for system, _ in provider.prompts
    )


def test_fixture_owner_voiced_rewrite_passes_first_time(vault: Path) -> None:
    """Positive: a substantive owner-voiced rewrite returns ``ok`` in one call."""
    install_fixture_samples(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(text=OWNER_VOICED)
    response = revoice_tool(
        vault_path=vault,
        text=SOURCE,
        llm_factory=provider.factory,
        register="analytical",
    )
    assert response["status"] == "ok"
    assert response["text"] == OWNER_VOICED
    check = response["voice_check"]
    assert check["voice_applied"] is True
    assert check["attempts"] == 1
    assert check["voice_rating"]["voice_distance"] == 0.0
    first = provider.prompts[0][1]
    assert "## Owner style profile" in first
    assert "## My own writing" not in first
    assert "Keep every heading line, and every citation" in first


def test_a_rejected_first_attempt_can_succeed_on_the_retry(vault: Path) -> None:
    """The one retry is real: a passing second attempt returns ``ok``."""
    install_fixture_samples(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(texts=[FAILED_AFTER, OWNER_VOICED])
    response = revoice_tool(vault_path=vault, text=SOURCE, llm_factory=provider.factory)
    assert response["status"] == "ok"
    assert response["text"] == OWNER_VOICED
    assert response["voice_check"]["attempts"] == 2
    assert response["usage"] == {"input_tokens": 84, "output_tokens": 34}


def test_a_requested_register_is_never_silently_dropped(vault: Path) -> None:
    """Without a register file, its canonical signature is still applied."""
    _skills(vault)
    prompt = split_revoice_prompt(INPUT, vault, phase=None, register="analytical")
    assert prompt.voice_skills == ("voice-core", "register-analytical")
    assert render_voice_signature("register-analytical") in prompt.static
    assert prompt.owner_style_skills == ("voice-core",)


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
    _skills(vault)

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
    _skills(vault)

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
    _skills(vault)
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
    _skills(vault)
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
    assert response["status"] == "ok"
    assert response["text"] == OUTPUT
    assert response["voice_check"]["voice_applied"] is None
    assert provider.prompts


@pytest.mark.parametrize(
    ("text", "status"), [(INPUT, "ok"), (LINKED, "refused")], ids=["ok", "refused"]
)
def test_detached_rewrite_is_readable_but_never_persisted(
    vault: Path, monkeypatch: pytest.MonkeyPatch, text: str, status: str
) -> None:
    """A detached verdict, success or fidelity refusal, is readable but unpersisted."""
    monkeypatch.setenv("CREEK_MCP_SYNC_DEADLINE_SECONDS", "0.01")
    _skills(vault)
    install_owner_fingerprint(vault)
    provider = _Provider(text=OUTPUT)
    provider.gate = threading.Event()
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        revoice_llm_factory=provider.factory,
    )

    async def run() -> dict[str, Any]:
        accepted = _structured(await server.call_tool("creek.revoice", {"text": text}))
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
    body = result["result"]
    assert body["status"] == status
    assert body["voice_check"]["voice_applied"] is True
    if status == "ok":
        assert body["text"] == OUTPUT
    else:
        assert body["reason"] == "fidelity_violation"
        assert "text" not in body
        assert body["voice_check"]["attempts"] == 2
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
    _skills(vault)
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


def test_missing_core_uses_its_canonical_signature(vault: Path) -> None:
    """A missing core is the canonical core: applied, but not owner style."""
    _skills(vault)
    (vault / "creek-skills/meta/voice-core.SKILL.md").unlink()
    prompt = split_revoice_prompt(
        INPUT, vault, phase="rising", register="conversational"
    )
    assert render_voice_signature("voice-core") in prompt.static
    assert prompt.voice_skills == (
        "voice-core",
        "phase-rising",
        "register-conversational",
    )
    assert prompt.owner_style_skills == ("phase-rising", "register-conversational")


def test_declared_all_routes_intimate_and_provenance_is_preserved(vault: Path) -> None:
    """Broad caller sensitivity cannot win cloud routing through unclassified text."""
    _skills(vault)
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
    _skills(vault)
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
