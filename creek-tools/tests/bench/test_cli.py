"""The ``python -m creek_mcp.bench`` entry point (AC1, AC13, AC14).

Exit codes are the operator contract: 0 success, 2 refusal (a fixed message
naming the flag or field, never echoing a value), 1 any other failure. Every
refusal must happen before the harness touches the network.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from creek.models import PrivacyTier
from creek_mcp.bench import cli
from creek_mcp.bench.corpus import entry_text
from creek_mcp.bench.deadline import Verdict
from creek_mcp.bench.fake import FakeLLM, fake_factory
from creek_mcp.bench.ollama_client import BenchOllamaClient
from creek_mcp.bench.report import load_report
from creek_mcp.bench.trial import Sweep
from tests.bench.conftest import quote_from

_DIGEST = "sha256:" + "a" * 64
_SHA = "b" * 40
_SMALL = [
    "--entries",
    "2",
    "--words-per-entry",
    "6",
    "--query-words",
    "6",
    "--cold-trials",
    "1",
    "--warm-trials",
    "1",
    "--concurrency",
    "1,2",
    "--context-sizes",
    "16",
]


def _reflect(tmp_path: Path, *extra: str) -> list[str]:
    """Return argv for a small ``reflect`` run writing under *tmp_path*."""
    return ["reflect", "--out", str(tmp_path / "report.json"), *_SMALL, *extra]


def _live(tmp_path: Path, *extra: str) -> list[str]:
    """Return argv for a small live run against an unroutable endpoint."""
    return _reflect(
        tmp_path,
        "--mode",
        "live",
        "--ollama-url",
        "http://127.0.0.1:9",
        "--model",
        "mistral:7b",
        *extra,
    )


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record (and refuse) any request an httpx client tries to send."""
    attempts: list[str] = []

    def _refuse(*args: object, **kwargs: object) -> httpx.Response:
        attempts.append("request")
        raise AssertionError

    monkeypatch.setattr(httpx.Client, "request", _refuse)
    return attempts


def test_fake_llm_quotes_the_entry_and_is_local() -> None:
    """The bench fake answers with one verbatim note and declares itself local."""
    llm = fake_factory(PrivacyTier.OPEN, max_tokens=8)
    assert isinstance(llm, FakeLLM)
    assert llm.is_cloud is False
    note = json.loads(llm("rules\n\nENTRY:\nalpha beta gamma delta"))["notes"][0]
    assert note["quote"] == "alpha beta gamma"


def test_fake_reflect_runs_hermetically_and_writes_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """AC1: the fake mode runs every sweep, writes a valid report, exits 0."""
    assert cli.main(_reflect(tmp_path, "--seed", "5")) == 0
    report = load_report(tmp_path / "report.json")
    assert report.verdict is Verdict.FITS
    assert set(report.per_sweep) == {Sweep.COLD_WARM, Sweep.CONTEXT, Sweep.CONCURRENCY}
    assert report.run.mode == "fake"
    assert report.run.grounding == "none"
    assert no_http == []
    captured = capsys.readouterr()
    assert captured.out.strip() == f"verdict=fits report={tmp_path / 'report.json'}"
    written = (tmp_path / "report.json").read_text(encoding="utf-8")
    for index in range(2, 8):
        quote = quote_from("ENTRY:\n" + entry_text(seed=5, index=index, words=6))
        for haystack in (captured.out, captured.err, written):
            assert quote not in haystack


def test_live_without_digest_exit_2_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A live run without --digest is refused by name, before any request."""
    assert cli.main(_live(tmp_path, "--git-sha", _SHA)) == 2
    assert no_http == []
    assert "--digest" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_live_without_git_sha_exit_2_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A live run without --git-sha is refused by name, before any request."""
    assert cli.main(_live(tmp_path, "--digest", _DIGEST)) == 2
    assert no_http == []
    assert "--git-sha" in capsys.readouterr().err


