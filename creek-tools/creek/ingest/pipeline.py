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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from creek.ingest.base import assemble_ingested_fragment

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from creek.ingest.base import Ingestor, ParsedFragment
    from creek.ingest.ledger import LedgerRecord, SourceLedger
    from creek.models import Fragment, PrivacyTier
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
        warnings: Non-fatal operator advisories. Unlike *errors*, these do not
            mean anything failed — they mean the run detected a vault state
            that will cause trouble if left alone (#1329).
    """

    written: int
    errors: list[str]
    discovered: int
    created: int
    updated: int
    unchanged: int
    tombed: int
    skipped: int
    warnings: list[str] = field(default_factory=list)


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


TOMBING_SOURCES: frozenset[str] = frozenset({"markdown"})
"""Source types whose directory ingest may soft-tomb units it no longer sees.

Deliberately *narrower* than "has a ledger", and separate from it (#1329).
:func:`resolve_ledger` lets a caller opt a non-markdown source type into
ledger-backed *identity* via ``ledger_source``, and until this split that same
opt-in silently armed :func:`tomb_missing_units` as well — so a directory
ingest under a borrowed ledger would tomb every previously-recorded unit it
did not happen to see. Identity and tombing are different questions; only a
source whose directory listing is genuinely the full, authoritative set of
live units belongs here.
"""


def ledger_for_source(source_type: str, vault_path: Path) -> SourceLedger | None:
    """Load the source ledger for mutable sources, else ``None`` (#672).

    Only the markdown (journal) source is ledger-wired; append-only event
    sources keep their content-hashed ids untouched.
    """
    if source_type != "markdown":
        return None
    from creek.ingest.ledger import SourceLedger

    return SourceLedger.load(vault_path, source=source_type)


def resolve_ledger(
    source_type: str,
    vault_path: Path,
    ledger_source: str | None,
) -> SourceLedger | None:
    """Return the ledger for this run, honouring an explicit override (#1023).

    With no override this is exactly :func:`ledger_for_source`, so the
    ``creek ingest`` CLI keeps its current non-ledgered semantics for every
    non-markdown source type — deliberately preserved, since widening
    :func:`ledger_for_source` itself would switch on ledger-backed identity
    *and* directory tombing for existing CLI users.

    An explicit *ledger_source* is how a caller that owns a stable staging
    path — ``creek.upload``, whose staged files live at a fixed
    vault-relative location — opts a non-markdown source type into
    ledger-backed identity. That is what earns the uploaded document its
    ``source.origin_key``, and therefore its coverage by the RTBF purge
    sweep, which keys on exactly that field.

    Args:
        source_type: Registry key of the ingestor being run.
        vault_path: Vault root holding ``00-Creek-Meta/State/ingest/``.
        ledger_source: Ledger name to force, or ``None`` for the default.

    Returns:
        The resolved :class:`SourceLedger`, or ``None`` when this run is
        unledgered.
    """
    if ledger_source is None:
        return ledger_for_source(source_type, vault_path)
    from creek.ingest.ledger import SourceLedger

    return SourceLedger.load(vault_path, source=ledger_source)


def stamp_declared_tier(fragment: Fragment, declared: PrivacyTier | None) -> None:
    """Merge a caller-declared privacy tier onto *fragment* (#1023).

    This is the *only* out-of-band tier channel into an ingested fragment:
    no ingestor in :mod:`creek.ingest` emits ``privacy_tier``, and a staged
    binary document (``.docx`` / ``.pdf`` / ``.xlsx``) has no frontmatter
    to carry one, so without this every uploaded document would land at
    ``unclassified``.

    The merge is :func:`creek.classify.privacy_pass.escalate` — never a
    plain assignment. A source that already declares a *higher* tier (an
    uploaded ``.md`` whose own frontmatter says ``privacy_tier:
    intimate``) must never be lowered to the caller's declared tier, and
    an assignment would do exactly that. In the other direction
    ``UNCLASSIFIED`` ranks below every real tier, so a declared ``open``
    still supersedes the ingest default.

    Args:
        fragment: The assembled fragment, before it is written.
        declared: The caller's declared tier, or ``None`` to declare nothing.
    """
    if declared is None:
        return
    # Deferred import: creek.classify pulls heavy deps; keep import light.
    from creek.classify.privacy_pass import escalate

    fragment.privacy_tier = escalate(fragment.privacy_tier, declared)


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

    When the ledger already maps this source unit to a fragment, **the
    ledger's recorded id wins on every branch** — changed, unchanged, or
    tombed. That assignment is hoisted above the branches deliberately
    (#1329): it used to happen only on the changed and tombed paths, leaving
    the unchanged path to write whatever id the ingestor had just *derived*.
    Any drift in id derivation — a timezone bug, a new hash input, a
    refactor — therefore turned every unchanged ledgered unit into a silent
    duplicate, because the writer's dedup keys on the id alone. Identity for
    a mutable file source is ledger-backed by design (#672 SPEC R1); the
    derivation is only how a *new* unit gets its first id.

    Beyond identity: changed content is rewritten in place (preserving
    classifications/links, flagging re-classification on a material change
    per *reclassify_threshold*); unchanged content falls through to a write
    the id makes a no-op; a new unit with no prior record is written fresh
    and reported as created.
    """
    record = ledger_record(ledger, fragment)
    # `record is not None` guarantees `ledger is not None` (ledger_record
    # returns None for a missing ledger); the explicit guard narrows for mypy.
    if record is None or ledger is None:
        writer.write_fragment(fragment, body=body)
        return "created"
    # The ledger is the authority on this unit's identity, on every branch
    # below. Do not push this back down into the branches (#1329).
    fragment.id = record.fragment_id
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
        updated = writer.update_fragment(
            fragment, body, reclassify_threshold=reclassify_threshold
        )
        if updated is None:
            # File gone out of band: recreate under the preserved id.
            writer.write_fragment(fragment, body=body)
        return "updated"
    # Known unit, content unchanged: idempotent no-op. The write resolves to
    # the existing file because `fragment.id` is now the *ledgered* id and the
    # writer's per-directory id index matches on it — the guarantee comes from
    # the ledger, not from trusting the derivation to reproduce (#1329).
    # Reported as unchanged so it never inflates the updated counter.
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

    Tombing is additionally gated on *source_type* being in
    :data:`TOMBING_SOURCES` (#1329). Holding a ledger is not sufficient: a
    caller can borrow one via ``ledger_source`` purely to pin identity, and
    that must not arm the tomb sweep for a source type whose directory
    listing is not the authoritative set of live units.

    A tomb that fails on I/O is collected into *errors* and the unit is left
    live in the ledger so the next full-source pass retries it, rather than
    crashing the whole run. Returns the number of units actually tombed.
    """
    if ledger is None or not input_path.is_dir():
        return 0
    if source_type not in TOMBING_SOURCES:
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


_UNPINNED_VAULT_WARNING = (
    "This vault has markdown fragments but an empty ingest ledger, so it has "
    "not been migrated for the #1329 id-derivation fix. Re-ingesting these "
    "sources will mint new ids and leave the existing fragments behind as "
    "duplicates. Run `creek ingest --pin-source-ids --vault <vault>` once "
    "first (use --dry-run to preview)."
)
"""Advisory shown when a populated vault has no ledger records yet (#1329)."""


def unpinned_vault_warning(
    ledger: SourceLedger | None,
    vault_path: Path,
) -> str | None:
    """Return the un-pinned-vault advisory, or ``None`` when it does not apply.

    A vault that already holds markdown fragments but whose ledger is empty
    predates the pin migration. The next ingest of those same sources will
    derive ids under the corrected rule, fail to match anything, and write
    duplicates — so the operator is told once, before that happens.

    The ledger's own emptiness *is* the marker; deriving the signal from it
    rather than from a version stamp in ``00-Creek-Meta/State/`` keeps this to
    one piece of state. A second marker is a second thing that can be wrong.

    Args:
        ledger: The ledger resolved for this run, or ``None`` when unledgered.
        vault_path: Vault root.

    Returns:
        The advisory text, or ``None`` when the vault is fresh, already
        pinned, or this run is unledgered.
    """
    if ledger is None or len(ledger) > 0:
        return None
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.is_dir():
        return None
    if not any(fragments_root.rglob("*.md")):
        return None
    return _UNPINNED_VAULT_WARNING


def run_ingest(
    *,
    ingestor_cls: type[Ingestor],
    source_type: str,
    input_path: Path,
    vault_path: Path,
    reclassify_threshold: float = 0.0,
    since: datetime | None = None,
    incremental: bool = False,
    ledger_source: str | None = None,
    privacy_tier: PrivacyTier | None = None,
    on_warning: Callable[[str], None] | None = None,
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
        ledger_source: Force a specific ledger name instead of the one
            :func:`ledger_for_source` would pick, opting a non-markdown
            source type into ledger-backed identity (#1023). **Hazard:**
            combining this with a *directory* ``input_path`` arms
            :func:`tomb_missing_units` for that ledger, soft-tombing every
            previously-ledgered unit the pass does not see; ``creek.upload``
            always passes a single file, which can never tomb.
        privacy_tier: Tier the caller declares for this content (#1023).
            Merged onto each fragment with
            :func:`creek.classify.privacy_pass.escalate`, so it can only
            raise the tier, never lower one the source already declared.
        on_warning: Called with each operator advisory **at the moment it is
            detected**, which for the un-pinned-vault advisory is before the
            first fragment is written (#1329). Surfaces still get every warning
            on :attr:`IngestRunResult.warnings`; this exists because an
            advisory whose whole purpose is "stop before this run hurts you"
            is worthless delivered after the run finished. Keeping it a plain
            ``str`` callback leaves this module UI-agnostic — the caller
            decides what printing means.

    Returns:
        An :class:`IngestRunResult` with the write tallies and any errors.
    """
    from creek.vault.writer import VaultWriter

    writer = VaultWriter(vault_path=vault_path)

    ingest_result = ingestor_cls().ingest(input_path)
    ledger = resolve_ledger(source_type, vault_path, ledger_source)
    filtering = since is not None or incremental

    warnings: list[str] = []

    def warn(message: str) -> None:
        """Record an advisory and hand it to the caller in the same breath.

        The single way a warning enters this run. Appending to the list
        without notifying *on_warning* is how an advisory becomes invisible,
        so the two are not separable here.
        """
        warnings.append(message)
        logger.warning("%s", message)
        if on_warning is not None:
            on_warning(message)

    # Checked BEFORE the write loop: once the loop has recorded its first
    # ledger entry the vault no longer looks un-pinned, and the advisory would
    # never fire for the very run it needed to warn about.
    unpinned = unpinned_vault_warning(ledger, vault_path)
    if unpinned is not None:
        warn(unpinned)

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
        stamp_declared_tier(assembled.fragment, privacy_tier)
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
        warnings=warnings,
    )
