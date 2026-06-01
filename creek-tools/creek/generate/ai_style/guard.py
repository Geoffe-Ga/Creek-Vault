"""Voice-fidelity guard + bounded revision loop for ``creek draft`` (FEAT-040.9).

The integration capstone. After a draft is composed, this guard:

1. **Sanitizes** (FEAT-040.3/.4) — mechanical and markup tells repaired,
   typography normalised only against the user's own grain.
2. **Scans** (FEAT-040.5/.6/.7) against the voice fingerprint, producing a
   :class:`~creek.generate.ai_style.model.ScanReport` with a scalar
   ``voice_distance`` and directional (over/under-use) findings.
3. **Revises** — when the distance exceeds
   :attr:`~creek.config.AIStyleConfig.voice_distance_upper` and a rewrite LLM
   is available, runs a bounded loop that rewrites *toward the user's measured
   voice* (the directional deltas and the fingerprint-derived voice targets),
   re-sanitizes, and re-scans. The objective is **minimising voice distance**,
   not zeroing a tell checklist.

It mirrors :mod:`creek.generate.grounding`: deterministic-first, frontmatter
stamped, surfaced on stderr. Three invariants keep it safe:

* **Regression-guarded** — a rewrite is kept only when it *lowers* distance;
  a pass that raises (or fails to lower) distance is discarded.
* **Never trade grounding for voice** — an optional grounding check vetoes a
  rewrite that drops the draft below its grounding floor, so the guard
  composes with FEAT-032 rather than fighting it.
* **Bounded** — at most :attr:`~creek.config.AIStyleConfig.max_revision_passes`
  rewrites, and ``no_llm`` (or a disabled subsystem) reduces the guard to
  sanitize-and-measure with no network hop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from creek.generate.ai_style.sanitize_typography import sanitize
from creek.generate.ai_style.scanner import scan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import Finding, ScanReport, VoiceFingerprint

VoiceRewriteLLM = Callable[[str], str]
"""Signature for the rewrite LLM: ``(prompt) -> revised body``."""

GroundingCheck = Callable[[str], bool]
"""Signature for the optional grounding veto: ``(body) -> still grounded?``.

