"""Markdown pre-ingestion filter -- skip empty/stub files, detect template residue.

Provides :class:`MarkdownFilter` which inspects raw markdown file content
and decides whether the file should be kept for ingestion or skipped.

Filter rules:

- **Empty/stub files**: skip files with no body content after frontmatter,
  or body shorter than a configurable minimum (default 10 characters).
- **Frontmatter-only files**: skip files that are exclusively YAML
  frontmatter with no markdown body.
- **Template residue detection**: flag files containing unfilled template
  markers (``{{...}}``, ``[FILL IN]``, ``[TBD]``, ``TODO:``, ``FIXME:``).
- **Broken internal links**: detect and log ``[[wiki-links]]`` pointing to
  nonexistent files (flag only, do not skip).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from creek.clean.filters._result import FilterResult
from creek.vault.links import build_link_index, extract_wikilinks

if TYPE_CHECKING:
    from pathlib import Path

    from creek.vault.links import LinkIndex

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_FRONTMATTER_PATTERN: re.Pattern[str] = re.compile(
    r"\A---\s*\n(.*?\n)---[ \t]*\n?",
    re.DOTALL,
)
"""Matches YAML frontmatter delimited by ``---`` at the start of the file."""

_TEMPLATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("{{...}}", re.compile(r"\{\{.+?\}\}")),
    ("[FILL IN]", re.compile(r"\[FILL IN\]", re.IGNORECASE)),
    ("[TBD]", re.compile(r"\[TBD\]", re.IGNORECASE)),
    ("TODO:", re.compile(r"TODO:")),
    ("FIXME:", re.compile(r"FIXME:")),
]
"""Template residue patterns with human-readable labels."""


# ---------------------------------------------------------------------------
# MarkdownFilter
# ---------------------------------------------------------------------------


class MarkdownFilter:
    """Filter markdown files before ingestion based on content quality rules.

    Inspects raw file content for empty/stub bodies, template residue,
    frontmatter-only files, and broken internal wiki-links.

    Attributes:
        min_body_length: Minimum character count for the body after
            frontmatter to be considered non-stub.
    """

    def __init__(self, *, min_body_length: int = 10) -> None:
        """Initialise the filter with configurable thresholds.

        Args:
            min_body_length: Minimum character count for the markdown
                body (after frontmatter) to pass the stub check.
        """
        self.min_body_length = min_body_length
        self._indexed_vault: Path | None = None
        self._link_index: LinkIndex | None = None

    def _index_for(self, vault_path: Path) -> LinkIndex:
        """Return the link index for *vault_path*, building it at most once.

        The previous implementation ran one ``rglob`` per wiki-link, so a run
        over a large vault re-walked the tree for every link in every file.
        The index is built once per filter instance per vault instead, which
        is what makes an ingest run pay for the walk a single time.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            The cached :class:`~creek.vault.links.LinkIndex`.
        """
        resolved = vault_path.resolve()
        if self._link_index is None or self._indexed_vault != resolved:
            self._link_index = build_link_index(resolved)
            self._indexed_vault = resolved
        return self._link_index

    def filter(
        self,
        content: str,
        *,
        vault_path: Path | None = None,
    ) -> FilterResult:
        """Apply all filter rules to raw markdown file content.

        Evaluates the file against each rule in sequence:

        1. Separate frontmatter from body.
        2. Check whether the body is empty or below the minimum length.
        3. Collect non-blocking warnings (template residue, broken links).

        Args:
            content: The full raw content of the markdown file.
            vault_path: Optional path to the Obsidian vault root for
                checking wiki-link targets.

        Returns:
            A :class:`FilterResult` indicating whether to keep the file.
        """
        stripped_body = self._extract_body(content).strip()

        # --- Gate: empty / stub body ---
        if len(stripped_body) < self.min_body_length:
            reason = self._build_skip_reason(stripped_body)
            return FilterResult(keep=False, reason=reason)

        # --- Non-blocking warnings ---
        warnings: list[str] = []
        warnings.extend(self._detect_template_residue(stripped_body))
        if vault_path is not None:
            warnings.extend(self._detect_broken_links(stripped_body, vault_path))

        return FilterResult(keep=True, warnings=warnings)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_body(self, content: str) -> str:
        """Separate YAML frontmatter from the markdown body.

        If the content starts with a valid ``---`` delimited frontmatter
        block, returns everything after the closing ``---``.  Otherwise
        returns the full content as the body.

        Args:
            content: Full raw file content.

        Returns:
            The markdown body after frontmatter (or the entire content).
        """
        match = _FRONTMATTER_PATTERN.match(content)
        if match:
            return content[match.end() :]
        return content

    def _build_skip_reason(self, stripped_body: str) -> str:
        """Build a human-readable reason for skipping the file.

        Args:
            stripped_body: The body text after stripping whitespace.

        Returns:
            A reason string describing why the file was skipped.
        """
        if not stripped_body:
            return "Empty body after frontmatter"
        return (
            f"Body too short: {len(stripped_body)} chars "
            f"(minimum {self.min_body_length})"
        )

    def _detect_template_residue(self, body: str) -> list[str]:
        """Detect unfilled template markers in the body.

        Args:
            body: The stripped markdown body text.

        Returns:
            A list of warning strings for each detected marker type.
        """
        warnings: list[str] = []
        found_labels: list[str] = [
            label for label, pattern in _TEMPLATE_PATTERNS if pattern.search(body)
        ]
        if found_labels:
            markers = ", ".join(found_labels)
            warnings.append(f"Template residue detected: {markers}")
        return warnings

    def _detect_broken_links(self, body: str, vault_path: Path) -> list[str]:
        """Detect wiki-links that resolve to no page in the vault.

        Resolution is delegated to
        :func:`~creek.vault.links.build_link_index`, the same primitive
        ``hygiene.py`` uses. This module used to carry its own stem-matching
        copy, which re-created all three defects #835/#887/#1225 had already
        removed from that one: a same-file anchor was treated as a target, a
        page reachable only by its ``title`` or an ``aliases`` entry looked
        dangling, and case-folded matches were missed (#1518).

        Args:
            body: The stripped markdown body text.
            vault_path: Root path of the Obsidian vault.

        Returns:
            A list of warning strings for each broken link.
        """
        index = self._index_for(vault_path)
        return [
            f"Broken wiki-link: [[{target}]]"
            for target in extract_wikilinks(body)
            if index.resolve(target) is None
        ]
