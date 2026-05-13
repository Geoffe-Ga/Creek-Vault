"""Creek CLI -- command-line interface for the Creek knowledge organization pipeline."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
from rich.console import Console
from rich.table import Table

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    override_elevates,
    parse_include_tier,
    record_privacy_override,
)
from creek.config import load_config
from creek.consent import ConsentManager
from creek.models import PrivacyTier
from creek.pipeline import Pipeline, RedactionRequiredError
from creek.save import SaveTarget

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.generate.drafts import DraftLLM
    from creek.ingest.base import Ingestor
    from creek.models import CompileTargetKind, Phase
    from creek.purge import PurgeEngine, PurgeResult


_INCLUDE_TIER_HELP = (
    "Privacy-tier override: open, personal, intimate, or all. "
    "Default policy excludes intimate fragments and replaces personal "
    "bodies with title-only summaries. Elevated values are recorded in "
    "<vault>/00-Creek-Meta/audit/privacy.jsonl."
)


def _parse_include_tier(value: str | None) -> PrivacyTierOverride | None:
    """Parse --include-tier and exit 2 with a clear error on bad values."""
    try:
        return parse_include_tier(value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _audit_privacy_override_if_needed(
    *,
    vault_path: Path,
    command: str,
    override: PrivacyTierOverride | None,
    fragment_ids: list[str],
) -> None:
    """Append a privacy-override audit entry when *override* elevates."""
    if not override_elevates(override):
        return
    record_privacy_override(
        vault_path=vault_path,
        command=command,
        fragment_ids=fragment_ids,
        operator=_operator_identity(),
        override=override,
    )


app = typer.Typer(name="creek", help="Creek knowledge organization pipeline")
clean_app = typer.Typer(name="clean", help="Vault hygiene commands")
purge_app = typer.Typer(
    name="purge",
    help="Right-to-be-forgotten deletion operations",
)
skills_app = typer.Typer(
    name="skills",
    help="Voice Skill Tree generation and schema-skill template sync.",
)
app.add_typer(clean_app, name="clean")
app.add_typer(purge_app, name="purge")
app.add_typer(skills_app, name="skills")
console = Console()


def _consent_log_dir(vault_path: Path) -> Path:
    """Resolve the canonical consent log directory beneath a vault.

    Args:
        vault_path: Vault root.

    Returns:
        Path to ``<vault>/00-Creek-Meta/Processing-Log``.
    """
    return vault_path / "00-Creek-Meta" / "Processing-Log"


_OPERATOR_IDENTITY_MAX_LEN = 64
"""Cap on the operator identity recorded in the consent log.

Adversaries with control of ``USER``/``USERNAME`` (e.g. a misconfigured
CI runner) could otherwise inject arbitrary text into the audit
record. Truncating to a reasonable length and filtering to a safe
character set bounds the blast radius without preventing legitimate
unicode usernames.
"""

_OPERATOR_IDENTITY_ALLOWED_CHARS = re.compile(r"[^\w.@+\- ]")
"""Characters stripped from the raw env-var value before logging."""


def _operator_identity() -> str:
    """Return a best-effort, sanitised identity string for the operator.

    Reads ``USER`` then ``USERNAME`` from the environment, strips
    control characters and metacharacters that have no place in an
    audit log entry, truncates to
    :data:`_OPERATOR_IDENTITY_MAX_LEN` characters, and falls back to
    ``"cli"`` when the result is empty.

    Returns:
        Identity string suitable for stamping on a consent record.
    """
    raw = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    cleaned = _OPERATOR_IDENTITY_ALLOWED_CHARS.sub("", raw).strip()
    if not cleaned:
        return "cli"
    return cleaned[:_OPERATOR_IDENTITY_MAX_LEN]


def _format_summary(file_count: int, size_bytes: int) -> str:
    """Render a human-readable size summary for the consent prompt.

    Args:
        file_count: Number of files in the source.
        size_bytes: Aggregate size of those files in bytes.

    Returns:
        A short ``"<N> files, <X> MB"`` style description.
    """
    mb = size_bytes / (1024 * 1024)
    return f"{file_count} file(s), {mb:.1f} MB"


def _gate_consent(
    *,
    source_path: Path,
    vault_path: Path,
    source_type: str,
    assume_yes: bool,
) -> ConsentManager:
    """Prompt for, or auto-grant, consent for processing *source_path*.

    Returns a :class:`ConsentManager` that the pipeline can consult.
    First-time sources display a short summary and require explicit
    confirmation. Non-interactive callers must pass ``--yes``; the
    bypass is recorded as a consent grant with an ``assume_yes``
    operator marker so it can be audited later.

    Args:
        source_path: Source directory the operator wants to ingest.
        vault_path: Vault path used to locate the consent log.
        source_type: Source identifier for the consent record (e.g.
            ``"pipeline"``, ``"markdown"``).
        assume_yes: When ``True``, skip the interactive prompt.

    Returns:
        A :class:`ConsentManager` rooted at the vault's processing log.

    Raises:
        typer.Exit: With code ``1`` when the operator declines, or
            when consent has not been recorded and the caller is
            non-interactive without ``--yes``.
    """
    log_dir = _consent_log_dir(vault_path)
    manager = ConsentManager(log_dir=log_dir)

    if manager.check_consent(source_type, str(source_path)):
        return manager

    if not source_path.exists():
        console.print(f"[red]Source path not found: {source_path}[/red]")
        raise typer.Exit(code=2)

    summary = manager.get_source_summary(source_path, exclusions=[])
    console.print(f"[bold]First time processing {source_path}.[/bold]")
    console.print(
        f"Found: {_format_summary(summary.file_count, summary.total_size_bytes)}."
    )
    if summary.sample_filenames:
        sample = ", ".join(summary.sample_filenames[:5])
        console.print(f"Sample: {sample}")

    if assume_yes:
        manager.record_consent(
            source_type=source_type,
            source_path=str(source_path),
            file_count=summary.file_count,
            exclusions=[],
            operator=f"{_operator_identity()} (assume_yes)",
        )
        console.print(
            "[yellow]Consent auto-granted via --yes; recorded in consent log.[/yellow]",
        )
        return manager

    if not _is_interactive():
        console.print(
            "[red]Non-interactive shell and consent not on file. "
            "Re-run with --yes or run interactively to record consent.[/red]",
        )
        raise typer.Exit(code=1)

    if not typer.confirm("Proceed?", default=False):
        console.print("[yellow]Consent declined; aborting.[/yellow]")
        raise typer.Exit(code=1)

    manager.record_consent(
        source_type=source_type,
        source_path=str(source_path),
        file_count=summary.file_count,
        exclusions=[],
        operator=_operator_identity(),
    )
    console.print("[green]Consent recorded.[/green]")
    return manager


def _is_interactive() -> bool:
    """Return ``True`` when stdin appears to be attached to a TTY.

    The CliRunner used in tests reports stdin as a non-TTY; tests
    therefore use ``--yes`` or ``input=`` to drive prompts. Returning
    ``False`` here prevents ``typer.confirm`` from looping forever in
    pipeline-driven invocations.

    Returns:
        ``True`` when stdin is a terminal.
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _resolve_ingestor(type_name: str) -> type[Ingestor]:
    """Look up an ingestor class by registry key, exiting on miss.

    Args:
        type_name: Registry key (e.g. ``"markdown"``, ``"claude"``).

    Returns:
        The matching :class:`~creek.ingest.base.Ingestor` subclass.

    Raises:
        typer.Exit: With code ``2`` when *type_name* is unknown.
    """
    from creek.ingest import INGESTOR_REGISTRY

    cls = INGESTOR_REGISTRY.get(type_name)
    if cls is None:
        known = ", ".join(sorted(INGESTOR_REGISTRY.keys()))
        if type_name == "gdrive":
            # ARCH-001: gdrive is a downloader, not an ingestor. Direct
            # the operator to the two-stage flow rather than failing
            # with a generic "unknown type" message.
            console.print(
                "[red]gdrive is a downloader, not an ingestor. Run "
                "[bold]creek gdrive --download --staging <dir>[/bold] to mirror "
                "Drive files locally, then point the appropriate ingestor at "
                "the staging directory (e.g. [bold]creek ingest --type document "
                "--input <dir>[/bold] for .docx / .pdf files).[/red]",
            )
        else:
            console.print(
                f"[red]Unknown ingestor type {type_name!r}. Known types: {known}[/red]",
            )
        raise typer.Exit(code=2)
    return cls


