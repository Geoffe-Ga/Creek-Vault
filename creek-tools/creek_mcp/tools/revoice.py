"""Thin MCP adapter for rewriting caller-supplied text in the owner's voice.

Nothing is saved here. Prompt assembly belongs to the Writing Desk's voice
stack, and the injected tier-keyed factory owns routing. Only declared
ceilings are echoed, never derived sensitivity or provider errors.

Creek, not the caller, decides whether the voice was applied:

1. **Grounding.** Owner-authored style skills, or the owner's register
   samples admitted at the declared ceiling
   (:mod:`creek.author.revoice_grounding`). Canonical skills are the same for
   every vault and do not count. Without either source, the call is refused
   as ``no_owner_voice`` before any model spend.
2. **Gate.** Every rewrite is judged by :mod:`creek.author.revoice_check`
   (fidelity, per-section transformation, opening, style shift).
3. **One bounded retry.** A failed first attempt gets exactly one more,
   with owner excerpts and fixed feedback naming the failed checks. There are
   never more than two model calls. A detached job runs this whole function
   once, so job semantics are unchanged.

A rewrite the gate rejects returns ``status: "refused"`` with a closed
``reason`` and content-free ``voice_check`` diagnostics. It never returns the
rejected text, so no caller can present it as the owner's voice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from creek.author.revoice_check import RevoiceCheck, RevoiceReason, check_revoice
from creek.author.revoice_grounding import (
    RETRY_FEEDBACK,
    OwnerGrounding,
    owner_grounding,
)
from creek.author.revoice_style import render_profile
from creek.author.voice import RevoiceGrounding, RevoicePrompt, split_revoice_prompt
from creek.classify.llm.router import IntimateRoutingError
from creek.models import Phase, PrivacyTier, VoiceRegister
from creek.progress import CompositionStage, StageCallback, ignore_stage
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    routing_tier,
    to_privacy_override,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.client import AuthorLLMClient
    from creek.classify.llm.completion import Completion
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
VOICE_REFUSAL_REASONS: Final = frozenset(reason.value for reason in RevoiceReason)
"""Closed ``reason`` codes of a voice refusal, as opposed to a routing one."""


class RevoiceLLMFactory(Protocol):
    """Build a voice client through the existing tier-aware routing chokepoint."""

    def __call__(self, tier: PrivacyTier | None) -> AuthorLLMClient | None:
        """Return a routed client, or ``None`` when no provider can serve."""


@dataclass(frozen=True)
class _Request:
    """One validated revoice request."""

    vault: Path
    text: str
    register: str | None
    phase: str | None
    ceiling: TierCeiling


@dataclass(frozen=True)
class _Attempt:
    """One model call and the gate's verdict on it."""

    prompt: RevoicePrompt
    completion: Completion
    check: RevoiceCheck


def is_voice_outcome(response: dict[str, Any]) -> bool:
    """Return whether *response* is a finished rewrite verdict, success or not.

    Both are content-free apart from an ``ok`` body, so a detached job keeps
    either one for ``creek.jobs.read``. Routing refusals and errors do not
    count.
    """
    status = response.get("status")
    return status == "ok" or (
        status == "refused" and response.get("reason") in VOICE_REFUSAL_REASONS
    )


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


def _local_provider_refusal(ceiling: TierCeiling) -> dict[str, Any]:
    """Return the canonical refusal for a ceiling no local provider can serve."""
    return refusal_response(
        tool=TOOL_NAME,
        ceiling=ceiling,
        reason="The declared ceiling requires an available local provider",
    )


def _grounding_summary(
    owner: OwnerGrounding, prompt: RevoicePrompt, attempts: int
) -> dict[str, Any]:
    """Return the content-free description of what grounded the rewrite."""
    return {
        "owner_samples": owner.sample_count,
        "owner_profile": owner.profile is not None,
        "owner_style_skills": list(prompt.owner_style_skills),
        "attempts": attempts,
    }


def _add_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
    """Accumulate one call's token usage into *total*."""
    for key, value in (usage or {}).items():
        total[key] = total.get(key, 0) + value


def _complete(
    request: _Request, client: AuthorLLMClient, grounding: RevoiceGrounding
) -> tuple[RevoicePrompt, Completion] | None:
    """Make one bounded model call, or return ``None`` for an unusable output."""
    prompt = split_revoice_prompt(
        request.text,
        request.vault,
        phase=request.phase,
        register=request.register,
        grounding=grounding,
    )
    completion = client.complete_with_usage(
        prompt.dynamic, system=prompt.static, max_tokens=MAX_REVOICE_OUTPUT_TOKENS
    )
    if (
        completion.stop_reason == "max_tokens"
        or not completion.text.strip()
        or len(completion.text) > MAX_REVOICE_OUTPUT_CHARS
    ):
        return None
    return prompt, completion


