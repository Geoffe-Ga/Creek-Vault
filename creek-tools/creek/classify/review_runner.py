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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

import frontmatter
import typer
import yaml

from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    CLASSIFIED_AT_KEY,
    MANUAL_METHOD,
)
from creek.classify.review import ReviewQueueGenerator
from creek.ingest.base import LA_TZ
from creek.models import Fragment, Frequency, FrequencyClassification
from creek.vault.reader import try_load_fragment

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)


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
        errors: Human-readable error messages collected when persisting
            an operator's decision failed (e.g. disk full, permission
            denied). The classify engine uses the same shape on
            ``ClassifySummary.errors`` for symmetry.
    """

    accepted: int = 0
    overridden: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)


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
            if entry.raw_metadata.get(CLASSIFICATION_METHOD_KEY) == MANUAL_METHOD:
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
                if self._save(entry, entry.fragment, summary):
                    summary.accepted += 1
            elif choice == "o":
                new_fragment = _override_frequency(entry, self.console)
                if new_fragment is None:
                    summary.deferred += 1
                    continue
                if self._save(entry, new_fragment, summary):
                    summary.overridden += 1
            else:
                summary.deferred += 1
        return summary

    def _save(
        self,
        entry: ReviewEntry,
        fragment: Fragment,
        summary: ReviewSummary,
    ) -> bool:
        """Persist an operator's decision, capturing any I/O failure.

        Without this guard a disk-full or permission-denied error mid
        ``creek review`` session would propagate as a raw traceback;
        we instead record the failure on ``summary.errors`` and
        surface it through the console so the operator can fix the
        underlying problem and resume.

        Args:
            entry: The queue entry being persisted.
            fragment: The updated fragment metadata to write.
            summary: Mutable summary to record any error onto.

        Returns:
            ``True`` when the write succeeded; ``False`` when an
            ``OSError`` was caught.
        """
        try:
            _persist_manual(entry, fragment)
        except OSError as exc:
            message = f"failed to persist {entry.path}: {exc}"
            summary.errors.append(message)
            self.console.print(f"[red]{message}[/red]")
            return False
        return True


def _parse_frequency_input(value: str) -> Frequency | None:
    """Parse operator-typed frequency input case-insensitively.

    Accepts ``F1``..``F10`` (any casing) plus ``unclassified`` (any
    casing). Returns ``None`` for anything else so the caller can
    print a helpful error and defer the entry instead of silently
    falling through.

    Args:
        value: Raw response from the prompt.

    Returns:
        The matching :class:`Frequency` enum, or ``None`` on no match.
    """
    cleaned = value.strip().lower()
    for member in Frequency:
        if cleaned == member.value.lower():
            return member
    return None


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
    primary = _parse_frequency_input(response)
    if primary is None:
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
    metadata[CLASSIFICATION_METHOD_KEY] = MANUAL_METHOD
    metadata[CLASSIFIED_AT_KEY] = datetime.now(tz=LA_TZ).isoformat()

    post = frontmatter.Post(content=entry.body, **metadata)
    entry.path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _read_entry(md_file: Path) -> ReviewEntry | None:
    """Load *md_file* into a :class:`ReviewEntry`, returning ``None`` on error.

    Delegates the validation chain to
    :func:`creek.vault.reader.try_load_fragment` so the engine,
    review runner, and link engine share one definition of "is this
    a Creek fragment?" — a future schema change to :class:`Fragment`
    or rename of the ``type`` sentinel only needs to update the
    shared helper.

    Args:
        md_file: Path to a fragment file.

    Returns:
        Parsed entry or ``None`` if the file is not a fragment.
    """
    try:
        record = try_load_fragment(md_file)
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None
    if record is None:
        return None
    fragment, body, metadata = record
    return ReviewEntry(
        path=md_file,
        fragment=fragment,
        body=body,
        raw_metadata=metadata,
    )
