"""Dry-run bookkeeping — the writes a preview only pretended to make.

A purge operation is a *sequence* of per-fragment passes over one vault,
and each pass changes what the next pass can still find. An apply run
gets that for free: fragment 1 really unlinks the shared intimate stub
and really rewrites the praxis note, so fragment 2's pass walks the
world fragment 1 left behind. A dry run writes nothing, so fragment 2's
pass walks the *original* world and re-counts work the real run had
already done. The preview then over-reports — the one thing a preview of
an irreversible erasure must never do, because an operator sizes a
right-to-be-forgotten request from it (#1340).

The divergence has two independent causes, and bookkeeping that models
only the first closes only half of it:

* **Deletion-driven.** A counted-only ``unlink`` leaves the file on
  disk, so a later pass finds an intimate stub, a staged source, a
  derived voice artifact or a sibling fragment that the apply run had
  already destroyed — and counts it a second time.
* **Rewrite-driven.** A counted-only ``write_bytes`` leaves the *old
  bytes* on disk, so a later pass matches references the apply run had
  already scrubbed. Nothing is deleted anywhere in this case, which is
  why tracking removed paths alone cannot close it: two fragments
  sharing a title, plus one surviving note carrying two links to that
  title, is enough to make a dry run report four wiki-link removals for
  an apply that does two.

:class:`DryRunLedger` records both — the paths an apply run *would* have
removed and the bytes an apply run *would* have written. The engine
populates it on **every** run but consults it **only** under
``dry_run``, so nothing recorded here can change what a real purge
deletes or rewrites. That gate lives in
:meth:`creek.purge.engine.PurgeEngine._skip_as_removed` and its two
sibling read paths, never in this module: a ledger that enforced its own
irrelevance would have to know which mode it was in, and the safety
property is easier to verify when every query site spells the gate out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _key_for(path: Path) -> str:
    """Return the one spelling this ledger files *path* under.

    Callers hand the ledger the same file under different spellings.
    The engine's vault walks yield unresolved ``rglob`` results built
    from whatever ``--vault`` the operator typed, while its containment
    guards — ``_resolve_pointer_in_vault`` and
    ``_contained_voice_artifact`` — hand over an already-``resolve()``d
    path. Those genuinely differ: on macOS a vault under ``/var``
    resolves to ``/private/var``, so a voice artifact marked under its
    resolved spelling would never match the scrub walk's unresolved
    query, and the ledger would silently do nothing on exactly the path
    it exists to cover.

    Both sides are therefore canonicalised rather than recording the two
    spellings a writer happens to know about. Storing both at write time
    is not enough: a writer that *starts* from a resolved path has only
    one spelling to record, so it cannot anticipate the unresolved
    spelling a later reader will present. Only normalising the query too
    makes the match total.

    The ``resolve()`` this costs on the read path is affordable because
    the read path is dry-run-only — an apply run never queries the
    ledger at all — and because every caller that queries is already
    reading the file's full contents in the same iteration, which dwarfs
    one path syscall.

    Args:
        path: The path being recorded or looked up.

    Returns:
        The resolved spelling, or the literal spelling when the path
        cannot be resolved at all.
    """
    try:
        return str(path.resolve())
    except (OSError, ValueError):
        # The same two failures ``PurgeEngine._resolve_pointer_in_vault``
        # already tolerates, spelled the same way: an embedded NUL raises
        # ``ValueError`` and a resolution loop or unreadable component
        # raises ``OSError``. Frontmatter pointers are operator-authored
        # text, so both are real inputs — and neither may abort the
        # preview of an erasure. Falling back to the literal spelling is
        # the safe direction: writer and reader fall back identically, so
        # such a path still matches itself, and the worst case is a
        # preview that over-counts rather than one that does not happen.
        return str(path)


class DryRunLedger:
    """The writes a dry run only pretended to make, for one operation.

    One instance covers exactly one purge operation. The engine
    re-creates it per operation so a second call on the same engine
    cannot inherit the first call's bookkeeping and hide a file that is
    still sitting on disk.

    Path identity follows :func:`_key_for`: writes and queries are both
    canonicalised, so a file marked under one spelling answers to every
    other spelling of itself.
    """

    def __init__(self) -> None:
        """Start an empty ledger that owns its own records.

        Both containers are per-instance rather than class attributes: a
        shared mutable default would leak one operation's bookkeeping
        into every operation that followed it on any engine.
        """
        self._removed: set[str] = set()
        self._pending_bytes: dict[str, bytes] = {}

    def mark_removed(self, path: Path) -> None:
        """Record that an apply run would have unlinked *path* by now.

        Called in place of the real ``unlink``, so "marked" means
        exactly "an apply run would have destroyed this by this point in
        the operation".

        One accepted imprecision: a *fragment* path is keyed under its
        resolved spelling while ``frag_file.unlink()`` removes the alias.
        So for the exotic ``01-Fragments/a.md -> 09-Reference/shared.md``
        the preview treats ``shared.md`` as gone and its apply twin does
        not, making the dry run under-report by one. That is the safe
        direction, and the obvious repair — unlinking the resolved
        target instead — must **not** be made: it would widen a real
        deletion from the alias to the file behind it.

        Args:
            path: The file the real run deletes at this point.
        """
        self._removed.add(_key_for(path))

    def is_removed(self, path: Path) -> bool:
        """Report whether an apply run would already have unlinked *path*.

        Args:
            path: The file a later pass is about to consider.

        Returns:
            ``True`` when *path* was marked removed earlier in this
            operation, ``False`` for anything this ledger has not seen.
            Defaulting to ``False`` is the safe direction: an
            ``is_removed`` that guessed ``True`` would make a preview
            skip the whole vault and report a flawless, empty erasure.
        """
        # Short-circuit before canonicalising: until the operation's
        # first deletion there is nothing to match, and that is the whole
        # of every single-fragment purge's scrub walk.
        return bool(self._removed) and _key_for(path) in self._removed

    def set_bytes(self, path: Path, data: bytes) -> None:
        """Record the bytes an apply run would have written to *path*.

        Bytes, not text (#948). The reference scrub is a byte-level
        substitution — it has to rewrite a file that is not valid UTF-8,
        and it has to leave CRLF line endings alone — so a ledger that
        could only hold ``str`` would force a second, text-only storage
        path beside it, and a preview and an apply run that disagree
        about which files they can rewrite at all.

        Args:
            path: The file the real run rewrites at this point.
            data: The scrubbed bytes, exactly as an apply run would have
                written them.
        """
        self._pending_bytes[_key_for(path)] = data

    def bytes_for(self, path: Path) -> bytes | None:
        """Return the bytes an apply run would already have written.

        Args:
            path: The file a later pass is about to read.

        Returns:
            The pending bytes, or ``None`` when this operation has not
            rewritten *path* — in which case the caller must read the
            real bytes off disk.
        """
        # Same short-circuit as ``is_removed``, for the same reason.
        if not self._pending_bytes:
            return None
        return self._pending_bytes.get(_key_for(path))
