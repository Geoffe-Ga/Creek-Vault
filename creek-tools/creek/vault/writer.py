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
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

import frontmatter

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

# Map source platform -> 01-Fragments subfolder
_PLATFORM_SUBFOLDER: dict[str, str] = {
    SourcePlatform.CLAUDE: "Conversations",
    SourcePlatform.CHATGPT: "Conversations",
    SourcePlatform.DISCORD: "Messages",
    SourcePlatform.ESSAY: "Writing",
    SourcePlatform.JOURNAL: "Journal",
    SourcePlatform.CODE: "Technical",
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

    def write_fragment(self, fragment: Fragment) -> Path:
        """Write a Fragment to the appropriate 01-Fragments/ subfolder.

        Maps the fragment's source platform to a subfolder (e.g. claude
        and chatgpt -> Conversations, discord -> Messages). Platforms
        without an explicit mapping go to Unsorted.

        Args:
            fragment: The Fragment model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        platform = fragment.source.platform
        subfolder = _PLATFORM_SUBFOLDER.get(str(platform), "Unsorted")
        target_dir = self.vault_path / "01-Fragments" / subfolder
        return self._write_model(fragment, target_dir)

    def write_thread(self, thread: Thread) -> Path:
        """Write a Thread to 02-Threads/{status}/.

        The subfolder is determined by the thread's status field,
        capitalised (e.g. Active, Dormant, Resolved).

        Args:
            thread: The Thread model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        status_folder = str(thread.status).capitalize()
        target_dir = self.vault_path / "02-Threads" / status_folder
        return self._write_model(thread, target_dir)

    def write_eddy(self, eddy: Eddy) -> Path:
        """Write an Eddy to 03-Eddies/.

        Args:
            eddy: The Eddy model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        target_dir = self.vault_path / "03-Eddies"
        return self._write_model(eddy, target_dir)

    def write_praxis(self, praxis: Praxis) -> Path:
        """Write a Praxis to 04-Praxis/{type}/.

        Maps praxis_type to subfolder: habit/practice -> Daily,
        framework/commitment -> Seasonal, insight -> Situational.

        Args:
            praxis: The Praxis model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        subfolder = _PRAXIS_SUBFOLDER.get(str(praxis.praxis_type), "Situational")
        target_dir = self.vault_path / "04-Praxis" / subfolder
        return self._write_model(praxis, target_dir)

    def write_decision(self, decision: Decision) -> Path:
        """Write a Decision to 08-Decisions/{status}/.

        Active statuses (sensing, deliberating, committing) go to
        Active/. Completed statuses (enacted, reflecting) go to Archive/.

        Args:
            decision: The Decision model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        subfolder = (
            "Active" if str(decision.status) in _ACTIVE_DECISION_STATUSES else "Archive"
        )
        target_dir = self.vault_path / "08-Decisions" / subfolder
        return self._write_model(decision, target_dir)

    def write_any(self, model: BaseModel) -> Path:
        """Dispatch to the appropriate write method based on the model's type field.

        Inspects the ``type`` attribute of the model and calls the
        corresponding ``write_*`` method.

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

    def _write_model(self, model: BaseModel, target_dir: Path) -> Path:
        """Serialise a model to markdown with YAML frontmatter and write to disk.

        Handles duplicate detection (by ID), filename generation,
        frontmatter serialisation, and provenance logging.

        Args:
            model: The Pydantic model to serialise.
            target_dir: The vault directory to write the file to.

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
        post = frontmatter.Post(content="", **data)
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

        The log is a JSON array stored at
        ``00-Creek-Meta/Processing-Log/provenance.json``.

        Args:
            model_id: The ID of the written model.
            model_type: The type string of the written model.
            file_path: The path where the model was written.
        """
        log_dir = self.vault_path / "00-Creek-Meta" / "Processing-Log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "provenance.json"

        entries: list[dict[str, str]] = []
        if log_path.exists():
            raw = log_path.read_text(encoding="utf-8")
            if raw.strip():
                entries = json.loads(raw)

        entry: dict[str, str] = {
            "id": model_id,
            "type": model_type,
            "path": str(file_path),
            "written_at": datetime.now().isoformat(),
        }
        entries.append(entry)
        log_path.write_text(
            json.dumps(entries, indent=2),
            encoding="utf-8",
        )
