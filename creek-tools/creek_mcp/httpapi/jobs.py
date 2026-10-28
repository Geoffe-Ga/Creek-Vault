"""Durable, consumer-bound state for long-running ``/v1`` pipeline jobs.

Job records live inside the vault so an accepted request survives the HTTP
process that accepted it.  The public projection is deliberately smaller than
the record: callers see only an opaque id, a lifecycle state, and the same
counts-only response a synchronous pipeline request returns.

**Two surfaces share this store (#1966).** ``/v1`` writes ``classification``
and ``link`` jobs; the MCP server writes ``draft`` and ``author`` jobs when a
composition outlives its synchronous deadline. One directory, one lock, one
record type and one stale-worker recovery rule — but each reader is confined
to its own kinds (:data:`PIPELINE_KINDS`, :data:`COMPOSITION_KINDS`), and the
kind check runs *before* recovery, so neither surface can mark the other's
live job failed merely by being asked about it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from creek._fsio import atomic_replace_path
from creek._fslock import vault_lock
from creek_mcp.api.models import (
    ClassificationResponse,
    JobFailureCode,
    JobState,
    JobStatusResponse,
    LinkResponse,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet
    from pathlib import Path

_JOB_DIRECTORY: Final[tuple[str, ...]] = ("00-Creek-Meta", "State", "jobs")
_JOB_LOCK_NAME: Final[str] = "jobs.lock"

WORKER_LOST: Final[str] = "worker_lost"
"""The ``failure_reason`` of a job its worker never finished.

