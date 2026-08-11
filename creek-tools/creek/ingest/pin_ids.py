"""Pin existing markdown fragment ids to their source units (#1329 migration).

``MarkdownIngestor`` and ``DocumentIngestor`` used to derive a fragment's
timestamp from a timezone-**naive** ``datetime.fromtimestamp(mtime)``.
:func:`~creek.ingest.base.generate_fragment_id` hashes that timestamp, so the
*same* file minted a *different* ``frag-…`` id depending on the host ``TZ``
environment variable. The fix replaces both fallbacks with
:func:`~creek.ingest.base.file_modified_time`, which is a pure function of the
epoch float and therefore host-independent — but it also **changes id
derivation**. Left alone, the next ``creek ingest`` over an existing vault
would fail to recognise every markdown fragment already on disk, mint a new id
for it, and leave the original behind as an orphaned duplicate.

The answer here is deliberately *not* to re-mint ids. It is to **pin** them:
back-fill the ingest ledger so each existing fragment's **existing** id is
recorded against its ``source_key``. From then on
``write_fragment_idempotent`` reuses ``record.fragment_id`` and no id ever
moves — whatever the new derivation would have produced is simply never
consulted for a unit the ledger already knows.

What this migration touches, and what it refuses to touch:

* **Ledger** — one appended :class:`~creek.ingest.ledger.LedgerRecord` per
  markdown-sourced fragment, carrying the id read off disk. Never a
  recomputed id.
* **Frontmatter** — exactly one added key, ``source.origin_key``. The ``id``,
  the ``created`` timestamp, the body and the filename are preserved
  byte-for-byte. Nothing is renamed and nothing is re-minted.
* **Duplicated vaults** — a ``source_key`` claimed by more than one live
  fragment is pinned for *neither*. Choosing arbitrarily would bless the wrong
  fragment forever, and pinning both is not expressible in a
  one-record-per-key ledger. The operator is pointed at ``creek clean
  duplicates`` instead. This conflict guard is the migration's primary safety
  property.

Only the markdown source is in scope: it is the one ledger-wired source
(:func:`creek.ingest.pipeline.ledger_for_source`), and append-only event
sources (Discord/chat exports) keep their content-hashed ids and never consult
the ledger at all.

The run is idempotent — a ``source_key`` already present in the ledger is not
re-pinned — and ``dry_run=True`` writes nothing while still returning a fully
populated :class:`PinResult` so a caller can print the plan.

It is also **resumable**. Pinning one fragment is two durable writes that
cannot be made atomic with respect to each other, so an interrupted run (an
``OSError`` partway through an N-fragment pass, not just a ``kill -9``) can
leave a fragment with a ledger record but no stamp. :func:`_pin_one` detects
and repairs exactly that state rather than skipping the candidate because its
key is already present — see its docstring for why the record is nonetheless
written first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path  # runtime use: suffix/existence checks on source paths

import frontmatter

from creek.ingest.base import generate_fragment_id
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import LEDGERED_SOURCE_TYPE, derive_source_key
from creek.vault.authors import OTHER_AUTHORS_DIR
from creek.vault.reader import iter_vault_fragments

# Importing these three from the writer rather than retyping them is
# deliberate. ``_atomic_write_text`` is the vault's one crash-safe rewrite
# (tempfile + ``os.replace``); the two relpart constants are the writer's
# single source of truth for where fragments live, kept that way so the
# scaffold drift guard can *derive* the directories ``creek init`` ships
# (#1025). Re-typing a destination is how the scaffold and the writer drifted
# apart in the first place. ``creek.ingest.refresh`` sets the precedent for
# reaching across a module boundary for a private helper.
from creek.vault.writer import (
    _FRAGMENTS_RELPART,
    _ORPHANED_RELPARTS,
    _atomic_write_text,
)

logger = logging.getLogger(__name__)

_LEDGER_SOURCE: str = LEDGERED_SOURCE_TYPE
"""Ledger name to back-fill.