def test_live_without_model_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A live run must name the model it measures."""
    argv = _reflect(tmp_path, "--mode", "live", "--digest", _DIGEST, "--git-sha", _SHA)
    assert cli.main(argv) == 2
    assert "--model" in capsys.readouterr().err
    assert no_http == []


def test_invalid_metadata_names_field_not_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed identifier is refused by field name; the value is not echoed."""
    argv = _live(tmp_path, "--digest", "sha256:zz", "--git-sha", _SHA)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "digest" in err
    assert "sha256:zz" not in err


def test_workload_refusal_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A context size above --num-ctx is a refusal, not a crash."""
    argv = _reflect(tmp_path, "--num-ctx", "8")
    assert cli.main(argv) == 2
    assert "num_ctx" in capsys.readouterr().err


def test_unreachable_live_endpoint_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pinned live run whose endpoint is down fails (1), naming only a type."""
    argv = _live(tmp_path, "--digest", _DIGEST, "--git-sha", _SHA)
    assert cli.main(argv) == 1
    assert "ProviderUnavailableError" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def _mock_ollama(request: httpx.Request) -> httpx.Response:
    """A hermetic Ollama: one installed model that quotes its entry."""
    routes: dict[str, Any] = {
        "/api/tags": {
            "models": [
                {
                    "name": "mistral:7b",
                    "digest": "a" * 64,
                    "details": {"quantization_level": "Q4_K_M", "parameter_size": "7B"},
                }
            ]
        },
        "/api/show": {"license": "Apache License Version 2.0"},
        "/api/ps": {"models": [{"name": "mistral:7b", "size": 4_000_000_000}]},
    }
    if request.url.path == "/api/generate":
        body = json.loads(request.content)
        if "prompt" not in body:
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, json={"response": FakeLLM()(body["prompt"])})
    return httpx.Response(200, json=routes[request.url.path])


def test_live_mode_end_to_end_on_a_mock_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole live path — pin, evict, generate, residency — on MockTransport."""
    real_init = BenchOllamaClient.__init__

    def _init(self: BenchOllamaClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(_mock_ollama)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BenchOllamaClient, "__init__", _init)
    argv = _live(
        tmp_path, "--digest", _DIGEST, "--git-sha", _SHA, "--cpu-kind", "shared"
    )
    assert cli.main(argv) == 0
    report = load_report(tmp_path / "report.json")
    assert report.run.mode == "live"
    assert report.run.endpoint_scope == "loopback"
    assert report.run.grounding == "default"
    assert report.run.license_id == "Apache-2.0"
    assert report.run.quantization == "Q4_K_M"
    assert report.host.cpu_kind == "shared"
    trials = report.per_sweep[Sweep.COLD_WARM].trials
    assert {t.model_resident_bytes for t in trials} == {4_000_000_000}
    assert report.verdict is Verdict.FITS


def test_corpus_tmpdir_removed_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthetic corpus does not outlive the run."""
    created: list[str] = []
    real = tempfile.TemporaryDirectory

    def _tracking(*args: Any, **kwargs: Any) -> tempfile.TemporaryDirectory[str]:
        directory = real(*args, **kwargs)
        created.append(directory.name)
        return directory

    monkeypatch.setattr(cli.tempfile, "TemporaryDirectory", _tracking)
    assert cli.main(_reflect(tmp_path)) == 0
    assert len(created) == 1
    assert not Path(created[0]).exists()


