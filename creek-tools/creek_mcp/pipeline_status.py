"""Build the ``GET /v1/pipeline/status`` body from records that already exist (#2124).

Three questions, three sources, nothing inferred:

* **Which folders hold something?** A walk of every scaffolded ontology folder
  (:data:`creek.vault.folder_writers.ONTOLOGY_FOLDERS`), with the writer or the
  reason it has none. Each note counts toward the deepest scaffolded folder
  holding it. Hidden (``.``) and scaffold (``_``) entries are not notes, and
  symlinks are never followed.
* **What happened to my uploads?** The upload receipts
  (:mod:`creek.ingest.upload_receipts`) say what landed and when; the stage log
  (:mod:`creek.audit.stage_log`) says which whole-vault passes ran after that.
* **Which jobs are running?** The durable job store, read without recovery.

**Cheap enough to poll.** A directory's listing is cached against its own
``st_mtime_ns``: adding, removing or atomically replacing a note (every Creek
writer renames into place) changes that, so an unchanged directory costs one
``stat``. An in-place rewrite that renames nothing can leave a folder's
``last_written_at`` stale until the directory next changes. Fragment tiers are
cached the same way, keyed on the file's own mtime.

**Ceiling and identity.** Counts are every tier: a number names nothing.
Documents are this caller's own uploads only, and one whose fragments include
any above the ceiling is withheld and counted in ``documents_withheld``. A
fragment's tier is its on-disk ``privacy_tier``, failing closed to
``intimate``, and never below the tier the upload declared. The body carries
filenames, the caller's ids and vault paths — never a title or a byte of
content.

**Read-only.** Nothing here opens a file for writing, takes a lock, or repairs
an index.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

from creek.audit.stage_log import PassStage, StageOutcome, read_stage_runs
from creek.ingest.upload_receipts import read_upload_receipts
from creek.models import PrivacyTier
from creek.vault.folder_writers import ONTOLOGY_FOLDERS
from creek.vault.links import read_header_meta
from creek.vault.writer import VaultWriter
from creek_mcp.api.models import (
    PIPELINE_STATUS_MAX_JOBS,
    FolderWriterlessReason,
    PipelineDocumentStatus,
    PipelineFolderStatus,
    PipelineStage,
    PipelineStageOutcome,
    PipelineStageRecord,
    PipelineStatusResponse,
    WireTierCeiling,
)
from creek_mcp.httpapi.jobs import recent_pipeline_jobs
from creek_mcp.tier_ceiling import frontmatter_tier, tier_allowed, tier_sensitivity

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from collections.abc import Set as AbstractSet

    from creek.audit.stage_log import StageRun
    from creek.ingest.upload_receipts import UploadReceipt
    from creek_mcp.tier_ceiling import TierCeiling

_K = TypeVar("_K")
_V = TypeVar("_V")

_NOTE_SUFFIX: Final[str] = ".md"
"""Only markdown files are notes."""

_SKIPPED_PREFIXES: Final[tuple[str, ...]] = (".", "_")
"""Hidden files (indexes, locks) and scaffold docs (``_README.md``) are not notes."""

_FRAGMENTS_ROOT: Final[str] = "01-Fragments"
"""Where fragments live, and therefore where their index files are."""

_NS_PER_SECOND: Final[int] = 1_000_000_000
"""Nanoseconds in a second, for ``st_mtime_ns``."""

_CACHE_MAX_ENTRIES: Final[int] = 8192
"""Bound on each cache; a full cache is cleared rather than grown."""

_REGISTERED: Final[frozenset[str]] = frozenset(f.path for f in ONTOLOGY_FOLDERS)
"""Every scaffolded folder path, for the deepest-folder attribution."""


@dataclass(frozen=True)
class _DirScan:
    """One directory's direct notes and subdirectories, as of one mtime."""

    mtime_ns: int
    count: int
    latest_ns: int | None
    subdirs: tuple[str, ...]


@dataclass
class _Caches:
    """Process-wide caches, shared by request threads under one lock."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    dirs: dict[Path, _DirScan] = field(default_factory=dict)
    tiers: dict[Path, tuple[int, PrivacyTier]] = field(default_factory=dict)


_CACHES: Final[_Caches] = _Caches()


def _remember(table: dict[_K, _V], key: _K, value: _V) -> None:
    """Store *value*, clearing *table* first when it is full."""
    with _CACHES.lock:
        if len(table) >= _CACHE_MAX_ENTRIES:
            table.clear()
        table[key] = value


def _listed(path: Path, mtime_ns: int) -> _DirScan:
    """List one directory's notes and subdirectories, no links followed."""
    count, latest, subdirs = 0, None, []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith(_SKIPPED_PREFIXES):
                continue
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(entry.name)
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(
                _NOTE_SUFFIX
            ):
                count += 1
                modified = entry.stat(follow_symlinks=False).st_mtime_ns
                latest = modified if latest is None else max(latest, modified)
    return _DirScan(mtime_ns, count, latest, tuple(sorted(subdirs)))


