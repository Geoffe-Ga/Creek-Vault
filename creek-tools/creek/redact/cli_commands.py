"""Command handlers for the ``creek redact`` CLI.

Provides the three user-facing redaction modes — :func:`run_scan`,
:func:`run_apply`, and :func:`run_review` — together with Rich-based
display helpers that render colorized summary tables, per-match
listings, and markdown review queues.

Keeping these helpers in a dedicated module isolates them from Typer
command registration and keeps :mod:`creek.cli` focused on routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import typer
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from creek.config import load_config
from creek.redact.patterns import PATTERN_METADATA
from creek.redact.redactor import Redactor
from creek.redact.scanner import RedactionScanner, ScanSummary

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rich.console import Console

    from creek.config import CreekConfig
    from creek.redact.scanner import RedactionMatch


# ---------------------------------------------------------------------------
# Severity styling
# ---------------------------------------------------------------------------

_SEVERITY_STYLES: dict[str, str] = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "unknown": "white",
}

_SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "unknown")


def _severity(match_type: str) -> str:
    """Look up the severity of a pattern by name.

    Args:
        match_type: Pattern name as recorded on a :class:`RedactionMatch`.

    Returns:
        The severity string, or ``"unknown"`` when the pattern has no
        metadata entry.
    """
    info = PATTERN_METADATA.get(match_type)
    return info.severity if info else "unknown"


def _style_for(severity: str) -> str:
    """Return the Rich style string for a given severity level.

    Args:
        severity: Severity string.

    Returns:
        A Rich style spec (falls back to ``"white"`` for unknown levels).
    """
    return _SEVERITY_STYLES.get(severity, "white")


def _count_by(
    matches: list[RedactionMatch],
    key_fn: Callable[[RedactionMatch], str],
) -> dict[str, int]:
    """Count matches by a derived key.

    Args:
        matches: Redaction matches to aggregate.
        key_fn: Function extracting the grouping key from a match.

    Returns:
        Mapping from key to count.
    """
    counts: dict[str, int] = {}
    for match in matches:
        key = key_fn(match)
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _render_stats_table(summary: ScanSummary, console: Console) -> None:
    """Render the headline statistics block.

    Args:
        summary: Scan summary to render.
        console: Rich console sink.
    """
    stats = Table(title="Redaction Scan Summary", show_header=False)
    stats.add_column("Metric", style="bold")
    stats.add_column("Value", justify="right")
    stats.add_row("Files scanned", str(summary.files_scanned))
    stats.add_row("Files skipped (binary)", str(summary.files_skipped_binary))
    stats.add_row("Files skipped (extension)", str(summary.files_skipped_extension))
    stats.add_row("Total findings", str(len(summary.matches)))
    console.print(stats)


def _render_severity_table(summary: ScanSummary, console: Console) -> None:
    """Render the severity breakdown table when findings exist.

    Args:
        summary: Scan summary to render.
        console: Rich console sink.
    """
    by_sev = _count_by(summary.matches, lambda m: _severity(m.match_type))
    table = Table(title="Findings by Severity")
    table.add_column("Severity", style="bold")
    table.add_column("Count", justify="right")
    for sev in _SEVERITY_ORDER:
        if sev in by_sev:
            table.add_row(Text(sev, style=_style_for(sev)), str(by_sev[sev]))
    console.print(table)


def _render_type_table(summary: ScanSummary, console: Console) -> None:
    """Render the match-type breakdown table when findings exist.

    Args:
        summary: Scan summary to render.
        console: Rich console sink.
    """
    by_type = _count_by(summary.matches, lambda m: m.match_type)
    table = Table(title="Findings by Type")
    table.add_column("Type", style="bold")
    table.add_column("Severity", style="bold")
    table.add_column("Count", justify="right")
    for mtype, count in sorted(by_type.items()):
        sev = _severity(mtype)
        table.add_row(mtype, Text(sev, style=_style_for(sev)), str(count))
    console.print(table)


def render_summary(summary: ScanSummary, console: Console) -> None:
    """Render the full summary: statistics plus severity and type tables.

    Args:
        summary: Scan summary to render.
        console: Rich console sink.
    """
    _render_stats_table(summary, console)
    if not summary.matches:
        return
    _render_severity_table(summary, console)
    _render_type_table(summary, console)


def render_matches(summary: ScanSummary, console: Console) -> None:
    """Render a colorized per-match table, sorted by file and line.

    Args:
        summary: Scan summary whose matches should be listed.
        console: Rich console sink.
    """
    if not summary.matches:
        return
    table = Table(title="Matches")
    table.add_column("File", style="dim")
    table.add_column("Line", justify="right")
    table.add_column("Type")
    table.add_column("Severity")
    sorted_matches = sorted(
        summary.matches,
        key=lambda m: (str(m.file_path), m.line_number),
    )
    for match in sorted_matches:
        sev = _severity(match.match_type)
        table.add_row(
            str(match.file_path),
            str(match.line_number),
            match.match_type,
            Text(sev, style=_style_for(sev)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------


def _scan_source(
    source: Path,
    config: CreekConfig,
) -> tuple[RedactionScanner, ScanSummary]:
    """Scan *source* (file or directory) and return the scanner + summary.

    Args:
        source: File or directory to scan.
        config: Loaded Creek configuration.

    Returns:
        Tuple of ``(scanner, summary)``. The scanner is returned so that
        the caller can reuse its session salt for redaction or review.
    """
    scanner = RedactionScanner(config=config.redaction)
    if source.is_file():
        matches = scanner.scan_file(source)
        summary = ScanSummary(matches=matches, files_scanned=1)
    else:
        summary = scanner.scan_batch(source, progress=True)
    return scanner, summary


def _error_exit(console: Console, message: str, *, code: int = 2) -> NoReturn:
    """Print a red error message and raise ``typer.Exit``.

    Args:
        console: Rich console sink.
        message: Human-readable error message.
        code: Process exit code (defaults to ``2``).

    Raises:
        typer.Exit: Always — this helper never returns.
    """
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=code)


def _require_existing(console: Console, path: Path, label: str) -> None:
    """Abort unless *path* exists on disk.

    Args:
        console: Rich console sink.
        path: Path to verify.
        label: Human-readable label used in the error message.

    Raises:
        typer.Exit: When *path* does not exist.
    """
    if not path.exists():
        _error_exit(console, f"{label} not found: {path}")


# ---------------------------------------------------------------------------
# Mode: scan
# ---------------------------------------------------------------------------


def run_scan(
    source: Path,
    *,
    report: bool,
    verbose: bool,
    console: Console,
) -> ScanSummary:
    """Scan *source* for sensitive data and render a report.

    Args:
        source: File or directory to scan.
        report: When ``True``, render the detailed markdown report.
        verbose: When ``True``, also list every individual match.
        console: Rich console sink.

    Returns:
        The :class:`ScanSummary` produced by the scanner.
    """
    _require_existing(console, source, "Source path")
    config = load_config()
    scanner, summary = _scan_source(source, config)
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)
    if report:
        console.print(Markdown(scanner.generate_markdown_summary(summary)))
    return summary


# ---------------------------------------------------------------------------
# Mode: apply
# ---------------------------------------------------------------------------


def _files_from_summary(summary: ScanSummary) -> list[Path]:
    """Return the unique file paths referenced by *summary*'s matches.

    Preserves first-seen order for stable reporting.

    Args:
        summary: Scan summary whose matches name the files to redact.

    Returns:
        Ordered list of unique file paths.
    """
    seen: set[Path] = set()
    ordered: list[Path] = []
    for match in summary.matches:
        if match.file_path not in seen:
            seen.add(match.file_path)
            ordered.append(match.file_path)
    return ordered


def _apply_redactions(
    redactor: Redactor,
    files: list[Path],
    console: Console,
) -> None:
    """Rewrite each file in place with sensitive data replaced.

    Args:
        redactor: Configured :class:`Redactor`.
        files: Files to rewrite.
        console: Rich console sink for the completion banner.
    """
    for file_path in files:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        redacted = redactor.redact_content(text)
        file_path.write_text(redacted, encoding="utf-8")
    console.print(f"[green]Applied redactions to {len(files)} file(s).[/green]")


def _confirm_apply(
    files: list[Path],
    *,
    assume_yes: bool,
    console: Console,
) -> bool:
    """Ask the user to confirm applying redactions.

    Args:
        files: Files that will be modified.
        assume_yes: Skip the prompt when ``True``.
        console: Rich console sink for the abort banner.

    Returns:
        ``True`` if the caller should proceed, ``False`` to abort.
    """
    if assume_yes:
        return True
    if typer.confirm(f"Apply redactions to {len(files)} file(s)?"):
        return True
    console.print("[yellow]Aborted — no files modified.[/yellow]")
    return False


def run_apply(
    source: Path,
    *,
    dry_run: bool,
    verbose: bool,
    assume_yes: bool,
    console: Console,
) -> None:
    """Scan *source*, obtain consent, and redact files in place.

    Args:
        source: File or directory to scan and redact.
        dry_run: When ``True``, scan but never modify files.
        verbose: When ``True``, render the per-match table.
        assume_yes: Skip the interactive confirmation prompt.
        console: Rich console sink.
    """
    _require_existing(console, source, "Source path")
    config = load_config()
    scanner, summary = _scan_source(source, config)
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)

    if not summary.matches:
        console.print("[green]No findings — nothing to redact.[/green]")
        return

    if dry_run:
        console.print("[yellow]Dry run: no files modified.[/yellow]")
        return

    files = _files_from_summary(summary)
    if not _confirm_apply(files, assume_yes=assume_yes, console=console):
        return

    redactor = Redactor(config=config.redaction, salt=scanner.salt)
    _apply_redactions(redactor, files, console)


# ---------------------------------------------------------------------------
# Mode: review
# ---------------------------------------------------------------------------


def run_review(
    vault: Path,
    *,
    verbose: bool,
    console: Console,
) -> None:
    """Re-scan *vault* and render the markdown review queue.

    Args:
        vault: Vault root (or any directory) to re-scan.
        verbose: When ``True``, also list every individual match.
        console: Rich console sink.
    """
    _require_existing(console, vault, "Vault path")
    config = load_config()
    scanner, summary = _scan_source(vault, config)
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)
    console.print(Markdown(scanner.generate_review_queue(summary)))