Imported rather than retyped: this has to be the same ledger
:func:`creek.ingest.pipeline.ledger_for_source` loads and
:func:`creek.ingest.pipeline.unpinned_vault_warning` judges, or the records
written here land in a file no ingest ever reads and the advisory that sends
operators to this migration never notices it ran.
"""

_MARKDOWN_SUFFIX: str = ".md"
"""The only source extension this migration pins.

``MarkdownIngestor.discover`` globs ``*.md`` exclusively, so a fragment whose
``original_file`` carries any other extension was produced by a different
ingestor and does not belong in the markdown ledger.
"""

_FRAGMENT_ROOTS: tuple[tuple[str, ...], ...] = (
    (_FRAGMENTS_RELPART,),
    (OTHER_AUTHORS_DIR,),
    _ORPHANED_RELPARTS,
)
"""Vault-relative roots holding fragment files, as ``joinpath`` part tuples.

Borrowed content under ``11-Other-Authors/`` and soft-tombed content under
``10-Liminal/Orphaned/`` are as ledger-eligible as a native fragment: a tombed
unit whose source re-appears is *restored* under its preserved id, which only
works if that id is pinned.
"""


@dataclass(frozen=True)
class PinResult:
    """Summary of a :func:`pin_source_ids` run.

    Attributes:
        pinned: Fragments whose existing id was newly recorded against its
            ``source_key`` (the count a ``dry_run`` *would* write).
        already_pinned: Fragments whose ``source_key`` was already in the
            ledger and were therefore not re-pinned.
        repaired: Of *already_pinned*, how many were half-pinned by an earlier
            interrupted run — ledger record present, ``source.origin_key``
            missing — and had the missing stamp restored by this one. A
            non-zero count means a previous run did not complete; it is not an
            error, but it is worth an operator's attention.
        conflicts: One human-readable line per ``source_key`` claimed by more
            than one live fragment, naming every claimant's path. None of
            those fragments were pinned.
        unpinnable: One human-readable line per fragment that could not be
            resolved to a live markdown source, naming the fragment path and
            the reason.
        reproduced: Advisory diagnostic — how many candidates re-derive their
            own id from their own stored frontmatter and body. See
            :func:`_reproduces_own_id`; this gates nothing.
        examined: Fragment files inspected, i.e. candidates (pinnable or
            conflicted) plus ``unpinnable``.
    """

    pinned: int
    already_pinned: int
    repaired: int
    conflicts: list[str]
    unpinnable: list[str]
    reproduced: int
    examined: int


@dataclass(frozen=True)
class _Candidate:
    """One on-disk fragment resolved to the source unit that produced it.

    Attributes:
        md_file: Path to the fragment file in the vault.
        fragment_id: The id as read from the fragment's frontmatter — the
            value that gets pinned, never a recomputed one.
        source_path: ``source.original_file`` verbatim; this exact string was
            hashed into ``fragment_id`` at first ingest.
        source_key: Ledger key derived from *source_path*.
        body: The fragment's stored markdown body (``post.content``).
        created: ``created`` as it appears in the raw frontmatter, or ``None``
            when absent/unparseable. Only used by the advisory diagnostic.
        has_origin_key: Whether ``source.origin_key`` is already present on
            disk. Drives the torn-pin repair in :func:`_pin_one`.
    """

    md_file: Path
    fragment_id: str
    source_path: str
    source_key: str
    body: str
    created: datetime | None
    has_origin_key: bool


def _raw_created(metadata: dict[str, object]) -> datetime | None:
    """Read ``created`` from *metadata* exactly as the file records it.

    The raw frontmatter value is used rather than ``Fragment.created``:
    :meth:`creek.models.Fragment._normalise_timestamp` anchors a naive
    timestamp to America/Los_Angeles at validation time, so the model's value
    is not necessarily the one that was hashed into the id.

    YAML delivers the key either already parsed into a :class:`datetime` (bare
    scalar) or as a string (the usual case — the writer serialises the
    timestamp with ``model_dump(mode="json")``, and PyYAML quotes a string
    that would otherwise implicitly resolve to a timestamp).

    Args:
        metadata: The fragment's raw frontmatter mapping.

    Returns:
        The parsed timestamp, or ``None`` when the key is missing, is a plain
        date, or does not parse as ISO-8601.
    """
    value = metadata.get("created")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.debug("Unparseable 'created' frontmatter value: %r", value)
            return None
    return None


def _unpinnable_reason(source_path_str: str) -> str | None:
    """Return why *source_path_str* cannot be pinned, or ``None`` if it can.

    Three dispositions are refused, each for its own reason:

    * an empty ``original_file`` gives nothing to key a ledger record on;
    * a non-``.md`` source came from a different ingestor and does not belong
      in the markdown ledger;
    * a source that no longer exists on disk cannot be re-ingested, so pinning
      it would only add a record no future run will ever match — and, worse,
      could shadow a *different* file that later takes that path.

    Args:
        source_path_str: The fragment's ``source.original_file``, verbatim.

    Returns:
        A human-readable reason, or ``None`` when the source is pinnable.
    """
    if not source_path_str:
        return "fragment records no source.original_file"
    source_path = Path(source_path_str)
    if source_path.suffix.lower() != _MARKDOWN_SUFFIX:
        return f"source {source_path_str!r} is not a {_MARKDOWN_SUFFIX} file"
    if not source_path.exists():
        return f"source {source_path_str!r} no longer exists on disk"
    return None


def _collect_candidates(vault_path: Path) -> tuple[list[_Candidate], list[str]]:
    """Walk the vault's fragment roots and resolve each fragment to its source.

    Args:
        vault_path: Vault root.

    Returns:
        ``(candidates, unpinnable)`` — the fragments that resolve to a live
        markdown source, and one ``"<path>: <reason>"`` line for each that
        does not.
    """
    candidates: list[_Candidate] = []
    unpinnable: list[str] = []
    for relparts in _FRAGMENT_ROOTS:
        for md_file, fragment, body, raw in iter_vault_fragments(
            vault_path.joinpath(*relparts),
        ):
            source_path_str = fragment.source.original_file or ""
            reason = _unpinnable_reason(source_path_str)
            if reason is not None:
                unpinnable.append(f"{md_file}: {reason}")
                continue
            candidates.append(
                _Candidate(
                    md_file=md_file,
                    fragment_id=fragment.id,
                    source_path=source_path_str,
                    source_key=derive_source_key(source_path_str, vault_path),
                    body=body,
                    created=_raw_created(raw),
                    has_origin_key=bool(fragment.source.origin_key),
                ),
            )
    return candidates, unpinnable


def _index_by_source_key(
    candidates: list[_Candidate],
) -> dict[str, list[_Candidate]]:
    """Group *candidates* by ``source_key``, preserving discovery order.

    Built in full **before** anything is pinned: a key with more than one
    claimant is the already-duplicated-vault case, and it can only be detected
    by looking at the whole population first.

    Args:
        candidates: Every fragment that resolved to a live markdown source.

    Returns:
        Mapping of ``source_key`` to the fragments claiming it.
    """
    index: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        index.setdefault(candidate.source_key, []).append(candidate)
    return index


def _conflict_line(source_key: str, claimants: list[_Candidate]) -> str:
    """Render the operator-facing message for a contested ``source_key``.

    Args:
        source_key: The contested ledger key.
        claimants: The two-or-more fragments claiming it.

    Returns:
        A single line naming every claimant's path and the remedy.
    """
    paths = ", ".join(str(candidate.md_file) for candidate in claimants)
    return (
        f"{source_key}: claimed by {len(claimants)} fragments ({paths}); "
        "pinned none — run `creek clean duplicates`, resolve them, then re-run."
    )


def _reproduces_own_id(candidate: _Candidate) -> bool:
    """Return whether *candidate* re-derives its own id from its own contents.

    Purely a diagnostic. It is **not** a gate, and pinning must never be made
    conditional on it: the id being pinned was minted by a build whose
    timestamp handling this issue exists to correct, and by an ingest whose
    ``created`` may have come from the source's own frontmatter rather than
    from ``parsed.timestamp`` at all. A migration gated on reproduction would
    match a fraction of the vault and therefore *do nothing* for the rest —
    which is precisely the population that gets orphaned.

    The real safety properties live elsewhere: the conflict guard in
    :func:`_index_by_source_key` / :func:`pin_source_ids`, and the
    source-exists check in :func:`_unpinnable_reason`. This number is reported
    so an operator can see how much of the vault independently corroborates
    its own pinning, and nothing more.

    Args:
        candidate: The fragment to check.

    Returns:
        ``True`` when the recomputed id equals the id on disk.
    """
    if candidate.created is None:
        return False
    recomputed = generate_fragment_id(
        candidate.source_path,
        candidate.created,
        candidate.body,
    )
    return recomputed == candidate.fragment_id


def _stored_body_hash(body: str) -> str:
    """Hash the fragment's **stored** body for the ledger's ``content_hash``.

    Hashing the vault-side body rather than re-reading the current source file
    is load-bearing twice over:

    * It is faithful. ``MarkdownIngestor.convert_to_markdown`` is the identity
      function and the body is written verbatim below the frontmatter, so the
      stored body *is* the parsed source content — the same string a fresh
      ingest hashes.
    * It keeps a post-ingest edit visible. If the source was edited after its
      original ingest, recording the hash of the **current** source would file
      the new hash against the **old** stored body. The next ``creek ingest``
      would compute that same hash, take the unchanged branch, and the edit
      would be silently swallowed — permanently, since nothing ever revisits
      an unchanged unit. Recording the stored body's hash instead makes that
      run correctly report ``updated`` and apply the edit in place, under the
      pinned id.

    Args:
        body: The fragment's stored markdown body (``post.content``).

    Returns:
        The ledger's SHA-256 hex digest of *body*.
    """
    return SourceLedger.content_hash(body)


def _stamp_origin_key(md_file: Path, source_key: str) -> None:
    """Add ``source.origin_key`` to *md_file*'s frontmatter, atomically.

    This stamp is mandatory, not cosmetic. The RTBF purge sweep resolves a
    fragment's purge target from its **on-disk** frontmatter via
    :func:`creek.purge.engine._extract_source_origin_key` and skips any
    fragment without the key; and ``VaultWriter.update_fragment`` reloads and
    *preserves* the on-disk frontmatter rather than merging fresh ``source``
    fields into it (see :mod:`creek.vault.writer`). So a fragment that lacks
    ``origin_key`` at migration time never gains one on any later ingest —
    omitting the stamp here would permanently strand RTBF coverage for exactly
    the population this migration exists to protect.

    The write goes through the vault's atomic helper rather than
    ``Path.write_text`` (which is what
    :func:`creek.ingest.refresh._rewrite_frontmatter` uses): this migration
    rewrites N live fragment files in one pass, and a plain write truncates
    its target before repopulating it, so a kill mid-run would destroy a
    fragment's content outright.

    Only the one key is added — ``id``, ``created``, every other frontmatter
    field, and the body are round-tripped unchanged. The rendered text carries
    no trailing newline beyond what ``frontmatter.dumps`` emits, matching
    :meth:`~creek.vault.writer.VaultWriter.update_fragment`, the vault's own
    in-place fragment rewrite.

    Args:
        md_file: The fragment file to stamp.
        source_key: The ledger key to record on ``source.origin_key``.
    """
    post = frontmatter.load(str(md_file))
    existing = post.metadata.get("source")
    # A validated fragment always carries a mapping here; the guard keeps the
    # helper total for a hand-edited file rather than raising mid-migration.
    source: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    source["origin_key"] = source_key
    post.metadata["source"] = source
    _atomic_write_text(md_file, frontmatter.dumps(post))


def _pin_one(
    candidate: _Candidate,
    ledger: SourceLedger,
    *,
    dry_run: bool,
) -> tuple[bool, bool]:
    """Pin one uncontested *candidate*, or repair a previously torn pin.

    Pinning is two durable writes — a ledger record and a frontmatter stamp —
    and they cannot be made atomic with respect to each other. Whichever runs
    second can fail on its own (a disk-full or permission-denied
    ``OSError`` partway through an N-fragment pass is far more likely than a
    ``kill -9``), leaving the fragment half-pinned.

    The ledger record is written **first**, deliberately. It is the write that
    protects *identity*: while a record exists, ``write_fragment_idempotent``
    reuses ``record.fragment_id`` and the fragment cannot be duplicated by a
    re-ingest, whether or not the stamp landed. Writing the stamp first would
    invert that, leaving a window in which the fragment is RTBF-visible but
    its identity is unpinned — trading a recoverable gap for an unrecoverable
    one, since a duplicate cannot be un-minted.

    That ordering is only safe because the already-ledgered path below is
    **self-healing**: a fragment whose record exists but whose stamp is
    missing gets the stamp on the next run. Without that, the presence of the
    record would make the retry skip the candidate entirely and the fragment
    would stay RTBF-invisible forever — the purge sweep resolves its target
    from on-disk frontmatter and skips fragments lacking the key, and
    ``update_fragment`` never merges fresh ``source`` fields, so nothing
    downstream would ever repair it.

    Args:
        candidate: The fragment to pin.
        ledger: The markdown ledger, already loaded.
        dry_run: When ``True``, decide but write nothing.

    Returns:
        ``(pinned, repaired)``. *pinned* is ``True`` when this call pinned the
        candidate (or would have, under *dry_run*). *repaired* is ``True``
        when the candidate was already ledgered but needed its missing stamp
        restored.
    """
    if candidate.source_key in ledger:
        if candidate.has_origin_key:
            return False, False
        if not dry_run:
            _stamp_origin_key(candidate.md_file, candidate.source_key)
        return False, True
    if not dry_run:
        ledger.record(
            candidate.source_key,
            candidate.fragment_id,
            _stored_body_hash(candidate.body),
        )
        _stamp_origin_key(candidate.md_file, candidate.source_key)
    return True, False


def pin_source_ids(vault_path: Path, *, dry_run: bool = False) -> PinResult:
    """Back-fill the markdown ingest ledger with existing fragment ids (#1329).

    Walks the vault's fragment roots, resolves each fragment to the markdown
    source that produced it, and records the fragment's **existing** id
    against that source's ledger key — so the id-derivation change that
    motivated this migration can never move an id that is already on disk.
    Each pinned fragment also gains ``source.origin_key`` in its frontmatter,
    written atomically.

    A ``source_key`` claimed by more than one live fragment is pinned for
    neither and reported as a conflict; see :func:`_index_by_source_key`.

    Idempotent: a key already present in the ledger is counted as
    ``already_pinned`` and left alone, so a second run reports ``pinned == 0``
    and writes nothing.

    Args:
        vault_path: Vault root (the directory containing ``01-Fragments/``).
        dry_run: When ``True``, nothing is written — no ledger append and no
            frontmatter stamp — but the returned result is fully populated so
            a caller can print the plan.

    Returns:
        A :class:`PinResult` summarising what was (or would be) pinned.
    """
    candidates, unpinnable = _collect_candidates(vault_path)
    ledger = SourceLedger.load(vault_path, source=_LEDGER_SOURCE)

    conflicts: list[str] = []
    pinned = 0
    already_pinned = 0
    repaired = 0
    for source_key, claimants in _index_by_source_key(candidates).items():
        if len(claimants) > 1:
            conflicts.append(_conflict_line(source_key, claimants))
            continue
        was_pinned, was_repaired = _pin_one(claimants[0], ledger, dry_run=dry_run)
        if was_pinned:
            pinned += 1
        else:
            already_pinned += 1
            repaired += int(was_repaired)

    return PinResult(
        pinned=pinned,
        already_pinned=already_pinned,
        repaired=repaired,
        conflicts=conflicts,
        unpinnable=unpinnable,
        reproduced=sum(1 for candidate in candidates if _reproduces_own_id(candidate)),
        examined=len(candidates) + len(unpinnable),
    )
