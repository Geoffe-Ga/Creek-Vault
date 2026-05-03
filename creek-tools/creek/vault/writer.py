"""Vault writer — write markdown files with YAML frontmatter to vault folders.

This module provides the ``VaultWriter`` class, which serialises Creek
ontological primitives (Fragment, Thread, Eddy, Praxis, Decision) as
Obsidian-compatible markdown files with YAML frontmatter. It handles:

- Mapping each primitive to the correct vault subfolder
- Sanitising titles into safe filenames
- Detecting duplicates (by ID) and skipping re-writes
- Appending provenance entries to the processing log
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.audit import AuditLog
from creek.models import (
    DecisionStatus,
    PraxisType,
    SourcePlatform,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from creek.models import (
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
    SourcePlatform.JOURNAL: "Journal",
    SourcePlatform.CODE: "Technical",
    SourcePlatform.MARKDOWN: "Notes",
    SourcePlatform.DOCUMENT: "Documents",
    SourcePlatform.SPREADSHEET: "Data",
    SourcePlatform.PRESENTATION: "Decks",
    SourcePlatform.IMAGE_OCR: "Images",
    SourcePlatform.OTHER: "Unsorted",
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


def _read_legacy_provenance_entries(legacy_path: Path) -> list[dict[str, object]]:
    """Return parsed legacy provenance entries, or empty on any failure."""
    try:
        raw = legacy_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read legacy provenance log %s", legacy_path)
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Legacy provenance log %s is not valid JSON; skipping migration",
            legacy_path,
        )
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _provenance_migration_marker(
    legacy_path: Path,
    migrated_count: int,
) -> dict[str, object]:
    """Build the migration marker entry recorded in the new chain."""
    return {
        "id": "_migration_",
        "type": "provenance.migration",
        "path": str(legacy_path),
        "written_at": datetime.now().isoformat(),
        "migrated_entries": migrated_count,
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
    """
    if not legacy_path.exists():
        return
    if new_log.path.exists() and new_log.path.stat().st_size > 0:
        return
    legacy_entries = _read_legacy_provenance_entries(legacy_path)
    for entry in legacy_entries:
        new_log.append(entry)
    new_log.append(_provenance_migration_marker(legacy_path, len(legacy_entries)))
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


class VaultWriter:
    """Write Creek ontological primitives to an Obsidian vault.

    Each ``write_*`` method serialises a Pydantic model as a markdown
    file with YAML frontmatter, placing it in the correct vault subfolder.
    Duplicate detection is based on the model's ``id`` field: if a file
    containing that ID already exists in the target directory, the write
    is skipped and the existing path is returned.

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
        # Reuse a single AuditLog instance across every provenance
        # append so the per-instance hash cache stays warm across the
        # vault-writer ingest loop. A fresh AuditLog per call would
        # collapse the cache and re-introduce the O(N²) read flagged
        # on PR #193 (BLOCKING review item, comment 4365147477).
        processing_log_dir = vault_path / "00-Creek-Meta" / "Processing-Log"
        self._provenance_log = AuditLog(processing_log_dir / "provenance.jsonl")
        _migrate_legacy_provenance(
            processing_log_dir / "provenance.json",
            self._provenance_log,
        )

    def write_fragment(self, fragment: Fragment, body: str = "") -> Path:
        """Write a Fragment to the appropriate 01-Fragments/ subfolder.

        Maps the fragment's source platform to a subfolder via the total
        ``_PLATFORM_SUBFOLDER`` mapping. The ``body`` parameter holds the
        converted markdown content, which is written below the YAML
        frontmatter — without it the fragment file would contain only
        metadata (the historical bug).

        Args:
            fragment: The Fragment model to write.
            body: The markdown body to render below the frontmatter.
                Empty strings are accepted (e.g. for placeholder writes
                in tests) but should be considered a code smell in
                production callers.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        platform = fragment.source.platform
        subfolder = _PLATFORM_SUBFOLDER[str(platform)]
        target_dir = self.vault_path / "01-Fragments" / subfolder
        return self._write_model(fragment, target_dir, body=body)

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

        Handles duplicate detection (by ID), filename generation,
        frontmatter serialisation, body rendering, and provenance logging.

        Args:
            model: The Pydantic model to serialise.
            target_dir: The vault directory to write the file to.
            body: Markdown body to render below the frontmatter block.

        Returns:
            Path to the written (or existing duplicate) file.
        """
        model_id: str = getattr(model, "id", "")
        existing = self._find_existing(model_id, target_dir)
        if existing is not None:
            return existing

        target_dir.mkdir(parents=True, exist_ok=True)

        filename = self._generate_filename(model, target_dir)
        file_path = target_dir / filename

        data = model.model_dump(mode="json")
        post = frontmatter.Post(content=body, **data)
        content = frontmatter.dumps(post)
        file_path.write_text(content, encoding="utf-8")

        self._log_provenance(model_id, str(getattr(model, "type", "")), file_path)
        return file_path

    def _find_existing(self, model_id: str, target_dir: Path) -> Path | None:
        """Search for an existing file with the given model ID in target_dir.

        Reads the frontmatter of each ``.md`` file in the directory and
        checks if its ``id`` field matches.

        Args:
            model_id: The ID to search for.
            target_dir: The directory to search in.

        Returns:
            The path to the existing file, or ``None`` if not found.
        """
        if not target_dir.exists():
            return None
        for md_file in target_dir.glob("*.md"):
            post = frontmatter.load(str(md_file))
            if post.get("id") == model_id:
                return md_file
        return None

    def _generate_filename(self, model: BaseModel, target_dir: Path) -> str:
        """Generate a unique filename for the model.

        Format: ``{date}-{sanitised_title}.md``. If a file with the
        same name already exists (title collision with different ID),
        a numeric suffix is appended.

        Args:
            model: The model to generate a filename for.
            target_dir: The directory where the file will be written.

        Returns:
            A unique filename string ending in ``.md``.
        """
        date_str = _extract_date_str(model)
        title = getattr(model, "title", "")
        sanitized = _sanitize_title(title)

        base_name = f"{date_str}-{sanitized}" if sanitized else date_str

        filename = f"{base_name}.md"
        if not (target_dir / filename).exists():
            return filename

        counter = 1
        while (target_dir / f"{base_name}-{counter}.md").exists():
            counter += 1
        return f"{base_name}-{counter}.md"

    def _log_provenance(
        self,
        model_id: str,
        model_type: str,
        file_path: Path,
    ) -> None:
        """Append a provenance entry to the processing log.

        The log is a JSONL stream (one entry per line, hash-chained for
        free via :class:`creek.audit.AuditLog`) stored at
        ``00-Creek-Meta/Processing-Log/provenance.jsonl``. Switching from
        the previous JSON-array shape eliminates the read-modify-write
        cycle that made appends ``O(n)`` in log size and dropped
        concurrent writes (see PERF-002 / BUG-006).

        Reuses :attr:`_provenance_log` so the per-instance hash cache
        stays warm across calls — vital for 10k-fragment ingest paths
        where a transient :class:`AuditLog` per call would re-read the
        whole log every append.

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
                "written_at": datetime.now().isoformat(),
            },
        )
