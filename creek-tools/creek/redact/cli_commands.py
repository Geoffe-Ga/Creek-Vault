"""Command handlers for the ``creek redact`` CLI.

Provides the three user-facing redaction modes — :func:`run_scan`,
:func:`run_apply`, and :func:`run_review` — together with Rich-based
display helpers that render colorized summary tables, per-match
listings, and markdown review queues.

Keeping these helpers in a dedicated module isolates them from Typer
command registration and keeps :mod:`creek.cli` focused on routing.
"""

from __future__ import annotations

import itertools
import logging
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from creek.config import load_config, resolve_config_path
from creek.redact.audit import RedactionAuditEntry, RedactionAuditLog
from creek.redact.patterns import PATTERN_METADATA
from creek.redact.redactor import Redactor
from creek.redact.scanner import RedactionScanner, ScanSummary

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
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
    by_sev: Counter[str] = Counter(_severity(m.match_type) for m in summary.matches)
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
    by_type: Counter[str] = Counter(m.match_type for m in summary.matches)
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


def _assert_no_escaping_symlinks(
    root: Path,
    *,
    console: Console,
    label: str,
) -> None:
    """Refuse to operate on a tree containing symlinks that escape *root*.

    Walks *root* without following directory symlinks (``followlinks=False``)
    and, for every symlink encountered, verifies that the resolved target
    is a descendant of the resolved root. Any symlink whose target lies
    outside the tree triggers ``typer.Exit`` with a clear, actionable
    error message — preventing the SEC-003 path-traversal scenario where
    a symlink under the source tree could cause redaction to overwrite
    arbitrary on-disk files.

    Symlinks whose resolved target stays inside *root* are permitted so
    that legitimate intra-tree aliases (e.g. ``alias.md`` → ``real.md``)
    continue to work.

    ``strict=False`` on ``Path.resolve`` is deliberate: a dangling
    symlink (pointing at a path that doesn't yet exist) still resolves
    to a candidate location that we can compare against *root*. The
    only resolve error we expect to see in practice is
    ``RuntimeError`` from a circular symlink, which we treat as
    "escaping" — the caller can't safely operate on the tree either
    way.

    Args:
        root: The user-supplied source or vault root.
        console: Rich console sink for the error banner.
        label: Human-readable label for the root in error messages
            (e.g. ``"source"`` or ``"vault"``).

    Raises:
        typer.Exit: When any descendant symlink resolves outside
            *root* or forms a loop.
    """
    resolved_root = root.resolve(strict=False)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for entry in itertools.chain(dirnames, filenames):
            candidate = Path(dirpath) / entry
            if not candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                logger.exception(
                    "Refusing to follow symlink that escapes the %s root: %s",
                    label,
                    candidate,
                )
                _error_exit(
                    console,
                    f"Refusing to follow symlink that escapes the {label} "
                    f"root: {candidate}",
                    code=1,
                )


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


def require_flag(
    value: Path | None,
    mode: str,
    flag: str,
    console: Console,
) -> Path:
    """Return *value* or abort when a required CLI flag is missing.

    Shared helper so that :mod:`creek.cli` and the handlers in this
    module emit identical, consistently styled error messages.

    Args:
        value: Candidate path from the CLI.
        mode: Mode flag that triggered the requirement (e.g. ``--scan``).
        flag: Required flag name (e.g. ``--source``).
        console: Rich console sink.

    Returns:
        The validated, non-``None`` path.

    Raises:
        typer.Exit: When *value* is ``None``.
    """
    if value is None:
        _error_exit(console, f"{mode} requires {flag}")
    return value


# ---------------------------------------------------------------------------
# Mode: scan
# ---------------------------------------------------------------------------


def run_scan(
    source: Path,
    *,
    report: bool,
    verbose: bool,
    console: Console,
    vault: Path | None = None,
) -> ScanSummary:
    """Scan *source* for sensitive data and render a report.

    Args:
        source: File or directory to scan.
        report: When ``True``, render the detailed markdown report.
        verbose: When ``True``, also list every individual match.
        console: Rich console sink.
        vault: Optional vault root used to auto-discover
            ``<vault>/00-Creek-Meta/creek_config.yaml`` (issue #322).
            Ignored when ``CREEK_CONFIG`` is set or the file is absent.

    Returns:
        The :class:`ScanSummary` produced by the scanner.
    """
    _require_existing(console, source, "Source path")
    config = load_config(resolve_config_path(vault, None))
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


