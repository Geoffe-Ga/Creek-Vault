"""Deterministic check: wiki-links and relative links must resolve."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.clean.hygiene import BrokenLinkScanner
from creek.lint._result import CheckResult


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Scan fragments for broken wiki-links / relative markdown links.

    Wraps :class:`creek.clean.hygiene.BrokenLinkScanner`. The ``since``
    parameter is accepted for interface consistency but ignored — the
    scanner is already fast enough that filtering by mtime adds little.
    """
    del since  # interface symmetry only
    scan = BrokenLinkScanner().scan(vault_path)
    findings: list[str] = []
    for source, targets in sorted(scan.broken_links.items()):
        for target in targets:
            findings.append(f"- `{source}` → `{target}`")
    summary = (
        f"{scan.total_broken} broken link(s) across {scan.total_files_scanned} file(s)"
    )
    return CheckResult(name="broken-links", summary=summary, findings=findings)
