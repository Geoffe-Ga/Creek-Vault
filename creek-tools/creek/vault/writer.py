"""Vault writer — write markdown files with YAML frontmatter to vault folders.

This module provides the ``VaultWriter`` class, which serialises Creek
ontological primitives (Fragment, Thread, Eddy, Praxis, Decision) as
Obsidian-compatible markdown files with YAML frontmatter. It handles:

- Mapping each primitive to the correct vault subfolder
- Sanitising titles into safe filenames
- Detecting duplicates (by ID) via a per-directory ``.id-index.jsonl``
  index — O(1) per write rather than rescanning the directory each time
- Atomic file creation via ``O_CREAT | O_EXCL`` with counter-suffix
  retry, so concurrent writers cannot clobber each other's files
- Appending provenance entries to the processing log under a process
  lock so concurrent writers do not lose entries
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.audit import AuditLog
from creek.models import (
    Authorship,
    DecisionStatus,
    PraxisType,
    SourcePlatform,
)
from creek.vault.authors import (
    OTHER_AUTHORS_DIR,
    load_author_manifest_or_default,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from creek.models import (
        AuthorManifest,
        Decision,
        Eddy,
        Fragment,
        Praxis,
        Thread,
    )

# Map source platform -> 01-Fragments subfolder.
# This mapping must remain *total* across SourcePlatform — every enum
# value has an entry. The totality is enforced by a unit test in
# tests/test_vault_writer.py so missing entries fail loudly rather than
# silently routing fragments to ``Unsorted/``.
_PLATFORM_SUBFOLDER: dict[str, str] = {
    SourcePlatform.CLAUDE: "Conversations",
    SourcePlatform.CHATGPT: "Conversations",
    SourcePlatform.DISCORD: "Messages",
    SourcePlatform.EMAIL: "Messages",
    SourcePlatform.ESSAY: "Writing",
    SourcePlatform.SUBSTACK: "Writing/Substack",
    SourcePlatform.JOURNAL: "Journal",
    SourcePlatform.CODE: "Technical",
    SourcePlatform.MARKDOWN: "Notes",
    SourcePlatform.DOCUMENT: "Documents",
    SourcePlatform.SPREADSHEET: "Data",
    SourcePlatform.PRESENTATION: "Decks",
    SourcePlatform.IMAGE_OCR: "Images",
    SourcePlatform.OTHER: "Unsorted",
}

# Map an AuthorManifest.author_kind -> the fragment's Authorship axis (#470).
# Any unrecognised kind (the manifest already fails closed to ``human_source``)
# resolves to OTHER via ``.get(..., Authorship.OTHER)`` at the call site.
_AUTHOR_KIND_TO_AUTHORSHIP: dict[str, Authorship] = {
    "ai_as_user": Authorship.AI,
    "collaborator": Authorship.COLLABORATIVE,
    "human_source": Authorship.OTHER,
}

# Map praxis type -> 04-Praxis subfolder
_PRAXIS_SUBFOLDER: dict[str, str] = {
    PraxisType.HABIT: "Daily",
    PraxisType.PRACTICE: "Daily",
    PraxisType.FRAMEWORK: "Seasonal",
    PraxisType.COMMITMENT: "Seasonal",
    PraxisType.INSIGHT: "Situational",
}

# Decision statuses that are considered "active" (go to Active/)
_MAX_FILENAME_LENGTH = 80
"""Maximum character length for sanitised filename components."""

_ACTIVE_DECISION_STATUSES: set[str] = {
    DecisionStatus.SENSING,
    DecisionStatus.DELIBERATING,
    DecisionStatus.COMMITTING,
}

logger = logging.getLogger(__name__)


_MAX_FILENAME_COLLISION_RETRIES = 10_000
"""Cap on counter-suffix retries inside :meth:`VaultWriter._atomic_create`.

10 000 leaves headroom for any realistic title-collision burst while
still giving up before an infinite loop hides a bug (e.g. a directory
that became read-only mid-write).
"""

INDEX_FILENAME = ".id-index.jsonl"
"""Per-directory append-only index file mapping ``id`` -> filename.

JSONL format (one JSON object per line, ``{"id": ..., "filename": ...}``)
keeps each write O(1) — appends never touch existing entries — so a
directory of 10 000 fragments does not produce O(N²) total work.
"""

PROVENANCE_FILENAME = "provenance.jsonl"
"""Append-only provenance log filename under ``00-Creek-Meta/Processing-Log/``.

