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
import yaml
from pydantic import BaseModel, Field

from creek.ingest.journal_staging import ADEPTHOOD_STAGING_RELDIRS
from creek.purge.audit import PurgeAuditEntry, PurgeAuditLog, PurgeOutcomeStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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

_VOICE_RELDIR: str = "07-Voice"
"""Vault folder holding the voice subsystem's derived artifacts (#1211)."""

_VOICE_SAMPLES_RELDIR: tuple[str, ...] = ("07-Voice", "Register-Samples")
"""Where ``save_exemplars`` copies exemplar fragment files byte for byte."""

_VOICE_LEXICON_RELDIR: tuple[str, ...] = ("07-Voice", "Lexicon")
"""Where the glossary and per-domain metaphor notes quote source sentences."""

_VOICE_PROFILE_GLOB: str = "*-profile.md"
"""Top-level ``07-Voice`` notes whose ``### Sample Passages`` are exemplar bodies."""

VAULT_MARKER_RELPATH = ("00-Creek-Meta", "creek_config.yaml")
"""Relative path to the file that proves a directory is a Creek vault (GAP-003).

The vault marker is the per-vault config file ``creek init`` deploys at
``<vault>/00-Creek-Meta/creek_config.yaml``. It survives every purge
operation (``purge_vault`` preserves ``00-Creek-Meta/`` and the audit
log is mandated non-purgeable), so its absence is a reliable signal
that the supplied path was never ``creek init``-ed — most likely a
typo on ``--vault`` that points at an unrelated directory with
coincidentally numeric-prefix folders.
"""

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

PURGED_MARKER = "[purged]"
"""GAP-004 replacement string for scrubbed fragment-ID references.

Used by :meth:`PurgeEngine._scrub_references` for both YAML frontmatter
list entries (e.g. ``source_fragments: [...]`` in drafts) and bare
fragment-ID mentions in body text. A literal placeholder leaves a
forensic trail — the user can see *that* a reference existed — without
exposing the original ID, satisfying the RTBF contract.

YAML flow-list side-effect (caveat): the marker is bracket-flavoured,
which is YAML-meaningful inside a flow sequence. A text-level
substitution turns ``source_fragments: [frag-A, frag-B]`` into
``source_fragments: [[purged], frag-B]``. A YAML parser then re-reads
the scrubbed entry as a one-element nested list (``["purged"]``),
yielding a ``list[str | list[str]]`` rather than the original flat
``list[str]``. The grep-based forensic intent is satisfied, but a
purged draft's ``source_fragments`` is therefore **not safe to
round-trip** through code that expects a flat string list. Today's
readers do not rely on that flat shape — the mining strategies build
``source_fragments`` from compiled-page provenance and raw-fragment
membership rather than re-reading a purged draft's frontmatter, and
the compiled-page loader validates provenance through a typed model.
If a future reader does parse purged ``source_fragments`` directly it
must tolerate nested-list entries (or skip purged drafts). The
regression test ``test_purged_marker_reparses_as_nested_list_in_yaml``
pins this trade-off so a future marker swap has to update the test and
this docstring together.
"""

