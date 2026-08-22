"""Symlink containment for trees the operator names (#1087/#1293/#1294).

Three surfaces need the same question answered — "does this path leave the
root it was reached through?" — and until #1294 each answered it in its own
words:

* ``creek.redact.scanner._scannable_candidates`` — the redaction **read**
  path, which skips an escaping child and counts it.
* ``creek.redact.cli_commands`` — the redaction **write** path, which
  refuses before it opens anything.
* ``creek.ingest`` — every ingestor's discovery walk, which had no
  containment check at all and read whatever its glob yielded.

A predicate copied three times is three predicates, and they drift. This
module is the single definition all three now import, so "inside" cannot
come to mean one thing for the scanner and another for the ingestors.

**Stdlib-only by design.** :mod:`creek.ingest.base` imports this module, and
:mod:`creek.redact.scanner` compiles a large regex battery at import time;
depending on the scanner from the ingest path would drag redaction into
every ingest. The same reasoning that keeps :mod:`creek._fsio` underneath
the writers keeps this module underneath both.

The policy, unchanged from the shipped SEC-003 guard:

* Resolve the ROOT exactly once.
* ``lstat`` (i.e. :meth:`~pathlib.Path.is_symlink`) only the LEAF; never
  resolve a child that is not itself a link. Resolving non-symlink children
  would flag every child of a root reached *through* a symlinked component
  — ``/tmp`` -> ``/private/tmp`` on macOS — as escaping.
* Unprovable containment IS an escape.
* The resolution primitive is ``os.path.realpath``, NOT
  :meth:`~pathlib.Path.resolve` — see :func:`_resolved_target` for why the
  difference is load-bearing rather than stylistic.

That policy is deliberately LEAF-ONLY, so a path reached through an
escaping *ancestor* component (``<root>/linkdir/a.md``, where ``linkdir``
is the link) is admitted by :func:`resolves_within` alone. For a walked
tree that residual is closed by :func:`inspect_tree`, which sees ``linkdir``
in its own right; for a directly-named path it remains the documented
residual recorded in ``docs/security/threat-model.md``.

A walk answers TWO questions, not one, and #1498 separated them: "did I find
an escaping link?" and "was there a subtree I could not even list?". They are
kept apart in :class:`TreeContainment` because the callers weigh them
differently — the ingest gate reports an unlistable subtree and continues,
the redaction write path refuses — and a single boolean would force one
uniform answer onto both.

Security direction here is one-way: every function may only ever cause a
caller to read *less*. There is no waiver — no ``--force``, no environment
variable, no config key — per SEC-003's no-waiver precedent.
"""

from __future__ import annotations

import itertools
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class EscapingSymlinkError(RuntimeError):
    """A symlink under a named root resolves outside that root.

    Raised by :func:`assert_source_contained` so that ingestion refuses a
    source tree rather than reading through the link. Refusal rather than
    skipping is the contract for this surface: ingest is a *write* path —
    what it reads becomes a durable vault fragment that then feeds
    classification, embeddings, cloud LLM prompts, drafts and compiled
    pages — and #1360 established that the write path refuses while only
    the read path (``redact --scan``, which writes nothing) may skip and
    count.

    Neither :attr:`path` nor the message ever names the link's *resolved*
    target. Disclosing where the link points is the exfiltration oracle
    #1087 closes, and it would be perverse for the refusal to leak the very
    thing it refused to read.

    Attributes:
        path: The offending link exactly as walked or supplied — never
            resolved, and never normalised, so the operator can find it by
            the name they know it by.
        root: The source root the operator named.
    """

    def __init__(self, path: Path, root: Path) -> None:
        """Build a containment refusal naming the link and its root.

        Args:
            path: The offending symlink, exactly as walked or supplied.
            root: The source root the operator named.
        """
        self.path = path
        self.root = root
        super().__init__(
            f"Refusing to ingest {root}: {path} is a symlink whose target "
            "resolves outside that tree, so ingestion would write content "
            "from outside the source into the vault. Remove or re-point the "
            "link, or ingest the target's own directory directly."
        )