Backed by :class:`creek.audit.AuditLog` (sha256 hash chain + flock +
fsync) per Batch C; the JSONL shape keeps each append O(1) and the
chain detects post-hoc tampering via :meth:`AuditLog.verify`.
"""

LEGACY_PROVENANCE_FILENAME = "provenance.json"
"""Pre-Batch-C/E provenance filename. Migrated on VaultWriter construction."""

_PIPE_BUF_BYTES = 4096
"""Conservative upper bound on a single ``O_APPEND`` ``write(2)``.

POSIX's ``PIPE_BUF`` atomicity guarantee technically applies only to
pipes and FIFOs. For **regular files** on Linux local filesystems
(ext4, xfs, btrfs, …), the kernel's VFS write lock causes a single
``write(2)`` to land as one contiguous chunk regardless of size — but
that is a Linux implementation detail, not a portable POSIX contract.
Network filesystems (NFS) and non-Linux platforms may split larger
writes between concurrent appenders.

This module bounds its append-mode writers at ``PIPE_BUF`` (4 096
bytes on Linux; 512 bytes on some BSDs) so the same code is safe
across the platforms this project supports without relying on the
Linux-specific VFS behaviour. A JSON line of
``{id, type, path, written_at}`` is well under that limit in
practice, but an unusually long fragment ID or path would push past
it; the producers in this module assert their encoded line size
against ``_PIPE_BUF_BYTES`` so a regression surfaces loudly rather
than silently risking interleaved writes.
"""


def _read_legacy_provenance_entries(
    legacy_path: Path,
) -> tuple[list[dict[str, object]], str]:
    """Return (entries, status) for the legacy provenance log.

    ``status`` is one of:

    * ``"ok"`` — file parsed cleanly (entries may still be empty if the
      file held an empty array).
    * ``"read_failed"`` — :class:`OSError` while reading the file.
    * ``"parse_failed"`` — file existed but did not parse as JSON or
      did not contain a list.

    The status is stamped on the migration marker so an operator
    inspecting the audit log can distinguish a clean migration of an
    empty legacy file from a transient I/O error that lost data.
    """
    try:
        raw = legacy_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read legacy provenance log %s", legacy_path)
        return [], "read_failed"
    if not raw.strip():
        return [], "ok"
    try:  # noqa: TRY101  # Separate failure modes: file IO vs JSON parsing each return distinct status strings.
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Legacy provenance log %s is not valid JSON; skipping migration",
            legacy_path,
        )
        return [], "parse_failed"
    if not isinstance(data, list):
        return [], "parse_failed"
    return [item for item in data if isinstance(item, dict)], "ok"


def _provenance_migration_marker(
    legacy_path: Path,
    migrated_count: int,
    status: str,
) -> dict[str, object]:
    """Build the migration marker entry recorded in the new chain.

    Timestamp is UTC-stamped to match every other audit log entry in
    the system; a naive ``datetime.now()`` would produce a local-time
    string that would not be comparable to the ``timestamp`` fields in
    the surrounding compliance entries.
    """
    return {
        "id": "_migration_",
        "type": "provenance.migration",
        "path": str(legacy_path),
        "written_at": datetime.now(tz=UTC).isoformat(),
        "migrated_entries": migrated_count,
        "migration_status": status,
    }


def _migrate_legacy_provenance(
    legacy_path: Path,
    new_log: AuditLog,
) -> None:
    """One-shot migration of pre-Batch-C ``provenance.json`` array.

    Older vaults shipped a JSON-array log at ``provenance.json``;
    Batch C switched to JSONL via :class:`AuditLog` so the log is
    chained, append-only, and crash-safe. We replay every legacy
    entry into the new chain in original order, append a marker
    line so the migration is auditable, and unlink the legacy
    file. If the new log is already populated we leave the legacy
    file alone so a partially-migrated state can be resolved by
    inspection.

    Defensively strips any caller-supplied ``prev_hash`` field from
    legacy entries before append — :meth:`AuditLog.append` rejects
    payloads that try to forge the chain key, and we want migration
    of older logs to be tolerant rather than blow up at startup.
    """
    if not legacy_path.exists():
        return
    if new_log.path.exists() and new_log.path.stat().st_size > 0:
        # JSONL already populated AND legacy still on disk: a previous
        # attempt wrote some entries then crashed before unlinking, or
        # an operator copied the legacy file back in. Either way the
        # state is half-migrated and silently doing nothing would let
        # the inconsistency drift indefinitely. Surface it explicitly
        # so an operator inspecting logs notices.
        logger.warning(
            "Provenance migration: %s exists and %s also exists with content; "
            "skipping migration. Inspect both files and remove %s by hand once "
            "you have confirmed every legacy entry is in the new log.",
            legacy_path,
            new_log.path,
            legacy_path,
        )
        return
    legacy_entries, status = _read_legacy_provenance_entries(legacy_path)
    try:
        for entry in legacy_entries:
            sanitised = {k: v for k, v in entry.items() if k != "prev_hash"}
            new_log.append(sanitised)
        new_log.append(
            _provenance_migration_marker(legacy_path, len(legacy_entries), status),
        )
    except OSError:
        # Mid-migration failure (disk full, permission flip). The new
        # log now has partial content so the size guard above will
        # short-circuit subsequent attempts; the legacy file is left
        # in place deliberately so no entries are lost. Warn loudly so
        # the operator can resolve it before the next run silently
        # treats the partial state as "already migrated".
        logger.exception(
            "Provenance migration of %s into %s failed mid-write; legacy file "
            "left intact for manual reconciliation.",
            legacy_path,
            new_log.path,
        )
        raise
    legacy_path.unlink(missing_ok=True)


def _sanitize_title(title: str) -> str:
    """Sanitise a title string into a safe filename component.

    Removes non-word, non-space, non-hyphen characters and truncates
    the result to 80 characters.

    Args:
        title: The raw title string.

    Returns:
        A sanitised string suitable for use in a filename.
    """
    # \w includes unicode word chars — intentional for international content
    cleaned = re.sub(r"[^\w\s-]", "", title)
    cleaned = cleaned.strip().replace(" ", "-")
    return cleaned[:_MAX_FILENAME_LENGTH]


def _render_thread_body(thread: Thread) -> str:
    """Render a one-paragraph summary body for a Thread."""
    desc = thread.description.strip() or (
        f"Thread `{thread.title}` carries {thread.fragment_count} fragments "
        f"and is currently {thread.status}."
    )
    return f"{desc}\n"


def _render_eddy_body(eddy: Eddy) -> str:
    """Render a one-paragraph summary body for an Eddy."""
    desc = eddy.description.strip() or (
        f"Eddy `{eddy.title}` clusters {eddy.fragment_count} fragments "
        f"across {len(eddy.threads)} thread(s)."
    )
    return f"{desc}\n"


def _render_praxis_body(praxis: Praxis) -> str:
    """Render a one-paragraph summary body for a Praxis."""
    return (
        f"Praxis `{praxis.title}` is a {praxis.praxis_type} "
        f"({praxis.status}); review {praxis.review_interval}.\n"
    )


def _render_decision_body(decision: Decision) -> str:
    """Render a one-paragraph summary body for a Decision."""
    body_parts = [
        f"Decision `{decision.title}` is currently {decision.status}.",
    ]
    if decision.options:
        body_parts.append(
            "Options under consideration: " + ", ".join(decision.options) + ".",
        )
    if decision.outcome:
        body_parts.append(f"Outcome: {decision.outcome}.")
    return " ".join(body_parts) + "\n"


def _extract_date_str(model: BaseModel) -> str:
    """Extract a date string from a model for use in filename prefix.

    Inspects model fields in order: ``created``, ``first_seen``,
    ``formed``, ``opened``. Falls back to today's date.

    Args:
        model: A Pydantic model instance.

    Returns:
        An ISO-format date string (YYYY-MM-DD).
    """
    for attr in ("created", "first_seen", "formed", "opened"):
        value = getattr(model, attr, None)
        if value is not None:
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%d")
            if isinstance(value, date):
                return value.isoformat()
    return date.today().isoformat()


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write *content* to *path* via a unique tempfile + ``os.replace``.

    The temp file is created with ``tempfile.NamedTemporaryFile`` in
    *path*'s directory so two concurrent writers (in-process or
    cross-process) do not collide on a fixed sidecar name. The rename
    is atomic on POSIX once both files share a filesystem.
    """
    target_dir = path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(target_dir),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            fp.write(content)
        os.replace(tmp_name, path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _apply_other_author_attribution(
    fragment: Fragment,
    manifest: AuthorManifest,
) -> Fragment:
    """Return a copy of *fragment* stamped with *manifest* attribution (#470).

    Pure function — *fragment* is never mutated; a new ``Fragment`` (with a new
    ``FragmentSource``) is returned. Only the *attribution* axis changes — the
    fragment's frequency / wavelength / voice (its IDEAS classification, set
    upstream) is carried over untouched:

    * ``source.author_slug`` ← the manifest slug (the folder name).
    * ``source.author`` ← per :data:`_AUTHOR_KIND_TO_AUTHORSHIP`
      (``ai_as_user`` → AI, ``collaborator`` → COLLABORATIVE, otherwise OTHER).
    * ``representativeness`` ← ``"endorsed"`` forced for ``ai_as_user``,
      otherwise the manifest's ``representativeness`` (default ``"reference"``).
    * ``voice_weight`` ← the manifest's ``voice_weight`` (default ``0.0``, and
      ``0.0`` when the manifest was loaded fail-closed from a missing file).

    Returning a copy (rather than mutating in place) means a future caller can't
    reintroduce the caller-mutation bug the writer's deep copy used to guard
    against (#500).

    Args:
        fragment: The fragment to stamp (read-only).
        manifest: The governing ``11-Other-Authors/<slug>/`` manifest.

    Returns:
        A new ``Fragment`` carrying the borrowed-author attribution.
    """
    representativeness = (
        "endorsed"
        if manifest.author_kind == "ai_as_user"
        else manifest.representativeness
    )
    source = fragment.source.model_copy(
        update={
            "author_slug": manifest.author_slug,
            "author": _AUTHOR_KIND_TO_AUTHORSHIP.get(
                manifest.author_kind,
                Authorship.OTHER,
            ),
        },
    )
    return fragment.model_copy(
        update={
            "source": source,
            "representativeness": representativeness,
            "voice_weight": manifest.voice_weight,
        },
    )


class VaultWriter:
    """Write Creek ontological primitives to an Obsidian vault.

    Each ``write_*`` method serialises a Pydantic model as a markdown
    file with YAML frontmatter, placing it in the correct vault subfolder.
    Duplicate detection is based on the model's ``id`` field: if a file
    containing that ID already exists in the target directory, the write
    is skipped and the existing path is returned.

    Per-directory ``.id-index.jsonl`` files persist the ``id -> filename``
    mapping so duplicate detection is O(1) per write rather than scanning
    every markdown file in the directory. File creation uses
    ``O_CREAT | O_EXCL`` so two concurrent threads picking the same
    filename always retry with a counter suffix instead of clobbering
    each other.

    Args:
        vault_path: Path to the root of the Obsidian vault.

    Raises:
        FileNotFoundError: If ``vault_path`` does not exist or is missing
            required vault directories.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialise the VaultWriter and validate vault structure.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Raises:
            FileNotFoundError: If the vault path does not exist or
                required directories are missing.
        """
        if not vault_path.exists():
            msg = f"Vault path does not exist: {vault_path}"
            raise FileNotFoundError(msg)

        required_dirs = ["00-Creek-Meta", "01-Fragments"]
        for d in required_dirs:
            if not (vault_path / d).is_dir():
                msg = f"Required vault directory missing: {d}"
                raise FileNotFoundError(msg)

        self.vault_path = vault_path
        # Per-directory in-memory caches of the ``id -> filename`` index.
        # Loaded lazily on first access; persisted to disk on every
        # write so a second process picks up entries written by the first.
        self._dir_indexes: dict[Path, dict[str, str]] = {}
        # Single process-level lock guards the in-memory index cache.
        # Cross-process safety for the index is out of scope (BUG-006);
        # the provenance log uses creek.audit.AuditLog which has its own
        # cross-process flock.
        self._lock = threading.Lock()
        # Reuse a single AuditLog instance across every provenance
        # append so the per-instance hash cache stays warm across the
        # vault-writer ingest loop (Batch C, PR #193). A fresh AuditLog
        # per call would collapse the cache and re-introduce the O(N²)
        # read pattern that PERF-002 was supposed to eliminate.
        processing_log_dir = vault_path / "00-Creek-Meta" / "Processing-Log"
        self._provenance_log = AuditLog(processing_log_dir / PROVENANCE_FILENAME)
        # Legacy provenance.json migration: replays into the chained
        # JSONL on first VaultWriter construction, then unlinks. The
        # _migrate_legacy_provenance helper short-circuits if the new
        # log already has content, so subsequent VaultWriter instances
        # in the same process pay only a single stat.
        _migrate_legacy_provenance(
            processing_log_dir / LEGACY_PROVENANCE_FILENAME,
            self._provenance_log,
        )

    def write_fragment(self, fragment: Fragment, body: str = "") -> Path:
        """Write a Fragment to the correct vault folder, stamping attribution.

        A *native* fragment (``source.author_slug`` is ``None``/empty) routes by
        source platform via the total ``_PLATFORM_SUBFOLDER`` mapping into
        ``01-Fragments/<subfolder>/``, unchanged.

        A *borrowed* fragment (``source.author_slug`` set) is other-author
        content (#470): it routes into ``11-Other-Authors/<slug>/`` instead, and
        its ATTRIBUTION (author / author_slug / representativeness / voice_weight)
        is stamped from that slug's ``_author.md`` manifest via a fail-closed
        loader — a missing/corrupt manifest yields ``voice_weight=0.0``. The
        fragment's IDEAS classification (frequency / wavelength / voice, set
        upstream) is never touched.

        The ``body`` parameter holds the converted markdown content, which is
        written below the YAML frontmatter — without it the fragment file would
        contain only metadata (the historical bug).

        Args:
            fragment: The Fragment model to write.
            body: The markdown body to render below the frontmatter.
                Empty strings are accepted (e.g. for placeholder writes
                in tests) but should be considered a code smell in
                production callers.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        target_dir = self._fragment_target_dir(fragment)
        slug = fragment.source.author_slug
        if slug:
            # The helper returns a stamped copy, so the caller's Fragment is
            # never mutated — only the written file carries the manifest
            # attribution (#470).
            manifest = load_author_manifest_or_default(self.vault_path, slug)
            stamped = _apply_other_author_attribution(fragment, manifest)
            return self._write_model(stamped, target_dir, body=body)
        return self._write_model(fragment, target_dir, body=body)

    def _fragment_target_dir(self, fragment: Fragment) -> Path:
        """Return the vault directory a fragment routes to (#673).

        The single source of truth for fragment routing, used by both
        :meth:`write_fragment` and :meth:`update_fragment`: a borrowed
        fragment (``source.author_slug`` set) lives under
        ``11-Other-Authors/<slug>/``; a native fragment routes by source
        platform into ``01-Fragments/<subfolder>/``.
        """
        slug = fragment.source.author_slug
        if slug:
            return self.vault_path / OTHER_AUTHORS_DIR / slug
        subfolder = _PLATFORM_SUBFOLDER[str(fragment.source.platform)]
        return self.vault_path / "01-Fragments" / subfolder

    def update_fragment(self, fragment: Fragment, body: str) -> Path | None:
        """Rewrite an existing fragment's body in place, or return ``None``.

        Locates the file already mapped to ``fragment.id`` (via the per-dir
        id index) and rewrites **only its body**, preserving the on-disk
        frontmatter verbatim — id, classifications (OPS-001), resonance links,
        and ``source.origin_key``. This is the changed-branch of idempotent
        mutable-source ingest (#673): an edited journal entry updates the same
        fragment instead of minting a new id and orphaning the old one.

        Returns ``None`` when no file is mapped to ``fragment.id`` (e.g. the
        file was removed out of band), so the caller can fall back to a fresh
        :meth:`write_fragment`.

        Args:
            fragment: The fragment whose id locates the existing file and
                whose platform/slug routes the lookup directory.
            body: The new markdown body to write below the preserved
                frontmatter.

        Returns:
            Path to the rewritten file, or ``None`` when no existing file
            maps to the id.
        """
        target_dir = self._fragment_target_dir(fragment)
        with self._lock:
            existing = self._find_existing_locked(fragment.id, target_dir)
            if existing is None:
                return None
            post = frontmatter.load(str(existing))
            post.content = body
            _atomic_write_text(existing, frontmatter.dumps(post))
            # Record the in-place edit in the provenance log too, so the audit
            # trail captures subsequent rewrites — not just the original write.
            self._log_provenance_locked(fragment.id, fragment.type, existing)
        return existing

    def write_thread(self, thread: Thread) -> Path:
        """Write a Thread to 02-Threads/{status}/.

        The subfolder is determined by the thread's status field,
        capitalised (e.g. Active, Dormant, Resolved). A short summary
        body is rendered automatically.

        Args:
            thread: The Thread model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        status_folder = str(thread.status).capitalize()
        target_dir = self.vault_path / "02-Threads" / status_folder
        return self._write_model(thread, target_dir, body=_render_thread_body(thread))

    def write_eddy(self, eddy: Eddy) -> Path:
        """Write an Eddy to 03-Eddies/.

        A short summary body describing the eddy's fragment count and
        bridged threads is rendered automatically.

        Args:
            eddy: The Eddy model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        target_dir = self.vault_path / "03-Eddies"
        return self._write_model(eddy, target_dir, body=_render_eddy_body(eddy))

    def write_praxis(self, praxis: Praxis) -> Path:
        """Write a Praxis to 04-Praxis/{type}/.

        Maps praxis_type to subfolder: habit/practice -> Daily,
        framework/commitment -> Seasonal, insight -> Situational.
        A short summary body is rendered automatically.

        Args:
            praxis: The Praxis model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        subfolder = _PRAXIS_SUBFOLDER.get(str(praxis.praxis_type), "Situational")
        target_dir = self.vault_path / "04-Praxis" / subfolder
        return self._write_model(praxis, target_dir, body=_render_praxis_body(praxis))

    def write_decision(self, decision: Decision) -> Path:
        """Write a Decision to 08-Decisions/{status}/.

        Active statuses (sensing, deliberating, committing) go to
        Active/. Completed statuses (enacted, reflecting) go to Archive/.
        A short summary body is rendered automatically.

        Args:
            decision: The Decision model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        subfolder = (
            "Active" if str(decision.status) in _ACTIVE_DECISION_STATUSES else "Archive"
        )
        target_dir = self.vault_path / "08-Decisions" / subfolder
        return self._write_model(
            decision,
            target_dir,
            body=_render_decision_body(decision),
        )

    def write_any(self, model: BaseModel) -> Path:
        """Dispatch to the appropriate write method based on the model's type field.

        Inspects the ``type`` attribute of the model and calls the
        corresponding ``write_*`` method.

        .. note::

            For ``Fragment`` models this dispatch path produces a
            **header-only** vault file because there is no body to
            forward through the generic ``BaseModel`` signature. The
            primary ingestion path bypasses ``write_any`` and calls
            :meth:`write_fragment` directly with the converted Markdown
            body. Reach for ``write_any`` only when you genuinely have
            no body — e.g. compaction tooling that re-writes existing
            metadata blobs.

        Args:
            model: A Pydantic model with a ``type`` field.

        Returns:
            Path to the written (or existing duplicate) markdown file.

        Raises:
            ValueError: If the model's type is not recognised.
        """
        type_field = getattr(model, "type", None)
        dispatch: dict[str, Callable[..., Path]] = {
            "fragment": self.write_fragment,
            "thread": self.write_thread,
            "eddy": self.write_eddy,
            "praxis": self.write_praxis,
            "decision": self.write_decision,
        }
        writer = dispatch.get(str(type_field))
        if writer is None:
            msg = f"Unsupported model type: {type_field}"
            raise ValueError(msg)
        return writer(model)

    def _write_model(
        self,
        model: BaseModel,
        target_dir: Path,
        *,
        body: str = "",
    ) -> Path:
        """Serialise a model to markdown with YAML frontmatter and write to disk.

        Uses the per-directory ID index for O(1) duplicate detection,
        creates the file atomically via ``O_CREAT | O_EXCL`` to prevent
        concurrent writers from clobbering each other, and updates the
        index transactionally before logging provenance.

        Args:
            model: The Pydantic model to serialise.
            target_dir: The vault directory to write the file to.
            body: Markdown body to render below the frontmatter block.

        Returns:
            Path to the written (or existing duplicate) file.
        """
        model_id: str = getattr(model, "id", "")

        # The lock is intentionally held across the whole write —
        # duplicate check, file creation, index append, and provenance
        # append all run serially within a single process. Narrowing
        # the critical section would re-introduce the TOCTOU between
        # ``_find_existing_locked`` and ``_atomic_create`` that BUG-006
        # was filed to close. The throughput trade-off is documented
        # in the PR description; revisit only with a benchmark-driven
        # follow-up issue.
        with self._lock:
            existing = self._find_existing_locked(model_id, target_dir)
            if existing is not None:
                return existing

            target_dir.mkdir(parents=True, exist_ok=True)
            base_name = self._compute_base_name(model)

            data = model.model_dump(mode="json")
            post = frontmatter.Post(content=body, **data)
            content = frontmatter.dumps(post)

            file_path = self._atomic_create(target_dir, base_name, content)

            # Update the in-memory + on-disk index transactionally with
            # the file write so a follow-up call (or a fresh process)
            # finds the entry without a directory rescan. Re-using
            # ``_load_index_locked`` rather than indexing
            # ``self._dir_indexes`` directly keeps the cache-population
            # contract local — the dup-check above happened to load
            # it, but a future refactor that skips the dup-check
            # branch would otherwise hit a ``KeyError`` here.
            index = self._load_index_locked(target_dir)
            index[model_id] = file_path.name
            self._append_index_entry(target_dir, model_id, file_path.name)

            self._log_provenance_locked(
                model_id,
                str(getattr(model, "type", "")),
                file_path,
            )
        return file_path

    def _find_existing_locked(
        self,
        model_id: str,
        target_dir: Path,
    ) -> Path | None:
        """Return the path of an existing file for *model_id*, or ``None``.

        Caller must hold ``self._lock``. Loads (and caches) the
        per-directory index on first access; rebuilds it from a
        directory scan if the index file is missing or corrupt so
        existing vaults remain compatible.

        Args:
            model_id: The ID to search for.
            target_dir: The directory to search in.

        Returns:
            The path to the existing file, or ``None`` if not indexed
            or the indexed path no longer exists on disk.
        """
        index = self._load_index_locked(target_dir)
        filename = index.get(model_id)
        if filename is None:
            return None
        candidate = target_dir / filename
        if candidate.exists():
            return candidate
        # Stale entry — drop it from the in-memory cache so the next
        # write reuses the slot. The on-disk JSONL still contains the
        # stale line, but it is harmless: the next write appends a new
        # mapping that overrides it on rebuild, and a future compaction
        # pass (out of scope for this fix) can reclaim the space.
        del index[model_id]
        return None

    def _find_existing(self, model_id: str, target_dir: Path) -> Path | None:
        """Return the path of an existing file for *model_id*, or ``None``.

        Retained for tests and tooling that introspect the writer
        without going through ``_write_model``. Acquires ``self._lock``
        and delegates to the locked variant so the caller does not have
        to know about the lock invariant.

        Args:
            model_id: The ID to search for.
            target_dir: The directory to search in.

        Returns:
            The path to the existing file, or ``None`` if not found.
        """
        with self._lock:
            return self._find_existing_locked(model_id, target_dir)

    def _load_index_locked(self, target_dir: Path) -> dict[str, str]:
        """Return the cached per-directory index, loading it if needed.

        On first access for *target_dir* the index is reconstructed from
        the JSONL file (later entries overwrite earlier ones, so the
        last successful write wins). If the JSONL file is missing the
        index is rebuilt by scanning the directory's ``.md`` files and
        the result is persisted so subsequent processes skip the scan.
        """
        cached = self._dir_indexes.get(target_dir)
        if cached is not None:
            return cached
        index_path = target_dir / INDEX_FILENAME
        if index_path.exists():
            index = self._load_index_file(index_path)
        elif target_dir.is_dir():
            index = self._rebuild_index(target_dir)
            self._persist_full_index(target_dir, index)
        else:
            index = {}
        self._dir_indexes[target_dir] = index
        return index

    @staticmethod
    def _load_index_file(index_path: Path) -> dict[str, str]:
        """Parse a JSONL index file into ``{id: filename}``.

        Malformed lines are skipped so a partial write under crash
        cannot poison the rest of the index. Later entries overwrite
        earlier ones — appending an entry for an existing id therefore
        rewrites the mapping in-place.
        """
        index: dict[str, str] = {}
        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return index
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            filename = entry.get("filename")
            if isinstance(mid, str) and isinstance(filename, str):
                index[mid] = filename
        return index

    @staticmethod
    def _rebuild_index(target_dir: Path) -> dict[str, str]:
        """Scan *target_dir* for ``.md`` files and rebuild the ID index."""
        index: dict[str, str] = {}
        if not target_dir.is_dir():
            return index
        for md_file in target_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(md_file))
            except (OSError, ValueError, yaml.YAMLError):
                # A non-fragment or unparseable sibling (e.g. a corrupt
                # ``_author.md`` manifest) must not crash the index rebuild —
                # it simply has no fragment id to index (#470).
                continue
            mid = post.get("id")
            if isinstance(mid, str):
                index[mid] = md_file.name
        return index

    @staticmethod
    def _persist_full_index(target_dir: Path, index: dict[str, str]) -> None:
        """Atomically write a fresh JSONL index file from *index*.

        Used only on first-time index construction (scanning a vault
        that predates the index file). Steady-state updates use
        :meth:`_append_index_entry`, which is O(1) per write.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            json.dumps({"id": mid, "filename": filename}, sort_keys=True) + "\n"
            for mid, filename in sorted(index.items())
        )
        _atomic_write_text(target_dir / INDEX_FILENAME, lines)

    @staticmethod
    def _append_index_entry(target_dir: Path, model_id: str, filename: str) -> None:
        """Append a single ``{id, filename}`` JSON line to the index.

        Uses ``O_APPEND`` so concurrent appenders cannot interleave
        partial writes — POSIX guarantees this only for writes up to
        :data:`_PIPE_BUF_BYTES`, so the encoded line size is checked
        before the write to make a regression (e.g. an unusually long
        fragment ID or filename) fail loudly rather than risk silent
        interleaving.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        index_path = target_dir / INDEX_FILENAME
        encoded = (
            json.dumps({"id": model_id, "filename": filename}, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(encoded) > _PIPE_BUF_BYTES:
            msg = (
                f"Index entry exceeds PIPE_BUF "
                f"({len(encoded)} > {_PIPE_BUF_BYTES} bytes); "
                "atomic O_APPEND cannot be guaranteed."
            )
            raise ValueError(msg)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(str(index_path), flags, 0o644)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)

    @staticmethod
    def _compute_base_name(model: BaseModel) -> str:
        """Return the ``{date}-{title}`` filename stem for *model*."""
        date_str = _extract_date_str(model)
        title = getattr(model, "title", "")
        sanitized = _sanitize_title(title)
        return f"{date_str}-{sanitized}" if sanitized else date_str

    @staticmethod
    def _atomic_create(target_dir: Path, base_name: str, content: str) -> Path:
        """Create ``target_dir/{base_name}.md`` atomically; retry with a counter.

        Uses ``O_CREAT | O_EXCL`` so two concurrent callers picking the
        same filename will not clobber each other — the second call
        increments a counter suffix and retries. The retry loop is
        capped at :data:`_MAX_FILENAME_COLLISION_RETRIES` so a runaway
        contention pattern surfaces as a loud ``RuntimeError`` rather
        than spinning forever.

        Args:
            target_dir: Directory the file will live in.
            base_name: Filename stem (without ``.md``).
            content: Full file contents to write.

        Returns:
            The path of the created file.

        Raises:
            RuntimeError: If a unique filename cannot be obtained within
                :data:`_MAX_FILENAME_COLLISION_RETRIES` attempts.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        encoded = content.encode("utf-8")
        for counter in range(_MAX_FILENAME_COLLISION_RETRIES):
            suffix = "" if counter == 0 else f"-{counter}"
            candidate = target_dir / f"{base_name}{suffix}.md"
            try:
                fd = os.open(str(candidate), flags, 0o644)
            except FileExistsError:
                continue
            try:
                os.write(fd, encoded)
            finally:
                os.close(fd)
            return candidate
        msg = (
            f"Could not allocate a unique filename for '{base_name}.md' in "
            f"{target_dir} after {_MAX_FILENAME_COLLISION_RETRIES} attempts"
        )
        raise RuntimeError(msg)

    def _generate_filename(self, model: BaseModel, target_dir: Path) -> str:
        """Return a unique filename for *model* under *target_dir*.

        Retained for tests and tooling that introspect the writer; the
        production path uses :meth:`_atomic_create` directly to avoid
        the TOCTOU window between filename selection and file creation.
        New callers should prefer :meth:`_atomic_create`.

        Args:
            model: The model to generate a filename for.
            target_dir: The directory where the file will be written.

        Returns:
            A unique filename string ending in ``.md``.
        """
        base_name = self._compute_base_name(model)
        filename = f"{base_name}.md"
        if not (target_dir / filename).exists():
            return filename
        counter = 1
        while (target_dir / f"{base_name}-{counter}.md").exists():
            counter += 1
        return f"{base_name}-{counter}.md"

    def _log_provenance_locked(
        self,
        model_id: str,
        model_type: str,
        file_path: Path,
    ) -> None:
        """Append a provenance entry to the JSONL processing log.

        Caller must hold ``self._lock`` (Batch E lock-to-call-site
        contract). The log is hash-chained JSONL via
        :class:`creek.audit.AuditLog` (Batch C) — flock + fsync +
        prev_hash chain — stored at
        ``00-Creek-Meta/Processing-Log/provenance.jsonl``. Reusing the
        single :attr:`_provenance_log` instance keeps the per-instance
        hash cache warm across the ingest loop so each append is O(1)
        rather than re-reading the growing log.

        Legacy ``provenance.json`` (a JSON array) is migrated forward
        in :meth:`__init__` via :func:`_migrate_legacy_provenance`, so
        this hot-path method does no per-write filesystem stat.

        Args:
            model_id: The ID of the written model.
            model_type: The type string of the written model.
            file_path: The path where the model was written.
        """
        self._provenance_log.append(
            {
                "id": model_id,
                "type": model_type,
                "path": str(file_path),
                "written_at": datetime.now(tz=UTC).isoformat(),
            },
        )

    def _log_provenance(
        self,
        model_id: str,
        model_type: str,
        file_path: Path,
    ) -> None:
        """Backwards-compatible shim for callers that bypass ``_write_model``."""
        with self._lock:
            self._log_provenance_locked(model_id, model_type, file_path)
