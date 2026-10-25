"""Thin MCP adapter for rewriting caller-supplied text in the owner's voice.

No corpus retrieval or saving happens here. Prompt assembly belongs to the
Writing Desk's voice stack; the injected tier-keyed factory owns routing.
Only declared ceilings are echoed, never derived sensitivity or provider errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

from creek.author.voice import split_revoice_prompt
from creek.classify.llm.router import IntimateRoutingError
from creek.models import Phase, PrivacyTier, VoiceRegister
from creek.progress import CompositionStage, StageCallback, ignore_stage
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, routing_tier

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.client import AuthorLLMClient
    from creek_mcp.provenance import InferenceProvenance

TOOL_NAME: Final = "creek.revoice"
MAX_REVOICE_CHARS: Final = 16_000
"""Input character ceiling, bounding model spend for one rewrite."""
MAX_REVOICE_OUTPUT_CHARS: Final = 32_000
"""Bound retained results even when a provider ignores its token limit."""
MAX_REVOICE_OUTPUT_TOKENS: Final = 8_192
"""Enough room to render a maximum-length passage without unbounded output."""
REVOICE_STAGES: Final = (CompositionStage.SKILLS, CompositionStage.COMPOSED)
"""The two progress boundaries crossed by a rewrite that saves nothing."""


class RevoiceLLMFactory(Protocol):
    """Build a voice client through the existing tier-aware routing chokepoint."""

    def __call__(self, tier: PrivacyTier | None) -> AuthorLLMClient | None:
        """Return a routed client, or ``None`` when no provider can serve."""


def _invalid(text: str, register: str | None, phase: str | None) -> str | None:
    """Validate source length and canonical slugs without echoing any input."""
    if not text.strip():
        return "text must be non-empty"
    if len(text) > MAX_REVOICE_CHARS:
        return f"text exceeds the {MAX_REVOICE_CHARS} character limit"
    if register is not None and register not in {
        member.value for member in VoiceRegister
    }:
        return "register must name a canonical voice register"
    if phase is not None and phase not in {member.value for member in Phase}:
        return "phase must name a canonical wavelength phase"
    return None


def _error(ceiling: TierCeiling, reason: str) -> dict[str, Any]:
    """Return a static error without either passage or provider exception text."""
    return {
        "status": "error",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "reason": reason,
    }


def _rewrite(
    *,
    vault_path: Path,
    text: str,
    register: str | None,
    phase: str | None,
    llm_factory: RevoiceLLMFactory | None,
    ceiling: TierCeiling,
    on_stage: StageCallback,
    provenance: InferenceProvenance | None,
) -> dict[str, Any]:
    """Assemble the core prompt, delegate once, and render the structured result."""
    prompt = split_revoice_prompt(text, vault_path, phase=phase, register=register)
    on_stage(CompositionStage.SKILLS)
    client = None if llm_factory is None else llm_factory(routing_tier(ceiling, None))
    if client is None:
        return _error(ceiling, "No model is available to revoice this passage")
    completion = client.complete_with_usage(
        prompt.dynamic, system=prompt.static, max_tokens=MAX_REVOICE_OUTPUT_TOKENS
    )
    if (
        completion.stop_reason == "max_tokens"
        or not completion.text.strip()
        or len(completion.text) > MAX_REVOICE_OUTPUT_CHARS
    ):
        return _error(ceiling, "The model did not return a complete bounded rewrite")
    on_stage(CompositionStage.COMPOSED)
    response: dict[str, Any] = {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "text": completion.text,
        "voice_skills": list(prompt.voice_skills),
        "usage": completion.usage or {},
    }
    if provenance is not None and provenance.source is not None:
        response["inference_source"] = provenance.source
    return response


def revoice_tool(
    *,
    vault_path: Path,
    text: str,
    llm_factory: RevoiceLLMFactory | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    register: str | None = None,
    phase: str | None = None,
    provenance: InferenceProvenance | None = None,
    on_stage: StageCallback = ignore_stage,
) -> dict[str, Any]:
    """Rewrite supplied text using only the owner's voice skills, without saving.

    Args:
        vault_path: The served vault's voice-skill root.
        text: The passage whose meaning and claims must be preserved.
        llm_factory: Tier-keyed client builder; this adapter chooses no provider.
        privacy_tier_ceiling: The caller's declared sensitivity, used for routing.
        consumer: The authenticated agent's audit identity.
        register: A canonical voice-register slug, or no register conditioning.
        phase: A canonical phase slug, or no phase conditioning.
        provenance: Request-scoped routing recorder, when supplied by the server.
        on_stage: Content-free progress sink.

    Returns:
        An ``ok`` envelope with text, applied skill names and usage; a static
        ``error`` for invalid input/model failure; or the canonical routing refusal.
        Every attempted call appends only its input/output character counts.
    """
    reason = _invalid(text, register, phase)
    response = _error(privacy_tier_ceiling, reason) if reason is not None else None
    try:
        if response is None:
            response = _rewrite(
                vault_path=vault_path,
                text=text,
                register=register,
                phase=phase,
                llm_factory=llm_factory,
                ceiling=privacy_tier_ceiling,
                on_stage=on_stage,
                provenance=provenance,
            )
    except IntimateRoutingError:
        response = dict(
            refusal_response(
                tool=TOOL_NAME,
                ceiling=privacy_tier_ceiling,
                reason="The declared ceiling requires an available local provider",
            )
        )
    except Exception:
        # Providers may quote the request in their exceptions. Nothing derived
        # from one may cross a response, audit entry, or process log here.
        response = _error(
            privacy_tier_ceiling, "The model could not revoice this passage"
        )
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"input_chars": len(text), "output_chars": len(response.get("text", ""))},
    )
    return response
