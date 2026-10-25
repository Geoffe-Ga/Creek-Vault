"""The ``/v1`` durable job store, reused for MCP compositions (#1966).

``creek.draft`` and ``creek.author`` hand off past the server deadline into the
same :class:`~creek_mcp.httpapi.jobs.StoredJob` records ``/v1`` already keeps
(#1605), discriminated by ``kind``. Two surfaces now read one directory, so the
store must keep each surface to its own kinds: ``GET /v1/jobs/{id}`` publishes
counts-only pipeline models and has no shape for a draft, and
``creek.jobs.read`` has none for a classification. Each must refuse the other's
record *without* touching it — in particular without running the stale-worker
recovery, which would otherwise mark a live job of the other surface failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest

from creek_mcp.api.models import JobState
from creek_mcp.httpapi.jobs import (
    claim_job,
    composition_job_for,
    create_job,
    finish_job,
    status_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.httpapi.jobs import StoredJob

_WORKER = "worker-a"
_CONSUMER = "agent-one"


def _running(
    vault: Path,
    kind: Literal["classification", "link", "draft", "author", "revoice"],
    *,
    consumer: str = _CONSUMER,
) -> StoredJob:
    """Create and claim one job of *kind*, returning the running record."""
    record = create_job(
        vault,
        consumer=consumer,
        kind=kind,
        method=kind,
        retier=False,
        ceiling="open",
        worker_id=_WORKER,
    )
    claimed = claim_job(vault, record.job_id, _WORKER)
    assert claimed is not None
    return claimed


@pytest.mark.parametrize("kind", ["draft", "author", "revoice"])
def test_a_composition_job_round_trips_its_content_free_result(
    tmp_path: Path, kind: Literal["draft", "author", "revoice"]
) -> None:
    """A finished composition job reads back with the dict it was given."""
    record = _running(tmp_path, kind)
    result = {"status": "ok", "draft_path": "07-Voice/Drafts/x.md"}
    finish_job(tmp_path, record.job_id, _WORKER, result)

    read = composition_job_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id=_WORKER,
        active_job_ids=frozenset(),
    )
    assert read is not None
    assert read.kind == kind
    assert read.state is JobState.SUCCEEDED
    assert read.result == result


def test_a_composition_job_without_a_result_finishes_failed(tmp_path: Path) -> None:
    """``None`` is the failed terminal state, exactly as for pipeline jobs."""
    record = _running(tmp_path, "draft")
    finish_job(tmp_path, record.job_id, _WORKER, None)
    read = composition_job_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id=_WORKER,
        active_job_ids=frozenset(),
    )
    assert read is not None
    assert read.state is JobState.FAILED
    assert read.result is None


def test_an_in_flight_composition_job_reads_running(tmp_path: Path) -> None:
    """A job this worker still runs is reported as running, untouched."""
    record = _running(tmp_path, "draft")
    read = composition_job_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id=_WORKER,
        active_job_ids=frozenset({record.job_id}),
    )
    assert read is not None
    assert read.state is JobState.RUNNING


def test_a_restarted_worker_marks_an_in_flight_composition_job_failed(
    tmp_path: Path,
) -> None:
    """The ``/v1`` recovery rule applies: a dead worker's job is failed."""
    record = _running(tmp_path, "author")
    read = composition_job_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id="worker-after-restart",
        active_job_ids=frozenset(),
    )
    assert read is not None
    assert read.state is JobState.FAILED


@pytest.mark.parametrize(
    "job_id",
    ["not-a-uuid", str(uuid4()), str(uuid4()).upper()],
    ids=["malformed", "unknown", "non-canonical"],
)
def test_an_unresolvable_composition_id_reads_none(tmp_path: Path, job_id: str) -> None:
    """Malformed, unknown and non-canonical ids are all the same ``None``."""
    assert (
        composition_job_for(
            tmp_path,
            job_id,
            consumer=_CONSUMER,
            worker_id=_WORKER,
            active_job_ids=frozenset(),
        )
        is None
    )


def test_another_consumers_composition_job_reads_none(tmp_path: Path) -> None:
    """A job is bound to the consumer that started it."""
    record = _running(tmp_path, "draft")
    assert (
        composition_job_for(
            tmp_path,
            record.job_id,
            consumer="agent-two",
            worker_id=_WORKER,
            active_job_ids=frozenset({record.job_id}),
        )
        is None
    )


@pytest.mark.parametrize("kind", ["draft", "author", "revoice"])
def test_the_v1_projection_refuses_a_composition_job_without_failing_it(
    tmp_path: Path,
    kind: Literal["draft", "author", "revoice"],
) -> None:
    """``GET /v1/jobs`` has no draft shape, and must not recover the record.

    It is asked under a *different* worker id with an empty active set — the
    exact conditions under which recovery would rewrite the record as failed.
    """
    record = _running(tmp_path, kind)
    assert (
        status_for(
            tmp_path,
            record.job_id,
            consumer=_CONSUMER,
            worker_id="another-process",
            active_job_ids=frozenset(),
        )
        is None
    )
    still = composition_job_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id=_WORKER,
        active_job_ids=frozenset({record.job_id}),
    )
    assert still is not None
    assert still.state is JobState.RUNNING


def test_the_composition_reader_refuses_a_pipeline_job_without_failing_it(
    tmp_path: Path,
) -> None:
    """``creek.jobs.read`` has no classification shape, and recovers nothing."""
    record = _running(tmp_path, "classification")
    assert (
        composition_job_for(
            tmp_path,
            record.job_id,
            consumer=_CONSUMER,
            worker_id="another-process",
            active_job_ids=frozenset(),
        )
        is None
    )
    status = status_for(
        tmp_path,
        record.job_id,
        consumer=_CONSUMER,
        worker_id=_WORKER,
        active_job_ids=frozenset({record.job_id}),
    )
    assert status is not None
    assert status.state is JobState.RUNNING
