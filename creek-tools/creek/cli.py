"""Creek CLI -- command-line interface for the Creek knowledge organization pipeline."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from creek.config import load_config
from creek.pipeline import Pipeline

app = typer.Typer(name="creek", help="Creek knowledge organization pipeline")
clean_app = typer.Typer(name="clean", help="Vault hygiene commands")
app.add_typer(clean_app, name="clean")
console = Console()


@app.command()
def process(
    source: Path | None = typer.Option(None, help="Source directory to process"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Full pipeline: ingest, redact, classify, link, index."""
    config = load_config()
    source_path = source or config.source_drive
    vault_path = vault or config.vault_path

    console.print(
        f"[bold green]Running full pipeline: "
        f"source={source_path}, vault={vault_path}[/bold green]"
    )

    pipeline = Pipeline(config=config)
    result = pipeline.run(source_path=source_path, vault_path=vault_path)

    console.print(f"[bold]Files scanned:[/bold] {result.files_scanned}")
    console.print(f"[bold]Fragments created:[/bold] {result.fragments_created}")
    console.print(f"[bold]Classifications made:[/bold] {result.classifications_made}")
    console.print(f"[bold]Links found:[/bold] {result.links_found}")
    console.print(f"[bold]Indexes generated:[/bold] {result.indexes_generated}")


@app.command()
def ingest(
    type: str | None = typer.Option(None, help="Source type to ingest"),
    input: Path | None = typer.Option(None, help="Input path"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Ingest a specific source type."""
    console.print(
        f"[bold green]Would ingest: "
        f"type={type}, input={input}, vault={vault}[/bold green]"
    )


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
    from creek.redact.cli_commands import run_apply, run_review, run_scan

    if scan:
        src = _require_flag(source, "--scan", "--source")
        run_scan(src, report=report, verbose=verbose, console=console)
        return
    if apply:
        src = _require_flag(source, "--apply", "--source")
        run_apply(
            src,
            dry_run=dry_run,
            verbose=verbose,
            assume_yes=assume_yes,
            console=console,
        )
        return
    vlt = _require_flag(vault, "--review", "--vault")
    run_review(vlt, verbose=verbose, console=console)


def _require_flag(value: Path | None, mode: str, flag: str) -> Path:
    """Return *value* or abort when a required flag is missing.

    Args:
        value: Candidate path.
        mode: Mode flag that triggered the requirement (e.g. ``--scan``).
        flag: Required flag name (e.g. ``--source``).

    Returns:
        The validated path.

    Raises:
        typer.Exit: When *value* is ``None``.
    """
    if value is None:
        console.print(f"[red]{mode} requires {flag}[/red]")
        raise typer.Exit(code=2)
    return value


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


@app.command()
def classify(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option("rules", help="Classification method"),
    batch_size: int = typer.Option(50, help="Batch size for classification"),
) -> None:
    """Run classification on vault fragments."""
    console.print(
        f"[bold green]Would classify: vault={vault}, "
        f"method={method}, batch_size={batch_size}[/bold green]"
    )


@app.command()
def link(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option("embeddings", help="Linking method"),
) -> None:
    """Run linking pass to connect fragments."""
    console.print(
        f"[bold green]Would link: vault={vault}, method={method}[/bold green]"
    )


@app.command()
def report(
    type: str | None = typer.Option(None, help="Report type"),
    period: str | None = typer.Option(None, help="Report period"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Generate reports on vault state."""
    config = load_config()
    vault_path = vault or config.vault_path

    if type == "tags":
        from creek.generate.tags import TagGardenGenerator

        generator = TagGardenGenerator(vault_path=vault_path)
        path = generator.generate_garden()
        console.print(f"[bold green]Tag Garden generated: {path}[/bold green]")
    else:
        console.print(
            f"[bold green]Would report: type={type}, "
            f"period={period}, vault={vault_path}[/bold green]"
        )


@app.command()
def review(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Interactive review queue for fragments."""
    console.print(f"[bold green]Would review: vault={vault}[/bold green]")


@app.command()
def purge(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    target: str | None = typer.Option(None, help="Target to purge"),
) -> None:
    """Delete fragments or classifications."""
    console.print(
        f"[bold green]Would purge: vault={vault}, target={target}[/bold green]"
    )


@app.command()
def gdrive(
    download: bool = typer.Option(False, help="Download from Google Drive"),
    staging: Path | None = typer.Option(None, help="Staging directory"),
) -> None:
    """Download from Google Drive."""
    console.print(
        f"[bold green]Would gdrive: download={download}, staging={staging}[/bold green]"
    )


@app.command()
def skills(
    generate: bool = typer.Option(False, help="Generate voice skill files"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    output: Path | None = typer.Option(None, help="Output path"),
) -> None:
    """Generate voice skill files."""
    console.print(
        f"[bold green]Would skills: generate={generate}, "
        f"vault={vault}, output={output}[/bold green]"
    )


@app.command()
def mine(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    strategy: str | None = typer.Option(None, help="Mining strategy"),
) -> None:
    """Mine blog and essay ideas from vault."""
    console.print(
        f"[bold green]Would mine: vault={vault}, strategy={strategy}[/bold green]"
    )


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
