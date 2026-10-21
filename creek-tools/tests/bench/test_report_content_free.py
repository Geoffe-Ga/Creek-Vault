"""The benchmark report and logs never carry corpus text, prompts or output.

The fake model echoes a canary and the whole prompt (which contains the entry
and grounding) back in its response, and raises once with the canary and the
prompt in the exception message. None of it may reach the serialized report,
the log stream at DEBUG, or a written report file — nor may the temp corpus
path.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from creek_mcp.bench.corpus import entry_text
from creek_mcp.bench.fake import FakePool
from creek_mcp.bench.local_only import LocalOnlyFactory
from creek_mcp.bench.pool_client import POOL_GENERATE_PATH, BenchPoolClient
from creek_mcp.bench.report import load_report, write_report
from creek_mcp.bench.runner import Backend, BenchPlan, run_bench
from tests.bench.conftest import (
    BASE_WORKLOAD,
    CANARY,
    FakeModel,
    make_plan,
    run_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_WINDOW = 8
"""Word-window length: any 8 consecutive corpus words is content."""

_POOL_IDENTITY = "tinfoil/gpt-oss-120b@v3"
_POOL_MODEL = "gpt-oss-120b"


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


def test_pool_report_never_contains_the_endpoint_or_the_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pool run leaks neither its endpoint nor what the pool answered.

    The pool's hostname, its path, its answer and the prompt it was sent are
    all absent from the report and from every log record the harness itself
    emits at DEBUG — the fields #1892 adds (``pool_vendor``,
    ``pool_identity``) carry an identity, never a destination (ADR-0016
    Decision 7).

    ``httpx`` logs its own request line, including the endpoint URL, on the
    ``httpx`` logger at INFO. That is the HTTP library talking in the
    operator's process about a URL the operator typed, not the harness
    publishing it: the harness configures no logging, and the report — the
    artefact ADR-0016 Decision 7 attaches as evidence — carries neither the
    URL nor the host. So the haystack here is the report plus the harness's
    own records, and ``docs/bench.md`` says so.
    """
    host = f"{CANARY}.pool.invalid"
    pool = FakePool(identity=_POOL_IDENTITY, model_tag=_POOL_MODEL)
    client = BenchPoolClient(
        f"https://{host}",
        _POOL_MODEL,
        identity=_POOL_IDENTITY,
        num_ctx=4096,
        num_predict=128,
        transport=pool.transport(),
    )
    plan = BenchPlan(
        metadata=run_metadata(
            provider="pool",
            pool_vendor="fake",
            pool_identity=_POOL_IDENTITY,
            model_tag=_POOL_MODEL,
        ),
        workload=replace(BASE_WORKLOAD, seed=13, words_per_entry=24, query_words=24),
        backend=Backend(
            factory=LocalOnlyFactory(client.factory(), provider_name="pool"),
            evict=client.evict,
            resident_bytes=client.resident_bytes,
            pin=client.verify_pinned,
        ),
    )
    caplog.set_level(logging.DEBUG)
    report = run_bench(plan, tmp_path / "corpus")
    out = tmp_path / "pool-report.json"
    write_report(report, out)

    assert pool.generate_calls > 0, "the fake pool was never reached"
    bodies = [entry_text(seed=13, index=index, words=24) for index in range(3)]
    harness_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("creek")
    )
    assert "bench trial" in harness_logs, "the harness logged no trial"
    for name, haystack in {
        "report": out.read_text(encoding="utf-8"),
        "logs": harness_logs,
    }.items():
        assert CANARY not in haystack, name
        assert host not in haystack, name
        assert POOL_GENERATE_PATH not in haystack, name
        for body in bodies:
            for window in _windows(body):
                assert window not in haystack, name
        for sent in pool.requests:
            assert str(sent.get("prompt")) not in haystack, name
