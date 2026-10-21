"""Deterministic check: surface drafts that diverge from the user's voice.

The FEAT-040.9 guard runs synchronously during ``creek draft`` and stamps
each saved draft's frontmatter with ``voice_distance`` / ``voice_findings``.
This lint check is the audit pass that asks the operator to revisit any
draft whose distance exceeds the configured
:attr:`creek.config.AIStyleConfig.voice_distance_upper`.

**Every draft is measured from its body, stamp or no stamp (#1936).** The
check used to trust a stamped ``voice_distance`` and re-scan only the
unstamped ones, which meant the same content could get two different
verdicts: a stamp is a snapshot of what some earlier run measured, against
the fingerprint, config and aggregation of *that* moment. After #1936 that
divergence was no longer hypothetical — every draft stamped under the old
diluting mean carries a number too small to flag — so the audit pass now
re-measures the prose in front of it. The stamp remains in frontmatter as the
guard's attestation, which :mod:`creek.generate.voice_authenticity` still
reads.

The cost is paid knowingly: a scan is pure regex over the body — no model
call, no I/O beyond the read this check already did — but it is now paid for
*every* draft on *every* run rather than skipped for stamped ones, so the
check's cost grows with the number of drafts times the tell count instead of
with the unstamped ones alone. At a few hundred drafts that is milliseconds;
if ``creek lint`` latency ever becomes a complaint, this is the line to
revisit, and the thing to restore is not stamp-trust (it gives identical
content two verdicts) but a cache keyed on the body digest *and* the
fingerprint, config and aggregation version that produced the number.

**What this check does and does not catch.** ``voice_distance`` aggregates
the per-tell-family divergences from the user's measured rates, and the
shipped ceiling of 0.35 is cleared by one family diverging by more than
about one occurrence per 371 words — so a short draft carrying a single
AI-register tell is surfaced, while a divergence milder than each tell's own
margin is treated as ordinary variation and never surfaced at all. The
instrument measures *register*, by tell families someone enumerated; prose
that is off-voice in a way no tell measures scores 0.0. See
:mod:`creek.generate.ai_style.distance` for the aggregation and its
measurements.

**Epistemics — read this before acting on a finding.** Voice distance is a
*probabilistic, vault-relative* signal: it measures how far a draft sits from
the user's *own measured writing*, not whether it "is AI". A high distance
means "this reads less like you than your baseline" — a prompt to revise
toward your voice, never proof of authorship and never an accusation. Humans
and tools are poor at AI detection; this check exists to surface for review,
not to convict. It only reads: it never deletes, rewrites, or re-drafts.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.config import AIStyleConfig, load_config
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek.generate.ai_style.scanner import scan
from creek.lint._result import CheckResult
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from creek.generate.ai_style.model import VoiceFingerprint

_DRAFTS_RELPATH = ("07-Voice", "Drafts")
"""Vault-relative location of generated drafts (mirrors ``DRAFTS_SUBDIR``)."""

_MAX_DIVERGENCES = 3
"""How many top divergences to name per flagged draft, to keep findings terse."""

_REMEDIATION = "re-run `creek draft` or revise toward your measured voice"
"""The non-accusatory remediation hint appended to every finding."""


def _resolve_ai_style_config(vault_path: Path) -> AIStyleConfig:
    """Return the vault's ``ai_style`` config, or defaults on a missing config.

    A missing / malformed ``creek_config.yaml`` collapses to
    :class:`AIStyleConfig` defaults so the check always has a ceiling to
    compare against rather than failing the run.
    """
    config_path = vault_path / "00-Creek-Meta" / "creek_config.yaml"
    try:
        return load_config(config_path, warn_on_missing=False).ai_style
    except (OSError, ValueError, yaml.YAMLError):
        return AIStyleConfig()


def _measure_draft(
    post: frontmatter.Post,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> tuple[float, str]:
    """Return ``(voice_distance, divergences)`` measured from the draft's body.

    The stamped ``voice_distance`` is deliberately not consulted (#1936): a
    stamp records an earlier run's measurement, so trusting it would let the
    same content reach two verdicts. The named divergences are the worst
    families by evidence, so the operator reads the strongest signal first
    rather than whichever tell happens to be registered earliest.
    """
    report = scan(post.content, fingerprint=fingerprint, config=config)
    worst = sorted(
        {finding.feature_key: finding for finding in report.findings}.values(),
        key=lambda finding: report.family_evidence.get(finding.feature_key, 0.0),
        reverse=True,
    )
    divergences = ", ".join(
        f"{finding.direction}-use {finding.feature_key}"
        for finding in worst[:_MAX_DIVERGENCES]
    )
    return report.voice_distance, divergences


def _scan_draft(
    path: Path,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> str | None:
    """Inspect one draft, returning a finding line or ``None`` when on-voice."""
    try:
        post = frontmatter.load(str(path))
    except FRONTMATTER_LOAD_ERRORS:
        return None
    distance, divergences = _measure_draft(post, fingerprint, config)
    if distance <= config.voice_distance_upper:
        return None
    detail = f"; top divergences: {divergences}" if divergences else ""
    return (
        f"- `{path.stem}`: voice_distance={distance:.2f} > "
        f"{config.voice_distance_upper:.2f}{detail} ({_REMEDIATION})"
    )


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Surface drafts whose voice distance exceeds the configured ceiling.

    Loads the voice fingerprint and walks ``07-Voice/Drafts/``. When no
    fingerprint exists the check emits a single informational finding rather
    than failing — the operator must build the profile before drafts can be
    measured. The check never modifies a file.
    """
    del since  # The check reads frontmatter / body snapshots, not timestamps.
    config = _resolve_ai_style_config(vault_path)
    fingerprint = load_fingerprint(vault_path, config)
    if fingerprint.fragment_count == 0:
        return CheckResult(
            name="voice-fidelity",
            summary="no voice fingerprint found — cannot measure drafts",
            findings=[
                "- No voice fingerprint found. Run `creek report --type "
                "fingerprint` to profile your writing before the voice-fidelity "
                "check can measure drafts.",
            ],
        )
    drafts_dir = vault_path.joinpath(*_DRAFTS_RELPATH)
    if not drafts_dir.is_dir():
        return CheckResult(
            name="voice-fidelity",
            summary="0 draft(s) scanned (07-Voice/Drafts missing)",
        )
    findings: list[str] = []
    scanned = 0
    for md_file in sorted(drafts_dir.rglob("*.md")):
        scanned += 1
        finding = _scan_draft(md_file, fingerprint, config)
        if finding is not None:
            findings.append(finding)
    summary = (
        f"{scanned} draft(s) scanned; {len(findings)} above "
        f"voice_distance_upper={config.voice_distance_upper:.2f}"
    )
    return CheckResult(name="voice-fidelity", summary=summary, findings=findings)
