"""The Reflection node for the Creek Writing Desk (FEAT-041, #473).

A *deterministic* judge — no LLM, so the mutation tests can assert exact
verdicts — scores a drafted body against the research-rubric dimensions and
returns a structured :class:`~creek.author.models.ReflectionResult`
(``PASS | REVISE | ESCALATE`` plus findings). Citation completeness and privacy
compliance are HARD gates for research. The conductor (#473) owns the retry loop
and the exhaustion → ESCALATE policy; this node decides only one round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.checks import (
    check_attribution_correctness,
    check_biographical_grounding,
    check_citation_completeness,
    check_ontological_accuracy,
    check_paradox_preservation,
    check_privacy_compliance,
    check_unglossed_jargon,
    check_voice_fidelity,
)
from creek.author.models import (
    EvidenceBundle,
    ReflectionFinding,
    ReflectionResult,
    ReflectionVerdict,
)
from creek.config import DraftConfig

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.generate.grounding import EmbeddingFn
    from creek.models import MediumContract

#: Default cosine floor a first-person biographical sentence must clear to count
#: as grounded when no explicit threshold is wired in (issue #515). Derived from
#: the ``DraftConfig.grounding_lower`` field default — the single source of truth
#: — so the desk and draft paths can never drift on what "grounded enough" means
#: for a single sentence.
_DEFAULT_BIOGRAPHICAL_GROUNDING_LOWER: float = DraftConfig.model_fields[
    "grounding_lower"
].default


class ReflectionNode:
    """A deterministic judge scoring a draft against the research rubric dimensions."""

    def review(
        self,
        body: str,
        evidence: EvidenceBundle,
        rubric: dict[str, float] | None = None,
        *,
        contract: MediumContract | None = None,
        vault: Path | None = None,
        fingerprint: VoiceFingerprint | None = None,
        ai_style_config: AIStyleConfig | None = None,
        embedding_fn: EmbeddingFn | None = None,
        grounding_lower: float = _DEFAULT_BIOGRAPHICAL_GROUNDING_LOWER,
        voice_core: str | None = None,
    ) -> ReflectionResult:
        """Judge *body* against *evidence* and return a structured result.

        When the draft cannot be authored at all — an empty body or no
        grounded claims — the node ``ESCALATE``s immediately. Otherwise it runs
        every check whose inputs are present, collects the findings, and
        decides ``PASS`` (no findings) or ``REVISE`` (at least one). Single-call
        ``ESCALATE`` is reserved for the no-draft case; escalation on retry
        exhaustion is the conductor's responsibility.

        Args:
            body: The drafted prose under review.
            evidence: The evidence the draft was rendered from.
            rubric: The medium's per-dimension reflection weights (§8). The
                deterministic checks gate on hard rules rather than weights, so
                this is accepted for interface stability but not scored.
            contract: The medium contract; its ``default_privacy_tier`` is the
                privacy ceiling. ``None`` does **not** skip the privacy check —
                it gates at the strictest ceiling instead (#1310); see
                :func:`~creek.author.checks.check_privacy_compliance`.
            vault: The vault root used to resolve cited fragments' privacy
                tiers. ``None`` skips the privacy check.
            fingerprint: The owner's voice fingerprint. ``None`` skips voice
                fidelity (the voice cannot be measured without it).
            ai_style_config: The AI-style configuration. ``None`` skips voice
                fidelity.
            embedding_fn: Embedding callable for the biographical-grounding
                check (issue #515). ``None`` skips it — the check is dormant
                unless a caller wires an embedder in.
            grounding_lower: Cosine floor a first-person biographical sentence
                must clear against the evidence to count as grounded.
            voice_core: The voice-core brief text, treated as tone guidance a
                biographical claim may also trace to, or ``None``.

        Returns:
            A :class:`ReflectionResult` with the verdict and any findings.
        """
        del rubric  # deterministic checks gate on hard rules, not weights
        if not body.strip() or not evidence.claims:
            return ReflectionResult(decision="ESCALATE")

        findings: list[ReflectionFinding] = []
        findings.extend(check_citation_completeness(evidence))
        findings.extend(check_privacy_compliance(body, evidence, vault, contract))
        findings.extend(check_ontological_accuracy(body))
        findings.extend(check_unglossed_jargon(body))
        findings.extend(check_paradox_preservation(body, evidence))
        findings.extend(check_attribution_correctness(body, evidence))
        findings.extend(check_voice_fidelity(body, fingerprint, ai_style_config))
        findings.extend(
            check_biographical_grounding(
                body,
                evidence,
                embedding_fn=embedding_fn,
                grounding_lower=grounding_lower,
                voice_core=voice_core,
            )
        )

        decision: ReflectionVerdict = "PASS" if not findings else "REVISE"
        return ReflectionResult(decision=decision, findings=findings)
