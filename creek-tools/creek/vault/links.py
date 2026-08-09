"""Wiki-link resolution for the vault — one definition, shared by every check.

A vault page is linkable by more than its filename. Since #730 the eddy and
thread linkers write date-prefixed files (``2020-09-26-Messages.md``) and put
the human-readable name in ``aliases:``; fragments then link the alias form,
``[[Messages]]``. Two lint checks nevertheless resolved links against filename
stems alone — ``BrokenLinkScanner`` in :mod:`creek.clean.hygiene` and the
orphan-compiled check — each with its own copy of that answer.

The result (issue #887, demo vault, 2026-07-22) was a lint report that was
almost entirely wrong: ``broken-links`` flagged 66,380 links of which 65,879
(99.2%) resolved through some page's ``aliases`` or ``title``, and
``orphan-compiled`` flagged a thread page carrying roughly 30,000 inbound
``[[Messages]]`` links. A check wrong 99.2% of the time is worse than one
that does not exist, because its output still has to be read.

This module is the single answer to "does this wiki-link resolve, and to
what". Callers build one :class:`LinkIndex` per run and query it.

``OrphanScanner`` — ``creek clean``'s own orphan check, distinct from the
``orphan-compiled`` lint check — was the last stem-based holdout and adopted
this index in #1225.

Resolution follows Obsidian: an exact-case match wins, and a case-insensitive
match is the fallback. The frontmatter is read **header-only** — a 35k-file
vault must never pay to load every body into memory just to learn a page's
aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_FENCE: str = "---"
"""Delimiter opening and closing a YAML frontmatter block."""

_MAX_HEADER_LINES: int = 200
"""Give up on a header longer than this rather than scan a whole file.

A frontmatter block this long is malformed — almost certainly an unclosed
fence — and following it would read the entire body, which is the cost this
module exists to avoid.
"""

_MAX_HEADER_BYTES: int = 64 * 1024
"""Byte ceiling on a single header, for the same reason as the line cap."""


@dataclass(frozen=True)
class LinkIndex:
    """Every name a vault page can be linked by, mapped to that page.

    Attributes:
        by_name: Exact-case name → page path.
        by_folded: Case-folded name → page path, the Obsidian-style fallback
            consulted only when no exact-case entry matches.
    """

    by_name: dict[str, Path]
    by_folded: dict[str, Path]

    def resolve(self, target: str) -> Path | None:
        """Return the page *target* names, or ``None`` if nothing matches.

        Args:
            target: A wiki-link target, with any ``#heading`` or ``|display``
                suffix already stripped by the caller's link regex.

        Returns:
            The resolved page path, or ``None`` when the link is genuinely
            dangling.
        """
        name = target.strip()
        exact = self.by_name.get(name)
        if exact is not None:
            return exact
        return self.by_folded.get(name.casefold())

    def __contains__(self, target: object) -> bool:
        """Return whether *target* is a string naming some page in the vault."""
        return isinstance(target, str) and self.resolve(target) is not None


def _read_header_block(path: Path) -> str | None:
    """Return the raw YAML frontmatter text of *path*, or ``None``.

    Reads line by line and stops at the closing fence, so the body is never
    pulled into memory. Returns ``None`` for a file with no frontmatter, an
    unterminated header, or one exceeding the size caps.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            if handle.readline().strip() != _FENCE:
                return None
            lines: list[str] = []
            size = 0
            for _ in range(_MAX_HEADER_LINES):
                line = handle.readline()
                if not line:
                    return None
                if line.strip() == _FENCE:
                    return "".join(lines)
                size += len(line)
                if size > _MAX_HEADER_BYTES:
                    return None
                lines.append(line)
    except OSError:
        return None
    return None


def _declared_names(meta: dict[str, object]) -> list[str]:
    """Extract the linkable names a frontmatter mapping declares.

    ``title`` contributes one name; ``aliases`` contributes each entry, and
    is accepted as either a scalar or a list because Obsidian permits both.
    Non-string and blank entries are ignored rather than stringified — an
    accidental ``aliases: [null]`` should not create a page named ``None``.
    """
    names: list[str] = []
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        names.append(title.strip())
    raw = meta.get("aliases")
    entries = [raw] if isinstance(raw, str) else raw
    if isinstance(entries, list):
        names.extend(
            entry.strip()
            for entry in entries
            if isinstance(entry, str) and entry.strip()
        )
    return names


def _header_names(path: Path) -> list[str]:
    """Return the ``title`` and ``aliases`` names declared by *path*.

    Any failure — unreadable file, malformed YAML, a header that is not a
    mapping — yields no names rather than raising. Lint walks every file in
    the vault, so one bad header must not cost the whole run its index.
    """
    block = _read_header_block(path)
    if block is None:
        return []
    try:
        meta = yaml.safe_load(block)
    except yaml.YAMLError:
        return []
    if not isinstance(meta, dict):
        return []
    return _declared_names(meta)


def build_link_index(vault_path: Path) -> LinkIndex:
    """Build the name → page index for every markdown file under *vault_path*.

    Each page is registered under its filename stem plus every name its
    frontmatter declares. Ties go to the first page in sorted path order, so
    the index is deterministic across runs regardless of filesystem ordering.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        A :class:`LinkIndex` covering the whole vault.
    """
    by_name: dict[str, Path] = {}
    by_folded: dict[str, Path] = {}
    for md_file in sorted(vault_path.rglob("*.md")):
        for name in (md_file.stem, *_header_names(md_file)):
            by_name.setdefault(name, md_file)
            by_folded.setdefault(name.casefold(), md_file)
    return LinkIndex(by_name=by_name, by_folded=by_folded)
