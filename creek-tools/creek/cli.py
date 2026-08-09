"""Creek CLI -- command-line interface for the Creek knowledge organization pipeline."""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    override_elevates,
    parse_include_tier,
    record_privacy_override,
)
from creek.config import AIStyleConfig, load_config, resolve_config_path
from creek.consent import ConsentManager
from creek.models import PraxisPotential, PrivacyTier
from creek.pipeline import (
    Pipeline,
    RedactionRequiredError,
    resolve_tier_a_plan,
    resolve_tier_b_plan,
)
from creek.save import SaveTarget

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.classify.prompt import PromptOntology
    from creek.config import CreekConfig
    from creek.generate.ai_style.model import ScanReport, VoiceFingerprint
    from creek.generate.compost_embedding import CompostExemplar
    from creek.generate.compost_scan import CompostScanResult
    from creek.generate.compost_verifier import SupportsVerifyCompost
    from creek.generate.drafts import (
        DraftGenerator,
        DraftLLM,
        OntologyDetector,
        SeedSpec,
    )
    from creek.generate.mining import MiningRunReport
    from creek.ingest.base import Ingestor
    from creek.ingest.discord_dispatch import DiscordMode
    from creek.link.link_engine import LinkSummary
    from creek.models import CompileTargetKind, Fragment, Frequency, Mode, Phase
    from creek.purge import PurgeEngine, PurgeResult


_INCLUDE_TIER_HELP = (
    "Privacy-tier override: open, personal, intimate, or all. "
    "Default policy excludes intimate fragments and replaces personal "
    "bodies with title-only summaries. Elevated values are recorded in "
    "<vault>/00-Creek-Meta/audit/privacy.jsonl."
)

