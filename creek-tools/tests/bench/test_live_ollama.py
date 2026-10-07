"""Live capacity smoke against a real, operator-supplied Ollama (AC15).

Opt-in operator evidence, never run in CI: the ``live`` marker keeps it out of
the default lane, and it skips itself at runtime unless all three of
``CREEK_BENCH_OLLAMA_URL``, ``CREEK_BENCH_MODEL`` and
``CREEK_BENCH_MODEL_DIGEST`` are set *and* the endpoint answers. Run it with
``./scripts/test.sh --live -k bench``.

It keeps the workload tiny (three entries, concurrency 1 and 2, one 1-second
idle cycle): it proves the harness drives a real runtime end to end and that
the report it produces is pinned and content-free. Real capacity numbers come
from ``scripts/bench.sh`` runs on the target hardware, not from this test.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest

from creek_mcp.bench import cli
from creek_mcp.bench.corpus import entry_text
from creek_mcp.bench.report import load_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.live, pytest.mark.slow]

_PROBE_TIMEOUT_S = 2.0
_ENTRIES = 3
_WORDS = 32
_SEED = 13
_HARNESS_SHA = "0" * 40
"""A placeholder sha: this smoke proves wiring, not a reproducible run."""


@pytest.fixture
def live_ollama() -> tuple[str, str, str]:
    """Return ``(url, model, digest)`` or skip when no runtime is configured."""
    url = os.environ.get("CREEK_BENCH_OLLAMA_URL", "")
    model = os.environ.get("CREEK_BENCH_MODEL", "")
    digest = os.environ.get("CREEK_BENCH_MODEL_DIGEST", "")
    if not (url and model and digest):
        pytest.skip("CREEK_BENCH_OLLAMA_URL/MODEL/MODEL_DIGEST not all set")
    try:
        httpx.get(f"{url}/api/tags", timeout=_PROBE_TIMEOUT_S).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("the configured Ollama endpoint is not reachable")
    return url, model, digest


def test_live_bench_produces_a_pinned_content_free_report(
    tmp_path: Path, live_ollama: tuple[str, str, str]
) -> None:
    """A real run validates, echoes the pinned digest, and leaks no corpus."""
    url, model, digest = live_ollama
    out = tmp_path / "live-report.json"
    argv = [
        "reflect",
        "--mode",
        "live",
        "--ollama-url",
        url,
        "--model",
        model,
        "--digest",
        digest,
        "--git-sha",
        _HARNESS_SHA,
        "--seed",
        str(_SEED),
        "--entries",
        str(_ENTRIES),
        "--words-per-entry",
        str(_WORDS),
        "--cold-trials",
        "1",
        "--warm-trials",
        "1",
        "--concurrency",
        "1,2",
        "--idle-cycles",
        "1",
        "--idle-seconds",
        "1",
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 0
    report = load_report(out)
    assert report.run.digest == digest
    written = out.read_text(encoding="utf-8")
    for index in range(_ENTRIES):
        assert entry_text(seed=_SEED, index=index, words=_WORDS) not in written
