"""Command handlers for the ``creek redact`` CLI.

Provides the three user-facing redaction modes — :func:`run_scan`,
:func:`run_apply`, and :func:`run_review` — together with Rich-based
display helpers that render colorized summary tables, per-match
listings, and markdown review queues.

Keeping these helpers in a dedicated module isolates them from Typer
command registration and keeps :mod:`creek.cli` focused on routing.
"""

from __future__ import annotations

import errno
import logging
import os
import tempfile
import uuid
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from creek._containment import inspect_tree, named_path_escapes
from creek.config import VAULT_CONFIG_RELPATH, load_vault_config
from creek.purge.audit import LEGACY_PURGE_LOG_RELPATH
from creek.redact.audit import (
    REDACT_AUDIT_RELPATH,
    RedactionAuditEntry,
    RedactionAuditLog,
    RedactionOutcomeStatus,
)
from creek.redact.patterns import PATTERN_METADATA
from creek.redact.redactor import Redactor
from creek.redact.scanner import (
    SYMLINK_SKIP_LABEL,
    RedactionScanner,
    ScanSummary,
)

logger = logging.getLogger(__name__)

# The named-leaf predicate moved to :mod:`creek._containment` in #1294, when
# the ingest gate became its third consumer; the name stays bound here so
# this module's call sites and the #1293 tests keep reading the same way.
# Dangling and looping links do not reach it from the CLI anyway —
# ``_require_existing`` reports them as "not found" first.
_named_path_escapes = named_path_escapes

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

    **A subtree the walk could not list refuses too (#1498)**, which is the
    opposite of what the same condition earns on the ingest gate
    (:func:`creek._containment.assert_source_contained` reports it and
    continues). Three things invert here:

    * There is no unattended loop behind this guard. It is reached only from
      the interactive ``creek redact --apply`` / ``--review`` handlers; the
      pipeline's redaction pass goes through
      :func:`creek.redact.scanner.scan_batch` instead. Refusing cannot break
      ``creek sync`` or ``creek process``.
    * ``--apply`` destroys bytes irreversibly. Proceeding over a region it
      could not examine and then printing "Applied redactions to N file(s)"
      is a *false assurance* from a safety tool — strictly worse than a
      refusal, because the operator's next action depends on believing the
      tree was scrubbed.
    * There is no report channel in this command. Nothing here corresponds
      to ``IngestResult.discovery_complete``, and a warning line above a
      green banner is noise, not a report.

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
        typer.Exit: When any descendant symlink resolves outside *root* or
            forms a loop, or when any directory beneath *root* could not be
            listed.
    """
    # The walk itself lives in :func:`creek._containment.inspect_tree`
    # (#1294/#1498), which the ingest gate also calls. Reporting the facts
    # rather than raising is what lets two callers with different refusal
    # mechanics — ``typer.Exit`` here, ``EscapingSymlinkError`` there — share
    # one walk, and what lets them weigh an unlistable subtree differently.
    # ``logger.warning`` rather than ``logger.exception``: no exception is in
    # flight at this point any more, and ``logger.exception`` outside an
    # ``except`` block appends a bogus ``NoneType: None`` traceback.
    report = inspect_tree(root)
    if report.escaping is not None:
        logger.warning(
            "Refusing to follow symlink that escapes the %s root: %s",
            label,
            report.escaping,
        )
        _error_exit(
            console,
            "Refusing to follow symlink that escapes the "
            f"{label} root: {report.escaping}",
            code=1,
        )
    if report.unlistable:
        # Only the directory is named. Nothing beneath it was opened, and a
        # refusal that quoted what it could not read would be the #1087
        # oracle in reverse.
        logger.warning(
            "Refusing to walk the %s root: %s could not be listed",
            label,
            report.unlistable[0],
        )
        _error_exit(
            console,
            f"Refusing to walk the {label} root: {report.unlistable[0]} "
            "could not be listed, so no symlink beneath it could be checked",
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

    The two named paths take opposite sides of #1293's split, because they
    answer different questions.

    *source* is the tree being examined, and it is never refused over
    containment: a *source* that is itself an escaping symlink is declined
    and counted in the statistics table (:attr:`SymlinkPolicy.SKIP`), so one
    bad link cannot disable the whole safety pass.

    *vault* is not examined at all. It is used for exactly one thing —
    locating ``<vault>/00-Creek-Meta/creek_config.yaml`` — and an escaping
    one is refused, matching ``--apply`` and ``--review`` (#1359). #1293
    left it open because the escape looked like an inert read; it is not.
    ``test_a_vault_config_can_disarm_a_scan_completely`` takes a scan of a
    two-finding file to zero findings three separate ways
    (``supported_extensions``, ``exclude_patterns``,
    ``false_positive_allowlist``), so an out-of-tree config is a silent off
    switch on the pass the operator runs *because* they are worried. Naming
    a different path for the config is a one-line correction; a scan that
    reports "no findings" because a file outside the tree said so is not
    correctable, because nothing tells the operator it happened.

    The refusal is outright — no ``--force``, no environment variable, no
    config key — per :func:`_assert_named_path_contained`.

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

    Raises:
        typer.Exit: With code ``1`` when *vault* is a symlink that escapes
            its own parent. Never for *source* — see above.
    """
    _require_existing(console, source, "Source path")
    # Before ``load_config``, which would otherwise read the config *through*
    # the link and let a file outside the named tree configure the scanner
    # (#1359). Deliberately NOT applied to ``source``: that path keeps its
    # skip-and-count contract (#1087).
    if vault is not None:
        _assert_named_path_contained(vault, console=console, label="vault")
    config = load_vault_config(vault, warn_on_missing=True)
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
    operation_id: str,
) -> RedactionAuditEntry:
    """Build a ``phase="file"`` entry for one touched file."""
    counts: dict[str, int] = {}
    for match in file_matches:
        counts[match.match_type] = counts.get(match.match_type, 0) + 1
    return RedactionAuditEntry(
        phase="file",
        operation_id=operation_id,
        source_path=str(file_path),
        pattern_names=sorted(counts.keys()),
        match_counts=counts,
        dry_run=dry_run,
    )


