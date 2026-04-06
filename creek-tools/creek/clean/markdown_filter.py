"""Markdown pre-ingestion filter — skip empty/stub files, detect template residue.

Evaluates markdown content before ingestion and returns a structured
:class:`MarkdownFilterResult` recommending ``accept``, ``review``, or
``skip``.  Filter rules include:

- **Empty/stub detection**: Skip files with no body or body below a
  configurable minimum length (default 10 characters).
- **Frontmatter-only detection**: Skip files that consist exclusively
  of YAML frontmatter with no markdown body.
- **Template residue detection**: Flag files containing unfilled template
  markers (``{{...}}``, ``[FILL IN]``, ``TBD``, ``TODO``) for review.
- **Broken wiki-link detection**: Log ``[[wiki-links]]`` pointing to
  nonexistent files as warnings (flag only, never skip).

Exports:
    MarkdownFilter: Pre-ingestion filter for markdown files.
    MarkdownFilterResult: Structured result from filtering.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_FRONTMATTER_PATTERN: re.Pattern[str] = re.compile(
    r"\A---\s*\n.*?\n---\s*\n?",
    re.DOTALL,
)
"""Matches YAML frontmatter delimited by ``---``."""

_TEMPLATE_DOUBLE_BRACE: re.Pattern[str] = re.compile(r"\{\{.+?\}\}")
"""Matches ``{{...}}`` template placeholders."""

_TEMPLATE_FILL_IN: re.Pattern[str] = re.compile(r"\[FILL\s+IN\]", re.IGNORECASE)
"""Matches ``[FILL IN]`` markers (case-insensitive)."""

_TEMPLATE_TBD: re.Pattern[str] = re.compile(r"\bTBD\b", re.IGNORECASE)
"""Matches standalone ``TBD`` markers (case-insensitive)."""

_TEMPLATE_TODO: re.Pattern[str] = re.compile(r"\bTODO\b", re.IGNORECASE)
"""Matches standalone ``TODO`` markers (case-insensitive)."""

_WIKI_LINK_PATTERN: re.Pattern[str] = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
"""Matches ``[[link-target]]`` and ``[[target|alias]]`` wiki-links."""

_TEMPLATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("{{...}} placeholder", _TEMPLATE_DOUBLE_BRACE),
    ("[FILL IN] marker", _TEMPLATE_FILL_IN),
    ("TBD marker", _TEMPLATE_TBD),
    ("TODO marker", _TEMPLATE_TODO),
]
"""Named template-residue patterns for detection."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class MarkdownFilterResult(BaseModel):
    """Structured result from markdown pre-ingestion filtering.

    Attributes:
        action: Recommended action: ``accept``, ``review``, or ``skip``.
        reasons: Human-readable explanations for skip/review decisions.
        warnings: Non-blocking issues (e.g. broken links).
    """

    action: Literal["accept", "review", "skip"]
    reasons: list[str]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class MarkdownFilter:
    """Pre-ingestion filter for markdown files.

    Evaluates raw markdown text against configurable rules and returns
    a :class:`MarkdownFilterResult` with an action recommendation.

    Attributes:
        min_body_length: Minimum character count for the body after
            frontmatter to be considered non-stub (default 10).
    """

    def __init__(self, *, min_body_length: int = 10) -> None:
        """Initialise the filter with configurable thresholds.

        Args:
            min_body_length: Minimum body character count.  Files with
                body text shorter than this are skipped as stubs.
        """
        self.min_body_length = min_body_length

    def filter(
        self,
        content: str,
        *,
        vault_path: Path | None = None,
    ) -> MarkdownFilterResult:
        """Evaluate markdown content against all filter rules.

        Rules are applied in priority order:

        1. Empty / whitespace-only → ``skip``
        2. Frontmatter-only (no body) → ``skip``
        3. Body below minimum length → ``skip``
        4. Template residue detected → ``review``
        5. Otherwise → ``accept``

        Broken wiki-link checks run independently and populate
        ``warnings`` without affecting the action.

        Args:
            content: Raw markdown text (may include YAML frontmatter).
            vault_path: Optional vault root for resolving wiki-links.
                If ``None``, broken-link detection is skipped.

        Returns:
            A :class:`MarkdownFilterResult` with action, reasons,
            and warnings.
        """
        reasons: list[str] = []
        warnings: list[str] = []

        body = self._extract_body(content)

        # --- Skip checks (highest priority) ---
        skip_reason = self._check_empty_or_stub(body, content)
        if skip_reason is not None:
            return MarkdownFilterResult(
                action="skip",
                reasons=[skip_reason],
                warnings=[],
            )

        # --- Review checks ---
        template_reasons = self._check_template_residue(body)
        reasons.extend(template_reasons)

        # --- Warning checks (never affect action) ---
        if vault_path is not None:
            link_warnings = self._check_broken_links(body, vault_path)
            warnings.extend(link_warnings)

        action: Literal["accept", "review", "skip"] = "review" if reasons else "accept"

        return MarkdownFilterResult(
            action=action,
            reasons=reasons,
            warnings=warnings,
        )

    # ---- Private helpers ----

    def _extract_body(self, content: str) -> str:
        """Strip YAML frontmatter and return only the body text.

        Args:
            content: Raw markdown text.

        Returns:
            The markdown body after frontmatter removal.
        """
        return _FRONTMATTER_PATTERN.sub("", content)

    def _check_empty_or_stub(self, body: str, raw: str) -> str | None:
        """Check for empty, whitespace-only, or stub body.

        Args:
            body: Extracted body text (frontmatter removed).
            raw: Original raw content (for frontmatter detection).

        Returns:
            A reason string if the file should be skipped, or ``None``.
        """
        stripped = body.strip()

        if not raw.strip():
            return "Content is empty or whitespace-only"

        if not stripped:
            has_frontmatter = _FRONTMATTER_PATTERN.match(raw) is not None
            if has_frontmatter:
                return "File contains only frontmatter with no body"
            return "Content is empty or whitespace-only"

        if len(stripped) < self.min_body_length:
            return (
                f"Body too short: {len(stripped)} chars "
                f"(minimum {self.min_body_length})"
            )

        return None

    def _check_template_residue(self, body: str) -> list[str]:
        """Detect unfilled template markers in the body.

        Args:
            body: Extracted body text.

        Returns:
            A list of reasons describing detected template residue.
        """
        reasons: list[str] = []
        for label, pattern in _TEMPLATE_PATTERNS:
            if pattern.search(body):
                reasons.append(f"Template residue detected: {label}")
        return reasons

    def _check_broken_links(
        self,
        body: str,
        vault_path: Path,
    ) -> list[str]:
        """Detect wiki-links that point to nonexistent vault files.

        A wiki-link ``[[target]]`` is resolved by checking for
        ``target.md`` anywhere under *vault_path*.  Only broken
        links are reported; valid links are silently accepted.

        Args:
            body: Extracted body text.
            vault_path: Root of the Obsidian vault for file lookup.

        Returns:
            A list of warning strings for broken links.
        """
        warnings: list[str] = []
        matches = _WIKI_LINK_PATTERN.findall(body)

        for target in matches:
            target_clean = target.strip()
            if not self._wiki_link_exists(target_clean, vault_path):
                warnings.append(f"Broken wiki-link: [[{target_clean}]]")

        return warnings

    def _wiki_link_exists(self, target: str, vault_path: Path) -> bool:
        """Check whether a wiki-link target resolves to an existing file.

        Searches for ``target.md`` anywhere under *vault_path* using
        recursive globbing.

        Args:
            target: The wiki-link target (without ``[[]]``).
            vault_path: Root of the vault to search.

        Returns:
            ``True`` if a matching ``.md`` file exists, ``False``
            otherwise.
        """
        return any(vault_path.rglob(f"{target}.md"))