def _price_file(tmp_path: Path, observed_on: str) -> Path:
    """Write a price sheet observed on *observed_on*."""
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "observed_on": observed_on,
                "currency": "USD",
                "source": "fly-pricing-page",
                "machine_monthly_usd": {"shared-1x-1024mb": "5.70"},
                "volume_gb_month_usd": "0.15",
                "rootfs_gb_month_usd": "0.15",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cost_subcommand_writes_model_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC13: the cost subcommand writes a ``kind=model`` estimate."""
    out = tmp_path / "cost.json"
    argv = [
        "cost",
        "--price-file",
        str(_price_file(tmp_path, "2026-10-01")),
        "--today",
        "2026-10-07",
        "--duty-low",
        "0.05",
        "--duty-high",
        "1",
        "--allowance",
        "20",
        "--seconds-per-reflection",
        "10",
        "--linger-seconds",
        "290",
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 0
    estimate = json.loads(out.read_text(encoding="utf-8"))
    assert estimate["kind"] == "model"
    assert estimate["low_usd"] == "1.18"
    assert estimate["high_usd"] == "6.45"
    assert estimate["allowance_usd"] == "0.91"
    assert capsys.readouterr().out.strip() == (
        f"kind=model low_usd=1.18 high_usd=6.45 report={out}"
    )


def test_cost_allocation_overrides(tmp_path: Path) -> None:
    """Allocation flags select a different priced machine."""
    path = _price_file(tmp_path, "2026-10-01")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["machine_monthly_usd"]["performance-2x-4096mb"] = "50.00"
    path.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "cost.json"
    argv = [
        "cost",
        "--price-file",
        str(path),
        "--today",
        "2026-10-07",
        "--cpu-kind",
        "performance",
        "--cpus",
        "2",
        "--memory-mb",
        "4096",
        "--rootfs-gb",
        "1",
        "--volume-gb",
        "5",
        "--duty-low",
        "0.10",
        "--duty-high",
        "0.50",
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 0
    estimate = json.loads(out.read_text(encoding="utf-8"))
    assert estimate["allocation_key"] == "performance-2x-4096mb"
    assert estimate["low_usd"] == "5.89"
    assert estimate["allowance_usd"] is None


def test_stale_price_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """AC14: a stale sheet is refused with exit 2 and no estimate."""
    out = tmp_path / "cost.json"
    argv = [
        "cost",
        "--price-file",
        str(_price_file(tmp_path, "2026-01-01")),
        "--today",
        "2026-10-07",
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 2
    assert "stale" in capsys.readouterr().err
    assert not out.exists()


def test_partial_allowance_flags_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An allowance needs its per-reflection and linger seconds too."""
    argv = [
        "cost",
        "--price-file",
        str(_price_file(tmp_path, "2026-10-01")),
        "--today",
        "2026-10-07",
        "--allowance",
        "20",
        "--out",
        str(tmp_path / "cost.json"),
    ]
    assert cli.main(argv) == 2
    assert "--seconds-per-reflection" in capsys.readouterr().err


def test_bad_integer_list_is_an_argparse_error(tmp_path: Path) -> None:
    """A malformed --concurrency list is a usage error (exit 2)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["reflect", "--out", str(tmp_path / "r.json"), "--concurrency", "1,x"])
    assert excinfo.value.code == 2


def test_remote_endpoint_refused_by_default_exit_2_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A public --ollama-url is refused before any request without the flag."""
    argv = _reflect(
        tmp_path,
        "--mode",
        "live",
        "--ollama-url",
        "http://8.8.4.4:11434",
        "--model",
        "mistral:7b",
        "--digest",
        _DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 2
    assert "--allow-remote-host" in capsys.readouterr().err
    assert no_http == []


def test_cloud_offload_tag_refused_exit_2_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A ``-cloud`` tag is refused even against a loopback daemon."""
    argv = _reflect(
        tmp_path,
        "--mode",
        "live",
        "--ollama-url",
        "http://127.0.0.1:9",
        "--model",
        "gpt-oss:120b-cloud",
        "--digest",
        _DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 2
    assert "cloud" in capsys.readouterr().err
    assert no_http == []


def test_allowed_remote_endpoint_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --allow-remote-host the run proceeds and says so in its report."""
    real_init = BenchOllamaClient.__init__

    def _init(self: BenchOllamaClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(_mock_ollama)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BenchOllamaClient, "__init__", _init)
    argv = _reflect(
        tmp_path,
        "--mode",
        "live",
        "--ollama-url",
        "http://8.8.4.4:11434",
        "--allow-remote-host",
        "--model",
        "mistral:7b",
        "--digest",
        _DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 0
    assert load_report(tmp_path / "report.json").run.endpoint_scope == "remote"
