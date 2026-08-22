"""Deterministic check: wiki-links and relative links must resolve.

Surveyed across the whole vault minus Creek's own machine-written reports —
see :func:`creek.vault.links.iter_link_sources` for the three withheld
directories and the argument for each.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # used at runtime to render sources vault-relative
from typing import TYPE_CHECKING

from creek.clean.hygiene import BrokenLinkScanner
from creek.lint._result import CheckResult

if TYPE_CHECKING:
    from creek.vault.links import LinkIndex


def run(
    vault_path: Path,
    *,
    since: datetime | None = None,
    link_index: LinkIndex | None = None,
) -> CheckResult:
    """Scan the surveyed vault for broken wiki-links / relative markdown links.

    Wraps :class:`creek.clean.hygiene.BrokenLinkScanner`, whose survey is
    :func:`creek.vault.links.iter_link_sources` — every ``*.md`` in the vault
    except Creek's own report folders, which quote findings back and would
    inflate the count on each successive run (#1344).

    The ``since`` parameter is accepted for interface consistency but
    ignored — the scanner is already fast enough that filtering by mtime adds
    little.

    ``link_index`` is the shared index :class:`creek.lint.runner.LintRunner`
    builds once per run and hands to every index-aware check (#1223). It is
    optional, and ``None`` means "build your own" rather than "skip the
    check", so every standalone caller keeps working unchanged.

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

    ``relative_to`` cannot raise: every key the scanner emits comes from
    :func:`~creek.vault.links.iter_link_sources`, which enumerates
    ``vault_path.rglob("*.md")`` — the same lexical join off ``vault_path``
    used here.
    """
    del since  # interface symmetry only
    scan = BrokenLinkScanner().scan(vault_path, link_index=link_index)
    findings: list[str] = []
    for source, targets in sorted(scan.broken_links.items()):
        relative = Path(source).relative_to(vault_path)
        for target in targets:
            findings.append(f"- `{relative}` → `{target}`")
    summary = (
        f"{scan.total_broken} broken link(s) across {scan.total_files_scanned} file(s)"
    )
    return CheckResult(name="broken-links", summary=summary, findings=findings)
