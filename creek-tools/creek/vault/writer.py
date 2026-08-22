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
import difflib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import frontmatter

from creek._fsio import create_exclusive, write_all
from creek.audit import AuditLog
from creek.models import (
    Authorship,
    DecisionStatus,
    PraxisType,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.authors import (
    OTHER_AUTHORS_DIR,
    load_author_manifest_or_default,
)
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS, load_post_or_raise

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

# Vault-relative roots this writer routes models into. Every destination is
# assembled from one of these names rather than an inline string literal so
# the scaffold drift guard (tests/test_vault_structure.py) can *derive* the
# directories ``creek init`` has to ship. Retyping a destination anywhere is
# how the scaffold and the writer drifted apart in the first place (#1025).
_META_RELPART: str = "00-Creek-Meta"
_PROCESSING_LOG_RELPARTS: tuple[str, str] = (_META_RELPART, "Processing-Log")
_FRAGMENTS_RELPART: str = "01-Fragments"
_THREADS_RELPART: str = "02-Threads"
_EDDIES_RELPART: str = "03-Eddies"
_PRAXIS_RELPART: str = "04-Praxis"
_DECISIONS_RELPART: str = "08-Decisions"

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

# Destination for soft-tombed fragments whose source unit has vanished (#674).
_ORPHANED_RELPARTS: tuple[str, ...] = ("10-Liminal", "Orphaned")

# Frontmatter key holding the serialised ``Fragment.voice_proxy_eligible``
# computed field (BUG-009). It is derived from ``privacy_tier`` +
# ``source.author``, so any code path that rewrites the tier on disk owes this
# key a refresh or the snapshot goes stale (#922).
_VOICE_PROXY_ELIGIBLE_KEY: str = "voice_proxy_eligible"

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
_ACTIVE_DECISION_SUBFOLDER: str = "Active"
_ARCHIVED_DECISION_SUBFOLDER: str = "Archive"

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


def _file_declares_id(path: Path, model_id: str) -> bool:
    """Return ``True`` only when *path*'s own frontmatter declares *model_id*.

    The verifier behind the id index (#1083): an index entry is a *claim*
    about a file, and this is the only thing that turns that claim into
    evidence. A missing ``id`` key, a non-``str`` id, and an unparseable
    file all answer ``False``.

    The guarded exception set and the ``isinstance(..., str)`` gate are
    deliberately identical to the ones
    :meth:`VaultWriter._rebuild_index` applies while scanning, so the
    verifier and the scanner can never disagree about which files
    declare an id — a file this function rejects is exactly a file the
    rebuild would decline to index. Both now name
    :data:`~creek.vault.reader.FRONTMATTER_LOAD_ERRORS`, and they must keep
    naming the *same* thing: widening one alone reintroduces the disagreement
    #1083 closed.

    Routing this through the splat-free
    :func:`creek.vault.links.read_header_meta` — it reads only ``id``, so it
    could — was measured and rejected. On a 1.9 KB fragment with ~20 header
    keys ``frontmatter.load`` costs 66 us against ``read_header_meta``'s 424 us,
    because the former resolves libyaml's C loader while the latter runs
    ``yaml.safe_load`` on the pure-Python one. This runs once per index *hit*,
    so on a 35k-fragment sweep that is ~2.3 s against ~14.8 s. The consequence
    — a file whose header will not parse resolves as "not found" rather than
    being reported — is issue #1543, whose fix is the bounded byte-scan for
    ``id`` that :meth:`VaultWriter._find_in_dir_locked` already names.
    """
    try:
        post = frontmatter.load(str(path))
    except FRONTMATTER_LOAD_ERRORS:
        return False
    declared = post.get("id")
    return isinstance(declared, str) and declared == model_id


class _IndexLoad(NamedTuple):
    """The outcome of parsing one ``.id-index.jsonl`` file (#1120).

    Separates *what the file said* from *whether the file was intact*,
    so a caller can tell an index that legitimately names nothing from
    one that was damaged and therefore names less than it should.

    Attributes:
        entries: The ``{id: filename}`` mappings that parsed cleanly.
        torn_lines: How many lines failed ``json.loads``. A torn append
            leaves exactly one; a crash-heavy history can leave several.
        read_failed: ``True`` when the file could not be read at all.
    """

    entries: dict[str, str]
    torn_lines: int
    read_failed: bool

    @property
    def damaged(self) -> bool:
        """Return ``True`` when *entries* may be missing a real mapping."""
        return self.torn_lines > 0 or self.read_failed


def _is_material_change(old_body: str, new_body: str, threshold: float) -> bool:
    """Return ``True`` when an edit changed the body materially (#675).

    A positive *threshold* compares old vs new body with ``difflib``; a
    similarity ratio below the threshold is a material change. A non-positive
    threshold never flags (every edit is treated as trivial).
    """
    if threshold <= 0.0:
        return False
    ratio = difflib.SequenceMatcher(None, old_body, new_body).ratio()
    return ratio < threshold


def _clear_classification(post: frontmatter.Post) -> None:
    """Drop classification frontmatter so OPS-001 re-classifies on next pass."""
    # Deferred import: `creek.classify.classify_engine` imports `VaultWriter`
    # from this module, so a module-level import of `creek.classify.constants`
    # here would close a load-time cycle. The constants are pure `Final[str]`;
    # extracting them to a dependency-free module would let this move to the top
    # (tracked as a follow-up).
    from creek.classify.constants import (
        CLASSIFICATION_METHOD_KEY,
        CLASSIFICATION_REASONING_KEY,
        CLASSIFIED_AT_KEY,
    )

    for key in (
        CLASSIFICATION_METHOD_KEY,
        CLASSIFIED_AT_KEY,
        CLASSIFICATION_REASONING_KEY,
    ):
        post.metadata.pop(key, None)


def _tier_on_disk(raw: object) -> PrivacyTier:
    """Parse a frontmatter ``privacy_tier`` value, failing closed to INTIMATE.

    Mirrors :func:`creek.classify.privacy_filter.tier_of` and
    ``creek_mcp.tools.reflect._fragment_tier``: a value this build does not
    recognise is a value nobody can vouch for, so it is treated as the
    most-restrictive tier rather than allowed to raise (which would abort an
    otherwise-valid in-place rewrite) or to fall through to ``open`` (which
    would expose the body). The legacy ``"public"`` spelling is still accepted
    — :meth:`creek.models.PrivacyTier._missing_` maps it to ``OPEN`` (INC-003).

    Args:
        raw: The frontmatter value, exactly as read from disk.

    Returns:
        The parsed tier, or ``INTIMATE`` when the value is unrecognised.
    """
    try:
        return PrivacyTier(str(raw))
    except ValueError:
        logger.warning(
            "Fragment file carries unrecognised privacy_tier %r; treating as "
            "INTIMATE while re-tiering an edited body. Re-run `creek classify` "
            "to assign a recognised tier.",
            raw,
        )
        return PrivacyTier.INTIMATE


def _retier_after_rewrite(
    post: frontmatter.Post,
    fragment: Fragment,
    body: str,
) -> None:
    """Re-derive ``privacy_tier`` from the *new* body after a material edit (#922).

    :func:`_clear_classification` drops the classification keys so the next
    pass revisits the fragment, but ``privacy_tier`` is not one of them — it
    would keep describing the body that was just overwritten. A fragment
    rewritten from an essay into recovery content therefore kept its ``open``
    tier and stayed admissible to an OPEN-ceiling MCP caller until someone
    happened to re-run a privacy pass.

    The policy mirrors :func:`creek.classify.privacy_pass.apply_tier` exactly,
    expressed in frontmatter terms rather than model terms:

    * The candidate comes from
      :class:`~creek.classify.privacy.PrivacyClassifier`, which is pure
      keyword/platform heuristics — no LLM call, no network, no config — so
      re-tiering on every material edit is free.
    * When the frontmatter still owes a tier
      (:func:`~creek.classify.privacy_pass.needs_tier`: key absent, or present
      but ``unclassified``) the candidate is taken outright; there is no
      decision to escalate against.
    * Otherwise the candidate is merged
      :func:`~creek.classify.privacy_pass.escalate`-only against the tier **on
      disk**, so an operator's ``intimate`` survives a benign rewrite. The
      merge base must be the on-disk value, *not* ``tier_of(fragment)``: the
      fragment the ingest pipeline hands us is freshly constructed and still
      carries the model's ``unclassified`` default, which ranks *below*
      ``open`` in ``privacy_pass._ESCALATION_RANK`` — escalating against it
      would silently discard the operator's decision.
    * ``voice_proxy_eligible`` is recomputed from the merged tier and the
      fragment's authorship. It is a *derived* field
      (:attr:`creek.models.Fragment.voice_proxy_eligible`, BUG-009) whose
      on-disk copy is only a serialised snapshot, so leaving it alone would
      strand a stale ``true`` on a now-intimate fragment and feed it straight
      into voice-proxy generation.

    Why re-derive, rather than either cheaper move that suggests itself:

    * **Stamping a blanket ``intimate`` on every material rewrite is
      permanent.** :func:`~creek.classify.privacy_pass.needs_tier` returns
      ``False`` for any explicit non-``unclassified`` tier, so no ordinary
      classify pass revisits it, and even ``creek classify --force`` merges
      through :func:`~creek.classify.privacy_pass.escalate`, which never
      lowers a tier. Nothing in the package downgrades ``privacy_tier`` — the
      purge engine explicitly excludes the key — so a blanket stamp would bury
      every edited fragment at ``intimate`` forever, with a hand edit of the
      frontmatter the only way back out.
    * **Resetting to ``unclassified`` would silently narrow exposure rather
      than describe the new body.** Since #961, ``creek_mcp.tier_ceiling``
      ranks ``UNCLASSIFIED`` with ``PERSONAL`` (#876), so a reset fragment
      would need an operator to re-run ``creek classify`` before an
      OPEN-ceiling MCP caller could read it again — correct as a side
      effect of the ranking, but the wrong tool for the job: a reset says
      nothing about what the *edited* body actually contains, whereas
      re-deriving keeps the tier truthful about it.

    Re-deriving avoids both: the tier always describes the current body, and
    the escalate-only merge preserves operator curation.

    Args:
        post: Loaded frontmatter of the file being rewritten. Its metadata is
            mutated in place; the caller writes the post back.
        fragment: The fragment being re-ingested, supplying the platform and
            authorship axes the classifier keys on. Never mutated.
        body: The new markdown body, scanned for recovery keywords.
    """
    # Deferred import for the same load-time cycle documented on
    # `_clear_classification`: `creek.classify.classify_engine` imports
    # `VaultWriter` from this module.
    from creek.classify.privacy import PrivacyClassifier
    from creek.classify.privacy_pass import PRIVACY_TIER_KEY, escalate, needs_tier

    # Mirrors `apply_tier`'s shape: assign outright when nothing is on record,
    # otherwise merge escalate-only. The ternary short-circuits, so the
    # subscript is only reached on the branch that proved the key is present.
    assigning = needs_tier(post.metadata)
    candidate = PrivacyClassifier().classify_tier(fragment, content=body)
    tier = (
        candidate
        if assigning
        else escalate(_tier_on_disk(post.metadata[PRIVACY_TIER_KEY]), candidate)
    )
    post.metadata[PRIVACY_TIER_KEY] = tier.value
    post.metadata[_VOICE_PROXY_ELIGIBLE_KEY] = (
        tier is not PrivacyTier.INTIMATE
        and Authorship(fragment.source.author) is Authorship.SELF
    )


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

        required_dirs = [_META_RELPART, _FRAGMENTS_RELPART]
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
        processing_log_dir = vault_path.joinpath(*_PROCESSING_LOG_RELPARTS)
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

    def write_fragment(
        self,
        fragment: Fragment,
        body: str = "",
        *,
        extra_frontmatter: dict[str, object] | None = None,
    ) -> Path:
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
            extra_frontmatter: Unmodelled frontmatter keys to merge into the
                YAML block, from an ingestor that emitted structured
                provenance ``Fragment`` does not model (#1392). Gated
                upstream by
                :data:`~creek.ingest.base.PASSTHROUGH_FRONTMATTER_KEYS`;
                ``None`` (the default) writes model fields only.

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
            return self._write_model(
                stamped, target_dir, body=body, extra_frontmatter=extra_frontmatter
            )
        return self._write_model(
            fragment, target_dir, body=body, extra_frontmatter=extra_frontmatter
        )

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
        return self.vault_path / _FRAGMENTS_RELPART / subfolder

    def update_fragment(
        self,
        fragment: Fragment,
        body: str,
        *,
        reclassify_threshold: float = 0.0,
    ) -> Path | None:
        """Rewrite an existing fragment's body in place, or return ``None``.

        Locates the file already mapped to ``fragment.id`` (via the per-dir
        id index) and rewrites **only its body**, preserving the on-disk
        frontmatter — id, classifications (OPS-001), resonance links, and
        ``source.origin_key``. This is the changed-branch of idempotent
        mutable-source ingest (#673): an edited journal entry updates the same
        fragment instead of minting a new id and orphaning the old one.

        When *reclassify_threshold* is positive (#675) the new body is compared
        to the prior one; a **material** change (``difflib`` ratio below the
        threshold) clears the fragment's ``classification_method`` /
        ``classified_at`` / ``classification_reasoning`` so the next classify
        pass re-does only this fragment (OPS-001, no global ``--force``). A
        trivial edit (ratio at/above the threshold) preserves classifications.
        The default ``0.0`` never flags — every edit preserves.

        A material change also re-derives ``privacy_tier`` (and the
        ``voice_proxy_eligible`` flag computed from it) from the **new** body
        via :func:`_retier_after_rewrite` (#922). Unlike the classification
        keys, the tier cannot simply be cleared and left for the next pass:
        while it is stale it still governs read-side admission, so a fragment
        rewritten into intimate content would stay visible at an OPEN ceiling
        in the meantime. The re-derivation is escalate-only against the tier
        already on disk, so it never lowers an operator's decision.

        Returns ``None`` when no file is mapped to ``fragment.id`` (e.g. the
        file was removed out of band), and also when the mapped file exists
        but declares a *different* id and no live file in the directory
        declares this one (#1083) — a rewrite there would clobber a foreign
        fragment. Either way the caller can fall back to a fresh
        :meth:`write_fragment`.

        Args:
            fragment: The fragment whose id locates the existing file and
                whose platform/slug routes the lookup directory.
            body: The new markdown body to write below the preserved
                frontmatter.
            reclassify_threshold: Body-similarity floor below which the edit is
                material — classifications are cleared for re-classification and
                the privacy tier is re-derived from the new body.

        Returns:
            Path to the rewritten file, or ``None`` when no existing file
            maps to the id — including the case where the mapped file
            exists but declares a different id and re-resolution finds no
            live file declaring this one, and the case where the mapped file's
            frontmatter will not parse at all (#1543).

        Raises:
            OSError: If the located file cannot be read — the vanished or
                half-rewritten file of the verify-then-load race. Kept in the
                ``OSError`` family so the ingest loop's per-unit handler still
                catches it (see :func:`load_post_or_raise`).
            ValueError: If the located file was read but its frontmatter will
                not parse; the message names the path (same function).
        """
        target_dir = self._fragment_target_dir(fragment)
        with self._lock:
            existing = self._find_existing_locked(fragment.id, target_dir)
            if existing is None:
                return None
            post = load_post_or_raise(existing)
            if _is_material_change(post.content, body, reclassify_threshold):
                _clear_classification(post)
                _retier_after_rewrite(post, fragment, body)
            post.content = body
            _atomic_write_text(existing, frontmatter.dumps(post))
            # Record the in-place edit in the provenance log too, so the audit
            # trail captures subsequent rewrites — not just the original write.
            self._log_provenance_locked(fragment.id, fragment.type, existing)
        return existing

    @staticmethod
    def _fragment_search_dirs(fragments_root: Path) -> list[Path]:
        """Return every directory under *fragments_root* a fragment may sit in.

        The union of two sets, and both halves earn their place:

        - **Declared** — every distinct value of :data:`_PLATFORM_SUBFOLDER`,
          joined a segment at a time. This is what makes a *nested* value
          reachable. ``substack -> "Writing/Substack"`` is the only two-level
          entry today, and enumerating immediate children alone probed the
          empty ``Writing`` index and never descended (#1332). Deriving the
          set from the routing map means a future third level is covered the
          day it is added rather than the day someone remembers the scan.
        - **On disk** — the immediate child directories, which is what this
          method used to return outright. Kept, because narrowing to the
          declared set would newly *lose* fragments in any directory the map
          no longer names — a hand-made folder, or one an older release
          routed to. Shipping that inside a fix for "the lookup misses
          things" would be the same defect wearing a tidier map.

        Derived per call rather than memoised at import: the routing map is
        monkeypatched by tests and read at call time by
        :meth:`_fragment_target_dir`, so a frozen copy would answer for a map
        that is no longer in force. The cost is a dozen path joins against an
        ``iterdir`` syscall this method already pays.

        Args:
            fragments_root: The ``01-Fragments`` directory, which must exist.

        Returns:
            Deduplicated, sorted directories. Declared entries that do not
            exist on disk are included and create nothing: the index load
            returns an empty mapping for a non-directory. It does *cache*
            that emptiness against the path, which is new — before #1332
            only directories that already existed were ever visited — and
            within one :class:`VaultWriter` it is harmless, because
            :meth:`_write_model` mutates the same cached dict in place. It
            does widen, by the width of one lookup, the cross-process index
            staleness already declared out of scope for BUG-006 in the
            ``self._lock`` docstring: another process could create and
            populate a declared directory between this process caching it
            empty and a later write in the same run.
        """
        declared = (
            fragments_root.joinpath(*subfolder.split("/"))
            for subfolder in dict.fromkeys(_PLATFORM_SUBFOLDER.values())
        )
        on_disk = (p for p in fragments_root.iterdir() if p.is_dir())
        # Deduplicate on the resolved Path, not the string: ``Writing`` is
        # both a declared value (ESSAY) and an on-disk child, and visiting it
        # twice would let one lookup fire ``_repair_index_locked``'s append
        # twice.
        return sorted({*declared, *on_disk})

    def _find_in_fragments_locked(self, fragment_id: str) -> Path | None:
        """Return the live fragment file for *fragment_id* under 01-Fragments.

        Caller must hold ``self._lock``. Scans each platform subfolder's id
        index so a tomb does not need to know which subfolder a fragment
        landed in — including the nested ones, which is what
        :meth:`_fragment_search_dirs` is for.

        This searches ``01-Fragments`` only. A *borrowed* fragment lives
        under ``11-Other-Authors/<slug>/`` (see :meth:`_fragment_target_dir`)
        and is deliberately out of reach here; that gap is tracked in #1424,
        and since #1332 a caller that cannot find a fragment is told so
        rather than assuming the fragment was dealt with.
        """
        fragments_root = self.vault_path / _FRAGMENTS_RELPART
        if not fragments_root.is_dir():
            return None
        for subdir in self._fragment_search_dirs(fragments_root):
            found = self._find_existing_locked(fragment_id, subdir)
            if found is not None:
                return found
        return None

    def find_fragment(self, fragment_id: str) -> Path | None:
        """Return the live fragment file for *fragment_id*, or ``None``.

        The public read-only view of the per-directory id index that
        :meth:`tomb_fragment` and :meth:`update_fragment` already navigate
        by. Exposed for #1305's un-migrated-vault advisory, which has to
        answer "does this vault still hold the id the old derivation would
        have minted?" without walking and parsing every fragment file.

        Read-only and index-backed, so asking is cheap. Asking also no
        longer *writes*: until #1332 a lookup over a directory with nothing
        to index persisted a zero-byte ``.id-index.jsonl`` into the
        operator's vault, which made this paragraph's promise false in
        exactly the directories the nested-subfolder fix now visits. See
        :meth:`_load_index_locked`.

        Args:
            fragment_id: The id to look for.

        Returns:
            The fragment's path, or ``None`` when ``01-Fragments/`` does not
            hold it (including when that directory does not exist). Scoped
            deliberately: a *borrowed* fragment lives under
            ``11-Other-Authors/<slug>/`` and is never reported here, so
            ``None`` means "not live under ``01-Fragments``", not "absent
            from the vault". That gap is tracked in #1424 — its fix needs a
            search set bounded by something other than the author count,
            which is a different shape from #1332's.
        """
        with self._lock:
            return self._find_in_fragments_locked(fragment_id)

    def find_tombed_fragment(self, fragment_id: str) -> Path | None:
        """Return the *tombed* file for *fragment_id*, or ``None`` (#1332).

        The orphan-directory counterpart to :meth:`find_fragment`, wrapping
        the same lookup :meth:`restore_fragment` already trusts to locate a
        tombstone.

        It exists so a caller can tell a tomb that *failed* apart from one
        that had already happened. :meth:`tomb_fragment` returns ``None`` for
        both, and
        :func:`creek.ingest.pipeline.tomb_missing_units` must not treat them
        alike: the first is a miss that has to be reported and retried, while
        the second is the state a crash between the file move and the ledger
        append leaves behind, and it converges only if it can be recognised.

        Args:
            fragment_id: The id to look for in ``10-Liminal/Orphaned/``.

        Returns:
            The tombstone's path, or ``None`` when no tombed file declares
            this id.
        """
        orphan_dir = self.vault_path.joinpath(*_ORPHANED_RELPARTS)
        with self._lock:
            return self._find_existing_locked(fragment_id, orphan_dir)

    def _relocate_fragment_locked(
        self,
        existing: Path,
        dest_dir: Path,
        post: frontmatter.Post,
        model_id: str,
        model_type: str,
    ) -> Path:
        """Move *existing* into *dest_dir*, never overwriting what is there.

        Caller must hold ``self._lock``. Shared by :meth:`tomb_fragment`
        and :meth:`restore_fragment`, which differ only in how they locate
        *existing* and in the frontmatter marker they stamp or clear; the
        relocation itself — create, unlink, re-index, log — is identical
        and lives here (#1302). Both directions previously composed
        ``<dir> / existing.name`` and wrote it with an unconditional
        ``os.replace``, so a same-named file at the destination was
        silently destroyed. Allocation now goes through
        :meth:`_atomic_create`, the same ``O_CREAT | O_EXCL`` path every
        other create in this module uses.

        The stem handed to :meth:`_atomic_create` is ``existing.stem``,
        never :meth:`_compute_base_name`: a title that drifted since the
        original write would rename the file out from under every
        ``[[wikilink]]`` pointing at it. Suffix stacking (``-1-1``)
        therefore only occurs under a genuine live collision, which is
        exactly when a distinguishing suffix is required; an uncontended
        tomb → restore round trip is name-stable, because the tomb frees
        the origin name before the restore asks for it back.

        Three orderings below are load-bearing:

        - The destination is created **before** the source is unlinked.
          :meth:`_atomic_create` can raise — retry exhaustion, or a short
          write that :func:`creek._fsio.create_exclusive` cleans up — and
          create-first means both escape with the source file fully
          intact. The worst case is a recoverable duplicate, never a
          fragment that exists nowhere. The mirror case — a create that
          succeeded followed by an ``unlink`` that failed — leaks an
          unindexed copy at the destination for the same reason, which
          is the same safe direction and is tracked in #1325.
        - The origin's in-memory entry is dropped **after** the unlink and
          **before** the destination entry is set. ``self._dir_indexes``
          is keyed by :class:`~pathlib.Path`, so were origin and
          destination ever the same directory, popping second would
          delete the mapping just written. The on-disk origin JSONL is
          deliberately left alone: a :meth:`_persist_full_index` rewrite
          from this process's snapshot would destroy entries another
          process appended (the rejection is argued in
          :meth:`_repair_index_locked` and :meth:`_load_index_locked`),
          and the stale line is harmless — a fresh process re-resolves it
          through the #1083 verification path.
        - The destination's in-memory mapping is set **before** the
          on-disk append, preserved from #1120. The relocated file is
          already on disk by then, so a torn append leaves this process
          still resolving the moved file while a fresh process re-derives
          it from the damage rescan in :meth:`_load_index_locked`.
          Reversing this would lose the relocation for the rest of the run.

        Args:
            existing: The live file to move; unlinked once its copy at the
                destination exists.
            dest_dir: Directory to move into, created if absent.
            post: The already-mutated frontmatter document to write.
            model_id: Id to re-point at the relocated file.
            model_type: Type string recorded in the provenance log.

        Returns:
            The path actually created, which carries a ``-N`` counter
            suffix when the stem was already taken in *dest_dir*.

        Raises:
            RuntimeError: If a unique filename cannot be allocated within
                :data:`_MAX_FILENAME_COLLISION_RETRIES` attempts.
            OSError: If the destination cannot be written, or its index
                entry cannot be appended.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._atomic_create(dest_dir, existing.stem, frontmatter.dumps(post))
        origin_dir = existing.parent
        existing.unlink()
        # ``pop``, not ``del``: ``_find_existing_locked`` may already have
        # healed this entry away on its repair path (#1083).
        self._load_index_locked(origin_dir).pop(model_id, None)
        index = self._load_index_locked(dest_dir)
        index[model_id] = dest.name
        self._append_index_entry(dest_dir, model_id, dest.name)
        self._log_provenance_locked(model_id, model_type, dest)
        return dest

    def tomb_fragment(self, fragment_id: str) -> Path | None:
        """Soft-tomb a fragment: move it to ``10-Liminal/Orphaned/`` (#674).

        Relocates the live fragment file for *fragment_id* into the orphaned
        directory and stamps ``lifecycle: orphaned`` + ``orphaned_at`` into its
        frontmatter, so a deleted source unit is reflected without hard-deleting
        the fragment. Returns the tombed path, or ``None`` when no live fragment
        maps to the id (already moved/removed).

        ``10-Liminal/Orphaned/`` is a single flat sink fed by every
        ``01-Fragments/<platform>/`` subfolder, while a filename stem is unique
        only *within* one of those subfolders — so tombstones genuinely
        collide. The move therefore runs through
        :meth:`_relocate_fragment_locked`, which gives the arriving tombstone a
        counter suffix instead of replacing the one already there (#1302). The
        returned path is authoritative: it, and not ``<orphan dir>/<old
        name>``, is what the id index now points at.

        Args:
            fragment_id: Id of the fragment to soft-tomb.

        Returns:
            Path to the tombed file, or ``None`` if no live fragment was
            found. May carry a ``-N`` counter suffix when the name was taken.

        Raises:
            RuntimeError: If a unique filename cannot be allocated in the
                orphan directory within
                :data:`_MAX_FILENAME_COLLISION_RETRIES` attempts.
            OSError: If the located file cannot be read (the verify-then-load
                race), if the tombstone cannot be created, or if its index
                entry cannot be appended.
            ValueError: If the located file was read but its frontmatter will
                not parse; the message names the path (see
                :func:`load_post_or_raise`). A file that cannot be *read* at
                all raises ``OSError`` above instead, so the ingest loop's
                per-unit handler still catches it.

        The ``RuntimeError`` and ``OSError`` propagate from
        :meth:`_relocate_fragment_locked`.
        :func:`creek.ingest.pipeline.tomb_missing_units` catches only
        ``(OSError, KeyError)`` around this call, so the exhaustion
        ``RuntimeError`` aborts the whole ingest run — deliberately loud, and
        out of scope to widen here. Tracked in #1325.
        """
        orphan_dir = self.vault_path.joinpath(*_ORPHANED_RELPARTS)
        with self._lock:
            existing = self._find_in_fragments_locked(fragment_id)
            if existing is None:
                return None
            post = load_post_or_raise(existing)
            post["lifecycle"] = "orphaned"
            post["orphaned_at"] = datetime.now(UTC).isoformat()
            # Create-before-unlink and the #1120 in-memory-before-append
            # ordering both live in the helper; see its docstring.
            return self._relocate_fragment_locked(
                existing,
                orphan_dir,
                post,
                fragment_id,
                "fragment",
            )

    def restore_fragment(self, fragment: Fragment) -> Path | None:
        """Un-tomb a fragment: move it back from ``10-Liminal/Orphaned/`` (#674).

        Relocates the tombed file for ``fragment.id`` back to its routing
        directory and clears the ``lifecycle``/``orphaned_at`` marker, so a
        re-appeared source unit becomes a live fragment again under its
        preserved id. Returns the restored path, or ``None`` when no tombed
        file maps to the id.

        The tomb freed the origin filename, so an uncontended restore lands
        back on the original name. When a *newer* fragment has since claimed
        that stem, :meth:`_relocate_fragment_locked` gives the returning file a
        counter suffix rather than overwriting the newcomer (#1302), and the
        returned path — not ``<target dir>/<tombstone name>`` — is the
        authoritative one the id index points at.

        Args:
            fragment: The fragment whose id locates the tombed file and whose
                platform/slug routes the restore destination.

        Returns:
            Path to the restored file, or ``None`` if no tombed file was
            found. May carry a ``-N`` counter suffix when the name was taken.

        Raises:
            RuntimeError: If a unique filename cannot be allocated in the
                target directory within
                :data:`_MAX_FILENAME_COLLISION_RETRIES` attempts.
            OSError: If the located file cannot be read (the verify-then-load
                race), if the restored file cannot be created, or if its index
                entry cannot be appended.
            ValueError: If the located file was read but its frontmatter will
                not parse; the message names the path (see
                :func:`load_post_or_raise`). A file that cannot be *read* at
                all raises ``OSError`` above instead, so the ingest loop's
                per-unit handler still catches it.

        The ``RuntimeError`` and ``OSError`` propagate from
        :meth:`_relocate_fragment_locked`.
        :func:`creek.ingest.pipeline.restore_tombed` wraps this call in no
        handler at all — and the sibling tomb path catches only ``(OSError,
        KeyError)`` — so the exhaustion ``RuntimeError`` aborts the ingest
        run. That is deliberately loud, and out of scope to widen here.
        Tracked in #1325.
        """
        orphan_dir = self.vault_path.joinpath(*_ORPHANED_RELPARTS)
        target_dir = self._fragment_target_dir(fragment)
        with self._lock:
            existing = self._find_existing_locked(fragment.id, orphan_dir)
            if existing is None:
                return None
            post = load_post_or_raise(existing)
            post.metadata.pop("lifecycle", None)
            post.metadata.pop("orphaned_at", None)
            # Same helper, same ordering guarantees as ``tomb_fragment``
            # (create-before-unlink; #1120 in-memory-before-append).
            return self._relocate_fragment_locked(
                existing,
                target_dir,
                post,
                fragment.id,
                fragment.type,
            )

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
        target_dir = self.vault_path / _THREADS_RELPART / status_folder
        return self._write_model(
            thread,
            target_dir,
            body=_render_thread_body(thread),
            # Alias the bare title so fragments' ``[[<title>]]`` thread links
            # resolve to the date-prefixed page filename in stock Obsidian.
            extra_frontmatter={"aliases": [thread.title]},
        )

    def write_eddy(self, eddy: Eddy) -> Path:
        """Write an Eddy to 03-Eddies/.

        A short summary body describing the eddy's fragment count and
        bridged threads is rendered automatically.

        Args:
            eddy: The Eddy model to write.

        Returns:
            Path to the written (or existing duplicate) markdown file.
        """
        target_dir = self.vault_path / _EDDIES_RELPART
        return self._write_model(
            eddy,
            target_dir,
            body=_render_eddy_body(eddy),
            # Alias the bare title so fragments' ``[[<title>]]`` eddy links
            # resolve to the date-prefixed page filename in stock Obsidian.
            extra_frontmatter={"aliases": [eddy.title]},
        )

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
        target_dir = self.vault_path / _PRAXIS_RELPART / subfolder
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
            _ACTIVE_DECISION_SUBFOLDER
            if str(decision.status) in _ACTIVE_DECISION_STATUSES
            else _ARCHIVED_DECISION_SUBFOLDER
        )
        target_dir = self.vault_path / _DECISIONS_RELPART / subfolder
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
        extra_frontmatter: dict[str, object] | None = None,
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
            extra_frontmatter: Optional non-model keys merged into the YAML
                frontmatter (e.g. ``aliases`` so an Obsidian ``[[Title]]`` link
                resolves to a ``{date}-{title}`` filename). Keys here override
                model-dumped keys of the same name.

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
        # follow-up issue. The id verification and any index repair
        # ``_find_existing_locked`` performs (#1083) run inside this same
        # critical section, so a repaired mapping cannot be raced by a
        # concurrent write between the re-resolution and the file creation.
        with self._lock:
            existing = self._find_existing_locked(model_id, target_dir)
            if existing is not None:
                return existing

            target_dir.mkdir(parents=True, exist_ok=True)
            base_name = self._compute_base_name(model)

            data = model.model_dump(mode="json")
            if extra_frontmatter:
                # Non-model frontmatter (e.g. ``aliases`` so an Obsidian
                # ``[[Title]]`` link resolves to a ``{date}-{title}`` filename).
                data.update(extra_frontmatter)
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
            # In-memory first, on-disk second — deliberate, and preserved
            # by #1120. ``_atomic_create`` above already put the note on
            # disk, so a torn append leaves this process resolving the
            # id correctly and a fresh process recovering it from the
            # damage rescan rather than minting a duplicate.
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

        The index is a claim, not evidence: a stale or poisoned entry
        names a file belonging to a *different* id, and every caller of
        this locator then acts on that foreign file — rewriting it
        (:meth:`update_fragment`), dropping a new fragment as a false
        duplicate (:meth:`_write_model`), or moving and unlinking it
        (:meth:`tomb_fragment` / :meth:`restore_fragment`). So a located
        file is verified against its own frontmatter before it is
        returned, and a mismatch re-resolves through
        :meth:`_repair_index_locked` (#1083).

        Cost and the rejected memo:

        - Exactly one :func:`frontmatter.load` per index **hit**, none
          per miss, so no directory scan enters the steady-state path.
          Measured by call-counting a real unchanged re-ingest: 400
          units produced 400 loads before this verification existed and
          400 after — one per hit, not one per lookup. Each load costs
          ~180 us on a real fragment (1.6 KB, ~20 frontmatter keys),
          which is roughly +8% on the steady-state unchanged re-ingest
          and about +6 s on a full 35 000-fragment sweep. That is the
          price of not move-and-unlinking a stranger's file, and it is
          paid only where the index claims a hit.
        - Parsing *only* the frontmatter block by hand was measured and
          rejected: ``frontmatter.load`` resolves libyaml's C loader,
          while a hand-rolled ``yaml.safe_load`` of the same block runs
          on the pure-Python loader and measured several times slower.
          Reducing the cost further needs a bounded byte-scan for the
          single ``id`` key, which is tracked as a follow-up rather
          than smuggled into a data-loss fix.
        - An mtime/size memo of the verification result was deliberately
          **rejected**: memoising the check reintroduces "trust a cache
          instead of the artifact" one level down — the exact defect
          being closed — and buys nothing on a cold-process sweep, where
          each path is visited exactly once.
        - The repair appends a single later-wins line and repairs only
          the requested id; other stale ids self-heal on first access.
          This is not a full index rebuild.

        Args:
            model_id: The ID to search for.
            target_dir: The directory to search in.

        Returns:
            The path to the existing file — returned only when that
            file's own frontmatter declares *model_id*. ``None`` if the
            id is not indexed, the indexed path no longer exists on
            disk, or the indexed path declares a different id and no
            live file in *target_dir* declares this one.

        Raises:
            ValueError: If a repaired index entry exceeds
                :data:`_PIPE_BUF_BYTES` when encoded — see
                :meth:`_append_index_entry`. A lookup-shaped method can
                therefore raise, because a mismatch persists its repair.
            OSError: If persisting a repaired entry fails to write — same
                origin, same reason it can escape a lookup. Since #1120
                the partial record such a failure leaves behind is
                self-terminating, so a torn repair no longer damages the
                index it was repairing.
        """
        index = self._load_index_locked(target_dir)
        filename = index.get(model_id)
        if filename is None:
            return None
        candidate = target_dir / filename
        if candidate.exists():
            if _file_declares_id(candidate, model_id):
                return candidate
            return self._repair_index_locked(index, model_id, target_dir)
        # Stale entry — drop it from the in-memory cache so the next
        # write reuses the slot. The on-disk JSONL still contains the
        # stale line, but it is harmless: the next write appends a new
        # mapping that overrides it on rebuild, and a future compaction
        # pass (out of scope for this fix) can reclaim the space.
        del index[model_id]
        return None

    def _repair_index_locked(
        self,
        index: dict[str, str],
        model_id: str,
        target_dir: Path,
    ) -> Path | None:
        """Re-resolve *model_id* by scanning *target_dir*, and persist the fix.

        Caller must hold ``self._lock``. Reached only when the indexed
        file exists but declares a different id, so the scan is paid
        once per poisoned entry rather than per lookup.

        The corrected mapping is persisted with
        :meth:`_append_index_entry`, never :meth:`_persist_full_index`:
        a whole-file rewrite from this process's in-memory snapshot
        would destroy every entry another process appended since this
        writer loaded the index. The append is later-wins and keeps the
        file append-only, so a fresh :class:`VaultWriter` resolves the
        id directly without repeating the scan.

        Args:
            index: The in-memory index for *target_dir*, mutated in place.
            model_id: The ID whose entry was found to be mis-mapped.
            target_dir: The directory to re-scan.

        Returns:
            The path of the file that genuinely declares *model_id*, or
            ``None`` when no live file in *target_dir* declares it.

        Raises:
            ValueError: If the repaired entry exceeds
                :data:`_PIPE_BUF_BYTES` when encoded.
            OSError: If the repaired entry cannot be written.

        Both propagate from :meth:`_append_index_entry`. The in-memory
        mapping is corrected *before* the append, so a failure to persist
        leaves this process holding ground truth and the on-disk index
        merely stale — a cost, not a correctness loss. Across processes
        the same holds since #1120: the torn record this path can leave
        is self-delimiting, and the unparseable line it forms is exactly
        the damage signal that makes the next load re-resolve by scan.
        """
        resolved = self._rebuild_index(target_dir).get(model_id)
        if resolved is None:
            del index[model_id]
            return None
        index[model_id] = resolved
        self._append_index_entry(target_dir, model_id, resolved)
        return target_dir / resolved

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
        the result is persisted so subsequent processes skip the scan —
        **unless the rebuild found nothing**, in which case nothing is
        written (#1332). A memo of "no ids here" saves a glob over an
        empty directory and costs a file created in the operator's vault
        by a *lookup*; the sole caller that reached this path with an
        empty directory was the tomb scan, which left a trail of zero-byte
        ``.id-index.jsonl`` files behind every miss. Re-globbing an empty
        directory once per :class:`VaultWriter` is the cheaper side of that
        trade, and it keeps :meth:`find_fragment` genuinely read-only.
        Pre-existing zero-byte index files stay valid: they parse to the
        same empty mapping a rescan produces.

        When the JSONL file is *present but damaged* — an unparseable
        line, or a file that cannot be read at all — the mappings it
        failed to yield are re-derived by scanning the directory (#1120).
        Without that, a mapping lost to a torn append was permanent: a
        rescan only ever ran when the file was missing, so an id absent
        from a present index was never re-resolved and the next write
        minted a second file for it.

        Precedence on the recovery path is **scan wins**: any id a live
        file declares takes the on-disk truth, and the parsed JSONL
        entries only fill gaps the scan cannot see (an id whose file
        was tombed, or whose frontmatter the scanner declines). The
        direction matches :meth:`_repair_index_locked`, which also
        prefers what the directory says over what the index claimed —
        though that method resolves *purely* by scan and drops the id
        when the scan misses, where this one keeps the parsed entry as a
        fallback rather than discarding a mapping it cannot disprove.
        Letting the parsed value win instead would preserve a mapping the
        scan just disproved, and every later lookup would pay
        verification, a second scan and a repair append to arrive at the
        answer this load already had.

        The recovery never calls :meth:`_persist_full_index`: a
        whole-file rewrite from this process's snapshot would destroy
        entries another process appended. The recovered index is cached
        in ``self._dir_indexes``, so the scan is paid at most once per
        directory per :class:`VaultWriter` instance — not once per
        process, since a caller that builds several writers pays it once
        for each.
        """
        cached = self._dir_indexes.get(target_dir)
        if cached is not None:
            return cached
        index_path = target_dir / INDEX_FILENAME
        if index_path.exists():
            load = self._read_index_records(index_path)
            index = load.entries
            if load.damaged:
                index = self._recover_damaged_index(index_path, target_dir, load)
        elif target_dir.is_dir():
            index = self._rebuild_index(target_dir)
            if index:
                self._persist_full_index(target_dir, index)
        else:
            index = {}
        self._dir_indexes[target_dir] = index
        return index

    @staticmethod
    def _recover_damaged_index(
        index_path: Path,
        target_dir: Path,
        load: _IndexLoad,
    ) -> dict[str, str]:
        """Re-derive a damaged index by scanning *target_dir* (#1120).

        Args:
            index_path: The damaged JSONL file, named in the warning.
            target_dir: The directory whose ``.md`` files are the truth.
            load: The partial parse, whose surviving entries fill any
                gap the scan cannot see.

        Returns:
            The merged index, with the disk scan taking precedence.
        """
        logger.warning(
            "index %s has %d unparseable line(s)%s; re-resolving by directory scan",
            index_path,
            load.torn_lines,
            " and could not be read" if load.read_failed else "",
        )
        recovered = VaultWriter._rebuild_index(target_dir)
        for model_id, filename in load.entries.items():
            recovered.setdefault(model_id, filename)
        return recovered

    @staticmethod
    def _load_index_file(index_path: Path) -> dict[str, str]:
        """Parse a JSONL index file into ``{id: filename}``.

        Malformed lines are skipped so a partial write under crash
        cannot poison the rest of the index. Later entries overwrite
        earlier ones — appending an entry for an existing id therefore
        rewrites the mapping in-place.

        Reports only the mappings. Callers that must also know whether
        the file was *intact* — and therefore whether the mappings are
        complete — use :meth:`_read_index_records` instead.
        """
        return VaultWriter._read_index_records(index_path).entries

    @staticmethod
    def _read_index_records(index_path: Path) -> _IndexLoad:
        """Parse a JSONL index file, reporting damage alongside the entries.

        Only two outcomes count as damage, because only they mean a
        mapping that *should* be here is missing: a failure to read or
        decode the file at all, and a ``json.JSONDecodeError`` (a torn
        record, per #1120). A line that parses as valid JSON but is not
        a ``{id: str, filename: str}`` object is *not* damage — it is a
        well-formed line the schema declines, and treating it as damage
        would buy a directory-wide scan for a file that is perfectly
        intact.

        ``UnicodeDecodeError`` joins ``OSError`` in the read guard. Our
        own writers can only emit ASCII (``json.dumps`` escapes
        non-ASCII by default), so a torn append cannot split a
        multi-byte character — but a hand-edited or externally corrupted
        index can still be undecodable, and letting that escape would
        take down every vault write into the directory rather than
        costing one directory scan.

        Args:
            index_path: The ``.id-index.jsonl`` file to parse.

        Returns:
            An :class:`_IndexLoad` holding the parsed mappings, the
            count of unparseable lines, and whether the read failed.
        """
        index: dict[str, str] = {}
        try:
            raw = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _IndexLoad(index, 0, read_failed=True)
        torn_lines = 0
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                # Load-bearing for the #1120 framing: every appended
                # record opens with its own newline, so a healthy file
                # carries a blank line before each one. Dropping this
                # skip would make every index written after #1120 look
                # damaged.
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                torn_lines += 1
                continue
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            filename = entry.get("filename")
            if isinstance(mid, str) and isinstance(filename, str):
                index[mid] = filename
        return _IndexLoad(index, torn_lines, read_failed=False)

    @staticmethod
    def _rebuild_index(target_dir: Path) -> dict[str, str]:
        """Scan *target_dir* for ``.md`` files and rebuild the ID index."""
        index: dict[str, str] = {}
        if not target_dir.is_dir():
            return index
        for md_file in target_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(md_file))
            except FRONTMATTER_LOAD_ERRORS:
                # A non-fragment or unparseable sibling (e.g. a corrupt
                # ``_author.md`` manifest) must not crash the index rebuild —
                # it simply has no fragment id to index (#470). The tuple
                # includes ``TypeError`` because one hand-edited note with a
                # bare-date frontmatter key used to take down ``find_fragment``
                # / ``write_fragment`` / ``tomb_fragment`` for the whole
                # directory (#1475).
                continue
            mid = post.get("id")
            if isinstance(mid, str):
                index[mid] = md_file.name
        return index

    @staticmethod
    def _persist_full_index(target_dir: Path, index: dict[str, str]) -> None:
        """Atomically write a fresh JSONL index file from *index*.

        Used only on first-time index construction (scanning a vault
        that predates the index file), and only when that scan found at
        least one id — see :meth:`_load_index_locked` for why an empty
        rebuild must not be memoised (#1332). Steady-state updates use
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

        The line is drained through :func:`creek._fsio.write_all`, so an
        ordinary short ``write(2)`` no longer leaves half a JSON line at
        the tail (#987).

        Each record is framed as ``\\n{json}\\n`` — a newline on *both*
        sides, so it is self-delimiting at its start as well as its end
        (#1120). ``O_APPEND`` writes land at EOF with no separator the
        filesystem supplies, so a record that ends but does not begin
        with a newline is silently concatenated onto whatever remnant
        precedes it; the merged line is not valid JSON and takes the
        innocent record down with the torn one. The leading newline
        terminates any remnant, so a tear costs at most its own entry.
        Both newlines are kept deliberately: leading-only is a byte
        cheaper, but a mixed-version straddle — old code appending
        ``{b}\\n`` straight after a new-code ``\\n{a}`` — would merge the
        two and reintroduce the defect on live vault data.

        The framing needs no migration. :meth:`_read_index_records`
        already skips blank lines, so pre-#1120 files (and the
        :meth:`_persist_full_index` output, which renames atomically and
        can never tear) keep parsing byte-for-byte unchanged.

        This method does **not** ``fsync``, unlike
        :meth:`creek.audit.AuditLog.append`. The index is a
        reconstructible cache, not a ledger: losing an unsynced tail to
        a power cut costs a directory scan, not data. That is defensible
        only because :meth:`_load_index_locked` now supplies the paired
        recovery — a damaged or short index re-derives its mappings by
        scanning the directory instead of reporting them as absent.

        ``ftruncate``-back remains the rejected alternative: under
        concurrent ``O_APPEND`` writers the write offset is chosen
        atomically inside the syscall, so we never reliably learn our own
        start offset, and another appender may already have landed a
        complete line past ours that the truncation would destroy.

        Args:
            target_dir: Directory holding the index file.
            model_id: ID of the model being recorded.
            filename: Name of the markdown file that holds it.

        Raises:
            ValueError: If the encoded line exceeds :data:`_PIPE_BUF_BYTES`,
                which would void the ``O_APPEND`` atomicity guarantee.
                The check counts the two framing bytes, so the ceiling is
                honoured for what actually reaches the descriptor.
            OSError: If the line cannot be written in full. The partial
                record left at the tail is self-terminating, so it costs
                only itself; the id it named is re-resolved by scan on
                the next load (#1120).
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        index_path = target_dir / INDEX_FILENAME
        encoded = (
            "\n"
            + json.dumps({"id": model_id, "filename": filename}, sort_keys=True)
            + "\n"
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
            write_all(fd, encoded)
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

        The create-and-drain step is :func:`creek._fsio.create_exclusive`,
        which writes the whole body even when ``write(2)`` goes short
        and unlinks the file if it cannot (#987).

        Args:
            target_dir: Directory the file will live in.
            base_name: Filename stem (without ``.md``).
            content: Full file contents to write.

        Returns:
            The path of the created file.

        Raises:
            RuntimeError: If a unique filename cannot be obtained within
                :data:`_MAX_FILENAME_COLLISION_RETRIES` attempts.
            OSError: If the file was created but its contents could not
                be written in full. The partial file is unlinked, so a
                truncated note is never promoted to the real path.
        """
        encoded = content.encode("utf-8")
        for counter in range(_MAX_FILENAME_COLLISION_RETRIES):
            suffix = "" if counter == 0 else f"-{counter}"
            candidate = target_dir / f"{base_name}{suffix}.md"
            try:
                create_exclusive(candidate, encoded)
            except FileExistsError:
                continue
            return candidate
        msg = (
            f"Could not allocate a unique filename for '{base_name}.md' in "
            f"{target_dir} after {_MAX_FILENAME_COLLISION_RETRIES} attempts"
        )
        raise RuntimeError(msg)

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
