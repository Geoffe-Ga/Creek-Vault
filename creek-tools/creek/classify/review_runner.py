"""Interactive review queue runner for the ``creek review`` command.

Walks the vault for fragments that need human review (per
:class:`ReviewQueueGenerator.needs_review`), prompts for an
``accept / override / defer / quit`` decision, and writes the
operator's choice back to the fragment file as
``classification_method: manual`` plus an ``classified_at`` timestamp.

A subsequent ``creek classify`` pass preserves manual decisions unless
``--force`` is supplied.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path  # noqa: TC003 — runtime use in dataclass field
from typing import TYPE_CHECKING, Final

import frontmatter
import typer
import yaml
from pydantic import ValidationError

from creek.classify.review import ReviewQueueGenerator
from creek.ingest.base import LA_TZ
from creek.models import Fragment, Frequency, FrequencyClassification

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

_CLASSIFICATION_METHOD_KEY: Final[str] = "classification_method"
_CLASSIFIED_AT_KEY: Final[str] = "classified_at"


@dataclass(frozen=True)
class ReviewEntry:
    """A single fragment surfaced for human review.

    Attributes:
        path: Path of the fragment file.
        fragment: Parsed :class:`Fragment` metadata.
        body: Markdown body retained when rewriting the file.
        raw_metadata: Original frontmatter dict (any non-Fragment keys
            are preserved on rewrite).
    """

    path: Path
    fragment: Fragment
    body: str
    raw_metadata: dict[str, object]


@dataclass
class ReviewSummary:
    """Counts produced by an interactive review session.

    Attributes:
        accepted: Fragments accepted as-is and stamped manual.
        overridden: Fragments whose primary frequency was overridden.
        deferred: Fragments left unchanged for a later session.
    """

    accepted: int = 0
    overridden: int = 0
    deferred: int = 0


def format_review_summary(entry: ReviewEntry) -> str:
    """Render a one-line summary for a queue entry.

    Args:
        entry: Queue entry to render.

    Returns:
        Human-readable summary line.
    """
    fragment = entry.fragment
    return (
        f"- {fragment.title} ({fragment.id}) "
        f"freq={fragment.frequency.primary} phase={fragment.wavelength.phase}"
    )


class ReviewQueueRunner:
    """Walk pending fragments and persist operator decisions.

    Args:
        vault_path: Vault root.
        console: Rich console used for prompts and status output.
    """

    def __init__(self, vault_path: Path, console: Console) -> None:
        """Initialise the runner.

        Args:
            vault_path: Vault root.
            console: Rich console sink.
        """
        self.vault_path = vault_path
        self.console = console
        self._generator = ReviewQueueGenerator()

    def list_pending(self) -> list[ReviewEntry]:
        """Return every fragment that currently needs review.

        Already-resolved fragments (``classification_method: manual``)
        are excluded so the queue empties as the operator works.

        Returns:
            List of pending :class:`ReviewEntry` records.
        """
        fragments_root = self.vault_path / "01-Fragments"
        if not fragments_root.exists():
            return []

        entries: list[ReviewEntry] = []
        for md_file in sorted(fragments_root.rglob("*.md")):
            entry = _read_entry(md_file)
            if entry is None:
                continue
            if entry.raw_metadata.get(_CLASSIFICATION_METHOD_KEY) == "manual":
                continue
            if not self._generator.needs_review(entry.fragment):
                continue
            entries.append(entry)
        return entries

    def run_interactive(self, entries: list[ReviewEntry]) -> ReviewSummary:
        """Prompt the operator for a decision on each entry.

        Args:
            entries: Pending entries from :meth:`list_pending`.

        Returns:
            A :class:`ReviewSummary` with per-decision counts.
        """
        summary = ReviewSummary()
        for index, entry in enumerate(entries, start=1):
            self.console.print(
                f"\n[bold]({index}/{len(entries)})[/bold] "
                f"{format_review_summary(entry)}",
            )
            choice = (
                typer.prompt(
                    "[a]ccept / [o]verride / [d]efer / [q]uit",
                    default="d",
                )
                .strip()
                .lower()
            )

            if choice == "q":
                self.console.print("[yellow]Exiting review.[/yellow]")
                break
            if choice == "a":
                _persist_manual(entry, entry.fragment)
                summary.accepted += 1
            elif choice == "o":
                new_fragment = _override_frequency(entry, self.console)
                if new_fragment is None:
                    summary.deferred += 1
                    continue
                _persist_manual(entry, new_fragment)
                summary.overridden += 1
            else:
                summary.deferred += 1
        return summary


def _override_frequency(
    entry: ReviewEntry,
    console: Console,
) -> Fragment | None:
    """Prompt for a new primary frequency and return an updated fragment.

    Args:
        entry: Entry being overridden.
        console: Rich console sink.

    Returns:
        The updated fragment, or ``None`` when the operator's input
        does not match a known frequency.
    """
    response = typer.prompt(
        "New primary frequency (F1..F10 / unclassified)",
        default=str(entry.fragment.frequency.primary),
    ).strip()
    try:
        primary = Frequency(response.upper() if response.startswith("f") else response)
    except ValueError:
        console.print(f"[red]Unknown frequency {response!r}; skipping.[/red]")
        return None

    return entry.fragment.model_copy(
        update={
            "frequency": FrequencyClassification(
                primary=primary,
                secondary=list(entry.fragment.frequency.secondary),
            ),
        },
    )


def _persist_manual(entry: ReviewEntry, fragment: Fragment) -> None:
    """Write the operator's decision back to disk.

    Args:
        entry: Original entry (used for path and body).
        fragment: Updated fragment metadata to persist.
    """
    metadata = dict(entry.raw_metadata)
    metadata.update(fragment.model_dump(mode="json"))
    metadata[_CLASSIFICATION_METHOD_KEY] = "manual"
    metadata[_CLASSIFIED_AT_KEY] = datetime.now(tz=LA_TZ).isoformat()

    post = frontmatter.Post(content=entry.body, **metadata)
    entry.path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _read_entry(md_file: Path) -> ReviewEntry | None:
    """Load *md_file* into a :class:`ReviewEntry`, returning ``None`` on error.

    Args:
        md_file: Path to a fragment file.

    Returns:
        Parsed entry or ``None`` if the file is not a fragment.
    """
    try:
        post = frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None
    metadata = dict(post.metadata)
    if metadata.get("type") != "fragment":
        return None
    try:
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return ReviewEntry(
        path=md_file,
        fragment=fragment,
        body=str(post.content),
        raw_metadata=metadata,
    )
