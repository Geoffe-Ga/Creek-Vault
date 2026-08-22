"""Deterministic check: surface drafts that diverge from the user's voice.

The FEAT-040.9 guard runs synchronously during ``creek draft`` and stamps
each saved draft's frontmatter with ``voice_distance`` / ``voice_findings``.
This lint check is the audit pass that asks the operator to revisit any
draft whose distance exceeds the configured
:attr:`creek.config.AIStyleConfig.voice_distance_upper`. Drafts that lack the
stamp (written before the guard, or hand-authored) are re-scanned against the
voice fingerprint so the check still has a measurement.

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


def _stamped_divergences(raw: object) -> str:
    """Format the top stamped ``voice_findings`` entries as ``dir-use feature``."""
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for entry in raw[:_MAX_DIVERGENCES]:
        if isinstance(entry, dict):
            direction = entry.get("direction", "?")
            feature = entry.get("feature_key", "?")
            parts.append(f"{direction}-use {feature}")
    return ", ".join(parts)


def _measure_draft(
    post: frontmatter.Post,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> tuple[float, str]:
    """Return ``(voice_distance, divergences)`` for one draft.

    Reads the stamped ``voice_distance`` / ``voice_findings`` when present;
    otherwise re-scans the body against the fingerprint so unstamped drafts
    are still measured.
    """
    stamped = post.metadata.get("voice_distance")
    if isinstance(stamped, (int, float)):
        return float(stamped), _stamped_divergences(post.metadata.get("voice_findings"))
    report = scan(post.content, fingerprint=fingerprint, config=config)
    divergences = ", ".join(
        f"{finding.direction}-use {finding.feature_key}"
        for finding in report.findings[:_MAX_DIVERGENCES]
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
