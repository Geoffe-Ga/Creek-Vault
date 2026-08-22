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
    # has them. What keeps the unfiltered survey safe is *not* that the
    # findings stay in-process — ``creek state``'s ``## Lint summary`` appends
    # this report verbatim (#969), so they do leave. It is that the section
    # admits that copy whole or not at all, and only at ``ceiling=intimate``
    # or broader: an untierable Processing-Log artefact has no row-level tier
    # to filter on, so a partial copy would be a truncated report presented as
    # a complete one.
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
