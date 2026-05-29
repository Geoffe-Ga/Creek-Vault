"""Deterministic check: surface drafts that failed the grounding guard.

The :mod:`creek.generate.grounding` guard runs synchronously during
``creek draft`` and stamps each saved draft's frontmatter with
``derivative_score`` / ``grounding_score`` / ``paragraph_grounding``.
This lint check is the audit pass that asks the operator to revisit
any draft whose stored scores fall outside the configured
``draft.derivative_upper`` / ``draft.grounding_fraction_lower``
thresholds.

Drafts written before issue #355 (and any markdown file that simply
lacks the scores) are skipped silently — re-running ``creek draft``
will backfill the metric. The check never deletes, rewrites, or
auto-redrafts: it only surfaces, in keeping with the non-negotiable
lint rules pinned by :mod:`creek.lint`.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter
import yaml

from creek.config import DraftConfig, load_config
from creek.lint._result import CheckResult

_DRAFTS_RELPATH = ("07-Voice", "Drafts")
"""Vault-relative location of generated drafts.

Mirrors :data:`creek.generate.drafts.DRAFTS_SUBDIR` but kept as a
tuple here so the check can compose it with ``Path.joinpath`` without
importing the heavier drafts module."""


def _resolve_draft_config(vault_path: Path) -> DraftConfig:
    """Load ``creek_config.yaml`` from *vault_path* and return its ``draft`` section.

    Missing / malformed configs collapse to :class:`DraftConfig`
    defaults so the check always has thresholds to compare against —
    silently emitting the wrong findings would be worse than the
    lenient default.
    """
    config_path = vault_path / "00-Creek-Meta" / "creek_config.yaml"
    try:
        return load_config(config_path, warn_on_missing=False).draft
    except (OSError, ValueError, yaml.YAMLError):
        return DraftConfig()


def _scan_draft(path: Path, draft_config: DraftConfig) -> str | None:
    """Inspect one draft and return a single finding line, or ``None`` if clean.

    Drafts that do not yet carry the guard's frontmatter (legacy drafts
    written before #355) are treated as clean — the operator can
    regenerate them when convenient.
    """
    try:
        post = frontmatter.load(str(path))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    metadata = post.metadata
    derivative = metadata.get("derivative_score")
    grounding = metadata.get("grounding_score")
    if not isinstance(derivative, (int, float)) or not isinstance(
        grounding,
        (int, float),
    ):
        return None
    failures: list[str] = []
    if derivative >= draft_config.derivative_upper:
        failures.append(
            f"derivative={derivative:.2f} ≥ {draft_config.derivative_upper:.2f}",
        )
    if grounding < draft_config.grounding_fraction_lower:
        failures.append(
            f"grounding={grounding:.2f} < {draft_config.grounding_fraction_lower:.2f}",
        )
    if not failures:
        return None
    return f"- `{path.stem}`: " + "; ".join(failures)


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Surface drafts whose grounding scores fall outside the configured bounds.

    Each finding lists the draft's filename stem (a stable identifier
    operators recognise from ``07-Voice/Drafts/``) and the failing
    metric(s) with their threshold so a reader can decide whether to
    re-draft, re-prompt with different sources, or accept the verdict.
    """
    del since  # The check reads frontmatter snapshots, not timestamps.
    drafts_dir = vault_path.joinpath(*_DRAFTS_RELPATH)
    if not drafts_dir.is_dir():
        return CheckResult(
            name="draft-grounding",
            summary="0 draft(s) scanned (07-Voice/Drafts missing)",
        )
    draft_config = _resolve_draft_config(vault_path)
    findings: list[str] = []
    scanned = 0
    for md_file in sorted(drafts_dir.rglob("*.md")):
        scanned += 1
        finding = _scan_draft(md_file, draft_config)
        if finding is not None:
            findings.append(finding)
    summary = (
        f"{scanned} draft(s) scanned; {len(findings)} outside "
        f"derivative_upper={draft_config.derivative_upper:.2f} / "
        f"grounding_fraction_lower={draft_config.grounding_fraction_lower:.2f}"
    )
    return CheckResult(name="draft-grounding", summary=summary, findings=findings)