Written when a reader finds a queued or running record this process is not
executing — the server restarted, or the machine was autostopped, mid-job.
"""

JobKind: TypeAlias = Literal["classification", "link", "draft", "author", "revoice"]
"""What a job runs: a ``/v1`` pipeline pass or an MCP composition."""

PIPELINE_KINDS: Final[frozenset[str]] = frozenset({"classification", "link"})
"""The kinds ``GET /v1/jobs/{job_id}`` projects through its counts-only models."""

COMPOSITION_KINDS: Final[frozenset[str]] = frozenset({"draft", "author", "revoice"})
"""The kinds ``creek.jobs.read`` resolves (#1966)."""


class StoredJob(BaseModel):
    """The private on-disk record used to execute and recover one job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    consumer: str
    kind: JobKind
    method: str
    retier: bool = False
    ceiling: str
    state: JobState
    worker_id: str
    result: dict[str, Any] | None = None
    failure_reason: str | None = None
    """Why a ``failed`` job failed, for the operator only.

    A tool refusal's fixed text, an exception's *type name* (never its
    message, which can carry fragment text), or :data:`WORKER_LOST`. Never
    projected onto ``/v1/jobs``, whose failed body stays ``result: null``.
    Optional so records written before the field existed still load.
    """
    failure_code: JobFailureCode | None = None
    """The caller-actionable reason a ``failed`` job failed, or ``None`` (#1877).

    Unlike :attr:`failure_reason` this one *is* projected onto ``/v1/jobs``:
    it is a closed code, never text, and names only a failure the caller can
    remedy. Optional so records written before the field existed still load.
    """


def _directory(vault: Path) -> Path:
    """Return the private job directory beneath *vault*."""
    return vault.joinpath(*_JOB_DIRECTORY)


def _lock_path(vault: Path) -> Path:
    """Return the lock serialising every job-record transition."""
    return _directory(vault).parent / _JOB_LOCK_NAME


def _path(vault: Path, job_id: str) -> Path:
    """Return the record path for a previously validated UUID."""
    return _directory(vault) / f"{job_id}.json"


def _read(path: Path) -> StoredJob | None:
    """Read one whole record, failing closed on absence or corruption."""
    try:
        return StoredJob.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def _write(vault: Path, record: StoredJob) -> None:
    """Durably and atomically replace a record while holding the job lock."""
    directory = _directory(vault)
    directory.mkdir(parents=True, exist_ok=True)
    with atomic_replace_path(_path(vault, record.job_id)) as staged:
        staged.write_text(
            json.dumps(record.model_dump(mode="json"), separators=(",", ":")),
            encoding="utf-8",
        )


def create_job(
    vault: Path,
    *,
    consumer: str,
    kind: JobKind,
    method: str,
    retier: bool,
    ceiling: str,
    worker_id: str,
) -> StoredJob:
    """Durably create and return one queued job."""
    record = StoredJob(
        job_id=str(uuid4()),
        consumer=consumer,
        kind=kind,
        method=method,
        retier=retier,
        ceiling=ceiling,
        state=JobState.QUEUED,
        worker_id=worker_id,
    )
    with vault_lock(_lock_path(vault)):
        _write(vault, record)
    return record


def claim_job(vault: Path, job_id: str, worker_id: str) -> StoredJob | None:
    """Atomically move this worker's queued job to ``running``."""
    with vault_lock(_lock_path(vault)):
        record = _read(_path(vault, job_id))
        if (
            record is None
            or record.worker_id != worker_id
            or record.state is not JobState.QUEUED
        ):
            return None
        running = record.model_copy(update={"state": JobState.RUNNING})
        _write(vault, running)
        return running


def finish_job(
    vault: Path,
    job_id: str,
    worker_id: str,
    result: ClassificationResponse | LinkResponse | Mapping[str, Any] | None,
    failure_reason: str | None = None,
    failure_code: JobFailureCode | None = None,
) -> None:
    """Atomically record a terminal state if this worker still owns the job.

    *result* is a pipeline response model, or — for a composition job — the
    already content-free envelope the MCP tool projected; ``None`` records the
    failed state, with *failure_reason* kept on the private record and
    *failure_code* published by :func:`status_for`.
    """
    with vault_lock(_lock_path(vault)):
        record = _read(_path(vault, job_id))
        if (
            record is None
            or record.worker_id != worker_id
            or record.state is not JobState.RUNNING
        ):
            return
        terminal = record.model_copy(
            update={
                "state": JobState.SUCCEEDED if result is not None else JobState.FAILED,
                "result": _dumped(result),
                "failure_reason": None if result is not None else failure_reason,
                "failure_code": None if result is not None else failure_code,
            }
        )
        _write(vault, terminal)


def _dumped(
    result: BaseModel | Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return *result* as the JSON-ready dict a record stores."""
    if result is None:
        return None
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return dict(result)


def _recovered(
    vault: Path,
    job_id: str,
    *,
    consumer: str,
    kinds: AbstractSet[str],
    worker_id: str,
    active_job_ids: AbstractSet[str],
) -> StoredJob | None:
    """Return one consumer's record of an admitted kind, recovering stale work.

    The kind and consumer checks run before the recovery write: a record this
    reader may not see is left exactly as it was found.
    """
    try:
        canonical_id = str(UUID(job_id))
    except ValueError:
        return None
    if canonical_id != job_id:
        return None

    with vault_lock(_lock_path(vault)):
        record = _read(_path(vault, canonical_id))
        if record is None or record.consumer != consumer or record.kind not in kinds:
            return None
        if record.state in {JobState.QUEUED, JobState.RUNNING} and (
            record.worker_id != worker_id or record.job_id not in active_job_ids
        ):
            record = record.model_copy(
                update={
                    "state": JobState.FAILED,
                    "result": None,
                    "failure_reason": WORKER_LOST,
                    "failure_code": None,
                }
            )
            _write(vault, record)
    return record


def composition_job_for(
    vault: Path,
    job_id: str,
    *,
    consumer: str,
    worker_id: str,
    active_job_ids: AbstractSet[str],
) -> StoredJob | None:
    """Return one consumer's ``draft`` / ``author`` record, recovering stale work.

    The MCP twin of :func:`status_for`, under the same recovery rule: a queued
    or running record this worker is not executing is rewritten ``failed``, so
    a server restart never leaves a job running forever. Malformed, unknown,
    another consumer's and another surface's ids are all the same ``None``.

    Args:
        vault: The vault holding the job directory.
        job_id: The caller-supplied id, accepted only in canonical UUID form.
        consumer: The caller's identity; a record is bound to the one that
            started it.
        worker_id: This process's worker id.
        active_job_ids: The ids this process is executing right now.

    Returns:
        The record, or ``None``.
    """
    return _recovered(
        vault,
        job_id,
        consumer=consumer,
        kinds=COMPOSITION_KINDS,
        worker_id=worker_id,
        active_job_ids=active_job_ids,
    )


def status_for(
    vault: Path,
    job_id: str,
    *,
    consumer: str,
    worker_id: str,
    active_job_ids: AbstractSet[str],
) -> JobStatusResponse | None:
    """Return one consumer's public job state, recovering stale work as failed."""
    record = _recovered(
        vault,
        job_id,
        consumer=consumer,
        kinds=PIPELINE_KINDS,
        worker_id=worker_id,
        active_job_ids=active_job_ids,
    )
    if record is None:
        return None

    result: ClassificationResponse | LinkResponse | None = None
    if record.result is not None:
        model = (
            ClassificationResponse if record.kind == "classification" else LinkResponse
        )
        try:
            result = model.model_validate(record.result)
        except ValidationError:
            return None
    return JobStatusResponse(
        status="ok",
        job_id=UUID(record.job_id),
        state=record.state,
        result=result,
        failure_code=record.failure_code if record.state is JobState.FAILED else None,
    )
