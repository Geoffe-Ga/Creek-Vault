"""Deny-by-default sweep of ``00-Creek-Meta/`` during a vault purge (#1453).

``purge_vault`` wipes the ten numbered content folders and, until this
module, left ``00-Creek-Meta/`` untouched — which meant a whole-vault
right-to-be-forgotten request left the ingest ledger (source path →
fragment id → a **full unsalted SHA-256 of the body**), the provenance
log (an absolute path built from the fragment's *title*), the consent log
(the operator's name and the original source directory), the dedup
manifest (content-hash → id, a plaintext-confirmation oracle) and the
Discord capture staging root (raw plaintext) sitting on disk.

The obvious repair — enumerate the survivors and delete them — rots the
moment somebody adds a twenty-first artifact, and it rots *silently*,
because nothing fails when a new leak appears. So the default is
inverted: **every regular file under ``00-Creek-Meta/`` is destroyed
unless it is on :data:`META_PURGE_KEEP`.** A future artifact nobody
anticipated is swept, which is the restrictive direction; the cost is
that a genuinely load-bearing new file has to be added to the keep-list,
where its survival is a written decision with a reason attached rather
than an accident of enumeration.

The walk lives here rather than in ``creek/purge/engine.py`` because
that module is already 2400 lines and sits at the ``xenon
--max-modules B`` ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

META_RELDIR: Final[str] = "00-Creek-Meta"
"""The vault folder this module sweeps."""


@dataclass(frozen=True)
class MetaKeep:
    """One path under ``00-Creek-Meta/`` that outlives a whole-vault purge.

    Attributes:
        relpath: Path relative to ``00-Creek-Meta/``. A directory keeps
            its entire subtree. Matched with ``PurePosixPath`` semantics
            — never a string prefix, so ``Ontology-drafts/`` is not
            sheltered by the ``Ontology/`` entry.
        reason: Why this artifact survives an erasure request. Non-empty
            by construction (:func:`_validate_keep_list`): "we always
            kept it" is not a reason, and an entry that cannot state one
            belongs on the wipe side of the line.
    """

    relpath: PurePosixPath
    reason: str


META_PURGE_KEEP: Final[tuple[MetaKeep, ...]] = (
    MetaKeep(
        PurePosixPath("creek_config.yaml"),
        "The vault marker `_require_vault_marker` checks for (GAP-003): delete "
        "it and `creek` stops recognising the directory as a vault, including "
        "for the next purge, whose marker check would refuse. Holds operator "
        "configuration — redaction patterns, classification thresholds — and "
        "no vault-derived content. (The `privacy_tier` ratchet is *not* here: "
        "it is per-fragment frontmatter, destroyed with the fragments, and the "
        "surviving record of tier overrides is `audit/privacy.jsonl`.)",
    ),
    MetaKeep(
        PurePosixPath("audit/purge.jsonl"),
        "The erasure record itself. A purge that destroyed its own compliance "
        "log would leave no evidence the erasure was performed.",
    ),
    MetaKeep(
        PurePosixPath("audit/privacy.jsonl"),
        "The privacy-tier ratchet history. Destroying it would erase the "
        "evidence of which tier content was held at when it was erased.",
    ),
    MetaKeep(
        PurePosixPath("audit/redact.jsonl"),
        "The redaction record — what was scrubbed, and when. Same compliance "
        "standing as the purge log.",
    ),
    MetaKeep(
        PurePosixPath("audit/mcp.jsonl"),
        "Hash-chained MCP tool audit log with its own chain verifier. An "
        "MCP-invoked purge appends to it *around* the purge, so sweeping it "
        "would destroy the chain the operation is about to write into.",
    ),
    MetaKeep(
        PurePosixPath("Processing-Log/purge-log.json"),
        "The pre-Batch-C spelling of `audit/purge.jsonl`, kept on the same "
        "compliance grounds. `PurgeAuditLog` migrates it into the new log at "
        "construction — but only when the new log is *empty*, and it is not "
        "empty in any vault that has ever purged: the migration logs a warning "
        "and skips. Sweeping this file would therefore destroy un-migrated "
        "erasure records in the common case, not the rare one.",
    ),
)
"""Everything that survives ``creek purge vault`` under ``00-Creek-Meta/``.