def _attempts(
    request: _Request,
    client: AuthorLLMClient,
    owner: OwnerGrounding,
    usage: dict[str, int],
) -> list[_Attempt] | None:
    """Run the first attempt and, only if the gate rejects it, one retry.

    Returns:
        The attempts made, last one decisive, or ``None`` when a model call
        returned an unusable output.
    """
    profile = render_profile(owner.profile) if owner.profile else None
    grounding = RevoiceGrounding(profile=profile)
    made: list[_Attempt] = []
    for _round in range(2):
        called = _complete(request, client, grounding)
        if called is None:
            return None
        prompt, completion = called
        _add_usage(usage, completion.usage)
        check = check_revoice(request.text, completion.text, owner=owner.profile)
        made.append(_Attempt(prompt, completion, check))
        if check.reason is None:
            break
        grounding = RevoiceGrounding(
            profile=profile,
            excerpts=owner.excerpts,
            feedback=tuple(RETRY_FEEDBACK[d] for d in check.diagnostics),
        )
    return made


def _verdict(
    request: _Request,
    owner: OwnerGrounding,
    attempts: list[_Attempt],
    usage: dict[str, int],
) -> dict[str, Any]:
    """Render the decisive attempt as an ``ok`` rewrite or a voice refusal."""
    last = attempts[-1]
    voice_check = last.check.as_dict() | _grounding_summary(
        owner, last.prompt, len(attempts)
    )
    response: dict[str, Any] = {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": request.ceiling.value,
        "voice_skills": list(last.prompt.voice_skills),
        "voice_check": voice_check,
        "usage": usage,
    }
    if last.check.reason is None:
        response["text"] = last.completion.text
    else:
        response["status"] = "refused"
        response["reason"] = last.check.reason.value
    return response


def _no_owner_voice(
    request: _Request, owner: OwnerGrounding, prompt: RevoicePrompt
) -> dict[str, Any]:
    """Refuse before any model spend: nothing owner-specific could ground it."""
    return {
        "status": "refused",
        "tool": TOOL_NAME,
        "tier_ceiling": request.ceiling.value,
        "reason": RevoiceReason.NO_OWNER_VOICE.value,
        "voice_skills": list(prompt.voice_skills),
        "voice_check": {"voice_applied": False, "diagnostics": []}
        | _grounding_summary(owner, prompt, 0),
        "usage": {},
    }


def _rewrite(
    request: _Request,
    llm_factory: RevoiceLLMFactory | None,
    on_stage: StageCallback,
    provenance: InferenceProvenance | None,
) -> dict[str, Any]:
    """Ground, delegate at most twice, gate, and render the structured result."""
    owner = owner_grounding(
        request.vault,
        override=to_privacy_override(request.ceiling),
        register=request.register,
    )
    plain = split_revoice_prompt(
        request.text, request.vault, phase=request.phase, register=request.register
    )
    on_stage(CompositionStage.SKILLS)
    if owner.profile is None and not plain.owner_style_skills:
        return _no_owner_voice(request, owner, plain)
    client = (
        None
        if llm_factory is None
        else llm_factory(routing_tier(request.ceiling, None))
    )
    if client is None:
        return _error(request.ceiling, "No model is available to revoice this passage")
    usage: dict[str, int] = {}
    attempts = _attempts(request, client, owner, usage)
    if attempts is None:
        return _error(
            request.ceiling, "The model did not return a complete bounded rewrite"
        )
    on_stage(CompositionStage.COMPOSED)
    response = _verdict(request, owner, attempts, usage)
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
    """Rewrite supplied text in the owner's voice, without saving, or refuse.

    Args:
        vault_path: The served vault's voice-skill and register-sample root.
        text: The passage whose meaning, claims and apparatus must be kept.
        llm_factory: Tier-keyed client builder; this adapter chooses no provider.
        privacy_tier_ceiling: The caller's declared sensitivity, used for
            routing and for admitting owner samples.
        consumer: The authenticated agent's audit identity.
        register: A canonical voice-register slug, or no register conditioning.
        phase: A canonical phase slug, or no phase conditioning.
        provenance: Request-scoped routing recorder, when supplied by the server.
        on_stage: Content-free progress sink.

    Returns:
        ``ok`` with text, applied skill names, ``voice_check`` and summed usage
        when the gate accepts the rewrite. ``refused`` with a closed ``reason``
        (``no_owner_voice``, ``insufficient_transformation`` or
        ``fidelity_violation``) and ``voice_check`` diagnostics, but no text,
        when it does not. A static ``error`` for invalid input or model
        failure, or the canonical routing refusal. Every attempted call
        appends only its input/output character counts to the audit log.
    """
    reason = _invalid(text, register, phase)
    response = _error(privacy_tier_ceiling, reason) if reason is not None else None
    try:
        if response is None:
            response = _rewrite(
                _Request(vault_path, text, register, phase, privacy_tier_ceiling),
                llm_factory,
                on_stage,
                provenance,
            )
    except IntimateRoutingError:
        response = _local_provider_refusal(privacy_tier_ceiling)
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
