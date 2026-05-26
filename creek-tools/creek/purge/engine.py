"""Purge engine — right-to-be-forgotten deletion operations.

Implements the five purge operations required by issue #46:

- **purge_fragment**: delete a single fragment and clean all references.
- **purge_source**: delete every fragment from a given source platform.
- **purge_classifications**: reset classification fields to unclassified.
- **purge_daterange**: delete fragments created within a date range.
- **purge_vault**: destroy all vault content, preserving folder structure.

Every operation supports a dry-run mode that previews changes without
writing to disk. Each call emits a **pair** of audit entries (GAP-002):
an ``intent`` line *before* the first destructive op, then an
``outcome`` line *after* the body completes (``status="complete"``) or
after an exception aborts it (``status="partial"``). The pair shares a
UUID4 ``operation_id`` so a crash recovery tool can reconstruct what
was being attempted from the intent line alone.

The engine is **not** transactional: a SIGKILL between two ``unlink``
calls leaves the filesystem in a partial state. The intent log is the
recovery contract — an operator inspecting
``<vault>/00-Creek-Meta/audit/purge.jsonl`` after a crash will see the
intent entry naming what was being attempted, and (if the process got
that far) an outcome entry with ``status="partial"`` recording how far
the work had progressed. A staging-directory rename pattern would
deliver real atomicity at the cost of doubling the I/O budget for
every purge; it is deferred to a future hardening pass.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import frontmatter
from pydantic import BaseModel, Field

from creek.purge.audit import PurgeAuditEntry, PurgeAuditLog, PurgeOutcomeStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

_VAULT_CONTENT_FOLDERS: tuple[str, ...] = (
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
)
"""Top-level vault folders whose contents can be purged by ``purge_vault``."""

VAULT_PURGE_CONFIRMATION = "I understand this is irreversible"
"""Exact phrase required to confirm a full-vault purge."""

_CLASSIFICATION_RESET_FIELDS: tuple[str, ...] = (
    "frequency",
    "wavelength",
    "voice",
)
"""Frontmatter fields wiped by ``purge_classifications``."""

_FILE_DELETING_OPERATIONS: frozenset[str] = frozenset(
    {"fragment", "source", "source-path", "daterange", "vault"},
)
"""Explicit allowlist of operations that remove files from disk.

Operations not listed here (currently only ``classifications``, which
resets metadata in place) record ``fragments_deleted=0`` regardless of
``fragments_affected``. Any new operation type added to this engine
must be classified explicitly here — defaulting unknown operations to
"deletes files" would risk over-counting deletions in audit entries.
"""


def _str_list(value: object) -> list[str]:
    """Coerce a frontmatter list value into a list of strings.

    Args:
        value: Raw value from a :class:`frontmatter.Post` field.

    Returns:
        A list of strings; empty when *value* is not a list.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


class PurgeResult(BaseModel):
    """Outcome of a single purge operation.

    Attributes:
        operation: Name of the operation (``fragment``, ``source``, ...).
        target: The purge target (fragment id, source type, date range, ...).
        criteria: Structured representation of *target* used for the
            audit log (e.g. ``{"fragment_id": "frag-abc"}``).
        affected_fragment_ids: IDs of fragments touched by the operation.
        dry_run: Whether deletions were only previewed.
        deleted_files: Paths of files deleted (or that would be deleted).
        fragments_affected: Number of fragments directly affected.
        wikilinks_removed: Number of wiki-link references scrubbed.
        threads_updated: Thread files whose metadata changed.
        eddies_updated: Eddy files whose metadata changed.
        classifications_reset: Fragments whose classifications were wiped.
        embeddings_removed: Real count of embedding cache rows that were
            removed (or would be removed in a dry run). Zero for
            metadata-only operations and for runs where the cache had
            no matching rows (GAP-001).
    """

    operation: str
    target: str
    criteria: dict[str, object] = Field(default_factory=dict)
    affected_fragment_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    deleted_files: list[str] = Field(default_factory=list)
    fragments_affected: int = 0
    wikilinks_removed: int = 0
    threads_updated: int = 0
    eddies_updated: int = 0
    classifications_reset: int = 0
    embeddings_removed: int = 0


