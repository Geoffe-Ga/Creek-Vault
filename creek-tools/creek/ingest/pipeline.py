"""Shared, UI-agnostic ingest pipeline: ledger-backed idempotent writes (#754).

This is the single source of truth for turning an ingestor's parsed output into
vault fragments *idempotently* — the ledger + ``write_fragment_idempotent`` +
tomb machinery that makes re-ingesting a source a no-op and an *edited* mutable
unit an update-in-place (preserving the fragment id and its classifications)
rather than an orphaned duplicate.

It was lifted out of ``creek.cli`` so both the ``creek ingest`` CLI and the
``creek.journal`` MCP tool call the *same* code (the CLI keeps a thin wrapper for
its typer.Exit / summary-print behavior). Nothing here imports typer, rich, or
``creek_mcp`` — it raises plain exceptions and returns a structured result, so
any surface can drive it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.ingest.base import assemble_ingested_fragment

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from creek.ingest.base import Ingestor, ParsedFragment
    from creek.ingest.ledger import LedgerRecord, SourceLedger
    from creek.models import Fragment
    from creek.vault.writer import VaultWriter


@dataclass(frozen=True)
class IngestRunResult:
    """Structured outcome of :func:`run_ingest` (UI-agnostic).

    Attributes:
        written: Units written or updated (created + updated + unchanged).
        errors: Human-readable ``[<source_type>] …`` error strings.
        discovered: How many inputs the ingestor's ``discover()`` found.
        created / updated / unchanged: Per-action tallies of the idempotent write.
        tombed: Ledgered units soft-tombed because a full-source pass no longer
            saw them.
        skipped: Units skipped by incremental/``since`` filtering.
    """

    written: int
    errors: list[str]
    discovered: int
    created: int
    updated: int
    unchanged: int
    tombed: int
    skipped: int


def derive_source_key(source_path: str, vault_path: Path) -> str:
    """Return a stable vault-relative ``source_key`` for a source file (#672).

    Prefers the path relative to the vault root (the stable identity a
    re-ingested edit is matched on); falls back to the bare filename when the
    source lives outside the vault.
    """
    from pathlib import Path as _Path

    candidate = _Path(source_path)
    try:
        return candidate.resolve().relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        return candidate.name


def ledger_for_source(source_type: str, vault_path: Path) -> SourceLedger | None:
    """Load the source ledger for mutable sources, else ``None`` (#672).

    Only the markdown (journal) source is ledger-wired; append-only event
    sources keep their content-hashed ids untouched.
    """
    if source_type != "markdown":
        return None
    from creek.ingest.ledger import SourceLedger

    return SourceLedger.load(vault_path, source=source_type)


def attach_origin_key(
    ledger: SourceLedger | None,
    parsed: ParsedFragment,
    fragment: Fragment,
    vault_path: Path,
) -> None:
    """Stamp the fragment's ``source.origin_key`` before it is written (#672)."""
    if ledger is None:
        return
    fragment.source.origin_key = derive_source_key(parsed.source_path, vault_path)


def record_in_ledger(
    ledger: SourceLedger | None,
    parsed: ParsedFragment,
    fragment: Fragment,
) -> None:
    """Record the written fragment in the source ledger (#672)."""
    if ledger is None or fragment.source.origin_key is None:
        return
    ledger.record(
        fragment.source.origin_key,
        fragment.id,
        ledger.content_hash(parsed.content),
    )


def ledger_record(
    ledger: SourceLedger | None,
    fragment: Fragment,
) -> LedgerRecord | None:
    """Return the prior ledger record for this fragment's source unit (#673)."""
    if ledger is None:
        return None
    origin_key = fragment.source.origin_key
    if origin_key is None:
        return None
    return ledger.get(origin_key)


def restore_tombed(
    writer: VaultWriter,
    fragment: Fragment,
    record: LedgerRecord,
    body: str,
    new_hash: str,
    reclassify_threshold: float,
) -> Path | None:
    """Un-tomb a re-appeared source unit and apply its body (#674).

    Reuses the preserved fragment id and moves the tombed fragment back into
    its routing directory. Only when the content actually changed does it
    rewrite the body in place — an unchanged re-appearance restores without a
    redundant write. Returns ``None`` when no tombed file maps to the id, so
    the caller falls back to a fresh write under the preserved id.
    """
    fragment.id = record.fragment_id
    restored = writer.restore_fragment(fragment)
    if restored is None:
        return None
    if new_hash == record.content_hash:
        return restored
    updated = writer.update_fragment(
        fragment, body, reclassify_threshold=reclassify_threshold
    )
    return updated if updated is not None else restored


def write_fragment_idempotent(
    ledger: SourceLedger | None,
    writer: VaultWriter,
    parsed: ParsedFragment,
    fragment: Fragment,
    body: str,
    reclassify_threshold: float,
) -> str:
    """Write or update the fragment; return ``"created"``/``"updated"``/``"unchanged"``.

    When the ledger already maps this source unit to a fragment and the
    content has *changed*, reuse that fragment id and rewrite it in place
    (preserving classifications/links, flagging re-classification on a material
    change per *reclassify_threshold*). Unchanged content falls through to the
    normal write, where the deterministic id makes it an idempotent no-op. A
    new unit (no prior ledger record) is written fresh and reported as created.
    """
    record = ledger_record(ledger, fragment)
    # `record is not None` guarantees `ledger is not None` (ledger_record
    # returns None for a missing ledger); the explicit guard narrows for mypy.
    if record is None or ledger is None:
        writer.write_fragment(fragment, body=body)
        return "created"
    new_hash = ledger.content_hash(parsed.content)
    if record.tombed:
        restored = restore_tombed(
            writer, fragment, record, body, new_hash, reclassify_threshold
        )
        if restored is None:
            # Tombstone lost out of band: recreate under the preserved id.
            writer.write_fragment(fragment, body=body)
        return "updated"
    if record.content_hash != new_hash:
        fragment.id = record.fragment_id
        updated = writer.update_fragment(
            fragment, body, reclassify_threshold=reclassify_threshold
        )
        if updated is None:
            # File gone out of band: recreate under the preserved id.
            writer.write_fragment(fragment, body=body)
        return "updated"
    # Known unit, content unchanged: idempotent no-op (the deterministic id
    # dedups the write). Reported as unchanged so it never inflates the
    # updated counter in the ingest summary.
    writer.write_fragment(fragment, body=body)
    return "unchanged"


def tomb_missing_units(
    ledger: SourceLedger | None,
    writer: VaultWriter,
    input_path: Path,
    seen_keys: set[str],
    errors: list[str],
    source_type: str,
) -> int:
    """Soft-tomb ledgered units absent from a full-source pass (#674).

    Only a directory (full-source) input computes a gone set; a single-file
    input never tombs — that guards the incremental epic from tombing every
    other unit when re-ingesting one file.

    A tomb that fails on I/O is collected into *errors* and the unit is left
    live in the ledger so the next full-source pass retries it, rather than
    crashing the whole run. Returns the number of units actually tombed.
    """
    if ledger is None or not input_path.is_dir():
        return 0
    tombed = 0
    for source_key in sorted(ledger.live_keys() - seen_keys):
        record = ledger.get(source_key)
        if record is None:
            continue
        try:
            writer.tomb_fragment(record.fragment_id)
        except (OSError, KeyError) as exc:
            errors.append(
                f"[{source_type}] failed to tomb {record.fragment_id}: {exc}",
            )
            continue
        ledger.record(
            source_key,
            record.fragment_id,
            record.content_hash,
            last_seen=record.last_seen,
            tombed=True,
        )
        tombed += 1
    return tombed


def should_skip_unit(
    *,
    filtering: bool,
    ledger: SourceLedger | None,
    parsed: ParsedFragment,
    fragment: Fragment,
    since: datetime | None,
) -> bool:
    """Return whether incremental mode should skip this unchanged unit (#677)."""
    if not filtering:
        return False
    # Deferred import: creek.pipeline pulls heavy stage deps; keep import light.
    from creek.pipeline import unit_is_changed

    record = ledger_record(ledger, fragment)
    content_hash = ledger.content_hash(parsed.content) if ledger is not None else ""
    return not unit_is_changed(parsed.timestamp, content_hash, record, since)


def run_ingest(
    *,
    ingestor_cls: type[Ingestor],
    source_type: str,
    input_path: Path,
    vault_path: Path,
    reclassify_threshold: float = 0.0,
    since: datetime | None = None,
    incremental: bool = False,
) -> IngestRunResult:
    """Run one ingestor and persist its output idempotently to the vault.

    UI-agnostic core of ``creek ingest``: no printing, no typer.Exit. A missing
    vault raises :class:`FileNotFoundError` (from :class:`VaultWriter`) for the
    caller to translate.

    Args:
        ingestor_cls: Concrete :class:`Ingestor` subclass to run.
        source_type: Registry key, used to prefix error messages (and to select
            the ledger — only ``"markdown"`` is ledger-backed).
        input_path: Source directory or file to ingest.
        vault_path: Vault root for :class:`VaultWriter`.
        reclassify_threshold: Body-similarity floor below which a materially
            edited unit is flagged for re-classification (#675).
        since: Incremental cutoff (#677) — only units newer than this are kept.
        incremental: Ledger-driven incremental mode (#677).

    Returns:
        An :class:`IngestRunResult` with the write tallies and any errors.
    """
    from creek.vault.writer import VaultWriter

    writer = VaultWriter(vault_path=vault_path)

    ingest_result = ingestor_cls().ingest(input_path)
    ledger = ledger_for_source(source_type, vault_path)
    filtering = since is not None or incremental

    errors: list[str] = [f"[{source_type}] {err}" for err in ingest_result.errors]
    written = 0
    skipped = 0
    counts: dict[str, int] = {}
    seen_keys: set[str] = set()
    for parsed in ingest_result.fragments:
        try:
            assembled = assemble_ingested_fragment(parsed)
        except (KeyError, ValueError) as exc:
            errors.append(
                f"[{source_type}] failed to assemble fragment from "
                f"{parsed.source_path}: {exc}",
            )
            continue
        attach_origin_key(ledger, parsed, assembled.fragment, vault_path)
        origin_key = assembled.fragment.source.origin_key
        # Record the key as seen BEFORE any incremental skip, so an unchanged
        # unit is never mistaken for a deleted one and tombed (#674/#677).
        if origin_key is not None:
            seen_keys.add(origin_key)
        if should_skip_unit(
            filtering=filtering,
            ledger=ledger,
            parsed=parsed,
            fragment=assembled.fragment,
            since=since,
        ):
            skipped += 1
            continue
        try:
            action = write_fragment_idempotent(
                ledger,
                writer,
                parsed,
                assembled.fragment,
                assembled.body,
                reclassify_threshold,
            )
        except (OSError, KeyError) as exc:
            errors.append(
                f"[{source_type}] failed to write {assembled.fragment.id}: {exc}",
            )
            continue
        record_in_ledger(ledger, parsed, assembled.fragment)
        written += 1
        counts[action] = counts.get(action, 0) + 1

    tombed = tomb_missing_units(
        ledger, writer, input_path, seen_keys, errors, source_type
    )
    return IngestRunResult(
        written=written,
        errors=errors,
        discovered=ingest_result.discovered,
        created=counts.get("created", 0),
        updated=counts.get("updated", 0),
        unchanged=counts.get("unchanged", 0),
        tombed=tombed,
        skipped=skipped,
    )
