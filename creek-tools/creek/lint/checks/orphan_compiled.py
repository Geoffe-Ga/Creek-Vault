"""Deterministic check: compiled pages with zero inbound wiki-links.

Orphan *fragments* are normal — only orphan *compiled* pages
(Threads, Eddies, Praxis, Frequency-indexes) are surfaced, per
ADOPT-002 and FEAT-008. The check emits suggestions only; it never
deletes anything.

Inbound links are counted through :func:`creek.vault.links.build_link_index`,
so a page is credited for links naming it by any of its names. Comparing
filename stems alone (the behaviour before #887) reported
``02-Threads/Dormant/2020-09-26-Messages.md`` as orphaned despite roughly
30,000 inbound ``[[Messages]]`` links, because the human-readable name lives
in ``aliases`` while the filename carries a date prefix.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult
from creek.vault.links import build_link_index

_COMPILED_DIRS: tuple[str, ...] = (
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "06-Frequencies",
)
"""Directories whose markdown files count as compiled-layer pages."""

_FRAGMENT_DIRS: tuple[str, ...] = (
    "01-Fragments",
    "10-Liminal",
)
"""Directories whose markdown files may carry inbound links to compiled pages."""

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[#|][^\]]*?)?\]\]")


def _stems_in(vault_path: Path, subdirs: tuple[str, ...]) -> list[Path]:
    """Collect every ``*.md`` path under any of *subdirs*."""
    paths: list[Path] = []
    for sub in subdirs:
        root = vault_path / sub
        if root.is_dir():
            paths.extend(root.rglob("*.md"))
    return paths


def _inbound_targets(fragment_files: list[Path]) -> set[str]:
    """Extract the union of every wiki-link target name across *fragment_files*."""
    targets: set[str] = set()
    for path in fragment_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        targets.update(_WIKILINK_RE.findall(text))
    return targets


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Find compiled-layer pages that no fragment links to.

    The ``since`` parameter is accepted for interface symmetry but
    ignored — the orphan check is cheap.

    Every inbound wiki-link target is resolved to the page it actually names
    before the comparison, so a page counts as linked when a fragment
    references it by alias or title. Resolution is per-page: a link that
    resolves *somewhere* credits only that page, never the whole compiled
    layer — otherwise the check would fall silent, which is the same end
    state as the bug it fixes.
    """
    del since
    compiled_files = _stems_in(vault_path, _COMPILED_DIRS)
    fragment_files = _stems_in(vault_path, _FRAGMENT_DIRS)
    link_index = build_link_index(vault_path)
    linked_pages = {
        resolved
        for target in _inbound_targets(fragment_files)
        if (resolved := link_index.resolve(target)) is not None
    }

    findings: list[str] = []
    for compiled in sorted(compiled_files):
        if compiled not in linked_pages:
            rel = compiled.relative_to(vault_path)
            findings.append(f"- `{rel}` (suggestion: review; never auto-deleted)")
    summary = f"{len(findings)} orphan compiled page(s)"
    return CheckResult(name="orphan-compiled", summary=summary, findings=findings)
