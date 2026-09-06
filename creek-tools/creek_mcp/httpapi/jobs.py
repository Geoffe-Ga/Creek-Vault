"""Durable, consumer-bound state for long-running ``/v1`` pipeline jobs.

Job records live inside the vault so an accepted request survives the HTTP
process that accepted it.  The public projection is deliberately smaller than
the record: callers see only an opaque id, a lifecycle state, and the same
counts-only response a synchronous pipeline request returns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from creek._fsio import atomic_replace_path
from creek._fslock import vault_lock
from creek_mcp.api.models import (
    ClassificationResponse,
    JobState,
    JobStatusResponse,
    LinkResponse,
)

if TYPE_CHECKING:
    from pathlib import Path

_JOB_DIRECTORY: Final[tuple[str, ...]] = ("00-Creek-Meta", "State", "jobs")
_JOB_LOCK_NAME: Final[str] = "jobs.lock"


class StoredJob(BaseModel):
    """The private on-disk record used to execute and recover one job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    consumer: str
    kind: Literal["classification", "link"]
    method: str
    retier: bool = False
    ceiling: str
    state: JobState
    worker_id: str
    result: dict[str, Any] | None = None


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
    kind: Literal["classification", "link"],
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
    result: ClassificationResponse | LinkResponse | None,
) -> None:
    """Atomically record a terminal state if this worker still owns the job."""
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
                "result": (
                    result.model_dump(mode="json") if result is not None else None
                ),
            }
        )
        _write(vault, terminal)


def status_for(
    vault: Path,
    job_id: str,
    *,
    consumer: str,
    worker_id: str,
    active_job_ids: set[str],
) -> JobStatusResponse | None:
    """Return one consumer's public job state, recovering stale work as failed."""
    try:
        canonical_id = str(UUID(job_id))
    except ValueError:
        return None
    if canonical_id != job_id:
        return None

    with vault_lock(_lock_path(vault)):
        record = _read(_path(vault, canonical_id))
        if record is None or record.consumer != consumer:
            return None
        if record.state in {JobState.QUEUED, JobState.RUNNING} and (
            record.worker_id != worker_id or record.job_id not in active_job_ids
        ):
            record = record.model_copy(
                update={"state": JobState.FAILED, "result": None}
            )
            _write(vault, record)

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
        status="ok", job_id=UUID(record.job_id), state=record.state, result=result
    )
