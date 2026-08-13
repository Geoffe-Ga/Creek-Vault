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
from pathlib import Path  # runtime use: resolving recorded source paths
from typing import TYPE_CHECKING, Final, NamedTuple

from creek.ingest.base import assemble_ingested_fragment, generate_fragment_id
from creek.ingest.source_unit import compose_source_unit

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from creek.ingest.base import Ingestor, IngestResult, ParsedFragment
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
            that will cause trouble if left alone (#1329). Written for an
            operator at a terminal, so an advisory here may name real vault
            fragments: this channel is **not** safe to hand to a caller whose
            tier ceiling has not admitted them.
        ceiling_safe_warnings: The subset of *warnings* proven free of vault
            content — the only advisory channel that may cross an MCP tier
            ceiling (#1372). Proven at the producer, which is the one place
            that knows what it interpolated; see :func:`run_ingest`'s ``warn``
            closure. An advisory with no content-free form is simply absent
            here rather than sanitised, so this list can be shorter than
            *warnings* but never longer and never says anything *warnings*
            does not.
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
    ceiling_safe_warnings: list[str] = field(default_factory=list)


def resolve_recorded_source(source_path: str, vault_path: Path) -> Path:
    """Interpret a recorded ``source.original_file`` string as a path on disk.

    ``source.original_file`` is stored **verbatim** as whatever path the ingest
    run was handed — ``MarkdownIngestor.parse`` records ``str(raw.path)`` and
    the CLI does not resolve ``--source`` — so a vault ingested with ``creek
    ingest --source 00-Inbox`` from the vault root holds *relative* recorded
    paths. A bare :class:`Path` of such a string is anchored to the current
    working directory, which makes every later reader of that record answer a
    different question depending on where it was invoked from.

    Anchoring to the vault is the recovery: the vault root is a stable anchor
    the reader already holds as an argument, and it is the directory a
    vault-relative record was almost certainly written against.

    The current directory still wins when it resolves, so a run made from the
    original ingest's directory behaves exactly as before; the vault is
    consulted only for a relative path that names nothing where it stands.
    When neither locates a file the string is returned unchanged, so a caller
    asking whether the source still exists gets the same honest ``no`` — this
    widens what *resolves*, never what is assumed to exist.

    Args:
        source_path: The recorded ``source.original_file``, verbatim.
        vault_path: Vault root, used to anchor a relative record.

    Returns:
        The best on-disk interpretation of *source_path*.
    """
    candidate = Path(source_path)
    # The ``is_absolute`` arm states intent and saves a stat; it is not a
    # behavioural branch. ``vault_path / <absolute>`` is that absolute path
    # again, so an absolute record takes the same value either way — dropping
    # this clause is an equivalent mutation, and no test can kill it.
    if candidate.is_absolute() or candidate.exists():
        return candidate
    vault_anchored = vault_path / candidate
    if vault_anchored.exists():
        return vault_anchored
    return candidate


def derive_source_key(source_path: str, vault_path: Path) -> str:
    """Return a stable vault-relative ``source_key`` for a source file (#672).

    Prefers the path relative to the vault root (the stable identity a
    re-ingested edit is matched on); falls back to the bare filename when the
    source lives outside the vault.

    The recorded string is interpreted by :func:`resolve_recorded_source`
    rather than being anchored to the current directory, so a relative record
    keys the same way whichever directory the caller runs from. Sharing that
    one resolution with the ``pin_ids`` migration is deliberate: two functions
    disagreeing about where the same recorded path points is how the migration
    came to call a live in-vault source deleted while this function happily
    derived a key for it.
    """
    candidate = resolve_recorded_source(source_path, vault_path)
    try:
        return candidate.resolve().relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        return candidate.name


LEDGERED_SOURCE_TYPE: Final[str] = "markdown"
"""The one source type whose identity is ledger-backed by default (#672).

Three places have to agree on this name: :func:`ledger_for_source`, which
loads that ledger; :func:`unpinned_vault_warning`, whose whole subject is
that ledger; and the ``creek.ingest.pin_ids`` migration, which back-fills
it. They agree by importing this constant rather than by each spelling
``"markdown"`` and a comment promising to stay in step — a promise is not
an enforcement, and the review that produced this constant found the
advisory already consulting a different ledger than the migration wrote.
"""

TOMBING_SOURCES: frozenset[str] = frozenset({LEDGERED_SOURCE_TYPE})
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
    if source_type != LEDGERED_SOURCE_TYPE:
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
    """Stamp the fragment's ``source.origin_key`` before it is written (#672).

    The key addresses the *unit* the fragment came from, not merely the file
    (#1305). Composed after :func:`derive_source_key` rather than by handing
    it a pre-composed string, so that function keeps resolving a path that
    actually exists on disk and the vault-relative/bare-filename choice is
    made on the real file.

    This is the half of #1305 a per-sheet fragment id does not reach.
    ``derive_source_key`` keys on the file, so without the unit every sheet
    of a workbook shares one ``origin_key`` and one ledger record, and
    :func:`write_fragment_idempotent` then reassigns ``fragment.id`` to the
    record's id and overwrites sheet 1 with sheet 2 — reporting ``updated``
    while losing the content.
    """
    if ledger is None:
        return
    base_key = derive_source_key(parsed.source_path, vault_path)
    fragment.source.origin_key = compose_source_unit(base_key, parsed.source_unit)


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
    *,
    discovery_complete: bool,
) -> int:
    """Soft-tomb ledgered units absent from a full-source pass (#674).

    Only a directory (full-source) input computes a gone set; a single-file
    input never tombs — that guards the incremental epic from tombing every
    other unit when re-ingesting one file.

    **The pass must first have been able to see the whole source.** Tombing
    is a soft-delete driven by *absence*, and absence means nothing when the
    walk that failed to see a key could not read part of the tree: the key's
    source may be sitting in exactly the part that could not be listed. So
    ``discovery_complete=False`` returns 0 before anything else is
    considered — the gone set is not computable, not merely inadvisable
    (#1444).

    *discovery_complete* is keyword-only with **no default**, copying the
    ``warn(..., *, ceiling_safe)`` precedent in :func:`run_ingest`. mypy
    strict covers ``creek/``, so a future caller that has not thought about
    whether its pass had the authority to delete cannot compile; a default of
    ``True`` would be fail-open, which is the defect rather than its fix.

    **Why an incomplete pass only warns and still exits 0.** ``creek.cli``'s
    ``_warn_if_discovered_but_empty`` returns early on ``written > 0 or
    discovered == 0``, so it structurally cannot catch this shape, and
    widening it would change behaviour for unrelated sources. Routing a new
    exit code out of ``_run_ingest`` would mean widening the public
    :class:`IngestRunResult` dataclass, which ``creek_mcp`` reads. A
    ``--strict`` flag is therefore left for its own issue; nothing is
    destroyed either way, which is the property that had to hold.

    Tombing is additionally gated on *source_type* being in
    :data:`TOMBING_SOURCES` (#1329). Holding a ledger is not sufficient: a
    caller can borrow one via ``ledger_source`` purely to pin identity, and
    that must not arm the tomb sweep for a source type whose directory
    listing is not the authoritative set of live units.

    A tomb that fails on I/O is collected into *errors* and the unit is left
    live in the ledger so the next full-source pass retries it, rather than
    crashing the whole run. A tomb that finds nothing to move is treated the
    same way (#1332) — see :func:`_tomb_one_unit`.

    Returns:
        The number of fragments this call actually relocated into
        ``10-Liminal/Orphaned/``. Not the number of units considered, and
        not the number of ledger records written: a unit whose fragment was
        already tombed has its record reconciled but moved nothing, so it
        does not count. The figure is printed to the operator verbatim, so
        it may only ever name work that happened.
    """
    # First, above every other guard. An incomplete enumeration cannot tell
    # "the operator deleted this unit" from "the walk could not read the
    # directory it lives in", so there is no gone set to compute. Being first
    # is also what covers the ``ledger_source`` hazard documented on
    # :func:`run_ingest`: a borrowed ledger plus a directory input is exactly
    # the shape that arms this sweep for a source type whose listing was
    # never authoritative to begin with.
    if not discovery_complete:
        return 0
    if ledger is None or not input_path.is_dir():
        return 0
    if source_type not in TOMBING_SOURCES:
        return 0
    tombed = 0
    for source_key in sorted(ledger.live_keys() - seen_keys):
        record = ledger.get(source_key)
        if record is None:
            continue
        tombed += _tomb_one_unit(ledger, writer, record, errors, source_type)
    return tombed


def _tomb_one_unit(
    ledger: SourceLedger,
    writer: VaultWriter,
    record: LedgerRecord,
    errors: list[str],
    source_type: str,
) -> int:
    """Soft-tomb one vanished unit; return 1 only if this call moved it.

    The three outcomes are kept distinct because conflating them is the
    #1332 defect. Before it, the caller discarded
    :meth:`~creek.vault.writer.VaultWriter.tomb_fragment`'s return value and
    recorded ``tombed=True`` unconditionally, so a lookup that found nothing
    wrote a permanent lie into the ledger — ``live_keys`` never revisits a
    tombed record — and reported a tomb the operator could disprove by
    opening the vault.

    - **Relocated.** The fragment moved. Record it and count it.
    - **Already tombed.** ``tomb_fragment`` returns ``None`` both when it
      found nothing and when the fragment is already in the orphan
      directory, so the two are separated by asking
      :meth:`~creek.vault.writer.VaultWriter.find_tombed_fragment`. This is
      the state a crash between the file move and the ledger append leaves
      behind — the tomb-then-record ordering is deliberate, because the
      reverse leaves ``tombed=True`` on a live fragment, which
      ``live_keys`` guarantees is permanent. Recording it here is not a
      guess: the vault is showing the tombstone. Nothing moved, so nothing
      is counted.
    - **Not found.** No live fragment and no tombstone. The record stays
      live so the next full-source pass retries, and the miss is reported.
      Recording success here is the behaviour this function exists to
      remove; recording a third ledger state instead would need every
      reader of ``tombed`` to learn a new case to answer the same question.

    Args:
        ledger: The ledger to reconcile; written only on a proven tomb.
        writer: Vault writer performing the relocation and the lookups.
        record: The ledgered unit that this pass no longer saw.
        errors: Collector for ``[<source_type>] …`` operator messages.
        source_type: Registry key, used to prefix those messages.

    Returns:
        ``1`` when this call relocated the fragment, else ``0``.
    """
    try:
        moved = writer.tomb_fragment(record.fragment_id)
        # ``or`` short-circuits, so the orphan directory is consulted only
        # when nothing was relocated. A ``Path`` is always truthy, so this
        # falls back exactly when *moved* is ``None``.
        tombstone = moved or writer.find_tombed_fragment(record.fragment_id)
    except (OSError, KeyError) as exc:
        errors.append(
            f"[{source_type}] failed to tomb {record.fragment_id}: {exc}",
        )
        return 0
    if tombstone is None:
        errors.append(
            f"[{source_type}] no fragment found to tomb for "
            f"{record.fragment_id} (source {record.source_key}); left live "
            f"in the ledger for the next full-source pass",
        )
        return 0
    ledger.record(
        record.source_key,
        record.fragment_id,
        record.content_hash,
        last_seen=record.last_seen,
        tombed=True,
    )
    return 1 if moved is not None else 0


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
    source_type: str,
    vault_path: Path,
) -> str | None:
    """Return the un-pinned-vault advisory, or ``None`` when it does not apply.

    A vault that already holds markdown fragments but whose **markdown**
    ledger is empty predates the pin migration. The next ingest of those same
    sources will derive ids under the corrected rule, fail to match anything,
    and write duplicates — so the operator is told once, before that happens.

    That ledger's own emptiness *is* the marker; deriving the signal from it
    rather than from a version stamp in ``00-Creek-Meta/State/`` keeps this to
    one piece of state. A second marker is a second thing that can be wrong.

    **This deliberately does not accept a ledger.** It used to take whichever
    ledger the run had resolved, and a run with a ``ledger_source`` override —
    which the ``creek.upload`` MCP tool always passes, including for an
    uploaded ``.md`` — therefore weighed an unrelated ledger's emptiness. That
    produced a spurious warning on an already-migrated vault, and, worse, went
    permanently silent on a genuinely un-pinned one as soon as the borrowed
    ledger gained any record. The subject of this advisory is fixed by what
    the advisory *says*, so it is looked up here from :data:`LEDGERED_SOURCE_TYPE`
    and cannot be substituted by anything a caller overrides.

    The run's *source_type* still gates it, because #1329 moved markdown id
    derivation only: a document or image run has nothing to be warned about,
    and a warning delivered on a run it does not apply to is trained-away
    noise. Unlike the resolved ledger, ``source_type`` names the identity
    scheme this run writes under and no override can move it.

    Args:
        source_type: Registry key of the ingestor being run, verbatim.
        vault_path: Vault root.

    Returns:
        The advisory text, or ``None`` when this run writes under some other
        source type's identity, or the vault is fresh or already pinned.
    """
    if source_type != LEDGERED_SOURCE_TYPE:
        return None
    from creek.ingest.ledger import SourceLedger

    if len(SourceLedger.load(vault_path, source=LEDGERED_SOURCE_TYPE)) > 0:
        return None
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.is_dir():
        return None
    if not any(fragments_root.rglob("*.md")):
        return None
    return _UNPINNED_VAULT_WARNING