def _safe_error_detail(exc: BaseException | None) -> str:
    """Describe a failure for an operator without echoing its message.

    ``str(OSError)`` renders as ``[Errno 28] No space left on device:
    '/vault/ssn-123-45-6789.md'`` — the ``filename`` argument rides
    along. In a redaction workflow filenames routinely carry the very
    secrets being redacted, and the console is not a private channel:
    it reaches CI logs, terminal scrollback and shared screens, all of
    which outlive the run. The audit log applies this rule already; the
    operator-facing messages on the destructive path must too.

    The errno *symbol* is kept because it is the actionable part and is
    OS-derived, never path-derived: an operator who sees ``ENOSPC``
    knows to free space, where a bare ``OSError`` tells them nothing.

    Args:
        exc: The failure to describe, or ``None``.

    Returns:
        A short, path-free description such as ``"OSError (ENOSPC)"``.
    """
    if exc is None:
        return "unknown cause"
    name = type(exc).__name__
    code = errno.errorcode.get(exc.errno or 0) if isinstance(exc, OSError) else None
    return f"{name} ({code})" if code else name


class _AuditWriteError(Exception):
    """The audit log could not record a file that was already rewritten.

    Distinct from the bare :class:`OSError` a *file* rewrite raises,
    because the two demand opposite messages: an ``OSError`` from
    :func:`_atomic_write` means the named file was NOT modified, while
    this means it WAS and the record of it is missing. Reporting the
    second with the first's "I/O error after redacting K of N file(s)"
    wording would misdescribe a successful rewrite as a failed one.

    The originating error is carried on ``__cause__``.
    """


