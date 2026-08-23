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

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path  # runtime use: resolving recorded source paths
from typing import TYPE_CHECKING, Final, NamedTuple

from creek.ingest.base import (
    IngestResult,
    assemble_ingested_fragment,
    generate_fragment_id,
)
from creek.ingest.images import ImageIngestor
from creek.ingest.source_unit import compose_source_unit

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from creek.config import OCRConfig
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
            that will cause trouble if left alone (#1329). Written for an
            operator at a terminal, so an advisory here may name real vault
            fragments: this channel is **not** safe to hand to a caller whose
            tier ceiling has not admitted them.
        fragment_ids: The vault id of every fragment this run wrote, in write
            order (#1525). Populated for *every* run, but load-bearing only
            where the ledger is not: an upload of a whole export archive runs
            **ledger-free** — the four export ingestors emit no
            ``source_unit``, so under a borrowed ledger every turn of one
            conversation file shares one key and overwrites its predecessor —
            and the ledger is therefore no longer available to answer "which
            fragments did this call produce". A response that could not answer
            that would leave a right-to-be-forgotten request with nothing to
            act on.
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
    fragment_ids: list[str] = field(default_factory=list)
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


_OUT_OF_VAULT_PREFIX: Final[str] = "external"
"""Namespace every out-of-vault ``source_key`` is written under (#953).

A fixed first segment keeps the two halves of the key space visibly apart in
a ledger file and in a fragment's frontmatter, so an operator reading either
can tell at a glance whether a key names something inside their vault. It is
*not* a reservation the in-vault arm defers to: that arm is tried first and
wins outright, so a vault directory literally named ``external/<hex>/`` still
keys as its own plain vault-relative path.
"""

_PATH_DIGEST_CHARS: Final[int] = 16
"""How much of the parent directory's SHA-256 goes into an out-of-vault key.

16 hex characters is 64 bits. The digest only has to separate the directories
one operator ingests from, so the birthday bound over even a million distinct
source directories is on the order of 1e-8 — while a full 64-character digest
would make every key unreadable for no additional safety.
"""


_DERIVED_OUT_OF_VAULT_KEY_RE: Final = re.compile(
    rf"^{_OUT_OF_VAULT_PREFIX}/[0-9a-f]{{{_PATH_DIGEST_CHARS}}}/[^/]+$",
)
"""Matches a string this module already minted as an out-of-vault key (#1575).

Since ``source.original_file`` now *stores* the derived key rather than the
host path, the recorded string is fed back through :func:`derive_source_key`
by ``pin_ids`` and by the ledger's adoption proof. Without recognising its own
output, that second derivation resolves ``external/<digest>/<name>`` against
the current working directory and mints a *different* key — so the migration
would write a ledger record the next ingest never looks up.

Built from the same two constants the minting arm uses, so the recogniser and
the writer cannot drift apart. An in-vault directory genuinely named
``external/<16 hex>/`` is unaffected: the vault-relative arm produces exactly
this string for it, so returning early is the same answer by a shorter route.
"""


def derive_source_key(source_path: str, vault_path: Path) -> str:
    """Return a stable ``source_key`` for a source file (#672, #953).

    Prefers the path relative to the vault root (the stable identity a
    re-ingested edit is matched on). A source that lives *outside* the vault
    is keyed ``external/<digest of its parent>/<filename>``.

    **The in-vault arm below is byte-identical to the one every already-synced
    vault was keyed with, and must stay that way.** A fragment's identity is
    ledger-backed on exactly this string: change how an in-vault key is spelled
    and the next ingest matches nothing already on disk, mints fresh ids and
    leaves every predecessor behind as an orphan — the #1304/#1329/#1384 scar.
    ``tests/test_ingest_source_key_identity.py`` pins the exact literals.

    **Why the out-of-vault arm had to move.** It returned ``candidate.name``,
    a bare filename, so ``~/a/report.pdf`` and ``~/b/report.pdf`` shared one
    key. That is not a duplicate, it is content destruction:
    :func:`write_fragment_idempotent` hoists ``fragment.id =
    record.fragment_id`` above every branch, so the second file adopts the
    first's id and rewrites its body. It is live for every ``creek sync``,
    which ingests the ``journal`` source as source type ``markdown`` from an
    external drive path, recursively, enabled by default.

    **Why the digest covers the parent and the filename stays literal.**
    Identity is bijective on ``(parent, name)``, which *is* the resolved path,
    so this is exactly as collision-free as digesting the whole path — while
    every file from one source directory groups under one digest and the key
    stays recognisable to an operator reading a ledger. The digest is also
    hex, so it can never contain :data:`~creek.ingest.source_unit.
    SOURCE_UNIT_SEPARATOR` and the base half of a composed key stays
    unambiguous.

    ``.resolve()`` runs first so a symlink and its target yield one key: they
    are one file, and keying them apart would ingest it twice.

    The recorded string is interpreted by :func:`resolve_recorded_source`
    rather than being anchored to the current directory, so a relative record
    keys the same way whichever directory the caller runs from. Sharing that
    one resolution with the ``pin_ids`` migration is deliberate: two functions
    disagreeing about where the same recorded path points is how the migration
    came to call a live in-vault source deleted while this function happily
    derived a key for it.

    Args:
        source_path: The recorded ``source.original_file``, verbatim.
        vault_path: Vault root.

    Returns:
        The vault-relative POSIX path, or the namespaced out-of-vault key.
    """
    if _DERIVED_OUT_OF_VAULT_KEY_RE.match(source_path):
        return source_path
    candidate = resolve_recorded_source(source_path, vault_path)
    try:
        return candidate.resolve().relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        resolved = candidate.resolve()
        digest = hashlib.sha256(
            resolved.parent.as_posix().encode("utf-8"),
        ).hexdigest()[:_PATH_DIGEST_CHARS]
        return f"{_OUT_OF_VAULT_PREFIX}/{digest}/{resolved.name}"


def legacy_source_key(source_path: str, vault_path: Path) -> str | None:
    """Return the pre-#953 bare-filename key for an out-of-vault source (#953).

    The single source of truth for "what did this used to be called", so the
    adoption path in :func:`adopt_legacy_ledger_key` and the seen-key widening
    in :func:`establish_source_identity` cannot disagree about it.

    **It answers ``None`` for anything in-vault**, and that is the load-bearing
    half: an in-vault key never had a legacy spelling, so every piece of
    migration machinery downstream is structurally unable to reach the in-vault
    corpus even by accident. The guard is the same ``relative_to`` the key
    derivation itself uses, not a second predicate that could drift from it.

    ``candidate.name`` — the *unresolved* candidate's basename — is returned
    verbatim, because that is the exact string the old code wrote into the
    ledger. Resolving first would produce the target's name for a symlinked
    source and miss the record that is actually there.

    Args:
        source_path: The recorded ``source.original_file``, verbatim.
        vault_path: Vault root.

    Returns:
        The bare filename this source used to key as, or ``None`` when it
        lives inside the vault and therefore never had one.
    """
    candidate = resolve_recorded_source(source_path, vault_path)
    try:
        candidate.resolve().relative_to(vault_path.resolve())
    except ValueError:
        return candidate.name
    return None


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

LEDGERED_SOURCES: frozenset[str] = frozenset(
    {LEDGERED_SOURCE_TYPE, "generic", "document"},
)
"""Source types whose fragment identity is ledger-backed by default (#1363).

Holding a ledger buys a source type three things and **no others**: an
idempotent re-ingest (an edited unit updates in place under its existing id
instead of orphaning it), a ``source.origin_key`` on every fragment — which is
the only field the RTBF purge sweep resolves a target from — and a content
hash to tell an edit from a touch.

It buys **no deletion authority**. That is :data:`TOMBING_SOURCES`, a
separate set consulted by a separate gate, and the two are deliberately kept
as two names rather than one: a set that meant both would make "let this
source type keep its ids" and "let this source type delete vault content"
the same decision, which is the coupling #1329 was filed to remove.

``generic`` and ``document`` joined in #1363. Both derive an id from the
source's mtime, so before this a ``touch`` alone minted a duplicate, an edit
orphaned its predecessor, and neither ever received an ``origin_key`` — which
means content ingested through them was unreachable by an erasure request.
The registry spells them singular: ``document``, not ``documents``.

Deliberately **not** widened: ``image``, ``code``, ``spreadsheet``,
``presentation`` and the four append-only export ingestors. Each needs its own
idempotency proof, ``spreadsheet`` interacts with the #1305 sub-unit path, and
the export ingestors emit no ``source_unit`` at all — under a ledger every
turn of one conversation file would share a key and overwrite its
predecessor. Widening them is a follow-up, not a line in this set.

**Why widening cannot import the collision defect.** Turning the ledger on for
a source type whose key is a bare filename is strictly worse than leaving it
off: two same-named documents would then resolve to one record and one would
overwrite the other. Measured, before the fix. That is why #953's key change
lands first in this branch and why
``tests/test_ingest_out_of_vault_identity.py`` asserts both halves in a single
test — they are only safe together.
"""

TOMBING_SOURCES: frozenset[str] = frozenset({LEDGERED_SOURCE_TYPE})
"""Source types whose directory ingest may soft-tomb units it no longer sees.

Deliberately *narrower* than :data:`LEDGERED_SOURCES`, and separate from it
(#1329). :func:`resolve_ledger` lets a caller opt a non-markdown source type
into ledger-backed *identity* via ``ledger_source``, and until this split that
same opt-in silently armed :func:`tomb_missing_units` as well — so a directory
ingest under a borrowed ledger would tomb every previously-recorded unit it
did not happen to see. Identity and tombing are different questions; only a
source whose directory listing is genuinely the full, authoritative set of
live units belongs here.

The markdown journal qualifies because its whole corpus lives under the
directory being walked. A document or a generic file does not: an operator
moving a PDF out of a staging folder is not asking for its fragment to be
soft-deleted.
"""


def ledger_for_source(source_type: str, vault_path: Path) -> SourceLedger | None:
    """Load the source ledger for a ledger-backed source, else ``None`` (#672).

    Membership of :data:`LEDGERED_SOURCES` is the whole question; append-only
    event sources keep their content-hashed ids and never consult a ledger.

    Each source type loads **its own** ledger file, named for the type, so a
    newly ledgered type starts from an empty file rather than inheriting
    another type's key space.

    Args:
        source_type: Registry key of the ingestor being run.
        vault_path: Vault root holding ``00-Creek-Meta/State/ingest/``.

    Returns:
        That source type's ledger, or ``None`` when it is unledgered.
    """
    if source_type not in LEDGERED_SOURCES:
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


def record_source_provenance(
    parsed: ParsedFragment,
    fragment: Fragment,
    vault_path: Path,
) -> None:
    """Replace the host path on ``source.original_file`` with a vault key (#1575).

    Every ingestor's ``parse`` records ``str(raw.path)`` verbatim, which for a
    CLI ingest is the operator's absolute path — their account name, their
    directory chain, and by implication the subject matter of the document.
    Fragment frontmatter is not a private channel:
    :mod:`creek.generate.indexes` renders ``source.original_file`` into a
    generated vault index note, and a remote MCP caller reads fragments
    through the tier ceiling.

    **Two call sites, not eleven rewrites.** ``Ingestor.parse`` never receives
    ``vault_path`` and therefore cannot answer "is this source inside the
    vault", so the eleven ``parse`` implementations could not make this
    decision even if each were edited. It is made instead at the
    assemble-and-write boundary —
    :func:`creek.ingest.base.assemble_ingested_fragment`, the only function
    that turns a :class:`~creek.ingest.base.ParsedFragment` into a writable
    :class:`~creek.models.Fragment`. That function has **three** callers, and
    the population matters more than the sample: wiring only the first left
    ``creek process`` writing absolute paths.

    * :func:`establish_source_identity`, covering every ``run_ingest`` caller
      — ``creek ingest``, ``creek sync``, the ``creek.ingest`` and
      ``creek.upload`` MCP tools, and ``POST /v1/uploads``; **calls this**.
    * :meth:`creek.pipeline.Pipeline._run_ingestion`, for ``creek process``;
      **calls this**.
    * :func:`creek.ingest.discord.write_fragments`, for all three Discord
      ingest paths; **exempt** — ``DiscordIngestor`` emits no
      ``source.original_file``, so there is no host path to redact, and
      stamping one in would hand every fragment from one channel file the same
      non-null key that
      ``creek/generate/voice_authenticity.py::_conversation_key`` reads as a
      sibling-pairing signal.

    A fourth caller added without a call to this function re-opens the
    disclosure, which is why the caller set is asserted from the source by
    ``tests/test_ingest_source_path_privacy.py`` rather than left to a grep.
    Doing it here also leaves
    ``parsed.source_path`` — the *real* path — untouched in memory, which is
    what :func:`legacy_source_key`, :func:`attach_origin_key` and the ledger's
    adoption proof consume. The #1577 tombing behaviour is therefore preserved
    by construction rather than by coincidence.

    **The stored spelling is :func:`derive_source_key`'s**, which is already
    what ``origin_key`` carries for ledgered sources: vault-relative under the
    vault root, ``external/<digest>/<basename>`` outside it. Reusing it adds
    no new class of disclosure and keeps one answer to "what is this source
    called" instead of two that can disagree.

    Args:
        parsed: The unit about to be written, holding the real source path.
        fragment: The assembled fragment, before it is written.
        vault_path: Vault root.
    """
    fragment.source.original_file = derive_source_key(parsed.source_path, vault_path)


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


def _recorded_source_of(fragment_path: Path) -> str:
    """Return the ``source.original_file`` a fragment file records, verbatim.

    Read straight off disk rather than from any in-memory model: the string on
    disk is the one the fragment was minted from, and it is the evidence
    :func:`adopt_legacy_ledger_key` demands before it lets one source inherit
    another's identity.

    Args:
        fragment_path: Path to the fragment file.

    Returns:
        The recorded source string, or ``""`` when the file records none or
        cannot be parsed as a fragment.
    """
    # Deferred import: creek.vault.reader pulls the model stack; keep it out
    # of this module's import time, which creek_mcp pays on every tool call.
    from creek.vault.reader import load_post_or_raise

    try:
        post = load_post_or_raise(fragment_path)
    except (OSError, ValueError):
        return ""
    source = post.metadata.get("source")
    if not isinstance(source, dict):
        return ""
    recorded = source.get("original_file")
    return recorded if isinstance(recorded, str) else ""


def _record_names_this_source(
    writer: VaultWriter,
    record: LedgerRecord,
    parsed: ParsedFragment,
    vault_path: Path,
) -> bool:
    """Return whether *record*'s fragment was minted from *parsed*'s source.

    The proof adoption runs on, and it is **exact, not heuristic**: the
    fragment on disk names its own source verbatim, so the two paths are
    resolved through the same :func:`resolve_recorded_source` the key
    derivation uses and compared. Nothing here guesses, which is what stops a
    second file that merely shares a basename from inheriting the first's
    history.

    The tombstone is consulted as well as the live fragment: a unit that
    vanished and came back is exactly the case whose id must survive, and
    :meth:`~creek.vault.writer.VaultWriter.find_fragment` is scoped to
    ``01-Fragments/`` only.

    Args:
        writer: Vault writer, consulted read-only.
        record: The legacy ledger record being considered for adoption.
        parsed: The unit this run is about to write.
        vault_path: Vault root, used to anchor a relative recorded path.

    Returns:
        ``True`` only when a fragment for ``record.fragment_id`` exists and
        records a source resolving to the same file as *parsed*.
    """
    fragment_path = writer.find_fragment(record.fragment_id) or (
        writer.find_tombed_fragment(record.fragment_id)
    )
    if fragment_path is None:
        return False
    recorded = _recorded_source_of(fragment_path)
    if not recorded:
        return False
    return (
        resolve_recorded_source(recorded, vault_path).resolve()
        == resolve_recorded_source(parsed.source_path, vault_path).resolve()
    )


def adopt_legacy_ledger_key(
    ledger: SourceLedger,
    writer: VaultWriter,
    parsed: ParsedFragment,
    vault_path: Path,
    origin_key: str,
    legacy_key: str,
) -> bool:
    """Re-record a pre-#953 bare-filename ledger entry under its new key.

    #953 re-spells the key of every source that lives outside the vault, and
    ``creek sync``'s ``journal`` pass — source type ``markdown``, an external
    drive path, on by default — has been writing exactly those keys since
    #672. Without this, the first run after the upgrade would miss on the new
    key, take :func:`write_fragment_idempotent`'s ``record is None`` branch,
    mint a fresh id and orphan every journal fragment in the vault at once.

    Adoption is refused unless **all** of these hold, so it can only ever
    preserve an identity and never invent one:

    * the new key is absent from the ledger — a key that already has a record
      is already migrated, and re-adopting would overwrite it;
    * the legacy key is present — otherwise there is no predecessor at all;
    * the recorded fragment still exists and its own ``source.original_file``
      resolves to *this* source (:func:`_record_names_this_source`).

    The stale legacy record is deliberately left in place rather than
    rewritten: the ledger is append-only, and :func:`establish_source_identity`
    keeps that key in the run's seen-set so it can never be read as a vanished
    unit. It stops mattering as soon as the new record exists.

    Cost is one vault read per *miss* on the new key, i.e. once per unit on
    the first post-upgrade run and never again — the pass is self-clearing.

    Args:
        ledger: The resolved ledger for this run.
        writer: Vault writer, consulted read-only.
        parsed: The unit this run is about to write.
        vault_path: Vault root.
        origin_key: The key this run derived, composed with any sub-unit.
        legacy_key: The pre-#953 spelling of the same unit's key.

    Returns:
        ``True`` when a record was adopted under *origin_key*.
    """
    if origin_key in ledger or legacy_key not in ledger:
        return False
    record = ledger.get(legacy_key)
    if record is None or not _record_names_this_source(
        writer, record, parsed, vault_path
    ):
        return False
    ledger.record(
        origin_key,
        record.fragment_id,
        record.content_hash,
        last_seen=record.last_seen,
        tombed=record.tombed,
    )
    return True


def establish_source_identity(
    ledger: SourceLedger | None,
    writer: VaultWriter,
    parsed: ParsedFragment,
    fragment: Fragment,
    vault_path: Path,
) -> set[str]:
    """Stamp the fragment's key, migrate it if needed, and report keys seen.

    Also where :func:`record_source_provenance` runs for every caller of
    :func:`run_ingest` — ``creek ingest``, ``creek sync``, the ``creek.ingest``
    and ``creek.upload`` MCP tools, and ``POST /v1/uploads`` (#1575). It is
    *not* the only place that call is needed:
    :meth:`creek.pipeline.Pipeline._run_ingestion` assembles for
    ``creek process`` without coming through here and makes the call itself.
    :func:`record_source_provenance`'s docstring enumerates the whole caller
    set and says why the third member is exempt; adding a fourth writer
    without a call re-opens the disclosure.

    One call so :func:`run_ingest`'s loop gains no branch — and so the three
    things that must happen together cannot be done apart. Stamping without
    adopting re-mints an out-of-vault corpus; adopting without reporting the
    legacy key **soft-tombs that whole corpus on the same run**, because the
    stale record stays in :meth:`~creek.ingest.ledger.SourceLedger.live_keys`
    and a ``creek sync`` journal pass is a directory ingest with tombing armed.

    The legacy key is reported as seen whether or not anything was adopted.
    The set is only ever *subtracted* from the gone set, so reporting a key no
    record uses costs nothing, while omitting one that a record does use
    relocates a live fragment.

    The imprecision this admits is broader than an earlier version of this
    note claimed, and worth stating exactly (#1577). Because
    :func:`legacy_source_key` derives the legacy key from ``candidate.name``
    — the bare basename — **any** two sources sharing a basename share one
    legacy key. So whenever one of them holds the legacy record and the other
    is present in this pass, the survivor's seen-report shields that record
    from the sweep. That covers the vault-root case named before, but also the
    ordinary one: two out-of-vault directories each holding a ``notes.md``.
    The cost is bounded to a *missed soft-delete* — never a destroyed or
    mis-attributed fragment — and it self-corrects on the first run in which
    the surviving namesake is absent too. Making it exact would mean reading
    every fragment's ``source.original_file`` on every run to prove which file
    a bare-basename record belongs to, which is the vault-wide scan #1367
    declined. Both arms are pinned by
    ``tests/test_ingest_legacy_key_adoption.py``'s parametrised
    shared-vs-distinct basename pair.

    Args:
        ledger: The resolved ledger, or ``None`` for an unledgered run.
        writer: Vault writer, consulted read-only.
        parsed: The unit about to be written.
        fragment: The assembled fragment, before it is written.
        vault_path: Vault root.

    Returns:
        Every ledger key this unit answers for, to be marked seen. Empty for
        an unledgered run, which has no keys and never tombs.
    """
    record_source_provenance(parsed, fragment, vault_path)
    attach_origin_key(ledger, parsed, fragment, vault_path)
    origin_key = fragment.source.origin_key
    if ledger is None or origin_key is None:
        return set()
    legacy = legacy_source_key(parsed.source_path, vault_path)
    if legacy is None:
        return {origin_key}
    legacy_key = compose_source_unit(legacy, parsed.source_unit)
    adopt_legacy_ledger_key(ledger, writer, parsed, vault_path, origin_key, legacy_key)
    return {origin_key, legacy_key}


def ledger_body_hash(body: str) -> str:
    """Return the ledger's content hash for the body the vault will hold (#1393).

    **The ledger's content hash is a hash of the WRITTEN body, never of
    ``ParsedFragment.content``.** Several ingestors leave ``content`` empty or
    partial and build the real body in ``convert_to_markdown``:
    ``SpreadsheetIngestor`` and ``PresentationIngestor`` return
    ``ParsedFragment(content="")`` outright, and the generic and image
    ingestors wrap or prefix theirs. Hashing ``content`` therefore recorded
    ``sha256("")`` for every workbook and every deck, forever — so an edited
    re-upload compared equal to its own predecessor, took the ``unchanged``
    branch, and the operator's edit was silently never written.

    This is deliberately the **only** place in this module that computes a
    ledger content hash. All three call sites — the row that is written
    (:func:`record_in_ledger`), the changed/unchanged compare
    (:func:`write_fragment_idempotent`) and the ``--since``/``--incremental``
    filter (:func:`should_skip_unit`) — route through it, because they must
    agree on the hashed string or the ledger becomes actively harmful. Fixing
    the record site while leaving the incremental filter hashing ``content``
    would make every workbook compare *changed* on every run and rewrite the
    whole corpus; fixing only the filter would leave the edit lost. One
    chokepoint is what stops the three from drifting apart again.

    Note that ``generate_fragment_id`` still hashes ``ParsedFragment.content``
    (``base.py``). That is a different namespace — *identity*, not *change
    detection* — and it is untouched here on purpose: moving it would remint
    every fragment id in every existing vault.

    Args:
        body: The Markdown body that will be written below the frontmatter.

    Returns:
        The SHA-256 hex digest the ledger records for that body.
    """
    # Deferred import: this module keeps ledger imports local (see
    # `ledger_for_source`), and `content_hash` is a staticmethod, so the
    # digest definition still lives in exactly one place.
    from creek.ingest.ledger import SourceLedger

    return SourceLedger.content_hash(body)


def record_in_ledger(
    ledger: SourceLedger | None,
    fragment: Fragment,
    body: str,
) -> None:
    """Record the written fragment in the source ledger (#672).

    Hashes *body* — the bytes the vault now holds — rather than the parsed
    source content, so the recorded digest describes the file that exists
    (#1393). See :func:`ledger_body_hash`.

    Args:
        ledger: The source ledger, or ``None`` for an unledgered run.
        fragment: The fragment that was just written.
        body: The Markdown body written below its frontmatter.
    """
    if ledger is None or fragment.source.origin_key is None:
        return
    ledger.record(
        fragment.source.origin_key,
        fragment.id,
        ledger_body_hash(body),
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
    fragment: Fragment,
    body: str,
    reclassify_threshold: float,
    extra_frontmatter: dict[str, object] | None = None,
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

    The change compare hashes *body* — the bytes about to be written — not
    the parsed source content (#1393). Because ``body`` is also the string
    handed to every ``write_fragment``/``update_fragment`` branch below, the
    hash and the write cannot describe different content.

    Args:
        ledger: The source ledger, or ``None`` for an unledgered run.
        writer: The vault writer.
        fragment: The fragment to write; its ``id`` may be reassigned to the
            ledger's recorded id.
        body: The Markdown body to write below the frontmatter.
        reclassify_threshold: Similarity below which a rewrite flags the
            fragment for re-classification.
        extra_frontmatter: Unmodelled frontmatter keys to write alongside the
            model fields (#1392). Only the ``write_fragment`` branches need
            it; the ``update_fragment`` branches rewrite the body of a file
            whose frontmatter already carries these keys and preserves them.

    Returns:
        ``"created"``, ``"updated"`` or ``"unchanged"``.
    """
    record = ledger_record(ledger, fragment)
    # `record is not None` guarantees `ledger is not None` (ledger_record
    # returns None for a missing ledger). No narrowing of `ledger` is needed
    # below: the hash now comes from `ledger_body_hash(body)`, which is a
    # free function precisely so all three sites share one definition (#1393).
    if record is None:
        writer.write_fragment(fragment, body=body, extra_frontmatter=extra_frontmatter)
        return "created"
    # The ledger is the authority on this unit's identity, on every branch
    # below. Do not push this back down into the branches (#1329).
    fragment.id = record.fragment_id
    new_hash = ledger_body_hash(body)
    if record.tombed:
        restored = restore_tombed(
            writer, fragment, record, body, new_hash, reclassify_threshold
        )
        if restored is None:
            # Tombstone lost out of band: recreate under the preserved id.
            writer.write_fragment(
                fragment, body=body, extra_frontmatter=extra_frontmatter
            )
        return "updated"
    if record.content_hash != new_hash:
        updated = writer.update_fragment(
            fragment, body, reclassify_threshold=reclassify_threshold
        )
        if updated is None:
            # File gone out of band: recreate under the preserved id.
            writer.write_fragment(
                fragment, body=body, extra_frontmatter=extra_frontmatter
            )
        return "updated"
    # Known unit, content unchanged: idempotent no-op. The write resolves to
    # the existing file because `fragment.id` is now the *ledgered* id and the
    # writer's per-directory id index matches on it — the guarantee comes from
    # the ledger, not from trusting the derivation to reproduce (#1329).
    # Reported as unchanged so it never inflates the updated counter.
    writer.write_fragment(fragment, body=body, extra_frontmatter=extra_frontmatter)
    return "unchanged"


def tombing_is_authorised(
    ledger: SourceLedger | None,
    input_path: Path,
    source_type: str,
    ledger_source: str | None,
    *,
    discovery_complete: bool,
    may_tomb: bool,
) -> bool:
    """Return whether this pass may compute a gone set at all (#674/#1329).

    Five independent conjuncts, each closing a different way a pass can lack
    the authority to soft-delete. They are gathered here, rather than left as
    a run of early returns inside :func:`tomb_missing_units`, so the whole
    authority question is answerable by reading one function — and so adding a
    sixth cannot quietly push the sweep over the complexity ceiling and get
    "simplified" back into a disjunction.

    * **Discovery was complete.** Checked first, and first for a reason.
      Tombing is driven by *absence*, and absence means nothing when the walk
      that failed to see a key could not read part of the tree: the key's
      source may be sitting in exactly the part that could not be listed. The
      gone set is not computable, not merely inadvisable (#1444).
    * **A ledger exists.** Nothing to sweep without one.
    * **The input is a directory.** Only a full-source pass enumerates the
      authoritative set; a single-file input never tombs, which is what keeps
      the incremental epic from tombing every other unit when one file is
      re-ingested.
    * **The source type may tomb** — membership of :data:`TOMBING_SOURCES`,
      which is deliberately narrower than :data:`LEDGERED_SOURCES` (#1329).
      Holding a ledger is identity, not deletion authority.
    * **The ledger is this source type's own.** A borrowed ledger
      (``ledger_source``) belongs to units this pass never enumerated, so its
      ``live_keys`` minus this pass's ``seen_keys`` is not a gone set, it is
      simply the other source's whole corpus. No caller does this today; it is
      closed by construction rather than by a comment saying not to.
    * **The caller permits it.** See ``may_tomb`` on :func:`run_ingest`.

    *discovery_complete* and *may_tomb* are keyword-only with **no default**,
    copying the ``warn(..., *, ceiling_safe)`` precedent in
    :func:`run_ingest`. mypy strict covers ``creek/``, so a future caller that
    has not thought about whether its pass had the authority to delete cannot
    compile; a default of ``True`` would be fail-open, which is the defect
    rather than its fix.

    Args:
        ledger: The resolved ledger for this run, or ``None``.
        input_path: The source the operator named, verbatim.
        source_type: Registry key of the ingestor being run.
        ledger_source: The ledger name the caller forced, or ``None``.
        discovery_complete: Whether discovery enumerated the whole source.
        may_tomb: Whether the calling surface permits soft-deletion.

    Returns:
        ``True`` only when every conjunct holds.
    """
    return (
        discovery_complete
        and ledger is not None
        and input_path.is_dir()
        and source_type in TOMBING_SOURCES
        and ledger_source is None
        and may_tomb
    )


def tomb_missing_units(
    ledger: SourceLedger | None,
    writer: VaultWriter,
    input_path: Path,
    seen_keys: set[str],
    errors: list[str],
    source_type: str,
    ledger_source: str | None,
    *,
    discovery_complete: bool,
    may_tomb: bool,
) -> int:
    """Soft-tomb ledgered units absent from a full-source pass (#674).

    Whether this pass is allowed to compute a gone set at all is
    :func:`tombing_is_authorised`; every guard and its reason lives there.
    This function is only the sweep.

    **Why an incomplete pass only warns and still exits 0.** ``creek.cli``'s
    ``_warn_if_discovered_but_empty`` returns early on ``written > 0 or
    discovered == 0``, so it structurally cannot catch this shape, and
    widening it would change behaviour for unrelated sources. Routing a new
    exit code out of ``_run_ingest`` would mean widening the public
    :class:`IngestRunResult` dataclass, which ``creek_mcp`` reads. A
    ``--strict`` flag is therefore left for its own issue; nothing is
    destroyed either way, which is the property that had to hold.

    A tomb that fails on I/O is collected into *errors* and the unit is left
    live in the ledger so the next full-source pass retries it, rather than
    crashing the whole run. A tomb that finds nothing to move is treated the
    same way (#1332) — see :func:`_tomb_one_unit`.

    Args:
        ledger: The resolved ledger for this run, or ``None``.
        writer: Vault writer performing the relocations.
        input_path: The source the operator named, verbatim.
        seen_keys: Every ledger key this pass answered for, including the
            pre-#953 spelling of an out-of-vault unit's key (#953). A key
            missing from this set is read as a deleted source.
        errors: Collector for ``[<source_type>] …`` operator messages.
        source_type: Registry key of the ingestor being run.
        ledger_source: The ledger name the caller forced, or ``None``.
        discovery_complete: Whether discovery enumerated the whole source.
        may_tomb: Whether the calling surface permits soft-deletion.

    Returns:
        The number of fragments this call actually relocated into
        ``10-Liminal/Orphaned/``. Not the number of units considered, and
        not the number of ledger records written: a unit whose fragment was
        already tombed has its record reconciled but moved nothing, so it
        does not count. The figure is printed to the operator verbatim, so
        it may only ever name work that happened.
    """
    authorised = tombing_is_authorised(
        ledger,
        input_path,
        source_type,
        ledger_source,
        discovery_complete=discovery_complete,
        may_tomb=may_tomb,
    )
    # ``authorised`` already established that the ledger is not ``None``; the
    # explicit clause is what narrows the type for mypy, not a second gate.
    if not authorised or ledger is None:
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
    body: str,
    since: datetime | None,
) -> bool:
    """Return whether incremental mode should skip this unchanged unit (#677).

    The third of the three ledger-hash sites, and the one it is worst to
    miss: this filter compares against the hash :func:`record_in_ledger`
    wrote, so if it hashes a different string than that function did, no
    unit ever compares equal and ``creek ingest --incremental`` rewrites the
    entire corpus on every run. It hashes *body* for that reason (#1393).

    Args:
        filtering: Whether an incremental mode is active at all.
        ledger: The source ledger, or ``None`` for an unledgered run.
        parsed: The parsed unit; read only for its ``timestamp``, which is
            the cursor the ``--since`` mode compares.
        fragment: The assembled fragment, used to find the prior record.
        body: The Markdown body this run would write.
        since: Explicit ``--since`` cutoff, or ``None`` for ledger-driven
            ``--incremental``.

    Returns:
        ``True`` to skip the unit as unchanged.
    """
    if not filtering:
        return False
    # Deferred import: creek.pipeline pulls heavy stage deps; keep import light.
    from creek.pipeline import unit_is_changed

    record = ledger_record(ledger, fragment)
    # Hashed unconditionally: `unit_is_changed` consults the hash only when
    # *record* is not None, which cannot happen without a ledger, so the old
    # `if ledger is not None else ""` guard chose between two values that
    # were never read. Computing it plainly keeps this site textually
    # identical to the other two.
    return not unit_is_changed(parsed.timestamp, ledger_body_hash(body), record, since)


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


_PARTIAL_PIN_TEMPLATE = (
    "This vault has {count} markdown source(s) that `creek ingest "
    "--pin-source-ids` refused to pin, because more than one live fragment "
    "claims each one. Their fragment ids are still unpinned, so re-ingesting "
    "those sources mints new ids and leaves the existing fragments behind as "
    "duplicates: {sample}. Run `creek clean duplicates`, resolve them, then "
    "re-run `creek ingest --pin-source-ids`."
)
"""Advisory for a vault the pin migration could only partly complete (#1367).

The console rendering. ``{sample}`` interpolates real vault-relative source
paths, which is why it must not leave this process for a caller whose ceiling
has not admitted them — see :data:`_PARTIAL_PIN_SAFE_TEMPLATE`.
"""

_PARTIAL_PIN_SAFE_TEMPLATE = (
    "This vault has {count} markdown source(s) that `creek ingest "
    "--pin-source-ids` refused to pin, because more than one live fragment "
    "claims each one. Their fragment ids are still unpinned, so re-ingesting "
    "those sources mints new ids and leaves the existing fragments behind as "
    "duplicates. Run the same ingest at a terminal to see which sources. Run "
    "`creek clean duplicates`, resolve them, then re-run `creek ingest "
    "--pin-source-ids`."
)
"""The content-free twin of :data:`_PARTIAL_PIN_TEMPLATE` (#1372).

Interpolates ``{count}`` and nothing else. The count stays because an advisory
that withholds the number as well as the paths reports no problem at all; what
is withheld is the ``{sample}`` clause, replaced by the instruction that
recovers it through a surface where the operator is entitled to see every path
in their own vault.
"""

_PARTIAL_PIN_SAMPLE = 3
"""How many contested source keys the advisory names before it stops listing."""


def partially_pinned_warning(
    source_type: str,
    vault_path: Path,
) -> Advisory | None:
    """Return the partially-pinned-vault advisory, or ``None`` (#1367).

    :func:`unpinned_vault_warning` derives its whole signal from the markdown
    ledger being *empty*, which makes it blind to the migration's most likely
    real outcome: the clean sources get pinned, the duplicated ones are
    refused, and the now-non-empty ledger silences the advisory forever —
    about exactly the fragments still at risk of being re-minted.

    So this asks a different question of different state. The refusals are
    persisted by :func:`creek.ingest.pin_ids.pin_source_ids` and read back
    here, which is what lets a finding computed in one process be reported by
    the next one. Reading a recorded list is also what keeps this off the hot
    path: no vault walk, no frontmatter parsing, and a cost that does not grow
    with the size of the vault.

    Self-clearing in both directions. The migration rewrites the state in full
    and removes it when nothing is outstanding, so resolving the duplicates
    and re-pinning restores silence — and an advisory that cannot go quiet is
    one operators learn to ignore.

    Gated on *source_type* for the same reason :func:`unpinned_vault_warning`
    is: the migration and its refusals concern markdown identity only, and an
    advisory delivered on a run it does not apply to is trained-away noise.

    Args:
        source_type: Registry key of the ingestor being run, verbatim.
        vault_path: Vault root.

    Returns:
        The advisory in both disclosure forms, or ``None`` when this run
        writes under another source type's identity or the vault has no
        recorded refusals.
    """
    if source_type != LEDGERED_SOURCE_TYPE:
        return None
    from creek.ingest.ledger import load_unpinned_sources

    contested = load_unpinned_sources(vault_path)
    if not contested:
        return None
    sample = ", ".join(contested[:_PARTIAL_PIN_SAMPLE])
    if len(contested) > _PARTIAL_PIN_SAMPLE:
        sample = f"{sample}, …"
    count = len(contested)
    return Advisory(
        message=_PARTIAL_PIN_TEMPLATE.format(count=count, sample=sample),
        ceiling_safe=_PARTIAL_PIN_SAFE_TEMPLATE.format(count=count),
    )


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
        # NOT a ledger hash — do not sweep this to the rendered body (#1393).
        # This deliberately reproduces the PRE-#1305 legacy id, which hashed
        # `parsed.content`, in order to find fragments minted under the old
        # rule. Feed it anything else and it computes an id no vault ever
        # held, the advisory matches nothing, and it goes permanently silent.
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
    ocr_disabled: bool = False,
) -> list[Advisory]:
    """Collect every advisory that must be raised BEFORE the first write.

    All three of these are checked before :func:`run_ingest`'s write loop,
    and each for its own reason:

    * **OCR switched off.** Raised first, because it explains the emptiness
      every later advisory would otherwise be read as evidence of. An image
      pass under ``ocr.enabled: false`` writes nothing by design, and a
      design that says nothing is indistinguishable from a bug (#1517).
    * **Incomplete discovery.** The run destroyed nothing on partial
      evidence, and a run that quietly writes little or nothing is
      indistinguishable from a run with nothing to do (#1444).
    * **Un-pinned vault.** Once the loop has recorded its first ledger entry
      the vault no longer *looks* un-pinned, so an advisory raised afterwards
      would never fire for the very run it needed to warn about (#1329).
    * **Partially pinned vault.** Same reason, one step later in the
      migration: this run is about to mint new ids for sources the migration
      refused to pin, so saying so afterwards would be a post-mortem (#1367).
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
        ocr_disabled: Whether :func:`ocr_is_disabled` refused to run this
            pass. Passed in rather than re-derived, because this function
            holds neither the ingestor class nor the config.

    Returns:
        The advisories that apply, in the order they should be delivered.
        Empty when the run has nothing to say.
    """
    advisories: list[Advisory] = []
    if ocr_disabled:
        advisories.append(
            Advisory(
                message=_OCR_DISABLED_ADVISORY,
                ceiling_safe=_OCR_DISABLED_ADVISORY,
            ),
        )
    incomplete = incomplete_discovery_advisory(ingest_result, input_path)
    if incomplete is not None:
        advisories.append(incomplete)
    unpinned = unpinned_vault_warning(source_type, vault_path)
    if unpinned is not None:
        # It travels verbatim: the text is a fixed constant with no
        # interpolation, so it names a command rather than a fragment, and
        # withholding it from an MCP caller would be caution with no subject.
        advisories.append(Advisory(message=unpinned, ceiling_safe=unpinned))
    partial = partially_pinned_warning(source_type, vault_path)
    if partial is not None:
        advisories.append(partial)
    collapsed = collapsed_unit_warning(writer, ingest_result.fragments)
    if collapsed is not None:
        advisories.append(collapsed)
    return advisories


_OCR_DISABLED_ADVISORY: Final[str] = (
    "OCR is off (`ocr.enabled: false` in the vault config), so this image "
    "pass read no images and wrote no fragments. Set `ocr.enabled: true` in "
    "<vault>/00-Creek-Meta/creek_config.yaml to ingest images."
)
"""Told to the operator when a disabled OCR pass produces nothing.

A run that silently ingests nothing looks exactly like a run with nothing to
do. Honouring ``ocr.enabled`` without saying so would trade the #1517 defect
(a key that does nothing) for its mirror image: a key that quietly eats the
operator's input. The text is a fixed constant with no interpolation, so it
names a config key rather than a fragment and travels verbatim across an MCP
tier ceiling.
"""


def build_ingestor(
    ingestor_cls: type[Ingestor],
    *,
    ocr: OCRConfig | None = None,
) -> Ingestor:
    """Construct one ingestor, handing it only the config it can use.

    Both production construction sites are generic: they hold an
    ``Ingestor`` subclass out of :data:`~creek.ingest.INGESTOR_REGISTRY` and
    called it with no arguments. That genericity is worth keeping — adding an
    ``ocr`` parameter to :class:`~creek.ingest.base.Ingestor` itself would
    put an OCR concern on eleven classes, ten of which have no pixels to
    read. So the narrow knowledge lives here instead, following the shape
    already set by :class:`~creek.ingest.chatgpt.ChatGPTIngestor`'s
    ``chatbot_filter`` and :class:`~creek.ingest.discord.DiscordIngestor`'s
    ``discord_filter_config``: a per-ingestor keyword, supplied by the
    caller that has it, and every other ingestor still constructed zero-arg.

    Args:
        ingestor_cls: Concrete ingestor class to construct.
        ocr: The vault's ``ocr`` block, when the caller has one. ``None``
            leaves every ingestor — image included — on its own defaults,
            which is what an API caller with no vault config gets.

    Returns:
        A ready ingestor.

    Raises:
        UnknownOcrEngineError: When *ingestor_cls* reads images and
            ``ocr.engine`` names no known backend.
    """
    if ocr is not None and issubclass(ingestor_cls, ImageIngestor):
        return ImageIngestor.from_ocr_config(ocr)
    return ingestor_cls()


def ocr_is_disabled(ingestor_cls: type[Ingestor], ocr: OCRConfig | None) -> bool:
    """Return whether this run must skip OCR entirely.

    Both halves matter. ``ocr.enabled`` is only meaningful for an ingestor
    whose work *is* OCR, so the class is checked too — otherwise a vault that
    turned OCR off would stop ingesting its markdown as well, which is the
    kind of blast radius a boolean should never have.

    Args:
        ingestor_cls: Concrete ingestor class about to run.
        ocr: The vault's ``ocr`` block, or ``None`` when the caller has none.

    Returns:
        ``True`` when *ingestor_cls* reads images and OCR is switched off.
    """
    return (
        ocr is not None and not ocr.enabled and issubclass(ingestor_cls, ImageIngestor)
    )


def run_ingestor(
    ingestor_cls: type[Ingestor],
    source_path: Path,
    *,
    ocr: OCRConfig | None = None,
) -> IngestResult:
    """Run one ingestor under the vault's config, or decline to run it.

    The single chokepoint both ``creek ingest`` (via :func:`run_ingest`) and
    ``creek process`` (via :meth:`creek.pipeline.Pipeline._run_ingestion`)
    go through, so ``ocr.enabled`` cannot be honoured on one surface and
    ignored on the other — which is precisely how #1517's sibling defects
    survive a partial fix.

    A disabled OCR pass returns an empty result **without constructing an
    engine**, so a vault that has switched OCR off never needs a working
    ``tesseract`` binary, and never resolves ``ocr.engine`` at all.

    The empty result keeps ``discovery_complete`` at its ``True`` default and
    reports ``discovered == 0``, which together say "this pass enumerated
    nothing and claims nothing about what exists". That cannot arm the orphan
    sweep: :func:`tombing_is_authorised` additionally requires the source type
    to be in :data:`TOMBING_SOURCES`, and ``image`` is deliberately absent
    from it — while this function short-circuits for no class other than
    :class:`~creek.ingest.images.ImageIngestor`, so no tombable source type
    can reach this branch.

    Args:
        ingestor_cls: Concrete ingestor class to run.
        source_path: Source directory or file to hand it.
        ocr: The vault's ``ocr`` block, when the caller has one.

    Returns:
        The ingestor's :class:`~creek.ingest.base.IngestResult`, or an empty
        one when OCR is switched off.
    """
    if ocr_is_disabled(ingestor_cls, ocr):
        logger.info(
            "OCR disabled by config (ocr.enabled: false); skipping %s.",
            ingestor_cls.__name__,
        )
        return IngestResult()
    return build_ingestor(ingestor_cls, ocr=ocr).ingest(source_path)


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
    may_tomb: bool = True,
    ocr: OCRConfig | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> IngestRunResult:
    """Run one ingestor and persist its output idempotently to the vault.

    UI-agnostic core of ``creek ingest``: no printing, no typer.Exit. A missing
    vault raises :class:`FileNotFoundError` (from :class:`VaultWriter`) for the
    caller to translate.

    Args:
        ingestor_cls: Concrete :class:`Ingestor` subclass to run.
        source_type: Registry key, used to prefix error messages and to select
            the ledger (see :data:`LEDGERED_SOURCES`).
        input_path: Source directory or file to ingest.
        vault_path: Vault root for :class:`VaultWriter`.
        reclassify_threshold: Body-similarity floor below which a materially
            edited unit is flagged for re-classification (#675).
        since: Incremental cutoff (#677) — only units newer than this are kept.
        incremental: Ledger-driven incremental mode (#677).
        ledger_source: Force a specific ledger name instead of the one
            :func:`ledger_for_source` would pick, opting an unledgered source
            type into ledger-backed identity (#1023). A borrowed ledger can
            never be swept: :func:`tombing_is_authorised` requires
            ``ledger_source is None``, because the units in someone else's
            ledger are precisely the units this pass did not enumerate. (An
            earlier version of this note claimed the opposite — that a
            ``ledger_source`` plus a directory input *armed* the sweep. #1329
            had already moved that gate onto the ingestor's registry key, and
            this parameter now subtracts authority rather than granting it.)
        privacy_tier: Tier the caller declares for this content (#1023).
            Merged onto each fragment with
            :func:`creek.classify.privacy_pass.escalate`, so it can only
            raise the tier, never lower one the source already declared.
        may_tomb: Whether this surface permits soft-deletion at all. A
            restrictive conjunct: it can only ever *subtract* from the
            authority :func:`tombing_is_authorised` already computes, never
            add to it, so passing ``True`` grants nothing on its own.
            ``creek_mcp.tools.ingest`` passes ``False`` — a remote caller has
            never held vault-mutating deletion authority on that surface and
            must not acquire it as a side effect of the tool gaining a ledger
            (#1467). It carries a default, unlike the keyword-only no-default
            arguments it feeds, purely for compatibility: a required argument
            here would mean editing every existing call site, and every one of
            those surfaces (the CLI, ``creek.upload``, ``creek.drive``) keeps
            exactly its present behaviour under ``True``.
        ocr: The vault's ``ocr`` block (#1517). An explicit parameter rather
            than a ``load_vault_config`` call inside this function: the CLI
            has already loaded a config by the time it gets here and already
            forwards config-derived values down this same call
            (``reclassify_threshold``), so a second load would be a second
            source of truth for the same file. ``None`` — the default every
            existing caller keeps — leaves the ingestor on its own defaults,
            which is exactly the behaviour those callers had before.
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

    ingest_result = run_ingestor(ingestor_cls, input_path, ocr=ocr)
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
        ocr_disabled=ocr_is_disabled(ingestor_cls, ocr),
    ):
        warn(advisory.message, ceiling_safe=advisory.ceiling_safe)

    errors: list[str] = [f"[{source_type}] {err}" for err in ingest_result.errors]
    fragment_ids: list[str] = []
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
        # Record every key as seen BEFORE any incremental skip, so an
        # unchanged unit is never mistaken for a deleted one and tombed
        # (#674/#677) — and so a unit's pre-#953 key is never read as one
        # either (#953).
        seen_keys |= establish_source_identity(
            ledger, writer, parsed, assembled.fragment, vault_path
        )
        if should_skip_unit(
            filtering=filtering,
            ledger=ledger,
            parsed=parsed,
            fragment=assembled.fragment,
            body=assembled.body,
            since=since,
        ):
            skipped += 1
            continue
        try:
            action = write_fragment_idempotent(
                ledger,
                writer,
                assembled.fragment,
                assembled.body,
                reclassify_threshold,
                assembled.extra_frontmatter,
            )
        except (OSError, KeyError) as exc:
            errors.append(
                f"[{source_type}] failed to write {assembled.fragment.id}: {exc}",
            )
            continue
        record_in_ledger(ledger, assembled.fragment, assembled.body)
        written += 1
        # Read AFTER ``write_fragment_idempotent``, never before: that call
        # reassigns ``fragment.id`` to the ledger's recorded id on every
        # branch, so an id captured earlier would name the id this run
        # derived rather than the one the vault now holds (#1329).
        fragment_ids.append(assembled.fragment.id)
        counts[action] = counts.get(action, 0) + 1

    tombed = tomb_missing_units(
        ledger,
        writer,
        input_path,
        seen_keys,
        errors,
        source_type,
        ledger_source,
        discovery_complete=ingest_result.discovery_complete,
        may_tomb=may_tomb,
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
        fragment_ids=fragment_ids,
        warnings=warnings,
        ceiling_safe_warnings=ceiling_safe_warnings,
    )