Deliberately short, and it holds **only** compliance records plus the
vault marker. Notably absent, and therefore destroyed:
``State/ingest/*.jsonl`` (the leak this list exists to close),
``Processing-Log/provenance.jsonl`` (documented as "not compliance-grade,
allowed to be lossy", which forecloses the compliance defence),
``Processing-Log/consent-log.json``, ``dedup-manifest.json``,
``voice-fingerprint.json`` and ``audit/compile-<target_id>.hash`` — a
content hash, not a compliance record.

**The ``creek init`` scaffold directories are absent too, deliberately.**
``Ontology/``, ``Skills/``, ``Templates/`` and ``Scripts/`` look like safe
keeps — they are deployed from this package's templates and a fresh vault
holds nothing else in them. They are not safe. ``deploy_skills``
(``creek/scaffold.py``) creates ``Skills/`` explicitly so "a vault always
has a ``Skills/`` directory to drop operator-authored skills into", and
``detect_drifted_skills`` compares only the canonical set precisely so
operator-authored files there are left alone. So ``Skills/`` holds
first-class operator content, and any of it may quote a fragment's title,
id or body.

Keeping the prefix would have made the whole-vault purge *weaker than the
scoped one*: ``_scrub_references`` already walks ``00-Creek-Meta/Skills``
during ``purge_fragment`` and rewrites wiki-links and bare id mentions
found there, so a scoped erasure cleans a custom skill file that the
"nuclear option" would have preserved untouched. Sweeping the scaffold
costs nothing that cannot be restored: ``creek init --vault <path>``
redeploys all four trees, and ``creek skills sync`` redeploys the skill
tree on its own.
"""

SWEEP_EXEMPT: Final[tuple[PurePosixPath, ...]] = (PurePosixPath("embeddings.parquet"),)
"""Paths this sweep steps over that are **not** kept.

``00-Creek-Meta/embeddings.parquet`` is destroyed by
``PurgeEngine._delete_cache_file()``, which reports how many cached rows
it removed. Sweeping the file first would make that report zero — the
deletion still happens, but the erasure record stops naming its size.
The distinction from a keep is load-bearing: exempt means "another pass
owns destroying this", not "this survives".
"""


def _validate_keep_list(keeps: tuple[MetaKeep, ...]) -> None:
    """Reject a keep-list that cannot mean what it says.

    Args:
        keeps: The keep-list to check.

    Raises:
        ValueError: On a duplicate relpath, an empty reason, or an entry
            that is absolute or re-states the ``00-Creek-Meta`` prefix
            it is already relative to.
    """
    seen: set[PurePosixPath] = set()
    for keep in keeps:
        if keep.relpath in seen:
            msg = f"Duplicate keep-list entry: {keep.relpath}"
            raise ValueError(msg)
        seen.add(keep.relpath)
        if not keep.reason.strip():
            msg = f"Keep-list entry {keep.relpath} states no reason"
            raise ValueError(msg)
        if keep.relpath.is_absolute() or keep.relpath.parts[:1] == (META_RELDIR,):
            msg = (
                f"Keep-list entry {keep.relpath} must be relative to "
                f"{META_RELDIR}/, without restating it"
            )
            raise ValueError(msg)


_validate_keep_list(META_PURGE_KEEP)


def is_kept(relpath: PurePosixPath) -> bool:
    """Report whether *relpath* is sheltered by :data:`META_PURGE_KEEP`.

    Uses ``is_relative_to`` rather than a string prefix, so a keep of
    ``audit/purge.jsonl`` cannot be made to shelter
    ``audit/purge.jsonl.bak``. Every current entry happens to name a
    single file, but the containment semantics are the ones to have: a
    prefix keep that ever *is* added shelters its subtree and nothing
    that merely starts with the same characters.

    Args:
        relpath: A path relative to ``00-Creek-Meta/``.

    Returns:
        ``True`` when the path is a keep-list entry or lies beneath one.
    """
    return any(relpath.is_relative_to(keep.relpath) for keep in META_PURGE_KEEP)


def _relpath_of(path: Path, meta_root: Path) -> PurePosixPath:
    """Return *path* as a POSIX path relative to *meta_root*.

    Args:
        path: A path inside the meta root.
        meta_root: The ``00-Creek-Meta/`` directory.

    Returns:
        The relative path, slash-separated on every platform.
    """
    return PurePosixPath(path.relative_to(meta_root).as_posix())


def _sweep_candidates(meta_root: Path, current: Path) -> Iterator[Path]:
    """Yield the regular files under *current* that the sweep may destroy.

    Symlink policy is copied from ``engine._regular_files_under`` so the
    meta sweep and the content wipe cannot disagree about what a link
    stands for: a symlink to a file is yielded (``unlink`` really does
    destroy that alias), a symlink to a directory is never walked
    through (the files behind it are not this purge's to claim, and
    naming out-of-vault paths in a record that survives the purge is its
    own leak).

    Args:
        meta_root: The ``00-Creek-Meta/`` directory, for relative paths.
        current: The directory being walked.

    Yields:
        Files that are neither kept nor exempt.
    """
    for entry in sorted(current.iterdir()):
        relpath = _relpath_of(entry, meta_root)
        if is_kept(relpath) or relpath in SWEEP_EXEMPT:
            continue
        if entry.is_symlink():
            if entry.is_file():
                yield entry
            continue
        if entry.is_dir():
            # Recurse even when nothing here is kept, and *especially*
            # when only a descendant is: `audit/` is not a keep-list
            # entry, but four files inside it are, so the directory has
            # to be entered and decided file by file.
            yield from _sweep_candidates(meta_root, entry)
        elif entry.is_file():
            yield entry


def sweep_unkept_meta(
    meta_root: Path,
    *,
    skip: Callable[[Path], bool],
    remove: Callable[[Path], None],
) -> int:
    """Destroy every unkept regular file under *meta_root*, and count them.

    Args:
        meta_root: The vault's ``00-Creek-Meta/`` directory. A missing
            directory sweeps nothing and counts nothing.
        skip: Consulted **after** the keep-list and exempt decisions,
            never before — the caller's dry-run bookkeeping, which must
            not be able to widen what an apply run destroys. A file an
            earlier pass in the same operation already removed is
            skipped so a preview does not count it twice.
        remove: Destroys one file. Called with a file the sweep has
            decided against; in a dry run the caller makes this a no-op.

    Returns:
        The number of files removed, incremented only **after** *remove*
        returns without raising. An erasure record that over-claims a
        destruction is worse than one that under-claims it (#1340), and
        an ``OSError`` mid-sweep aborts the purge into a
        ``status="partial"`` outcome line that must not name work the
        operation did not do.
    """
    if not meta_root.is_dir():
        return 0
    removed = 0
    for path in _sweep_candidates(meta_root, meta_root):
        if skip(path):
            continue
        remove(path)
        removed += 1
    return removed