_COLLAPSED_UNIT_WARNING_TEMPLATE = (
    "This vault holds {count} fragment(s) written before #1305, when every "
    "sheet of a multi-sheet workbook shared one fragment id and only the "
    "FIRST reached disk. Each one is now superseded by the per-sheet "
    "fragments this run writes, and is left in place: {sample}. Nothing is "
    "deleted automatically — inspect with `creek purge source "
    "--source-path <path> --match exact --dry-run`, and purge only what you "
    "recognise. Links pointing at a superseded fragment keep resolving to "
    "it until you do."
)
"""Advisory for a vault still holding pre-#1305 collapsed fragments.

The console rendering. ``{sample}`` interpolates real pre-existing vault
fragment ids, which is exactly why it must never leave this process for a
caller whose ceiling has not admitted them — see
:data:`_COLLAPSED_UNIT_SAFE_TEMPLATE` for the form that may.
"""

_COLLAPSED_UNIT_SAFE_TEMPLATE = (
    "This vault holds {count} fragment(s) written before #1305, when every "
    "sheet of a multi-sheet workbook shared one fragment id and only the "
    "FIRST reached disk. Each one is now superseded by the per-sheet "
    "fragments this run writes, and is left in place. Run the same ingest "
    "at a terminal to see which fragments. Nothing is deleted automatically "
    "— inspect with `creek purge source --source-path <path> --match exact "
    "--dry-run`, and purge only what you recognise. Links pointing at a "
    "superseded fragment keep resolving to it until you do."
)
"""The content-free twin of :data:`_COLLAPSED_UNIT_WARNING_TEMPLATE` (#1372).

Interpolates ``{count}`` and nothing else. The count stays because an
advisory that suppresses the number as well as the ids reports no data loss
at all; what is withheld is only the ``{sample}`` clause, replaced by the
instruction that recovers it through a surface — the terminal — where the
operator is entitled to every fragment in their own vault.
"""