def _atomic_write(file_path: Path, content: str) -> None:
    """Atomically replace *file_path*'s content.

    Writes to a same-directory temporary file first, then swaps it into
    place with :func:`os.replace`. The swap is atomic on POSIX and best
    effort on Windows. If any step fails the temp file is unlinked so
    the original on-disk file is left untouched.

    Args:
        file_path: Destination path to rewrite.
        content: New file contents.

    Raises:
        OSError: Propagated from filesystem primitives. The original
            file is never observed in a half-written state.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.",
        suffix=".redact-tmp",
        dir=file_path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_path, file_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _matches_by_file(
    summary: ScanSummary,
) -> dict[Path, list[RedactionMatch]]:
    """Group *summary*'s matches into ``{file_path: [matches]}``."""
    grouped: dict[Path, list[RedactionMatch]] = {}
    for match in summary.matches:
        grouped.setdefault(match.file_path, []).append(match)
    return grouped


def _audit_entry_for_file(
    file_path: Path,
    file_matches: list[RedactionMatch],
    *,
    dry_run: bool,
) -> RedactionAuditEntry:
    """Build a :class:`RedactionAuditEntry` for one touched file."""
    counts: dict[str, int] = {}
    for match in file_matches:
        counts[match.match_type] = counts.get(match.match_type, 0) + 1
    return RedactionAuditEntry(
        source_path=str(file_path),
        pattern_names=sorted(counts.keys()),
        match_counts=counts,
        dry_run=dry_run,
    )


def _write_redaction_audit(
    summary: ScanSummary,
    files: list[Path],
    *,
    vault_path: Path,
    dry_run: bool,
) -> None:
    """Append one audit entry per touched file under *vault_path*."""
    audit_log = RedactionAuditLog(vault_path)
    grouped = _matches_by_file(summary)
    for file_path in files:
        file_matches = grouped.get(file_path, [])
        if not file_matches:
            continue
        audit_log.append(
            _audit_entry_for_file(file_path, file_matches, dry_run=dry_run),
        )


def _apply_redactions(
    redactor: Redactor,
    files: list[Path],
    console: Console,
) -> None:
    """Rewrite each file in place with sensitive data replaced.

    Each file is rewritten atomically via :func:`_atomic_write`, so a
    mid-loop failure cannot leave any single file half-written. If an
    :class:`OSError` interrupts the batch, already-redacted files keep
    their redactions and the remaining files remain untouched — the
    partial progress is reported and the error is surfaced as a
    non-zero exit code.

    Args:
        redactor: Configured :class:`Redactor`.
        files: Files to rewrite.
        console: Rich console sink for progress and error messages.

    Raises:
        typer.Exit: With code ``1`` when the batch is interrupted by an
            I/O failure after reporting how many files were completed.
    """
    completed = 0
    try:
        for file_path in files:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            redacted = redactor.redact_content(text)
            _atomic_write(file_path, redacted)
            completed += 1
    except OSError as exc:
        console.print(
            f"[red]I/O error after redacting {completed} of "
            f"{len(files)} file(s): {exc}[/red]"
        )
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Applied redactions to {completed} file(s).[/green]")


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
    vault: Path | None = None,
) -> None:
    """Scan *source*, obtain consent, and redact files in place.

    Both dry-run and apply runs append per-file entries to
    ``<vault>/00-Creek-Meta/audit/redact.jsonl`` so the audit trail
    captures every preview operators reviewed in addition to the
    committed rewrites.

    Args:
        source: File or directory to scan and redact.
        dry_run: When ``True``, scan but never modify files.
        verbose: When ``True``, render the per-match table.
        assume_yes: Skip the interactive confirmation prompt.
        console: Rich console sink.
        vault: Vault root for the audit log. Defaults to
            ``load_config().vault_path``.
    """
    _require_existing(console, source, "Source path")
    if source.is_dir():
        _assert_no_escaping_symlinks(source, console=console, label="source")
    config = load_config(resolve_config_path(vault, None))
    vault_path = vault if vault is not None else config.vault_path
    scanner, summary = _scan_source(source, config)
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)

    if not summary.matches:
        console.print("[green]No findings — nothing to redact.[/green]")
        return

    files = _files_from_summary(summary)

    if dry_run:
        _write_redaction_audit(
            summary,
            files,
            vault_path=vault_path,
            dry_run=True,
        )
        console.print("[yellow]Dry run: no files modified.[/yellow]")
        return

    if not _confirm_apply(files, assume_yes=assume_yes, console=console):
        return

    redactor = Redactor(config=config.redaction, salt=scanner.salt)
    _apply_redactions(redactor, files, console)
    _write_redaction_audit(
        summary,
        files,
        vault_path=vault_path,
        dry_run=False,
    )


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
    if vault.is_dir():
        _assert_no_escaping_symlinks(vault, console=console, label="vault")
    config = load_config(resolve_config_path(vault, None))
    scanner, summary = _scan_source(vault, config)
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)
    console.print(Markdown(scanner.generate_review_queue(summary)))