def _run_ingest(
    *,
    ingestor_cls: type[Ingestor],
    source_type: str,
    input_path: Path,
    vault_path: Path,
) -> tuple[int, list[str]]:
    """Run a single ingestor and persist its output to the vault.

    Args:
        ingestor_cls: Concrete :class:`Ingestor` subclass to run.
        source_type: Registry key, used to prefix error messages.
        input_path: Source directory or file to ingest.
        vault_path: Vault root for :class:`VaultWriter`.

    Returns:
        Tuple of ``(written_count, errors)`` where ``errors`` is a list
        of human-readable strings prefixed with ``[<source_type>]``.

    Raises:
        typer.Exit: With code ``1`` when the vault cannot be opened.
    """
    from creek.ingest.base import assemble_ingested_fragment
    from creek.vault.writer import VaultWriter

    try:
        writer = VaultWriter(vault_path=vault_path)
    except FileNotFoundError as exc:
        console.print(f"[red]Vault unavailable: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    ingestor = ingestor_cls()
    ingest_result = ingestor.ingest(input_path)

    errors: list[str] = [f"[{source_type}] {err}" for err in ingest_result.errors]
    written = 0
    for parsed in ingest_result.fragments:
        try:
            assembled = assemble_ingested_fragment(parsed)
        except (KeyError, ValueError) as exc:
            errors.append(
                f"[{source_type}] failed to assemble fragment from "
                f"{parsed.source_path}: {exc}",
            )
            continue
        try:
            writer.write_fragment(assembled.fragment, body=assembled.body)
        except (OSError, KeyError) as exc:
            errors.append(
                f"[{source_type}] failed to write {assembled.fragment.id}: {exc}",
            )
            continue
        written += 1

    return written, errors


def _guard_vault_path(vault: Path, allow_in_repo: bool) -> None:
    """Refuse vault paths inside a git repo unless ``--allow-in-repo`` (FEAT-019)."""
    from creek.scaffold import find_enclosing_git_repo

    enclosing = find_enclosing_git_repo(vault)
    if enclosing is None:
        return
    if not allow_in_repo:
        console.print(
            f"[red]{vault} is inside a git repository ({enclosing}). "
            "Personal vault data should not be version-controlled. "
            "Pass --allow-in-repo to override.[/red]",
        )
        raise typer.Exit(code=1)
    console.print(
        f"[yellow]Warning: {vault} is inside a git repository "
        f"({enclosing}). Personal data committed here will be tracked. "
        "Proceeding under --allow-in-repo.[/yellow]",
    )


@app.command()
def init(
    vault: Path = typer.Option(..., "--vault", help="Vault root to initialise"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing creek_config.yaml.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help=(
            "Re-copy canonical templates (ontology, AGENTS.md, schema "
            "skills, scaffold) into an existing vault. User data and "
            "creek_config.yaml are preserved."
        ),
    ),
    allow_in_repo: bool = typer.Option(
        False,
        "--allow-in-repo",
        help=(
            "Allow scaffolding inside a git repository. Off by default "
            "to protect against accidental version-control of personal "
            "vault data (FEAT-019)."
        ),
    ),
) -> None:
    """Scaffold a Creek vault at ``--vault <path>`` (FEAT-019 / ARCH-002).

    Materialises the canonical folder topology, copies the ontology
    spec, AGENTS.md, and the schema-skill tree into the user-chosen
    vault, then writes a starter ``creek_config.yaml`` so the operator
    can edit a real file with their own privacy / redaction / cleaning
    decisions before any ingestion runs.

    The user's vault lives wherever they want it (default suggestion:
    ``~/Obsidian/Creek-Vault/``). It is NEVER inside this repository.
    By default ``creek init`` refuses to scaffold inside a git repo;
    pass ``--allow-in-repo`` to override.

    ``--refresh`` re-copies canonical material into an existing vault
    without touching user-edited content or ``creek_config.yaml``.
    """
    from creek.config import generate_default_config
    from creek.scaffold import deploy_canonical

    _guard_vault_path(vault, allow_in_repo)

    config_dir = vault / "00-Creek-Meta"
    config_path = config_dir / "creek_config.yaml"

    if not refresh and config_path.exists() and not force:
        console.print(
            f"[yellow]{config_path} already exists; pass --force to "
            "overwrite or --refresh to update canonical templates only.[/yellow]",
        )
        raise typer.Exit(code=1)

    result = deploy_canonical(vault)

    if not refresh:
        generate_default_config(config_path)
        console.print(
            f"[bold green]Vault scaffolded at {vault}.[/bold green] "
            f"(folders ensured: {result.folders_ensured}, "
            f"skills: {result.skills_synced})",
        )
        console.print(
            "[dim]Edit [bold]<vault>/00-Creek-Meta/creek_config.yaml[/bold] "
            "before running [bold]creek process[/bold] — the defaults are "
            "intentionally cautious.[/dim]",
        )
    else:
        console.print(
            f"[bold green]Refreshed canonical templates in {vault}.[/bold green] "
            f"(skills: {result.skills_synced})",
        )


@app.command()
def process(
    source: Path | None = typer.Option(None, help="Source directory to process"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the consent prompt for first-time sources (logged).",
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help=(
            "Skip Pass 3 (LLM classification). Pass 1 (deterministic) "
            "and Pass 2 (local model: embeddings, OCR) still run; "
            "residue is reported but never sent to Anthropic / Ollama. "
            "Wins over LLMConfig.provider — safe to combine with "
            "provider: anthropic for an audited zero-egress run."
        ),
    ),
) -> None:
    """Run the full pipeline: redact, ingest, classify, link, index.

    Aborts with a remediation hint if the redaction scanner finds
    unresolved sensitive matches; run ``creek redact --apply`` first to
    clear them. Per-source consent is enforced — first-time sources
    prompt for confirmation before ingestion. Use ``--yes`` to skip the
    prompt in non-interactive contexts (the bypass is logged).

    Pass ``--no-llm`` to run the deterministic and local-model passes
    end-to-end without ever invoking Pass 3 (LLM classification). The
    pre-LLM yield line emitted at the end of the run reports the
    deterministic / local-model / residue counts.
    """
    config = load_config()
    source_path = source or config.source_drive
    vault_path = vault or config.vault_path

    console.print(
        f"[bold green]Running full pipeline: "
        f"source={source_path}, vault={vault_path}"
        f"{' (no-LLM)' if no_llm else ''}[/bold green]"
    )

    consent_manager = _gate_consent(
        source_path=source_path,
        vault_path=vault_path,
        source_type="pipeline",
        assume_yes=yes,
    )

    pipeline = Pipeline(
        config=config,
        consent_manager=consent_manager,
        no_llm=no_llm,
    )
    try:
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
    except RedactionRequiredError as exc:
        console.print(f"[red]Redaction gate: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Files scanned:[/bold] {result.files_scanned}")
    console.print(f"[bold]Fragments created:[/bold] {result.fragments_created}")
    console.print(f"[bold]Classifications made:[/bold] {result.classifications_made}")
    console.print(f"[bold]Links found:[/bold] {result.links_found}")
    console.print(f"[bold]Indexes generated:[/bold] {result.indexes_generated}")
    console.print(
        f"[bold]Deterministic:[/bold] {result.deterministic_classified} classified | "
        f"[bold]Local-model:[/bold] {result.local_model_processed} embedded/OCR'd | "
        f"[bold]Residue:[/bold] {result.residue} "
        "(would go to LLM if Pass-3 enabled)"
    )
    error_count = len(result.errors)
    error_style = "red" if error_count else "dim"
    console.print(f"[bold {error_style}]Errors:[/bold {error_style}] {error_count}")
    for err in result.errors:
        console.print(f"  [dim]{err}[/dim]")


@app.command()
def ingest(
    type: str | None = typer.Option(None, "--type", help="Source type to ingest"),
    input: Path | None = typer.Option(None, "--input", help="Input path"),
    vault: Path | None = typer.Option(None, "--vault", help="Obsidian vault path"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the consent prompt for first-time sources (logged).",
    ),
) -> None:
    """Ingest a specific source type into the vault.

    Resolves ``--type`` against
    :data:`creek.ingest.INGESTOR_REGISTRY`, runs the matching ingestor
    against ``--input``, and writes each produced fragment via
    :class:`~creek.vault.writer.VaultWriter`. Re-running against the same
    input is idempotent: deterministic fragment IDs ensure existing
    files are recognised and skipped.
    """
    if type is None or input is None:
        console.print("[red]--type and --input are required.[/red]")
        raise typer.Exit(code=2)

    config = load_config()
    vault_path = vault or config.vault_path
    ingestor_cls = _resolve_ingestor(type)

    if not input.exists():
        console.print(f"[red]Input path not found: {input}[/red]")
        raise typer.Exit(code=2)

    _gate_consent(
        source_path=input,
        vault_path=vault_path,
        source_type=type,
        assume_yes=yes,
    )

    written, errors = _run_ingest(
        ingestor_cls=ingestor_cls,
        source_type=type,
        input_path=input,
        vault_path=vault_path,
    )

    console.print(f"[bold green]Ingested {written} fragment(s).[/bold green]")
    if errors:
        console.print(f"[yellow]Errors: {len(errors)}[/yellow]")
        for err in errors:
            console.print(f"  [dim]{err}[/dim]")


def _dispatch_redact(
    *,
    scan: bool,
    apply: bool,
    review: bool,
    source: Path | None,
    vault: Path | None,
    report: bool,
    dry_run: bool,
    verbose: bool,
    assume_yes: bool,
) -> None:
    """Route the ``redact`` command to the selected mode handler.

    The caller (:func:`redact`) guarantees that exactly one of ``scan``,
    ``apply``, or ``review`` is ``True`` before invocation, so the final
    ``review`` branch is reached by elimination. The invariant is
    re-asserted defensively below to keep the coupling explicit.

    Args:
        scan: ``--scan`` flag.
        apply: ``--apply`` flag.
        review: ``--review`` flag.
        source: ``--source`` path (scan/apply).
        vault: ``--vault`` path (review).
        report: ``--report`` flag (scan).
        dry_run: ``--dry-run`` flag (apply).
        verbose: ``--verbose`` flag.
        assume_yes: ``--yes`` flag (apply).
    """
    from creek.redact.cli_commands import (
        require_flag,
        run_apply,
        run_review,
        run_scan,
    )

    if scan:
        src = require_flag(source, "--scan", "--source", console)
        run_scan(src, report=report, verbose=verbose, console=console)
        return
    if apply:
        src = require_flag(source, "--apply", "--source", console)
        run_apply(
            src,
            dry_run=dry_run,
            verbose=verbose,
            assume_yes=assume_yes,
            console=console,
            vault=vault,
        )
        return
    # Invariant: redact() rejects any flag combination where review is
    # not the sole mode flag, so reaching this branch implies review=True.
    if not review:  # pragma: no cover — defence in depth
        msg = "dispatch reached without a mode flag"
        raise RuntimeError(msg)
    vlt = require_flag(vault, "--review", "--vault", console)
    run_review(vlt, verbose=verbose, console=console)


@app.command()
def redact(
    scan: bool = typer.Option(False, "--scan", help="Scan for sensitive content"),
    apply: bool = typer.Option(
        False, "--apply", help="Apply redactions to matched files"
    ),
    review: bool = typer.Option(
        False, "--review", help="Render the review queue for a vault"
    ),
    source: Path | None = typer.Option(
        None, "--source", help="Source path (scan/apply)"
    ),
    vault: Path | None = typer.Option(None, "--vault", help="Vault path (review)"),
    report: bool = typer.Option(
        False, "--report", help="Include the detailed markdown report (scan)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Scan but do not modify files (apply)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show per-match details"
    ),
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (apply)"
    ),
) -> None:
    """Scan for sensitive data, apply redactions, or review the queue.

    Exactly one of ``--scan``, ``--apply``, or ``--review`` must be given.
    """
    if sum([scan, apply, review]) != 1:
        console.print("[red]Specify exactly one of --scan, --apply, --review.[/red]")
        raise typer.Exit(code=2)
    _dispatch_redact(
        scan=scan,
        apply=apply,
        review=review,
        source=source,
        vault=vault,
        report=report,
        dry_run=dry_run,
        verbose=verbose,
        assume_yes=assume_yes,
    )