class _ApplyAudit:
    """The single writer of ``<vault>/00-Creek-Meta/audit/redact.jsonl``.

    Emits the three-phase grammar #1308 introduced: one ``intent`` entry
    naming every candidate file *before* the first byte is rewritten,
    one ``file`` entry appended immediately after each file's own
    :func:`_atomic_write`, and one ``outcome`` entry closing the run.
    All three carry one :attr:`operation_id`.

    **Ordering is the whole point.** Previously every entry was batched
    after the rewrite loop returned, so an abort mid-batch destroyed
    bytes and recorded nothing at all. Interleaving the per-file append
    costs the same N appends and buys a compliance reader a real bound:
    every file with a ``file`` entry was rewritten, at most one further
    file may have been rewritten without its record, and nothing outside
    the intent entry's ``files`` list was touched.

    **Three-tier write policy**, because an audit append can fail for
    the same reason the rewrite did (ENOSPC on the shared volume) and
    the right response differs by phase:

    * :meth:`record_intent` fails **closed** — nothing has been
      destroyed yet, so refusing costs nothing and turns "destroy, then
      discover the log is unwritable" into "fail before touching
      anything".
    * :meth:`record_file` fails **fast** — the bytes are already gone;
      continuing would destroy more files that also could not be
      recorded, so the loop stops with the damage bounded to the
      entries already written.
    * :meth:`record_outcome` is **best effort** — it runs from inside
      exception handlers and must never displace the original failure.

    **Durability scope**, stated rather than claimed:
    :meth:`creek.audit.AuditLog.append` calls ``os.fsync`` inside the
    flock window, so the intent line's bytes are durable before the
    first rewrite and no extra fsync is needed here. NOT claimed: that a
    freshly created log's parent directory entry is directory-fsynced,
    nor that :func:`_atomic_write`'s ``os.replace`` is. Both residuals
    fail safe — intent present, rewrite lost.

    **Disclosure note.** The intent entry records candidate file PATHS,
    so an aborted run that modifies nothing now leaves those paths in
    the log where previously it left nothing. That is the same property
    ``source_path`` has always had, but it is new for aborted runs.

    Each entry records paths exactly as the pipeline saw them, never
    resolved. That is truthful without being exhaustive, and the
    difference matters to anyone reading the trail:

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
        files: Files this run may touch, in report order.
        vault_path: Vault root owning ``00-Creek-Meta/audit/redact.jsonl``.
        dry_run: Whether this run previews rather than commits changes.
    """

    def __init__(
        self,
        summary: ScanSummary,
        files: list[Path],
        *,
        vault_path: Path,
        dry_run: bool,
    ) -> None:
        """Mint an ``operation_id`` and bind one log instance to the run."""
        self.files = files
        self.dry_run = dry_run
        self.operation_id = uuid.uuid4().hex
        self._grouped = _matches_by_file(summary)
        # One instance for the whole run: ``AuditLog`` caches the chain
        # head per instance, so a fresh log per append would rescan the
        # whole file N times.
        self._log = RedactionAuditLog(vault_path)

    @property
    def log_path(self) -> Path:
        """Absolute path of the JSONL log this run writes to."""
        return self._log.log_path

    def record_intent(self, *, console: Console) -> None:
        """Declare the candidate set before anything is rewritten.

        Args:
            console: Rich console sink for the refusal message.

        Raises:
            typer.Exit: With code ``1`` when the log cannot be written.
                Nothing has been destroyed at this point, so refusing is
                strictly cheaper than proceeding blind.
        """
        entry = RedactionAuditEntry(
            phase="intent",
            operation_id=self.operation_id,
            files=[str(path) for path in self.files],
            dry_run=self.dry_run,
        )
        try:
            self._log.append(entry)
        except OSError as exc:
            console.print(
                f"[red]Cannot write the redaction audit log at "
                f"{self.log_path}: {type(exc).__name__}. "
                f"No files were modified.[/red]"
            )
            raise typer.Exit(code=1) from exc

    def record_file(self, file_path: Path) -> None:
        """Record one rewritten file, immediately after its own write.

        Args:
            file_path: The file just rewritten by :func:`_atomic_write`.

        Raises:
            _AuditWriteError: When the append fails. The caller must
                stop the batch: the bytes are gone and cannot be
                certified.
        """
        entry = _audit_entry_for_file(
            file_path,
            self._grouped.get(file_path, []),
            dry_run=self.dry_run,
            operation_id=self.operation_id,
        )
        try:
            self._log.append(entry)
        except OSError as exc:
            raise _AuditWriteError(str(file_path)) from exc

    def record_outcome(
        self,
        *,
        status: RedactionOutcomeStatus,
        console: Console,
        failure_reason: str | None = None,
    ) -> None:
        """Close the run. Best effort — never raises.

        Args:
            status: ``"complete"`` when the batch finished,
                ``"partial"`` when it aborted.
            console: Rich console sink for the degraded-logging warning.
            failure_reason: Exception **type name only**. Never
                ``str(exc)``: an ``OSError`` message embeds the
                offending path and filenames here carry secrets.
        """
        entry = RedactionAuditEntry(
            phase="outcome",
            operation_id=self.operation_id,
            status=status,
            failure_reason=failure_reason,
            dry_run=self.dry_run,
        )
        try:
            self._log.append(entry)
        except OSError as exc:
            logger.warning(
                "Could not append the redaction outcome entry: %s",
                type(exc).__name__,
            )
            console.print(
                f"[yellow]Warning: could not record the redaction outcome "
                f"in the audit log: {type(exc).__name__}.[/yellow]"
            )

    def record_preview(self, *, console: Console) -> None:
        """Write the full three-phase grammar for a ``--dry-run``.

        Kept separate from the rewrite loop so a read-only mode gains no
        new failure surface from the destructive path.

        Args:
            console: Rich console sink.

        Raises:
            typer.Exit: With code ``1`` when the log cannot be written.
        """
        self.record_intent(console=console)
        try:
            for file_path in self.files:
                self.record_file(file_path)
        except _AuditWriteError as exc:
            console.print(
                f"[red]Cannot write the redaction audit log at "
                f"{self.log_path}: {type(exc.__cause__).__name__}. "
                f"No files were modified.[/red]"
            )
            raise typer.Exit(code=1) from exc
        self.record_outcome(status="complete", console=console)


