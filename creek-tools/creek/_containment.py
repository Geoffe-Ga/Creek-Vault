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

That policy is deliberately LEAF-ONLY, so a path reached through an
escaping *ancestor* component (``<root>/linkdir/a.md``, where ``linkdir``
is the link) is admitted by :func:`resolves_within` alone. For a walked
tree that residual is closed by :func:`find_escaping_symlink`, which sees
``linkdir`` in its own right; for a directly-named path it remains the
documented residual recorded in ``docs/security/threat-model.md``.

Security direction here is one-way: every function may only ever cause a
caller to read *less*. There is no waiver — no ``--force``, no environment
variable, no config key — per SEC-003's no-waiver precedent.
"""

from __future__ import annotations

import itertools
import logging
import os
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


def resolves_within(child: Path, resolved_root: Path) -> bool:
    """Report whether *child*'s symlink target stays under *resolved_root*.

    The single containment predicate for the whole codebase: the redaction
    scanner's walked-child surface, the redaction CLI's named-path surface,
    and the ingest discovery gate all call this one function, so they cannot
    drift into subtly different definitions of "inside".

    The failure arm is a deliberate classification, not a swallowed error:
    a loop (``RuntimeError``), an unreadable link (``OSError``), and a
    target outside the root (``ValueError`` from ``relative_to``) are all
    cases where containment cannot be proven — and an unprovable
    containment is an escape. Every caller logs and counts its rejections.

    ``strict=False`` is deliberate: a dangling link still resolves to a
    candidate location worth comparing, so a broken link pointing *outside*
    is still refused rather than waved through on the technicality that its
    target does not exist yet.

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
        child.resolve(strict=False).relative_to(resolved_root)
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


def find_escaping_symlink(root: Path) -> Path | None:
    """Return the first descendant of *root* that links out of it, if any.

    Walks with ``followlinks=False`` so the walk itself never descends
    through a link, and inspects BOTH ``dirnames`` and ``filenames``.
    Checking directory entries is what closes
    :meth:`creek.ingest.code.CodeIngestor._discover_directory`, which
    recurses manually with ``is_dir()`` — and ``is_dir()`` follows symlinks,
    so unlike the ``rglob`` ingestors it would otherwise walk an entire
    out-of-tree subtree.

    Returning the offender rather than raising is what lets the two callers
    with different refusal mechanics — ``typer.Exit`` in
    :mod:`creek.redact.cli_commands`, :class:`EscapingSymlinkError` in
    :func:`assert_source_contained` — share one walk instead of keeping two
    copies of it.

    Args:
        root: The tree to inspect, exactly as the operator supplied it.

    Returns:
        The first escaping entry found, as walked, or ``None`` when every
        symlink under *root* resolves inside it.
    """
    resolved_root = root.resolve(strict=False)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for entry in itertools.chain(dirnames, filenames):
            candidate = Path(dirpath) / entry
            if candidate.is_symlink() and not resolves_within(candidate, resolved_root):
                return candidate
    return None


def assert_source_contained(source_path: Path) -> None:
    """Refuse a source tree that links out of itself (#1294).

    The gate every ingestor passes through. Two checks, and the order
    matters:

    1. *source_path* itself, via :func:`named_path_escapes`. This must come
       BEFORE any ``is_dir()``, which follows the link. #1360 found that
       resolving a named symlinked directory launders every child as
       in-root: the tree walk would resolve the root to the link's target
       and then find nothing escaping inside it.
    2. Every descendant, via :func:`find_escaping_symlink`.

    A symlink whose target stays inside the root is admitted, so ordinary
    intra-tree aliases keep working. Containment is about the target
    escaping, not about the link existing.

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
    escaping = find_escaping_symlink(source_path)
    if escaping is not None:
        logger.warning(
            "Refusing to ingest %s: it contains a symlink that escapes the "
            "source root: %s",
            source_path,
            escaping,
        )
        raise EscapingSymlinkError(escaping, source_path)