_FRONTMATTER_LOAD_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    TypeError,
    ValueError,
    yaml.YAMLError,
)
"""Exceptions a hand-edited or corrupt vault file raises out of ``frontmatter.load``.

Widens the house ``(OSError, ValueError, yaml.YAMLError)`` tuple with
``TypeError``, which is load-bearing here and must never be dropped:
``frontmatter.load`` splats the parsed metadata as
``Post(content, handler, **metadata)``, so frontmatter carrying a
non-string key — a bare YAML date like ``2024-05-01: note``, a realistic
hand-edited-vault case — parses to a ``datetime.date`` key and raises a
plain ``TypeError: keywords must be strings`` that the house tuple
misses (precedent: PR #927 / issue #847).

``UnicodeDecodeError`` is a ``ValueError`` subclass and is therefore
covered deliberately: a file whose *body* carries non-UTF-8 bytes fails
at read time, before its (possibly byte-clean ASCII) frontmatter block
is ever parsed.
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


def _read_bytes_for_match(path: Path) -> bytes:
    """Read *path* as raw bytes for a containment test, never for rewriting.

    Bytes rather than text on purpose: the caller is deciding whether a
    derived artifact still holds a purged fragment's content, and a
    ``read_text`` would raise on the first undecodable byte — turning a
    file that *might* hold the erased body into one the sweep skips
    silently. An unreadable file yields ``b""``, which matches nothing,
    and is logged so a skipped artifact stays distinguishable from a
    clean one.

    Args:
        path: File to read.

    Returns:
        The file's bytes, or ``b""`` when it cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        # Type name only, never str(exc): an OSError message can quote a
        # title-derived filename, and through it vault content.
        logger.warning(
            "Skipping unreadable derived artifact during purge: %s (%s)",
            path,
            type(exc).__name__,
        )
        return b""


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
        provenance_scrubbed: Number of fragment-ID mentions replaced
            with ``[purged]`` across the vault (GAP-004). Counts YAML
            list entries (e.g. ``source_fragments``) and bare body-text
            mentions in every ``.md`` file; the wiki-link count stays
            on :attr:`wikilinks_removed`.
        intimate_stubs_removed: Number of intimate-body stub files
            deleted under ``10-Liminal/Compost/intimate-stubs/`` because
            a purged note carried a ``saved_from.intimate_body_pointer``
            at them (GAP-012). Counted-only in a dry run.
        journal_staged_removed: Number of staged Adepthood source files
            deleted under ``00-Creek-Meta/adepthood/journal/`` or
            ``00-Creek-Meta/adepthood/uploads/`` because the fragment
            they produced was purged (issues #845, #1023). The staged
            file holds the entry's full plaintext body or the uploaded
            document's bytes, so leaving it behind would defeat the
            RTBF request. Counted-only in a dry run. The field keeps its
            journal-era name because it is serialised into the
            append-only ``purge.jsonl``, where a rename would break
            every existing log.
        voice_artifacts_removed: Number of derived ``07-Voice/`` notes
            deleted because they carried the purged fragment's own
            content (#1211) — the ``Register-Samples`` copy of its file,
            the ``<register>-profile.md`` quoting its body, and the
            ``Lexicon`` notes quoting its sentences. Counted-only in a
            dry run. Deliberately **not** appended to
            :attr:`deleted_files` and never inflates
            :attr:`fragments_affected`: these are derived copies, not
            fragments, and the same rule already governs
            :attr:`journal_staged_removed`.
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
    provenance_scrubbed: int = 0
    intimate_stubs_removed: int = 0
    journal_staged_removed: int = 0
    voice_artifacts_removed: int = 0


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
            # Strictly before the scrub: the lexicon's only link back to
            # this fragment is a ``[[<id>]]`` wikilink, and the scrub
            # rewrites it to ``[[[purged]]]``. Swept afterwards, the
            # sweep would be blind to exactly the notes quoting the body.
            self._purge_voice_artifacts(fragment_id, post.content, result)
            # One vault walk applies both the wiki-link and provenance scrubs.
            wiki_count, prov_count = self._scrub_references(
                title=title,
                fragment_id=fragment_id,
                exclude=frag_file,
            )
            result.wikilinks_removed = wiki_count
            result.provenance_scrubbed += prov_count
            result.threads_updated = self._decrement_counts(
                "02-Threads",
                thread_ids,
            )
            result.eddies_updated = self._decrement_counts(
                "03-Eddies",
                eddy_ids,
            )
            self._purge_intimate_stub(post, result)
            self._purge_staged_source_entry(post, result)
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
                post = self._load_frontmatter_for_match(frag_file)
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
            ValueError: If ``confirmation`` does not match, or if
                ``self.vault_path`` does not look like a Creek vault
                (no ``00-Creek-Meta/creek_config.yaml`` marker file —
                GAP-003).
        """
        if confirmation != VAULT_PURGE_CONFIRMATION:
            msg = (
                f"Vault purge requires explicit confirmation text: "
                f"{VAULT_PURGE_CONFIRMATION!r}"
            )
            raise ValueError(msg)
        self._require_vault_marker()

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
            self._wipe_adepthood_staging(result)
            result.embeddings_removed = self._delete_cache_file()

        return self._run_audited(result, body)

    # -- Private helpers -------------------------------------------------

    def _require_vault_marker(self) -> None:
        """Refuse to operate on a directory that is not a Creek vault (GAP-003).

        Looks for ``<vault>/00-Creek-Meta/creek_config.yaml`` — the
        per-vault config file that ``creek init`` deploys and that
        survives every purge operation. Its absence almost always
        means the operator typoed ``--vault`` and is pointing at an
        unrelated directory with coincidentally numeric-prefix
        folders. Raising here, *before* the intent audit line is
        written, prevents an entry from being committed to a
        non-vault's would-be audit log.

        Raises:
            ValueError: When the marker file is not present. The
                message names the absolute path of the file the engine
                looked for so the operator can diagnose the typo.
        """
        marker = self.vault_path.joinpath(*VAULT_MARKER_RELPATH)
        if not marker.is_file():
            msg = (
                f"{self.vault_path} does not appear to be a Creek vault "
                f"(missing marker file {marker}). Run `creek init "
                f"--vault <path>` first, or fix the --vault argument."
            )
            raise ValueError(msg)

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
        # ``frontmatter.Post.get`` returns ``Any``, so the id may be a
        # non-string; only a real string is a scrubbable fragment ID.
        frag_id = post.get("id")
        if isinstance(frag_id, str):
            result.affected_fragment_ids.append(frag_id)
        result.fragments_affected += 1
        # Before the scrub, for the reason spelled out in purge_fragment.
        self._purge_voice_artifacts(
            frag_id if isinstance(frag_id, str) else "",
            post.content,
            result,
        )
        # One vault walk applies both the wiki-link and provenance scrubs.
        wiki_count, prov_count = self._scrub_references(
            title=title,
            fragment_id=frag_id if isinstance(frag_id, str) else "",
            exclude=frag_file,
        )
        result.wikilinks_removed += wiki_count
        result.provenance_scrubbed += prov_count
        result.threads_updated += self._decrement_counts(
            "02-Threads",
            thread_ids,
        )
        result.eddies_updated += self._decrement_counts(
            "03-Eddies",
            eddy_ids,
        )
        self._purge_intimate_stub(post, result)
        self._purge_staged_source_entry(post, result)
        if not self.dry_run:
            frag_file.unlink()

    def _resolve_pointer_in_vault(self, pointer: str, *, field: str) -> Path | None:
        """Resolve a frontmatter-supplied path pointer inside the vault.

        Pointers such as ``saved_from.intimate_body_pointer`` and
        ``source.origin_key`` are vault *content*: an operator (or
        anyone who can write one note) controls their text, so they are
        untrusted input to a destructive path. This helper turns such a
        pointer into an absolute path only when it is safe to follow —
        a refusal is a no-op, never an error, per the purge engine's
        "a missing or bad pointer is skipped" contract.

        A pointer is refused when it cannot be resolved at all (an
        embedded NUL byte makes :meth:`~pathlib.Path.resolve` raise
        ``ValueError``; a resolution loop or unreadable component
        raises ``OSError``) or when it resolves outside the vault root.
        Both refusals are logged at WARNING so an ignored pointer is
        distinguishable from a sweep that never ran.

        Args:
            pointer: Vault-relative path string taken from frontmatter.
            field: Frontmatter field name the pointer came from, used
                verbatim in the refusal logs so operators can grep for
                the field that carried the bad value.

        Returns:
            The resolved absolute path, or ``None`` meaning *refuse* —
            the caller must not follow this pointer.
        """
        try:
            resolved = (self.vault_path / pointer).resolve()
        except (OSError, ValueError):
            logger.warning(
                "Ignoring %s %r that cannot be resolved to a path",
                field,
                pointer,
            )
            return None
        vault_root = self.vault_path.resolve()
        if not resolved.is_relative_to(vault_root):
            logger.warning(
                "Ignoring %s %r that resolves outside the vault root %s",
                field,
                pointer,
                vault_root,
            )
            return None
        return resolved

    def _purge_intimate_stub(
        self,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Sweep the intimate-body stub a purged note points at (GAP-012).

        ``creek save`` of an ``intimate``-tier answer writes a
        title-only note and routes the full body to a stub file under
        ``10-Liminal/Compost/intimate-stubs/``, recording the link in
        the note's ``saved_from.intimate_body_pointer`` frontmatter. A
        scoped purge that deletes the note must follow that pointer and
        delete the stub too, or the full intimate body survives the
        right-to-be-forgotten request.

        The method is defensive: a missing or empty pointer, an
        unresolvable pointer, an already-deleted stub, and a pointer
        that resolves outside the vault root are all treated as no-ops
        rather than errors. A second containment guard additionally
        scopes deletion to the canonical stub directory itself, so a
        hand-edited or malicious pointer (``../../secret``,
        ``00-Creek-Meta/creek_config.yaml``, or a ``..`` walk back out
        of the stub dir) can never steer a delete at an arbitrary file.

        That guard is deliberately fail-closed, with one accepted side
        effect: a hand-authored stub parked *outside* the canonical
        directory now survives the purge. That is the safe direction —
        the note itself is still deleted, so the RTBF request is
        honoured, and the operator can purge the stray file explicitly.

        Args:
            post: Frontmatter of the note being purged.
            result: Result accumulator; ``intimate_stubs_removed`` is
                incremented for each stub actually removed (or that
                would be removed in a dry run).
        """
        # Imported inside the function to keep the RTBF purge path's
        # module-level import surface thin: at module scope this pulls
        # ``creek/save/__init__.py`` → ``writer.py`` →
        # ``creek.classify.privacy_filter`` → the whole
        # ``creek.classify.llm`` provider subtree (httpx and friends).
        # This is NOT a circular-import workaround — nothing under
        # ``creek/save/`` imports ``creek.purge``, so please do not
        # "fix" it by hoisting the import to module scope.
        from creek.save._constants import INTIMATE_STUB_RELPATH

        pointer = self._intimate_pointer(post)
        if not pointer:
            return
        stub_path = self._resolve_pointer_in_vault(
            pointer,
            field="intimate_body_pointer",
        )
        if stub_path is None:
            return
        stub_root = (self.vault_path / INTIMATE_STUB_RELPATH).resolve()
        # Checked before the counter so a dry run never previews a
        # deletion the real run would refuse. The resolved victim path
        # is deliberately left out of the message: the pointer is
        # attacker-controlled, so echoing what it resolved to would
        # turn the log into a filesystem oracle.
        if not stub_path.is_relative_to(stub_root):
            logger.warning(
                "Ignoring intimate_body_pointer %r that resolves outside "
                "the intimate stub dir %s",
                pointer,
                stub_root,
            )
            return
        if not stub_path.is_file():
            return
        result.intimate_stubs_removed += 1
        if not self.dry_run:
            stub_path.unlink(missing_ok=True)

    @staticmethod
    def _intimate_pointer(post: frontmatter.Post) -> str | None:
        """Extract ``saved_from.intimate_body_pointer`` from a note.

        Args:
            post: Parsed frontmatter of a vault note.

        Returns:
            The stub pointer (vault-relative path string), or ``None``
            when the note carries no usable pointer.
        """
        saved_from = post.get("saved_from")
        if not isinstance(saved_from, dict):
            return None
        pointer = saved_from.get("intimate_body_pointer")
        if isinstance(pointer, str) and pointer.strip():
            return pointer
        return None

    def _in_any_staging_root(self, path: Path) -> bool:
        """Report whether *path* lies inside an Adepthood staging root.

        The containment guard behind :meth:`_purge_staged_source_entry`,
        factored out so the guard reads as one condition however many
        roots :data:`~creek.ingest.journal_staging.ADEPTHOOD_STAGING_RELDIRS`
        grows to hold.

        Args:
            path: An already vault-contained absolute path (the output of
                :meth:`_resolve_pointer_in_vault`).

        Returns:
            ``True`` when *path* is inside at least one staging root.
        """
        return any(
            path.is_relative_to((self.vault_path / reldir).resolve())
            for reldir in ADEPTHOOD_STAGING_RELDIRS
        )

    def _purge_staged_source_entry(
        self,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Sweep the staged source a purged fragment points at (#845/#1023).

        Adepthood stages the content it captures under the vault and
        records the vault-relative path in the fragment's
        ``source.origin_key``: ``creek.journal`` writes each entry's
        full body as markdown under
        ``00-Creek-Meta/adepthood/journal/``, and ``creek.upload``
        writes each uploaded document's bytes verbatim under
        ``00-Creek-Meta/adepthood/uploads/``. A scoped purge that
        deletes the fragment must follow that key and delete the staged
        file too, or the full — often intimate — source content
        survives the right-to-be-forgotten request.

        The method is defensive, and now shares that defence with
        :meth:`_purge_intimate_stub` in both directions: a missing or
        empty key, an unresolvable key, an already-deleted staged file,
        and a key that resolves outside the vault root are all no-ops
        rather than errors (see :meth:`_resolve_pointer_in_vault`). A
        second containment guard additionally scopes deletion to the
        staging directories themselves
        (:meth:`_in_any_staging_root`), so a hand-edited or malicious
        ``origin_key`` (``../x``, ``01-Fragments/other.md``) can never
        steer a delete at an arbitrary vault file. #1023 widened that
        guard to every declared staging root; it never relaxed it.

        Args:
            post: Frontmatter of the fragment being purged.
            result: Result accumulator; ``journal_staged_removed`` is
                incremented for each staged file actually removed (or
                that would be removed in a dry run).
        """
        origin_key = _extract_source_origin_key(post)
        if not origin_key:
            return
        staged_path = self._resolve_pointer_in_vault(
            origin_key,
            field="source.origin_key",
        )
        if staged_path is None:
            return
        if not self._in_any_staging_root(staged_path):
            logger.warning(
                "Ignoring source.origin_key %r that resolves outside "
                "the Adepthood staging dirs %s",
                origin_key,
                [str(reldir) for reldir in ADEPTHOOD_STAGING_RELDIRS],
            )
            return
        if not staged_path.is_file():
            return
        result.journal_staged_removed += 1
        if not self.dry_run:
            staged_path.unlink(missing_ok=True)

    def _purge_voice_artifacts(
        self,
        fragment_id: str,
        body: str,
        result: PurgeResult,
    ) -> None:
        """Sweep the ``07-Voice/`` artifacts derived from a purged fragment (#1211).

        The voice subsystem does not merely *reference* a fragment, it
        **copies its body**, so scrubbing references leaves the erased
        content on disk in three shapes:
        ``Register-Samples/<register>/<id>.md`` is a ``shutil.copy2`` of
        the fragment file; ``<register>-profile.md`` renders each
        exemplar body verbatim under ``### Sample Passages``; and
        ``Lexicon/glossary.md`` plus ``Lexicon/Metaphors/<domain>.md``
        quote whole source sentences.

        Each match is **deleted rather than edited**. A derived note is a
        function of the corpus: excising one passage would leave the
        purged fragment's statistical residue (its n-grams, its
        contribution to every count in the note) behind while the note
        went on advertising a total it no longer has. All four artifacts
        are regenerated by ``creek report --type voice`` / ``--type
        lexicon``, so deletion costs a re-run and buys a clean erasure.

        The sweep is scoped to those declared locations, never a blanket
        ``07-Voice`` walk. ``07-Voice/Drafts/`` in particular holds the
        operator's own writing, which the existing provenance scrub
        handles and which a fragment purge has no mandate to delete.

        Args:
            fragment_id: Id of the fragment being purged. Empty string
                skips the id-keyed passes.
            body: The fragment's body text, used to attribute a profile's
                sample passages — see :meth:`_voice_profiles_quoting` for
                why that pass has to be content-keyed.
            result: Result accumulator; ``voice_artifacts_removed`` is
                incremented per artifact actually removed (or that would
                be removed in a dry run).
        """
        voice_root = self.vault_path / _VOICE_RELDIR
        if not voice_root.is_dir():
            return
        contained_root = voice_root.resolve()
        seen: set[Path] = set()
        for candidate in self._iter_voice_artifacts(fragment_id, body):
            target = self._contained_voice_artifact(candidate, contained_root)
            # Checked before the counter so a dry run never previews a
            # deletion the real run would refuse, and de-duplicated
            # because two passes can name the same note.
            if target is None or target in seen:
                continue
            seen.add(target)
            result.voice_artifacts_removed += 1
            if not self.dry_run:
                target.unlink(missing_ok=True)

    @staticmethod
    def _contained_voice_artifact(
        candidate: Path,
        contained_root: Path,
    ) -> Path | None:
        """Resolve *candidate* and confirm it is a file inside the voice root.

        Deletion targets the **resolved** path so a symlinked copy loses
        its content rather than just its link — an RTBF sweep that
        unlinked the alias would leave the body on disk. Resolving first
        is also what makes containment meaningful: a register folder
        symlinked out of the vault would otherwise steer the sweep at an
        arbitrary file.

        Args:
            candidate: A path produced by one of the artifact passes.
            contained_root: The already-resolved ``07-Voice`` directory.

        Returns:
            The resolved path to delete, or ``None`` meaning *refuse* —
            unresolvable, outside the voice root, or not a file.
        """
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            logger.warning(
                "Ignoring voice artifact %s that cannot be resolved to a path",
                candidate,
            )
            return None
        if not resolved.is_relative_to(contained_root):
            # The resolved victim is deliberately left out of the message:
            # naming it would turn the log into a filesystem oracle.
            logger.warning(
                "Ignoring voice artifact %s that resolves outside %s",
                candidate,
                contained_root,
            )
            return None
        if not resolved.is_file():
            return None
        return resolved

    def _iter_voice_artifacts(
        self,
        fragment_id: str,
        body: str,
    ) -> Iterator[Path]:
        """Yield every ``07-Voice`` artifact attributable to one fragment.

        Args:
            fragment_id: Id of the fragment being purged.
            body: Its body text.

        Yields:
            Candidate paths, before any containment or existence check.
        """
        yield from self._voice_sample_copies(fragment_id)
        yield from self._voice_lexicon_notes(fragment_id)
        yield from self._voice_profiles_quoting(body)

    def _voice_sample_copies(self, fragment_id: str) -> Iterator[Path]:
        """Yield the exemplar copies named after *fragment_id*.

        ``VoiceExemplarCollector._persist_fragment`` writes
        ``f"{fragment.id}.md"``, so the filename **stem** is the recorded
        link — and it is the one link the reference scrub cannot destroy,
        which is exactly why #879's own prune matches on it too.

        Args:
            fragment_id: Id of the fragment being purged.

        Yields:
            One candidate per register folder.
        """
        # A separator in the id would make the join a subpath rather than
        # a filename; the containment guard would catch an escape, but
        # refusing here keeps the sweep from naming a file the collector
        # could never have written.
        if not fragment_id or {"/", "\\", "\x00"} & set(fragment_id):
            return
        samples_root = self.vault_path.joinpath(*_VOICE_SAMPLES_RELDIR)
        if not samples_root.is_dir():
            return
        for register_dir in sorted(samples_root.iterdir()):
            if register_dir.is_dir():
                yield register_dir / f"{fragment_id}.md"

    def _voice_lexicon_notes(self, fragment_id: str) -> Iterator[Path]:
        """Yield lexicon notes that quote *fragment_id*'s sentences.

        ``creek.generate.lexicon._render_context_line`` renders every
        usage as ``- [[<fragment_id>]] — <sentence>``, so the wikilink is
        a recorded, exact link from the id the caller holds to the note
        holding its prose.

        Args:
            fragment_id: Id of the fragment being purged.

        Yields:
            Every glossary or metaphor note carrying that wikilink.
        """
        if not fragment_id:
            return
        lexicon_root = self.vault_path.joinpath(*_VOICE_LEXICON_RELDIR)
        if not lexicon_root.is_dir():
            return
        needle = f"[[{fragment_id}]]".encode()
        for note in sorted(lexicon_root.rglob("*.md")):
            if needle in _read_bytes_for_match(note):
                yield note

    def _voice_profiles_quoting(self, body: str) -> Iterator[Path]:
        """Yield register profiles whose sample passages contain *body*.

        This pass is content-keyed because nothing else is available: a
        profile note records the exemplar **bodies** and no fragment ids
        at all, so there is no provenance to key on (issue #1211's
        recorded design finding). The match is nonetheless exact rather
        than heuristic — ``VoiceProfileGenerator._render_profile_body``
        emits each passage verbatim, so the body appears as a contiguous
        substring. Reaching it still starts from the id: the caller
        resolved the id to the fragment file that supplied this body.

        Args:
            body: The purged fragment's body text.

        Yields:
            Every top-level ``<register>-profile.md`` quoting that body.
        """
        stripped = body.strip()
        if not stripped:
            return
        voice_root = self.vault_path / _VOICE_RELDIR
        needle = stripped.encode()
        for profile in sorted(voice_root.glob(_VOICE_PROFILE_GLOB)):
            if needle in _read_bytes_for_match(profile):
                yield profile

    def _wipe_adepthood_staging(self, result: PurgeResult) -> None:
        """Wipe every Adepthood staging root during a vault purge (#845/#1023).

        ``purge_vault`` deliberately preserves ``00-Creek-Meta/``
        (config marker, audit log), so the content-folder wipe never
        reaches the staged journal bodies under
        ``00-Creek-Meta/adepthood/journal/`` or the staged upload bytes
        under ``00-Creek-Meta/adepthood/uploads/`` — they must be swept
        explicitly or full source plaintext survives a whole-vault RTBF
        request. Staged files are source material, not fragments, so
        they are counted on ``journal_staged_removed`` only — never
        appended to ``deleted_files`` and never inflating
        ``fragments_affected``.

        The walk is non-recursive by design (the staging layouts are
        flat), so anything that is not a file — a subdirectory an
        operator or a future tool dropped in — is skipped rather than
        unlinked. A bare ``unlink`` on a directory raises an
        ``OSError`` (``IsADirectoryError`` on Linux, ``PermissionError``
        on macOS), which :meth:`_run_audited` turns into a
        ``status="partial"`` outcome and re-raises, aborting the entire
        vault purge — so the guard is what stops one stray directory
        from defeating a whole RTBF request.

        Args:
            result: Result accumulator; ``journal_staged_removed`` is
                incremented per staged file removed (or counted-only in
                a dry run).
        """
        for reldir in ADEPTHOOD_STAGING_RELDIRS:
            staging_dir = self.vault_path / reldir
            if not staging_dir.is_dir():
                continue
            for staged_file in sorted(staging_dir.iterdir()):
                if not staged_file.is_file():
                    continue
                result.journal_staged_removed += 1
                if not self.dry_run:
                    staged_file.unlink(missing_ok=True)

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
            post = self._load_frontmatter_for_match(frag_file)
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
            post = self._load_frontmatter_for_match(frag_file)
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
            post = self._load_frontmatter_for_match(frag_file)
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
        """Read a frontmatter post byte-faithfully, tolerating bad files.

        The **write-safe** loader, for the callers that rewrite the post
        in place (``purge_classifications`` and :meth:`_decrement_counts`
        both do ``frontmatter.dumps`` + ``write_text``). A file this
        cannot decode is skipped rather than salvaged, because re-encoding
        a lossy read would overwrite the operator's original bytes during
        a non-destructive metadata reset. Callers that only ever
        ``unlink`` the file must use :meth:`_load_frontmatter_for_match`.

        Args:
            path: Path to a markdown file.

        Returns:
            Parsed :class:`frontmatter.Post`, or ``None`` when the file
            cannot be read or parsed.
        """
        try:
            return frontmatter.load(str(path))
        except _FRONTMATTER_LOAD_ERRORS as exc:
            # Log the exception *type* only: yaml.MarkedYAMLError
            # stringifies with the offending source snippet, which would
            # copy vault content into the log.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None

    def _load_frontmatter_for_match(self, path: Path) -> frontmatter.Post | None:
        """Read a post for a *delete decision only* — never write it back.

        The returned :class:`frontmatter.Post` may be **lossy**: when the
        file is not valid UTF-8, its body is re-read with
        ``errors="replace"`` so every offending byte becomes U+FFFD.
        Writing such a post back to disk would destroy the operator's
        original bytes, so this loader exists solely to decide whether a
        file matches the purge criteria before it is ``unlink``-ed. The
        rewrite paths use :meth:`_load_frontmatter` instead.

        The lossy read is safe for that one job because a purge match is
        a function of *frontmatter metadata* only — ``id``,
        ``source.platform``, ``source.original_file``, ``created`` — and
        never of the body. Replacing bad body bytes therefore cannot
        change the outcome; it can only stop a matching fragment from
        escaping the purge with its private body intact, which is the
        right-to-be-forgotten failure this exists to close (#910).

        Args:
            path: Path to a markdown file.

        Returns:
            Parsed :class:`frontmatter.Post` — lossy in the body when the
            file was not valid UTF-8 — or ``None`` when the frontmatter
            cannot be parsed at all. ``None`` means "matches nothing", so
            an unreadable file is left on disk rather than deleted.
        """
        try:
            return frontmatter.load(str(path))
        except UnicodeDecodeError:
            # Degrading to a lossy read on an RTBF-critical path is an
            # operator-visible event, so name the file at WARNING.
            logger.warning(
                "Body of %s is not valid UTF-8; re-reading it lossily to test it "
                "against the purge criteria (the file itself is never rewritten)",
                path,
            )
            return self._load_frontmatter_lossily(path)
        except _FRONTMATTER_LOAD_ERRORS as exc:
            # Type name only — never str(exc); see _load_frontmatter.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None

    def _load_frontmatter_lossily(self, path: Path) -> frontmatter.Post | None:
        """Re-read an undecodable file, replacing its bad bytes with U+FFFD.

        The retry half of :meth:`_load_frontmatter_for_match`, and subject
        to the same prohibition: the returned post's content is lossy and
        must never be written back to disk.

        Args:
            path: Path to a markdown file that failed to decode as UTF-8.

        Returns:
            Parsed :class:`frontmatter.Post` with invalid bytes replaced,
            or ``None`` when the file is *also* unparseable as
            frontmatter and so can match no purge criteria.
        """
        try:
            # Go through the file object rather than decoding bytes and
            # calling ``frontmatter.loads``: ``frontmatter.load`` is itself
            # ``open(...)`` + ``loads(text)``, so this keeps it the single
            # parse entry point and preserves universal-newline handling,
            # leaving a CRLF vault file identical on both paths.
            with path.open(encoding="utf-8", errors="replace") as handle:
                return frontmatter.load(handle)
        except _FRONTMATTER_LOAD_ERRORS as exc:
            # Both undecodable *and* unparseable: no criteria can match.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None

    def _scrub_references(
        self,
        *,
        title: str,
        fragment_id: str,
        exclude: Path,
    ) -> tuple[int, int]:
        """Scrub wiki-links and fragment-ID provenance in a single vault walk.

        Walks every ``.md`` file in the vault exactly once (instead of
        once per scrub type) and applies both substitutions in-memory
        before writing:

        * Wiki-links pointing at the deleted fragment's *title*
          (``[[title]]`` / ``[[title|alias]]``) are removed. They match
          by name, not by ID, so the title-based pass is still needed.
        * Bare fragment-ID occurrences are replaced with the
          :data:`PURGED_MARKER` placeholder. This catches YAML
          provenance list entries (``source_fragments: [frag-…]`` in
          drafts and mining ideas) and prose mentions of the ID in the
          body in the same pass.

        The walk covers all ``.md`` files in the vault, including
        derived content under ``04-Praxis``, ``05-Wavelength``,
        ``07-Voice/Drafts``, ``08-Decisions``, and the deployed skill
        tree under ``00-Creek-Meta/Skills``. The compliance audit log
        at ``00-Creek-Meta/audit/purge.jsonl`` is a JSONL file (not
        Markdown) so the recursive walk naturally excludes it — the
        audit's ``affected_fragments`` list keeps the real ID for
        compliance reconstruction.

        Args:
            title: The fragment title whose wiki-links should be removed
                (empty string skips the wiki-link pass).
            fragment_id: The exact fragment ID to scrub (empty string
                skips the provenance pass).
            exclude: Path to skip (the file currently being deleted).

        Returns:
            A ``(wikilinks_removed, provenance_scrubbed)`` count pair.
        """
        wiki_pattern = _build_wikilink_pattern(title) if title else None
        prov_pattern = _build_provenance_pattern(fragment_id) if fragment_id else None
        if wiki_pattern is None is prov_pattern:
            return 0, 0
        wiki_total = 0
        prov_total = 0
        for md_file in self._list_vault_md_files():
            if md_file == exclude:
                continue
            wiki_count, prov_count = self._scrub_one_file(
                md_file,
                wiki_pattern,
                prov_pattern,
            )
            wiki_total += wiki_count
            prov_total += prov_count
        return wiki_total, prov_total

    def _scrub_one_file(
        self,
        md_file: Path,
        wiki_pattern: re.Pattern[str] | None,
        prov_pattern: re.Pattern[str] | None,
    ) -> tuple[int, int]:
        """Apply the wiki-link and provenance scrubs to a single file.

        Args:
            md_file: Markdown file to scrub.
            wiki_pattern: Wiki-link regex, or ``None`` to skip that pass.
            prov_pattern: Fragment-ID regex, or ``None`` to skip that pass.

        Returns:
            A ``(wikilinks_removed, provenance_scrubbed)`` count pair for
            this file. ``(0, 0)`` when the file cannot be read or is not
            valid UTF-8.
        """
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Skipping is the only safe response to an undecodable file:
            # this method writes ``text`` back, so an ``errors="replace"``
            # read here would persist U+FFFD over the operator's bytes.
            # A reference living inside such a file therefore survives
            # unscrubbed: an accepted trade-off, since the alternative is
            # corrupting the file — and far better than raising, which
            # would abort the purge before the target is even unlinked.
            # Type name only, never str(exc): the message can quote file
            # content (possibly the very secret being purged).
            logger.warning(
                "Skipping unreadable file during reference scrub: %s (%s)",
                md_file,
                type(exc).__name__,
            )
            return 0, 0
        wiki_count = prov_count = 0
        if wiki_pattern is not None:
            text, wiki_count = wiki_pattern.subn("", text)
        if prov_pattern is not None:
            # NOTE: PURGED_MARKER is bracket-flavoured, so substituting it
            # inside a YAML flow sequence (source_fragments: [a, b]) nests
            # the scrubbed entry on re-parse — see PURGED_MARKER.
            text, prov_count = prov_pattern.subn(PURGED_MARKER, text)
        if (wiki_count or prov_count) and not self.dry_run:
            md_file.write_text(text, encoding="utf-8")
        return wiki_count, prov_count

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
        ``status="partial"`` (with the exception **type name only** in
        ``failure_reason``) when *body* raises. The exception then
        propagates so callers see the failure.

        The message is deliberately dropped: the audit log is preserved
        by every purge, including ``purge_vault``, so anything written
        there outlives the right-to-be-forgotten request that produced
        it — and exception text routinely quotes vault-derived content
        (a title-derived filename in an ``OSError``, a source snippet
        in a ``yaml.MarkedYAMLError``). The type is the forensic value;
        the message is the leak. Callers that need the detail still get
        it from the propagating exception.

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
            failure_reason = type(exc).__name__
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
            failure_reason: The exception **type name only** (e.g.
                ``"OSError"``) when ``status="partial"``; ``None``
                otherwise. The message is omitted because the audit log
                survives every purge, so vault-derived text quoted in an
                exception would outlive the right-to-be-forgotten
                request that produced it.

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
            provenance_scrubbed=result.provenance_scrubbed,
            intimate_stubs_removed=result.intimate_stubs_removed,
            journal_staged_removed=result.journal_staged_removed,
            voice_artifacts_removed=result.voice_artifacts_removed,
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


def _extract_source_origin_key(post: frontmatter.Post) -> str | None:
    """Extract the ``source.origin_key`` string from frontmatter (#845).

    ``origin_key`` is the vault-relative source-ledger key the ingest
    pipeline records on each fragment; for journal fragments it names
    the staged full-body file under ``00-Creek-Meta/adepthood/journal/``.

    Args:
        post: Parsed frontmatter post.

    Returns:
        The origin key, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        origin_key = source.get("origin_key")
        return str(origin_key) if origin_key is not None else None
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
    """Build a regex matching every wikilink form targeting ``title``.

    Matches ``[[title]]`` and ``[[title|alias]]``, plus heading
    (``[[title#Heading]]``), block-reference (``[[title#^id]]``), and
    heading+alias (``[[title#Heading|alias]]``) variants, so a purge
    removes all links that leak the title.

    Args:
        title: The target title to match exactly.

    Returns:
        A compiled regex pattern.
    """
    escaped = re.escape(title)
    return re.compile(rf"\[\[{escaped}(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _build_provenance_pattern(fragment_id: str) -> re.Pattern[str]:
    """Build a regex matching a bare ``fragment_id`` but not hyphen-suffixed IDs.

    A plain ``\\b`` word boundary treats the hyphen as a word/non-word
    boundary, so ``\\bfrag-01\\b`` would match the ``frag-01`` prefix
    inside ``frag-01-extended``. The lookarounds below treat both
    word characters *and* ``-`` as ID continuation, so a trailing or
    leading hyphen marks a longer ID and is left untouched.

    Args:
        fragment_id: The exact fragment ID to match.

    Returns:
        A compiled regex pattern.
    """
    escaped = re.escape(fragment_id)
    return re.compile(rf"(?<![\w-]){escaped}(?![\w-])")


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
