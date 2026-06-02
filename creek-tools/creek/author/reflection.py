"""The Reflection node for the Creek Writing Desk (FEAT-041, #473).

A *deterministic* judge — no LLM, so the mutation tests can assert exact
verdicts — scores a drafted body against the six research-rubric dimensions and
returns a structured :class:`~creek.author.models.ReflectionResult`
(``PASS | REVISE | ESCALATE`` plus findings). Citation completeness and privacy
compliance are HARD gates for research. The conductor (#473) owns the retry loop
and the exhaustion → ESCALATE policy; this node decides only one round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.checks import (
    check_attribution_correctness,
    check_citation_completeness,
    check_ontological_accuracy,
    check_paradox_preservation,
    check_privacy_compliance,
    check_voice_fidelity,
)
from creek.author.models import (
    EvidenceBundle,
    ReflectionFinding,
    ReflectionResult,
    ReflectionVerdict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.models import MediumContract


class ReflectionNode:
    """A deterministic judge that scores a draft against the six dimensions."""

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
                privacy ceiling. ``None`` skips the privacy check.
            vault: The vault root used to resolve cited fragments' privacy
                tiers. ``None`` skips the privacy check.
            fingerprint: The owner's voice fingerprint. ``None`` skips voice
                fidelity (the voice cannot be measured without it).
            ai_style_config: The AI-style configuration. ``None`` skips voice
                fidelity.

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
        findings.extend(check_paradox_preservation(body, evidence))
        findings.extend(check_attribution_correctness(body, evidence))
        findings.extend(check_voice_fidelity(body, fingerprint, ai_style_config))

        decision: ReflectionVerdict = "PASS" if not findings else "REVISE"
        return ReflectionResult(decision=decision, findings=findings)