_COLLAPSED_UNIT_SAMPLE = 3
"""How many superseded ids the advisory names before it stops listing."""


class Advisory(NamedTuple):
    """An operator advisory in both of its disclosure forms.

    Named for the shape rather than for one producer, because
    :func:`pre_write_advisories` collects several of them into one
    homogeneous list; a per-advisory type would make that list heterogeneous
    for no gain, since every producer answers the same two questions.

    Attributes:
        message: What an operator at a terminal sees, in full detail. It may
            name real vault content — the superseded ids of the pre-#1305
            advisory, the operator's own source path in the #1444 one —
            which is what makes it the useful form, and the one that may not
            cross a tier ceiling.
        ceiling_safe: What may cross a tier ceiling. The same finding with
            every interpolated vault id and filesystem path withheld.
    """

    message: str
    ceiling_safe: str


def collapsed_unit_warning(
    writer: VaultWriter,
    fragments: Sequence[ParsedFragment],
) -> Advisory | None:
    """Return the pre-#1305 collapsed-fragment advisory, or ``None`` (#1305).

    Before #1305 every sheet of a multi-sheet workbook derived the same
    fragment id, so ``_write_model``'s first-writer-wins dedup kept sheet 1
    and silently dropped the rest. This run writes each sheet under its own
    id, which is the fix — but it also means the single fragment the old
    derivation left in an operator's vault is now superseded by N new ones
    and is not overwritten by any of them.

    **Detected, reported, and left alone.** That is deliberately the same
    answer #1304 gave the same shape of problem: naming the strays and
    handing over a read-only inspection command, rather than deleting vault
    content on the strength of a heuristic. Deleting a fragment is not
    something an ingest run gets to decide, and the id recomputation below,
    however exact, cannot know whether the operator has since edited that
    file, linked to it, or curated it into a thread.

    The detection needs no vault walk, no frontmatter parsing and no
    scoping heuristic, because the superseded id is *exactly* what this
    same fragment would have hashed to with no unit — same source path,
    same timestamp, same content. So it is recomputed from the parsed
    fragment in hand and looked up in the writer's id index. That also
    sidesteps the two traps a frontmatter-based detector hits (``ingested``
    reads back as a ``str``, and its trailing ``Z`` hashes differently from
    the ``+00:00`` the derivation emits) by never reading frontmatter.

    It follows that the advisory is self-clearing: once the operator purges
    the superseded fragments, the lookup misses and the run goes quiet. It
    is also silent on a fresh vault and on every single-unit source, which
    keeps no unit and therefore has nothing to supersede.

    Args:
        writer: The vault writer, consulted read-only via
            :meth:`~creek.vault.writer.VaultWriter.find_fragment`.
        fragments: This run's parsed fragments, before any are written.

    Both renderings are built here, from the one ``superseded`` list, because
    this is the only place that knows which ids were interpolated (#1372). A
    downstream filter would have to guess, and the guess is not available: an
    id-shaped regex catches an id but not a fragment title or a body excerpt.

    Returns:
        The advisory in both disclosure forms, or ``None`` when no superseded
        fragment is present.
    """
    superseded: list[str] = []
    for parsed in fragments:
        if parsed.source_unit is None:
            continue
        legacy_id = generate_fragment_id(
            parsed.source_path, parsed.timestamp, parsed.content
        )
        if legacy_id in superseded:
            continue
        if writer.find_fragment(legacy_id) is not None:
            superseded.append(legacy_id)
    if not superseded:
        return None
    sample = ", ".join(sorted(superseded)[:_COLLAPSED_UNIT_SAMPLE])
    if len(superseded) > _COLLAPSED_UNIT_SAMPLE:
        sample = f"{sample}, …"
    count = len(superseded)
    return Advisory(
        message=_COLLAPSED_UNIT_WARNING_TEMPLATE.format(count=count, sample=sample),
        ceiling_safe=_COLLAPSED_UNIT_SAFE_TEMPLATE.format(count=count),
    )