Returns ``True`` when the candidate body still clears its grounding floor.
A rewrite for which this returns ``False`` is discarded so voice fidelity is
never bought at the cost of grounding (compose with FEAT-032, don't fight it).
"""

VOICE_DISTANCE_KEY = "voice_distance"
"""Frontmatter key for the final scalar voice distance."""

VOICE_FINDINGS_KEY = "voice_findings"
"""Frontmatter key for the residual per-finding list."""


class VoiceFindingEntry(TypedDict):
    """Frontmatter shape for one residual voice-fidelity finding.

    Mirrors :class:`~creek.generate.ai_style.model.Finding` minus the raw
    span (redundant with ``line``/``excerpt``). A ``TypedDict`` lets
    ``mypy --strict`` catch key typos at the call site.
    """

    tell_id: str
    category: str
    feature_key: str
    line: int
    excerpt: str
    draft_rate: float
    user_rate: float | None
    direction: str
    message: str


@dataclass(frozen=True)
class _Candidate:
    """A body paired with its scan report, threaded through the revision loop."""

    body: str
    report: ScanReport


@dataclass(frozen=True)
class VoiceFidelityReport:
    """The guard's verdict for one draft.

    Attributes:
        body: The final body — sanitized, and rewritten toward voice when a
            rewrite lowered the distance. This is what ``save_draft`` writes.
        voice_distance: The final aggregate distance from the user's voice
            in ``[0, 1)``. ``0.0`` means the draft matches the user's profile.
        findings: The residual directional divergences after the final pass.
        thin_fingerprint: ``True`` when the fingerprint was below the
            configured minimum and the distance was softened accordingly.
        passes: How many rewrite passes were actually attempted (``0`` when
            the draft was already within bounds, ``no_llm``, or disabled).
    """

    body: str
    voice_distance: float
    findings: tuple[Finding, ...]
    thin_fingerprint: bool
    passes: int

    def summary_line(self) -> str:
        """Return the stderr one-liner ``creek draft`` prints after composing.

        Stable wording is part of the contract — the walkthrough and the
        integration tests match on it.
        """
        count = len(self.findings)
        noun = "divergence" if count == 1 else "divergences"
        return (
            f"voice-fidelity: distance {self.voice_distance:.2f} "
            f"({count} residual {noun}) — see frontmatter"
        )

    def to_frontmatter(self) -> dict[str, object]:
        """Render the report as the frontmatter payload ``save_draft`` writes."""
        return build_voice_fidelity_frontmatter(
            voice_distance=self.voice_distance,
            findings=self.findings,
        )


def _finding_entry(finding: Finding) -> VoiceFindingEntry:
    """Convert a :class:`Finding` to its frontmatter-friendly mapping."""
    user_rate = finding.user_rate
    return VoiceFindingEntry(
        tell_id=finding.tell_id,
        category=finding.category,
        feature_key=finding.feature_key,
        line=finding.line,
        excerpt=finding.excerpt,
        draft_rate=round(finding.draft_rate, 4),
        user_rate=None if user_rate is None else round(user_rate, 4),
        direction=finding.direction,
        message=finding.message,
    )


def build_voice_fidelity_frontmatter(
    *,
    voice_distance: float | None,
    findings: Sequence[Finding],
) -> dict[str, object]:
    """Build the partial frontmatter payload for voice-fidelity fields.

    Single source of truth for how the guard's fields appear on disk.
    ``None`` distance and an empty findings sequence are skipped so a draft
    saved without the guard never grows misleading zero-value fields.

    Args:
        voice_distance: The final scalar distance, or ``None`` when the guard
            did not run.
        findings: The residual findings (empty sequence when none).

    Returns:
        A dict carrying only the populated keys, for the caller to merge.
    """
    payload: dict[str, object] = {}
    if voice_distance is not None:
        payload[VOICE_DISTANCE_KEY] = round(voice_distance, 4)
    if findings:
        payload[VOICE_FINDINGS_KEY] = [_finding_entry(f) for f in findings]
    return payload


def build_voice_rewrite_prompt(
    body: str,
    report: ScanReport,
    *,
    style_preamble: str = "",
) -> str:
    """Assemble the targeted-rewrite prompt for one revision pass.

    The prompt names the directional divergences (over-used AI tells *and*
    under-used signature moves to restore), echoes the fingerprint-derived
    voice targets, and forbids inventing facts or dropping grounded content —
    the rewrite must move toward the user's voice, not toward emptiness.

    Args:
        body: The current (sanitized) draft body.
        report: The scan report whose findings drive the instruction.
        style_preamble: The FEAT-040.8 ``## Voice targets`` preamble; empty
            string to omit.

    Returns:
        The full rewrite prompt.
    """
    parts: list[str] = []
    if style_preamble.strip():
        parts.append(style_preamble.strip())
    if report.findings:
        divergences = "\n".join(
            f"- ({finding.direction}-use) {finding.message}"
            for finding in report.findings
        )
        parts.append(f"## Voice divergences to repair\n{divergences}")
    parts.extend(
        (
            "## Ask\n"
            "Revise the draft below to move toward the writer's measured voice: "
            "use their plain copulas and sentence rhythm, cut the puffery and "
            "transitions they do not use, de-pad triads, and restore any "
            "characteristic moves flagged as under-used. Do not invent facts and "
            "do not drop any grounded content — change how it reads, not what it "
            "claims. Return only the revised draft.",
            f"## Draft\n{body}",
        ),
    )
    return "\n\n".join(parts)


def _revise_toward_voice(
    best: _Candidate,
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
    llm: VoiceRewriteLLM,
    style_preamble: str,
    grounding_check: GroundingCheck | None,
    context: str,
) -> tuple[_Candidate, int]:
    """Run the bounded rewrite loop, returning the best candidate and pass count.

    A pass is kept only when it strictly lowers distance and (when a check is
    supplied) preserves grounding; otherwise the loop stops with the current
    best. The deterministic rewrite means a non-improving pass would repeat,
    so stopping on the first non-improvement is both correct and cheap.
    """
    passes = 0
    while (
        best.report.voice_distance > config.voice_distance_upper
        and passes < config.max_revision_passes
    ):
        passes += 1
        prompt = build_voice_rewrite_prompt(
            best.body, best.report, style_preamble=style_preamble
        )
        candidate_body = sanitize(llm(prompt), fingerprint=fingerprint, config=config)
        candidate_report = scan(
            candidate_body, fingerprint=fingerprint, config=config, context=context
        )
        if candidate_report.voice_distance >= best.report.voice_distance:
            break
        if grounding_check is not None and not grounding_check(candidate_body):
            break
        best = _Candidate(candidate_body, candidate_report)
    return best, passes


def run_voice_fidelity_guard(
    body: str,
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
    llm: VoiceRewriteLLM | None = None,
    no_llm: bool = False,
    style_preamble: str = "",
    grounding_check: GroundingCheck | None = None,
    context: str = "article",
) -> VoiceFidelityReport:
    """Sanitize, measure, and bounded-rewrite *body* toward the user's voice.

    Args:
        body: The composed draft body (frontmatter excluded).
        fingerprint: The user's voice fingerprint.
        config: The AI-style configuration (ceiling, pass cap, toggles).
        llm: The rewrite LLM ``(prompt) -> body``; ``None`` skips the rewrite.
        no_llm: When ``True``, sanitize and measure only — no rewrite hop.
        style_preamble: The FEAT-040.8 voice-targets preamble for the rewrite.
        grounding_check: Optional veto rejecting a rewrite that drops grounding.
        context: Scan context (``"article"`` or ``"comment"``).

    Returns:
        A :class:`VoiceFidelityReport` carrying the final body, distance,
        residual findings, and pass count. When the subsystem is disabled the
        body is returned untouched with a zero distance and no findings.
    """
    if not config.enabled:
        return VoiceFidelityReport(
            body=body,
            voice_distance=0.0,
            findings=(),
            thin_fingerprint=False,
            passes=0,
        )
    sanitized = sanitize(body, fingerprint=fingerprint, config=config)
    report = scan(sanitized, fingerprint=fingerprint, config=config, context=context)
    best = _Candidate(sanitized, report)
    passes = 0
    if llm is not None and not no_llm:
        best, passes = _revise_toward_voice(
            best,
            fingerprint=fingerprint,
            config=config,
            llm=llm,
            style_preamble=style_preamble,
            grounding_check=grounding_check,
            context=context,
        )
    return VoiceFidelityReport(
        body=best.body,
        voice_distance=best.report.voice_distance,
        findings=tuple(best.report.findings),
        thin_fingerprint=best.report.thin_fingerprint,
        passes=passes,
    )
