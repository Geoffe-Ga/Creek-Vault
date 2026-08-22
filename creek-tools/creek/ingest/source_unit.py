"""Compose and split the sub-unit half of a source key (#1305).

Most ingestors map one source *file* to one fragment, so the file's path is
a sufficient identity. Some do not: ``SpreadsheetIngestor`` emits one
fragment per sheet of a workbook, and every one of those sheets shares a
single file. Until #1305 they also shared a single fragment id, a single
provenance entry and a single ledger record, so ``_write_model``'s
first-writer-wins dedup (``creek/vault/writer.py``) silently dropped every
sheet after the first.

The fix is to give such a fragment a *sub-unit* discriminator, and this
module owns the one spelling of how that discriminator is joined to and
recovered from a path. Three unrelated layers have to agree on it — the
ingest derivation that mints it, the upload ledger that stores it, and the
RTBF purge sweep that has to find the underlying file again — and a
separator that three modules each spell for themselves is a separator that
will eventually be spelled two ways.

Why ``#``
---------
It is not legal in a ``safe_stem`` slug (``creek_mcp.tools.upload``'s
``_SLUG_RE`` is ``[^A-Za-z0-9._-]+``), so no staged upload path can contain
one and the upload ledger's key space stays unambiguous. It is legal in a
POSIX filename, though, which is why :func:`split_source_unit` is documented
as a *hypothesis* rather than a parse: a caller must try the whole key first
and fall back to the split only when the whole key resolves to nothing.

Deliberately free of heavy imports. :mod:`creek.purge.engine` and
:mod:`creek_mcp.tools.upload` both need it, and neither should be made to
pull :mod:`creek.classify` in behind it.
"""

from __future__ import annotations

from typing import Final

SOURCE_UNIT_SEPARATOR: Final[str] = "#"
"""Joins a source path to the sub-unit of it a fragment addresses."""

_UNIT_SEPARATOR_REPLACEMENT: Final[str] = "-"
"""What a separator inside a unit name is rewritten to. See :func:`sanitize_unit`."""


def sanitize_unit(unit: str) -> str:
    """Return *unit* with every separator rewritten, so it cannot be ambiguous.

    The separator is legal in the things units are named after — Excel
    permits ``#`` in a sheet title — and a unit containing one produces a
    key that cannot be read back. ``book.xlsx#Rev#2`` splits at the *last*
    separator to ``("book.xlsx#Rev", "2")``, naming a file that does not
    exist; the RTBF purge sweep then finds no staged source and silently
    leaves the operator's document on disk.

    Rewriting at mint time is the fix, rather than teaching every reader to
    try successive splits: it makes "the unit half of a composed key never
    contains a separator" an invariant of the key space instead of a
    property each reader has to rediscover. One rpartition is then exact
    for every key this module produced.

    Callers that derive a unit from user-controlled text must apply this
    **before** de-duplicating, not after — two distinct names that sanitize
    to the same string (``Rev#2`` and ``Rev-2``) must be seen as a
    collision by the de-duplicator, or they silently mint one id for two
    units, which is the very defect #1305 exists to fix.

    Args:
        unit: The raw unit name.

    Returns:
        *unit* with each separator replaced by
        :data:`_UNIT_SEPARATOR_REPLACEMENT`.
    """
    return unit.replace(SOURCE_UNIT_SEPARATOR, _UNIT_SEPARATOR_REPLACEMENT)


def compose_source_unit(path: str, unit: str | None) -> str:
    """Return the source key addressing *unit* within *path*.

    The unit is passed through :func:`sanitize_unit` so a composed key can
    always be split back apart. That is enforced here, at the single place
    keys are minted, rather than trusted to each caller.

    Args:
        path: The source key or path of the whole file.
        unit: The sub-unit discriminator, or ``None`` for the whole file.

    Returns:
        ``path`` unchanged when *unit* is ``None`` or empty, else
        ``f"{path}#{sanitized}"``. An empty unit is treated as no unit
        deliberately: a key ending in a bare separator would be a second
        spelling of "the whole file", and two spellings of one identity is
        how the same fragment comes to be written twice.
    """
    if not unit:
        return path
    return f"{path}{SOURCE_UNIT_SEPARATOR}{sanitize_unit(unit)}"


def split_source_unit(key: str) -> tuple[str, str | None]:
    """Split *key* into its path half and its sub-unit half, if it has one.

    This is a **hypothesis, not a parse**. ``#`` is a legal character in a
    POSIX filename, so ``report#1.xlsx`` is indistinguishable from unit
    ``1.xlsx`` of a file named ``report``. Callers must therefore try the
    whole key first and consult this only when that resolved to nothing —
    which is exactly what the purge engine's staged-source sweep does, so
    a genuinely ``#``-named file keeps winning.

    Args:
        key: A source key, with or without a sub-unit suffix.

    Returns:
        ``(path, unit)``, with *unit* ``None`` when *key* carries no
        separator or when the text after the last separator is empty
        (mirroring :func:`compose_source_unit`, which never mints one).
    """
    path, separator, unit = key.rpartition(SOURCE_UNIT_SEPARATOR)
    if not separator or not unit:
        return key, None
    return path, unit