_INCOMPLETE_DISCOVERY_TEMPLATE = (
    "Ingest did not see all of {path}; {count} part(s) could not be listed "
    "or read. Nothing was removed on this pass's evidence."
)
"""Advisory for a pass whose discovery could not enumerate the whole source.

The console rendering. ``{path}`` interpolates the operator's own filesystem
layout, which is exactly why it must not leave this process for a caller whose
ceiling has not admitted it — see :data:`_INCOMPLETE_DISCOVERY_SAFE_MESSAGE`
for the form that may.

"listed **or** read" because the count aggregates two genuinely different
failures: a subtree whose ``scandir`` was refused (recorded by
``_enumerate_markdown_paths``' ``onerror`` as "cannot list …") and a candidate
file whose ``read_bytes()`` was refused ("cannot read …"). Saying only "read"
would misdescribe the directory case, and the operator's next move — go look at
the ``errors`` lines, which name each one precisely — is the same either way.
"""

_INCOMPLETE_DISCOVERY_SAFE_MESSAGE = (
    "Ingest did not see the whole source; nothing was removed on this pass's evidence."
)
"""The content-free twin of :data:`_INCOMPLETE_DISCOVERY_TEMPLATE` (#1372).

Interpolates nothing at all. The path is withheld because a source path *is*
operator filesystem layout; the count goes with it, because a count of
unreadable parts of a named tree is a fact about that tree's shape. What
survives is the finding and its consequence, which is what the remote caller
actually needs in order to know this run's evidence was incomplete.
"""