def _scan(path: Path) -> _DirScan | None:
    """Return *path*'s listing, from cache when its mtime is unchanged.

    Raises:
        OSError: When an existing directory cannot be listed.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    with _CACHES.lock:
        cached = _CACHES.dirs.get(path)
    if cached is not None and cached.mtime_ns == info.st_mtime_ns:
        return cached
    scanned = _listed(path, info.st_mtime_ns)
    _remember(_CACHES.dirs, path, scanned)
    return scanned


def _attributed(vault: Path, folder: str) -> Iterator[_DirScan]:
    """Yield *folder* and its unregistered descendants' listings."""
    pending = [folder]
    while pending:
        relative = pending.pop()
        scanned = _scan(vault / relative)
        if scanned is None:
            continue
        yield scanned
        children = (f"{relative}/{name}" for name in scanned.subdirs)
        pending.extend(child for child in children if child not in _REGISTERED)


def _instant(ns: int | None) -> datetime | None:
    """Return an mtime as a whole-second UTC instant, or ``None``."""
    if ns is None:
        return None
    return datetime.fromtimestamp(ns // _NS_PER_SECOND, tz=UTC)


def folder_statuses(vault: Path) -> list[PipelineFolderStatus]:
    """Return every scaffolded folder's count, freshness and writer.

    Args:
        vault: Vault root.

    Returns:
        One row per scaffolded folder, in path order.
    """
    rows = []
    for folder in ONTOLOGY_FOLDERS:
        scans = list(_attributed(vault, folder.path))
        stamps = [scan.latest_ns for scan in scans if scan.latest_ns is not None]
        rows.append(
            PipelineFolderStatus(
                path=folder.path,
                count=sum(scan.count for scan in scans),
                last_written_at=_instant(max(stamps, default=None)),
                writer=folder.writer,
                writer_reason=(
                    None
                    if folder.reason is None
                    else FolderWriterlessReason(folder.reason.value)
                ),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@dataclass
class _Document:
    """One upload's latest landing, assembled from its receipts."""

    external_id: str
    filename: str
    at: datetime
    declared: PrivacyTier
    fragment_ids: list[str]


def _declared_tier(raw: str) -> PrivacyTier:
    """Parse a receipt's tier, failing closed to ``intimate``."""
    try:
        return PrivacyTier(raw)
    except ValueError:
        return PrivacyTier.INTIMATE


def _latest_documents(
    receipts: Iterable[UploadReceipt], consumer: str
) -> list[_Document]:
    """Group one consumer's receipts into documents, newest landing first.

    Only the receipts of each document's most recent landing count: an update
    that produced different fragments supersedes the earlier set.
    """
    latest: dict[str, _Document] = {}
    for receipt in receipts:
        if receipt.consumer != consumer:
            continue
        known = latest.get(receipt.external_id)
        if known is None or receipt.at > known.at:
            latest[receipt.external_id] = _Document(
                external_id=receipt.external_id,
                filename=receipt.filename,
                at=receipt.at,
                declared=_declared_tier(receipt.tier),
                fragment_ids=[receipt.fragment_id],
            )
        elif receipt.at == known.at:
            known.fragment_ids.append(receipt.fragment_id)
    return sorted(latest.values(), key=attrgetter("at"), reverse=True)


def _fragment_dirs(vault: Path) -> Iterator[Path]:
    """Yield every directory under ``01-Fragments``, links not followed."""
    for root, dirnames, _files in os.walk(vault / _FRAGMENTS_ROOT):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        yield Path(root)


def _fragment_paths(vault: Path, wanted: AbstractSet[str]) -> dict[str, Path]:
    """Resolve each wanted fragment id to its live file through the indexes."""
    found: dict[str, Path] = {}
    for directory in _fragment_dirs(vault):
        for fragment_id, filename in VaultWriter.read_index(directory).items():
            if fragment_id not in wanted or fragment_id in found:
                continue
            candidate = directory / filename
            if candidate.is_file() and not candidate.is_symlink():
                found[fragment_id] = candidate
        if len(found) == len(wanted):
            break
    return found


def _tier_of(path: Path) -> PrivacyTier:
    """Return a fragment file's tier, cached on its mtime, failing closed."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return PrivacyTier.INTIMATE
    with _CACHES.lock:
        cached = _CACHES.tiers.get(path)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    tier = frontmatter_tier(read_header_meta(path))
    _remember(_CACHES.tiers, path, (mtime_ns, tier))
    return tier


def _admitted(
    document: _Document, paths: dict[str, Path], ceiling: TierCeiling
) -> bool:
    """Return whether every fragment of *document* is within *ceiling*."""
    tiers = [document.declared]
    tiers.extend(_tier_of(paths[f]) for f in document.fragment_ids if f in paths)
    strictest = max(tiers, key=tier_sensitivity)
    return tier_allowed(strictest, ceiling)


def _stage_record(
    stage: PassStage, runs: Iterable[StageRun], since: datetime
) -> PipelineStageRecord | None:
    """Return the row for *stage*, or ``None`` when no covering pass ran.

    A pass covers a document when it started at or after the document landed.
    The first covering pass that did its work is reported; failing that, the
    most recent failed attempt, so a stage that keeps failing is visible.
    """
    covering = [run for run in runs if run.stage is stage and run.started_at >= since]
    worked = (run for run in covering if run.outcome is not StageOutcome.FAILED)
    done = next(worked, None)
    chosen = done if done is not None else (covering[-1] if covering else None)
    if chosen is None:
        return None
    return PipelineStageRecord(
        stage=PipelineStage(chosen.stage.value),
        at=chosen.finished_at,
        outcome=PipelineStageOutcome(chosen.outcome.value),
    )


def _stages(document: _Document, runs: list[StageRun]) -> list[PipelineStageRecord]:
    """Return a document's trail: ``ingested``, then each covering stage."""
    trail = [
        PipelineStageRecord(
            stage=PipelineStage.INGESTED,
            at=document.at,
            outcome=PipelineStageOutcome.OK,
        )
    ]
    for stage in PassStage:
        record = _stage_record(stage, runs, document.at)
        if record is not None:
            trail.append(record)
    return trail


def _document_status(
    vault: Path, document: _Document, paths: dict[str, Path], runs: list[StageRun]
) -> PipelineDocumentStatus:
    """Project one admitted document onto its wire row."""
    first = next((paths[f] for f in document.fragment_ids if f in paths), None)
    return PipelineDocumentStatus(
        external_id=document.external_id,
        filename=document.filename,
        fragment_path=None if first is None else first.relative_to(vault).as_posix(),
        fragment_count=len(document.fragment_ids),
        stages=_stages(document, runs),
    )


def document_statuses(
    vault: Path, *, consumer: str, ceiling: TierCeiling, limit: int
) -> tuple[list[PipelineDocumentStatus], int]:
    """Return this caller's newest documents and how many were withheld.

    Args:
        vault: Vault root.
        consumer: The caller; only its own uploads are listed.
        ceiling: The caller's admitted ceiling.
        limit: The most documents to consider, newest first.

    Returns:
        The admitted rows and the count withheld for the ceiling.
    """
    documents = _latest_documents(read_upload_receipts(vault), consumer)[:limit]
    if not documents:
        return [], 0
    wanted = {f for document in documents for f in document.fragment_ids}
    paths = _fragment_paths(vault, wanted)
    runs = read_stage_runs(vault)
    admitted = [d for d in documents if _admitted(d, paths, ceiling)]
    rows = [_document_status(vault, d, paths, runs) for d in admitted]
    return rows, len(documents) - len(admitted)


def build_status(
    vault: Path,
    *,
    consumer: str,
    ceiling: TierCeiling,
    limit: int,
    worker_id: str,
    active_job_ids: AbstractSet[str],
) -> PipelineStatusResponse:
    """Return the whole status body for one caller.

    Args:
        vault: Vault root, already probed as available.
        consumer: The authenticated caller.
        ceiling: The caller's admitted ceiling.
        limit: The clamped document limit.
        worker_id: This process's job worker id.
        active_job_ids: The job ids this process is executing.

    Returns:
        The response model.

    Raises:
        OSError: When a scaffolded folder exists but cannot be listed.
    """
    documents, withheld = document_statuses(
        vault, consumer=consumer, ceiling=ceiling, limit=limit
    )
    return PipelineStatusResponse(
        status="ok",
        tier_ceiling=WireTierCeiling(ceiling.value),
        folders=folder_statuses(vault),
        documents=documents,
        documents_withheld=withheld,
        jobs=recent_pipeline_jobs(
            vault,
            consumer=consumer,
            worker_id=worker_id,
            active_job_ids=active_job_ids,
            limit=PIPELINE_STATUS_MAX_JOBS,
        ),
    )