_CLASSIFY_METHODS = ("rules", "llm")
_LINK_METHODS = ("embeddings", "temporal", "eddies")


@app.command()
def classify(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option("rules", help="Classification method (rules|llm)"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite fragments classified as method: manual.",
    ),
) -> None:
    """Run classification on existing vault fragments.

    ``--method rules`` runs the keyword-based classifier locally.
    ``--method llm`` calls the configured LLM provider for any
    fragment the rules left unclassified or below
    ``classification.confidence_threshold``. Fragments with
    ``classification.method: manual`` are preserved unless ``--force``
    is supplied.

    LLM concurrency is governed by ``llm.max_concurrent`` in the
    config, not a CLI flag.
    """
    if method not in _CLASSIFY_METHODS:
        console.print(
            f"[red]Unknown method {method!r}. "
            f"Supported: {', '.join(_CLASSIFY_METHODS)}.[/red]",
        )
        raise typer.Exit(code=2)

    config = load_config()
    vault_path = _resolve_vault(vault)

    from creek.classify.classify_engine import run_classify

    summary = run_classify(
        vault_path=vault_path,
        config=config,
        method=method,
        force=force,
    )
    console.print(
        f"[bold green]Classified {summary.classified} of "
        f"{summary.total} fragment(s) "
        f"({summary.preserved_manual} manual preserved, "
        f"{summary.skipped_high_confidence} skipped).[/bold green]",
    )
    if summary.errors:
        console.print(f"[yellow]Errors: {len(summary.errors)}[/yellow]")
        for err in summary.errors:
            console.print(f"  [dim]{err}[/dim]")


@app.command()
def link(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option(
        "embeddings",
        help="Linking method (embeddings|temporal|eddies)",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Invalidate the embeddings cache and recompute from scratch.",
    ),
) -> None:
    """Run a single linker stage against the vault.

    Loads every fragment from ``<vault>/01-Fragments/``, runs the chosen
    linker, and writes the resulting links back to fragment frontmatter
    (and any thread/eddy notes). ``--rebuild`` invalidates the cached
    embeddings file before running ``--method embeddings`` so the
    similarity matrix is recomputed from scratch.
    """
    if method not in _LINK_METHODS:
        console.print(
            f"[red]Unknown method {method!r}. "
            f"Supported: {', '.join(_LINK_METHODS)}.[/red]",
        )
        raise typer.Exit(code=2)

    config = load_config()
    vault_path = _resolve_vault(vault)

    from creek.link.link_engine import run_link

    summary = run_link(
        vault_path=vault_path,
        config=config,
        method=method,
        rebuild=rebuild,
    )
    console.print(
        f"[bold green]{method.capitalize()} linker: "
        f"{summary.fragment_count} fragment(s), "
        f"{summary.link_count} link(s).[/bold green]",
    )


@app.command(name="compile")
def compile_(
    fragment_id: str = typer.Argument(..., help="Source fragment ID to roll up"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    target_kind: str = typer.Option(
        "thread",
        "--target-kind",
        help="Compiled-page surface (thread|eddy|frequency_index)",
    ),
    target_id: str = typer.Option(
        ...,
        "--target-id",
        help="Stable ID of the compiled page (e.g. thread-systems)",
    ),
    target_title: str = typer.Option(
        ...,
        "--target-title",
        help="Human-readable title for the compiled page",
    ),
) -> None:
    """Roll a fragment up into a compiled-layer page (FEAT-003).

    Reads the source fragment from ``<vault>/01-Fragments``, calls the
    configured LLM to synthesise claims with per-claim provenance back
    to the fragment ID, and writes the result to the appropriate
    compiled-layer directory. LLM-detected paradoxes are routed to the
    side-channel log under ``00-Creek-Meta/Processing-Log/`` rather
    than flattened into the synthesis page.
    """
    from creek.compile.engine import TARGET_KINDS, _default_llm, compile_to_vault

    if target_kind not in TARGET_KINDS:
        console.print(
            f"[red]Unknown --target-kind {target_kind!r}. "
            f"Supported: {', '.join(TARGET_KINDS)}.[/red]",
        )
        raise typer.Exit(code=2)

    config = load_config()
    vault_path = _resolve_vault(vault)
    kind = cast("CompileTargetKind", target_kind)
    written = compile_to_vault(
        fragment_ids=[fragment_id],
        vault_path=vault_path,
        target_kind=kind,
        target_id=target_id,
        target_title=target_title,
        llm=_default_llm(config.llm),
    )
    console.print(
        f"[bold green]Compiled {fragment_id} -> {written}[/bold green]",
    )


def _report_tags(vault_path: Path) -> None:
    """Generate the tag-garden report."""
    from creek.generate.tags import TagGardenGenerator

    path = TagGardenGenerator(vault_path=vault_path).generate_garden()
    console.print(f"[bold green]Tag Garden generated: {path}[/bold green]")


def _report_unnamed(vault_path: Path) -> None:
    """Generate the weekly unnamed-fragment digest."""
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from creek.generate.unnamed import UnnamedDigestGenerator
    from creek.link.embeddings import EmbeddingLinker

    config = load_config()
    linker = EmbeddingLinker(config=config.embeddings)
    digest_generator = UnnamedDigestGenerator(embedding_linker=linker)
    today = _date.today()
    week_start = today - _timedelta(days=today.weekday())
    digest_path = digest_generator.generate_weekly_digest(vault_path, week_start)
    console.print(
        f"[bold green]Unnamed digest generated: {digest_path}[/bold green]",
    )


def _report_voice(vault_path: Path) -> None:
    """Generate per-register voice profiles, if exemplars exist."""
    from creek.generate.voice import VoiceProfileGenerator

    profile_paths = VoiceProfileGenerator().generate_all_profiles(vault_path)
    if not profile_paths:
        console.print(
            "[yellow]No voice profiles generated: "
            "no qualifying exemplars found.[/yellow]",
        )
        return
    names = ", ".join(path.stem for path in profile_paths)
    console.print(
        f"[bold green]Voice profiles generated ({len(profile_paths)}): "
        f"{names}[/bold green]",
    )


def _report_wavelength(vault_path: Path, period: str | None) -> None:
    """Generate weekly or monthly wavelength reports."""
    from datetime import date as _date

    from creek.generate.wavelength import WavelengthTracker

    if period not in {"weekly", "monthly"}:
        console.print(
            "[red]--period must be 'weekly' or 'monthly' for wavelength reports.[/red]",
        )
        raise typer.Exit(code=2)
    tracker = WavelengthTracker()
    today = _date.today()
    if period == "weekly":
        wavelength_path = tracker.generate_weekly_report(vault_path, week_of=today)
    else:
        wavelength_path = tracker.generate_monthly_report(vault_path, month=today)
    console.print(
        f"[bold green]Wavelength {period} report generated: "
        f"{wavelength_path}[/bold green]",
    )


_REPORT_DISPATCH: dict[str, Callable[[Path], None]] = {
    "tags": _report_tags,
    "unnamed": _report_unnamed,
    "voice": _report_voice,
}


@app.command()
def report(
    type: str | None = typer.Option(None, help="Report type"),
    period: str | None = typer.Option(None, help="Report period"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_INCLUDE_TIER_HELP,
    ),
) -> None:
    """Generate reports on vault state.

    Privacy override audit: ``report`` flows iterate vault content
    internally (per-week digests, per-register profiles, …) rather
    than operating on a caller-supplied fragment list, so the audit
    entry is **invocation-level** and intentionally carries an empty
    ``fragment_ids`` list. The ``command`` field encodes the report
    type (e.g. ``"report.unnamed"``) so operators can still trace
    *which* report was elevated, even though per-fragment scope is
    not available. ``mine`` and ``draft`` audit *after* the handler
    runs because the IDs are derived from mining seeds.
    """
    config = load_config()
    vault_path = vault or config.vault_path
    override = _parse_include_tier(include_tier)
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command=f"report.{type}" if type else "report",
        override=override,
        fragment_ids=[],
    )

    handler = _REPORT_DISPATCH.get(type or "")
    if handler is not None:
        handler(vault_path)
        return
    if type == "wavelength":
        _report_wavelength(vault_path, period)
        return
    console.print(
        f"[bold green]Would report: type={type}, "
        f"period={period}, vault={vault_path}[/bold green]",
    )