def incomplete_discovery_advisory(
    ingest_result: IngestResult,
    input_path: Path,
) -> Advisory | None:
    """Return the incomplete-discovery advisory, or ``None`` when it does not apply.

    Disarming :func:`tomb_missing_units` is necessary but not sufficient: a
    run that quietly ingests nothing looks exactly like a run with nothing to
    do, so without this the operator never learns a folder went unreadable —
    and on the ``creek sync`` surface, which prints no summary and reads no
    return value, nothing at all would be said (#1329/#1444).

    **The wording deliberately does not claim the orphan sweep was skipped.**
    This advisory fires for every ingestor and for single-*file* inputs too —
    ``creek_mcp.tools.upload`` routes an uploaded file through
    :func:`run_ingest` — and on that path :func:`tomb_missing_units`' own
    ``not input_path.is_dir()`` guard means no sweep was ever armed. "The
    sweep was skipped" would simply be false there. What is true on every
    path is that the pass saw only part of the source and destroyed nothing
    on that partial evidence, so that is what it says.

    The two renderings are built here, at the producer, for the same reason
    :func:`run_ingest`'s ``warn`` closure demands the decision at the
    producer: this is the only place that knows what was interpolated. A
    downstream scrub would have to recognise an operator's directory layout
    by shape, and that guess is not available.

    The count is :meth:`~creek.ingest.base.IngestResult.discovery_failure_count`
    — the number of failures the discovery stage actually recorded on
    ``errors``, so every part it counts is a line the operator can go and
    read. It is printed verbatim, so like ``tombed`` it may only ever name
    work that happened.

    Args:
        ingest_result: The completed ingest, consulted for
            :attr:`~creek.ingest.base.IngestResult.discovery_complete` and
            the failure count.
        input_path: The source the operator named, verbatim.

    Returns:
        The advisory in both disclosure forms, or ``None`` when discovery
        saw the whole source.
    """
    if ingest_result.discovery_complete:
        return None
    return Advisory(
        message=_INCOMPLETE_DISCOVERY_TEMPLATE.format(
            path=input_path,
            count=ingest_result.discovery_failure_count(),
        ),
        ceiling_safe=_INCOMPLETE_DISCOVERY_SAFE_MESSAGE,
    )


