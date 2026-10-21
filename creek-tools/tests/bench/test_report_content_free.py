"""The benchmark report and logs never carry corpus text, prompts or output.

The fake model echoes a canary and the whole prompt (which contains the entry
and grounding) back in its response, and raises once with the canary and the
prompt in the exception message. None of it may reach the serialized report,
the log stream at DEBUG, or a written report file — nor may the temp corpus
path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from creek_mcp.bench.corpus import entry_text
from creek_mcp.bench.report import load_report, write_report
from creek_mcp.bench.runner import run_bench
from tests.bench.conftest import CANARY, FakeModel, make_plan

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_WINDOW = 8
"""Word-window length: any 8 consecutive corpus words is content."""


class _CanaryBombError(MemoryError):
    """An exception whose message carries the canary and the prompt."""


def _windows(text: str) -> set[str]:
    """Return every run of :data:`_WINDOW` consecutive words in *text*."""
    words = text.split()
    return {" ".join(words[i : i + _WINDOW]) for i in range(len(words) - _WINDOW + 1)}


def test_report_never_contains_corpus_or_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary, entry bodies, word windows, prompt and path are all absent."""
    model = FakeModel(
        echo_canary=True,
        call_exc=_CanaryBombError(f"{CANARY} leaked"),
        raise_first_only=True,
    )
    root = tmp_path / "corpus"
    plan = make_plan(
        model,
        seed=11,
        entries=3,
        words_per_entry=24,
        query_words=24,
        cold_trials=1,
        warm_trials=2,
        context_sizes=(32,),
        concurrency_levels=(2,),
        idle_cycles=1,
        measure_tokens=True,
    )
    caplog.set_level(logging.DEBUG)
    report = run_bench(plan, root)
    out = tmp_path / "report.json"
    write_report(report, out)
    written = out.read_text(encoding="utf-8")
    assert load_report(out) == report

    bodies = [entry_text(seed=11, index=i, words=24) for i in range(3)]
    haystacks = {"report": written, "logs": caplog.text}
    assert model.prompts, "the fake model was never reached"
    for name, haystack in haystacks.items():
        assert CANARY not in haystack, name
        assert str(root) not in haystack, name
        for body in bodies:
            assert body not in haystack, name
            for window in _windows(body):
                assert window not in haystack, name
        for prompt in model.prompts:
            assert prompt not in haystack, name
            for window in _windows(prompt.rsplit("ENTRY:", 1)[-1]):
                assert window not in haystack, name
    assert "_CanaryBombError" in caplog.text, "a failed trial logs its type name"


def test_measured_tokens_are_counts_and_never_content(tmp_path: Path) -> None:
    """The token block holds integers; the prompts it measured stay behind.

    The fake echoes the whole prompt back, so its measured output overruns the
    budget reflect asked for. That overrun must be *reported* — a number — and
    the text that caused it must not travel with it.
    """
    model = FakeModel(echo_canary=True)
    plan = make_plan(model, seed=3, measure_tokens=True)
    report = run_bench(plan, tmp_path / "corpus")
    block = report.tokens_per_reflection
    rendered = report.model_dump_json()
    assert CANARY not in rendered
    for measured in block.per_surface.values():
        assert isinstance(measured.prompt_tokens, int)
        assert isinstance(measured.output_tokens, int)
        assert measured.output_cap_exceeded
    sweep_tokens = next(iter(report.per_sweep.values())).tokens
    assert sweep_tokens.output_cap_exceeded_count > 0