def _resolved_target(child: Path) -> Path | None:
    """Resolve *child* to the location containment must be judged against.

    The one place symlinks are followed, and deliberately NOT via
    :meth:`pathlib.Path.resolve`. ``Path.resolve``'s behaviour on a symlink
    **cycle** is not part of pathlib's contract and it changed under us:

    * On 3.11/3.12, ``Path.resolve(strict=False)`` raises
      ``RuntimeError("Symlink loop from ...")``.
    * On 3.13 it delegates to ``os.path.realpath``, which for
      ``strict=False`` treats a cycle as "stop unwinding here" and returns
      a PARTIAL path with the looping component left unresolved.

    That partial answer is worse than an error, because for a cycle that
    closes back on its own starting point it erases the hops in between.
    ``<root>/a.md -> <outside>/o.md -> <root>/a.md`` resolves, on 3.13, to
    ``<root>/a.md`` — an in-root answer for a link whose very first hop
    leaves the root. Judging containment on that answer admits the link.

    ``os.path.realpath(..., strict=True)`` reports a cycle as
    ``OSError(ELOOP)`` on every supported version, so it is the primitive
    with the stable contract. Two arms, and the split is the whole policy:

    * ``FileNotFoundError`` is NOT a containment failure. A dangling link
      still names a candidate location worth comparing, so it falls back to
      ``strict=False`` and is judged on where its target *would* sit —
      which is what refuses a broken link pointing out of the root while
      admitting a stale in-tree alias.
    * Every other ``OSError`` — a cycle, an unreadable component — is a
      containment that cannot be proven, and by this module's policy an
      unprovable containment is an escape.

    Args:
        child: A path being tested for containment, as walked.

    Returns:
        The location to compare against the root, or ``None`` when
        resolution failed in a way that leaves containment unproven.
    """
    try:
        return Path(os.path.realpath(child, strict=True))
    except FileNotFoundError:
        return Path(os.path.realpath(child, strict=False))
    except OSError:
        return None


def resolves_within(child: Path, resolved_root: Path) -> bool:
    """Report whether *child*'s symlink target stays under *resolved_root*.

    The single containment predicate for the whole codebase: the redaction
    scanner's walked-child surface, the redaction CLI's named-path surface,
    and the ingest discovery gate all call this one function, so they cannot
    drift into subtly different definitions of "inside".

    The failure arm is a deliberate classification, not a swallowed error:
    an unresolvable link (``None`` from :func:`_resolved_target` — a cycle,
    an unreadable component) and a target outside the root (``ValueError``
    from ``relative_to``) are both cases where containment cannot be
    proven, and an unprovable containment is an escape. Every caller logs
    and counts its rejections.

    Resolution goes through :func:`_resolved_target` rather than
    :meth:`~pathlib.Path.resolve` so that this classification is a decision
    this module makes, not one inherited from whichever exception the
    stdlib happens to raise that release. It was the latter until 3.13
    changed the answer underneath it; see that function.

    A dangling link is still judged on its candidate location, so a broken
    link pointing *outside* is refused rather than waved through on the
    technicality that its target does not exist yet.

    The exception object itself is deliberately dropped rather than logged:
    ``relative_to``'s message quotes the *resolved* target, which is exactly
    the out-of-root path this guard exists to keep out of the record.

    Args:
        child: A path being tested for containment, as walked.
        resolved_root: The root, already resolved exactly once by the
            caller.

    Returns:
        ``True`` when the link's target is a descendant of *resolved_root*.
    """
    try:
        target = _resolved_target(child)
        if target is None:
            return False
        target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def named_path_escapes(path: Path) -> bool:
    """Report whether *path* is itself a symlink leaving its own parent.

    The containment question for a path the operator names directly, as
    opposed to one discovered while walking a tree.

    Three properties carry the correctness argument:

    * ``is_symlink()`` is an ``lstat`` on the leaf, so a path that is not
      itself a link is never resolved and never compared. That is what
      makes this guard behave identically on darwin and on Linux CI: a root
      reached *through* a symlinked component (``/tmp`` -> ``/private/tmp``
      on macOS) is not flagged.
    * When the leaf *is* a link, BOTH sides are resolved — the target
      inside :func:`resolves_within`, the parent here — so ``/tmp`` ->
      ``/private/tmp`` cannot manufacture a spurious refusal for a link
      that never left its own directory.
    * The predicate is LEAF-ONLY, and deliberately so: a named path whose
      escaping link is an ANCESTOR component is admitted. That is a known,
      accepted residual — not full coverage of path traversal.

    Args:
        path: The path exactly as the operator supplied it.

    Returns:
        ``True`` when *path* is a symlink whose target is not a descendant
        of its own resolved parent directory.
    """
    return path.is_symlink() and not resolves_within(
        path,
        path.parent.resolve(strict=False),
    )


