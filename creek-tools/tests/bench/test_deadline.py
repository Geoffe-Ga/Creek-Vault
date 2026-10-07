"""The deadline-fit verdict and the budget it is judged against (#1850).

The budget is *derived* from the two code constants that actually shed a slow
reflection — the HTTP server's request deadline and the Ollama provider's
request timeout — never restated. The verdict is the evidence D04 consumes, so
it must never call a failing envelope "fits": one fast out-of-memory trial
among quick successes is still a failure to serve.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import TYPE_CHECKING

from creek.classify.llm.providers import OllamaProvider
from creek_mcp.bench.deadline import (
    Verdict,
    deadline_budget_seconds,
    deadline_verdict,
)
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.stats import LatencySummary
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.httpapi.middleware import limits

if TYPE_CHECKING:
    import pytest

_BENCH_PACKAGE = Path(__file__).resolve().parents[2] / "creek_mcp" / "bench"


def _summary(latencies: list[float], failures: int = 0) -> LatencySummary:
    """Summarise ok trials at *latencies* plus *failures* fast OOMs."""
    trials = [
        Trial(
            sweep=Sweep.COLD_WARM,
            phase=Phase.WARM,
            concurrency=1,
            input_words=8,
            latency_s=latency,
            generation_s=None,
            outcome=Outcome.OK,
            model_resident_bytes=None,
        )
        for latency in latencies
    ]
    trials += [
        trials[0].model_copy(update={"latency_s": 0.001, "outcome": Outcome.OOM})
    ] * failures
    return LatencySummary.from_trials(trials)


def test_budget_is_the_tighter_of_the_two_shipped_constants() -> None:
    """With nothing patched, the budget is the smaller real constant."""
    assert deadline_budget_seconds() == min(
        limits.DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT
    )


def test_budget_tracks_server_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowering the server deadline lowers the budget with it."""
    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", 12.0)
    assert deadline_budget_seconds() == 12.0


def test_budget_tracks_ollama_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowering the Ollama request timeout lowers the budget with it."""
    monkeypatch.setattr(OllamaProvider, "REQUEST_TIMEOUT", 7.0)
    assert deadline_budget_seconds() == 7.0


def test_p95_equal_budget_fits_and_nextafter_exceeds() -> None:
    """The budget is inclusive: exactly at it fits, one ulp over exceeds."""
    budget = deadline_budget_seconds()
    assert deadline_verdict(_summary([budget]), budget) is Verdict.FITS
    over = math.nextafter(budget, math.inf)
    assert deadline_verdict(_summary([over]), budget) is Verdict.EXCEEDS


def test_verdict_defaults_to_the_derived_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit budget the verdict reads the live constants."""
    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", 2.0)
    assert deadline_verdict(_summary([2.5])) is Verdict.EXCEEDS
    assert deadline_verdict(_summary([1.5])) is Verdict.FITS


def test_any_non_ok_exceeds_even_when_p95_fast() -> None:
    """One fast OOM among quick successes is a failing envelope."""
    summary = _summary([0.1, 0.2, 0.3], failures=1)
    assert summary.p95_s is not None
    assert summary.p95_s < deadline_budget_seconds()
    assert deadline_verdict(summary) is Verdict.EXCEEDS


def test_no_ok_trials_insufficient() -> None:
    """With no successful trial there is nothing to judge a fit on."""
    all_failed = _summary([1.0], failures=1).model_copy(
        update={"ok_count": 0, "p50_s": None, "p95_s": None}
    )
    assert deadline_verdict(all_failed) is Verdict.INSUFFICIENT_DATA


def _numeric_constants(path: Path) -> list[float]:
    """Return every int/float literal in *path*'s source."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ]


def test_no_duplicated_budget_literal() -> None:
    """No harness module restates either deadline constant as a literal."""
    forbidden = {limits.DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT}
    sources = sorted(_BENCH_PACKAGE.glob("*.py"))
    assert sources, "the bench package has no sources to check"
    offenders = [
        (path.name, value)
        for path in sources
        for value in _numeric_constants(path)
        if value in forbidden
    ]
    assert offenders == []
