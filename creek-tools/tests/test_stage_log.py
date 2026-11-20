"""Tests for the append-only pipeline stage log (#2124)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from creek.audit.stage_log import (
    PassStage,
    StageOutcome,
    StageRun,
    link_stage,
    read_stage_runs,
    record_stage_run,
    stage_log_path,
)

if TYPE_CHECKING:
    from pathlib import Path

_START = datetime(2026, 11, 20, 17, 0, 0, tzinfo=UTC)
_FINISH = _START + timedelta(seconds=42)


def _vault(tmp_path: Path) -> Path:
    """Return a vault root carrying the ``00-Creek-Meta`` marker directory."""
    (tmp_path / "00-Creek-Meta").mkdir()
    return tmp_path


def test_stage_log_lives_under_state(tmp_path: Path) -> None:
    """The log is one JSONL file under ``00-Creek-Meta/State``."""
    assert stage_log_path(tmp_path) == (
        tmp_path / "00-Creek-Meta" / "State" / "stage-log.jsonl"
    )


def test_a_recorded_run_reads_back_exactly(tmp_path: Path) -> None:
    """A run round-trips through the file with every field intact."""
    vault = _vault(tmp_path)

    written = record_stage_run(
        vault,
        stage=PassStage.CLASSIFIED,
        outcome=StageOutcome.PARTIAL,
        started_at=_START,
        finished_at=_FINISH,
    )

    assert written is True
    assert read_stage_runs(vault) == [
        StageRun(
            stage=PassStage.CLASSIFIED,
            outcome=StageOutcome.PARTIAL,
            started_at=_START,
            finished_at=_FINISH,
        )
    ]


def test_runs_append_in_order(tmp_path: Path) -> None:
    """A second run appends a line; nothing earlier is rewritten."""
    vault = _vault(tmp_path)
    record_stage_run(
        vault,
        stage=PassStage.CLASSIFIED,
        outcome=StageOutcome.OK,
        started_at=_START,
        finished_at=_FINISH,
    )
    record_stage_run(
        vault,
        stage=PassStage.LINKED_EDDIES,
        outcome=StageOutcome.FAILED,
        started_at=_FINISH,
        finished_at=_FINISH,
    )

    stages = [run.stage for run in read_stage_runs(vault)]
    lines = stage_log_path(vault).read_text(encoding="utf-8").splitlines()

    assert stages == [PassStage.CLASSIFIED, PassStage.LINKED_EDDIES]
    assert len(lines) == 2
    assert json.loads(lines[0])["stage"] == "classified"


def test_a_vault_without_its_meta_marker_is_never_scaffolded(tmp_path: Path) -> None:
    """Recording against a non-vault writes nothing and reports so."""
    written = record_stage_run(
        tmp_path,
        stage=PassStage.CLASSIFIED,
        outcome=StageOutcome.OK,
        started_at=_START,
        finished_at=_FINISH,
    )

    assert written is False
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_log_is_reported_not_raised(tmp_path: Path) -> None:
    """A stage pass never fails because its telemetry could not be written."""
    vault = _vault(tmp_path)
    stage_log_path(vault).mkdir(parents=True)

    written = record_stage_run(
        vault,
        stage=PassStage.CLASSIFIED,
        outcome=StageOutcome.OK,
        started_at=_START,
        finished_at=_FINISH,
    )

    assert written is False


def test_an_absent_log_reads_as_no_runs(tmp_path: Path) -> None:
    """A vault that never ran a pass has an empty trail, not an error."""
    assert read_stage_runs(_vault(tmp_path)) == []


def test_an_unreadable_log_reads_as_no_runs(tmp_path: Path) -> None:
    """A log path that is not a readable file is treated as empty."""
    vault = _vault(tmp_path)
    stage_log_path(vault).mkdir(parents=True)

    assert read_stage_runs(vault) == []


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[1, 2]",
        '{"stage": "classified"}',
        '{"stage": "bogus", "outcome": "ok", '
        '"started_at": "2026-11-20T17:00:00+00:00", '
        '"finished_at": "2026-11-20T17:00:00+00:00"}',
        '{"stage": "classified", "outcome": "ok", '
        '"started_at": "yesterday", "finished_at": "2026-11-20T17:00:00+00:00"}',
        '{"stage": "classified", "outcome": "ok", '
        '"started_at": "2026-11-20T17:00:00", '
        '"finished_at": "2026-11-20T17:00:00+00:00"}',
        '{"stage": "classified", "outcome": "ok", '
        '"started_at": 7, "finished_at": "2026-11-20T17:00:00+00:00"}',
    ],
)
def test_a_damaged_line_is_skipped_and_the_rest_survive(
    tmp_path: Path, line: str
) -> None:
    """One torn or hand-edited line costs that line only."""
    vault = _vault(tmp_path)
    record_stage_run(
        vault,
        stage=PassStage.CLASSIFIED,
        outcome=StageOutcome.OK,
        started_at=_START,
        finished_at=_FINISH,
    )
    with stage_log_path(vault).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n\n")

    assert len(read_stage_runs(vault)) == 1


@pytest.mark.parametrize(
    ("method", "stage"),
    [
        ("embeddings", PassStage.LINKED_EMBEDDINGS),
        ("temporal", PassStage.LINKED_TEMPORAL),
        ("eddies", PassStage.LINKED_EDDIES),
        ("threads", PassStage.LINKED_THREADS),
    ],
)
def test_every_link_method_names_its_stage(method: str, stage: PassStage) -> None:
    """Each linker method maps onto exactly one ``linked:<method>`` stage."""
    assert link_stage(method) is stage


def test_an_unknown_link_method_names_no_stage() -> None:
    """A method the linker would refuse is not a stage that ran."""
    assert link_stage("bogus") is None


def test_the_stage_vocabulary_is_pinned() -> None:
    """The closed stage vocabulary is part of the published contract."""
    assert [stage.value for stage in PassStage] == [
        "classified",
        "praxis_scanned",
        "linked:embeddings",
        "linked:temporal",
        "linked:eddies",
        "linked:threads",
    ]
    assert [outcome.value for outcome in StageOutcome] == ["ok", "partial", "failed"]
