"""Deterministic check: surface orphan tags via :class:`TagGardenGenerator`."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate.tags import TagGardenGenerator
from creek.lint._result import CheckResult


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Scan vault tags and surface orphans + merge candidates."""
    del since
    # Stated explicitly rather than inherited (#968): an orphan-tag check must
    # survey the whole vault or it reports "no orphan tags" about a vault that
    # has them. Safe to keep unfiltered because ``creek.lint``'s read posture is
    # METADATA_ONLY — ``findings`` never leave the process over MCP.
    scan = TagGardenGenerator(
        vault_path=vault_path,
        override=PrivacyTierOverride.ALL,
    ).scan_tags()
    orphans = sorted(tag for tag, count in scan.tag_counts.items() if count == 1)
    findings = [f"- `{tag}` (single use; not auto-deleted)" for tag in orphans]
    summary = (
        f"{len(scan.tag_counts)} tag(s) tracked; {len(orphans)} single-use orphan(s)"
    )
    return CheckResult(name="tags", summary=summary, findings=findings)