def escaping_child(child: Path, resolved_root: Path) -> bool:
    """Report whether a walked leaf is a symlink leaving *resolved_root*.

    The leaf test every walk needs, in one place. The expression it holds —
    ``child.is_symlink() and not resolves_within(child, resolved_root)`` — was
    written out independently in :func:`inspect_tree`'s ancestor and in
    :func:`creek.redact.scanner._scannable_candidates`, and #1373 needed it a
    third time in :func:`creek.vault.reader.iter_vault_fragments`. #1294
    already settled the same argument for :func:`resolves_within`: two copies
    that agree today are two copies that disagree after the next fix lands in
    one of them.

    ``is_symlink()`` first is not merely an optimisation, it is the policy: a
    child that is not itself a link is never resolved, which is what keeps a
    root reached *through* a symlinked component (``/tmp`` ->
    ``/private/tmp`` on macOS) from flagging every one of its children.

    Args:
        child: A path being tested for containment, exactly as walked.
        resolved_root: The root, already resolved exactly once by the caller.

    Returns:
        ``True`` when *child* is a symlink whose target is not a descendant
        of *resolved_root*.
    """
    return child.is_symlink() and not resolves_within(child, resolved_root)


@dataclass(frozen=True, slots=True)
class TreeContainment:
    """What one containment walk of a tree was able to establish (#1498).

    Two independent facts, deliberately not collapsed into one verdict:

    Attributes:
        escaping: The first entry found that links out of the root, as
            walked and never resolved, or ``None`` when the walk saw no
            such entry. ``None`` means "none seen", which is only "none
            exists" when :attr:`unlistable` is empty.
        unlistable: Every directory whose ``scandir`` was refused, as
            walked. A non-empty tuple means the walk's answer is a
            partial one: nothing beneath those directories was inspected,
            so containment under them is unproven rather than proven good.
    """

    escaping: Path | None
    unlistable: tuple[Path, ...]


def inspect_tree(root: Path) -> TreeContainment:
    """Walk *root* and report both what escaped and what could not be read.

    Walks with ``followlinks=False`` so the walk itself never descends
    through a link, and inspects BOTH ``dirnames`` and ``filenames``.
    Checking directory entries is what closes
    :meth:`creek.ingest.code.CodeIngestor._discover_directory`, which
    recurses manually with ``is_dir()`` — and ``is_dir()`` follows symlinks,
    so unlike the ``rglob`` ingestors it would otherwise walk an entire
    out-of-tree subtree.

    **The ``onerror`` handler is the point of this function.** ``os.walk``
    swallows a failed ``scandir`` when no handler is passed, so a directory
    the process cannot list is indistinguishable from one that is empty, and
    the walk then reports "no escaping symlink" over a region it never
    opened. That is a guarantee the code cannot honour, and it contradicts
    this module's own policy — "unprovable containment IS an escape" — which
    :func:`_resolved_target` already applies to the identical ``EACCES``
    condition when it is hit during *resolution* rather than during the walk.

    Reporting rather than raising is what lets callers with different
    refusal mechanics — ``typer.Exit`` in :mod:`creek.redact.cli_commands`,
    :class:`EscapingSymlinkError` in :func:`assert_source_contained` — share
    one walk instead of keeping two copies of it, and it is also what lets
    them weigh an unlistable subtree differently: the ingest gate reports and
    continues, the redaction write path refuses. See each caller for why.

    The walk runs to COMPLETION rather than returning on the first escape,
    because both facts have to be complete for a caller to weigh them. The
    cost is paid only on a tree that already holds an escaping link; a clean
    tree — the overwhelmingly common case — was always walked in full.

    Deliberately mirrors
    :func:`creek.ingest.markdown._enumerate_markdown_paths` (#1444): same
    ``onerror`` idiom, same ``followlinks=False``, so the two walks over the
    same trees cannot drift into disagreeing about what they saw.

    Args:
        root: The tree to inspect, exactly as the operator supplied it.

    Returns:
        A :class:`TreeContainment` naming the first escaping entry, if any,
        and every directory the walk was refused.
    """
    resolved_root = root.resolve(strict=False)
    unlistable: list[Path] = []

    def _record(error: OSError) -> None:
        """Record the directory ``scandir`` refused, named as walked.

        Args:
            error: The ``OSError`` ``os.walk`` would otherwise discard. Its
                ``filename`` is the directory that could not be listed.
        """
        unlistable.append(Path(str(error.filename)))

    escaping: Path | None = None
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=_record
    ):
        for entry in itertools.chain(dirnames, filenames):
            candidate = Path(dirpath) / entry
            if escaping is None and escaping_child(candidate, resolved_root):
                escaping = candidate
    return TreeContainment(escaping=escaping, unlistable=tuple(unlistable))


