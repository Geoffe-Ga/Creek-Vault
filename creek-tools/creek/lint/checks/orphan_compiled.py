"""Deterministic check: compiled pages with zero inbound wiki-links.

Orphan *fragments* are normal — only orphan *compiled* pages (Threads,
Eddies, Praxis, Frequency-indexes) are surfaced, per ADOPT-002 and FEAT-008.
The check emits suggestions only; it never deletes anything.

**Candidates.** A ``*.md`` file under :data:`_COMPILED_DIRS` whose declared
frontmatter ``type`` is *not* in
:data:`~creek.generate.indexes.GENERATED_INDEX_TYPES`. Those excluded types
name Dataview index notes Creek itself writes; nothing is ever expected to
link them, so reporting one asks the operator to review a page Creek created
moments earlier. Everything else stays a candidate — a page with no ``type``
key, an unknown ``type``, no frontmatter at all, or a header YAML cannot
parse. The exclusion defaults to *included* on purpose: were the default the
other way, any page could exempt itself from the check by writing a broken
header, and a check that falls silent is the same end state as the bug it
replaces.

**Inbound sources.** :func:`~creek.vault.links.iter_link_sources` — the whole
vault minus Creek's own report folders — rather than a hard-coded fragment
list. Each target is resolved through
:func:`~creek.vault.links.build_link_index`, so a page is credited for links
naming it by filename stem, ``title``, or any ``aliases`` entry. Comparing
stems alone (the behaviour before #887) reported
``02-Threads/Dormant/2020-09-26-Messages.md`` as orphaned despite roughly
30,000 inbound ``[[Messages]]`` links.

**The #1344 measurement.** On a vault carrying ``generate_all()``, one praxis
page, and an ``08-Decisions`` brief linking it, the check reported ``13 orphan
compiled page(s)`` — all thirteen false. Twelve needed the generated-index
exclusion; the thirteenth needed the widened survey, because ``08-Decisions``
was simply never read. The two remedies fix disjoint subsets, which is why
both ship: either alone leaves that vault dirty.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit
from typing import TYPE_CHECKING

from creek.generate.indexes import GENERATED_INDEX_TYPES
from creek.lint._result import CheckResult
from creek.vault.links import build_link_index, iter_link_sources, read_header_meta

if TYPE_CHECKING:
    from creek.vault.links import LinkIndex

_COMPILED_DIRS: tuple[str, ...] = (
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "06-Frequencies",
)
"""Directories whose markdown files count as compiled-layer pages."""

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[#|][^\]]*?)?\]\]")


def _stems_in(vault_path: Path, subdirs: tuple[str, ...]) -> list[Path]:
    """Collect every ``*.md`` path under any of *subdirs*."""
    paths: list[Path] = []
    for sub in subdirs:
        root = vault_path / sub
        if root.is_dir():
            paths.extend(root.rglob("*.md"))
    return paths


def _is_generated_index(path: Path) -> bool:
    """Return whether *path* declares a type Creek's index generators write.

    Read header-only: a compiled layer can be large, and the body says
    nothing about what generated the page.

    Args:
        path: Compiled-layer markdown file.

    Returns:
        True only for an explicitly declared generated-index ``type``. Every
        other outcome — missing key, unknown value, unparseable header —
        returns False and keeps the page a candidate.
    """
    return read_header_meta(path).get("type") in GENERATED_INDEX_TYPES


def _candidate_pages(vault_path: Path) -> list[Path]:
    """Return the compiled-layer pages an orphan verdict may be passed on.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        Sorted compiled-layer paths, minus Creek's own generated index notes.
    """
    return [
        page
        for page in sorted(_stems_in(vault_path, _COMPILED_DIRS))
        if not _is_generated_index(page)
    ]


def _credited_pages(source: Path, link_index: LinkIndex) -> set[Path]:
    """Resolve *source*'s outbound wiki-links to the pages they credit.

    A link resolving back to *source* itself is dropped. That guard is new
    with #1344 and newly necessary: while the source set was fragments-only a
    compiled page could never credit itself, but a survey that reads the
    compiled layer would otherwise let any self-referencing page exempt
    itself from the check.

    Args:
        source: Markdown file whose outbound links are being read.
        link_index: Vault-wide name → page index.

    Returns:
        The set of pages *source* links to, excluding itself. An unreadable
        file credits nothing rather than aborting the walk.
    """
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {
        page
        for target in _WIKILINK_RE.findall(text)
        if (page := link_index.resolve(target)) is not None and page != source
    }


def _inbound_pages(sources: list[Path], link_index: LinkIndex) -> set[Path]:
    """Return every vault page credited with an inbound link by *sources*.

    Both *sources* and the index's resolved targets are built by ``rglob``
    from the same vault root, so the two are the same lexical form and the
    self-link comparison in :func:`_credited_pages` is sound without calling
    ``Path.resolve`` on either side.

    Args:
        sources: Files whose outbound links count, per
            :func:`~creek.vault.links.iter_link_sources`.
        link_index: Vault-wide name → page index.

    Returns:
        The set of linked-to pages.
    """
    credited: set[Path] = set()
    for source in sources:
        credited.update(_credited_pages(source, link_index))
    return credited


def run(
    vault_path: Path,
    *,
    since: datetime | None = None,
    link_index: LinkIndex | None = None,
) -> CheckResult:
    """Find compiled-layer pages that nothing in the vault links to.

    A page is reported when it is a candidate — under a compiled directory
    and not one of Creek's own generated index notes — and no *other* page in
    the surveyed source set resolves a wiki-link to it.

    Resolution is per-page: a link that resolves *somewhere* credits only that
    page, never the whole compiled layer — otherwise the check would fall
    silent, which is the same end state as the bug it fixes.

    Args:
        vault_path: Root of the Obsidian vault.
        since: Accepted for interface symmetry with the other checks and
            ignored — the orphan check is cheap.
        link_index: The shared index :class:`creek.lint.runner.LintRunner`
            builds once per run and hands to every index-aware check
            (#1223). ``None`` means "build your own" rather than "skip the
            check", so every standalone caller keeps working unchanged.

    Returns:
        A :class:`~creek.lint._result.CheckResult` naming each orphan page
        vault-relative, in sorted path order.
    """
    del since
    index = link_index if link_index is not None else build_link_index(vault_path)
    linked_pages = _inbound_pages(iter_link_sources(vault_path), index)

    findings = [
        f"- `{page.relative_to(vault_path)}` (suggestion: review; never auto-deleted)"
        for page in _candidate_pages(vault_path)
        if page not in linked_pages
    ]
    summary = f"{len(findings)} orphan compiled page(s)"
    return CheckResult(name="orphan-compiled", summary=summary, findings=findings)