class PurgeEngine:
    """Execute right-to-be-forgotten deletions against a vault.

    Every public method returns a :class:`PurgeResult` summarising what
    changed (or would change, when ``dry_run=True``) and appends an
    entry to the audit log.

    Args:
        vault_path: Path to the Obsidian vault root.
        dry_run: When ``True``, no filesystem writes occur; results
            describe the hypothetical outcome.
    """

    def __init__(self, vault_path: Path, *, dry_run: bool = False) -> None:
        """Initialise the engine with the target vault path.

        Args:
            vault_path: Path to the Obsidian vault root.
            dry_run: If ``True``, preview changes without applying them.
        """
        self.vault_path = vault_path
        self.dry_run = dry_run
        self.audit_log = PurgeAuditLog(vault_path)

    # -- Fragment purge ---------------------------------------------------

    def purge_fragment(self, fragment_id: str) -> PurgeResult:
        """Delete a fragment and remove every reference to it.

        Searches ``01-Fragments/`` for a markdown file whose frontmatter
        ``id`` matches ``fragment_id``. If found, the file is deleted,
        all wiki-links pointing at its title are scrubbed from every
        markdown file in the vault, and the ``fragment_count`` on any
        thread/eddy listed in its frontmatter is decremented.

        Args:
            fragment_id: The fragment's unique ID (e.g. ``frag-abc123``).

        Returns:
            A :class:`PurgeResult` describing the deletion.
        """
        result = PurgeResult(
            operation="fragment",
            target=fragment_id,
            criteria={"fragment_id": fragment_id},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the per-fragment destructive ops, mutating ``result``."""
            frag_file, post = self._find_fragment_by_id(fragment_id)
            if frag_file is None or post is None:
                return
            title = str(post.get("title", frag_file.stem))
            thread_ids = _str_list(post.get("threads"))
            eddy_ids = _str_list(post.get("eddies"))
            result.deleted_files.append(str(frag_file))
            result.affected_fragment_ids.append(fragment_id)
            result.fragments_affected = 1
            result.wikilinks_removed = self._scrub_wikilinks(
                title,
                exclude=frag_file,
            )
            result.threads_updated = self._decrement_counts(
                "02-Threads",
                thread_ids,
            )
            result.eddies_updated = self._decrement_counts(
                "03-Eddies",
                eddy_ids,
            )
            if not self.dry_run:
                frag_file.unlink()
            result.embeddings_removed = self._purge_cache_for(
                result.affected_fragment_ids,
            )

        return self._run_audited(result, body)

    # -- Source purge -----------------------------------------------------

    def purge_source(self, source_type: str) -> PurgeResult:
        """Delete every fragment ingested from a given source platform.

        Args:
            source_type: Source platform identifier
                (e.g. ``claude``, ``discord``).

        Returns:
            A :class:`PurgeResult` summarising the deletions.
        """
        result = PurgeResult(
            operation="source",
            target=source_type,
            criteria={"source_type": source_type},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the per-source destructive ops, mutating ``result``."""
            matches = self._fragments_from_source(source_type)
            for frag_file, post in matches:
                self._purge_single(frag_file, post, result)
            result.embeddings_removed = self._purge_cache_for(
                result.affected_fragment_ids,
            )

        return self._run_audited(result, body)

    def count_fragments_from_source(self, source_type: str) -> int:
        """Count fragments currently attributed to a source platform.

        Args:
            source_type: Source platform identifier.

        Returns:
            Number of matching fragment files.
        """
        return len(self._fragments_from_source(source_type))

    def purge_source_path(
        self,
        source_path: str,
        *,
        match: str = "exact",
    ) -> PurgeResult:
        """Delete every fragment whose ``source.original_file`` matches (INC-008).

        Args:
            source_path: The path (or substring / regex) to match against
                each fragment's ``source.original_file``.
            match: ``"exact"`` (full-string equality, default),
                ``"substring"`` (Python ``in``), or ``"regex"``
                (re.search). An invalid regex raises ``ValueError``
                fast rather than silently mismatching.

        Returns:
            A :class:`PurgeResult` summarising the deletions and
            recording the match mode in its criteria.

        Raises:
            ValueError: When ``match`` is unrecognised, or when
                ``match="regex"`` and ``source_path`` is not a valid
                regex.
        """
        result = PurgeResult(
            operation="source-path",
            target=source_path,
            criteria={"source_path": source_path, "match": match},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the source-path destructive ops, mutating ``result``."""
            matches = self._fragments_from_source_path(source_path, match=match)
            for frag_file, post in matches:
                self._purge_single(frag_file, post, result)
            result.embeddings_removed = self._purge_cache_for(
                result.affected_fragment_ids,
            )

        return self._run_audited(result, body)

    def count_fragments_from_source_path(
        self,
        source_path: str,
        *,
        match: str = "exact",
    ) -> int:
        """Count fragments whose ``source.original_file`` matches (INC-008).

        Args:
            source_path: The path / substring / regex to match.
            match: One of ``"exact"`` / ``"substring"`` / ``"regex"``.

        Returns:
            Number of matching fragment files.
        """
        return len(self._fragments_from_source_path(source_path, match=match))

    # -- Classifications purge -------------------------------------------

    def purge_classifications(self) -> PurgeResult:
        """Reset classification fields on every fragment to unclassified.

        Preserves source metadata, timestamps, threads, eddies, and body
        content; only ``frequency``, ``wavelength``, and ``voice``
        frontmatter blocks are reset to defaults.

        Returns:
            A :class:`PurgeResult` recording how many fragments changed.
        """
        result = PurgeResult(
            operation="classifications",
            target="all",
            criteria={"scope": "all"},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Reset classification fields on every fragment, mutating ``result``."""
            for frag_file in self._list_fragment_files():
                post = self._load_frontmatter(frag_file)
                if post is None:
                    continue
                if self._reset_classifications(post):
                    result.classifications_reset += 1
                    frag_id = post.get("id")
                    if isinstance(frag_id, str):
                        result.affected_fragment_ids.append(frag_id)
                    if not self.dry_run:
                        frag_file.write_text(
                            frontmatter.dumps(post),
                            encoding="utf-8",
                        )
            result.fragments_affected = result.classifications_reset

        return self._run_audited(result, body)

    # -- Daterange purge -------------------------------------------------

    def purge_daterange(
        self,
        start: date,
        end: date,
    ) -> PurgeResult:
        """Delete fragments whose ``created`` date falls within a range.

        Args:
            start: Inclusive start date (UTC).
            end: Inclusive end date (UTC).

        Returns:
            A :class:`PurgeResult` summarising the deletions.

        Raises:
            ValueError: If ``end`` is before ``start``.
        """
        if end < start:
            msg = f"End date {end} is before start date {start}"
            raise ValueError(msg)

        target = f"{start.isoformat()}..{end.isoformat()}"
        result = PurgeResult(
            operation="daterange",
            target=target,
            criteria={"start": start.isoformat(), "end": end.isoformat()},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the date-range destructive ops, mutating ``result``."""
            for frag_file in self._list_fragment_files():
                post = self._load_frontmatter(frag_file)
                if post is None:
                    continue
                created = _coerce_date(post.get("created"))
                if created is None or not (start <= created <= end):
                    continue
                self._purge_single(frag_file, post, result)
            result.embeddings_removed = self._purge_cache_for(
                result.affected_fragment_ids,
            )

        return self._run_audited(result, body)

    # -- Vault purge ------------------------------------------------------

    def purge_vault(self, confirmation: str) -> PurgeResult:
        """Destroy every fragment, thread, eddy, and related file.

        Folder structure is preserved; only the *contents* of the
        top-level vault content folders are removed. The purge log
        itself is preserved.

        Args:
            confirmation: Must equal :data:`VAULT_PURGE_CONFIRMATION`
                exactly.

        Returns:
            A :class:`PurgeResult` describing the destruction.

        Raises:
            ValueError: If ``confirmation`` does not match.
        """
        if confirmation != VAULT_PURGE_CONFIRMATION:
            msg = (
                f"Vault purge requires explicit confirmation text: "
                f"{VAULT_PURGE_CONFIRMATION!r}"
            )
            raise ValueError(msg)

        result = PurgeResult(
            operation="vault",
            target="entire vault",
            criteria={"scope": "entire vault"},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Wipe every vault-content folder, mutating ``result``."""
            for folder in _VAULT_CONTENT_FOLDERS:
                folder_path = self.vault_path / folder
                if not folder_path.is_dir():
                    continue
                self._wipe_folder_contents(folder_path, result)
            result.fragments_affected = len(result.deleted_files)
            result.embeddings_removed = self._delete_cache_file()

        return self._run_audited(result, body)

    # -- Private helpers -------------------------------------------------

    def _purge_cache_for(self, fragment_ids: list[str]) -> int:
        """Drop matching rows from the embeddings cache (GAP-001).

        Called by every file-deleting purge operation *after* the
        fragments have been removed from disk so the audit entry's
        ``embeddings_removed`` reflects what is now provably gone from
        the parquet cache (or what *would* be gone in a dry run).

        Args:
            fragment_ids: Fragment IDs whose cached embedding rows
                should be scrubbed.

        Returns:
            Number of cache rows removed (or counted-only in dry-run).
        """
        from creek.link.embeddings import (
            embeddings_cache_path,
            purge_fragment_ids_from_cache,
        )

        return purge_fragment_ids_from_cache(
            embeddings_cache_path(self.vault_path),
            fragment_ids,
            dry_run=self.dry_run,
        )

    def _delete_cache_file(self) -> int:
        """Delete the embeddings cache outright (GAP-001 vault path).

        Returns:
            Number of rows that were in the cache when it was removed
            (or that would be removed in a dry run). Zero when the
            cache file did not exist.
        """
        from creek.link.embeddings import (
            delete_embeddings_cache,
            embeddings_cache_path,
        )

        return delete_embeddings_cache(
            embeddings_cache_path(self.vault_path),
            dry_run=self.dry_run,
        )

    def _purge_single(
        self,
        frag_file: Path,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Apply a single-fragment purge to a result accumulator.

        Args:
            frag_file: Fragment file to remove.
            post: Parsed frontmatter of ``frag_file``.
            result: Result object being accumulated.
        """
        title = str(post.get("title", frag_file.stem))
        thread_ids = _str_list(post.get("threads"))
        eddy_ids = _str_list(post.get("eddies"))

        result.deleted_files.append(str(frag_file))
        frag_id = post.get("id")
        if isinstance(frag_id, str):
            result.affected_fragment_ids.append(frag_id)
        result.fragments_affected += 1
        result.wikilinks_removed += self._scrub_wikilinks(
            title,
            exclude=frag_file,
        )
        result.threads_updated += self._decrement_counts(
            "02-Threads",
            thread_ids,
        )
        result.eddies_updated += self._decrement_counts(
            "03-Eddies",
            eddy_ids,
        )
        if not self.dry_run:
            frag_file.unlink()

    def _find_fragment_by_id(
        self,
        fragment_id: str,
    ) -> tuple[Path | None, frontmatter.Post | None]:
        """Locate a fragment markdown file by its frontmatter ID.

        Args:
            fragment_id: The fragment ID to locate.

        Returns:
            Tuple of ``(path, post)`` or ``(None, None)`` if not found.
        """
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter(frag_file)
            if post is not None and post.get("id") == fragment_id:
                return frag_file, post
        return None, None

    def _fragments_from_source(
        self,
        source_type: str,
    ) -> list[tuple[Path, frontmatter.Post]]:
        """List all fragment files whose source platform matches.

        Args:
            source_type: Source platform identifier.

        Returns:
            List of ``(path, post)`` pairs.
        """
        matches: list[tuple[Path, frontmatter.Post]] = []
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter(frag_file)
            if post is None:
                continue
            if _extract_source_platform(post) == source_type:
                matches.append((frag_file, post))
        return matches

    def _fragments_from_source_path(
        self,
        source_path: str,
        *,
        match: str,
    ) -> list[tuple[Path, frontmatter.Post]]:
        """List fragments whose ``source.original_file`` matches *source_path*.

        Args:
            source_path: Path string / substring / regex.
            match: ``"exact"`` / ``"substring"`` / ``"regex"``.

        Returns:
            List of ``(path, post)`` pairs.

        Raises:
            ValueError: When *match* is unrecognised, or when
                ``match="regex"`` and *source_path* is not a valid regex.
        """
        predicate = _build_source_path_matcher(source_path, match=match)
        matches: list[tuple[Path, frontmatter.Post]] = []
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter(frag_file)
            if post is None:
                continue
            original_file = _extract_source_original_file(post)
            if original_file is not None and predicate(original_file):
                matches.append((frag_file, post))
        return matches

    def _list_fragment_files(self) -> list[Path]:
        """Return every ``.md`` file under ``01-Fragments``.

        Returns:
            Sorted list of fragment markdown paths.
        """
        fragments_dir = self.vault_path / "01-Fragments"
        if not fragments_dir.is_dir():
            return []
        return sorted(fragments_dir.rglob("*.md"))

    def _list_vault_md_files(self) -> list[Path]:
        """Return every ``.md`` file anywhere in the vault.

        Returns:
            Sorted list of markdown paths.
        """
        if not self.vault_path.is_dir():
            return []
        return sorted(self.vault_path.rglob("*.md"))

    def _load_frontmatter(self, path: Path) -> frontmatter.Post | None:
        """Read a frontmatter post from disk, tolerating bad files.

        Args:
            path: Path to a markdown file.

        Returns:
            Parsed :class:`frontmatter.Post`, or ``None`` on failure.
        """
        try:
            return frontmatter.load(str(path))
        except Exception:
            logger.debug("Unable to parse frontmatter in %s", path)
            return None

    def _scrub_wikilinks(self, title: str, *, exclude: Path) -> int:
        """Remove all ``[[title]]`` wiki-links from every vault file.

        Args:
            title: The target title whose links should be removed.
            exclude: Path to skip (typically the file being deleted).

        Returns:
            Total number of wiki-link occurrences removed.
        """
        if not title:
            return 0
        pattern = _build_wikilink_pattern(title)
        total_removed = 0
        for md_file in self._list_vault_md_files():
            if md_file == exclude:
                continue
            total_removed += self._scrub_file(md_file, pattern)
        return total_removed

    def _scrub_file(self, md_file: Path, pattern: re.Pattern[str]) -> int:
        """Remove wiki-link occurrences from a single file.

        Args:
            md_file: File to modify.
            pattern: Regex pattern matching the wiki-link.

        Returns:
            Number of occurrences removed in this file.
        """
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            return 0
        new_text, count = pattern.subn("", text)
        if count and not self.dry_run:
            md_file.write_text(new_text, encoding="utf-8")
        return count

    def _decrement_counts(
        self,
        subfolder: str,
        ids: list[str],
    ) -> int:
        """Decrement ``fragment_count`` on thread/eddy files.

        Args:
            subfolder: ``02-Threads`` or ``03-Eddies``.
            ids: IDs of files whose counts should decrement.

        Returns:
            Number of files updated.
        """
        if not ids:
            return 0
        folder = self.vault_path / subfolder
        if not folder.is_dir():
            return 0
        id_set = set(ids)
        updated = 0
        for md_file in folder.rglob("*.md"):
            post = self._load_frontmatter(md_file)
            if post is None or post.get("id") not in id_set:
                continue
            raw_count = post.get("fragment_count", 0)
            current = int(raw_count) if isinstance(raw_count, (int, float, str)) else 0
            post["fragment_count"] = max(0, current - 1)
            updated += 1
            if not self.dry_run:
                md_file.write_text(
                    frontmatter.dumps(post),
                    encoding="utf-8",
                )
        return updated

    def _reset_classifications(self, post: frontmatter.Post) -> bool:
        """Reset classification fields on a frontmatter post.

        Args:
            post: Frontmatter to mutate in-place.

        Returns:
            True if any classification field was actually changed.
        """
        changed = False
        for field_name in _CLASSIFICATION_RESET_FIELDS:
            default = _CLASSIFICATION_DEFAULTS[field_name]
            if post.get(field_name) != default:
                post[field_name] = default
                changed = True
        return changed

    def _wipe_folder_contents(
        self,
        folder_path: Path,
        result: PurgeResult,
    ) -> None:
        """Remove every file/subdirectory inside a vault folder.

        Args:
            folder_path: Folder whose contents should be removed.
            result: Result accumulator to update with deleted paths.
        """
        for entry in sorted(folder_path.iterdir()):
            result.deleted_files.append(str(entry))
            if self.dry_run:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def _run_audited(
        self,
        result: PurgeResult,
        body: Callable[[], None],
    ) -> PurgeResult:
        """Wrap a purge body in the GAP-002 intent → outcome audit pair.

        Emits an ``intent`` entry before invoking *body*, then an
        ``outcome`` entry after — ``status="complete"`` on success,
        ``status="partial"`` (with the exception type + message in
        ``failure_reason``) when *body* raises. The exception then
        propagates so callers see the failure.

        The intent entry is the engine's recovery contract: even a
        SIGKILL between the intent write and the first destructive op
        leaves a forensic trail naming what was being attempted.

        Args:
            result: Result accumulator the body will populate.
            body: Callable that does the actual destructive work and
                mutates *result* in place.

        Returns:
            The same ``result`` with counts populated.

        Raises:
            Exception: Whatever *body* raises, after the partial
                outcome line has been written.
        """
        operation_id = uuid.uuid4().hex
        self._write_intent_audit(result, operation_id)
        try:
            body()
        except BaseException as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            self._write_outcome_audit(
                result,
                operation_id,
                status="partial",
                failure_reason=failure_reason,
            )
            raise
        self._write_outcome_audit(result, operation_id, status="complete")
        return result

    def _write_intent_audit(
        self,
        result: PurgeResult,
        operation_id: str,
    ) -> None:
        """Append the GAP-002 ``intent`` entry for *result*.

        The intent line captures the *planned* scope (operation name,
        criteria, dry-run flag). Counts are deliberately zero — they
        are not known until the body runs.

        Args:
            result: Result accumulator carrying the planned criteria.
            operation_id: UUID4 hex linking this intent to its outcome.
        """
        self._validate_operation(result.operation)
        entry = PurgeAuditEntry(
            operation=result.operation,
            criteria=result.criteria.copy(),
            dry_run=result.dry_run,
            phase="intent",
            operation_id=operation_id,
        )
        self.audit_log.append(entry)

    def _write_outcome_audit(
        self,
        result: PurgeResult,
        operation_id: str,
        *,
        status: PurgeOutcomeStatus,
        failure_reason: str | None = None,
    ) -> None:
        """Append the GAP-002 ``outcome`` entry for *result*.

        ``fragments_deleted`` counts only operations that remove files
        from disk (see :data:`_FILE_DELETING_OPERATIONS`);
        ``classifications`` resets metadata in place and therefore
        reports zero deletions. ``embeddings_removed`` is the *real*
        number of rows the engine just dropped from
        ``<vault>/00-Creek-Meta/embeddings.parquet`` (GAP-001) — zero
        when the cache had not been built yet, zero for metadata-only
        operations, and the actual row delta otherwise.

        Args:
            result: The result whose metadata should be logged.
            operation_id: UUID4 hex matching the paired intent entry.
            status: ``"complete"`` if the body ran to the end, or
                ``"partial"`` if it raised.
            failure_reason: Short ``"<ExceptionType>: <message>"`` when
                ``status="partial"``; ``None`` otherwise.

        Raises:
            ValueError: If ``result.operation`` is not a known purge
                operation. Unknown operations are rejected explicitly
                so a new operation type cannot silently default to
                ``deletes_files=True``.
        """
        deletes_files = self._validate_operation(result.operation)
        fragments_deleted = result.fragments_affected if deletes_files else 0
        entry = PurgeAuditEntry(
            operation=result.operation,
            criteria=result.criteria.copy(),
            affected_fragments=result.affected_fragment_ids.copy(),
            fragments_deleted=fragments_deleted,
            references_scrubbed=result.wikilinks_removed,
            embeddings_removed=result.embeddings_removed,
            dry_run=result.dry_run,
            phase="outcome",
            operation_id=operation_id,
            status=status,
            failure_reason=failure_reason,
        )
        self.audit_log.append(entry)

    def _validate_operation(self, operation: str) -> bool:
        """Confirm *operation* is known and return whether it deletes files.

        Args:
            operation: The operation name to validate.

        Returns:
            ``True`` when the operation removes files from disk,
            ``False`` for metadata-only operations.

        Raises:
            ValueError: When *operation* is neither in
                :data:`_FILE_DELETING_OPERATIONS` nor the metadata-only
                allowlist.
        """
        if operation in _FILE_DELETING_OPERATIONS:
            return True
        if operation == "classifications":
            return False
        msg = (
            f"Unknown purge operation {operation!r}; classify it "
            f"explicitly in _FILE_DELETING_OPERATIONS or add a metadata-only "
            f"branch before logging."
        )
        raise ValueError(msg)

    def _write_audit(self, result: PurgeResult) -> None:
        """Backward-compat shim that writes a single outcome entry.

        Pre-existing callers (and a small handful of tests) treat
        ``_write_audit`` as "emit one final audit line". The GAP-002
        contract has the engine emit *two* lines per operation via
        :meth:`_run_audited`. This shim is preserved so direct callers
        keep working: they get an outcome line with a fresh
        ``operation_id`` and ``status="complete"``, no paired intent.

        Args:
            result: Result whose metadata should be logged.
        """
        self._write_outcome_audit(result, uuid.uuid4().hex, status="complete")


_CLASSIFICATION_DEFAULTS: dict[str, dict[str, object]] = {
    "frequency": {"primary": "unclassified", "secondary": []},
    "wavelength": {
        "phase": "unclassified",
        "mode": "unclassified",
        "orientation": "unclassified",
        "dosage": "unclassified",
        "color": "unclassified",
        "descriptor": "",
    },
    "voice": {"voice_register": None, "confidence": None},
}
"""Default values applied to classification fields during reset."""


def _extract_source_platform(post: frontmatter.Post) -> str | None:
    """Extract the ``source.platform`` string from frontmatter.

    Args:
        post: Parsed frontmatter post.

    Returns:
        The source platform identifier, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        platform = source.get("platform")
        return str(platform) if platform is not None else None
    return None


def _extract_source_original_file(post: frontmatter.Post) -> str | None:
    """Extract the ``source.original_file`` string from frontmatter (INC-008).

    Args:
        post: Parsed frontmatter post.

    Returns:
        The original file path, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        original = source.get("original_file")
        return str(original) if original is not None else None
    return None


SOURCE_PATH_MATCH_MODES: frozenset[str] = frozenset(
    {"exact", "substring", "regex"},
)
"""Canonical set of valid ``--match`` modes for ``purge source --source-path``.

Exported (no leading underscore) so the CLI layer can re-use the same
constant rather than duplicating it. The CLI iterates this set when
rendering its own usage error and the engine validates against it
inside :meth:`PurgeEngine.purge_source_path`. INC-008.
"""


def _build_source_path_matcher(
    source_path: str,
    *,
    match: str,
) -> Callable[[str], bool]:
    """Build a per-fragment predicate from *source_path* and *match* mode.

    Args:
        source_path: Path string the operator passed.
        match: One of ``"exact"`` / ``"substring"`` / ``"regex"``.

    Returns:
        A callable that takes a fragment's ``source.original_file`` and
        returns ``True`` when the fragment should be purged.

    Raises:
        ValueError: When *match* is unrecognised, or when
            ``match="regex"`` and *source_path* is not a valid regex.
    """
    if match not in SOURCE_PATH_MATCH_MODES:
        msg = (
            f"Unknown match mode {match!r}; expected one of "
            f"{sorted(SOURCE_PATH_MATCH_MODES)}."
        )
        raise ValueError(msg)
    if match == "exact":
        return lambda candidate: candidate == source_path
    if match == "substring":
        return lambda candidate: source_path in candidate
    # match == "regex"
    try:
        pattern = re.compile(source_path)
    except re.error as exc:
        msg = f"Invalid regex {source_path!r}: {exc}"
        raise ValueError(msg) from exc
    return lambda candidate: pattern.search(candidate) is not None


def _build_wikilink_pattern(title: str) -> re.Pattern[str]:
    """Build a regex matching ``[[title]]`` and ``[[title|alias]]``.

    Args:
        title: The target title to match exactly.

    Returns:
        A compiled regex pattern.
    """
    escaped = re.escape(title)
    return re.compile(rf"\[\[{escaped}(?:\|[^\]]*)?\]\]")


def _coerce_date(value: object) -> date | None:
    """Coerce a frontmatter value into a date, if possible.

    Accepts :class:`datetime`, :class:`date`, or ISO-format strings.

    Args:
        value: Frontmatter value to coerce.

    Returns:
        A ``date``, or ``None`` if ``value`` cannot be coerced.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC).date()
        except ValueError:
            return None
    return None
