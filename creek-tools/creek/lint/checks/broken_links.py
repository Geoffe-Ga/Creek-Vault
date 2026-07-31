"""Deterministic check: wiki-links and relative links must resolve."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # used at runtime to render sources vault-relative

from creek.clean.hygiene import BrokenLinkScanner
from creek.lint._result import CheckResult


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Scan fragments for broken wiki-links / relative markdown links.

    Wraps :class:`creek.clean.hygiene.BrokenLinkScanner`. The ``since``
    parameter is accepted for interface consistency but ignored — the
    scanner is already fast enough that filtering by mtime adds little.

    The source is rendered **vault-relative**, matching every sibling check
    (``compost``, ``unnamed``, ``skill_size_budget``, ``orphan_compiled``).
    This one was the outlier: :class:`~creek.clean.hygiene.BrokenLinkScanner`
    keys its result by ``str(path)``, which on a normal call is an absolute
    path under the operator's home directory, and ``creek state``'s
    ``## Lint summary`` appends this report **verbatim** at
    ``ceiling=intimate`` or broader — including a plain ``creek state``, whose
    default ceiling is ``all`` (#969). An absolute source here therefore put
    ``/Users/<operator>/...`` into a committable, shareable artefact and
    falsified the standing "no path in the artefact is absolute" claim in
    ``docs/generation.md``.

    ``relative_to`` cannot raise: the scanner builds every key from
    ``vault_path / "01-Fragments"`` by the same lexical join used here.
    """
    del since  # interface symmetry only
    scan = BrokenLinkScanner().scan(vault_path)
    findings: list[str] = []
    for source, targets in sorted(scan.broken_links.items()):
        relative = Path(source).relative_to(vault_path)
        for target in targets:
            findings.append(f"- `{relative}` → `{target}`")
    summary = (
        f"{scan.total_broken} broken link(s) across {scan.total_files_scanned} file(s)"
    )
    return CheckResult(name="broken-links", summary=summary, findings=findings)