def assert_source_contained(source_path: Path) -> None:
    """Refuse a source tree that links out of itself (#1294).

    The gate every ingestor passes through. Two checks, and the order
    matters:

    1. *source_path* itself, via :func:`named_path_escapes`. This must come
       BEFORE any ``is_dir()``, which follows the link. #1360 found that
       resolving a named symlinked directory launders every child as
       in-root: the tree walk would resolve the root to the link's target
       and then find nothing escaping inside it.
    2. Every descendant, via :func:`inspect_tree`.

    A symlink whose target stays inside the root is admitted, so ordinary
    intra-tree aliases keep working. Containment is about the target
    escaping, not about the link existing.

    **An unlistable subtree is REPORTED, not refused (#1498).** That is a
    deliberate split from the redaction write path's
    ``_assert_no_escaping_symlinks``, which refuses on the same condition,
    and the asymmetry is the whole ruling rather than an oversight:

    * No leak is possible on this arm. A subtree the gate cannot list is one
      the ingestor cannot list either — ``rglob`` yields only the directory
      itself, and :meth:`creek.ingest.code.CodeIngestor._discover_directory`
      takes the ``EACCES`` straight into ``_discover_safe``. What the walk
      loses is a false *guarantee*, not a true *admission*; nothing
      out-of-tree becomes readable.
    * The durable report channel already exists on this path and #1444
      already chose it. ``IngestResult.discovery_complete`` records a failed
      enumeration and ``creek/ingest/pipeline.py`` disarms
      ``tomb_missing_units`` on it. A second, contradictory answer for the
      same physical condition on the same path is exactly the drift this
      module exists to prevent.
    * Refusing here is a measured outage. ``creek sync`` shares the ingest
      path (see ``creek/cli.py``'s advisory policy), so one chmod-000
      ``.Trashes`` or a root-owned ``.git/objects`` would refuse every
      scheduled pass, permanently and silently.

    The escaping-link arm is untouched by that ruling and still raises. The
    residual — permissions relaxing between this walk and the ingestor's —
    is the check-then-act window recorded in
    ``docs/security/threat-model.md``.

    Args:
        source_path: The source file or directory as the caller supplied
            it.

    Raises:
        EscapingSymlinkError: When *source_path* is, or contains, a symlink
            whose target resolves outside the tree the operator named.
    """
    if named_path_escapes(source_path):
        logger.warning(
            "Refusing to ingest a source path that is a symlink escaping "
            "its own parent: %s",
            source_path,
        )
        raise EscapingSymlinkError(source_path, source_path)
    if not source_path.is_dir():
        return
    report = inspect_tree(source_path)
    if report.escaping is not None:
        logger.warning(
            "Refusing to ingest %s: it contains a symlink that escapes the "
            "source root: %s",
            source_path,
            report.escaping,
        )
        raise EscapingSymlinkError(report.escaping, source_path)
    for directory in report.unlistable:
        # Only the directory is named, never anything beneath it: the walk
        # never opened it, and #1087's no-oracle invariant applies to a
        # report just as much as to a refusal.
        logger.warning(
            "Containment under %s could not be proven: %s could not be "
            "listed, so no symlink beneath it was checked. Nothing beneath "
            "it was read either; restore read+execute permission on it to "
            "have it covered.",
            source_path,
            directory,
        )