PROTECTED_AUDIT_RELPATHS: tuple[Path, ...] = (
    REDACT_AUDIT_RELPATH.parent,
    LEGACY_PURGE_LOG_RELPATH,
    VAULT_CONFIG_RELPATH,
)
"""Vault-relative files ``--apply`` must never rewrite.

The first entry is the whole ``00-Creek-Meta/audit/`` directory, not
just ``redact.jsonl``: ``purge.jsonl``, ``privacy.jsonl`` and
``mcp.jsonl`` are the same hash-chained shape and ``.json`` *is* in the
default ``supported_extensions``, so they were being rewritten already.

The second is the pre-Batch-C purge log, which lives outside that
directory and is worse than plain data loss:
:meth:`creek.purge.audit.PurgeAuditLog._migrate_legacy_if_needed`
replays it into the chained ``purge.jsonl`` on next use, so redacting it
would get the mutated records chain-*signed* into the purge trail.

The third is the vault's own ``creek_config.yaml`` (#1398). ``.yaml`` is
in the default ``supported_extensions`` and ``exclude_patterns`` says
nothing about ``00-Creek-Meta``, so a vault-wide apply treated the
config as an ordinary candidate and rewrote it in place — the standing
"``--apply`` corrupts structured files" hazard landing on the one
structured file that steers the *next* run. The damage is not symmetric
with losing data: an ``exclude_patterns`` token replaced by
``[REDACTED:high_entropy_string]`` *widens* what the next apply walks,
so running redaction reconfigures redaction. (``false_positive_allowlist``
entries are the one class of value the rewrite could never touch —
:meth:`creek.redact.scanner.RedactionScanner._is_allowlisted` is
exact-string membership, so the scanner declines the match before the
redactor sees it. Everything else in the file was in scope.)

The name still says ``AUDIT`` because the tuple's *contract* has not
changed — it is the never-rewrite set — and renaming it would churn every
reference for no behavioural gain.

All three are derived from their owning module's constant rather than
restated here, so a future relocation cannot silently unprotect them.
"""


_VAULT_META_DIRNAME = VAULT_CONFIG_RELPATH.parent
"""The vault's meta directory name, derived from its owning constant.

Deriving rather than restating it keeps this in step with
:data:`creek.config.VAULT_CONFIG_RELPATH` if the layout ever moves.
"""


def _render_meta_dirs(vault_paths: tuple[Path, ...]) -> str:
    """Render the meta directories of *vault_paths* for an operator message.

    Args:
        vault_paths: The vault roots being protected.

    Returns:
        A comma-separated list of ``<root>/00-Creek-Meta`` paths.
    """
    return ", ".join(str(path / _VAULT_META_DIRNAME) for path in vault_paths)


