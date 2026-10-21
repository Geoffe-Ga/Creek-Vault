"""``BenchReport.quality_score`` is filled, and stays shareable evidence.

The quality score rides in the benchmark report, so it is closed by
construction like everything else there: enums, numbers and a semver pattern.
The prompts live in the eval's own artifact, which is why the content-free
invariant on :class:`~creek_mcp.bench.report.BenchReport` does not have to be
weakened to satisfy #1875.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.judge import HermeticJudge
from creek_mcp.bench.eval.rubric import Dimension, QualityScore
from creek_mcp.bench.eval.runner import EvalPlan, run_eval
from creek_mcp.bench.metadata import capture_host
from creek_mcp.bench.report import build_report, load_report, write_report
from tests.bench.conftest import run_metadata
from tests.bench.eval.conftest import in_voice_candidate

if TYPE_CHECKING:
    from pathlib import Path


def _quality(tmp_path: Path) -> QualityScore:
    """Run a hermetic eval and return its quality score."""
    plan = EvalPlan(
        candidates=(in_voice_candidate(),),
        judge=HermeticJudge(),
        seed=5,
        voice_evidence_present=True,
    )
    return run_eval(plan, tmp_path / "corpus").quality


def test_build_report_defaults_quality_to_none(tmp_path: Path) -> None:
    """A capacity-only run still reports no quality score."""
    report = build_report(run_metadata(), capture_host(tmp_path), [])
    assert report.quality_score is None


def test_build_report_carries_the_quality_score(tmp_path: Path) -> None:
    """The reserved hook is now filled by the eval (AC7)."""
    quality = _quality(tmp_path)
    report = build_report(run_metadata(), capture_host(tmp_path), [], quality=quality)
    assert report.quality_score == quality
    assert report.quality_score is not None
    assert set(report.quality_score.per_dimension) == set(Dimension)
    assert report.quality_score.rubric_version == RUBRIC_VERSION


def test_a_report_with_a_quality_score_round_trips(tmp_path: Path) -> None:
    """Serialising and reloading preserves every per-dimension score."""
    report = build_report(
        run_metadata(), capture_host(tmp_path), [], quality=_quality(tmp_path)
    )
    out = tmp_path / "report.json"
    write_report(report, out)
    assert load_report(out) == report


def test_the_quality_score_carries_no_prompt_or_candidate_text(
    tmp_path: Path,
) -> None:
    """The report stays content-free: prompts live in the eval artifact only."""
    quality = _quality(tmp_path)
    report = build_report(run_metadata(), capture_host(tmp_path), [], quality=quality)
    written = report.model_dump_json()
    candidate = in_voice_candidate()
    assert candidate.text not in written
    assert candidate.model_tag not in written
    words = candidate.text.split()
    for index in range(len(words) - 7):
        assert " ".join(words[index : index + 8]) not in written