# ``report`` needs its own wording because the flag runs the other way here
# (#968). Everywhere else ``--include-tier`` *widens* an already-restrictive
# default; on ``report`` an absent flag means **unfiltered**, and the flag
# narrows. That is the same reasoning already written at ``creek compile``
# below: the CLI operator *is* the vault owner, so there is no caller
# declaration to reconcile against and no ceiling to default to. Making an
# absent flag mean ``open`` would silently delete most of an existing
# operator's tag garden — a data-loss bug wearing a privacy fix's costume.
_REPORT_INCLUDE_TIER_HELP = (
    "Privacy-tier ceiling for the report's vault scan: open, personal, "
    "intimate, or all. Omitting the flag leaves the scan unfiltered (the CLI "
    "operator is the vault owner), so this flag NARROWS what the generated "
    "artifact may contain. Elevated values are recorded in "
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
compost_app = typer.Typer(
    name="compost",
    help="Compost detection utilities (embedding gate + verifier).",
)
app.add_typer(clean_app, name="clean")
app.add_typer(purge_app, name="purge")
app.add_typer(skills_app, name="skills")
app.add_typer(compost_app, name="compost")
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


def _parse_since_arg(text: str) -> datetime:
    """Parse a ``--since`` argument as an ISO timestamp or a duration (#677).

    Accepts an ISO-8601 timestamp (e.g. ``2026-06-25T00:00:00``, as the ledger
    records ``last_seen``) or a ``creek lint``-style duration (``7d``, ``1w``,
    ``1mo``). Naive ISO timestamps are treated as UTC. Exits with code 2 on an
    unparseable value.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        from creek.lint import parse_since  # public re-export of runner.parse_since

        try:
            return parse_since(text)
        except ValueError as exc:
            console.print(
                f"[red]Invalid --since {text!r}: expected an ISO timestamp "
                "(2026-06-25T00:00:00) or a duration (7d, 1w, 1mo).[/red]",
            )
            raise typer.Exit(code=2) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _run_ingest(
    *,
    ingestor_cls: type[Ingestor],
    source_type: str,
    input_path: Path,
    vault_path: Path,
    reclassify_threshold: float = 0.0,
    print_summary: bool = False,
    since: datetime | None = None,
    incremental: bool = False,
) -> tuple[int, list[str], int]:
    """Run a single ingestor and persist its output to the vault.

    Args:
        ingestor_cls: Concrete :class:`Ingestor` subclass to run.
        source_type: Registry key, used to prefix error messages.
        input_path: Source directory or file to ingest.
        vault_path: Vault root for :class:`VaultWriter`.
        reclassify_threshold: Body-similarity floor below which a materially
            edited mutable unit is flagged for re-classification (#675). ``0.0``
            (the default, used by tests/internal callers) always preserves.
        print_summary: When ``True``, print a created/updated/tombed summary
            line (the ``creek ingest`` surface; #675).
        since: Incremental cutoff (#677). When set, only units newer than this
            timestamp are processed; older units are skipped.
        incremental: Ledger-driven incremental mode (#677). When ``True`` and
            *since* is ``None``, units whose ledger content-hash is unchanged
            are skipped. Ignored for sources without a ledger.

    Returns:
        Tuple of ``(written_count, errors, discovered)`` where ``errors`` is a
        list of human-readable strings prefixed with ``[<source_type>]`` and
        ``discovered`` is how many inputs the ingestor's ``discover()`` found.

    Raises:
        typer.Exit: With code ``1`` when the vault cannot be opened.
    """
    from creek.ingest.pipeline import run_ingest

    try:
        result = run_ingest(
            ingestor_cls=ingestor_cls,
            source_type=source_type,
            input_path=input_path,
            vault_path=vault_path,
            reclassify_threshold=reclassify_threshold,
            since=since,
            incremental=incremental,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Vault unavailable: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if print_summary:
        console.print(
            f"[dim]Ingest summary: {result.created} created, "
            f"{result.updated} updated, {result.tombed} tombed, "
            f"{result.skipped} skipped[/dim]",
        )
    return result.written, result.errors, result.discovered


def _warn_if_discovered_but_empty(
    *,
    written: int,
    discovered: int,
    source_type: str,
    strict: bool,
) -> None:
    """Warn (and optionally exit non-zero) when inputs parsed to 0 fragments.

    The silent ``Ingested 0 fragment(s)`` is the symptom that masked the
    ChatGPT/Discord/Substack export-format bugs (#595): ``discover()`` found
    inputs but none parsed, signalling an unrecognized format. ``--strict``
    turns the warning into a non-zero exit for pipelines/CI.

    Raises:
        typer.Exit: With code ``1`` when *strict* and the condition holds.
    """
    if written > 0 or discovered == 0:
        return
    console.print(
        f"[yellow]WARNING: discovered {discovered} {source_type} input(s) but "
        "produced 0 fragments — the export format may be unrecognized.[/yellow]",
    )
    if strict:
        raise typer.Exit(code=1)


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
            "vault data."
        ),
    ),
) -> None:
    """Scaffold a Creek vault at ``--vault <path>``.

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


def _sync_state_path(vault_path: Path) -> Path:
    """Return the sync last-run state file path."""
    return vault_path / "00-Creek-Meta" / "State" / "sync" / "last-run.json"


def _write_sync_state(
    vault_path: Path,
    tier: str,
    *,
    dry_run: bool,
    source_counts: dict[str, int] | None = None,
) -> dict[str, object] | None:
    """Write the sync last-run state, returning the prior state if any (#676).

    Keeps the #676 top-level keys (``tier``/``at``/``dry_run``) and adds a
    backward-compatible per-source ``sources`` block (#680): each enabled source
    records its last ``{tier, at, ingested}``. Sources untouched by this run
    keep their prior entry, so ``--status`` always reflects the latest run of
    each source.
    """
    state_path = _sync_state_path(vault_path)
    prior: dict[str, object] | None = None
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            prior = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            prior = None
    now = datetime.now(UTC).isoformat()
    prior_sources = prior.get("sources", {}) if isinstance(prior, dict) else {}
    # Guard against a corrupted file where "sources" is not a dict.
    sources = dict(prior_sources) if isinstance(prior_sources, dict) else {}
    for name, ingested in (source_counts or {}).items():
        sources[name] = {"tier": tier, "at": now, "ingested": ingested}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"tier": tier, "at": now, "dry_run": dry_run, "sources": sources}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return prior


def _sync_status(vault_path: Path) -> None:
    """Print the per-source last-run + counts from last-run.json (#680)."""
    state_path = _sync_state_path(vault_path)
    if not state_path.exists():
        console.print("[yellow]No sync has run yet (no last-run.json).[/yellow]")
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        console.print("[red]last-run.json is unreadable.[/red]")
        return
    dry = " (dry-run)" if state.get("dry_run") else ""
    console.print(
        f"[bold]Last sync:[/bold] tier={state.get('tier')}{dry} at {state.get('at')}",
    )
    sources = state.get("sources", {})
    if not isinstance(sources, dict) or not sources:
        console.print("[dim](no per-source runs recorded yet)[/dim]")
        return
    table = Table("source", "last tier", "last run", "ingested")
    for name, entry in sorted(sources.items()):
        cells = entry if isinstance(entry, dict) else {}
        table.add_row(
            name,
            str(cells.get("tier", "")),
            str(cells.get("at", "")),
            str(cells.get("ingested", "")),
        )
    console.print(table)


def _sync_tier_a(
    enabled_sources: list[str],
    source: str | None,
) -> None:
    """Echo the Tier-A per-source plan (dry-run skeleton)."""
    sources = [source] if source is not None else enabled_sources
    console.print(
        f"[sync] tier=A (dry-run) enabled sources: "
        f"{', '.join(sources) if sources else '(none)'}",
    )
    for name in sources:
        plan = " -> ".join(resolve_tier_a_plan(name))
        console.print(f"[sync] plan ({name}): {plan}")


def _sync_tier_b() -> None:
    """Echo the Tier-B global plan (dry-run skeleton)."""
    console.print("[sync] tier=B (dry-run) global passes")
    console.print(f"[sync] plan: {' -> '.join(resolve_tier_b_plan())}")


# ---- creek sync: real tier execution (#678) ----------------------------


def _sync_source_input(source: str, config: CreekConfig) -> Path | None:
    """Resolve the local input path for a Tier-A source, or ``None`` (#678)."""
    rel = getattr(config.sources, source, None)
    if not isinstance(rel, str):
        return None
    return config.source_drive / rel


def _sync_pull(source: str, config: CreekConfig, _vault_path: Path) -> None:
    """Pull a remote source into staging (Tier A, #678).

    Only Google Drive has a pull step today (it mirrors files into a local
    staging directory via the existing read-only downloader); local sources
    such as the journal are already on disk and no-op here. Connector
    generalisation is a later epic. (``_vault_path`` is unused but kept so all
    step helpers share a uniform ``(source, config, vault_path)`` shape.)
    """
    if source != "gdrive":
        return
    from creek.ingest.gdrive import GoogleApiDriveClient, GoogleDriveDownloader

    client = GoogleApiDriveClient(config.google_drive)
    if not client.is_available():
        console.print(
            "[yellow][sync] Google Drive libraries unavailable; skipping pull."
            "[/yellow]",
        )
        return
    staging = config.source_drive / config.google_drive.staging_dir
    GoogleDriveDownloader(client=client, config=config.google_drive).download_all(
        staging,
    )


def _sync_ingest_source(source: str, config: CreekConfig, vault_path: Path) -> int:
    """Incrementally ingest one Tier-A source; return the units processed (#678).

    Returns ``0`` when the source has no ingestor (e.g. gdrive) or its path is
    missing — both are logged so a scheduled run's no-ops are diagnosable.
    """
    from creek.ingest import INGESTOR_REGISTRY
    from creek.pipeline import sync_ingest_type

    ingest_type = sync_ingest_type(source)
    ingestor_cls = INGESTOR_REGISTRY.get(ingest_type)
    if ingestor_cls is None:
        return 0  # e.g. gdrive is a downloader, not an ingestor
    input_path = _sync_source_input(source, config)
    if input_path is None or not input_path.exists():
        console.print(
            f"[yellow][sync] skipping {source}: source path not found "
            f"({input_path}).[/yellow]",
        )
        logger.info("sync.tier_a.skip source=%s reason=path-missing", source)
        return 0
    written, _errors, _discovered = _run_ingest(
        ingestor_cls=ingestor_cls,
        source_type=ingest_type,
        input_path=input_path,
        vault_path=vault_path,
        reclassify_threshold=config.classification.reclassify_on_edit_threshold,
        incremental=True,
    )
    logger.info("sync.tier_a.ingest source=%s ingested=%d", source, written)
    return written


def _sync_classify(vault_path: Path, config: CreekConfig, method: str) -> None:
    """Run a classify pass for a sync tier (#678)."""
    from creek.classify.classify_engine import run_classify

    run_classify(vault_path=vault_path, config=config, method=method, force=False)


def _sync_link(vault_path: Path, config: CreekConfig) -> None:
    """Run the embeddings linker — Tier B only (R6, #678)."""
    from creek.link.link_engine import run_link

    run_link(vault_path=vault_path, config=config, method="embeddings", rebuild=False)


def _sync_index(vault_path: Path) -> None:
    """Regenerate index notes — Tier B only (R6, #678)."""
    from creek.generate.indexes import IndexGenerator

    IndexGenerator(vault_path=vault_path).generate_all()


def _sync_run_tier_a(
    config: CreekConfig,
    vault_path: Path,
    sources: list[str],
) -> dict[str, int]:
    """Execute Tier A; return per-source ingested counts (#678/#680).

    Per source: pull + incremental ingest, then one rules-classify pass.
    Linking and indexing are **never** invoked here (R6: they are O(n²)/global
    and belong to Tier B).
    """
    counts: dict[str, int] = {}
    for source in sources:
        _sync_pull(source, config, vault_path)
        counts[source] = _sync_ingest_source(source, config, vault_path)
    _sync_classify(vault_path, config, "rules")
    logger.info("sync.tier_a.done count=%d", len(sources))
    return counts


def _sync_run_tier_b(config: CreekConfig, vault_path: Path) -> None:
    """Execute Tier B globally: llm classify -> link -> index (#678)."""
    _sync_classify(vault_path, config, "llm")
    _sync_link(vault_path, config)
    _sync_index(vault_path)
    logger.info("sync.tier_b.done")


def _sync_dispatch(
    tier_norm: str,
    *,
    dry_run: bool,
    config: CreekConfig,
    vault_path: Path,
    source: str | None,
) -> dict[str, int] | None:
    """Echo (dry-run) or execute the chosen sync tier; return Tier-A counts.

    Returns the per-source ingested counts for a real Tier-A run (for the
    status file), or ``None`` for dry-run / Tier-B runs.
    """
    if tier_norm == "A":
        if dry_run:
            _sync_tier_a(config.sync.enabled_sources(), source)
            return None
        # An explicit --source overrides the enable toggle (the operator
        # asked for it by name); otherwise run the enabled set.
        sources = [source] if source is not None else config.sync.enabled_sources()
        return _sync_run_tier_a(config, vault_path, sources)
    if dry_run:
        _sync_tier_b()
    else:
        _sync_run_tier_b(config, vault_path)
    return None


def _install_launchd(
    vault_path: Path,
    minutes: int,
    hour: int,
    out_dir: Path | None,
) -> None:
    """Write both launchd plists and print activation instructions."""
    from creek.sync import render_launchd_plists

    plists = render_launchd_plists(
        vault=vault_path, tier_a_minutes=minutes, tier_b_hour=hour
    )
    base = out_dir or (Path.home() / "Library" / "LaunchAgents")
    base.mkdir(parents=True, exist_ok=True)
    tier_a = base / plists.tier_a_filename
    tier_b = base / plists.tier_b_filename
    tier_a.write_text(plists.tier_a, encoding="utf-8")
    tier_b.write_text(plists.tier_b, encoding="utf-8")
    console.print(f"# wrote {tier_a}")
    console.print(f"# wrote {tier_b}")
    console.print(f"# To activate: launchctl load {tier_a} && launchctl load {tier_b}")


def _install_systemd(
    vault_path: Path,
    minutes: int,
    hour: int,
    out_dir: Path | None,
) -> None:
    """Write systemd service+timer units (and print the cron alternative)."""
    from creek.sync import render_crontab, render_systemd_units

    units = render_systemd_units(
        vault=vault_path, tier_a_minutes=minutes, tier_b_hour=hour
    )
    base = out_dir or (Path.home() / ".config" / "systemd" / "user")
    base.mkdir(parents=True, exist_ok=True)
    for fname, content in (
        (units.tier_a_service_filename, units.tier_a_service),
        (units.tier_a_timer_filename, units.tier_a_timer),
        (units.tier_b_service_filename, units.tier_b_service),
        (units.tier_b_timer_filename, units.tier_b_timer),
    ):
        (base / fname).write_text(content, encoding="utf-8")
        console.print(f"# wrote {base / fname}")
    console.print(
        "# To activate: systemctl --user enable --now "
        "creek-sync-tier-a.timer creek-sync-tier-b.timer",
    )
    console.print("# Or as cron (crontab -e):")
    cron = render_crontab(vault=vault_path, tier_a_minutes=minutes, tier_b_hour=hour)
    for line in cron.splitlines():
        console.print(f"#   {line}")


def _install_schedule(host: str, vault: Path | None, out_dir: Path | None) -> None:
    """Emit schedule units for *host* (launchd|systemd), parametrised by config."""
    if host not in {"launchd", "systemd"}:
        console.print("[red]--install-schedule must be launchd or systemd[/red]")
        raise typer.Exit(code=2)
    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    minutes = config.sync.tier_a_interval_minutes
    hour = config.sync.tier_b_hour
    if host == "launchd":
        _install_launchd(vault_path, minutes, hour, out_dir)
    else:
        _install_systemd(vault_path, minutes, hour, out_dir)
    console.print(
        "# Tokens (OAuth) read from this host's keychain/env; "
        "see docs/sync-scheduling.md",
    )


def _sync_validate_source(
    tier_norm: str,
    source: str | None,
    config: CreekConfig,
) -> None:
    """Reject a Tier-B ``--source`` or an unknown source name (exits 2)."""
    if source is None:
        return
    if tier_norm == "B":
        # --source is a Tier-A concept; Tier B always runs the global passes.
        console.print("[red]--source is not supported with --tier B.[/red]")
        raise typer.Exit(code=2)
    if source not in config.sync.sources:
        known = ", ".join(sorted(config.sync.sources))
        console.print(
            f"[red]Unknown --source '{source}'. Known sources: {known}.[/red]",
        )
        raise typer.Exit(code=2)


@app.command()
def sync(
    tier: str = typer.Option(
        "A", "--tier", help="A (cheap, per-source) or B (nightly)"
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Limit a Tier-A run to one source (explicitly overrides its toggle)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Echo the plan without executing (current default)"
    ),
    install_schedule: str | None = typer.Option(
        None,
        "--install-schedule",
        help="Emit schedule units for a host (launchd|systemd) and exit",
    ),
    schedule_out_dir: Path | None = typer.Option(
        None,
        "--schedule-out-dir",
        help="Directory to write emitted schedule units (default: host standard)",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Show the per-source last run + counts from last-run.json and exit",
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Run a scheduled sync pass, emit schedule units, or show status.

    ``--tier A`` is the cheap per-source pass (pull -> incremental ingest ->
    rules classify) for each enabled source; ``--tier B`` is the nightly global
    pass (LLM classify -> link -> index). Linking and indexing run only in Tier
    B (R6). ``--dry-run`` echoes the ordered plan without executing. Every run
    records ``00-Creek-Meta/State/sync/last-run.json``; because every step is
    idempotent, a missed tick self-heals on the next run (no catch-up queue).

    ``--install-schedule launchd|systemd`` emits the host's schedule units and
    exits; ``--status`` prints the per-source last-run table and exits.
    """
    if status:
        # Status only needs the vault path; avoid loading (and possibly failing
        # on) the config when --vault is explicitly given.
        _sync_status(vault or _load_config_for_vault(None).vault_path)
        return

    if install_schedule is not None:
        _install_schedule(install_schedule, vault, schedule_out_dir)
        return

    tier_norm = tier.upper()
    if tier_norm not in {"A", "B"}:
        console.print("[red]--tier must be A or B[/red]")
        raise typer.Exit(code=2)

    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    _sync_validate_source(tier_norm, source, config)

    source_counts = _sync_dispatch(
        tier_norm,
        dry_run=dry_run,
        config=config,
        vault_path=vault_path,
        source=source,
    )

    prior = _write_sync_state(
        vault_path, tier_norm, dry_run=dry_run, source_counts=source_counts
    )
    if prior is not None:
        console.print(
            f"[sync] (previous run: tier={prior.get('tier')} at {prior.get('at')})",
        )
    mode = "planned" if dry_run else "ran"
    console.print(f"[sync] {mode} tier={tier_norm}; wrote last-run.json")


def _parse_discord_mode(mode: str) -> DiscordMode:
    """Parse a ``--mode`` string into a :class:`DiscordMode`, or exit 2 (#685)."""
    from creek.ingest.discord_dispatch import DiscordMode

    try:
        return DiscordMode(mode)
    except ValueError:
        valid = ", ".join(m.value for m in DiscordMode)
        console.print(f"[red]Unknown --mode '{mode}'. Valid: {valid}.[/red]")
        raise typer.Exit(code=2) from None


def _run_discord_exporter(
    config: CreekConfig,
    vault_path: Path,
    binary: str,
) -> None:
    """Run the real opt-in DM exporter connector (#686)."""
    import subprocess

    from creek.ingest.discord_dispatch import (
        ExporterConnector,
        SubprocessExporterRunner,
        TokenMissingError,
    )

    runner = SubprocessExporterRunner(
        binary, timeout_seconds=config.discord.exporter_timeout_seconds
    )
    try:
        ExporterConnector(config.discord, runner, vault_path).run()
    except TokenMissingError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Discord exporter failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green][discord] exporter run complete; staged + ingested.[/green]")


def _run_discord_data_package(vault_path: Path, package: Path) -> None:
    """Ingest a downloaded Discord Data Package incrementally (#688).

    The clean, always-available compliant path — no token, no ToS concern. Stable
    Discord ids make a re-pointed package a clean no-op (only new messages cost).
    """
    from creek.ingest.discord import run_discord_data_package

    if not package.exists():
        console.print(f"[red]Data Package path does not exist: {package}[/red]")
        raise typer.Exit(code=1)
    if not package.is_dir():
        console.print(
            f"[red]Data Package must be the unzipped export directory, "
            f"not a file: {package}[/red]"
        )
        raise typer.Exit(code=1)
    new_count = run_discord_data_package(vault_path, package)
    console.print(
        f"[green][discord] data_package ingested {new_count} new fragment(s).[/green]"
    )


def _run_real_discord_mode(
    selected: DiscordMode,
    config: CreekConfig,
    vault_path: Path,
    *,
    dry_run: bool,
    package: Path | None,
) -> bool:
    """Run the exporter / Data Package helper if its gate is met (#686/#688).

    Returns ``True`` when a real ingest ran (the caller should stop), ``False``
    when no real path applies and the caller should fall back to the echo plan.
    """
    from creek.ingest.discord_dispatch import DiscordMode

    if dry_run:
        return False
    discord = config.discord
    binary = discord.exporter_binary
    if selected is DiscordMode.EXPORTER and discord.exporter.enabled and binary:
        _run_discord_exporter(config, vault_path, binary)
        return True
    if (
        selected is DiscordMode.DATA_PACKAGE
        and discord.data_package.enabled
        and package is not None
    ):
        _run_discord_data_package(vault_path, package)
        return True
    return False


def _dispatch_discord(
    selected: DiscordMode,
    config: CreekConfig,
    vault_path: Path,
    *,
    dry_run: bool,
    package: Path | None = None,
) -> None:
    """Run the exporter / Data Package helper, else echo the mode plan (#686/#688)."""
    from creek.ingest.discord_dispatch import ModeDisabledError, resolve_discord_handler

    if _run_real_discord_mode(
        selected, config, vault_path, dry_run=dry_run, package=package
    ):
        return

    try:
        handler = resolve_discord_handler(selected, config.discord)
    except ModeDisabledError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    handler.announce()
    for line in handler.plan():
        console.print(line)
    if not dry_run:
        console.print(
            "[dim][discord] echo only — pass --package <path> for data_package, "
            "set discord.exporter_binary for the exporter, or run the bot for "
            "bot_capture.[/dim]",
        )


@app.command()
def discord(
    mode: str = typer.Option(
        ..., "--mode", help="data_package | exporter | bot_capture"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Echo the plan without running"
    ),
    package: Path | None = typer.Option(
        None, "--package", help="Path to a downloaded Discord Data Package to ingest"
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Dispatch a Discord ingest mode (#685/#686/#688).

    Resolves ``--mode`` against the configured Discord toggles. When
    ``exporter`` is enabled and ``discord.exporter_binary`` is set (and not
    ``--dry-run``), runs the real opt-in user-token DM exporter. When
    ``data_package`` is selected with ``--package <path>`` (and not
    ``--dry-run``), incrementally ingests the downloaded Data Package. Otherwise
    echoes the mode's plan. The ``exporter`` mode is opt-in (off by default) and
    prints a Terms-of-Service caveat. The Discord token lives in the
    environment, never in config; the Data Package path carries no token.
    """
    selected = _parse_discord_mode(mode)
    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    _dispatch_discord(selected, config, vault_path, dry_run=dry_run, package=package)


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
    config = _load_config_for_vault(vault)
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
    refresh_dates: bool = typer.Option(
        False,
        "--refresh-dates",
        help=(
            "Backfill: re-extract authored_at on existing fragments "
            "without re-ingesting body content. Idempotent."
        ),
    ),
    refresh_ai_chat: bool = typer.Option(
        False,
        "--refresh-ai-chat",
        help=(
            "Migration: re-split previously-merged Claude/ChatGPT fragments "
            "into per-turn attributed fragments (human=self, AI=ai, "
            "voice_weight 0), removing AI prose from the voice corpus. "
            "Idempotent. --type / --input are ignored."
        ),
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Exit non-zero when inputs were discovered but produced 0 "
            "fragments (likely an unrecognized export format)."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "Incremental: only process units newer than this ISO timestamp "
            "(e.g. 2026-06-25T00:00:00) or duration (e.g. 7d, 1w)."
        ),
    ),
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help=(
            "Incremental: skip units whose content is unchanged vs the ledger "
            "(ledger-driven; overridden by --since)."
        ),
    ),
) -> None:
    """Ingest a specific source type into the vault.

    Resolves ``--type`` against
    :data:`creek.ingest.INGESTOR_REGISTRY`, runs the matching ingestor
    against ``--input``, and writes each produced fragment via
    :class:`~creek.vault.writer.VaultWriter`. Re-running against the same
    input is idempotent: deterministic fragment IDs ensure existing
    files are recognised and skipped.

    ``--refresh-dates`` is a one-shot backfill: it walks
    ``--vault`` (or the configured vault), re-runs the per-format
    authored-date extraction chain against each fragment's source
    file, and rewrites the frontmatter in place. ``--type`` /
    ``--input`` are ignored in refresh mode.
    """
    if refresh_dates:
        _run_refresh_dates(vault)
        return

    if refresh_ai_chat:
        _run_refresh_ai_chat(vault)
        return

    if type is None or input is None:
        console.print("[red]--type and --input are required.[/red]")
        raise typer.Exit(code=2)

    config = _load_config_for_vault(vault)
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

    since_dt = _parse_since_arg(since) if since is not None else None

    written, errors, discovered = _run_ingest(
        ingestor_cls=ingestor_cls,
        source_type=type,
        input_path=input,
        vault_path=vault_path,
        reclassify_threshold=config.classification.reclassify_on_edit_threshold,
        print_summary=True,
        since=since_dt,
        incremental=incremental,
    )

    console.print(f"[bold green]Ingested {written} fragment(s).[/bold green]")
    if errors:
        console.print(f"[yellow]Errors: {len(errors)}[/yellow]")
        for err in errors:
            console.print(f"  [dim]{err}[/dim]")

    _warn_if_discovered_but_empty(
        written=written,
        discovered=discovered,
        source_type=type,
        strict=strict,
    )


def _run_refresh_dates(vault: Path | None) -> None:
    """Execute the FEAT-031 ``--refresh-dates`` backfill.

    Resolves the vault path (CLI flag → configured default), invokes
    :func:`creek.ingest.refresh.refresh_authored_dates`, and renders a
    one-screen summary. Exit codes mirror the rest of the CLI: 0 on
    success (regardless of how many fragments were updated), 1 if the
    vault path is unusable.
    """
    from creek.ingest.refresh import refresh_authored_dates

    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    if not vault_path.exists():
        console.print(f"[red]Vault path does not exist: {vault_path}[/red]")
        raise typer.Exit(code=1)

    result = refresh_authored_dates(vault_path)
    console.print(
        f"[bold green]Refreshed authored_at on {result.updated} "
        "fragment(s).[/bold green]"
    )
    console.print(f"  scanned: {result.scanned}")
    console.print(f"  already-set: {result.already_set}")
    console.print(f"  no source date: {result.no_date_found}")
    console.print(f"  missing source file: {result.missing_source}")
    console.print(f"  unsupported source: {result.unsupported}")
    if result.errors:
        console.print(f"[yellow]Errors: {len(result.errors)}[/yellow]")
        for err in result.errors:
            console.print(f"  [dim]{err}[/dim]")


def _run_refresh_ai_chat(vault: Path | None) -> None:
    """Execute the ``--refresh-ai-chat`` merged-fragment re-split migration.

    Resolves the vault path (CLI flag → configured default), invokes
    :func:`creek.ingest.refresh.resplit_merged_ai_chat`, and renders a
    one-screen summary. Exit codes: 0 on success, 1 if the vault path is
    unusable.
    """
    from creek.ingest.refresh import resplit_merged_ai_chat

    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    if not vault_path.exists():
        console.print(f"[red]Vault path does not exist: {vault_path}[/red]")
        raise typer.Exit(code=1)

    result = resplit_merged_ai_chat(vault_path)
    console.print(
        f"[bold green]Re-split {result.resplit} merged AI-chat "
        "fragment(s).[/bold green]",
    )
    console.print(f"  scanned: {result.scanned}")
    console.print(f"  skipped (not merged AI-chat): {result.skipped}")
    if result.errors:
        console.print(f"[yellow]Errors: {len(result.errors)}[/yellow]")
        for err in result.errors:
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
        run_scan(src, report=report, verbose=verbose, console=console, vault=vault)
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
_LINK_METHODS = ("embeddings", "temporal", "eddies", "threads")
_REATOMIZE_DIRECTIONS = ("auto", "split", "aggregate")


_CLASSIFY_METHOD_HELP: str = (
    "Classification method (rules|llm). "
    "'rules' is local and offline. 'llm' calls the provider configured "
    "under llm: in creek_config.yaml; the default 'ollama' provider is "
    "local, while any cloud provider (e.g. 'anthropic') requires its API "
    "key plus CREEK_CLOUD_CONSENT=1 (the legacy CREEK_ANTHROPIC_CONSENT is "
    "still honored) to be set in the environment before the run "
    "(data-egress acknowledgement; see issue #320)."
)


def _preflight_cloud_consent(config: CreekConfig) -> None:
    """Abort early when ``--method llm`` will hit a cloud-consent gate.

    Issue #320: previously the consent requirement only surfaced once the
    cloud provider was instantiated mid-classify run, after vault load and
    first-fragment iteration. This pre-flight inspects the resolved config
    plus the live process environment and surfaces the exact remediation
    BEFORE any fragment work begins.

    Provider-neutral (#606): it fires for *any* cloud provider via
    :func:`~creek.classify.llm.providers.provider_is_cloud` and accepts the
    neutral ``CREEK_CLOUD_CONSENT`` or the legacy ``CREEK_ANTHROPIC_CONSENT``.
    No-ops for local providers (Ollama), and silent when consent is already on
    file (the run continues normally).

    Args:
        config: Loaded :class:`CreekConfig` for the current invocation.

    Raises:
        typer.Exit: With code ``1`` when a cloud provider is selected but
            cloud-egress consent is missing.
    """
    from creek.classify.llm.consent import (
        CLOUD_CONSENT_ENV,
        LEGACY_CONSENT_ENV,
        has_cloud_consent,
    )
    from creek.classify.llm.providers import (
        provider_display_name,
        provider_is_cloud,
    )

    # Per-stage routing (#646): a cloud provider configured for ANY stage
    # (not just the global default) requires consent — check them all, and name
    # the offending stage so the operator looks at the right config key.
    cloud_stage = next(
        (
            (label, cfg.provider.strip().lower())
            for label, cfg in config.llm.iter_stage_configs()
            if provider_is_cloud(cfg.provider.strip().lower())
        ),
        None,
    )
    if cloud_stage is None:
        return
    if has_cloud_consent():
        return
    stage_label, provider = cloud_stage
    name = provider_display_name(provider)
    console.print(
        f"[red]{name} provider configured for the '{stage_label}' LLM stage "
        f"in creek_config.yaml (llm.{stage_label}.provider: {provider}), but "
        "cloud processing requires explicit consent.[/red]",
    )
    console.print(
        f"[yellow]Set [bold]{CLOUD_CONSENT_ENV}=1[/bold] "
        "(also accepts 'true' or 'yes'; the legacy "
        f"[bold]{LEGACY_CONSENT_ENV}[/bold] is still honored) to confirm "
        f"that fragment content may be sent to {name}'s servers, then "
        "re-run [bold]creek classify --method llm[/bold].[/yellow]",
    )
    console.print(
        "[dim]To keep classification fully local instead, set "
        "[bold]llm.provider: ollama[/bold] in your creek_config.yaml "
        "or pass [bold]--method rules[/bold].[/dim]",
    )
    raise typer.Exit(code=1)


@app.command()
def classify(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option("rules", help=_CLASSIFY_METHOD_HELP),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite fragments classified as method: manual.",
    ),
    calibrate: bool = typer.Option(
        False,
        "--calibrate",
        help=(
            "Run the LLM classifier against the calibration fixture and "
            "print per-dimension agreement. Skips the vault "
            "run, but still requires a valid creek_config.yaml because "
            "the LLM provider settings come from `llm:` there."
        ),
    ),
    calibration_fixture: Path | None = typer.Option(
        None,
        "--calibration-fixture",
        help=(
            "Path to a calibration fixture YAML. Defaults to "
            "tests/fixtures/classification/calibration_set.yaml."
        ),
    ),
    enforce_floors: bool = typer.Option(
        False,
        "--enforce-floors",
        help=(
            "Exit non-zero if any per-dimension agreement falls below the "
            "documented floor. Use in CI runs that should fail "
            "loud on a regressed prompt or example set."
        ),
    ),
    reatomize: bool = typer.Option(
        False,
        "--reatomize",
        help=(
            "Enable confidence-driven re-atomization for this run "
            "regardless of the YAML default. Below-threshold or unclassified "
            "fragments are re-atomized (split or aggregated) and re-classified "
            "until a leaf clears the threshold or a terminal level is reached."
        ),
    ),
    reatomize_direction: str = typer.Option(
        "auto",
        "--reatomize-direction",
        help=(
            "Override the direction heuristic. 'auto' routes by "
            "source/level; 'split' forces zoom-in; 'aggregate' forces zoom-out."
        ),
    ),
) -> None:
    """Run classification on existing vault fragments.

    ``--method rules`` runs the keyword-based classifier locally.
    ``--method llm`` calls the configured LLM provider for any
    fragment the rules left unclassified or below
    ``classification.confidence_threshold``. Fragments with
    ``classification.method: manual`` are preserved unless ``--force``
    is supplied.

    ``--calibrate`` flips this into calibration mode: instead of touching
    the vault, the classifier runs against the calibration fixture and
    prints per-dimension agreement. Pair with ``--enforce-floors`` in
    CI to fail the build when a dimension regresses below the
    :data:`creek.classify.calibration.DEFAULT_FLOORS` threshold.

    LLM concurrency is governed by ``llm.max_concurrent`` in the
    config, not a CLI flag.
    """
    if calibrate:
        _run_calibration_cli(
            fixture_path=calibration_fixture,
            enforce_floors=enforce_floors,
            vault=vault,
        )
        return

    if method not in _CLASSIFY_METHODS:
        console.print(
            f"[red]Unknown method {method!r}. "
            f"Supported: {', '.join(_CLASSIFY_METHODS)}.[/red]",
        )
        raise typer.Exit(code=2)
    if reatomize_direction not in _REATOMIZE_DIRECTIONS:
        console.print(
            f"[red]Unknown --reatomize-direction {reatomize_direction!r}. "
            f"Supported: {', '.join(_REATOMIZE_DIRECTIONS)}.[/red]",
        )
        raise typer.Exit(code=2)

    config = _load_config_for_vault(vault)
    if method == "llm":
        # Issue #320 / #606: surface the cloud-egress consent gate before
        # any vault iteration. The provider's own __init__ also raises,
        # but only after the engine has already started — by then the
        # operator has waited through vault load and per-fragment setup.
        _preflight_cloud_consent(config)
    if reatomize:
        # Late-bind the FEAT-023 CLI overrides into the loaded config so
        # downstream call-sites only ever look at the config object, not
        # at sticky locals. The `reatomize_direction` here is already
        # validated against `_REATOMIZE_DIRECTIONS`, so the cast to the
        # config's `Literal` is sound.
        config.classification = config.classification.model_copy(
            update={
                "reatomize": True,
                "reatomize_direction": cast(
                    "Literal['auto', 'split', 'aggregate']",
                    reatomize_direction,
                ),
            },
        )
    vault_path = _resolve_vault(vault)

    from creek.classify.classify_engine import (
        LLMProviderUnavailableError,
        run_classify,
    )

    try:
        summary = run_classify(
            vault_path=vault_path,
            config=config,
            method=method,
            force=force,
        )
    except LLMProviderUnavailableError as exc:
        # The engine refused to iterate because the configured LLM
        # provider is unreachable. Surface the provider-specific
        # remediation hint and exit non-zero so a shell pipeline does
        # NOT see "Classified N of N" plus a clean exit when zero
        # fragments were actually classified.
        console.print(f"[red]Classification aborted: {exc}.[/red]")
        raise typer.Exit(code=1) from exc

    # Issue #321: report manual-preserved and prior-LLM-preserved as
    # distinct counts so the operator can tell genuine hand-curation
    # ("a person tagged these") apart from the OPS-001 resume path
    # ("we left them alone because a previous --method llm run already
    # paid for them"). Collapsing both into "N manual preserved" was a
    # bug: fragments touched only by a failed automated run were
    # incorrectly attributed to a human.
    console.print(
        f"[bold green]Classified {summary.classified} of "
        f"{summary.total} fragment(s) "
        f"({summary.preserved_manual} manual preserved, "
        f"{summary.preserved_llm} previously LLM-classified preserved, "
        f"{summary.skipped_high_confidence} skipped, "
        # Issue #876: tier assignment is orthogonal to the classification
        # method — preserved fragments get one too — so it is reported as
        # its own count rather than folded into "classified".
        f"{summary.privacy_tiers_assigned} privacy tier(s) assigned, "
        # Issue #877: same reasoning for the praxis axis — it is the only
        # visible evidence that the field has a producer at all.
        f"{summary.praxis_marked} praxis marked, "
        # Issue #878: and again for hashtag tags. Counts fragments gained,
        # not tags written, so a second run over an unchanged vault reports
        # zero rather than re-reporting the whole vault.
        f"{summary.tags_extracted} tagged"
        ").[/bold green]",
    )
    if summary.errors:
        console.print(f"[yellow]Errors: {len(summary.errors)}[/yellow]")
        for err in summary.errors:
            console.print(f"  [dim]{err}[/dim]")


_DEFAULT_CALIBRATION_FIXTURE: Path = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "classification"
    / "calibration_set.yaml"
)
"""Default fixture path for `creek classify --calibrate`.

Anchored to the source layout (``creek-tools/creek/cli.py`` →
``parents[1]`` = ``creek-tools/``) so the command works regardless of
the operator's current working directory. Used when
``--calibration-fixture`` is omitted.

Source-tree assumption: ``parents[1]`` resolves to ``creek-tools/``
for editable / direct-run installs (which is how this tool is used
today). A non-editable pip install of ``creek`` would place
``cli.py`` inside ``site-packages/creek/`` without an adjacent
``tests/`` directory; in that scenario the operator must supply
``--calibration-fixture`` explicitly. If the project is ever
distributed as a wheel, the long-term fix is to re-ship the fixture
as package data inside ``creek/classify/`` and load via
``importlib.resources``.
"""


def _run_calibration_cli(
    *,
    fixture_path: Path | None,
    enforce_floors: bool,
    vault: Path | None = None,
) -> None:
    """Drive `creek classify --calibrate` end-to-end (FEAT-017b).

    Loads the fixture, builds an :class:`LLMClassifier` from the
    current config, runs the calibration, and prints the per-dimension
    agreement report. When ``enforce_floors`` is set, exits with code
    1 on a floor breach so a CI invocation surfaces the regression.

    Args:
        fixture_path: Operator-supplied fixture path; ``None`` falls
            back to :data:`_DEFAULT_CALIBRATION_FIXTURE`.
        enforce_floors: Exit non-zero on floor breach when ``True``.
        vault: Optional vault root threaded through for config
            auto-discovery (issue #322).
    """
    from creek.classify.calibration import (
        CalibrationFloorBreachError,
        assert_floors,
        load_fixture,
        run_calibration,
    )
    from creek.classify.llm import LLMClassifier

    resolved = fixture_path or _DEFAULT_CALIBRATION_FIXTURE
    if not resolved.exists():
        message = f"[red]Calibration fixture not found: {resolved}.[/red] "
        if fixture_path is None:
            message += (
                "The default path is anchored to the source tree; if you "
                "installed `creek` via a non-editable `pip install`, the "
                "`tests/fixtures/...` directory is not packaged. Pass "
                "--calibration-fixture explicitly, or re-install in "
                "editable mode (`pip install -e .`)."
            )
        else:
            message += "Pass --calibration-fixture to point at a fixture file."
        console.print(message)
        raise typer.Exit(code=2)

    config = _load_config_for_vault(vault)
    # No tier argument, and none exists to pass: the classifier only ever sees
    # `creek.classify.calibration.load_fixture(...)` output — a labelled fixture
    # (default `tests/fixtures/classification/calibration_set.yaml`) that carries
    # no `privacy_tier` and is not vault content. Unlike `creek compile` (#962),
    # there is no fragment here whose tier the router could enforce.
    classifier = LLMClassifier(config=config.model_router.resolve("classification"))
    report = run_calibration(classifier, load_fixture(resolved))
    console.print(report.render())

    if enforce_floors:
        try:
            assert_floors(report)
        except CalibrationFloorBreachError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None


@app.command()
def link(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    method: str = typer.Option(
        "embeddings",
        help="Linking method (embeddings|temporal|eddies|threads)",
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

    config = _load_config_for_vault(vault)
    vault_path = _resolve_vault(vault)

    from creek.link import EmbeddingModelUnavailableError
    from creek.link.link_engine import run_link

    try:
        summary = run_link(
            vault_path=vault_path,
            config=config,
            method=method,
            rebuild=rebuild,
        )
    except EmbeddingModelUnavailableError as exc:
        # The sentence-transformer model could not be loaded (GAP-008).
        # The exception message already names the model and spells out
        # the remediation, so surface it verbatim and exit non-zero
        # rather than letting the generic handler swallow the guidance.
        console.print(f"[red]Embedding linker aborted: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(_format_link_summary(summary))


def _format_link_summary(summary: LinkSummary) -> str:
    """Render a method-specific summary message for the ``creek link`` CLI.

    Each method gets its own phrasing because the word "link" used to mean
    two incompatible things: pairwise cosine edges cached in the parquet
    (embeddings) and topic clusters that may or may not have been written
    to disk (eddies). Per-method strings name the actual side effect so
    operators can correlate the number with what they see on disk.

    Args:
        summary: The link summary returned by
            :func:`creek.link.link_engine.run_link`.

    Returns:
        A Rich-markup string ready for :func:`console.print`.

    Raises:
        ValueError: If ``summary.method`` is not a known linker name.
    """
    fragments = summary.fragment_count
    if summary.method == "embeddings":
        body = (
            f"Embeddings linker: {fragments} fragment(s) embedded, "
            f"{summary.similarity_edges} similarity edge(s) cached in "
            f"00-Creek-Meta/embeddings.parquet."
        )
    elif summary.method == "eddies":
        body = (
            f"Eddies linker: {fragments} fragment(s) scanned, "
            f"{summary.eddies_detected} eddy(ies) detected, "
            f"{summary.eddies_written} eddy file(s) written to 03-Eddies/, "
            f"{summary.member_fragments_updated} fragment(s) updated with "
            f"`eddies:` wiki-links"
            f"{_format_cluster_stats(summary)}."
        )
    elif summary.method == "temporal":
        body = (
            f"Temporal linker: {fragments} fragment(s), {summary.link_count} link(s)."
        )
    elif summary.method == "threads":
        body = (
            f"Threads linker: {fragments} fragment(s) scanned, "
            f"{summary.threads_detected} thread(s) detected, "
            f"{summary.threads_written} page(s) written to "
            "02-Threads/{Active,Dormant,Resolved}/, "
            f"{summary.member_fragments_updated} fragment(s) updated with "
            "`threads:` wiki-links"
            f"{_format_cluster_stats(summary)}."
        )
    else:
        msg = f"Unknown link method: {summary.method!r}"
        raise ValueError(msg)
    return f"[bold green]{body}[/bold green]"


def _format_cluster_stats(summary: LinkSummary) -> str:
    """Render the clustering-health clause for a link summary.

    Issue #880: the largest cluster is the one number that tells an
    operator whether detection degenerated — a single eddy holding 87% of
    the vault used to be invisible from the CLI. Splits and discards are
    reported only when they happened, so an ordinary run stays terse; a
    discard is data loss (those fragments carry no wiki-link at all) and
    therefore always named when non-zero.

    Args:
        summary: The link summary being rendered.

    Returns:
        A clause to append to the method's sentence, starting with ``", "``,
        or an empty string when nothing was detected.
    """
    if summary.largest_cluster_fragments == summary.oversized_discarded == 0:
        return ""
    parts = [f"largest cluster: {summary.largest_cluster_fragments} fragment(s)"]
    if summary.clusters_split:
        parts.append(f"{summary.clusters_split} oversized cluster(s) re-clustered")
    if summary.oversized_discarded:
        parts.append(
            f"{summary.oversized_discarded} fragment(s) discarded as unsplittable",
        )
    return ", " + ", ".join(parts)


@app.command()
def index(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Regenerate the Dataview index notes without running the full pipeline.

    Runs only the index stage: rebuilds the per-frequency indexes under
    ``06-Frequencies/``, the master thread index, the eddy map, and the temporal
    and source indexes from whatever fragments and threads already live in the
    vault. Deterministic and local — no ingest, redact, classify, link, or LLM —
    so operators who added fragments or re-linked can refresh the indexes in
    place instead of re-running all of ``process``. Re-running overwrites each
    note with identical content (idempotent).
    """
    from creek.generate.indexes import IndexGenerator

    vault_path = _resolve_vault(vault)
    generated = IndexGenerator(vault_path=vault_path).generate_all()
    rel = ", ".join(str(path.relative_to(vault_path)) for path in generated)
    console.print(
        f"[bold green]Wrote {len(generated)} index note(s): {rel}[/bold green]",
    )


_FILL_SUMMARY_FOLDERS: tuple[str, ...] = (
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
)
"""Vault folders whose markdown population ``creek fill`` reports in its summary."""


def _build_fill_steps(
    vault_path: Path,
    config: CreekConfig,
    *,
    with_compost: bool,
) -> list[tuple[str, Callable[[], object]]]:
    """Build the ordered ``(label, action)`` plan for ``creek fill``.

    Pure orchestration: each action calls an existing step command/function in
    dependency order — embeddings/temporal/eddies/thread-page links first (links
    must precede the thread and eddy pages), then the deterministic reports, then
    the index regeneration. ``--with-compost`` appends the deterministic compost
    overview report. No generator logic lives here.

    Args:
        vault_path: Vault root.
        config: Loaded Creek configuration (threaded into the link steps).
        with_compost: When ``True`` append the compost overview report step.

    Returns:
        The ordered list of ``(label, zero-arg callable)`` steps.
    """
    from creek.generate.indexes import IndexGenerator
    from creek.link.link_engine import run_link

    def _link(method: str) -> Callable[[], object]:
        return lambda: run_link(
            vault_path=vault_path, config=config, method=method, rebuild=False
        )

    # ``creek fill`` has no ``--include-tier`` of its own, and an absent flag
    # means unfiltered on the report surface (#968), so every step states
    # ``ALL`` rather than inheriting a default nobody typed.
    unfiltered = PrivacyTierOverride.ALL
    steps: list[tuple[str, Callable[[], object]]] = [
        ("link/embeddings", _link("embeddings")),
        ("link/temporal", _link("temporal")),
        ("link/eddies", _link("eddies")),
        ("link/threads", _link("threads")),
        ("report/decisions", lambda: _report_decisions(vault_path, unfiltered)),
        ("report/unnamed", lambda: _report_unnamed(vault_path, unfiltered)),
        ("report/paradox", lambda: _report_paradox(vault_path, unfiltered)),
        ("report/synchronicity", lambda: _report_synchronicity(vault_path, unfiltered)),
        ("report/mode-profiles", lambda: _report_mode_profiles(vault_path, unfiltered)),
        # #879: ``fill`` is the "make my vault prod-ready" command, so the voice
        # report belongs in it — otherwise the register samples are only ever
        # written by an operator who knows to run ``report --type voice`` by hand.
        ("report/voice", lambda: _report_voice(vault_path, unfiltered)),
        ("report/wavelength", lambda: _report_wavelength(vault_path, "weekly")),
        ("index", lambda: IndexGenerator(vault_path=vault_path).generate_all()),
    ]
    if with_compost:
        steps.append(("compost/report", lambda: _fill_compost_report(vault_path)))
    return steps


def _fill_compost_report(vault_path: Path) -> object:
    """Run the deterministic compost overview report (the opt-in fill step)."""
    from creek.generate.compost import CompostTracker

    return CompostTracker().generate_compost_report(vault_path)


def _count_markdown(folder: Path) -> int:
    """Count non-hidden markdown files under *folder* (0 when it is absent)."""
    if not folder.exists():
        return 0
    return sum(1 for p in folder.rglob("*.md") if not p.name.startswith("."))


def _format_fill_summary(vault_path: Path) -> str:
    """Render the per-folder population summary line for ``creek fill``."""
    parts = [
        f"{name} {_count_markdown(vault_path / name)}" for name in _FILL_SUMMARY_FOLDERS
    ]
    return "[bold green][fill] done. " + " | ".join(parts) + "[/bold green]"


@dataclass(frozen=True)
class _ClassifyUpgradeOffer:
    """A quality-aware classification upgrade ``creek fill`` can offer (#736).

    Attributes:
        count: Number of ``rules``-classified fragments that a now-available
            higher-fidelity method would improve.
        non_intimate_label: ``provider/model`` the non-Intimate route would use.
        intimate_label: ``provider/model`` the Intimate route would use (always
            local — Intimate is never proposed a cloud upgrade).
    """

    count: int
    non_intimate_label: str
    intimate_label: str

    def context(self) -> str:
        """The bold context line printed before the prompt."""
        return (
            f"[yellow][fill] {self.count} fragment(s) were classified by `rules`; "
            "a higher-fidelity method is now available "
            f"({self.non_intimate_label} for non-Intimate; "
            f"{self.intimate_label} for Intimate, kept local).[/yellow]"
        )

    def hint(self) -> str:
        """The non-interactive hint (printed when no upgrade is performed)."""
        return (
            f"[dim][fill] {self.count} `rules`-classified fragment(s) could be "
            f"upgraded ({self.non_intimate_label} / {self.intimate_label}); "
            "re-run with --upgrade to apply (Intimate stays local).[/dim]"
        )


def _classification_route_label(config: CreekConfig, *, intimate: bool) -> str:
    """Return a ``provider/model`` label for the classification route."""
    from creek.classify.llm.router import IntimateRoutingError
    from creek.models import PrivacyTier

    router = config.model_router
    try:
        cfg = router.resolve(
            "classification", PrivacyTier.INTIMATE if intimate else None
        )
    except IntimateRoutingError:
        return "rules (no local backend)"
    return f"{cfg.provider}/{cfg.model}" if cfg.model else cfg.provider


def _detect_classify_upgrade(
    vault_path: Path, config: CreekConfig
) -> _ClassifyUpgradeOffer | None:
    """Return an upgrade offer when ``rules`` artifacts could now do better.

    A fragment is upgradeable when it was classified by ``rules`` and its tier's
    best-available method (per the fidelity ladder) outranks ``rules`` — i.e. an
    LLM is now reachable. Returns ``None`` when no LLM is available or no
    ``rules`` fragment would benefit (so the offer never appears spuriously).
    """
    from creek.classify.constants import (
        CLASSIFICATION_METHOD_KEY,
        RULES_METHOD,
    )
    from creek.classify.fidelity import RANK_RULES, best_available
    from creek.classify.privacy_filter import tier_of
    from creek.vault.reader import iter_vault_fragments

    best = best_available(config)
    if not best.any_llm_available():
        return None
    upgradeable = 0
    for _path, fragment, _body, raw in iter_vault_fragments(
        vault_path / "01-Fragments"
    ):
        if raw.get(CLASSIFICATION_METHOD_KEY) != RULES_METHOD:
            continue
        if best.for_tier(tier_of(fragment)) > RANK_RULES:
            upgradeable += 1
    if upgradeable == 0:
        return None
    return _ClassifyUpgradeOffer(
        count=upgradeable,
        non_intimate_label=_classification_route_label(config, intimate=False),
        intimate_label=_classification_route_label(config, intimate=True),
    )


@dataclass(frozen=True)
class _FillGapCounts:
    """What one walk of the vault found for ``creek fill``'s hints.

    Every gap detector needs the same expensive thing — each fragment's
    frontmatter *and* body, parsed. Bundling their answers lets
    :func:`_scan_fill_gaps` pay for that walk once on a 35k-fragment
    vault instead of once per hint.

    Attributes:
        untiered: Fragments still carrying no privacy tier (#876).
        praxis_backfillable: Fragments the free praxis heuristic can
            prove are ``explicit`` while the disk still says ``none``
            (#877).
        tags_backfillable: Fragments whose body carries a hashtag the
            frontmatter does not record (#878).
    """

    untiered: int
    praxis_backfillable: int
    tags_backfillable: int


def _scan_fill_gaps(vault_path: Path) -> _FillGapCounts:
    """Walk the vault once and count every class of fill-time gap.

    The praxis detector deserves a note, because the two obvious choices
    are both wrong. It cannot be "the ``praxis_potential`` key is absent"
    — ``Fragment.model_dump`` always writes it — and it must not be "the
    value is ``none``", because most fragments genuinely are ``none``, so
    that nag would be permanent and instantly ignored. The one shape that
    *proves* work is outstanding is this: the free keyword heuristic says
    ``explicit`` while the disk still says ``none``. That is positive
    evidence the fragment was classified before the field had a producer,
    it costs zero tokens to compute, and it goes quiet the moment the
    operator acts on it.

    The tags detector (#878) is the same shape for the same reasons:
    ``tags`` is always written, and "the list is empty" would nag forever
    about the many fragments that genuinely have no hashtags. The
    provable gap is "the free extractor finds a tag the disk does not
    record", which
    :func:`~creek.classify.tags_pass.has_unrecorded_tags` answers in the
    same normalised space the merge works in — comparing raw strings
    would report ``tags: [Recovery]`` beside a body reading ``#recovery``
    as a gap and bill the operator for a paid ``--force`` run that
    changes nothing. Computing it here rather than in its own function is
    what keeps it free — it rides the walk the other two already pay for,
    and must never become a second pass over 35k files.

    The imports are function-local (like the rest of this module's) so a
    bare ``creek --help`` never pays to import the classification stack.

    Args:
        vault_path: Vault root.

    Returns:
        The :class:`_FillGapCounts` for this vault; all zeroes when there
        is no ``01-Fragments`` directory at all.
    """
    from creek.classify.praxis_pass import detect
    from creek.classify.privacy_pass import needs_tier
    from creek.classify.tags_pass import has_unrecorded_tags
    from creek.vault.reader import iter_vault_fragments

    untiered = 0
    backfillable = 0
    untagged = 0
    for _path, fragment, body, raw in iter_vault_fragments(
        vault_path / "01-Fragments",
    ):
        if needs_tier(raw):
            untiered += 1
        recorded_none = str(fragment.praxis_potential) == PraxisPotential.NONE.value
        if recorded_none and detect(fragment, body) is PraxisPotential.EXPLICIT:
            backfillable += 1
        if has_unrecorded_tags(fragment.tags, body):
            untagged += 1
    return _FillGapCounts(
        untiered=untiered,
        praxis_backfillable=backfillable,
        tags_backfillable=untagged,
    )


def _scan_fill_gaps_quietly(vault_path: Path) -> _FillGapCounts | None:
    """Run :func:`_scan_fill_gaps`, swallowing any failure.

    The shared scan is an optimisation, not a step: if it fails, each
    hint below still falls back to its own scan and reports its own
    failure through its own guard, so one broken hint can never silence
    the other.

    Args:
        vault_path: Vault root.

    Returns:
        The counts, or ``None`` when the walk raised.
    """
    try:
        return _scan_fill_gaps(vault_path)
    except Exception as exc:
        logger.warning("fill: shared fragment-gap scan failed: %s", exc)
        return None


def _count_untiered_fragments(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> int:
    """Count fragments under *vault_path* that still carry no privacy tier.

    "Untiered" is both on-disk shapes a fragment that has never been
    through a privacy pass can take: the ``privacy_tier`` key is absent,
    or it is present as ``unclassified`` (issue #876). Since #876 those
    fragments are gated like ``personal`` by every generation flow, so a
    vault full of them silently yields thin drafts and empty mining runs
    — worth telling the operator about.

    Args:
        vault_path: Vault root.
        scan: An already-completed :func:`_scan_fill_gaps` result to read
            the answer out of. Omit it to walk the vault here.

    Returns:
        The number of untiered fragments; ``0`` when the vault has no
        ``01-Fragments`` directory at all.
    """
    if scan is None:
        scan = _scan_fill_gaps(vault_path)
    return scan.untiered


def _count_praxis_backfillable_fragments(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> int:
    """Count fragments whose praxis verdict a ``--force`` run would raise.

    See :func:`_scan_fill_gaps` for why "the heuristic proves ``explicit``
    but the disk says ``none``", and not "the value is ``none``", is the
    gap worth reporting (issue #877).

    Args:
        vault_path: Vault root.
        scan: An already-completed :func:`_scan_fill_gaps` result to read
            the answer out of. Omit it to walk the vault here.

    Returns:
        The number of backfillable fragments; ``0`` when the vault has no
        ``01-Fragments`` directory at all.
    """
    if scan is None:
        scan = _scan_fill_gaps(vault_path)
    return scan.praxis_backfillable


def _count_tags_backfillable_fragments(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> int:
    """Count fragments whose body carries a hashtag ``tags`` does not record.

    See :func:`_scan_fill_gaps` for why "the extractor finds a tag the
    disk is missing", and not "the list is empty", is the gap worth
    reporting (issue #878).

    Args:
        vault_path: Vault root.
        scan: An already-completed :func:`_scan_fill_gaps` result to read
            the answer out of. Omit it to walk the vault here.

    Returns:
        The number of backfillable fragments; ``0`` when the vault has no
        ``01-Fragments`` directory at all.
    """
    if scan is None:
        scan = _scan_fill_gaps(vault_path)
    return scan.tags_backfillable


def _hint_untiered_fragments(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> None:
    """Print the ``creek classify`` nudge when untiered fragments remain.

    Best-effort and advisory, exactly like the classify-upgrade probe it
    sits beside (#736): an unreadable fragment must never abort ``creek
    fill`` before a single step has run. Silent on a fully-tiered vault so
    the nag cannot become permanent.

    Args:
        vault_path: Vault root.
        scan: The shared vault walk, when one already succeeded.
    """
    try:
        untiered = _count_untiered_fragments(vault_path, scan)
    except Exception as exc:
        logger.warning("fill: skipping untiered-fragment hint: %s", exc)
        return
    if untiered == 0:
        return
    console.print(
        f"[dim][fill] {untiered} fragment(s) are untiered "
        "(privacy_tier absent or `unclassified`); they are gated like "
        "`personal`, so they contribute summaries only. "
        "Run `creek classify` to assign real tiers.[/dim]",
    )


def _hint_praxis_backfill(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> None:
    """Print the backfill nudge when praxis verdicts are provably missing.

    Own guard, own warning: sharing the untiered hint's ``try`` would let
    one failing scan silently suppress the other hint too. Silent at
    zero, so a vault the operator has already backfilled never nags.

    Reports a **count only** — never a fragment id or a body excerpt.
    Naming either would turn an advisory line into an unaudited
    disclosure of vault content through stdout (the config-oracle rule,
    #846 / #848).

    Args:
        vault_path: Vault root.
        scan: The shared vault walk, when one already succeeded.
    """
    try:
        backfillable = _count_praxis_backfillable_fragments(vault_path, scan)
    except Exception as exc:
        logger.warning("fill: skipping praxis-backfill hint: %s", exc)
        return
    if backfillable == 0:
        return
    console.print(
        f"[dim][fill] {backfillable} fragment(s) show explicit praxis signals "
        "but are recorded as `praxis_potential: none` (classified before the "
        "field had a producer). Run `creek classify --method llm --force` to "
        "backfill — that re-sends those fragments to the configured provider "
        "and costs tokens.[/dim]",
    )


def _hint_tags_backfill(
    vault_path: Path,
    scan: _FillGapCounts | None = None,
) -> None:
    """Print the backfill nudge when body hashtags are provably unrecorded.

    Own guard, own warning: sharing the praxis hint's ``try`` would let
    one failing scan silently suppress the other hint too. Silent at
    zero, so a vault the operator has already backfilled never nags.

    Reports a **count only** — never a fragment id, a body excerpt, or
    the tags themselves. Any of those would turn an advisory line into an
    unaudited disclosure of vault content through stdout (the
    config-oracle rule, #846 / #848), and the tags are the most
    disclosive of the three.

    The named remedy is ``creek classify --method llm --force``, and its
    token cost is stated. It is deliberately **not**
    ``--method rules --force``, which would be free: on this path
    :func:`creek.classify.classify_engine._classify_one` returns
    :meth:`~creek.classify.rules.RuleClassifier.classify`'s answer, which
    overwrites ``frequency`` / ``wavelength`` / ``voice`` wholesale and
    re-stamps ``classification_method: rules``. On a vault already
    classified by an LLM that trades every considered classification for
    keyword output — a catastrophic price for a tag.

    Args:
        vault_path: Vault root.
        scan: The shared vault walk, when one already succeeded.
    """
    try:
        backfillable = _count_tags_backfillable_fragments(vault_path, scan)
    except Exception as exc:
        logger.warning("fill: skipping tags-backfill hint: %s", exc)
        return
    if backfillable == 0:
        return
    console.print(
        f"[dim][fill] {backfillable} fragment(s) carry hashtags in their body "
        "that `tags` does not record (ingested before the field had a "
        "producer). Run `creek classify --method llm --force` to backfill — "
        "that re-sends those fragments to the configured provider and costs "
        "tokens.[/dim]",
    )


def _run_classify_upgrade(vault_path: Path, config: CreekConfig) -> None:
    """Re-classify ``rules`` fragments via the LLM, preserving manual/llm.

    Runs ``classify --method llm`` WITHOUT ``--force``: the resume-skip leaves
    ``llm``/``manual`` fragments untouched, so only the ``rules`` ones are
    upgraded, and the per-tier ModelRouter keeps Intimate content local.
    """
    from creek.classify.classify_engine import run_classify

    console.print(
        "[bold green][fill] upgrading classify: rules → LLM "
        "(non-Intimate per config; Intimate kept local) …[/bold green]"
    )
    run_classify(vault_path=vault_path, config=config, method="llm", force=False)


def _maybe_upgrade_classification(
    vault_path: Path, config: CreekConfig, *, upgrade: bool
) -> None:
    """Surface / apply a quality-aware classification upgrade before filling.

    Non-interactive default is a safe no-op (never silently overwrites, never
    silently egresses): it only prints a hint. ``--upgrade`` applies the upgrade
    without a prompt; an interactive TTY prompts ``[y/N]`` (default No).

    The #876 untiered-fragment hint, the #877 praxis-backfill hint and the
    #878 tags-backfill hint all fire first, ahead of every early return: the
    vault that most needs them (rules-classified, no LLM reachable, every
    fragment untiered) is exactly the one where there is no upgrade to offer.
    All three read one shared vault walk so ``creek fill`` parses every
    fragment once, not once per hint.
    """
    gaps = _scan_fill_gaps_quietly(vault_path)
    _hint_untiered_fragments(vault_path, gaps)
    _hint_praxis_backfill(vault_path, gaps)
    _hint_tags_backfill(vault_path, gaps)
    try:
        offer = _detect_classify_upgrade(vault_path, config)
    except Exception as exc:
        # The upgrade check is best-effort: a provider/config hiccup (an
        # unexpected build_provider error, a malformed routing block) must not
        # crash fill before any step runs. Skip the offer and carry on.
        logger.warning("fill: skipping classify-upgrade check: %s", exc)
        return
    if offer is None:
        return
    if upgrade:
        _run_classify_upgrade(vault_path, config)
        return
    if not sys.stdin.isatty():
        console.print(offer.hint())
        return
    console.print(offer.context())
    if typer.confirm("Re-run classification to upgrade?", default=False):
        _run_classify_upgrade(vault_path, config)
    else:
        console.print(
            "[dim][fill] declined — existing classifications kept "
            "(second run is a clean no-op).[/dim]"
        )


@app.command()
def fill(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without running any step."
    ),
    with_compost: bool = typer.Option(
        False,
        "--with-compost",
        help="Also run the compost overview report (off by default).",
    ),
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help=(
            "Re-classify `rules`-stamped fragments with the best LLM now "
            "available before filling (Intimate stays local). Non-interactive "
            "opt-in; without it a TTY is prompted and other runs are a no-op."
        ),
    ),
) -> None:
    """Run the deterministic vault-population sequence in dependency order.

    An umbrella over the existing per-generator steps: it runs the link stages
    (embeddings, temporal, eddies, thread-pages), the deterministic reports
    (decisions, unnamed, paradox, synchronicity, mode-profiles, wavelength), and
    the index regeneration — so an already-classified, already-embedded vault
    becomes prod-ready from one command. Classification and embedding are
    preconditions, not run here.

    Each step is non-fatal: a failure is logged and the sequence continues, so
    one bad step cannot abort the rest. ``--dry-run`` prints the plan and runs
    nothing. ``--with-compost`` appends the deterministic compost overview
    report, which surveys whatever notes ``creek compost scan`` has filed —
    run that first, or the report has nothing to survey (#882). Every
    underlying step is idempotent, so a second ``creek fill`` is a clean no-op.
    """
    config = _load_config_for_vault(vault)
    vault_path = _resolve_vault(vault)
    steps = _build_fill_steps(vault_path, config, with_compost=with_compost)

    if dry_run:
        console.print("[bold]creek fill plan (dry-run, nothing run):[/bold]")
        for index_, (label, _action) in enumerate(steps, start=1):
            console.print(f"  {index_:>2}. {label}")
        return

    # Quality-aware upgrade (#736): offer/apply re-classification of `rules`
    # fragments before the steps consume them. Safe no-op unless --upgrade or an
    # interactive yes; the ModelRouter keeps Intimate content local.
    _maybe_upgrade_classification(vault_path, config, upgrade=upgrade)

    for label, action in steps:
        console.print(f"[dim][fill] {label} …[/dim]")
        try:
            action()
        except Exception as exc:
            # Orchestrator contract: a single failing step (missing embedding
            # model, empty dimension, I/O error) must not abort the remaining
            # steps. Catch broadly, log, and carry on; the final summary shows
            # what actually populated.
            logger.warning("fill step %s failed: %s", label, exc)
            console.print(f"[yellow][fill] {label} failed: {exc}[/yellow]")

    console.print(_format_fill_summary(vault_path))


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
    """Roll a fragment up into a compiled-layer page.

    Reads the source fragment from ``<vault>/01-Fragments``, calls the
    configured LLM to synthesise claims with per-claim provenance back
    to the fragment ID, and writes the result to the appropriate
    compiled-layer directory. LLM-detected paradoxes are routed to the
    side-channel log under ``00-Creek-Meta/Processing-Log/`` rather
    than flattened into the synthesis page.

    The privacy tier of the fragments being rolled up decides where the
    synthesis runs: an intimate source is forced onto the local model, and
    refused outright when no local model is configured to fall back to.
    """
    from creek.classify.llm.router import IntimateRoutingError
    from creek.compile.engine import TARGET_KINDS, compile_to_vault, default_llm

    if target_kind not in TARGET_KINDS:
        console.print(
            f"[red]Unknown --target-kind {target_kind!r}. "
            f"Supported: {', '.join(TARGET_KINDS)}.[/red]",
        )
        raise typer.Exit(code=2)

    config = _load_config_for_vault(vault)
    vault_path = _resolve_vault(vault)
    kind = cast("CompileTargetKind", target_kind)
    try:
        written = compile_to_vault(
            fragment_ids=[fragment_id],
            vault_path=vault_path,
            target_kind=kind,
            target_id=target_id,
            target_title=target_title,
            # The content tier alone, with no ceiling term — unlike
            # ``creek_mcp.tools.compile``, which reconciles the two via
            # ``routing_tier``. The CLI operator *is* the vault owner, so
            # there is no caller declaration to reconcile against. Modelling
            # the CLI as ``ceiling=all`` would not be conservative but
            # wrong: ``CEILING_ROUTING_TIER[ALL] == INTIMATE``, which would
            # pin every compile of every open fragment to the local model.
            llm_factory=lambda tier: default_llm(
                config.model_router.resolve("generation", tier),
            ),
        )
    except IntimateRoutingError as exc:
        # The one refusal that must be legible. Threading the tier through
        # *introduces* this failure mode on a P1 operator path (all-cloud
        # config + intimate content), and a stack trace would read as a
        # crash rather than as the privacy guarantee doing its job.
        # Deliberately narrow: the not-found and target-escape ``ValueError``s
        # already surface as tracebacks today, and widening the handler here
        # would fold an unrelated UX gap into a privacy fix.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]Compiled {fragment_id} -> {written}[/bold green]",
    )


def _report_tags(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate the tag-garden report.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the scan (#968). Note the garden is
            fragment-derived only below ``intimate``: the four non-fragment
            scan directories hold note types with no ``privacy_tier``.
    """
    from creek.generate.tags import TagGardenGenerator

    path = TagGardenGenerator(
        vault_path=vault_path,
        override=override,
    ).generate_garden()
    console.print(f"[bold green]Tag Garden generated: {path}[/bold green]")


def _report_unnamed(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate the weekly unnamed-fragment digest.

    Args:
        vault_path: Vault root.
        override: Unused. ``unnamed`` is not exposed over MCP and is outside
            #968's scope; the parameter exists only to satisfy
            :data:`_REPORT_DISPATCH`'s uniform signature.
    """
    del override  # not MCP-exposed; out of #968's scope, not half-wired.

    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from creek.generate.unnamed import UnnamedDigestGenerator
    from creek.link.embeddings import EmbeddingLinker

    config = _load_config_for_vault(vault_path)
    linker = EmbeddingLinker(config=config.embeddings)
    digest_generator = UnnamedDigestGenerator(embedding_linker=linker)
    today = _date.today()
    week_start = today - _timedelta(days=today.weekday())
    digest_path = digest_generator.generate_weekly_digest(vault_path, week_start)
    console.print(
        f"[bold green]Unnamed digest generated: {digest_path}[/bold green]",
    )


def _report_voice(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate per-register voice profiles and register samples (#879).

    Two halves of one report, over one corpus: the rendered profiles under
    ``07-Voice/`` and the ranked exemplar copies under
    ``07-Voice/Register-Samples/``.

    The second half also **deletes** — a sample whose fragment has left the
    corpus is removed — so this is a report that mutates the vault in both
    directions, and prints both.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the exemplar walk (#968). Additive to the
            collector's ``allow_intimate`` consent gate, never a replacement
            for it.
    """
    from creek.generate.voice import VoiceProfileGenerator, generate_register_samples

    profile_paths = VoiceProfileGenerator(override=override).generate_all_profiles(
        vault_path,
    )
    # Deliberately above the no-profiles early return: an emptied corpus has
    # nothing left to profile, but the samples tree may still hold verbatim
    # copies of the fragments that left it, and this is the call that prunes
    # them (#879).
    #
    # This is a SECOND full walk of 01-Fragments, and knowingly so. It cannot
    # share ``generate_all_profiles``' walk without a refactor: below,
    # ``_persist_fragment`` copies a fragment's source file only while the
    # collector's ``_records`` cache holds its entry, and only
    # ``collect_exemplars`` fills that cache — so saving through a collector
    # that never walked writes a full set of valid-looking samples with empty
    # bodies. Measured on synthetic vaults (1k/5k fragments, linear scaling):
    # the redundant walk takes ``_report_voice`` from 1.0x to 1.8x one corpus
    # parse (~16s of ~39s projected at 35k). The trade is bounded because
    # ``creek fill`` already makes ~12 full-corpus parse passes, so this is
    # ~7% of its parse budget and less of its wall clock, which belongs to
    # embedding inference and DBSCAN. ``save_exemplars`` itself is O(1) in
    # vault size (<=7 registers x 20 copies). Sharing the walk is issue #1220.
    pruned: list[Path] = []
    summaries = generate_register_samples(
        vault_path,
        override=override,
        on_prune=pruned.append,
    )
    # Announced before the early return, because the emptied corpus that
    # takes that return is exactly the run with the most to delete. This
    # command removes files from the vault, ``creek fill`` runs it
    # unattended, and a silent deletion is one nobody can audit (#879).
    if pruned:
        console.print(
            f"[yellow]Stale register samples pruned ({len(pruned)})[/yellow]",
        )
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
    registers = ", ".join(sorted(summaries))
    console.print(
        f"[bold green]Register samples written ({len(summaries)}): "
        f"{registers}[/bold green]",
    )


def _report_decisions(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate draft Decision notes from decision-signalling fragments (#581).

    Scans the vault's fragments for decision signals and writes a draft note per
    *new* candidate to ``08-Decisions/Active/``; re-running is idempotent (a
    fragment already captured by a note is skipped). Prints a friendly message
    when there are no new candidates.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the fragment walk (#968). A written note's
            filename and ``title:`` are a source fragment's title verbatim.
    """
    from creek.generate.decisions import generate_decisions

    written_paths = generate_decisions(vault_path, override=override)
    if not written_paths:
        console.print(
            "[yellow]No decision notes generated: "
            "no new decision candidates found.[/yellow]",
        )
        return
    rels = ", ".join(str(p.relative_to(vault_path)) for p in written_paths)
    console.print(
        f"[bold green]Decision notes generated ({len(written_paths)}): "
        f"{rels}[/bold green]",
    )


def _report_lexicon(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate the voice lexicon glossary + metaphor index (#580).

    Collects the vault's voice exemplars (sharing the voice report's eligibility
    gate), builds a :class:`~creek.generate.lexicon.Lexicon`, and persists it to
    ``07-Voice/Lexicon/``. Mirrors ``_report_voice``'s friendly "no qualifying
    exemplars" message when the corpus is empty.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the exemplar walk (#968). Borrowed-term
            entries record the whole surrounding sentence verbatim.
    """
    from creek.generate.lexicon import generate_lexicon

    lexicon, written_paths = generate_lexicon(vault_path, override=override)
    if lexicon is None or not written_paths:
        console.print(
            "[yellow]No lexicon generated: no qualifying exemplars found.[/yellow]",
        )
        return
    glossary = written_paths[0]
    console.print(
        f"[bold green]Lexicon generated: {glossary.relative_to(vault_path)} "
        f"({len(lexicon.coined_terms)} coined terms, "
        f"{len(lexicon.metaphors)} metaphors)[/bold green]",
    )


def _report_rhetorical_patterns(
    vault_path: Path,
    override: PrivacyTierOverride,
) -> None:
    """Persist per-register rhetorical-pattern notes (#582).

    Writes the ontology's "### Rhetorical Moves" section per voice register to
    ``07-Voice/Rhetorical-Patterns/``, reusing the voice subsystem's existing
    move detection. Mirrors ``_report_voice``'s friendly "no qualifying
    exemplars" message when the corpus is empty.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the exemplar walk (#968). The notes hold
            only integer counts, but *which* per-register files exist is
            itself derived from who entered the corpus.
    """
    from creek.generate.voice import VoiceProfileGenerator

    written_paths = VoiceProfileGenerator(
        override=override,
    ).generate_rhetorical_patterns(vault_path)
    if not written_paths:
        console.print(
            "[yellow]No rhetorical patterns written: "
            "no qualifying exemplars found.[/yellow]",
        )
        return
    names = ", ".join(str(p.relative_to(vault_path)) for p in written_paths)
    console.print(
        f"[bold green]Rhetorical patterns written ({len(written_paths)}): "
        f"{names}[/bold green]",
    )


def _report_mode_profiles(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Generate per-mode wavelength profiles to 05-Wavelength/Mode-Profiles/ (#583).

    Writes one note per engagement mode that has fragments; prints a friendly
    message when no fragment carries a classified mode.

    Args:
        vault_path: Vault root.
        override: Tier ceiling for the fragment walk (#968). A mode profile
            lists sample fragment titles.
    """
    from creek.generate.wavelength import ModeProfileGenerator

    written_paths = ModeProfileGenerator().generate_mode_profiles(
        vault_path,
        override=override,
    )
    if not written_paths:
        console.print(
            "[yellow]No mode profiles written: "
            "no classified-mode fragments found.[/yellow]",
        )
        return
    names = ", ".join(str(p.relative_to(vault_path)) for p in written_paths)
    console.print(
        f"[bold green]Mode profiles written ({len(written_paths)}): "
        f"{names}[/bold green]",
    )


_WAVELENGTH_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_WAVELENGTH_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _resolve_wavelength_period(period: str | None) -> tuple[str, date] | None:
    """Resolve a ``--period`` string to a ``(mode, anchor-date)`` pair.

    Accepts the relative keywords ``weekly`` / ``monthly`` (anchored on today)
    and explicit ``YYYY-Www`` (ISO week) / ``YYYY-MM`` (calendar month) periods,
    so an operator can regenerate a historical phase-map deterministically.

    Args:
        period: The raw ``--period`` value.

    Returns:
        ``("weekly", anchor)`` or ``("monthly", anchor)`` where *anchor* is any
        date inside the target window, or ``None`` when *period* is missing or
        unparseable (the caller surfaces the error).
    """
    if period is None:
        return None
    if period in {"weekly", "monthly"}:
        return (period, date.today())
    week_match = _WAVELENGTH_ISO_WEEK_RE.match(period)
    if week_match:
        year, week = int(week_match.group(1)), int(week_match.group(2))
        try:
            return ("weekly", date.fromisocalendar(year, week, 1))
        except ValueError:
            return None
    month_match = _WAVELENGTH_MONTH_RE.match(period)
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        try:
            return ("monthly", date(year, month, 1))
        except ValueError:
            return None
    return None


def _wavelength_dimension_populated(fragments: list[Fragment]) -> bool:
    """Return whether any *fragments* carry a classified wavelength phase."""
    # Local import: cli.py defers model imports to keep CLI startup fast.
    from creek.models import Phase

    return any(f.wavelength.phase != Phase.UNCLASSIFIED for f in fragments)


def _report_wavelength(vault_path: Path, period: str | None) -> None:
    """Generate a descriptive wavelength phase-map to ``05-Wavelength/Phase-Maps/``.

    Wires the deterministic :class:`WavelengthTracker` weekly/monthly generators
    to the report surface. Supports ``--period weekly|monthly`` (today) and
    explicit ``YYYY-Www`` / ``YYYY-MM`` periods. When no fragment carries a
    classified wavelength phase the dimension is unpopulated, so it prints an
    informative message instead of writing a silent empty file. Wavelength
    output is descriptive only — it never prescribes action.
    """
    from creek.generate.wavelength import (
        WavelengthTracker,
        load_fragments_from_vault,
    )

    resolved = _resolve_wavelength_period(period)
    if resolved is None:
        console.print(
            "[red]--period must be 'weekly', 'monthly', an ISO week "
            "(YYYY-Www), or a month (YYYY-MM) for wavelength reports.[/red]",
        )
        raise typer.Exit(code=2)
    mode, anchor = resolved

    fragments = load_fragments_from_vault(vault_path)
    if not _wavelength_dimension_populated(fragments):
        console.print(
            "[yellow]No wavelength phase-map written: no fragment carries a "
            "classified wavelength phase. Classify the wavelength dimension "
            "first (see #319).[/yellow]",
        )
        return

    tracker = WavelengthTracker()
    if mode == "weekly":
        wavelength_path = tracker.generate_weekly_report(
            vault_path, week_of=anchor, fragments=fragments
        )
    else:
        wavelength_path = tracker.generate_monthly_report(
            vault_path, month=anchor, fragments=fragments
        )
    console.print(
        f"[bold green]Wrote {wavelength_path.relative_to(vault_path)} "
        "(descriptive; non-prescriptive)[/bold green]",
    )


def _report_fingerprint(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Build and persist the voice fingerprint (FEAT-040.2).

    Args:
        vault_path: Vault root.
        override: Unused. ``fingerprint`` is not exposed over MCP and is
            outside #968's scope; the parameter exists only to satisfy
            :data:`_REPORT_DISPATCH`'s uniform signature.
    """
    del override  # not MCP-exposed; out of #968's scope, not half-wired.

    from creek.config import load_config
    from creek.generate.ai_style.fingerprint import (
        build_fingerprint,
        save_fingerprint,
    )

    full_config = load_config()
    config = full_config.ai_style
    fingerprint = build_fingerprint(
        vault_path,
        config,
        audience_weighting=full_config.voice_audience_weighting,
    )
    if fingerprint.fragment_count == 0:
        console.print(
            "[yellow]No voice fingerprint built: no self-authored "
            "fragments found in the vault.[/yellow]",
        )
        return
    path = save_fingerprint(fingerprint, vault_path, config)
    thin = (
        " [dim](thin — flagging will be softened)[/dim]"
        if (fingerprint.fragment_count < config.min_fingerprint_fragments)
        else ""
    )
    console.print(
        f"[bold green]Voice fingerprint built from "
        f"{fingerprint.fragment_count} self-authored fragment(s) "
        f"across {len(fingerprint.features)} features → {path}"
        f"[/bold green]{thin}",
    )


def _resolve_voice_fingerprint(
    vault_path: Path,
    ai_style: AIStyleConfig,
) -> VoiceFingerprint:
    """Return the vault's voice fingerprint, persisted-first then built.

    Mirrors how ``draft`` consumes the fingerprint: prefer the artefact
    saved at ``<vault>/00-Creek-Meta/voice-fingerprint.json`` and fall back
    to building one from the vault's self-authored fragments when no file
    exists yet.

    Args:
        vault_path: Vault root.
        ai_style: AI-style configuration (fingerprint path + weights).

    Returns:
        A :class:`VoiceFingerprint`; its ``fragment_count`` is ``0`` when
        neither a persisted artefact nor eligible fragments exist.
    """
    from creek.generate.ai_style.fingerprint import (
        build_fingerprint,
        load_fingerprint,
    )

    fingerprint = load_fingerprint(vault_path, ai_style)
    if fingerprint.fragment_count > 0:
        return fingerprint
    return build_fingerprint(vault_path, ai_style)


def _print_voice_check_summary(
    *,
    report: ScanReport,
    in_voice: bool,
    max_distance: float,
) -> None:
    """Render the human-readable ``voice-check`` summary.

    Args:
        report: The scan report produced for the target file.
        in_voice: Whether the file scored within bounds.
        max_distance: The threshold the distance was compared against.
    """
    verdict = (
        "[bold green]in voice[/bold green]"
        if in_voice
        else "[bold red]diverges from voice[/bold red]"
    )
    thin = " [dim](thin fingerprint — flagging softened)[/dim]"
    console.print(
        f"voice_distance: {report.voice_distance:.4f} "
        f"(max {max_distance:.4f}) → {verdict}"
        f"{thin if report.thin_fingerprint else ''}",
    )
    if not report.findings:
        return
    noun = "finding" if len(report.findings) == 1 else "findings"
    console.print(f"[bold]{len(report.findings)} {noun}:[/bold]")
    for finding in report.findings:
        console.print(f"  [dim]L{finding.line}:[/dim] {finding.message}")


def _emit_voice_check_json(
    *,
    target: Path,
    report: ScanReport,
    in_voice: bool,
    max_distance: float,
) -> None:
    """Print a machine-readable JSON object for ``voice-check --json``.

    Args:
        target: The scanned file.
        report: The scan report.
        in_voice: Whether the file scored within bounds.
        max_distance: The threshold compared against.
    """
    import json

    payload = {
        "file": str(target),
        "voice_distance": report.voice_distance,
        "max_distance": max_distance,
        "in_voice": in_voice,
        "thin_fingerprint": report.thin_fingerprint,
        "findings": [
            {
                "line": finding.line,
                "tell_id": finding.tell_id,
                "category": str(finding.category),
                "feature_key": finding.feature_key,
                "direction": str(finding.direction),
                "message": finding.message,
            }
            for finding in report.findings
        ],
    }
    # Emit raw so Rich never interprets ``[...]`` in the content as markup
    # and mangles the JSON (e.g. a finding message containing brackets).
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="voice-check")
def voice_check(
    file: Path = typer.Argument(..., help="Markdown file to score against your voice"),
    vault: Path | None = typer.Option(None, "--vault", help="Obsidian vault path"),
    max_distance: float | None = typer.Option(
        None,
        "--max-distance",
        help=(
            "Voice-distance ceiling. A file whose distance exceeds this is "
            "treated as diverging and the command exits non-zero. When "
            "omitted, defaults to the vault's configured "
            "voice_distance_upper (the same ceiling creek draft enforces)."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable JSON object instead of a text summary.",
    ),
) -> None:
    """Score a markdown FILE against the vault voice fingerprint.

    Loads the vault's voice fingerprint (the persisted
    ``00-Creek-Meta/voice-fingerprint.json`` when present, otherwise built
    from self-authored fragments — the same artefact ``creek report --type
    fingerprint`` and ``creek draft`` consume), scans FILE's content, and
    prints the resulting ``voice_distance`` plus any divergence findings.

    Exit codes: ``0`` when the file is in voice (distance within
    ``--max-distance``); ``1`` when it diverges; ``2`` for a missing file.
    When no fingerprint can be resolved (no persisted artefact and no
    eligible fragments) the command emits a notice pointing at ``creek
    report --type fingerprint`` and exits ``0`` (nothing to compare
    against — fail open so pre-commit / CI hooks do not block on an
    un-profiled vault). The scan is fully offline; no LLM or consent is
    involved.
    """
    from creek.generate.ai_style.scanner import scan

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(code=2)

    vault_path = _resolve_vault(vault)
    ai_style = _load_config_for_vault(vault).ai_style
    # Resolve the ceiling from THIS vault's config at call time so a per-vault
    # voice_distance_upper override is honoured; an explicit flag still wins.
    effective_max_distance = (
        max_distance if max_distance is not None else ai_style.voice_distance_upper
    )
    fingerprint = _resolve_voice_fingerprint(vault_path, ai_style)

    if fingerprint.fragment_count == 0:
        console.print(
            "[yellow]No voice fingerprint available (no persisted "
            "artefact and no self-authored fragments). Run "
            "[bold]creek report --type fingerprint[/bold] to build one. "
            "Skipping the check.[/yellow]",
        )
        return

    report = scan(
        file.read_text(encoding="utf-8"),
        fingerprint=fingerprint,
        config=ai_style,
    )
    in_voice = report.voice_distance <= effective_max_distance

    if json_out:
        _emit_voice_check_json(
            target=file,
            report=report,
            in_voice=in_voice,
            max_distance=effective_max_distance,
        )
    else:
        _print_voice_check_summary(
            report=report,
            in_voice=in_voice,
            max_distance=effective_max_distance,
        )

    if not in_voice:
        raise typer.Exit(code=1)


@app.command(name="voice-authenticity")
def voice_authenticity(
    vault: Path | None = typer.Option(None, "--vault", help="Obsidian vault path"),
    draft: Path | None = typer.Option(
        None,
        "--draft",
        help=(
            "Optional drafted essay to audit. Reads its voice-fidelity "
            "attestation (voice_distance) from the frontmatter for the "
            "de-slop sub-score."
        ),
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable JSON object instead of a text summary.",
    ),
) -> None:
    """Audit a vault's voice corpus (and optionally a draft) for authenticity.

    Read-only diagnostic that makes three voice defects observable:

    * **audience mix** — the voice-eligible corpus bucketed by privacy tier
      (and whether graduated audience weighting is active yet);
    * **AI-corpus leak** — the fraction of eligible fragments that are
      ``claude`` / ``chatgpt`` ingests (these leak into the voice corpus
      today);
    * **de-slop** — when ``--draft`` is given, whether the AI-mannerism guard
      left a ``voice_distance`` attestation and how many residual findings
      it recorded.

    The command mutates nothing and always exits ``0`` on success; a missing
    ``--draft`` file exits ``2``.
    """
    from creek.generate.voice_authenticity import build_voice_authenticity_report

    vault_path = _resolve_vault(vault)
    if draft is not None and not draft.exists():
        console.print(f"[red]Draft not found: {draft}[/red]")
        raise typer.Exit(code=2)

    report = build_voice_authenticity_report(vault_path, draft_path=draft)

    if json_out:
        # Emit raw so Rich never interprets ``[...]`` in the content as markup.
        typer.echo(report.to_json())
    else:
        console.print(report.summary_line(), markup=False)


def _report_paradox(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Write Paradox notes for contradictory fragment pairs (#711).

    Wires the implemented ``ParadoxDetector`` to a runnable command: scans the
    vault for contradictory pairs and writes one neutral note per paradox into
    ``10-Liminal/Paradoxes/``. Idempotent; deterministic (no LLM).

    Args:
        vault_path: Vault root.
        override: Unused. ``paradox`` is not exposed over MCP and is outside
            #968's scope; the parameter exists only to satisfy
            :data:`_REPORT_DISPATCH`'s uniform signature.
    """
    del override  # not MCP-exposed; out of #968's scope, not half-wired.

    from creek.generate.paradox import generate_paradoxes

    config = _load_config_for_vault(vault_path)
    written = generate_paradoxes(vault_path, config.embeddings)
    if not written:
        console.print(
            "[yellow]No paradox notes generated: "
            "no contradictory fragment pairs found.[/yellow]",
        )
        return
    console.print(
        f"[bold green]Paradox notes generated ({len(written)}) "
        f"in 10-Liminal/Paradoxes/[/bold green]",
    )


def _report_synchronicity(vault_path: Path, override: PrivacyTierOverride) -> None:
    """Write Synchronicity notes for surprising cross-source resonances (#711).

    Wires the implemented ``SynchronicityDetector`` to a runnable command: loads
    embeddings, computes resonances, filters for cross-source >0.9-similarity
    >30-day pairs, and writes one note per pair into
    ``10-Liminal/Synchronicities/``. Idempotent; deterministic (no LLM).

    Args:
        vault_path: Vault root.
        override: Unused. ``synchronicity`` is not exposed over MCP and is
            outside #968's scope; the parameter exists only to satisfy
            :data:`_REPORT_DISPATCH`'s uniform signature.
    """
    del override  # not MCP-exposed; out of #968's scope, not half-wired.

    from creek.generate.synchronicity import generate_synchronicities

    config = _load_config_for_vault(vault_path)
    written = generate_synchronicities(vault_path, config.embeddings)
    if not written:
        console.print(
            "[yellow]No synchronicity notes generated: "
            "no qualifying cross-source resonances found.[/yellow]",
        )
        return
    console.print(
        f"[bold green]Synchronicity notes generated ({len(written)}) "
        f"in 10-Liminal/Synchronicities/[/bold green]",
    )


_REPORT_DISPATCH: dict[str, Callable[[Path, PrivacyTierOverride], None]] = {
    "tags": _report_tags,
    "unnamed": _report_unnamed,
    "voice": _report_voice,
    "fingerprint": _report_fingerprint,
    "decisions": _report_decisions,
    "lexicon": _report_lexicon,
    "rhetorical-patterns": _report_rhetorical_patterns,
    "mode-profiles": _report_mode_profiles,
    "paradox": _report_paradox,
    "synchronicity": _report_synchronicity,
}


@app.command()
def report(
    type: str | None = typer.Option(None, help="Report type"),
    period: str | None = typer.Option(None, help="Report period"),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_REPORT_INCLUDE_TIER_HELP,
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
    config = _load_config_for_vault(vault)
    vault_path = vault or config.vault_path
    override = _parse_include_tier(include_tier)
    # NB (#968): this audits the *flag*, not the effective ceiling, and the
    # shared ``override_elevates`` helper reads backwards on this surface —
    # it was built for ``mine``/``author``, where the flag WIDENS, whereas on
    # ``report`` the flag NARROWS. It answers ``False`` for both ``None`` and
    # ``OPEN``, so a bare ``creek report`` (unfiltered, the widest scan there
    # is) and a typed ``--include-tier open`` (the narrowest) are alike
    # unaudited and indistinguishable in the log. Pinned by
    # ``tests/test_cli_privacy.py::test_report_voice_include_tier_open_writes_no_audit``
    # and tracked as issue #1218 — not fixed in place because auditing the
    # resolved ceiling here fires before ``--type`` is validated, turning the
    # #716 unknown-type exit-2 into an OSError on an unwritable vault path
    # (``tests/test_cli.py::test_report_command``), so the fix is a reordering
    # plus an audit-volume decision, not a one-line swap.
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command=f"report.{type}" if type else "report",
        override=override,
        fragment_ids=[],
    )

    handler = _REPORT_DISPATCH.get(type or "")
    if handler is not None:
        # An absent ``--include-tier`` means *unfiltered* here, not ``open``
        # (#968) — see ``_REPORT_INCLUDE_TIER_HELP``. On ``report`` the flag
        # narrows rather than widens.
        handler(vault_path, override or PrivacyTierOverride.ALL)
        return
    if type == "wavelength":
        _report_wavelength(vault_path, period)
        return
    # Unknown/missing type: fail loudly with the valid list, not a no-op (#716).
    valid = ", ".join([*_REPORT_DISPATCH, "wavelength"])
    if type is None:
        console.print(f"[red]--type is required. Valid types: {valid}.[/red]")
    else:
        # escape the operator-supplied value so it can't inject Rich markup.
        console.print(
            f"[red]Unknown report type '{escape(str(type))}'. "
            f"Valid types: {valid}.[/red]",
        )
    raise typer.Exit(code=2)


@app.command()
def state(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=_REPORT_INCLUDE_TIER_HELP,
    ),
) -> None:
    """Render ``00-Creek-Meta/State/<iso-week>.md`` audit report.

    The command is a *view* over the compiled vault layer — it never
    re-runs classification, linking, or compile. It reads existing
    fragments, threads, eddies, praxis, synchronicities, and the most
    recent ``run-summary.jsonl`` line, and writes a single markdown
    document organised in eleven sections (wavelength snapshot, vault
    summary, pre-LLM yield, liminal watch, active eddies, active threads,
    surprising connections, hyperedges, drift warnings, suggested
    questions, lint summary). ``latest.md`` next to the ISO-week file
    always points at the most recent report.

    ``--include-tier`` runs the same way round as it does on ``report``
    (#968): omitting it leaves the render **unfiltered**, because the CLI
    operator is the vault owner, and supplying it NARROWS what the artifact may
    contain. It is also half of the documented upgrade path for a pre-#969
    ``latest.md``: ``creek state --include-tier open`` re-renders and
    re-stamps so an ``open``-ceiling MCP reader stops being refused.

    Privacy override audit: the entry is written only when the flag was
    **explicitly supplied**. ``override_elevates`` answers ``True`` for
    ``PrivacyTierOverride.ALL``, so auditing the resolved ceiling would write a
    line on every bare ``creek state`` — the loudest possible record of the one
    case where the operator asked for nothing.
    """
    from creek.generate.state import StateReportGenerator

    override = _parse_include_tier(include_tier)
    vault_path = _resolve_vault(vault)
    if include_tier is not None:
        _audit_privacy_override_if_needed(
            vault_path=vault_path,
            command="state",
            override=override,
            fragment_ids=[],
        )
    written = StateReportGenerator(
        vault_path=vault_path,
        override=override or PrivacyTierOverride.ALL,
    ).write()
    console.print(f"[bold green]State report written: {written}[/bold green]")


@app.command(name="state-budget")
def state_budget(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
) -> None:
    """Verify ``00-Creek-Meta/State/latest.md`` is within its size budget.

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
    """Run unified vault hygiene checks.

    Default behaviour runs all deterministic checks. Pass ``--since`` to
    also run the semantic checks (paradox, synchronicity, unnamed) over
    the same window. Pass one or more ``--check NAME`` to run only the
    named checks. Lint never resolves paradoxes, never auto-creates
    compiled pages, and never deletes orphan fragments — those are the
    load-bearing rules pinned by the hygiene suite.
    """
    from creek.lint import LintRunner, parse_since

    config = _load_config_for_vault(vault)
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


def _gdrive_check() -> None:
    """Run the read-only Drive doctor and print a secret-free report.

    Reports credentials/token presence, local token validity, optional-
    library availability, and a dry-run reachability listing. Downloads
    nothing and triggers no OAuth browser flow; when no locally-valid
    token is present the live probe is skipped so nothing egresses
    (issue #681). The report surfaces only paths and derived status —
    never a token, refresh token, or client secret.
    """
    from creek.ingest.gdrive import GoogleApiDriveClient, check_drive

    config = load_config()
    client = GoogleApiDriveClient(config.google_drive)
    report = check_drive(config.google_drive, client=client)
    for line in report.notes:
        console.print(line)
    if report.drive_reachable is None:
        console.print(
            "\n[dim]No network calls were made. Nothing was downloaded.[/dim]",
        )
    else:
        console.print(
            "\n[dim]Read-only listing only. Nothing was downloaded.[/dim]",
        )


@app.command()
def gdrive(
    download: bool = typer.Option(False, help="Download from Google Drive"),
    revoke: bool = typer.Option(
        False,
        "--revoke",
        help="Revoke the cached OAuth token and delete the local token file",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Read-only doctor: report auth/token/reachability, download nothing",
    ),
    staging: Path | None = typer.Option(None, help="Staging directory"),
) -> None:
    """Download from Google Drive, revoke the token, or run the doctor.

    ``--download`` mirrors files into a local staging directory
    (read-only; subsequent runs are incremental). ``--revoke`` runs the
    SEC-008 hygiene path: it best-effort calls Google's revocation
    endpoint, then erases the local token file with a zero-byte pass
    before unlinking. ``--check`` is a read-only doctor (issue #681): it
    reports credentials/token presence, local token validity, library
    availability, and a dry-run reachability listing — downloading
    nothing and triggering no OAuth flow. The three flags are mutually
    exclusive.

    First ``--download`` run opens a browser for OAuth authorisation;
    the refresh token is cached at ``GoogleDriveConfig.token_file``
    (mode ``0o600``) so subsequent runs are non-interactive.
    """
    if sum([download, revoke, check]) > 1:
        console.print(
            "[red]Specify exactly one of --download, --revoke, or --check.[/red]",
        )
        raise typer.Exit(code=2)
    if check:
        _gdrive_check()
        return
    if revoke:
        _gdrive_revoke()
        return
    if not download:
        console.print(
            "[yellow]Nothing to do. Pass --download, --revoke, or --check.[/yellow]",
        )
        return

    config = load_config()
    staging_dir = (
        staging if staging is not None else Path(config.google_drive.staging_dir)
    )

    from creek.ingest.gdrive import GoogleApiDriveClient, build_drive_connector

    client = GoogleApiDriveClient(config.google_drive)
    if not client.is_available():
        console.print(
            "[red]Google API client unavailable; cannot download from Drive. "
            "Install with `pip install google-api-python-client "
            "google-auth-oauthlib`.[/red]",
        )
        raise typer.Exit(code=1)

    # Route the download through the source-agnostic connector (#683); fetch_to
    # delegates to download_all, so observable behaviour is unchanged.
    connector = build_drive_connector(
        config.google_drive, client=client, staging=staging_dir
    )
    try:
        result = connector.fetch_to(staging_dir)
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
    signature_only: bool = typer.Option(
        False,
        "--signature-only",
        help=(
            "Emit the signature-only variant (.SIGNATURE.md): abstract "
            "voice patterns only, no quoted exemplar passages from "
            "user writing. Coexists with the legacy .SKILL.md tree "
            "(issue #353)."
        ),
    ),
) -> None:
    """Generate the Voice Skill Tree (Section 11.4).

    Writes a tree of skill files under *output* (default
    ``<vault>/creek-skills``) covering frequencies, phases, modes,
    registers, threads, eddies, and two meta skills. The default
    variant emits ``.SKILL.md`` files that include quoted exemplar
    passages harvested from the vault. With ``--signature-only``,
    ``.SIGNATURE.md`` files are emitted instead — same voice patterns,
    anti-patterns, and writing instructions but zero quoted user
    content. The two variants can coexist in the same output directory.
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
    written = SkillTreeGenerator(
        signature_only=signature_only,
    ).generate_all_skills(vault_path, output_dir)
    variant_label = "signature-only" if signature_only else "exemplar-bearing"
    console.print(
        f"[bold green]Voice Skill Tree generated ({len(written)} "
        f"{variant_label} files) at {output_dir}[/bold green]",
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
    print(
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
    lint`` to surface later.
    """
    from creek.generate.mining import IdeaMiner

    config = _load_config_for_vault(vault)
    # Re-resolve so future _resolve_vault hooks apply regardless of source.
    vault_path = _resolve_vault(vault if vault is not None else config.vault_path)
    current_phase = _parse_phase(phase)
    override = _parse_include_tier(include_tier)
    if bypass_compiled:
        _warn_bypass_compiled("mine")
    report = IdeaMiner(
        privacy_override=override,
        bypass_compiled=bypass_compiled,
        min_thread_fragments=config.mining.min_thread_fragments,
        min_chain_length=config.mining.min_chain_length,
        similarity_liminal=config.mining.similarity_liminal,
        similarity_resonance=config.mining.similarity_resonance,
    ).mine_all_with_report(
        vault_path,
        current_phase=current_phase,
    )
    seeds = list(report.seeds)
    fragment_ids = sorted({fid for seed in seeds for fid in seed.source_fragments})
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="mine",
        override=override,
        fragment_ids=fragment_ids,
    )
    if not seeds:
        _print_no_seeds_diagnostic(report, vault_path=vault_path)
        return

    display = seeds if limit <= 0 else seeds[:limit]
    table = Table(title=f"Idea seeds ({len(display)} of {len(seeds)})")
    table.add_column("Strategy")
    table.add_column("Title")
    table.add_column("Score", justify="right")
    for seed in display:
        table.add_row(seed.strategy.value, seed.title, f"{seed.score:.2f}")
    console.print(table)


def _print_no_seeds_diagnostic(
    report: MiningRunReport,
    *,
    vault_path: Path,
) -> None:
    """Print a per-strategy breakdown when no seeds surfaced.

    Each strategy reports its own score/threshold in its own units
    (fragment counts, component sizes, Jaccard similarity, binary
    gates) so the operator can read across them without conversion.
    Points at ``compile-gaps.jsonl`` for fallback breadcrumbs.
    """
    from creek.generate.mining import COMPILE_GAPS_LOG_RELPATH

    n_strategies = len(report.diagnostics)
    gaps_path = vault_path / COMPILE_GAPS_LOG_RELPATH
    lines = [
        f"[yellow]No idea seeds surfaced. Per-strategy breakdown "
        f"({n_strategies} strategies):[/yellow]",
    ]
    for diag in report.diagnostics:
        suffix = f" — {diag.fallback_reason}" if diag.fallback_reason else ""
        lines.append(
            f"  [yellow]{diag.strategy.value:<20}"
            f" {diag.candidates_considered} considered,"
            f" {diag.candidates_kept} kept"
            f" (top score {diag.top_score:.2f} /"
            f" threshold {diag.threshold:.2f}){suffix}[/yellow]",
        )
    lines.append(f"[yellow]See {gaps_path} for fallback breadcrumbs.[/yellow]")
    console.print("\n".join(lines))


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


@functools.cache
def _aptitude_frequency_labels() -> dict[str, str]:
    """Return ``{label: Frequency.value}`` derived from ``FREQUENCY_NAMES``.

    Each entry in :data:`creek.generate.indexes.FREQUENCY_NAMES` is a
    slash-separated pair (e.g. ``"Self-Love/Power"``); this helper
    splits, lowercases, and registers each half plus hyphen /
    underscore / no-separator variants so the CLI accepts every
    canonical UI label without a hand-maintained duplicate. Lazily
    computed and cached so the import graph stays shallow at CLI
    cold-start. The returned dict is module-private; callers should
    treat it as read-only.
    """
    from creek.generate.indexes import FREQUENCY_NAMES

    labels: dict[str, str] = {}
    for freq, name in FREQUENCY_NAMES.items():
        for part in name.split("/"):
            normalised = part.strip().lower()
            labels[normalised] = freq.value
            if "-" in normalised:
                labels[normalised.replace("-", "_")] = freq.value
                labels[normalised.replace("-", "")] = freq.value
    return labels


def _parse_seed_frequency(value: str | None) -> tuple[Frequency, ...]:
    """Parse ``--seed-frequency`` into a one-tuple of :class:`Frequency`.

    Returns an empty tuple when *value* is ``None``. Accepts both the
    ``F1``..``F10`` codes and the human-readable APTITUDE labels
    (``agency``, ``receptivity``, ``self-love``, …); unknown values
    exit with code 2 listing the valid options.
    """
    from creek.models import Frequency

    if value is None:
        return ()
    aliases = _aptitude_frequency_labels()
    normalised = value.strip().lower().replace(" ", "")
    canonical = aliases.get(normalised)
    raw = canonical if canonical is not None else value.strip().upper()
    try:
        return (Frequency(raw),)
    except ValueError as exc:
        codes = ", ".join(f.value for f in Frequency if f != Frequency.UNCLASSIFIED)
        labels = ", ".join(sorted(aliases))
        console.print(
            f"[red]Unknown --seed-frequency {value!r}. "
            f"Valid codes: {codes}. "
            f"Valid labels: {labels}.[/red]",
        )
        raise typer.Exit(code=2) from exc


def _parse_seed_phase(value: str | None) -> tuple[Phase, ...]:
    """Parse ``--seed-phase`` into a one-tuple of :class:`Phase`."""
    if value is None:
        return ()
    return (_parse_phase(value),)


def _parse_seed_mode(value: str | None) -> tuple[Mode, ...]:
    """Parse ``--seed-mode`` into a one-tuple of :class:`Mode`.

    Unknown modes exit with code 2 listing the valid stances.
    """
    from creek.models import Mode

    if value is None:
        return ()
    try:
        return (Mode(value.strip().lower()),)
    except ValueError as exc:
        valid = ", ".join(m.value for m in Mode if m != Mode.UNCLASSIFIED)
        console.print(
            f"[red]Unknown --seed-mode {value!r}. Valid stances: {valid}.[/red]",
        )
        raise typer.Exit(code=2) from exc


def _build_seed_spec(
    *,
    seed_fragment: str | None,
    seed_topic: str | None,
    seed_frequency: str | None,
    seed_phase: str | None,
    seed_mode: str | None,
) -> SeedSpec | None:
    """Assemble a :class:`SeedSpec` from raw CLI args, or ``None`` if unused.

    Enforces the FEAT-032 mutual-exclusion rule before constructing
    the spec so the error message names CLI flags rather than dataclass
    fields. Returns ``None`` when no seed flag was passed so the caller
    can fall back to the default mining flow.
    """
    from creek.generate.drafts import SeedSpec

    if seed_fragment is not None:
        conflicts = [
            name
            for name, value in (
                ("--seed-topic", seed_topic),
                ("--seed-frequency", seed_frequency),
                ("--seed-phase", seed_phase),
                ("--seed-mode", seed_mode),
            )
            if value is not None
        ]
        if conflicts:
            console.print(
                f"[red]--seed-fragment is mutually exclusive with "
                f"{', '.join(conflicts)}.[/red]",
            )
            raise typer.Exit(code=2)
        return SeedSpec(fragment_id=seed_fragment.strip())

    frequencies = _parse_seed_frequency(seed_frequency)
    phases = _parse_seed_phase(seed_phase)
    modes = _parse_seed_mode(seed_mode)
    topic = seed_topic.strip() if seed_topic else None

    if not (topic or frequencies or phases or modes):
        return None
    return SeedSpec(
        topic=topic,
        frequencies=frequencies,
        phases=phases,
        modes=modes,
    )


def _build_draft_llm(max_tokens: int | None = None) -> DraftLLM:
    """Construct a :data:`DraftLLM` callable from the configured LLM provider.

    Uses the Ollama/Anthropic adapter already wired up for classification.
    Reads the vault-resolved config via :func:`load_config` so the helper
    keeps a stable signature; the ``draft`` command pre-warms
    ``CREEK_CONFIG`` resolution by calling :func:`_load_config_for_vault`
    upstream.

    The returned callable yields a
    :class:`~creek.generate.drafts.DraftLLMResponse` so the draft
    generator can detect a ``max_tokens`` truncation via the provider's
    stop reason instead of saving a silently cut-off essay.

    Args:
        max_tokens: Explicit per-invocation token ceiling (the
            ``--max-tokens`` flag). ``None`` falls back to the loaded
            ``draft.max_tokens`` config, then to the provider's built-in
            default. The Ollama path ignores it.

    Returns:
        A callable ``(prompt) -> DraftLLMResponse`` ready to feed to
        :class:`DraftGenerator`.

    Raises:
        typer.Exit: If the configured LLM provider is not reachable.
    """
    from creek.classify.llm import LLMClassifier
    from creek.generate.drafts import DraftLLMResponse

    config = load_config()
    # The explicit flag wins; otherwise honour the config default. Both
    # the provider settings and this ceiling come from the same loaded
    # config so they cannot diverge.
    effective_max_tokens = (
        max_tokens if max_tokens is not None else config.draft.max_tokens
    )
    # Issue #1031: a real, unfixed tier leak — unlike the deliberate no-tier
    # sites in this file, this client is fed vault fragment *bodies*, and
    # `--include-tier intimate|all` lets intimate ones through untouched. Not a
    # considered decision; tracked separately because the fix shape differs
    # (the tier is per-fragment here, not per-call as in `creek compile` #962).
    classifier = LLMClassifier(config.model_router.resolve("classification"))
    if not classifier.available:
        console.print(
            "[red]LLM provider unavailable; cannot generate draft. "
            "Check Ollama or ANTHROPIC_API_KEY configuration.[/red]",
        )
        raise typer.Exit(code=1)

    def _invoke(prompt: str) -> DraftLLMResponse:
        completion = classifier.invoke_prompt_with_metadata(
            prompt,
            max_tokens=effective_max_tokens,
        )
        return DraftLLMResponse(
            text=completion.text,
            stop_reason=completion.stop_reason,
        )

    return _invoke


def _run_seeded_draft(
    generator: DraftGenerator,
    *,
    seed_spec: SeedSpec,
    vault_path: Path,
    current_phase: Phase,
    override: PrivacyTierOverride | None,
) -> None:
    """Run the seeded-draft pipeline and surface errors as typer exits.

    Extracted from :func:`draft` so the CLI command body stays under
    the cyclomatic-complexity floor. The branches it owns are the
    expected error surfaces (``SeedResolutionError``,
    ``PluralitySourceError``, and a ``RuntimeError`` from an empty LLM
    body) plus the happy-path save / audit.
    """
    from creek.generate.drafts import SeedResolutionError
    from creek.generate.twist import PluralitySourceError

    try:
        draft_obj = generator.generate_seeded_draft(
            seed_spec,
            vault_path=vault_path,
            current_phase=current_phase,
        )
    except (SeedResolutionError, PluralitySourceError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="draft",
        override=override,
        fragment_ids=list(draft_obj.source_fragments),
    )
    saved_path = generator.save_draft(draft_obj, vault_path)
    console.print(f"[bold green]Draft saved: {saved_path}[/bold green]")


def _read_outline_input(
    seed_outline: Path | None,
    seed_outline_text: str | None,
) -> str | None:
    """Resolve the outline input from the file/inline flags (issue #354).

    Returns ``None`` when neither flag is set so the caller falls back to
    the single-topic / mining paths. Exits 2 when both flags are set
    (mutually exclusive) or when the outline file cannot be read.
    """
    if seed_outline is not None and seed_outline_text is not None:
        console.print(
            "[red]--seed-outline and --seed-outline-text are mutually "
            "exclusive; pass only one.[/red]",
        )
        raise typer.Exit(code=2)
    if seed_outline_text is not None:
        return seed_outline_text
    if seed_outline is None:
        return None
    try:
        return seed_outline.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not read --seed-outline {seed_outline}: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _build_ontology_detector(vault: Path | None) -> OntologyDetector:
    """Return an :data:`OntologyDetector` bound to the vault's LLM config.

    The returned closure runs :func:`creek.classify.prompt.detect_ontology`
    against the resolved :class:`~creek.config.LLMConfig` so each outline
    section's ontology is detected with the same provider the rest of the
    pipeline uses.
    """
    from creek.classify.prompt import detect_ontology

    config = _load_config_for_vault(vault)

    def _detect(prompt: str) -> PromptOntology:
        # No tier argument, and none exists to pass: `prompt` is only ever an
        # `OutlineSection.seed_text`, i.e. operator-supplied `--seed-outline` /
        # `--seed-outline-text` copy. No fragment content reaches this provider,
        # so there is no classified tier for the router to enforce. Contrast
        # `_build_draft_llm` above, which does see fragment bodies (#1031).
        return detect_ontology(prompt, config.model_router.resolve("classification"))

    return _detect


def _run_outline_draft(
    generator: DraftGenerator,
    *,
    outline_text: str,
    vault: Path | None,
    vault_path: Path,
    current_phase: Phase,
    override: PrivacyTierOverride | None,
) -> None:
    """Run the multi-section outline pipeline and surface errors as exits.

    Extracted from :func:`draft` so the command body stays under the
    cyclomatic-complexity floor. Owns the outline-parse error surface
    (exit 2, a malformed-input error) and the empty-LLM-body
    ``RuntimeError`` surface (exit 1, a runtime failure), plus the
    happy-path save / audit.
    """
    from creek.generate.outline import OutlineParseError

    detector = _build_ontology_detector(vault)
    try:
        outline_draft = generator.generate_outline_draft(
            outline_text,
            vault_path=vault_path,
            detect_ontology=detector,
            current_phase=current_phase,
        )
    except OutlineParseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    fragment_ids = [
        fid for section in outline_draft.sections for fid in section.source_fragments
    ]
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="draft",
        override=override,
        fragment_ids=fragment_ids,
    )
    saved_path = generator.save_outline_draft(outline_draft, vault_path)
    console.print(f"[bold green]Draft saved: {saved_path}[/bold green]")


def _validate_draft_mode(
    *,
    outline_text: str | None,
    seed_spec: SeedSpec | None,
    ontology_twist: bool,
) -> None:
    """Validate the mutually-exclusive draft modes, exiting 2 on conflict.

    The outline path (issue #354) owns the whole composition, so it
    cannot be combined with the single-topic seed flags. ``--ontology-twist``
    still requires a single-topic seed because the outline path applies
    its twist composition per section automatically.
    """
    if outline_text is not None and seed_spec is not None:
        console.print(
            "[red]--seed-outline / --seed-outline-text cannot be combined "
            "with --seed-fragment / --seed-topic / --seed-frequency / "
            "--seed-phase / --seed-mode.[/red]",
        )
        raise typer.Exit(code=2)
    has_seed = outline_text is not None or seed_spec is not None
    if ontology_twist and not has_seed:
        console.print(
            "[red]--ontology-twist requires a seed (use --seed-topic, "
            "--seed-frequency, --seed-phase, or --seed-mode) or an outline "
            "(--seed-outline / --seed-outline-text).[/red]",
        )
        raise typer.Exit(code=2)


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
    seed_fragment: str | None = typer.Option(
        None,
        "--seed-fragment",
        help=(
            "Draft a follow-up to one specific fragment ID. "
            "Mutually exclusive with --seed-topic and the dimensional filters."
        ),
    ),
    seed_topic: str | None = typer.Option(
        None,
        "--seed-topic",
        help=(
            "Free-form topic phrase. Combinable with dimensional "
            "filters to narrow source material."
        ),
    ),
    seed_frequency: str | None = typer.Option(
        None,
        "--seed-frequency",
        help=(
            "Restrict source material to fragments with the named "
            "primary frequency. Accepts F1..F10 codes or APTITUDE labels "
            "(agency, receptivity, self-love, ...)."
        ),
    ),
    seed_phase: str | None = typer.Option(
        None,
        "--seed-phase",
        help=(
            "Restrict source material to the named wavelength phase "
            "(rising, peaking, withdrawal, diminishing, bottoming_out, restoration)."
        ),
    ),
    seed_mode: str | None = typer.Option(
        None,
        "--seed-mode",
        help=(
            "Restrict source material to the named stance "
            "(inhabit, express, collaborate, integrate, absorb)."
        ),
    ),
    ontology_twist: bool = typer.Option(
        False,
        "--ontology-twist",
        help=(
            "Enable ontology-twist composition. Computes the "
            "delta between the retrieved source profile and the operator's "
            "target profile, names divergent dimensions in the prompt, and "
            "forbids verbatim paraphrase. Requires >=2 source fragments."
        ),
    ),
    seed_outline: Path | None = typer.Option(
        None,
        "--seed-outline",
        help=(
            "Path to a markdown outline file. Each header-rooted "
            "section is composed independently with its own detected ontology, "
            "then stitched together with transition-only smoothing. Mutually "
            "exclusive with --seed-outline-text."
        ),
    ),
    seed_outline_text: str | None = typer.Option(
        None,
        "--seed-outline-text",
        help=(
            "Inline markdown outline (same per-section composition "
            "as --seed-outline). Mutually exclusive with --seed-outline."
        ),
    ),
    max_tokens: int | None = typer.Option(
        None,
        "--max-tokens",
        min=1,
        help=(
            "Maximum tokens the draft LLM may generate. Raise this for longer "
            "essays. Defaults to the vault's draft.max_tokens config (or the "
            "provider default when unset). A draft cut off at this ceiling is "
            "flagged truncated in frontmatter and warned about on stderr."
        ),
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help=(
            "Run the voice-fidelity guard in measure-only mode — sanitize and "
            "score the draft against your voice fingerprint, but skip the LLM "
            "rewrite pass (no extra network hop)."
        ),
    ),
    cohesion: bool = typer.Option(
        False,
        "--cohesion",
        help=(
            "Opt in to the no-fabrication cohesion pass: after "
            "composing a single-topic draft, run an LLM pass that smooths "
            "abrupt seams with transitions. A deterministic entity-preservation "
            "guard rejects any output that introduces a new proper noun, "
            "number, or ontology term, falling back to the original body. "
            "Default off; requires the LLM, so it is skipped under --no-llm."
        ),
    ),
) -> None:
    """Draft an essay from a mined idea with the activated skill stack.

    Mines ideas, presents the chosen idea as an invitation, assembles
    the frequency/phase/mode/register skill stack, gathers source
    material, asks the LLM to generate a draft, and saves it to
    ``07-Voice/Drafts/`` with full provenance.

    Manual seeding: pass any of ``--seed-fragment``,
    ``--seed-topic``, ``--seed-frequency``, ``--seed-phase``, or
    ``--seed-mode`` to direct composition. ``--seed-fragment`` is
    mutually exclusive with the others; topic and dimensional filters
    combine freely. When any seed flag is supplied the idea miner is
    bypassed and the draft is generated from the matching fragments,
    with the seed flags recorded in the draft's frontmatter for
    reproducibility.

    Pass ``--ontology-twist`` alongside a seed flag to enable
    ontology-twist composition — the draft's source / target ontology
    profiles are recorded in frontmatter, divergent dimensions are named
    in the prompt, and the LLM is instructed to recombine across the
    corpus rather than paraphrase any single source. Requires at least
    two source fragments.

    Pass ``--seed-outline <file>`` or ``--seed-outline-text "..."`` to
    compose a multi-section essay from a markdown outline. Each
    header-rooted section is detected, retrieved, and composed
    independently with its own ontology profile, then stitched together
    with a transition-only smoothing pass. Outline mode is mutually
    exclusive with the single-topic seed flags; a header-less outline is
    rejected with a clear error.

    Pass ``--max-tokens`` to raise the draft LLM's output ceiling for
    longer essays; a draft cut off at the ceiling is flagged
    ``truncated`` in frontmatter and warned about on stderr.
    """
    from creek.generate.ai_style.fingerprint import load_fingerprint
    from creek.generate.ai_style.preamble import build_style_preamble
    from creek.generate.drafts import DraftGenerator
    from creek.generate.mining import IdeaMiner

    vault_path = _resolve_vault(vault)
    skills_dir = skills_root if skills_root is not None else vault_path / "creek-skills"
    current_phase = _parse_phase(phase)
    voice_text = _read_voice_core(voice_core)
    vault_config = _load_config_for_vault(vault)
    ai_style = vault_config.ai_style
    cohesion_enabled = cohesion or vault_config.draft.cohesion
    fingerprint = load_fingerprint(vault_path, ai_style)
    style_preamble = build_style_preamble(fingerprint, ai_style)
    override = _parse_include_tier(include_tier)
    seed_spec = _build_seed_spec(
        seed_fragment=seed_fragment,
        seed_topic=seed_topic,
        seed_frequency=seed_frequency,
        seed_phase=seed_phase,
        seed_mode=seed_mode,
    )
    outline_text = _read_outline_input(seed_outline, seed_outline_text)
    _validate_draft_mode(
        outline_text=outline_text,
        seed_spec=seed_spec,
        ontology_twist=ontology_twist,
    )
    llm = _build_draft_llm(max_tokens)
    if bypass_compiled:
        _warn_bypass_compiled("draft")

    generator = DraftGenerator(
        llm=llm,
        skills_root=skills_dir,
        voice_core=voice_text,
        style_preamble=style_preamble,
        privacy_override=override,
        bypass_compiled=bypass_compiled,
        ontology_twist=ontology_twist,
        fingerprint=fingerprint,
        ai_style_config=ai_style,
        voice_guard_no_llm=no_llm,
        cohesion=cohesion_enabled,
    )

    if outline_text is not None:
        _run_outline_draft(
            generator,
            outline_text=outline_text,
            vault=vault,
            vault_path=vault_path,
            current_phase=current_phase,
            override=override,
        )
        return

    if seed_spec is not None:
        _run_seeded_draft(
            generator,
            seed_spec=seed_spec,
            vault_path=vault_path,
            current_phase=current_phase,
            override=override,
        )
        return

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
    "Destination type: thread, eddy, praxis, paradox, unnamed, draft, "
    "ai-as-user, or observation. Paradox always routes to 10-Liminal/Paradoxes/ "
    "regardless of other inputs; ai-as-user files kept AI output to "
    "11-Other-Authors/ai-as-user/; observation files raw wavelength reflections "
    "to 05-Wavelength/Observations/."
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


def _validate_author_inputs(
    medium: str, query: str | None, work: Path | None, supported: frozenset[str]
) -> None:
    """Validate the ``author`` command's medium/query/work combination.

    Enforces the per-medium input contract: a known medium, ``--work`` required
    for ``book-report``, and ``--query`` required for every other medium.

    Args:
        medium: The requested output medium.
        query: The optional ``--query`` value.
        work: The optional ``--work`` path (book-report only).
        supported: The set of wired mediums.

    Raises:
        typer.Exit: With code 2 when the combination is invalid.
    """
    if medium not in supported:
        wired = ", ".join(repr(m) for m in sorted(supported))
        console.print(
            f"[red]Unsupported --medium {medium!r}; wired mediums are {wired} "
            "(FEAT-041).[/red]",
        )
        raise typer.Exit(code=2)
    if medium == "book-report" and work is None:
        console.print(
            "[red]--work is required for --medium book-report: pass the "
            "11-Other-Authors/<author>/<work> path to report on.[/red]",
        )
        raise typer.Exit(code=2)
    if medium != "book-report" and query is None:
        console.print(
            f"[red]--query is required for --medium {medium}.[/red]",
        )
        raise typer.Exit(code=2)


def _compose_author_query(query: str | None, work: Path | None) -> str:
    """Compose the effective query string the conductor receives.

    For ``book-report`` the query is derived from the ``--work`` path (so
    ``--work`` is a CLI-layer convenience and the conductor signature is
    unchanged), with any explicit ``--query`` appended. Otherwise the validated
    ``--query`` is used verbatim.

    Args:
        query: The optional ``--query`` value.
        work: The optional ``--work`` path (book-report only).

    Returns:
        The effective query string to author from.

    Raises:
        ValueError: If both ``query`` and ``work`` are ``None`` — the
            upstream-validated invariant has been violated.
    """
    if work is not None:
        derived = f"Report on the work at {work}, relating it to my threads and eddies."
        return f"{derived} {query}" if query else derived
    if query is None:
        # Unreachable via the CLI: ``_validate_author_inputs`` requires a query
        # for every non-book-report medium, and book-report always supplies
        # ``--work`` (handled above). Make that contract explicit instead of
        # silently authoring from an empty query.
        msg = "--query or --work must be provided to compose an author query."
        raise ValueError(msg)
    return query


@app.command()
def author(
    query: str | None = typer.Option(None, "--query", help="What to author about."),
    medium: str = typer.Option(
        "research",
        "--medium",
        help=(
            "Output medium: research, chat, essay, research-piece, "
            "book-report, or how-to."
        ),
    ),
    work: Path | None = typer.Option(
        None,
        "--work",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="For book-report: the 11-Other-Authors/<author>/<work> path to report on.",
    ),
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the pipeline plan and stub evidence summary without authoring.",
    ),
    max_rounds: int | None = typer.Option(
        None,
        "--max-rounds",
        min=1,
        max=10,
        help=(
            "Override the Conductor's voice/reflect round bound. Defaults to "
            "the vault's author.max_author_rounds config (3)."
        ),
    ),
    include_tier: str | None = typer.Option(
        None,
        "--include-tier",
        help=(
            "Privacy ceiling for retrieved evidence: open (default) / personal "
            "/ intimate / all. Fragments above the ceiling are excluded from "
            "the desk's evidence (#660)."
        ),
    ),
) -> None:
    """Author a piece with the Creek Writing Desk.

    Drives an end-to-end author desk. The Graph, Retrieval, and Ontology
    specialists and the Reflection node do **real, deterministic** work — a
    link-graph walk, embedding-based retrieval, rule-based ontology grounding,
    and quality-gate judging. The Voice node renders **live via the configured
    ``generation`` provider** when one is available (routed by content tier so
    Intimate stays local, #661), and falls back to a deterministic rendering when
    no provider is configured/available. Returns a shaped
    :class:`~creek.author.models.AuthoredDraft`. ``--dry-run`` plans and gathers
    real evidence only (no voicing or reflection). ``book-report`` takes
    ``--work`` (the 11-Other-Authors path) instead of ``--query``; every other
    medium requires ``--query``.
    """
    from creek.author import SUPPORTED_MEDIUMS, build_default_conductor
    from creek.author.client import AuthorLLMClient

    _validate_author_inputs(medium, query, work, SUPPORTED_MEDIUMS)
    effective_query = _compose_author_query(query, work)

    config = _load_config_for_vault(vault)
    vault_path = _resolve_vault(vault if vault is not None else config.vault_path)
    rounds = max_rounds if max_rounds is not None else config.author.max_author_rounds
    # #660: the privacy ceiling for retrieved evidence (default OPEN).
    override = _parse_include_tier(include_tier)

    if dry_run:
        # The dry run only plans + gathers evidence; no voicing, so no client.
        conductor = build_default_conductor(max_rounds=rounds)
        evidence = conductor.gather_evidence(
            effective_query,
            vault_path,
            override=override,
        )
        # #660: an elevated --include-tier admits above-OPEN fragments into the
        # evidence; record that operator decision in the privacy audit (no-op
        # for None/OPEN). Mirrors the other --include-tier callers.
        _audit_privacy_override_if_needed(
            vault_path=vault_path,
            command="author",
            override=override,
            fragment_ids=evidence.all_source_fragments(),
        )
        console.print(f"PLAN: {' → '.join(conductor.plan())}")
        console.print(
            f"EVIDENCE: {len(evidence.claims)} claims, "
            f"{len(evidence.all_source_fragments())} source_fragments",
        )
        return

    # #658/#661: build the router-resolved voice client *per the run's content
    # tier* so live voicing of Intimate content is redirected to a local
    # provider by the chokepoint. The factory falls back to ``None``
    # (deterministic stub) when the provider is unavailable.
    def _voice_client(tier: PrivacyTier | None) -> AuthorLLMClient | None:
        return AuthorLLMClient.for_voice_or_none(
            config.model_router,
            author=config.author,
            tier=tier,
        )

    if _voice_client(None) is None:
        console.print(
            "[dim]No LLM provider available — rendering the deterministic "
            "stub. Configure llm.generation (and consent for a cloud provider) "
            "for live voicing.[/dim]",
        )
    draft_result = build_default_conductor(
        max_rounds=rounds,
        voice_client_factory=_voice_client,
    ).run(
        medium=medium,
        query=effective_query,
        vault=vault_path,
        override=override,
    )
    # #660: audit the elevated access using the fragments actually cited in the
    # draft's provenance (no-op for None/OPEN).
    _audit_privacy_override_if_needed(
        vault_path=vault_path,
        command="author",
        override=override,
        fragment_ids=[
            fid for entry in draft_result.provenance for fid in entry.fragment_ids
        ],
    )
    console.print(
        f"medium={draft_result.medium} verdict={draft_result.verdict} "
        f"rounds={draft_result.rounds} provenance={len(draft_result.provenance)}",
    )
    console.print(draft_result.body)


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
    """File an answer back into the vault.

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


def _load_config_for_vault(vault: Path | None) -> CreekConfig:
    """Load :class:`CreekConfig`, auto-discovering ``--vault``'s config (issue #322).

    Wraps :func:`creek.config.resolve_config_path` + :func:`load_config`
    so every CLI command that accepts ``--vault <path>`` automatically
    picks up ``<path>/00-Creek-Meta/creek_config.yaml`` when neither
    ``--config`` nor ``CREEK_CONFIG`` is set. Explicit overrides still
    take precedence; the helper is otherwise transparent to existing
    callers.

    Args:
        vault: Vault root from the command's ``--vault`` flag, or
            ``None`` when the command was invoked without one.

    Returns:
        A fully-validated :class:`CreekConfig`.
    """
    config_path = resolve_config_path(vault, None)
    return load_config(config_path)


def _resolve_vault(vault: Path | None) -> Path:
    """Resolve vault path from argument or config.

    Args:
        vault: Explicit vault path, or None to use config default.

    Returns:
        Resolved vault path.
    """
    if vault is not None:
        return vault
    return _load_config_for_vault(vault).vault_path


@clean_app.command(name="orphans")
def clean_orphans(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    age_days: int = typer.Option(30, help="Minimum age in days for orphan detection"),
) -> None:
    """Identify fragments with zero incoming/outgoing links after N days."""
    from creek.clean.hygiene import OrphanScanner

    vault_path = _resolve_vault(vault)
    result = OrphanScanner(age_days=age_days).scan(vault_path)

    console.print("\n[bold]Orphan Scan[/bold]")
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
) -> None:
    """Find review queue items older than N days."""
    from creek.clean.hygiene import StaleReviewScanner

    vault_path = _resolve_vault(vault)
    result = StaleReviewScanner(age_days=age_days).scan(vault_path)

    console.print("\n[bold]Stale Review Scan[/bold]")
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
) -> None:
    """Scan fragments for wiki-links pointing to nonexistent files."""
    from creek.clean.hygiene import BrokenLinkScanner

    vault_path = _resolve_vault(vault)
    result = BrokenLinkScanner().scan(vault_path)

    console.print("\n[bold]Broken Link Scan[/bold]")
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
) -> None:
    """Execute normalized dedup sweep and output review report."""
    from creek.clean.hygiene import DuplicateScanner

    vault_path = _resolve_vault(vault)
    result = DuplicateScanner().scan(vault_path)

    console.print("\n[bold]Duplicate Scan[/bold]")
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


def _render_voice_purge_notes(result: PurgeResult) -> None:
    """Render the ``07-Voice`` sweep's count, follow-up, and any shortfall.

    Two things an operator cannot infer from the count alone. First, the
    swept notes are *shared* derived artifacts: deleting the profile or
    glossary that quoted the purged fragment also drops every other
    fragment's legitimately-retained content from it until the report is
    regenerated, so the follow-up command is named here rather than left
    to be discovered. Second, a fragment whose body is not valid UTF-8
    cannot be matched against a profile at all (#1211), and an erasure
    that fell short must say so where the operator is already looking —
    the audit ``outcome`` line records the same shortfall as
    ``status="partial"``.

    Args:
        result: The completed purge result.
    """
    if result.voice_artifacts_removed:
        console.print(
            f"Voice artifacts removed: {result.voice_artifacts_removed}",
        )
        console.print(
            "[dim]Those derived notes are shared: re-run "
            "`creek report --type voice` and `creek report --type lexicon` "
            "to regenerate them for the fragments that remain.[/dim]",
        )
    if result.voice_body_undecodable:
        named = ", ".join(result.voice_body_undecodable)
        console.print(
            f"[red]Voice sweep INCOMPLETE for: {named}[/red] — "
            "the body is not valid UTF-8, so a "
            "07-Voice/<register>-profile.md may still quote it. "
            "Re-run `creek report --type voice` to regenerate 07-Voice.",
        )


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
    if result.intimate_stubs_removed:
        console.print(
            f"Intimate stubs removed: {result.intimate_stubs_removed}",
        )
    _render_voice_purge_notes(result)

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
    """Delete a fragment and scrub every reference to it.

    Scrubbing covers (GAP-004): wiki-links by title removed from every
    `.md` file in the vault, plus YAML provenance lists and bare body
    mentions of the fragment ID replaced with ``[purged]`` across
    01-Fragments through 10-Liminal and the deployed Skills tree.
    Embedding-cache rows for the fragment are also dropped (GAP-001).
    If the note carries a ``saved_from.intimate_body_pointer``, the
    pointed-to intimate-body stub under
    ``10-Liminal/Compost/intimate-stubs/`` is deleted too (GAP-012).
    """
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

    The two are mutually exclusive; pass exactly one. Reference
    scrubbing (GAP-004) runs for every matched fragment: wiki-links
    by title plus word-boundary ID mentions replaced with
    ``[purged]`` across the whole vault. Any matched note carrying a
    ``saved_from.intimate_body_pointer`` also has its intimate-body
    stub under ``10-Liminal/Compost/intimate-stubs/`` deleted (GAP-012).
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
    """Delete fragments created within a date range.

    Any deleted note carrying a ``saved_from.intimate_body_pointer``
    also has its intimate-body stub under
    ``10-Liminal/Compost/intimate-stubs/`` deleted (GAP-012).
    """
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


@compost_app.command("calibrate")
def compost_calibrate(
    fixture: Path | None = typer.Option(
        None,
        "--fixture",
        help=(
            "Path to a YAML calibration set of `{id, title, body, expected: "
            "bool}` entries. Defaults to "
            "`tests/fixtures/compost-calibration.yaml`. When omitted *and* "
            "the default fixture is missing, the command falls back to "
            "exemplar-coverage reporting only."
        ),
    ),
    exemplars: Path | None = typer.Option(
        None,
        "--exemplars",
        help=(
            "Path to a custom exemplars YAML. Defaults to the packaged "
            "`creek/generate/exemplars/compost.yaml`."
        ),
    ),
    json_path: Path | None = typer.Option(
        None,
        "--json",
        help=(
            "Write a machine-readable JSON sidecar with the confusion "
            "matrix, metrics, per-stage hit rates, and per-entry breakdown."
        ),
    ),
    embedding_threshold: float = typer.Option(
        0.6,
        "--embedding-threshold",
        help=(
            "Override the embedding-gate cosine floor (default 0.6 — matches "
            "CompostConfig.embedding_threshold). Tighten when scoring shows "
            "the default under-precises against your corpus."
        ),
    ),
    no_verifier: bool = typer.Option(
        False,
        "--no-verifier",
        help=(
            "Skip the LLM verifier stage — score with the embedding gate "
            "only. Useful for offline calibration runs and CI suites with "
            "no API key."
        ),
    ),
    floor_recall: float | None = typer.Option(
        None,
        "--floor-recall",
        help=("CI gate: exit non-zero when recall falls below this value (0.0-1.0)."),
    ),
    floor_precision: float | None = typer.Option(
        None,
        "--floor-precision",
        help=(
            "CI gate: exit non-zero when precision falls below this value (0.0-1.0)."
        ),
    ),
) -> None:
    """Score the compost detector against a labelled fixture.

    Runs every fixture entry through the embedding gate + (optional) LLM
    verifier, then reports the confusion matrix, recall, precision, F1,
    false-positive rate, and per-stage hit counts. Pair with
    ``--floor-recall`` and ``--floor-precision`` in CI to fail the build
    when the pipeline regresses against the labelled set.

    With no fixture available, falls back to the exemplar-coverage
    summary so the command still surfaces useful state on first run.
    """
    from creek.generate.compost_embedding import load_exemplars

    try:
        loaded = load_exemplars(exemplars)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Failed to load exemplars: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    by_texture: dict[str, int] = {}
    for exemplar in loaded:
        by_texture[exemplar.texture] = by_texture.get(exemplar.texture, 0) + 1
    console.print(f"[bold green]Loaded {len(loaded)} compost exemplars.[/bold green]")
    for texture, count in sorted(by_texture.items()):
        console.print(f"  {texture:<20} {count}")

    _run_compost_calibration(
        fixture=fixture,
        exemplars_loaded=loaded,
        json_path=json_path,
        embedding_threshold=embedding_threshold,
        no_verifier=no_verifier,
        floor_recall=floor_recall,
        floor_precision=floor_precision,
    )


def _run_compost_calibration(
    *,
    fixture: Path | None,
    exemplars_loaded: tuple[CompostExemplar, ...],
    json_path: Path | None,
    embedding_threshold: float,
    no_verifier: bool,
    floor_recall: float | None,
    floor_precision: float | None,
) -> None:
    """Drive ``creek compost calibrate --fixture`` end-to-end.

    Resolves the fixture path (falling back to the packaged default),
    wires the embedding similarity closure + LLM verifier (or skips the
    verifier under ``--no-verifier``), scores the fixture, and prints
    the report. Optionally writes a JSON sidecar and enforces
    recall/precision floors for CI gating.
    """
    from creek.generate.compost_calibration import (
        DEFAULT_FIXTURE_PATH,
        CompostFloorBreachError,
        assert_floors,
        load_fixture,
        score_compost,
        write_json_sidecar,
    )
    from creek.generate.compost_embedding import make_similarity_fn

    resolved = fixture or DEFAULT_FIXTURE_PATH
    if not resolved.exists():
        if fixture is None:
            console.print(
                "\nNo --fixture supplied and the packaged default at "
                f"{DEFAULT_FIXTURE_PATH} was not found (this happens under a "
                "wheel install). Pass --fixture path/to/fixture.yaml to score "
                "against your own labelled set.",
            )
            return
        console.print(f"[red]Compost fixture not found: {resolved}[/red]")
        raise typer.Exit(code=2)

    try:
        entries = load_fixture(resolved)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Failed to load compost fixture: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    config = load_config()
    from creek.link.embeddings import EmbeddingLinker

    linker = EmbeddingLinker(config=config.embeddings)
    similarity_fn = make_similarity_fn(exemplars_loaded, linker)

    verifier = None if no_verifier else _build_compost_verifier(config)

    report = score_compost(
        entries,
        similarity_fn=similarity_fn,
        verifier=verifier,
        embedding_threshold=embedding_threshold,
    )
    console.print("")
    console.print(report.render())

    if json_path is not None:
        write_json_sidecar(report, json_path)
        console.print(f"\n[green]JSON sidecar written: {json_path}[/green]")

    if floor_recall is not None or floor_precision is not None:
        try:
            assert_floors(
                report,
                floor_recall=floor_recall,
                floor_precision=floor_precision,
            )
        except CompostFloorBreachError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None


def _build_compost_verifier(config: CreekConfig) -> SupportsVerifyCompost | None:
    """Construct the production LLM verifier, or ``None`` if it can't be built.

    ``creek compost calibrate`` runs in environments where the configured
    ``generation`` provider's credentials (or local host) may or may not be
    present; rather than fail loud, fall back to embedding-only scoring with a
    console note so the operator can re-run with the verifier once the provider
    is ready.
    """
    from creek.classify.llm import build_provider
    from creek.generate.compost_verifier import LLMCompostVerifier

    # Provider-neutral (#667): build the configured ``generation`` provider
    # (#646) and gate on availability so any backend (Ollama / OpenAI / Gemini /
    # Anthropic) degrades the same way. A cloud provider validates its env in the
    # constructor and may raise (build_provider contract); a local provider
    # constructs but reports ``available is False`` when its host is down — both
    # fall back to embedding-only scoring.
    #
    # No tier argument, and this is *not* the same class of site as the
    # `creek compile` bug (#962). The only call site is
    # `_run_compost_calibration` above, reached only from `creek compost
    # calibrate`, and the verifier this builds only ever sees
    # `CompostCalibrationEntry` fields loaded from a YAML fixture — it never
    # reads the vault, so no fragment tier exists to thread.
    #
    # Durable caveat: `creek.generate.compost.CompostDetector` accepts a
    # `verifier`, so if a vault-scanning compost path ever wires this builder
    # in, its input becomes fragment content and this call MUST resolve with a
    # tier. Re-derive it there rather than assuming this comment still holds.
    try:
        provider = build_provider(config.model_router.resolve("generation"))
    except RuntimeError as exc:
        console.print(
            f"[yellow]Skipping LLM verifier — provider unavailable: {exc}[/yellow]",
        )
        return None
    if not provider.available:
        console.print(
            "[yellow]Skipping LLM verifier — provider prerequisites unmet "
            "(missing key/consent, or local host unreachable).[/yellow]",
        )
        return None
    return LLMCompostVerifier(provider=provider)


def _build_compost_similarity_fn(
    config: CreekConfig,
    vault_path: Path,
) -> Callable[[str], float]:
    """Build the embedding-gate closure ``creek compost scan`` scores fragments with.

    Split out of the command body so tests can substitute a deterministic
    stub — the real closure loads a sentence-transformers model, which is far
    too heavy for CLI wiring tests.

    ``compost.exemplars_relpath`` is resolved against *vault_path*, matching
    the field's own "vault-relative" contract and the handling its sibling
    ``compost.review_queue_relpath`` already gets. Resolving it against the
    process CWD instead would make the command depend on the operator's shell
    location: a ``FileNotFoundError`` when run from elsewhere, or — worse,
    silently — whatever same-named file happened to sit in that directory.

    Args:
        config: The loaded Creek config; supplies the embedding settings and,
            when set, ``compost.exemplars_relpath``.
        vault_path: Vault root that ``exemplars_relpath`` hangs off.

    Returns:
        A closure mapping fragment text to its maximum cosine similarity
        against the compost exemplar set.
    """
    from creek.generate.compost_embedding import load_exemplars, make_similarity_fn
    from creek.link.embeddings import EmbeddingLinker

    relpath = config.compost.exemplars_relpath
    exemplars_path = vault_path / relpath if relpath else None
    try:
        exemplars = load_exemplars(exemplars_path)
    except (FileNotFoundError, ValueError) as exc:
        # Same failure mode, same message as `compost_calibrate` — a config
        # typo should read as a config typo, not as a Python traceback.
        console.print(f"[red]Failed to load exemplars: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    return make_similarity_fn(exemplars, EmbeddingLinker(config=config.embeddings))


def _refuse_scan_without_verifier(detail: str) -> NoReturn:
    """Abort ``creek compost scan`` when the LLM verifier cannot be built.

    ``creek compost calibrate`` degrades to embedding-only scoring in this
    situation, which is harmless because it only prints a number. ``scan``
    *writes to the vault*, so the same fallback would file unverified
    embedding hits as canonical compost — asserting a finding the pipeline
    never actually made. Refusing keeps the vault honest and leaves the
    operator a deliberate opt-in.

    Args:
        detail: Why the verifier is unavailable, shown to the operator.

    Raises:
        typer.Exit: Always, with code 2 (matching the command's other
            configuration failures).
    """
    console.print(f"[red]Cannot verify compost candidates: {detail}[/red]")
    console.print(
        "Set the provider credentials (API key + CREEK_CLOUD_CONSENT=1 for a "
        "cloud provider, or start the local host), or re-run with "
        "[bold]--no-llm[/bold] to file unverified candidates to the review "
        "queue instead.",
    )
    raise typer.Exit(code=2)


def _build_scan_verifier(config: CreekConfig) -> SupportsVerifyCompost:
    """Construct the vault-scanning compost verifier, or refuse to scan.

    This is the tier-aware builder the durable caveat on
    :func:`_build_compost_verifier` asks for. That function feeds the verifier
    YAML-fixture text and so needs no tier; **this** one feeds it real fragment
    bodies, so the routing call must state a tier ceiling rather than pass
    ``None``.

    The ceiling is ``Personal``, and it is a derived fact rather than a guess:
    :func:`creek.generate.compost_scan.run_compost_scan` forces
    ``skip_intimate=True`` — not read from config, so no config edit or flag can
    turn it off — which means an ``Intimate`` fragment is dropped before the
    embedding gate, let alone the verifier. ``Personal`` is therefore the most
    sensitive tier that can physically reach this provider. Should that forced
    flag ever become configurable, this resolve call must be re-derived
    per-fragment instead of hoisted here.

    Unlike :func:`_build_compost_verifier`, an unavailable provider is fatal —
    see :func:`_refuse_scan_without_verifier`.

    Args:
        config: The loaded Creek config, supplying the model router.

    Returns:
        A ready :class:`~creek.generate.compost_verifier.LLMCompostVerifier`.

    Raises:
        typer.Exit: When the provider cannot be built or reports its
            prerequisites unmet.
    """
    from creek.classify.llm import build_provider
    from creek.generate.compost_verifier import LLMCompostVerifier

    llm_config = config.model_router.resolve("generation", PrivacyTier.PERSONAL)
    try:
        provider = build_provider(llm_config)
    except RuntimeError as exc:
        _refuse_scan_without_verifier(str(exc))
    if not provider.available:
        _refuse_scan_without_verifier(
            "provider prerequisites unmet (missing API key, missing cloud "
            "consent, or local host unreachable)",
        )
    return LLMCompostVerifier(provider=provider)


def _render_compost_scan(result: CompostScanResult, *, dry_run: bool) -> None:
    """Print the pre-flight estimate and, for a real run, what was written."""
    plan = result.plan
    console.print(
        f"[bold]Candidates:[/bold] {plan.fragment_candidates} fragment, "
        f"{plan.thread_candidates} thread, {plan.project_candidates} project "
        f"({result.skipped_existing} already composted, skipped)",
    )
    console.print(f"[bold]LLM calls this run:[/bold] {plan.llm_calls}")
    if dry_run:
        console.print("[dim]Dry run — nothing written.[/dim]")
        return
    console.print(
        f"[green]Composted:[/green] {len(result.composted)} note(s) → "
        "10-Liminal/Compost/",
    )
    console.print(
        f"[yellow]Queued for review:[/yellow] {len(result.review_queued)} note(s)",
    )


@compost_app.command("scan")
def compost_scan(
    vault: Path | None = typer.Option(None, help="Obsidian vault path"),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help=(
            "Run the embedding gate only — no fragment content leaves the "
            "device. Candidates are filed to the review queue unverified "
            "rather than as canonical compost."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the candidate counts and the LLM-call estimate, then stop "
            "without verifying or writing anything."
        ),
    ),
    embedding_threshold: float | None = typer.Option(
        None,
        "--embedding-threshold",
        help=(
            "Override the embedding-gate cosine floor for this run (default: "
            "`compost.embedding_threshold`, 0.6). Raise it after "
            "`creek compost calibrate` shows the default over-recalls."
        ),
    ),
) -> None:
    """Scan the vault for composted ideas and write `10-Liminal/Compost/` notes.

    The production counterpart to `creek compost calibrate`: where calibrate
    scores the FEAT-018 detector against a labelled fixture, this points it at
    a real vault. Dormant threads, silent tagged projects, and fragments whose
    text reads as abandonment become compost notes preserving what each idea
    was, why it faded, and what energy remains.

    Fragments run the two-stage pipeline — embedding gate, then LLM verifier.
    A `yes` verdict files canonically; `ambiguous` goes to the review queue
    for you to triage; `no` is dropped. `--no-llm` runs the gate alone and
    files every hit to the review queue, because an embedding match on its own
    is a suspicion rather than a finding.

    Intimate-tier fragments are excluded before the gate and never reach a
    provider. Re-running is idempotent: sources already carrying a compost
    note are skipped and reported, so a second scan spends no LLM calls on
    them.

    Setting `compost.llm_verification: false` in `creek_config.yaml` has the
    same effect as passing `--no-llm` on every run.
    """
    from creek.generate.compost_scan import run_compost_scan

    config = _load_config_for_vault(vault)
    vault_path = _resolve_vault(vault)

    compost_config = config.compost
    if embedding_threshold is not None:
        compost_config = compost_config.model_copy(
            update={"embedding_threshold": embedding_threshold},
        )

    # `--no-llm` is the per-run form of `compost.llm_verification: false`;
    # either one declares an offline pass, so honour whichever is set rather
    # than building (and then refusing on) a provider the operator never
    # asked for.
    verify_with_llm = not no_llm and compost_config.llm_verification

    # A dry run never calls the verifier, so it must not require one to exist.
    # Demanding credentials to print a cost estimate would defeat the estimate:
    # not-yet-configured is exactly when an operator wants to see the number.
    # `will_verify` keeps the quoted count honest despite the skipped build.
    verifier = _build_scan_verifier(config) if verify_with_llm and not dry_run else None
    similarity_fn = _build_compost_similarity_fn(config, vault_path)

    result = run_compost_scan(
        vault_path,
        similarity_fn=similarity_fn,
        config=compost_config,
        verifier=verifier,
        dry_run=dry_run,
        will_verify=verify_with_llm,
    )
    _render_compost_scan(result, dry_run=dry_run)
