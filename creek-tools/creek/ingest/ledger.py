"""Per-source ingest ledger (issue #672 / SPEC R1).

Maps a stable ``source_key`` — the vault-relative path of a *mutable* source
unit (e.g. a journal ``.md``) — to the fragment it produced, the content hash
at last ingest, and a timestamp. Persisted as append-only JSONL under
``00-Creek-Meta/State/ingest/<source>.jsonl`` so a later ingest can decide
**unchanged / changed / gone** for that unit.

This module is the skeleton seam: it only *records* and *reads back*. The
load-bearing decisions — update-in-place on a changed unit, soft-tomb on a
vanished one — land in issues #673 and #674. Append-only event sources
(Discord/chat exports) keep their content-hashed ids and never touch the
ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek.ingest.source_unit import SOURCE_UNIT_SEPARATOR

if TYPE_CHECKING:
    from collections.abc import Iterable

_LEDGER_RELPARTS: tuple[str, ...] = ("00-Creek-Meta", "State", "ingest")

_CORRUPT_BYTE_POLICY: Final[str] = "surrogateescape"
"""How the erasure path decodes a ledger byte that is not valid UTF-8.

One constant because read and write must agree: ``surrogateescape``
smuggles an undecodable byte through as a lone surrogate and emits the
original byte again on the way out, so a corrupt line that survives the
erasure round-trips unchanged. Diverge the two and the rewrite would
either crash or silently rewrite somebody's bytes.
"""


@dataclass(frozen=True)
class LedgerRecord:
    """One source unit's last-known ingest state.

    Attributes:
        source_key: Stable vault-relative identity of the source unit.
        fragment_id: The fragment id that unit currently maps to.
        content_hash: SHA-256 of the source content at last ingest.
        last_seen: ISO-8601 timestamp of the last ingest of this unit.
        tombed: ``True`` when the source unit has vanished and its fragment
            was soft-tombed to ``10-Liminal/Orphaned/`` (#674). Excluded from
            :meth:`SourceLedger.live_keys`; a re-appearance restores it.
    """

    source_key: str
    fragment_id: str
    content_hash: str
    last_seen: str
    tombed: bool = False


def _loads_object(line: str) -> dict[str, object] | None:
    """Parse one JSONL line into a raw object, or ``None``.

    Stops short of :meth:`SourceLedger._record_from_dict`'s completeness
    check on purpose. The erasure pass in :func:`forget_fragment_ids` has
    to recognise a row by its ``fragment_id`` even when the row is too
    damaged to be a valid mapping — a truncated line still names the
    fragment in cleartext, and the whole point of an erasure is that the
    name goes away.

    Args:
        line: One raw line of a ledger file.

    Returns:
        The parsed JSON object, or ``None`` when the line is blank,
        not valid JSON, or not an object.
    """
    text = line.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class SourceLedger:
    """Append-only JSONL ledger of source-unit -> fragment mappings.

    Loaded once per ingest run via :meth:`load`; :meth:`record` appends a
    line (crash-safe) and updates the in-memory view. On reload the latest
    line per ``source_key`` wins, so re-ingests accumulate harmlessly and
    resolve to current state.
    """

    def __init__(self, path: Path, records: dict[str, LedgerRecord]) -> None:
        """Bind a ledger to its backing file and pre-loaded records."""
        self._path = path
        self._records = records

    @staticmethod
    def path_for(vault_path: Path, source: str) -> Path:
        """Return the ledger file path for *source* under the vault meta dir."""
        return vault_path.joinpath(*_LEDGER_RELPARTS, f"{source}.jsonl")

    @staticmethod
    def content_hash(content: str) -> str:
        """Return a stable SHA-256 hex digest of *content*."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, vault_path: Path, *, source: str) -> SourceLedger:
        """Load the ledger for *source*, resolving last-write-wins per key.

        A missing file yields an empty ledger. Corrupt or non-object lines
        are skipped so one bad line never aborts the load.

        Args:
            vault_path: Vault root containing ``00-Creek-Meta/``.
            source: Source/ingestor key (e.g. ``"markdown"``).

        Returns:
            A :class:`SourceLedger` bound to the resolved records.
        """
        path = cls.path_for(vault_path, source)
        records: dict[str, LedgerRecord] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                record = cls._parse_line(line)
                if record is not None:
                    records[record.source_key] = record
        return cls(path, records)

    @staticmethod
    def _nonempty_str(value: object) -> str | None:
        """Return *value* when it is a non-empty string, else ``None``."""
        return value if isinstance(value, str) and value else None

    @classmethod
    def _record_from_dict(cls, data: dict[str, object]) -> LedgerRecord | None:
        """Build a record from a parsed object, requiring every field.

        A record is only accepted when ``source_key``, ``fragment_id``,
        ``content_hash`` and ``last_seen`` are all present, non-empty
        strings. Rejecting partial rows keeps a truncated or hand-edited
        line from masquerading as a valid mapping — an empty ``content_hash``
        would otherwise make the changed-check in #673 read every re-ingest
        as "changed".
        """
        source_key = cls._nonempty_str(data.get("source_key"))
        fragment_id = cls._nonempty_str(data.get("fragment_id"))
        content_hash = cls._nonempty_str(data.get("content_hash"))
        last_seen = cls._nonempty_str(data.get("last_seen"))
        if (
            source_key is None
            or fragment_id is None
            or content_hash is None
            or last_seen is None
        ):
            return None
        return LedgerRecord(
            source_key=source_key,
            fragment_id=fragment_id,
            content_hash=content_hash,
            last_seen=last_seen,
            tombed=bool(data.get("tombed", False)),
        )

    @classmethod
    def _parse_line(cls, line: str) -> LedgerRecord | None:
        """Parse one JSONL line into a record, or ``None`` if malformed."""
        data = _loads_object(line)
        if data is None:
            return None
        return cls._record_from_dict(data)

    def get(self, source_key: str) -> LedgerRecord | None:
        """Return the current record for *source_key*, or ``None``."""
        return self._records.get(source_key)

    def live_keys(self) -> set[str]:
        """Return the tracked source keys that are not tombed (#674)."""
        return {key for key, rec in self._records.items() if not rec.tombed}

    def all_keys(self) -> set[str]:
        """Return every tracked source key, tombed or not (#1305).

        The counterpart to :meth:`live_keys`, for the readers that must not
        treat a tombed record as absence. A soft-tombed unit's fragment was
        moved to ``10-Liminal/Orphaned/``, not deleted, so a gate deciding
        whether an identity *owns anything* has to count it — ``live_keys``
        answers a different question, and answering this one with it is how
        a gate comes to admit a caller to content that still exists.

        Returns:
            All tracked source keys.
        """
        return set(self._records)

    def records_for(self, base_key: str) -> list[tuple[str, LedgerRecord]]:
        """Return every record keyed on *base_key* or a sub-unit of it (#1305).

        :meth:`get` answers "what is this file's fragment", which stopped
        being a question with one answer once one file could hold several
        independently identified units — the sheets of a workbook. A caller
        holding only the file's path (the ``creek.upload`` tool holds a
        staged path, never a sheet name) has no way to enumerate the units
        without this.

        Both the exact key and its ``<base_key>#<unit>`` children are
        returned, so a single-unit source still answers with its one record
        and callers need no special case for the two shapes.

        The ``#`` scan cannot mis-fire for the upload ledger, its only
        caller: staged filenames come from ``creek_mcp.staged_names.safe_stem``,
        whose slug regex excludes ``#`` outright, so no staged path can look
        like a composed key.

        Args:
            base_key: The whole file's source key.

        Returns:
            ``(source_key, record)`` pairs ordered by key, so a caller that
            reduces the list to one id — a response's ``fragment_id`` —
            picks the same one on every run.
        """
        prefix = f"{base_key}{SOURCE_UNIT_SEPARATOR}"
        return sorted(
            (key, record)
            for key, record in self._records.items()
            if key == base_key or key.startswith(prefix)
        )

    def __len__(self) -> int:
        """Return the number of distinct source keys tracked."""
        return len(self._records)

    def __contains__(self, source_key: object) -> bool:
        """Return whether *source_key* is tracked."""
        return source_key in self._records

    def record(
        self,
        source_key: str,
        fragment_id: str,
        content_hash: str,
        *,
        last_seen: str | None = None,
        tombed: bool = False,
    ) -> LedgerRecord:
        """Upsert a source unit's state, appending a line and updating memory.

        Args:
            source_key: Stable vault-relative identity of the source unit.
            fragment_id: The fragment id it currently maps to.
            content_hash: SHA-256 of the source content (see
                :meth:`content_hash`).
            last_seen: ISO timestamp; defaults to the current UTC time.
            tombed: Whether the unit is soft-tombed (vanished from source).

        Returns:
            The :class:`LedgerRecord` that was written.
        """
        record = LedgerRecord(
            source_key=source_key,
            fragment_id=fragment_id,
            content_hash=content_hash,
            last_seen=last_seen or datetime.now(UTC).isoformat(),
            tombed=tombed,
        )
        self._records[source_key] = record
        self._append(record)
        return record

    def _append(self, record: LedgerRecord) -> None:
        """Append one record as a JSONL line, creating parents as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "source_key": record.source_key,
                "fragment_id": record.fragment_id,
                "content_hash": record.content_hash,
                "last_seen": record.last_seen,
                "tombed": record.tombed,
            },
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")


def ledger_dir(vault_path: Path) -> Path:
    """Return the directory holding every per-source ledger file.

    This module owns :data:`_LEDGER_RELPARTS`; callers that need the
    location — the purge engine, the docs pin — ask here rather than
    restating the layout, so moving the ledger moves one constant.

    Args:
        vault_path: Vault root containing ``00-Creek-Meta/``.

    Returns:
        The ``00-Creek-Meta/State/ingest/`` directory (which need not
        exist).
    """
    return vault_path.joinpath(*_LEDGER_RELPARTS)


def _doomed_source_keys(lines: list[str], doomed_ids: frozenset[str]) -> set[str]:
    """Collect the source keys belonging to any doomed fragment id.

    The first of two passes. A source unit accumulates one appended row
    per ingest, each carrying that revision's id and content hash, so
    matching on ``fragment_id`` alone leaves every superseded row on
    disk — and those rows hold hashes of *earlier drafts of the same
    private text*, plus the source path, which is the join the erasure
    exists to break.

    Args:
        lines: Raw lines of one ledger file.
        doomed_ids: Fragment ids being erased.

    Returns:
        Every ``source_key`` that any doomed row names.
    """
    keys: set[str] = set()
    for line in lines:
        data = _loads_object(line)
        if data is None or data.get("fragment_id") not in doomed_ids:
            continue
        key = data.get("source_key")
        if isinstance(key, str) and key:
            keys.add(key)
    return keys


def _text_shapes(value: str) -> tuple[str, str]:
    """Both shapes *value* can take inside a raw ledger line.

    :meth:`SourceLedger._append` serialises with ``json.dumps`` at its
    default ``ensure_ascii=True``, so a value holding any non-ASCII
    character reaches disk **escaped** — ``café.md`` is stored as the
    twelve characters ``caf\\u00e9.md`` — while :func:`_doomed_source_keys`
    recovers it *decoded* through ``json.loads``. Matching one shape
    against the other silently misses, and vault source keys are note
    titles: accents, CJK and emoji are the norm, not the exception. The
    same divergence applies to an embedded ``"`` or ``\\``.

    Args:
        value: A fragment id or source key being searched for.

    Returns:
        The decoded value and its on-disk JSON-escaped spelling. For a
        plain ASCII value the two are identical, so this adds no
        over-match surface.
    """
    return value, json.dumps(value)[1:-1]


def _line_is_doomed(
    line: str,
    doomed_ids: frozenset[str],
    doomed_keys: set[str],
) -> bool:
    """Report whether one raw ledger line must not survive the erasure.

    An unparseable line is matched as text, against ids *and* source
    keys, in both of the shapes :func:`_text_shapes` describes. It is
    the case that matters most: a half-written row is exactly the shape
    a crash leaves behind, and no structured matcher can see it. Keys
    are needed as well as ids because :meth:`SourceLedger._append`
    serialises ``source_key`` before ``fragment_id`` — a truncation
    landing between the two leaves a line naming the doomed source path
    with no id on it at all, and the path is the join the erasure exists
    to break.

    Substring matching can over-match, but here it is costless rather
    than merely acceptable: the only lines reaching this branch are the
    ones :func:`_loads_object` rejected, which is the same set
    :meth:`SourceLedger._parse_line` discards on load. An over-matched
    line is by construction a line no reader could have used.

    Args:
        line: One raw line of the ledger file.
        doomed_ids: Fragment ids being erased.
        doomed_keys: Source keys any doomed row named.

    Returns:
        ``True`` when the line names a doomed id or a doomed source key.
    """
    data = _loads_object(line)
    if data is None:
        return any(
            shape in line
            for value in (*doomed_ids, *doomed_keys)
            for shape in _text_shapes(value)
        )
    return (
        data.get("fragment_id") in doomed_ids or data.get("source_key") in doomed_keys
    )


def _rewrite_atomically(path: Path, lines: list[str]) -> None:
    """Replace *path*'s contents with *lines*, atomically.

    Temp file in the same directory plus ``os.replace``, so a crash
    mid-write leaves either the whole old ledger or the whole new one —
    never a truncated file whose surviving bytes are an arbitrary prefix
    of somebody's erased source path.

    Durability scope: atomicity here is against ordinary process death,
    not power loss. Neither the temp file nor the parent directory is
    ``fsync``-ed, so bytes ``os.replace`` has already committed may still
    be sitting in the OS page cache. This matches the ledger's own
    append path (:meth:`SourceLedger._append` does not fsync either) and
    the same explicit carve-out in :mod:`creek.vault.writer`. The
    fsync-ing writers in this tree each fsync for a reason this function
    does not share: :class:`creek.audit.AuditLog` because its records
    *are* the tamper-evidence,
    :func:`creek.confidential.keyvault.save_key_vault` because the vault
    is the only copy and a torn write turns a recoverable state into
    permanent lockout, and :func:`creek.ingest.gdrive._secure_erase`
    because an overwrite-before-unlink is worthless until the bytes
    reach the platter. Widening this to fsync is a change to the whole
    ledger's durability contract, not to this function.

    Encode errors are handled with ``surrogateescape``, mirroring
    :func:`_forget_in_file`'s read, so an undecodable byte on a
    surviving line is emitted as the original byte rather than as a
    substitution character. Line *framing* is not preserved and never
    was: the read translates universal newlines and ``splitlines`` cuts
    on more separators than ``\\n``, so every survivor is re-joined with
    a bare ``\\n``.

    Args:
        path: The ledger file to rewrite.
        lines: The surviving lines, without trailing newlines.
    """
    handle, raw_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.name}.",
        suffix=".tmp",
    )
    tmp_path = path.with_name(Path(raw_name).name)
    try:
        with os.fdopen(
            handle,
            "w",
            encoding="utf-8",
            errors=_CORRUPT_BYTE_POLICY,
        ) as stream:
            stream.writelines(f"{line}\n" for line in lines)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _forget_in_file(
    path: Path,
    doomed_ids: frozenset[str],
    *,
    dry_run: bool,
) -> int:
    """Erase every doomed row from one ledger file.

    The read is decoded with ``surrogateescape``, not strict UTF-8. A
    single corrupt byte anywhere under the ledger directory would
    otherwise raise :exc:`UnicodeDecodeError` and abort the erasure —
    and because :func:`forget_fragment_ids` walks the files in sorted
    order, one bad file would spare every file after it too. That is the
    exact veto the scoped purge reorders itself to deny a corrupt
    embeddings cache, so the ledger must not hand it back. Undecodable
    bytes are preserved rather than dropped, and mirrored on write by
    :func:`_rewrite_atomically`, so the byte itself survives a rewrite
    unchanged instead of being silently mangled into ``U+FFFD``.

    Args:
        path: One ``<source>.jsonl`` ledger file.
        doomed_ids: Fragment ids being erased.
        dry_run: Count what would be erased and write nothing.

    Returns:
        The number of rows erased (or that would be).
    """
    lines = path.read_text(
        encoding="utf-8",
        errors=_CORRUPT_BYTE_POLICY,
    ).splitlines()
    doomed_keys = _doomed_source_keys(lines, doomed_ids)
    survivors: list[str] = []
    removed = 0
    for line in lines:
        if not line.strip():
            continue
        if _line_is_doomed(line, doomed_ids, doomed_keys):
            removed += 1
        else:
            survivors.append(line)
    if removed == 0 or dry_run:
        return removed
    if survivors:
        _rewrite_atomically(path, survivors)
    else:
        path.unlink()
    return removed


def forget_fragment_ids(
    vault_path: Path,
    fragment_ids: Iterable[str],
    *,
    dry_run: bool = False,
) -> int:
    """Physically erase every ledger row naming any of *fragment_ids* (#1453).

    The ledger is otherwise append-only, and deliberately so: it is an
    event log whose last line per key wins. An erasure cannot be
    expressed in that vocabulary. Appending a tombstone leaves the
    original row — source path, fragment id and a full unsalted SHA-256
    of the body — verbatim on disk, and blanking a row in place is worse
    than useless: :meth:`SourceLedger._record_from_dict` rejects an empty
    ``content_hash``, so the row becomes invisible to every reader while
    still spelling the fragment id in cleartext. The row is therefore
    physically removed, and a file left with no rows is unlinked rather
    than left as a husk naming its source.

    Args:
        vault_path: Vault root containing ``00-Creek-Meta/``.
        fragment_ids: The ids being erased. Empty strings are ignored;
            an empty collection is a no-op, never a wildcard.
        dry_run: Count the rows that would be erased and write nothing,
            leaving every ledger byte identical.

    Returns:
        The number of rows erased (or that would be, in a dry run).
    """
    doomed = frozenset(
        fragment_id for fragment_id in fragment_ids if fragment_id.strip()
    )
    if not doomed:
        return 0
    directory = ledger_dir(vault_path)
    if not directory.is_dir():
        return 0
    return sum(
        _forget_in_file(path, doomed, dry_run=dry_run)
        for path in sorted(directory.glob("*.jsonl"))
    )