def _resolve_or_self(path: Path) -> Path:
    """Resolve *path*, falling back to it unchanged when that is impossible.

    A symlink loop or a permission error must not drop the protection
    guard entirely: an unresolvable path still contributes its declared
    form, so :func:`_reachable_vault_roots` fails closed rather than
    returning nothing.

    Args:
        path: The path to resolve.

    Returns:
        The resolved path, or *path* itself if resolution failed.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _reachable_vault_roots(source: Path, vault_path: Path) -> tuple[Path, ...]:
    """Vault roots whose compliance artifacts *source* could rewrite.

    ``CreekConfig.vault_path`` defaults to ``Path()`` — the process CWD —
    so anchoring the protected set on it alone protected wherever the
    operator happened to be standing rather than the tree actually being
    walked. A ``--apply --source <vault>`` run from a neighbouring
    directory rewrote that vault's own ``creek_config.yaml`` and legacy
    purge log (#1561).

    *source* is resolved **before** its ancestors are enumerated:
    ``Path("sub").parents`` is ``(Path("."),)``, so an unresolved
    relative path can never climb to the vault root above it.

    Args:
        source: The tree ``--apply`` walks.
        vault_path: The explicit ``--vault``, or the config default.

    Returns:
        Every distinct vault root to protect, nearest ancestor first.
    """
    roots: list[Path] = []
    resolved_source = _resolve_or_self(source)
    for anchor in (resolved_source, *resolved_source.parents):
        if (anchor / _VAULT_META_DIRNAME).is_dir() and anchor not in roots:
            roots.append(anchor)
    declared = _resolve_or_self(vault_path)
    if declared not in roots:
        roots.append(declared)
    return tuple(roots)


def _exclude_audit_artifacts(
    files: list[Path],
    vault_paths: tuple[Path, ...],
    *,
    console: Console,
) -> list[Path]:
    """Drop candidates the vault must never have rewritten under it.

    Both the audit trail and ``creek_config.yaml`` sit *inside* the tree
    ``--apply`` walks: ``exclude_patterns`` defaults to
    ``['.git', 'node_modules']`` and says nothing about ``00-Creek-Meta``.
    ``redact.jsonl`` escaped only because ``.jsonl`` happens to be absent
    from the *user-editable* ``supported_extensions`` list; ``.yaml`` is
    in it, so the config did not escape at all (#1398). A run that
    redacts its own history mutates lines the hash chain then re-anchors
    onto, and :meth:`RedactionAuditLog.verify` still passes: silent,
    undetectable tampering with a tamper-evidence log. A run that
    redacts its own config changes what the *next* run walks.

    So the exclusion is unconditional — independent of both
    ``supported_extensions`` and ``exclude_patterns`` — and covers every
    path in :data:`PROTECTED_AUDIT_RELPATHS`.

    Applied to the APPLY file list only, never to ``scan_batch``:
    detection must stay exactly as wide as it is today for ``--scan``,
    ``--review``, the MCP scan tool and ``creek process``. Only in-place
    destruction narrows.

    Args:
        files: Candidate files the apply would rewrite.
        vault_path: Vault root owning the compliance artifacts.
        console: Rich console sink for the one-line notice.

    Returns:
        *files* minus every protected compliance artifact.
    """
    try:
        protected = tuple(
            (vault_path / relpath).resolve()
            for vault_path in vault_paths
            for relpath in PROTECTED_AUDIT_RELPATHS
        )
    except (OSError, RuntimeError) as exc:
        # The candidate side of the comparison fails closed (see
        # :func:`_is_protected`); the protected-root side must too, or the
        # invariant only holds for half of it. A symlink loop at
        # ``00-Creek-Meta/audit`` would otherwise crash with a raw
        # traceback. Nothing has been rewritten at this point.
        console.print(
            f"[red]Cannot resolve the vault's compliance artifacts under "
            f"{_render_meta_dirs(vault_paths)}: {_safe_error_detail(exc)}. "
            f"Refusing to redact anything, because the audit trail cannot "
            f"be reliably excluded. No files were modified.[/red]"
        )
        raise typer.Exit(code=1) from exc
    kept: list[Path] = []
    dropped = 0
    for file_path in files:
        if _is_protected(file_path, protected):
            dropped += 1
        else:
            kept.append(file_path)
    if dropped:
        console.print(
            f"[yellow]Skipped {dropped} protected file(s) under "
            f"{_render_meta_dirs(vault_paths)}: the audit trail, the legacy "
            f"purge log and the config that governs redaction are never "
            f"rewritten in place.[/yellow]"
        )
    return kept


def _is_protected(file_path: Path, protected: tuple[Path, ...]) -> bool:
    """Return ``True`` when *file_path* is, or sits under, a protected path.

    Fails **closed**: a path that cannot be resolved is treated as
    protected, so an unresolvable candidate is skipped rather than
    rewritten. Refusing to redact a file is recoverable; corrupting the
    audit log is not.

    That inversion is why this does not reuse
    :func:`creek._containment.resolves_within`, the repo's usual
    containment predicate. That helper returns ``False`` — "outside" —
    when resolution fails, which is the right default when the question
    is "may I touch this?" and the wrong one when the question is "must
    I leave this alone?". Reusing it here would keep an unresolvable
    candidate in the rewrite set and destroy it.

    Args:
        file_path: Candidate path to test.
        protected: Already-resolved paths to test against.

    Returns:
        Whether *file_path* must be left alone.
    """
    try:
        resolved = file_path.resolve()
    except OSError:
        return True
    return any(resolved == root or root in resolved.parents for root in protected)


def _nothing_to_redact_message(summary: ScanSummary) -> str:
    """Return the message for an apply that has no file to rewrite.

    Two ways to get here: the scan found nothing, or every match it found
    was in a protected file (:data:`PROTECTED_AUDIT_RELPATHS` — the audit
    trail, the legacy purge log, ``creek_config.yaml``), which is never
    rewritten. The message must not contradict the summary table already
    printed, and must not name only the audit trail when the config is
    equally capable of being the sole reason (#1398).

    Args:
        summary: The scan summary just rendered.

    Returns:
        A Rich-markup string containing the substring
        ``"nothing to redact"``.
    """
    if summary.matches:
        return (
            "[green]Nothing to redact — every match is in a protected "
            "file under 00-Creek-Meta (the audit trail, the legacy purge "
            "log, or the config that governs redaction), which is never "
            "rewritten in place.[/green]"
        )
    return "[green]No findings — nothing to redact.[/green]"


def _rewrite_and_record(
    redactor: Redactor,
    file_path: Path,
    audit: _ApplyAudit,
) -> None:
    """Rewrite one file and record it — the pair is indivisible.

    The ONLY call site of :func:`_atomic_write`, deliberately. #1308 was
    an ordering defect: the appends were batched after the rewrite loop,
    so an abort left destroyed bytes with no record. Binding the two
    into one function makes reintroducing that ordering a structural
    change rather than a subtle one, and
    ``test_atomic_write_has_exactly_one_call_site_and_it_records``
    fails if anyone tries.

    Args:
        redactor: Configured :class:`Redactor`.
        file_path: File to rewrite in place.
        audit: The run's audit writer.

    Raises:
        OSError: From the read or the atomic write; the file is then
            untouched.
        _AuditWriteError: From the append; the file IS rewritten.
    """
    text = file_path.read_text(encoding="utf-8", errors="replace")
    redacted = redactor.redact_content(text)
    _atomic_write(file_path, redacted)
    audit.record_file(file_path)


def _apply_redactions(
    redactor: Redactor,
    files: list[Path],
    audit: _ApplyAudit,
    console: Console,
) -> None:
    """Rewrite each file in place, recording each one as it lands.

    Each file is rewritten atomically via :func:`_atomic_write`, so a
    mid-loop failure cannot leave any single file half-written, and
    :meth:`_ApplyAudit.record_file` runs immediately after each write
    (inside :func:`_rewrite_and_record`) so an abort cannot leave a
    destroyed file unrecorded. If the batch is interrupted,
    already-redacted files keep their redactions, the remaining files
    remain untouched, the partial progress is reported, and the error
    surfaces as a non-zero exit code.

    The outcome entry is written from inside each handler rather than
    from an outer wrapper, and that is not incidental: ``typer.Exit``
    subclasses ``click.exceptions.Exit`` → ``RuntimeError``, so a
    wrapper placed outside this function would catch the *converted*
    exception and record ``failure_reason="Exit"`` — the literal string
    — instead of the real cause. The handler order is likewise
    load-bearing: most specific first.

    The ``except BaseException ... raise`` clause mirrors
    :meth:`creek.purge.engine.PurgeEngine._run_audited`, so a Ctrl-C
    mid-batch is recorded rather than silently losing the run.

    Args:
        redactor: Configured :class:`Redactor`.
        files: Files to rewrite.
        audit: The run's audit writer, already carrying its intent line.
        console: Rich console sink for progress and error messages.

    Raises:
        typer.Exit: With code ``1`` when the batch is interrupted by an
            I/O failure or by an audit-write failure, after reporting
            how many files were completed.
    """
    completed = 0
    try:
        for file_path in files:
            _rewrite_and_record(redactor, file_path, audit)
            completed += 1
    except _AuditWriteError as exc:
        # ``record_file`` always raises ``from`` the OSError it caught, so
        # ``__cause__`` is set; ``_safe_error_detail`` degrades to a loud
        # "unknown cause" rather than a silent "NoneType" if that ever
        # stops being true.
        console.print(
            f"[red]Redaction audit write failed after {completed} of "
            f"{len(files)} file(s): {_safe_error_detail(exc.__cause__)}. "
            f"Stopping — the file just rewritten has no audit record, and "
            f"continuing would destroy more files that could not be "
            f"recorded either.[/red]"
        )
        audit.record_outcome(
            status="partial",
            failure_reason=type(exc.__cause__).__name__,
            console=console,
        )
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        # Never ``{exc}``: str(OSError) embeds the filename it failed on,
        # and those filenames carry secrets. Same rule as failure_reason.
        console.print(
            f"[red]I/O error after redacting {completed} of "
            f"{len(files)} file(s): {_safe_error_detail(exc)}[/red]"
        )
        audit.record_outcome(
            status="partial",
            failure_reason=type(exc).__name__,
            console=console,
        )
        raise typer.Exit(code=1) from exc
    except BaseException as exc:
        audit.record_outcome(
            status="partial",
            failure_reason=type(exc).__name__,
            console=console,
        )
        raise
    console.print(f"[green]Applied redactions to {completed} file(s).[/green]")
    audit.record_outcome(status="complete", console=console)


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

    Both dry-run and apply runs write the three-phase record described
    on :class:`_ApplyAudit` to ``<vault>/00-Creek-Meta/audit/
    redact.jsonl``, so the audit trail captures every preview operators
    reviewed in addition to the committed rewrites. The ``intent`` entry
    is written after consent and before the first rewrite; per-file
    entries are interleaved with the rewrites themselves; the
    ``outcome`` entry closes the run.

    What a reader of that log may conclude, and no more (#1308): every
    file carrying a ``file`` entry WAS rewritten; at most one further
    file may have been rewritten without its record (the one in flight
    when the run died); and nothing outside the ``intent`` entry's
    ``files`` list was touched. An ``intent`` with no ``outcome`` means
    the run did not finish — it does not mean nothing happened.

    Durability scope: :meth:`creek.audit.AuditLog.append` fsyncs inside
    its flock window, so the intent line is durable before the first
    rewrite. Not claimed: directory-level fsync of a freshly created
    log, or of :func:`_atomic_write`'s ``os.replace``. Both residuals
    fail safe — intent present, rewrite lost.

    The vault's own ``00-Creek-Meta/audit/`` directory is excluded from
    the rewrite set unconditionally (:func:`_exclude_audit_artifacts`),
    so a run can never redact its own history.

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
    config = load_vault_config(vault, warn_on_missing=True)
    declared_vault = vault if vault is not None else config.vault_path
    # Anchor on the vault actually being walked, not on the process CWD that
    # ``CreekConfig.vault_path`` falls back to (#1561).
    protected_roots = _reachable_vault_roots(source, declared_vault)
    vault_path = vault if vault is not None else protected_roots[0]
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

    files = _exclude_audit_artifacts(
        _files_from_summary(summary),
        protected_roots,
        console=console,
    )
    if not files:
        console.print(_nothing_to_redact_message(summary))
        return

    audit = _ApplyAudit(summary, files, vault_path=vault_path, dry_run=dry_run)

    if dry_run:
        audit.record_preview(console=console)
        console.print("[yellow]Dry run: no files modified.[/yellow]")
        return

    if not _confirm_apply(files, assume_yes=assume_yes, console=console):
        return

    # After consent, before the first byte: an operator who declined has
    # authorised nothing, and an unwritable log must abort while there is
    # still nothing to be sorry about.
    audit.record_intent(console=console)
    redactor = Redactor(config=config.redaction, salt=scanner.salt)
    _apply_redactions(redactor, files, audit, console)


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
    config = load_vault_config(vault, warn_on_missing=True)
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
