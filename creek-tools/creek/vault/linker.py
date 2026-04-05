"""Vault linker — inject wiki-links into vault markdown files.

This module provides the ``VaultLinker`` class, which writes discovered
links (resonances, threads, eddies) into existing vault markdown files.
It updates both YAML frontmatter and the markdown body, ensuring
idempotency (no duplicates on re-runs) and supporting stale link cleanup.

Typical usage::

    linker = VaultLinker(vault_path=Path("/path/to/vault"))
    linker.link_fragment(
        file_path,
        resonances=["[[Related Note]]"],
        threads=["[[Thread Alpha]]"],
        eddies=["[[Eddy Cluster]]"],
    )
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import frontmatter
from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_WIKILINK_PATTERN = re.compile(r"^\[\[(.+?)(?:\|.+?)?\]\]$")
"""Regex to extract the note title from a ``[[title]]`` or ``[[title|alias]]``."""


class BatchLinkResult(BaseModel):
    """Result of a batch linking operation.

    Attributes:
        processed: Number of files successfully processed.
        skipped: Number of files skipped (e.g. missing).
    """

    processed: int
    skipped: int


class VaultLinker:
    """Inject wiki-links into existing vault markdown files.

    Reads vault fragment files (markdown with YAML frontmatter), adds
    resonance, thread, and eddy links, and writes them back.  All
    operations are idempotent — running the same operation twice will
    not produce duplicate entries.

    Args:
        vault_path: Path to the root of the Obsidian vault.

    Raises:
        FileNotFoundError: If ``vault_path`` does not exist or is missing
            required vault directories.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialise the VaultLinker and validate vault structure.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Raises:
            FileNotFoundError: If the vault path does not exist or
                required directories are missing.
        """
        if not vault_path.exists():
            msg = f"Vault path does not exist: {vault_path}"
            raise FileNotFoundError(msg)

        required_dirs = ["00-Creek-Meta", "01-Fragments"]
        for d in required_dirs:
            if not (vault_path / d).is_dir():
                msg = f"Required vault directory missing: {d}"
                raise FileNotFoundError(msg)

        self.vault_path = vault_path

    def add_resonances(
        self,
        file_path: Path,
        links: list[str],
    ) -> None:
        """Add resonance wiki-links to a fragment file.

        Updates both the YAML frontmatter ``resonances`` list and appends
        a ``## Resonances`` section to the markdown body.  Existing links
        are preserved; duplicates are skipped.

        Args:
            file_path: Path to the markdown file to update.
            links: List of wiki-link strings (e.g. ``["[[Note A]]"]``).

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        if not file_path.exists():
            msg = f"Fragment file not found: {file_path}"
            raise FileNotFoundError(msg)

        if not links:
            return

        post = frontmatter.load(str(file_path))

        # Update frontmatter
        existing: list[str] = list(post.metadata.get("resonances", []))
        existing_set = set(existing)
        for link in links:
            if link not in existing_set:
                existing.append(link)
                existing_set.add(link)
        post.metadata["resonances"] = existing

        # Update body — add or extend ## Resonances section
        body = post.content
        body = self._update_resonances_section(body, existing)
        post.content = body

        file_path.write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )

    def update_threads(
        self,
        file_path: Path,
        links: list[str],
    ) -> None:
        """Update thread wiki-links in a fragment file's frontmatter.

        Args:
            file_path: Path to the markdown file to update.
            links: List of thread wiki-link strings.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        self._update_frontmatter_list(file_path, "threads", links)

    def update_eddies(
        self,
        file_path: Path,
        links: list[str],
    ) -> None:
        """Update eddy wiki-links in a fragment file's frontmatter.

        Args:
            file_path: Path to the markdown file to update.
            links: List of eddy wiki-link strings.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        self._update_frontmatter_list(file_path, "eddies", links)

    def link_fragment(
        self,
        file_path: Path,
        *,
        resonances: list[str] | None = None,
        threads: list[str] | None = None,
        eddies: list[str] | None = None,
    ) -> None:
        """Update all link types for a single fragment file.

        Convenience method that calls ``add_resonances``,
        ``update_threads``, and ``update_eddies`` in sequence.

        Args:
            file_path: Path to the markdown file to update.
            resonances: Resonance wiki-link strings to add.
            threads: Thread wiki-link strings to add.
            eddies: Eddy wiki-link strings to add.
        """
        self.add_resonances(file_path, resonances or [])
        self.update_threads(file_path, threads or [])
        self.update_eddies(file_path, eddies or [])

    def batch_link(
        self,
        specs: dict[Path, dict[str, list[str]]],
    ) -> BatchLinkResult:
        """Process multiple fragment files with their link specifications.

        Each spec dict must contain keys ``resonances``, ``threads``,
        and ``eddies`` with list-of-string values.  Missing files are
        skipped and counted.

        Args:
            specs: Mapping of file paths to link specification dicts.

        Returns:
            A ``BatchLinkResult`` with processed and skipped counts.
        """
        processed = 0
        skipped = 0

        for file_path, spec in specs.items():
            if not file_path.exists():
                logger.warning("Skipping missing file: %s", file_path)
                skipped += 1
                continue

            self.link_fragment(
                file_path,
                resonances=spec.get("resonances", []),
                threads=spec.get("threads", []),
                eddies=spec.get("eddies", []),
            )
            processed += 1
            logger.info(
                "Linked fragment %d/%d: %s",
                processed,
                processed + skipped,
                file_path.name,
            )

        logger.info(
            "Batch linking complete: %d processed, %d skipped",
            processed,
            skipped,
        )
        return BatchLinkResult(processed=processed, skipped=skipped)

    def remove_stale_links(self, file_path: Path) -> int:
        """Remove wiki-links that point to non-existent vault notes.

        Checks ``resonances``, ``threads``, and ``eddies`` in the
        frontmatter.  A wiki-link ``[[Title]]`` is considered stale if
        no ``.md`` file matching that title exists anywhere in the vault.
        Non-wikilink entries (plain strings) are always preserved.

        Also updates the ``## Resonances`` body section to match.

        Args:
            file_path: Path to the markdown file to clean.

        Returns:
            Number of stale links removed.
        """
        post = frontmatter.load(str(file_path))
        removed = 0

        for key in ("resonances", "threads", "eddies"):
            entries: list[str] = list(post.metadata.get(key, []))
            kept: list[str] = []
            for entry in entries:
                match = _WIKILINK_PATTERN.match(entry)
                if match is None:
                    # Not a wikilink — always keep
                    kept.append(entry)
                elif self._note_exists(match.group(1)):
                    kept.append(entry)
                else:
                    removed += 1
            post.metadata[key] = kept

        # Rebuild body resonances section from cleaned frontmatter
        resonances: list[str] = post.metadata.get("resonances", [])
        post.content = self._update_resonances_section(
            post.content,
            resonances,
        )

        file_path.write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )
        return removed

    # ---- Private helpers ----

    def _update_frontmatter_list(
        self,
        file_path: Path,
        key: str,
        links: list[str],
    ) -> None:
        """Add links to a frontmatter list field without duplicates.

        Args:
            file_path: Path to the markdown file to update.
            key: The frontmatter key to update (e.g. ``threads``).
            links: List of link strings to add.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
        """
        if not file_path.exists():
            msg = f"Fragment file not found: {file_path}"
            raise FileNotFoundError(msg)

        if not links:
            return

        post = frontmatter.load(str(file_path))
        existing: list[str] = list(post.metadata.get(key, []))
        existing_set = set(existing)
        for link in links:
            if link not in existing_set:
                existing.append(link)
                existing_set.add(link)
        post.metadata[key] = existing

        file_path.write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )

    @staticmethod
    def _update_resonances_section(
        body: str,
        resonances: list[str],
    ) -> str:
        """Replace or create the ## Resonances section in the body.

        If ``resonances`` is empty, any existing section is removed.

        Args:
            body: The markdown body content.
            resonances: List of resonance link strings to render.

        Returns:
            The updated markdown body.
        """
        # Remove existing ## Resonances section (greedy up to next ## or end)
        pattern = re.compile(
            r"\n*## Resonances\n(?:- .+\n?)*",
            re.MULTILINE,
        )
        body = pattern.sub("", body)

        if not resonances:
            return body

        # Build new section
        lines = [f"- {link}" for link in resonances]
        section = "\n\n## Resonances\n" + "\n".join(lines) + "\n"
        return body.rstrip() + section

    def _note_exists(self, title: str) -> bool:
        """Check if a note with the given title exists in the vault.

        Searches all ``.md`` files in common vault directories for
        a filename matching the title.

        Args:
            title: The note title extracted from a wiki-link.

        Returns:
            True if a matching ``.md`` file is found.
        """
        search_dirs = [
            "01-Fragments",
            "02-Threads",
            "03-Eddies",
            "04-Praxis",
            "08-Decisions",
        ]
        for search_dir in search_dirs:
            dir_path = self.vault_path / search_dir
            if not dir_path.exists():
                continue
            for md_file in dir_path.rglob("*.md"):
                if md_file.stem == title:
                    return True
        return False
