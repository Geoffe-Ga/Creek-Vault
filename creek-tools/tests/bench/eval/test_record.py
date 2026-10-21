"""The eval artifact round-trips, and validates what it reads back.

:func:`~creek_mcp.bench.eval.record.write_record` is the eval's only
serializer and :func:`~creek_mcp.bench.eval.record.load_record` its only
reader, mirroring ``write_report`` / ``load_report`` in the capacity harness.
The reader validates through pydantic rather than trusting the file, so a
hand-edited score cannot be read back as evidence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.judge import HermeticJudge, JudgeRequest
from creek_mcp.bench.eval.record import EvalRecord, load_record, write_record
from creek_mcp.bench.eval.rubric import Dimension, JudgeKind
from creek_mcp.bench.eval.runner import EvalPlan, run_eval
from tests.bench.eval.conftest import in_voice_candidate, slop_candidate

if TYPE_CHECKING:
    from pathlib import Path


def _record(tmp_path: Path, *, evidence: bool = True) -> EvalRecord:
    """Run a hermetic eval and return its record."""
    plan = EvalPlan(
        candidates=(in_voice_candidate(), slop_candidate()),
        judge=HermeticJudge(),
        seed=23,
        voice_evidence_present=evidence,
    )
    return run_eval(plan, tmp_path / "corpus").record


def test_a_record_round_trips(tmp_path: Path) -> None:
    """Writing then reading gives back an equal record."""
    record = _record(tmp_path)
    out = tmp_path / "eval.json"
    write_record(record, out)
    assert load_record(out) == record


def test_a_round_tripped_record_keeps_every_judge_payload(tmp_path: Path) -> None:
    """The prompts #1875 asks for survive the file, in call order."""
    record = _record(tmp_path)
    out = tmp_path / "eval.json"
    write_record(record, out)
    loaded = load_record(out)
    assert len(loaded.requests) == len(record.requests) == 2
    assert [request.candidate_id for request in loaded.requests] == [
        request.candidate_id for request in record.requests
    ]
    assert all(isinstance(request, JudgeRequest) for request in loaded.requests)


def test_a_round_tripped_record_keeps_every_dimension(tmp_path: Path) -> None:
    """All four scores come back, with their bases and probe counts."""
    record = _record(tmp_path)
    out = tmp_path / "eval.json"
    write_record(record, out)
    loaded = load_record(out)
    assert set(loaded.quality.per_dimension) == set(Dimension)
    for dimension, score in record.quality.per_dimension.items():
        assert loaded.quality.per_dimension[dimension] == score


def test_an_unsupported_dimension_round_trips(tmp_path: Path) -> None:
    """The #1880 default path carries no judge payloads, and that is readable."""
    record = _record(tmp_path, evidence=False)
    out = tmp_path / "eval.json"
    write_record(record, out)
    loaded = load_record(out)
    assert loaded.requests == ()
    assert loaded == record


def test_the_written_artifact_names_its_seed_and_rubric_version(
    tmp_path: Path,
) -> None:
    """Both are top level: a score is comparable only within one of each."""
    out = tmp_path / "eval.json"
    write_record(_record(tmp_path), out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["seed"] == 23
    assert payload["rubric_version"] == RUBRIC_VERSION
    assert payload["judge"] == JudgeKind.HERMETIC.value
    assert payload["corpus_entries"] == 16


def test_load_refuses_an_out_of_range_score(tmp_path: Path) -> None:
    """A hand-edited artifact must not read back as evidence."""
    out = tmp_path / "eval.json"
    write_record(_record(tmp_path), out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["per_dimension"][Dimension.GROUNDING_ACCURACY.value]["score"] = 4.2
    out.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="less than or equal to 1"):
        load_record(out)


def test_load_refuses_an_unknown_dimension(tmp_path: Path) -> None:
    """A dimension this rubric version does not define is not readable."""
    out = tmp_path / "eval.json"
    write_record(_record(tmp_path), out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["per_dimension"]["vibes"] = payload["per_dimension"].pop(
        Dimension.VOICE_MATCH.value
    )
    out.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="vibes"):
        load_record(out)
