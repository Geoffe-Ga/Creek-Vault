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
from enum import Enum
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
from creek.redact.scanner import (
    SYMLINK_SKIP_LABEL,
    RedactionScanner,
    ScanSummary,
    resolves_within,
)

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

    The escaping-symlink row appears only when the walk actually declined
    something (#1087). ``--scan`` skips such a file rather than refusing the
    tree the way the SEC-003 write guard does, so this row is the only place
    the operator learns the scan was not exhaustive — but rendering a
    permanent ``0`` would train them to ignore it.

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
    if summary.files_skipped_symlink:
        stats.add_row(SYMLINK_SKIP_LABEL, str(summary.files_skipped_symlink))
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


class SymlinkPolicy(Enum):
    """What a redaction mode does with a directly-named escaping symlink.

    The read and write contracts differ in kind, and the fix for #1293 has to
    keep them distinct rather than collapsing both into one refusal:

    * ``REFUSE`` — ``--apply`` and ``--review``. Both may write, so following
      a link named on the command line risks rewriting a file outside the
      tree the operator named. They abort with exit ``1`` before anything is
      read through the link.
    * ``SKIP`` — ``--scan``. It writes nothing, so refusing a whole scan over
      one bad link would be a denial of service on the safety pass itself —
      the operator's only way of learning what is exposed. The path is
      declined, counted under ``files_skipped_symlink``, and the scan still
      exits ``0``.

    The distinction is pinned by ``tests/test_cli_redact.py:545-553``.

    Attributes:
        REFUSE: Abort the run — the write path (``--apply``, ``--review``).
        SKIP: Decline the path and carry on — the read path (``--scan``).
    """

    REFUSE = "refuse"
    SKIP = "skip"


def _scan_source(
    source: Path,
    config: CreekConfig,
    *,
    console: Console,
    label: str,
    policy: SymlinkPolicy,
) -> tuple[RedactionScanner, ScanSummary]:
    """Scan *source* (file or directory) and return the scanner + summary.

    The single chokepoint through which every redaction mode reaches the
    filesystem, and therefore the right place to decide what happens when the
    operator names an escaping symlink outright (#1293). *policy* has no
    default on purpose: mypy strict then makes it a build error to add a
    fourth mode without stating a symlink policy, which is precisely the
    omission that left ``run_scan`` unguarded in the first place.

    Args:
        source: File or directory to scan, exactly as the operator named it.
        config: Loaded Creek configuration.
        console: Rich console sink for the refusal banner.
        label: Human-readable label for the root in the refusal message
            (e.g. ``"source"`` or ``"vault"``).
        policy: What to do when *source* itself escapes its own parent —
            see :class:`SymlinkPolicy`.

    Returns:
        Tuple of ``(scanner, summary)``. The scanner is returned so that
        the caller can reuse its session salt for redaction or review. Under
        :attr:`SymlinkPolicy.SKIP` an escaping *source* yields an empty
        summary carrying a single ``files_skipped_symlink``.

    Raises:
        typer.Exit: With code ``1`` under :attr:`SymlinkPolicy.REFUSE` when
            *source* is a symlink resolving outside its own parent.
    """
    scanner = RedactionScanner(config=config.redaction)
    if _named_path_escapes(source):
        if policy is SymlinkPolicy.REFUSE:
            _assert_named_path_contained(source, console=console, label=label)
        logger.warning("Skipping symlink that escapes the scan root: %s", source)
        return scanner, ScanSummary(files_skipped_symlink=1)
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


def _named_path_escapes(path: Path) -> bool:
    """Report whether *path* is itself a symlink leaving its own parent.

    The containment question for a path the operator names on the command
    line, as opposed to one the scanner discovers while walking a tree. It
    delegates to :func:`creek.redact.scanner.resolves_within` rather than
    restating the predicate, so the named-leaf surface and the walked-child
    surface cannot drift into two definitions of "inside".

    Three properties carry the correctness argument:

    * ``is_symlink()`` is an ``lstat`` on the leaf, so a path that is not
      itself a link is never resolved and never compared. That is what makes
      this guard behave identically on darwin and on Linux CI: a root reached
      *through* a symlinked component (``/tmp`` → ``/private/tmp`` on macOS)
      is not flagged. Same reasoning as the scanner's own walk policy.
    * When the leaf *is* a link, BOTH sides are resolved — the target inside
      ``resolves_within``, the parent here — so ``/tmp`` → ``/private/tmp``
      cannot manufacture a spurious refusal for a link that never left its
      own directory.
    * The predicate is LEAF-ONLY, and deliberately so: a named path whose
      escaping link is an ANCESTOR component (``<root>/linkdir/a.md``, where
      ``linkdir`` is the link) is admitted. That is a known, accepted
      residual — not full coverage of path traversal.

    ``strict=False`` matches the shipped guard: a target that does not exist
    still resolves to a candidate location worth comparing. Dangling and
    looping links do not reach here in the CLI anyway — ``_require_existing``
    reports them as "not found" first.

    Args:
        path: The source or vault path exactly as the operator supplied it.

    Returns:
        ``True`` when *path* is a symlink whose target is not a descendant of
        its own resolved parent directory.
    """
    return path.is_symlink() and not resolves_within(
        path,
        path.parent.resolve(strict=False),
    )


def _assert_named_path_contained(
    path: Path,
    *,
    console: Console,
    label: str,
) -> None:
    """Refuse a directly-named symlink that escapes its own parent (#1293).

    The write-path half of the contract: ``--apply`` and ``--review`` stop
    here, before the named path is opened, walked, or used to locate
    ``<vault>/00-Creek-Meta/creek_config.yaml``. The companion read path
    (``--scan``) skips instead — see :class:`SymlinkPolicy`.

    The refusal is outright. There is no ``--force``, no environment
    variable, and no config key, per SEC-003's no-waiver precedent: the
    security direction here is one-way, and this guard may only ever cause
    the tool to read and write *less*.

    Only the as-supplied path is named — in the banner and in the log. The
    resolved target is never disclosed: doing so is the very oracle #1087
    closes. The log is a ``warning`` rather than an ``exception`` because no
    exception is being handled at this point; ``logger.exception`` here would
    append a bogus ``NoneType: None`` traceback to the record.

    Args:
        path: The source or vault path exactly as the operator supplied it.
        console: Rich console sink for the error banner.
        label: Human-readable label for the root in the error message
            (e.g. ``"source"`` or ``"vault"``).

    Raises:
        typer.Exit: With code ``1`` when *path* is an escaping symlink.
    """
    if not _named_path_escapes(path):
        return
    logger.warning(
        "Refusing to follow symlink that escapes the %s root: %s",
        label,
        path,
    )
    _error_exit(
        console,
        f"Refusing to follow symlink that escapes the {label} root: {path}",
        code=1,
    )


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

    The read path never refuses over containment: a *source* that is itself
    an escaping symlink is declined and counted in the statistics table
    (:attr:`SymlinkPolicy.SKIP`), so one bad link cannot disable the whole
    safety pass.

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
    scanner, summary = _scan_source(
        source,
        config,
        console=console,
        label="source",
        policy=SymlinkPolicy.SKIP,
    )
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

    When *file_path* is a symlink, :func:`os.replace` MATERIALISES it: the
    link is replaced by a regular file rather than written through. That
    side effect is deliberate, not incidental. Writing *through* a link is
    the operation SEC-003 forbids; by the time execution reaches here both
    ends of any surviving alias are inside the tree the operator named
    (:func:`_named_path_escapes` for the named leaf,
    :func:`_assert_no_escaping_symlinks` for descendants), and
    ``scan_batch`` surfaces the alias's target as a file in its own right,
    so the target is redacted on its own pass.

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
    """Append one audit entry per touched file under *vault_path*.

    Each entry records ``source_path`` exactly as the pipeline saw it, never
    resolved. That is truthful without being exhaustive, and the difference
    matters to anyone reading the trail:

    * for a directly-named path, because no admitted named path resolves
      outside its own parent (:func:`_named_path_escapes`);
    * for a descendant, because the walk guard and the scanner's own
      candidate filter both decline children that escape the root.
    * Residual: a path reached through a symlinked ANCESTOR component is
      admitted by that leaf-only policy, and the audit then records the
      as-supplied path rather than where the write landed.

    Resolving audit paths would not close that residual — it would only
    start disclosing real intra-tree paths behind ordinary aliases.

    Args:
        summary: Scan summary whose matches describe what was found.
        files: Files touched by this run, in report order.
        vault_path: Vault root owning ``00-Creek-Meta/audit/redact.jsonl``.
        dry_run: Whether this run previewed rather than committed changes.
    """
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
            ``load_config().vault_path``. Contained like *source*: it is
            both read from and *written to*, so an escaping link here is an
            out-of-root write even when *source* is innocent.

    Raises:
        typer.Exit: With code ``1`` when *source* or *vault* is, or
            contains, a symlink that escapes the tree the operator named.
    """
    _require_existing(console, source, "Source path")
    # Before ``is_dir()``, which follows the link, and before ``load_config``,
    # which would otherwise read the config *through* it (#1293).
    _assert_named_path_contained(source, console=console, label="source")
    # ``--vault`` is the second path the operator names, and the only one this
    # mode WRITES to: the audit record lands in
    # ``<vault>/00-Creek-Meta/audit/redact.jsonl``, creating that directory if
    # needed. Guarding only ``--source`` would leave the audit trail — the
    # record of what was touched — landing wherever a link points, with an
    # entirely innocent source (#1293).
    if vault is not None:
        _assert_named_path_contained(vault, console=console, label="vault")
    if source.is_dir():
        _assert_no_escaping_symlinks(source, console=console, label="source")
    config = load_config(resolve_config_path(vault, None))
    vault_path = vault if vault is not None else config.vault_path
    scanner, summary = _scan_source(
        source,
        config,
        console=console,
        label="source",
        policy=SymlinkPolicy.REFUSE,
    )
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

    Raises:
        typer.Exit: With code ``1`` when *vault* is, or contains, a symlink
            that escapes the tree the operator named.
    """
    _require_existing(console, vault, "Vault path")
    # Before ``is_dir()``, which follows the link, and before ``load_config``,
    # which would otherwise read the config *through* it (#1293).
    _assert_named_path_contained(vault, console=console, label="vault")
    if vault.is_dir():
        _assert_no_escaping_symlinks(vault, console=console, label="vault")
    config = load_config(resolve_config_path(vault, None))
    scanner, summary = _scan_source(
        vault,
        config,
        console=console,
        label="vault",
        policy=SymlinkPolicy.REFUSE,
    )
    render_summary(summary, console)
    if verbose:
        render_matches(summary, console)
    console.print(Markdown(scanner.generate_review_queue(summary)))
