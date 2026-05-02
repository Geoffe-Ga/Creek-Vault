"""Creek CLI -- command-line interface for the Creek knowledge organization pipeline."""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from creek.config import load_config
from creek.pipeline import Pipeline

logger = logging.getLogger(__name__)


def _stdin_is_interactive() -> bool:
    """Return ``True`` when the process stdin is attached to a real TTY.

    Wrapping ``sys.stdin.isatty`` makes the OPS-002 non-interactive
    refusal trivially testable: tests can monkeypatch this single
    helper instead of fighting Typer's :class:`CliRunner`, which
    always provides a non-tty StringIO regardless of the host.
    """
    return sys.stdin.isatty()


if TYPE_CHECKING:
    from creek.generate.drafts import DraftLLM
    from creek.models import Phase
    from creek.purge import PurgeEngine, PurgeResult

app = typer.Typer(name="creek", help="Creek knowledge organization pipeline")
clean_app = typer.Typer(name="clean", help="Vault hygiene commands")
purge_app = typer.Typer(
    name="purge",
    help="Right-to-be-forgotten deletion operations",
)
app.add_typer(clean_app, name="clean")
app.add_typer(purge_app, name="purge")
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
    elif type == "unnamed":
        from datetime import date as _date
        from datetime import timedelta as _timedelta

        from creek.generate.unnamed import UnnamedDigestGenerator
        from creek.link.embeddings import EmbeddingLinker

        linker = EmbeddingLinker(config=config.embeddings)
        digest_generator = UnnamedDigestGenerator(embedding_linker=linker)
        today = _date.today()
        week_start = today - _timedelta(days=today.weekday())
        digest_path = digest_generator.generate_weekly_digest(
            vault_path,
            week_start,
        )
        console.print(
            f"[bold green]Unnamed digest generated: {digest_path}[/bold green]",
        )
    elif type == "voice":
        from creek.generate.voice import VoiceProfileGenerator

        profile_generator = VoiceProfileGenerator()
        profile_paths = profile_generator.generate_all_profiles(vault_path)
        if profile_paths:
            names = ", ".join(path.stem for path in profile_paths)
            console.print(
                f"[bold green]Voice profiles generated ({len(profile_paths)}): "
                f"{names}[/bold green]",
            )
        else:
            console.print(
                "[yellow]No voice profiles generated: "
                "no qualifying exemplars found.[/yellow]",
            )
    elif type == "wavelength":
        from datetime import date as _date

        from creek.generate.wavelength import WavelengthTracker

        if period not in {"weekly", "monthly"}:
            console.print(
                "[red]--period must be 'weekly' or 'monthly' for "
                "wavelength reports.[/red]",
            )
            raise typer.Exit(code=2)
        wavelength_tracker = WavelengthTracker()
        today = _date.today()
        if period == "weekly":
            wavelength_path = wavelength_tracker.generate_weekly_report(
                vault_path,
                week_of=today,
            )
        else:
            wavelength_path = wavelength_tracker.generate_monthly_report(
                vault_path,
                month=today,
            )
        console.print(
            f"[bold green]Wavelength {period} report generated: "
            f"{wavelength_path}[/bold green]",
        )
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
    console.print(f"[green]Token file removed: {token_path}[/green]")
    if result.remote_revoked:
        console.print("[green]Remote token revoked at Google.[/green]")
    else:
        message = result.error or "remote endpoint returned a non-success status"
        console.print(
            f"[yellow]Local token erased, but remote revocation did not "
            f"confirm: {message}. Visit "
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


@app.command()
def skills(
    generate: bool = typer.Option(False, help="Generate voice skill files"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    output: Path | None = typer.Option(None, help="Output path"),
) -> None:
    """Generate the Voice Skill Tree (Section 11.4).

    Writes a tree of ``SKILL.md`` files under *output* (default
    ``<vault>/creek-skills``) covering frequencies, phases, modes,
    registers, threads, eddies, and two meta skills.
    """
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


def _parse_phase(phase: str) -> "Phase":
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
) -> None:
    """Mine blog and essay ideas from the vault (Section 11.5).

    Runs every strategy - liminal cross-eddy, thread terminus, resonance
    chain, and wavelength-phase window - then prints a deduped,
    score-ranked table of :class:`IdeaSeed` records.
    """
    from creek.generate.mining import IdeaMiner

    vault_path = _resolve_vault(vault)
    current_phase = _parse_phase(phase)
    seeds = IdeaMiner().mine_all(vault_path, current_phase=current_phase)
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


def _build_draft_llm() -> "DraftLLM":
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
    llm = _build_draft_llm()

    seeds = IdeaMiner().mine_all(vault_path, current_phase=current_phase)
    if not seeds:
        console.print("[yellow]No idea seeds surfaced; nothing to draft.[/yellow]")
        return
    if index < 0 or index >= len(seeds):
        console.print(
            f"[red]--index {index} is out of range (0..{len(seeds) - 1}).[/red]",
        )
        raise typer.Exit(code=2)

    idea = seeds[index]
    generator = DraftGenerator(
        llm=llm,
        skills_root=skills_dir,
        voice_core=voice_text,
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


def _render_purge_result(result: "PurgeResult") -> None:
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
) -> "PurgeEngine":
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


@purge_app.command(name="source")
def purge_source(
    source_type: str = typer.Argument(
        ...,
        help="Source platform (e.g. claude, discord)",
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
    """Delete every fragment ingested from a given source."""
    engine = _build_engine(vault, dry_run=dry_run)
    count = engine.count_fragments_from_source(source_type)
    console.print(
        f"[bold]This will delete {count} fragments from {source_type!r}.[/bold]",
    )
    if not dry_run and not _confirm("Continue?", assume_yes=yes):
        console.print("[yellow]Aborted.[/yellow]")
        return
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

    interactive = _stdin_is_interactive()
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

    Three legal paths: (1) operator pre-supplied ``confirm_text`` for
    non-interactive use, (2) interactive session in which the operator
    types the absolute vault path, (3) abort.

    Args:
        engine_vault_path: Vault path the engine will operate on (used
            as the prompt's expected literal).
        confirm_text: ``--confirm-text`` value, possibly empty.
        interactive: Whether stdin is a TTY.

    Returns:
        The phrase to pass to ``PurgeEngine.purge_vault`` when accepted,
        or ``None`` when the interactive prompt was answered incorrectly.
    """
    from creek.purge.engine import VAULT_PURGE_CONFIRMATION

    if confirm_text:
        return confirm_text

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