def pre_write_advisories(
    *,
    source_type: str,
    vault_path: Path,
    writer: VaultWriter,
    ingest_result: IngestResult,
    input_path: Path,
) -> list[Advisory]:
    """Collect every advisory that must be raised BEFORE the first write.

    All three of these are checked before :func:`run_ingest`'s write loop,
    and each for its own reason:

    * **Incomplete discovery.** The run destroyed nothing on partial
      evidence, and a run that quietly writes little or nothing is
      indistinguishable from a run with nothing to do (#1444).
    * **Un-pinned vault.** Once the loop has recorded its first ledger entry
      the vault no longer *looks* un-pinned, so an advisory raised afterwards
      would never fire for the very run it needed to warn about (#1329).
    * **Collapsed pre-#1305 units.** The operator is told which fragments
      this run supersedes before the run that supersedes them finishes,
      rather than after (#1305).

    Gathering them here keeps :func:`run_ingest` — already at the project's
    complexity ceiling — from growing a branch per advisory, and makes the
    ordering of the operator's console output one readable list instead of an
    implicit consequence of statement order.

    Args:
        source_type: Registry key of the ingestor being run.
        vault_path: Vault root.
        writer: The vault writer, consulted read-only.
        ingest_result: The completed ingest, before anything is written.
        input_path: The source the operator named, verbatim.

    Returns:
        The advisories that apply, in the order they should be delivered.
        Empty when the run has nothing to say.
    """
    advisories: list[Advisory] = []
    incomplete = incomplete_discovery_advisory(ingest_result, input_path)
    if incomplete is not None:
        advisories.append(incomplete)
    unpinned = unpinned_vault_warning(source_type, vault_path)
    if unpinned is not None:
        # It travels verbatim: the text is a fixed constant with no
        # interpolation, so it names a command rather than a fragment, and
        # withholding it from an MCP caller would be caution with no subject.
        advisories.append(Advisory(message=unpinned, ceiling_safe=unpinned))
    collapsed = collapsed_unit_warning(writer, ingest_result.fragments)
    if collapsed is not None:
        advisories.append(collapsed)
    return advisories


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
            decides what printing means. It receives the *operator* rendering,
            which may name real vault fragments, so it is for a surface with a
            console and an operator standing at it; a surface answering a
            remote caller reads
            :attr:`IngestRunResult.ceiling_safe_warnings` instead (#1372).

    Returns:
        An :class:`IngestRunResult` with the write tallies and any errors.
    """
    from creek.vault.writer import VaultWriter

    writer = VaultWriter(vault_path=vault_path)

    ingest_result = ingestor_cls().ingest(input_path)
    ledger = resolve_ledger(source_type, vault_path, ledger_source)
    filtering = since is not None or incremental

    warnings: list[str] = []
    ceiling_safe_warnings: list[str] = []

    def warn(message: str, *, ceiling_safe: str | None) -> None:
        """Record an advisory and hand it to the caller in the same breath.

        The single way a warning enters this run. Appending to the list
        without notifying *on_warning* is how an advisory becomes invisible,
        so the two are not separable here.

        *message* is what an operator at a terminal sees, and it is allowed to
        name real vault fragments. *ceiling_safe* is the same finding in a form
        proven free of vault content, and it is the only one that may cross an
        MCP tier ceiling; a caller with no console — every MCP tool — gets only
        that one (#1372). ``None`` means this advisory has no content-free form
        and therefore does not travel.

        Deciding here, at the producer, is the whole point. The producer is the
        only place that knows what it interpolated; a downstream scrub would
        have to guess which substrings are vault content, and the guess is not
        available — an id-shaped regex catches an id but not a fragment title
        or a body excerpt. For the same reason *ceiling_safe* is keyword-only
        with **no default**: mypy strict covers ``creek/``, so every present and
        future advisory producer is forced to state which it is. A default of
        ``ceiling_safe=message`` would be fail-open, which is the defect rather
        than the fix.

        Args:
            message: The operator-facing advisory, in full detail.
            ceiling_safe: The content-free rendering of the same advisory, or
                ``None`` when it has none.
        """
        warnings.append(message)
        logger.warning("%s", message)
        if ceiling_safe is not None:
            ceiling_safe_warnings.append(ceiling_safe)
        if on_warning is not None:
            on_warning(message)

    # Raised BEFORE the write loop, every one of them; see
    # :func:`pre_write_advisories` for why each cannot wait until after.
    for advisory in pre_write_advisories(
        source_type=source_type,
        vault_path=vault_path,
        writer=writer,
        ingest_result=ingest_result,
        input_path=input_path,
    ):
        warn(advisory.message, ceiling_safe=advisory.ceiling_safe)

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
        ledger,
        writer,
        input_path,
        seen_keys,
        errors,
        source_type,
        discovery_complete=ingest_result.discovery_complete,
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
        ceiling_safe_warnings=ceiling_safe_warnings,
    )