@app.command()
def state(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Render ``00-Creek-Meta/State/<iso-week>.md`` audit report (FEAT-006).

    The command is a *view* over the compiled vault layer — it never
    re-runs classification, linking, or compile. It reads existing
    fragments, threads, eddies, praxis, synchronicities, and the most
    recent ``run-summary.jsonl`` line, and writes a single markdown
    document organised in seven sections (vault summary, pre-LLM yield,
    active eddies, active threads, surprising connections, hyperedges,
    drift warnings). ``latest.md`` next to the ISO-week file always
    points at the most recent report.
    """
    from creek.generate.state import StateReportGenerator

    vault_path = _resolve_vault(vault)
    written = StateReportGenerator(vault_path=vault_path).write()
    console.print(f"[bold green]State report written: {written}[/bold green]")


@app.command(name="state-budget")
def state_budget(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Verify ``00-Creek-Meta/State/latest.md`` is within its size budget (FEAT-007).

    The audit report is the session-start context for CrawDad and Claude
    Code — it must fit in a single context window. This command checks
    the rendered ``latest.md`` against the 50,000-token budget and exits
    non-zero when the budget is exceeded. A missing report (e.g. CI
    without a populated vault) is treated as a pass.
    """
    from creek.generate.state_budget import check_budget

    vault_path = _resolve_vault(vault)
    latest = vault_path / "00-Creek-Meta" / "State" / "latest.md"
    result = check_budget(latest)
    if result.ok:
        console.print(f"[bold green]{result.message}[/bold green]")
        return
    console.print(f"[bold red]{result.message}[/bold red]")
    raise typer.Exit(code=1)


@app.command()
def lint(
    check: list[str] | None = typer.Option(
        None,
        "--check",
        help=(
            "Run only this check (repeatable). Names: paradox, unnamed, "
            "synchronicity, compost, tags, broken-links, orphan-compiled, "
            "skill-size."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Incremental window: 7d, 1w, 1mo, 30d.",
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Run unified vault hygiene checks (FEAT-008).

    Default behaviour runs all deterministic checks. Pass ``--since`` to
    also run the semantic checks (paradox, synchronicity, unnamed) over
    the same window. Pass one or more ``--check NAME`` to run only the
    named checks. Lint never resolves paradoxes, never auto-creates
    compiled pages, and never deletes orphan fragments — those are the
    load-bearing rules pinned by FEAT-008.
    """
    from creek.lint import LintRunner, parse_since

    config = load_config()
    vault_path = vault or config.vault_path
    since_dt = None
    if since is not None:
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
    runner_ = LintRunner(
        vault_path=vault_path,
        since=since_dt,
        since_text=since,
    )
    try:
        report = runner_.run(checks=list(check) if check else None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    written = runner_.write(report)
    console.print(f"[bold green]Lint report written: {written}[/bold green]")


@app.command()
def review(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Print pending review queue items and exit (non-interactive).",
    ),
) -> None:
    """Walk the review queue and persist accept/override/defer decisions.

    Prints each fragment that needs review, then prompts for one of:

    * ``a`` — accept the current classification (writes
      ``classification.method: manual``).
    * ``o`` — override; the operator types a new primary frequency.
    * ``d`` — defer (skip without changes).
    * ``q`` — quit.

    Pass ``--list`` to dump the queue without prompting; useful in CI
    or when scripting around the queue.
    """
    vault_path = _resolve_vault(vault)

    from creek.classify.review_runner import (
        ReviewQueueRunner,
        format_review_summary,
    )

    runner = ReviewQueueRunner(vault_path=vault_path, console=console)
    pending = runner.list_pending()

    if not pending:
        console.print("[green]Review queue is empty.[/green]")
        return

    if list_only:
        for entry in pending:
            console.print(format_review_summary(entry))
        return

    summary = runner.run_interactive(pending)
    console.print(
        f"[bold green]Review complete: {summary.accepted} accepted, "
        f"{summary.overridden} overridden, {summary.deferred} deferred.[/bold green]",
    )
    if summary.errors:
        console.print(f"[yellow]Errors: {len(summary.errors)}[/yellow]")
        for err in summary.errors:
            console.print(f"  [dim]{err}[/dim]")


def _gdrive_revoke() -> None:
    """Run the SEC-008 OAuth token revocation flow and report the outcome.

    Loads the configured token-file path, delegates to
    :func:`creek.ingest.gdrive.revoke_token`, then prints a clear
    summary so the operator can see whether a follow-up step (visiting
    Google's revocation page manually) is required.
    """
    from creek.ingest.gdrive import revoke_token

    config = load_config()
    result = revoke_token(config.google_drive)
    token_path = config.google_drive.token_file
    if not result.token_file_existed:
        console.print(
            f"[yellow]No cached token at {token_path}; nothing to revoke.[/yellow]",
        )
        return
    if result.token_file_removed:
        console.print(f"[green]Token file removed: {token_path}[/green]")
    else:
        console.print(
            f"[red]Could not remove token file at {token_path}; "
            "check filesystem permissions.[/red]",
        )
    if result.remote_revoked:
        console.print("[green]Remote token revoked at Google.[/green]")
    else:
        # `revoke_token` always populates `result.error` when
        # `remote_revoked` is False, so no `or`-fallback is needed.
        console.print(
            f"[yellow]Local token erased, but remote revocation did not "
            f"confirm: {result.error}. Visit "
            f"https://myaccount.google.com/permissions to revoke "
            f"manually if needed.[/yellow]",
        )


@app.command()
def gdrive(
    download: bool = typer.Option(False, help="Download from Google Drive"),
    revoke: bool = typer.Option(
        False,
        "--revoke",
        help="Revoke the cached OAuth token and delete the local token file",
    ),
    staging: Path | None = typer.Option(None, help="Staging directory"),
) -> None:
    """Download files from Google Drive or revoke the cached OAuth token.

    ``--download`` mirrors files into a local staging directory
    (read-only; subsequent runs are incremental). ``--revoke`` runs the
    SEC-008 hygiene path: it best-effort calls Google's revocation
    endpoint, then erases the local token file with a zero-byte pass
    before unlinking. The two flags are mutually exclusive.

    First ``--download`` run opens a browser for OAuth authorisation;
    the refresh token is cached at ``GoogleDriveConfig.token_file``
    (mode ``0o600``) so subsequent runs are non-interactive.
    """
    if download and revoke:
        console.print(
            "[red]Specify exactly one of --download or --revoke.[/red]",
        )
        raise typer.Exit(code=2)
    if revoke:
        _gdrive_revoke()
        return
    if not download:
        console.print(
            "[yellow]Nothing to do. Pass --download or --revoke.[/yellow]",
        )
        return

    config = load_config()
    staging_dir = (
        staging if staging is not None else Path(config.google_drive.staging_dir)
    )

    from creek.ingest.gdrive import GoogleApiDriveClient, GoogleDriveDownloader

    client = GoogleApiDriveClient(config.google_drive)
    if not client.is_available():
        console.print(
            "[red]Google API client unavailable; cannot download from Drive. "
            "Install with `pip install google-api-python-client "
            "google-auth-oauthlib`.[/red]",
        )
        raise typer.Exit(code=1)

    downloader = GoogleDriveDownloader(client=client, config=config.google_drive)
    try:
        result = downloader.download_all(staging_dir)
    except Exception as exc:
        # Drive surface includes GoogleApiUnavailableError (missing
        # optional deps), HttpError (quota / rate limit / revoked
        # token), network IOErrors, and OAuth failures. We can't
        # import googleapiclient.errors at module top-level since
        # it's optional, so catch broadly here and present a clean
        # message rather than a raw traceback.
        console.print(f"[red]Google Drive download failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold green]Downloaded {len(result.downloaded)} / "
        f"Skipped {len(result.skipped)} (unchanged) files to "
        f"{staging_dir}[/bold green]",
    )


@skills_app.command("generate")
def skills_generate(
    generate: bool = typer.Option(False, help="Generate voice skill files"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    output: Path | None = typer.Option(None, help="Output path"),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_INCLUDE_TIER_HELP,
    ),
) -> None:
    """Generate the Voice Skill Tree (Section 11.4).

    Writes a tree of ``SKILL.md`` files under *output* (default
    ``<vault>/creek-skills``) covering frequencies, phases, modes,
    registers, threads, eddies, and two meta skills.
    """
    override = _parse_include_tier(include_tier)
    # Skill tree generation already excludes intimate exemplars; the
    # override is recorded as an explicit operator decision rather
    # than mutating downstream behaviour. _audit_privacy_override_if_
    # needed already short-circuits for None / OPEN via override_elev-
    # ates, so there is no extra guard to write at this call site.
    _audit_privacy_override_if_needed(
        vault_path=_resolve_vault(vault),
        command="skills",
        override=override,
        fragment_ids=[],
    )

    if not generate:
        console.print(
            "[yellow]Pass --generate to create the Voice Skill Tree.[/yellow]",
        )
        return

    from creek.generate.skills import SkillTreeGenerator

    vault_path = _resolve_vault(vault)
    output_dir = output if output is not None else vault_path / "creek-skills"
    written = SkillTreeGenerator().generate_all_skills(vault_path, output_dir)
    console.print(
        f"[bold green]Voice Skill Tree generated ({len(written)} files) "
        f"at {output_dir}[/bold green]",
    )


@skills_app.command("sync")
def skills_sync(
    vault: Path = typer.Option(
        ...,
        "--vault",
        help="Vault root whose schema-skill tree should be re-deployed.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite locally-modified skill files.",
    ),
) -> None:
    """Re-deploy the canonical schema-skill tree into ``<vault>/00-Creek-Meta/Skills/``.

    Pulls upstream changes from
    ``creek-tools/creek/templates/skills/*.SKILL.md`` after upgrading
    ``creek-tools``. Local edits to deployed skill files block the
    overwrite unless ``--force`` is passed.
    """
    from creek.scaffold import deploy_skills, detect_drifted_skills

    drifted = detect_drifted_skills(vault)
    if drifted and not force:
        names = ", ".join(p.name for p in drifted)
        console.print(
            f"[red]Refusing to sync: local changes detected in "
            f"{len(drifted)} skill file(s): {names}. "
            "Pass --force to overwrite.[/red]",
        )
        raise typer.Exit(code=1)

    synced = deploy_skills(vault)
    console.print(
        f"[bold green]Synced {synced} skill file(s) to "
        f"{vault / '00-Creek-Meta' / 'Skills'}.[/bold green]",
    )


def _warn_bypass_compiled(verb: str) -> None:
    """Emit a stderr warning when ``--bypass-compiled`` is set.

    The escape hatch lets an operator side-step the compiled-layer
    routing introduced in FEAT-004. Warning loudly is the contract:
    bypass should be a deliberate, visible choice, not silent.
    """
    print(  # operator-facing CLI warning
        f"[creek {verb}] WARNING: --bypass-compiled is set; "
        "the compiled-layer-first contract is being side-stepped.",
        file=sys.stderr,
    )


def _parse_phase(phase: str) -> Phase:
    """Parse a phase CLI argument, exiting with code 2 on unknown values.

    Args:
        phase: Raw phase string from the CLI.

    Returns:
        The parsed :class:`Phase` enum member.
    """
    from creek.models import Phase as _Phase

    try:
        return _Phase(phase.lower())
    except ValueError:
        console.print(
            f"[red]Unknown phase '{phase}'. "
            "Use one of: rising, peaking, withdrawal, diminishing, "
            "bottoming_out, restoration, unclassified.[/red]",
        )
        raise typer.Exit(code=2) from None


@app.command()
def mine(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    phase: str = typer.Option(
        "unclassified",
        help="Current Archetypal Wavelength phase (rising, peaking, ...).",
    ),
    limit: int = typer.Option(
        10,
        help="Maximum number of seeds to display (0 for all).",
    ),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_INCLUDE_TIER_HELP,
    ),
    bypass_compiled: bool = typer.Option(
        False,
        "--bypass-compiled",
        help=(
            "Skip the compiled layer and read fragments directly. "
            "Documented escape hatch — emits a stderr warning."
        ),
    ),
) -> None:
    """Mine blog and essay ideas from the vault (Section 11.5).

    Runs every strategy - liminal cross-eddy, thread terminus, resonance
    chain, and wavelength-phase window - then prints a deduped,
    score-ranked table of :class:`IdeaSeed` records.

    By default the miner routes through the compiled layer first
    (Threads, Eddies, Frequency indexes) and falls back to fragments
    only when a compiled page is missing — appending a
    ``compile-needed`` entry to ``compile-gaps.jsonl`` for ``creek
    lint`` to surface later (FEAT-004).
    """
    from creek.generate.mining import IdeaMiner

    vault_path = _resolve_vault(vault)
    current_phase = _parse_phase(phase)
    override = _parse_include_tier(include_tier)
    if bypass_compiled:
        _warn_bypass_compiled("mine")
    seeds = IdeaMiner(
        privacy_override=override,
        bypass_compiled=bypass_compiled,
    ).mine_all(
        vault_path,
        current_phase=current_phase,
    )
    fragment_ids = sorted({fid for seed in seeds for fid in seed.source_fragments})
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="mine",
        override=override,
        fragment_ids=fragment_ids,
    )
    if not seeds:
        console.print("[yellow]No idea seeds surfaced.[/yellow]")
        return

    display = seeds if limit <= 0 else seeds[:limit]
    table = Table(title=f"Idea seeds ({len(display)} of {len(seeds)})")
    table.add_column("Strategy")
    table.add_column("Title")
    table.add_column("Score", justify="right")
    for seed in display:
        table.add_row(seed.strategy.value, seed.title, f"{seed.score:.2f}")
    console.print(table)


def _read_voice_core(path: Path | None) -> str:
    """Read a voice-core text file, exiting 2 on read errors.

    Args:
        path: Optional path to a voice-core text file.

    Returns:
        The file contents, or the empty string when *path* is ``None``.

    Raises:
        typer.Exit: With code 2 when the file cannot be read.
    """
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not read --voice-core {path}: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _build_draft_llm() -> DraftLLM:
    """Construct a :data:`DraftLLM` callable from the configured LLM provider.

    Uses the Ollama/Anthropic adapter already wired up for classification.

    Returns:
        A callable ``(prompt) -> response`` ready to feed to
        :class:`DraftGenerator`.

    Raises:
        typer.Exit: If the configured LLM provider is not reachable.
    """
    from creek.classify.llm import LLMClassifier

    config = load_config()
    classifier = LLMClassifier(config.llm)
    if not classifier.available:
        console.print(
            "[red]LLM provider unavailable; cannot generate draft. "
            "Check Ollama or ANTHROPIC_API_KEY configuration.[/red]",
        )
        raise typer.Exit(code=1)
    return classifier.invoke_prompt


@app.command()
def draft(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    skills_root: Path | None = typer.Option(
        None,
        "--skills-root",
        help="Skill tree root (default <vault>/creek-skills).",
    ),
    phase: str = typer.Option(
        "unclassified",
        help="Current Archetypal Wavelength phase.",
    ),
    index: int = typer.Option(
        0,
        "--index",
        help="Pick the Nth mined idea (0-based, score-ranked).",
    ),
    voice_core: Path | None = typer.Option(
        None,
        "--voice-core",
        help="Path to a voice-core text file prepended to the prompt.",
    ),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_INCLUDE_TIER_HELP,
    ),
    bypass_compiled: bool = typer.Option(
        False,
        "--bypass-compiled",
        help=(
            "Skip the compiled layer when gathering source material. "
            "Documented escape hatch — emits a stderr warning."
        ),
    ),
) -> None:
    """Draft an essay from a mined idea with the activated skill stack.

    Mines ideas, presents the chosen idea as an invitation, assembles
    the frequency/phase/mode/register skill stack, gathers source
    material, asks the LLM to generate a draft, and saves it to
    ``07-Voice/Drafts/`` with full provenance.
    """
    from creek.generate.drafts import DraftGenerator
    from creek.generate.mining import IdeaMiner

    vault_path = _resolve_vault(vault)
    skills_dir = skills_root if skills_root is not None else vault_path / "creek-skills"
    current_phase = _parse_phase(phase)
    voice_text = _read_voice_core(voice_core)
    override = _parse_include_tier(include_tier)
    llm = _build_draft_llm()
    if bypass_compiled:
        _warn_bypass_compiled("draft")

    seeds = IdeaMiner(
        privacy_override=override,
        bypass_compiled=bypass_compiled,
    ).mine_all(
        vault_path,
        current_phase=current_phase,
    )
    if not seeds:
        console.print("[yellow]No idea seeds surfaced; nothing to draft.[/yellow]")
        return
    if index < 0 or index >= len(seeds):
        console.print(
            f"[red]--index {index} is out of range (0..{len(seeds) - 1}).[/red]",
        )
        raise typer.Exit(code=2)

    idea = seeds[index]
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="draft",
        override=override,
        fragment_ids=list(idea.source_fragments),
    )
    generator = DraftGenerator(
        llm=llm,
        skills_root=skills_dir,
        voice_core=voice_text,
        privacy_override=override,
        bypass_compiled=bypass_compiled,
    )

    console.print(generator.present_idea(idea))
    draft_obj = generator.generate_draft(
        idea,
        vault_path=vault_path,
        current_phase=current_phase,
    )
    saved_path = generator.save_draft(draft_obj, vault_path)
    console.print(f"[bold green]Draft saved: {saved_path}[/bold green]")


# ---------------------------------------------------------------------------
# `creek save` — answer-filing-back primitive (FEAT-009)
# ---------------------------------------------------------------------------


_SAVE_TARGET_HELP = (
    "Destination type: thread, eddy, praxis, paradox, unnamed, or draft. "
    "Paradox always routes to 10-Liminal/Paradoxes/ regardless of other inputs."
)
_SAVE_TIER_HELP = (
    "Privacy tier (open|personal|intimate). Required when stdin is the body "
    "source; defaults to the source fragments' max tier when --provenance "
    "is supplied."
)
_SAVE_SOURCE_KINDS: tuple[str, ...] = (
    "discord",
    "claude-session",
    "manual",
    "mcp",
)
"""Allowed ``--source-kind`` values; mirrors the ``saved_from`` schema.

Validated at the CLI boundary (rather than typing :class:`SaveRequest`
as a ``Literal``) so the CLI exits 2 with a clear error listing the
accepted values, consistent with how ``--target`` and ``--tier`` are
parsed. A stricter type on the dataclass would silently accept any
string at the library boundary, which is exactly what the reviewer
flagged before the MCP surface (FEAT-016) goes live."""


def _parse_save_source_kind(value: str) -> str:
    """Validate ``--source-kind`` or exit 2 with a clear listing."""
    if value in _SAVE_SOURCE_KINDS:
        return value
    options = ", ".join(_SAVE_SOURCE_KINDS)
    console.print(
        f"[red]Unknown --source-kind {value!r}. Supported: {options}.[/red]",
    )
    raise typer.Exit(code=2)


def _read_save_body(body_arg: str | None) -> tuple[str, bool]:
    """Return ``(body_text, came_from_stdin)`` for the ``--body`` option.

    ``--body`` accepts a path or ``-`` (explicit stdin); omitting it
    also reads from stdin. A non-existent path is a hard error rather
    than a silent inline fallback — for a privacy-sensitive filing
    tool, filing the path string itself when the operator mistyped a
    file path is dangerously confusing (the resulting note's body is
    the path, and the operator believes their answer was filed).
    """
    if body_arg is None or body_arg == "-":
        return sys.stdin.read(), True
    body_path = Path(body_arg)
    if not body_path.exists():
        console.print(
            f"[red]--body path does not exist: {body_arg}. "
            "Pass '-' (or omit --body) to read the body from stdin.[/red]",
        )
        raise typer.Exit(code=2)
    return body_path.read_text(encoding="utf-8"), False


def _parse_save_target(value: str) -> SaveTarget:
    """Parse the ``--target`` value or exit 2 with a clear listing."""
    try:
        return SaveTarget(value)
    except ValueError as exc:
        options = ", ".join(member.value for member in SaveTarget)
        console.print(
            f"[red]Unknown --target {value!r}. Supported: {options}.[/red]",
        )
        raise typer.Exit(code=2) from exc


def _parse_save_tier(value: str | None) -> PrivacyTier | None:
    """Parse ``--tier`` into a :class:`PrivacyTier`, or ``None`` if unset."""
    if value is None:
        return None
    try:
        return PrivacyTier(value)
    except ValueError as exc:
        options = ", ".join(
            member.value for member in PrivacyTier if member != PrivacyTier.UNCLASSIFIED
        )
        console.print(
            f"[red]Unknown --tier {value!r}. Supported: {options}.[/red]",
        )
        raise typer.Exit(code=2) from exc


def _warn_if_paradox_downgrades_tier(
    target: SaveTarget,
    tier: PrivacyTier | None,
) -> None:
    """Print a stderr warning when paradox saves silently widen the tier.

    Per the FEAT, ``--target paradox`` always lands in
    ``10-Liminal/Paradoxes/`` with the body filtered as ``open`` —
    *the fact* of the contradiction is what's preserved, not a
    tier-protected summary. A user who passes ``--tier intimate`` or
    ``--tier personal`` for protection is likely surprised when the
    body lands in the vault unredacted, so we surface the override
    explicitly rather than letting it pass silently.
    """
    if target != SaveTarget.PARADOX:
        return
    if tier is None or tier == PrivacyTier.OPEN:
        return
    console.print(
        f"[yellow]Note: --target paradox forces tier=open for the body; "
        f"--tier {tier.value} will be widened. The contradiction will be "
        "written in full to 10-Liminal/Paradoxes/. Use --target unnamed "
        "(or thread/eddy/praxis) with --tier intimate if you want the "
        "body protected.[/yellow]",
    )


def _resolve_save_tier(
    tier: PrivacyTier | None,
    provenance: tuple[str, ...],
    *,
    came_from_stdin: bool,
) -> PrivacyTier:
    """Resolve the effective tier per FEAT-009's tier-defaulting rule.

    * Explicit ``--tier`` always wins.
    * Otherwise, when ``--provenance`` is supplied *and* the body did
      not come from stdin, default to ``open`` (the v1 surface; a
      future revision will derive from the source fragments' max tier).
    * No tier and either no provenance or stdin body → refuse so the
      operator makes an intentional choice.
    """
    if tier is not None:
        return tier
    if not provenance or came_from_stdin:
        console.print(
            "[red]--tier is required when --provenance is empty or the body "
            "comes from stdin. Pass --tier open|personal|intimate explicitly.[/red]",
        )
        raise typer.Exit(code=2)
    return PrivacyTier.OPEN


@app.command(name="save")
def save_cmd(
    target: str = typer.Option(
        ...,
        "--target",
        help=_SAVE_TARGET_HELP,
    ),
    body: str | None = typer.Option(
        None,
        "--body",
        help="Path to the body file, '-' for stdin, or omitted for stdin.",
    ),
    title: str | None = typer.Option(None, "--title", help="Optional title."),
    provenance: str | None = typer.Option(
        None,
        "--provenance",
        help="Comma-separated contributing fragment IDs (e.g. frag-001,frag-002).",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Opaque source ID (conversation/discord-msg/claude-session).",
    ),
    source_kind: str = typer.Option(
        "manual",
        "--source-kind",
        help="Source kind: discord, claude-session, manual, or mcp.",
    ),
    tier: str | None = typer.Option(None, "--tier", help=_SAVE_TIER_HELP),
    full_body: bool = typer.Option(
        False,
        "--full-body",
        help="Allow personal-tier bodies into the vault unredacted.",
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """File an answer back into the vault (FEAT-009).

    Writes a properly-classified note with full ``saved_from``
    frontmatter to the directory chosen by ``--target``. Honours
    privacy-tier policy: intimate bodies are diverted to the
    gitignored ``10-Liminal/Compost/intimate-stubs/`` directory and
    only a title-only summary is written into the vault; personal
    bodies are summarised unless ``--full-body`` is passed; paradox
    saves always land in ``10-Liminal/Paradoxes/``.
    """
    from creek.save import SaveRequest, save_to_vault

    save_target = _parse_save_target(target)
    parsed_source_kind = _parse_save_source_kind(source_kind)
    body_text, came_from_stdin = _read_save_body(body)
    parsed_tier = _parse_save_tier(tier)
    fragments = tuple(
        frag.strip() for frag in (provenance or "").split(",") if frag.strip()
    )
    effective_tier = _resolve_save_tier(
        parsed_tier,
        fragments,
        came_from_stdin=came_from_stdin,
    )
    _warn_if_paradox_downgrades_tier(save_target, parsed_tier)
    vault_path = _resolve_vault(vault)
    request = SaveRequest(
        target=save_target,
        body=body_text,
        title=title,
        tier=effective_tier,
        provenance=fragments,
        source_kind=parsed_source_kind,
        source_id=source,
        saved_by=_operator_identity(),
        full_body=full_body,
    )
    written = save_to_vault(request, vault_path=vault_path)
    console.print(f"[bold green]Saved {save_target.value} -> {written}[/bold green]")


# ---------------------------------------------------------------------------
# Clean subcommands
# ---------------------------------------------------------------------------


def _resolve_vault(vault: Path | None) -> Path:
    """Resolve vault path from argument or config.

    Args:
        vault: Explicit vault path, or None to use config default.

    Returns:
        Resolved vault path.
    """
    if vault is not None:
        return vault
    return load_config().vault_path


@clean_app.command(name="orphans")
def clean_orphans(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    age_days: int = typer.Option(30, help="Minimum age in days for orphan detection"),
    apply: bool = typer.Option(False, help="Apply changes (default is dry-run)"),
) -> None:
    """Identify fragments with zero incoming/outgoing links after N days."""
    from creek.clean.hygiene import OrphanScanner

    vault_path = _resolve_vault(vault)
    scanner = OrphanScanner(age_days=age_days)
    result = scanner.scan(vault_path)

    mode = "[red]APPLY[/red]" if apply else "[yellow]DRY-RUN[/yellow]"
    console.print(f"\n[bold]Orphan Scan[/bold] ({mode})")
    console.print(f"Total fragments: {result.total_fragments}")
    console.print(f"Orphans found: {len(result.orphan_paths)}")

    if result.orphan_paths:
        table = Table(title="Orphaned Fragments")
        table.add_column("Path", style="dim")
        for path in result.orphan_paths:
            table.add_row(path)
        console.print(table)


@clean_app.command(name="stale-reviews")
def clean_stale_reviews(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    age_days: int = typer.Option(14, help="Maximum age in days for review items"),
    apply: bool = typer.Option(False, help="Apply changes (default is dry-run)"),
) -> None:
    """Find review queue items older than N days."""
    from creek.clean.hygiene import StaleReviewScanner

    vault_path = _resolve_vault(vault)
    scanner = StaleReviewScanner(age_days=age_days)
    result = scanner.scan(vault_path)

    mode = "[red]APPLY[/red]" if apply else "[yellow]DRY-RUN[/yellow]"
    console.print(f"\n[bold]Stale Review Scan[/bold] ({mode})")
    console.print(f"Total review files: {result.total_review_files}")
    console.print(f"Stale files: {len(result.stale_paths)}")

    if result.stale_paths:
        table = Table(title="Stale Review Files")
        table.add_column("Path", style="dim")
        for path in result.stale_paths:
            table.add_row(path)
        console.print(table)


@clean_app.command(name="broken-links")
def clean_broken_links(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    apply: bool = typer.Option(False, help="Apply changes (default is dry-run)"),
) -> None:
    """Scan fragments for wiki-links pointing to nonexistent files."""
    from creek.clean.hygiene import BrokenLinkScanner

    vault_path = _resolve_vault(vault)
    scanner = BrokenLinkScanner()
    result = scanner.scan(vault_path)

    mode = "[red]APPLY[/red]" if apply else "[yellow]DRY-RUN[/yellow]"
    console.print(f"\n[bold]Broken Link Scan[/bold] ({mode})")
    console.print(f"Files scanned: {result.total_files_scanned}")
    console.print(f"Broken links: {result.total_broken}")

    if result.broken_links:
        from rich.markup import escape

        table = Table(title="Broken Links")
        table.add_column("Source File", style="dim")
        table.add_column("Broken Target")
        for source_file, targets in result.broken_links.items():
            for target in targets:
                table.add_row(source_file, escape(target))
        console.print(table)


@clean_app.command(name="duplicates")
def clean_duplicates(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    apply: bool = typer.Option(False, help="Apply changes (default is dry-run)"),
) -> None:
    """Execute normalized dedup sweep and output review report."""
    from creek.clean.hygiene import DuplicateScanner

    vault_path = _resolve_vault(vault)
    scanner = DuplicateScanner()
    result = scanner.scan(vault_path)

    mode = "[red]APPLY[/red]" if apply else "[yellow]DRY-RUN[/yellow]"
    console.print(f"\n[bold]Duplicate Scan[/bold] ({mode})")
    console.print(f"Total fragments: {result.total_fragments}")
    console.print(f"Duplicate candidates: {len(result.candidates)}")

    if result.candidates:
        table = Table(title="Duplicate Candidates")
        table.add_column("File A", style="dim")
        table.add_column("File B", style="dim")
        table.add_column("Match Type")
        for candidate in result.candidates:
            table.add_row(candidate.file_a, candidate.file_b, candidate.match_type)
        console.print(table)


@clean_app.command(name="report")
def clean_report(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    output: Path | None = typer.Option(None, help="Output markdown report path"),
) -> None:
    """Provide summary statistics on vault health."""
    from creek.clean.hygiene import HygieneReporter

    vault_path = _resolve_vault(vault)
    reporter = HygieneReporter()
    hygiene_report = reporter.generate(vault_path)

    table = Table(title="Vault Health Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Total fragments", str(hygiene_report.total_fragments))
    table.add_row("Orphaned fragments", str(hygiene_report.orphan_count))
    table.add_row("Stale review files", str(hygiene_report.stale_review_count))
    table.add_row("Broken links", str(hygiene_report.broken_link_count))
    table.add_row("Duplicate candidates", str(hygiene_report.duplicate_candidate_count))
    console.print(table)

    if hygiene_report.quality_distribution:
        qtable = Table(title="Quality Distribution")
        qtable.add_column("Action", style="bold")
        qtable.add_column("Count", justify="right")
        for action, count in sorted(hygiene_report.quality_distribution.items()):
            qtable.add_row(action, str(count))
        console.print(qtable)

    if output is not None:
        reporter.write_markdown(hygiene_report, output)
        console.print(f"\n[green]Report written to {output}[/green]")


# ---------------------------------------------------------------------------
# Purge subcommands
# ---------------------------------------------------------------------------


def _render_purge_result(result: PurgeResult) -> None:
    """Render a purge result as a rich table.

    Args:
        result: The completed purge result.
    """
    mode = "[yellow]DRY-RUN[/yellow]" if result.dry_run else "[red]APPLY[/red]"
    console.print(f"\n[bold]Purge {result.operation}[/bold] ({mode})")
    console.print(f"Target: {result.target}")
    console.print(f"Fragments affected: {result.fragments_affected}")
    console.print(f"Wiki-links removed: {result.wikilinks_removed}")
    console.print(f"Threads updated: {result.threads_updated}")
    console.print(f"Eddies updated: {result.eddies_updated}")
    if result.classifications_reset:
        console.print(
            f"Classifications reset: {result.classifications_reset}",
        )

    if result.deleted_files:
        table = Table(title="Deleted files")
        table.add_column("Path", style="dim")
        for path in result.deleted_files:
            table.add_row(path)
        console.print(table)


def _confirm(message: str, *, assume_yes: bool) -> bool:
    """Prompt the user for confirmation unless ``assume_yes`` is set.

    Args:
        message: Confirmation message to display.
        assume_yes: If ``True``, skip the prompt and return ``True``.

    Returns:
        Whether the user confirmed the operation.
    """
    if assume_yes:
        return True
    return typer.confirm(message, default=False)


def _build_engine(
    vault: Path | None,
    *,
    dry_run: bool,
) -> PurgeEngine:
    """Construct a :class:`PurgeEngine` rooted at the resolved vault.

    Args:
        vault: Optional explicit vault override.
        dry_run: Whether to preview changes only.

    Returns:
        A ready-to-use :class:`PurgeEngine`.
    """
    from creek.purge import PurgeEngine as _Engine

    vault_path = _resolve_vault(vault)
    return _Engine(vault_path, dry_run=dry_run)


@purge_app.command(name="fragment")
def purge_fragment(
    fragment_id: str = typer.Argument(..., help="Fragment ID to purge"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(False, help="Preview changes without deleting"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation",
    ),
) -> None:
    """Delete a fragment and scrub every reference to it."""
    engine = _build_engine(vault, dry_run=dry_run)
    if not dry_run and not _confirm(
        f"Purge fragment {fragment_id!r} and all references?",
        assume_yes=yes,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return
    result = engine.purge_fragment(fragment_id)
    _render_purge_result(result)


def _purge_source_match_modes() -> tuple[str, ...]:
    """Return the canonical ``--match`` modes for ``purge source --source-path``.

    Reuses :data:`creek.purge.engine.SOURCE_PATH_MATCH_MODES` (INC-008
    review nit) so the CLI usage error can never drift from the engine
    validator. Returned as a sorted tuple for deterministic CLI help
    output.
    """
    from creek.purge.engine import SOURCE_PATH_MATCH_MODES

    return tuple(sorted(SOURCE_PATH_MATCH_MODES))


@purge_app.command(name="source")
def purge_source(
    source_type: str | None = typer.Argument(
        None,
        help=(
            "Source platform (e.g. claude, discord). Mutually exclusive "
            "with --source-path."
        ),
    ),
    source_path: str | None = typer.Option(
        None,
        "--source-path",
        help=(
            "Match against source.original_file in each fragment's "
            "frontmatter (INC-008). Use with --match to choose the "
            "comparison mode."
        ),
    ),
    match: str = typer.Option(
        "exact",
        "--match",
        help="How --source-path is compared: exact | substring | regex.",
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(False, help="Preview changes without deleting"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation",
    ),
) -> None:
    """Delete every fragment ingested from a given source.

    Two modes:

    * Positional ``SOURCE_TYPE`` matches against ``source.platform``
      (e.g. ``claude``, ``discord``) — the original behaviour.
    * ``--source-path`` matches against ``source.original_file`` with
      a configurable ``--match`` mode (INC-008): ``exact`` (default),
      ``substring``, or ``regex``.

    The two are mutually exclusive; pass exactly one.
    """
    if (source_type is None) == (source_path is None):
        console.print(
            "[red]Pass exactly one of SOURCE_TYPE or --source-path.[/red]",
        )
        raise typer.Exit(code=2)
    valid_modes = _purge_source_match_modes()
    if match not in valid_modes:
        console.print(
            f"[red]Unknown --match {match!r}; expected one of "
            f"{', '.join(valid_modes)}.[/red]",
        )
        raise typer.Exit(code=2)

    engine = _build_engine(vault, dry_run=dry_run)
    if source_path is not None:
        try:
            count = engine.count_fragments_from_source_path(
                source_path,
                match=match,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        target_repr = f"source path {source_path!r} (match={match})"
    else:
        # The XOR guard above guarantees source_type is non-None
        # whenever source_path is None — assert that invariant
        # explicitly so a future refactor that breaks it surfaces
        # immediately rather than silently passing an empty platform.
        assert source_type is not None, (  # nosec B101
            "XOR guard failed: source_type required when source_path is None"
        )
        count = engine.count_fragments_from_source(source_type)
        target_repr = f"source platform {source_type!r}"
    console.print(
        f"[bold]This will delete {count} fragments from {target_repr}.[/bold]",
    )
    if not dry_run and not _confirm("Continue?", assume_yes=yes):
        console.print("[yellow]Aborted.[/yellow]")
        return
    if source_path is not None:
        result = engine.purge_source_path(source_path, match=match)
    else:
        assert source_type is not None, (  # nosec B101
            "XOR guard failed: source_type required when source_path is None"
        )
        result = engine.purge_source(source_type)
    _render_purge_result(result)


@purge_app.command(name="classifications")
def purge_classifications(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(False, help="Preview changes without deleting"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation",
    ),
) -> None:
    """Reset classification fields on every fragment to unclassified."""
    engine = _build_engine(vault, dry_run=dry_run)
    if not dry_run and not _confirm(
        "Reset classifications on every fragment?",
        assume_yes=yes,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return
    result = engine.purge_classifications()
    _render_purge_result(result)


@purge_app.command(name="daterange")
def purge_daterange(
    start: str = typer.Argument(..., help="Start date (YYYY-MM-DD, inclusive)"),
    end: str = typer.Argument(..., help="End date (YYYY-MM-DD, inclusive)"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(False, help="Preview changes without deleting"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation",
    ),
) -> None:
    """Delete fragments created within a date range."""
    from datetime import date as _date

    try:
        start_date = _date.fromisoformat(start)
        end_date = _date.fromisoformat(end)
    except ValueError as exc:
        console.print(f"[red]Invalid date: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    engine = _build_engine(vault, dry_run=dry_run)
    if not dry_run and not _confirm(
        f"Delete fragments created between {start_date} and {end_date}?",
        assume_yes=yes,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return
    result = engine.purge_daterange(start_date, end_date)
    _render_purge_result(result)


@purge_app.command(name="vault")
def purge_vault(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(False, help="Preview changes without deleting"),
    confirm_text: str = typer.Option(
        "",
        help=(
            "Must equal 'I understand this is irreversible' "
            "to bypass interactive prompt (requires --force-non-interactive)."
        ),
    ),
    force_non_interactive: bool = typer.Option(
        False,
        "--force-non-interactive",
        help=(
            "Allow vault purge without a TTY. Logs a WARNING. "
            "Required when stdin is piped or redirected."
        ),
    ),
) -> None:
    """Destroy every fragment, thread, and eddy (nuclear option).

    OPS-002 hardening: outside of ``--dry-run``, the command refuses to
    proceed unless either (a) stdin is a real TTY and the operator types
    the absolute vault path, or (b) the operator explicitly opts in with
    ``--force-non-interactive`` and supplies ``--confirm-text``. The
    second path emits a ``WARNING`` log entry so an audit trail records
    the bypass.
    """
    from creek.purge.engine import VAULT_PURGE_CONFIRMATION

    engine = _build_engine(vault, dry_run=dry_run)

    if dry_run:
        try:
            result = engine.purge_vault(VAULT_PURGE_CONFIRMATION)
        except ValueError as exc:
            console.print(f"[red]Aborted: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        _render_purge_result(result)
        return

    interactive = _is_interactive()
    if not interactive and not force_non_interactive:
        console.print(
            "[red]Refusing to purge vault from a non-interactive session. "
            "Pass --force-non-interactive (with caution) to override.[/red]",
        )
        raise typer.Exit(code=1)
    if not interactive and force_non_interactive:
        logger.warning(
            "creek purge vault running non-interactively at %s "
            "via --force-non-interactive",
            engine.vault_path,
        )

    phrase = _resolve_purge_phrase(
        engine_vault_path=engine.vault_path,
        confirm_text=confirm_text,
        interactive=interactive,
    )
    if phrase is None:
        console.print("[red]Aborted: vault path did not match.[/red]")
        raise typer.Exit(code=1)

    try:
        result = engine.purge_vault(phrase)
    except ValueError as exc:
        console.print(f"[red]Aborted: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    _render_purge_result(result)


def _resolve_purge_phrase(
    *,
    engine_vault_path: Path,
    confirm_text: str,
    interactive: bool,
) -> str | None:
    """Return the engine-level confirmation phrase, or ``None`` to abort.

    Three legal paths: (1) operator pre-supplied a *valid*
    ``confirm_text`` for non-interactive use, (2) interactive session
    in which the operator types the absolute vault path, (3) abort.

    Validation happens here at the CLI boundary so an invalid
    ``--confirm-text`` produces a message naming the flag, rather
    than letting :class:`PurgeEngine` raise a generic ``ValueError``
    (which is correct behaviour but reads as an internal error to
    operators).

    Args:
        engine_vault_path: Vault path the engine will operate on (used
            as the prompt's expected literal).
        confirm_text: ``--confirm-text`` value, possibly empty.
        interactive: Whether stdin is a TTY.

    Returns:
        The phrase to pass to ``PurgeEngine.purge_vault`` when accepted,
        or ``None`` when the supplied phrase is wrong or the
        interactive prompt was answered incorrectly.
    """
    from creek.purge.engine import VAULT_PURGE_CONFIRMATION

    if confirm_text:
        if confirm_text != VAULT_PURGE_CONFIRMATION:
            console.print(
                "[red]--confirm-text did not match the required phrase "
                f"{VAULT_PURGE_CONFIRMATION!r}.[/red]",
            )
            return None
        return VAULT_PURGE_CONFIRMATION

    if not interactive:
        return None

    expected = str(engine_vault_path.resolve())
    console.print(
        "[bold red]This will destroy the entire vault contents.[/bold red]",
    )
    typed = typer.prompt(
        f"Type the absolute vault path {expected!r} to continue",
        default="",
        show_default=False,
    )
    if typed.strip() != expected:
        return None
    return VAULT_PURGE_CONFIRMATION
