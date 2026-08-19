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
        relpath: Path relative to ``00-Creek-Meta/``, naming exactly
            one **regular file**. Matched with ``PurePosixPath``
            equality — never a string prefix, so ``Ontology-drafts/``
            is not sheltered by an ``Ontology/`` entry, and never a
            containment prefix, so a *directory* standing at this path
            shelters nothing beneath it (#1484). The "one regular file"
            part is a requirement on whoever adds a row, not a promise
            the type can keep: an entry written with a directory in
            mind shelters *nothing*, silently, because no file inside
            it equals the entry's path. See :func:`_validate_keep_list`.
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

    **Every entry must name exactly one regular file**, and that is a
    constraint this function can only partly enforce. Since #1484
    :func:`is_kept` matches by path *equality*, so an entry added with
    a *directory* in mind shelters nothing at all: the walk descends
    into the directory and each file inside is compared against the
    entry's own path, which none of them equals. The failure is silent
    — no error, no warning, just an intended survivor swept — so the
    constraint is stated here, in :class:`MetaKeep` and in
    ``docs/cleaning-and-purge.md`` rather than discovered during an
    erasure. A directory-shaped keep needs :func:`is_kept` to grow a
    deliberate second matching mode, with the unbounded-subtree
    argument in that function's docstring answered first.

    What *is* enforced statically: no duplicates, a stated reason, a
    path that is relative to ``00-Creek-Meta/`` without restating it,
    and a path that actually names something. The last of those is the
    meta root itself sneaking in as ``PurePosixPath(".")``, which under
    equality semantics shelters nothing and under any future
    containment mode would shelter the entire directory this module
    exists to sweep.

    Args:
        keeps: The keep-list to check.

    Raises:
        ValueError: On a duplicate relpath, an empty reason, an entry
            that is absolute or re-states the ``00-Creek-Meta`` prefix
            it is already relative to, or an entry that names no path
            beneath that prefix.
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
        if not keep.relpath.parts:
            msg = f"Keep-list entry {keep.relpath!s} names no file under {META_RELDIR}/"
            raise ValueError(msg)


_validate_keep_list(META_PURGE_KEEP)


def is_kept(relpath: PurePosixPath) -> bool:
    """Report whether *relpath* is exactly a :data:`META_PURGE_KEEP` entry.

    ``PurePosixPath`` **equality**, so a keep of ``audit/purge.jsonl``
    shelters neither ``audit/purge.jsonl.bak`` (a string prefix) nor
    ``audit/purge.jsonl/leak.md`` (a containment prefix).

    Containment matching was the original spelling and it was the wrong
    call (#1484). Every entry names a single file, but nothing stops a
    *directory* appearing at one of those paths — a hand-edited vault,
    a botched restore, an attacker with local write access — and under
    containment the entire subtree beneath it was then sheltered. One
    documented survivor would have become an unbounded number of
    undocumented ones on a right-to-be-forgotten path, while the
    survivor table in ``docs/cleaning-and-purge.md`` still claimed the
    vault was clean. A keep shelters one file; a directory standing
    where it names a file is swept like anything else, which is the
    restrictive direction and the same default the whole module is
    built on.

    Args:
        relpath: A path relative to ``00-Creek-Meta/``.

    Returns:
        ``True`` only when the path *is* a keep-list entry.
    """
    return any(relpath == keep.relpath for keep in META_PURGE_KEEP)


def _relpath_of(path: Path, meta_root: Path) -> PurePosixPath:
    """Return *path* as a POSIX path relative to *meta_root*.

    Args:
        path: A path inside the meta root.
        meta_root: The ``00-Creek-Meta/`` directory.

    Returns:
        The relative path, slash-separated on every platform.
    """
    return PurePosixPath(path.relative_to(meta_root).as_posix())


def _is_sheltered(relpath: PurePosixPath) -> bool:
    """Report whether the sweep steps over *relpath* rather than destroying it.

    The two dispositions differ in meaning and not in effect here: a
    keep survives the erasure, an exempt path is destroyed by another
    pass that reports what it removed. Neither is this walk's to take.

    Args:
        relpath: A path relative to ``00-Creek-Meta/``.

    Returns:
        ``True`` when *relpath* is on the keep-list or the exempt tuple.
    """
    return is_kept(relpath) or relpath in SWEEP_EXEMPT


def _breaks_no_link(_entry: Path) -> bool:
    """Answer that the caller destroys nothing a meta symlink points at.

    The default *breaks_link* for :func:`sweep_unkept_meta`, and the
    only correct answer for a caller that is not itself deleting the
    link's target. ``purge_vault`` overrides it, because it is.

    Args:
        _entry: The symlink being classified; deliberately unused.

    Returns:
        ``False``, always.
    """
    return False


def _sweep_destroys_link(entry: Path, *, breaks_link: Callable[[Path], bool]) -> bool:
    """Report whether the sweep unlinks the symlink *entry*.

    ``Path.is_file()`` **follows** the link, which is why the second
    clause is needed rather than redundant: a *dangling* link answers
    ``False`` to ``is_file()``, so asking that question alone dropped it
    on the floor (#1485). ``purge_vault`` creates dangling links itself
    — it wipes the content folders before this sweep runs — and the
    residue a survivor leaves is not a body but its **target string**,
    which in this vault is routinely title-derived.

    So a dangling link is unlinked **regardless of what it once pointed
    at**, a directory target included. The directory exemption below
    therefore holds only for a target that *outlives* the purge, and
    *breaks_link* is what makes a dry run say so. Without it the two
    runs met different filesystems and disagreed: a preview does not
    wipe the content folders, so ``00-Creek-Meta/latest-thread ->
    01-Fragments/Journal/`` still resolved to a live directory and was
    left alone, while the apply run met the same link dangling and
    unlinked it. Preview 0, apply 1 — the #1485 divergence in a mirror,
    which is why the caller is asked whether it is *about to* destroy
    the target rather than only whether it already has.

    A link to a directory the purge does not touch is the one case that
    stays: unlinking it would be safe, but it is the content wipe's
    pinned policy that a surviving directory link is neither followed
    nor claimed, and the two walks must not disagree about what such a
    link stands for.

    ``engine._regular_files_under`` answers ``[]`` for a broken link and
    that is not a disagreement: it builds the *record* of destroyed
    content, and a broken link destroys none — the wipe's ``rmtree``
    removes it regardless. This function decides what gets destroyed at
    all, so the same link has to be named here or nothing removes it.

    On Python 3.11 to 3.13 ``exists()`` and ``is_file()`` re-raise
    ``PermissionError`` on an unreadable parent, so an ``EACCES`` mid
    sweep aborts the purge into ``status="partial"`` rather than
    silently widening this predicate. Python 3.14 changed that: both
    swallow ``OSError`` and answer ``False``, which would turn an
    unreadable link into "dangling, unlink it". That is the restrictive
    direction, so it is safe — but it is a silent behaviour change, and
    the abort it removes is one this module's callers rely on for the
    *count* to be honest. Revisit when 3.14 enters the support matrix.

    Args:
        entry: A path already known to be a symlink.
        breaks_link: Reports whether the caller's own operation destroys
            what *entry* points at, so a dry run classifies the link the
            way the apply run will.

    Returns:
        ``True`` for a link to a regular file, for a broken link, and
        for a link whose target this operation is about to destroy.
    """
    return entry.is_file() or not entry.exists() or breaks_link(entry)


def _sweep_candidates(
    meta_root: Path,
    current: Path,
    *,
    breaks_link: Callable[[Path], bool],
) -> Iterator[Path]:
    """Yield the files and symlinks under *current* the sweep may destroy.

    Symlink policy is copied from ``engine._regular_files_under`` so the
    meta sweep and the content wipe cannot disagree about what a link
    stands for: a symlink to a file is yielded (``unlink`` really does
    destroy that alias), a symlink to a directory is never walked
    through (the files behind it are not this purge's to claim, and
    naming out-of-vault paths in a record that survives the purge is its
    own leak). It parts from that helper on exactly one case: a
    **dangling** symlink is yielded here (#1485), because this walk
    decides what is destroyed while that one only records what already
    was.

    Ordering is the load-bearing part. The shelter check runs **after**
    the ``is_dir()`` branch, never before (#1484): asked first, it
    short-circuited a directory standing at a keep or exempt path, and
    the walk then never descended into it at all — sheltering an
    unbounded subtree on the strength of one documented file. A
    directory is walked no matter what its name is; only what the walk
    finds *inside* gets a disposition.

    Args:
        meta_root: The ``00-Creek-Meta/`` directory, for relative paths.
        current: The directory being walked.
        breaks_link: Passed to :func:`_sweep_destroys_link`; reports
            whether the caller's own operation destroys a link's target.

    Yields:
        Files and links that are neither kept nor exempt.
    """
    for entry in sorted(current.iterdir()):
        relpath = _relpath_of(entry, meta_root)
        if entry.is_symlink():
            # Asked before ``is_dir()``, which follows the link: a link
            # is an alias, never a directory this walk may descend.
            if not _is_sheltered(relpath) and _sweep_destroys_link(
                entry,
                breaks_link=breaks_link,
            ):
                yield entry
            continue
        if entry.is_dir():
            # Recurse even when nothing here is kept, and *especially*
            # when only a descendant is: `audit/` is not a keep-list
            # entry, but four files inside it are, so the directory has
            # to be entered and decided file by file.
            yield from _sweep_candidates(meta_root, entry, breaks_link=breaks_link)
            continue
        if entry.is_file() and not _is_sheltered(relpath):
            yield entry


def sweep_unkept_meta(
    meta_root: Path,
    *,
    skip: Callable[[Path], bool],
    remove: Callable[[Path], None],
    breaks_link: Callable[[Path], bool] = _breaks_no_link,
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
        breaks_link: Reports whether the caller's own operation destroys
            what a ``00-Creek-Meta/`` symlink points at. Defaults to
            "nothing", which is right for every caller that is not also
            deleting vault content; ``purge_vault`` is, and passing this
            is what keeps its preview and its apply run agreeing about a
            link into a content folder.

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
    for path in _sweep_candidates(meta_root, meta_root, breaks_link=breaks_link):
        if skip(path):
            continue
        remove(path)
        removed += 1
    return removed


def prune_empty_meta_dirs(meta_root: Path) -> None:
    """Remove every directory left empty under *meta_root*, bottom-up (#1547).

    :func:`sweep_unkept_meta` destroys files and symlinks and never a
    directory, so a right-to-be-forgotten purge left identifying
    directory *names* standing —
    ``State/discord/capture-staging/messages/<channel>/`` is an empty
    folder afterwards, and a Discord channel name is routinely
    identifying. That is the same residue #1485 argued about for a
    dangling symlink's target string, on a directory entry instead: the
    body is gone and the name is not.

    **No directory under ``00-Creek-Meta/`` is protected.** The
    survivors are exactly the directories that still hold something —
    ``audit/`` because four compliance logs are on
    :data:`META_PURGE_KEEP`, ``Processing-Log/`` when the legacy
    ``purge-log.json`` is present — and they survive by being non-empty,
    not by being named anywhere. The ``creek init`` scaffold roots
    (``State/``, ``Ontology/``, ``Scripts/``, ``Templates/``,
    ``Skills/``) are pruned along with everything else, on exactly the
    argument this module already makes for their *contents*: ``creek
    init --vault <path>`` redeploys all of them and ``creek skills
    sync`` redeploys the skill tree alone, so nothing is lost that the
    tool cannot restore, and an operator who is told to re-run
    ``creek init`` after a vault purge is being told that already. A
    keep-list for directories would have to carry the same "one
    documented survivor, unbounded undocumented subtree" hazard #1484
    closed, for no gain.

    ``rmdir``, never ``rmtree``: it refuses a directory that is not
    empty, so the walk **cannot** destroy content on a wrong turn — the
    worst a bug here can do is leave a directory standing. A symlink is
    never descended (``is_dir()`` follows one), so a link pointing out
    of the vault cannot steer the prune at a tree this purge does not
    own; it counts as an occupant of its parent instead, which keeps
    that parent standing.

    *meta_root* itself is never removed. It holds ``creek_config.yaml``
    — the vault marker the *next* purge's ``_require_vault_marker``
    check looks for — so it is non-empty in any real vault; the walk
    ignores its emptiness regardless rather than depending on that.

    Runs strictly **after** :func:`sweep_unkept_meta`, and that ordering
    keeps a ``--dry-run`` preview honest: the sweep classifies a symlink
    to a *surviving* directory as one to leave alone, and it must reach
    that verdict from the same facts on both runs. One residue is left
    standing and is worth naming rather than discovering: a surviving
    link that pointed at a directory this pass then prunes is left
    dangling, carrying its target string. That is the pre-existing shape
    of the surviving-directory-link exemption (such a link survives with
    the same string today, pruned target or not), not something this
    pass introduces — but it is the one case where a name outlives the
    prune.

    Nothing is counted. A removed empty directory destroyed no content,
    so it is not a ``meta_artifacts_removed`` artifact, and every number
    a purge reports is unchanged by this pass — which is what keeps a
    ``--dry-run`` preview and its apply twin agreeing. A dry run does
    not call this at all: it removes nothing, so there is nothing to
    simulate.

    Args:
        meta_root: The vault's ``00-Creek-Meta/`` directory. A missing
            directory prunes nothing.
    """
    if not meta_root.is_dir():
        return
    _prune_empty_dirs_below(meta_root)


def _prune_empty_dirs_below(current: Path) -> bool:
    """Prune *current*'s empty descendants and report whether it is now empty.

    Depth-first, so a directory is only judged after the pass has had a
    chance to empty it: the ``messages/<channel>/`` leaf goes first, then
    ``messages/``, then ``capture-staging/``. A single top-down pass
    would remove none of them.

    Args:
        current: A real directory (never a symlink to one).

    Returns:
        ``True`` when nothing is left inside *current*. The caller
        ``rmdir``s it on that answer; the top-level caller ignores it,
        because the meta root itself is not this pass's to remove.
    """
    occupied = False
    for entry in sorted(current.iterdir()):
        # ``is_dir()`` follows a symlink, so the link test comes first —
        # otherwise a link to a directory is descended and pruned, which
        # would reach outside ``00-Creek-Meta/`` and possibly outside the
        # vault. A link is an occupant of this directory, nothing more.
        if entry.is_symlink() or not entry.is_dir():
            occupied = True
            continue
        if _prune_empty_dirs_below(entry):
            entry.rmdir()
        else:
            occupied = True
    return not occupied
