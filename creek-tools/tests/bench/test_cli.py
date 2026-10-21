"""The ``python -m creek_mcp.bench`` entry point (AC1, AC13, AC14).

Exit codes are the operator contract: 0 success, 2 refusal (a fixed message
naming the flag or field, never echoing a value), 1 any other failure. Every
refusal must happen before the harness touches the network.
"""

from __future__ import annotations

import functools
import json
import socket
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from creek.models import PrivacyTier
from creek_mcp.bench import cli
from creek_mcp.bench.corpus import entry_text
from creek_mcp.bench.deadline import Verdict
from creek_mcp.bench.fake import FakeLLM, FakePool, fake_factory
from creek_mcp.bench.ollama_client import BenchOllamaClient
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.pool_client import BenchPoolClient
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


def test_model_store_path_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A model store that is not a directory is refused by flag name."""
    argv = _reflect(tmp_path, "--model-store-path", str(tmp_path / "absent"))
    assert cli.main(argv) == 2
    assert "--model-store-path" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_model_store_path_is_recorded(tmp_path: Path) -> None:
    """With a model store named, the disk figure is labelled as that store."""
    store = tmp_path / "models"
    store.mkdir()
    assert cli.main(_reflect(tmp_path, "--model-store-path", str(store))) == 0
    host = load_report(tmp_path / "report.json").host
    assert host.disk_scope == "model_store"
    assert str(store) not in (tmp_path / "report.json").read_text(encoding="utf-8")


_POOL_IDENTITY = "tinfoil/gpt-oss-120b@v3"
_POOL_MODEL = "gpt-oss-120b"
_POOL_DIGEST = "sha256:" + "d" * 64


def _pool_argv(tmp_path: Path, *extra: str) -> list[str]:
    """Return argv for a small hermetic pool run."""
    return _reflect(
        tmp_path,
        "--provider",
        "pool",
        "--model",
        _POOL_MODEL,
        "--pool-identity",
        _POOL_IDENTITY,
        *extra,
    )


def _knobbed_pool(**overrides: Any) -> Any:
    """Return a ``FakePool`` replacement applying *overrides* to every build."""

    def _build(**fields: Any) -> FakePool:
        return FakePool(**(fields | overrides))

    return _build


@pytest.fixture
def public_pool_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve a pool hostname to a public address without asking a resolver.

    The run is otherwise hermetic, and a test must not depend on what DNS
    says about a reserved ``.example`` name: a resolver that hijacks NXDOMAIN
    to a private address would flip ``endpoint_scope`` from ``remote`` to
    ``private`` and fail the assertion for a reason that is not the code's.

    ``require_local_target``'s ``resolve`` default is bound when the function
    is defined, so patching ``local_only._resolve`` would never reach this
    call; the CLI's own imported symbol is the seam.
    """
    monkeypatch.setattr(
        cli,
        "require_local_target",
        functools.partial(
            cli.require_local_target, resolve=lambda host: ["93.184.216.34"]
        ),
    )


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens a real socket connection.

    The fake pool answers over an in-process ``httpx`` transport, so the run
    is hermetic even though it goes through a client; what must never happen
    is a connection leaving the process.
    """

    def _refuse(self: socket.socket, address: object) -> None:
        msg = f"the run opened a socket to {type(address).__name__}"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", _refuse)


def test_fake_pool_runs_every_sweep_and_reports_provider_pool(
    tmp_path: Path, no_sockets: None
) -> None:
    """AC1: the fake pool runs the sweeps and the report says ``pool``."""
    assert cli.main(_pool_argv(tmp_path, "--idle-cycles", "1")) == 0
    report = load_report(tmp_path / "report.json")
    assert report.run.provider == "pool"
    assert report.run.pool_identity == _POOL_IDENTITY
    assert report.run.pool_vendor == "fake"
    assert report.run.endpoint_scope == "fake"
    assert report.verdict is Verdict.FITS
    assert set(report.per_sweep) == {
        Sweep.COLD_WARM,
        Sweep.CONTEXT,
        Sweep.CONCURRENCY,
        Sweep.IDLE,
    }


def test_pool_advertising_another_identity_exits_2_with_no_generate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a foreign identity is refused with zero generation calls."""
    built: list[FakePool] = []

    def _build(**fields: Any) -> FakePool:
        pool = FakePool(**(fields | {"served_identity": "impostor/model@v1"}))
        built.append(pool)
        return pool

    monkeypatch.setattr(cli, "FakePool", _build)
    assert cli.main(_pool_argv(tmp_path)) == 2
    assert "identity" in capsys.readouterr().err
    assert built and built[-1].generate_calls == 0
    assert not (tmp_path / "report.json").exists()


def test_pool_echoing_another_identity_is_never_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: an answer under another identity is discarded, not counted ok."""
    monkeypatch.setattr(cli, "FakePool", _knobbed_pool(echo_identity="impostor"))
    assert cli.main(_pool_argv(tmp_path)) == 0
    report = load_report(tmp_path / "report.json")
    outcomes = {
        trial.outcome for sweep in report.per_sweep.values() for trial in sweep.trials
    }
    assert Outcome.OK not in outcomes
    assert report.verdict is Verdict.EXCEEDS


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("rate_limited", Outcome.RATE_LIMITED),
        ("insufficient_quota", Outcome.INSUFFICIENT_QUOTA),
    ],
)
def test_pool_admission_refusals_are_reported_as_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: Outcome,
) -> None:
    """AC6: both 429 codes reach the report as their own error outcome."""
    monkeypatch.setattr(cli, "FakePool", _knobbed_pool(refusal_code=code))
    assert cli.main(_pool_argv(tmp_path)) == 0
    report = load_report(tmp_path / "report.json")
    outcomes = {
        trial.outcome for sweep in report.per_sweep.values() for trial in sweep.trials
    }
    assert outcomes == {expected}
    assert report.verdict is Verdict.EXCEEDS


def test_pool_mode_requires_the_pinned_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A pool run with nothing pinned is refused by flag name."""
    argv = _reflect(tmp_path, "--provider", "pool", "--model", _POOL_MODEL)
    assert cli.main(argv) == 2
    assert "--pool-identity" in capsys.readouterr().err
    assert no_http == []


def test_live_pool_requires_the_vendor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """A live pool run must say whose pool produced the measurement."""
    argv = _pool_argv(
        tmp_path,
        "--mode",
        "live",
        "--pool-url",
        "http://127.0.0.1:9",
        "--digest",
        _POOL_DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 2
    assert "--pool-vendor" in capsys.readouterr().err
    assert no_http == []


def test_remote_pool_refused_by_default_exit_2_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """AC4: a public --pool-url needs the operator's explicit opt-in."""
    argv = _pool_argv(
        tmp_path,
        "--mode",
        "live",
        "--pool-url",
        "http://8.8.4.4",
        "--pool-vendor",
        "tinfoil",
        "--digest",
        _POOL_DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 2
    assert "--allow-remote-host" in capsys.readouterr().err
    assert no_http == []


def test_allowed_remote_pool_records_remote_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public_pool_dns: None
) -> None:
    """AC4: with the opt-in, the run proceeds and records ``remote``."""
    pool = FakePool(
        identity=_POOL_IDENTITY,
        model_tag=_POOL_MODEL,
        measurement_digest=_POOL_DIGEST,
    )
    real_init = BenchPoolClient.__init__

    def _init(self: BenchPoolClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = pool.transport()
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BenchPoolClient, "__init__", _init)
    argv = _pool_argv(
        tmp_path,
        "--mode",
        "live",
        "--pool-url",
        "https://pool.vendor.example",
        "--allow-remote-host",
        "--pool-vendor",
        "tinfoil",
        "--digest",
        _POOL_DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 0
    report = load_report(tmp_path / "report.json")
    assert report.run.endpoint_scope == "remote"
    assert report.run.provider == "pool"
    assert report.run.digest == _POOL_DIGEST
    assert report.host.describes_model_host is False
    assert pool.generate_calls > 0


def test_cloud_offload_tag_refused_in_pool_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_http: list[str]
) -> None:
    """AC5: a ``*-cloud`` tag is refused in pool mode too, hermetic or not."""
    argv = _reflect(
        tmp_path,
        "--provider",
        "pool",
        "--model",
        "gpt-oss:120b-cloud",
        "--pool-identity",
        _POOL_IDENTITY,
    )
    assert cli.main(argv) == 2
    assert "cloud" in capsys.readouterr().err
    assert no_http == []


def test_pool_report_holds_no_url_or_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public_pool_dns: None
) -> None:
    """AC7: the pool's URL and hostname appear nowhere in the report."""
    host = "pool-canary-7f3e.vendor.example"
    pool = FakePool(
        identity=_POOL_IDENTITY,
        model_tag=_POOL_MODEL,
        measurement_digest=_POOL_DIGEST,
    )
    real_init = BenchPoolClient.__init__

    def _init(self: BenchPoolClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = pool.transport()
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(BenchPoolClient, "__init__", _init)
    argv = _pool_argv(
        tmp_path,
        "--mode",
        "live",
        "--pool-url",
        f"https://{host}",
        "--allow-remote-host",
        "--pool-vendor",
        "tinfoil",
        "--digest",
        _POOL_DIGEST,
        "--git-sha",
        _SHA,
    )
    assert cli.main(argv) == 0
    written = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert host not in written
    assert "pool-canary" not in written
    assert "vendor.example" not in written


# --------------------------------------------------------------------------
# Metered per-token pricing (#1897).
# --------------------------------------------------------------------------

_METERED_MODEL = "synthetic-fixture-model"
"""Not a vendor id: no ADR-0016 Options-table figure is a current price."""


def _metered_price_file(tmp_path: Path, **overrides: object) -> Path:
    """Write a metered sheet with obviously synthetic round prices."""
    sheet: dict[str, object] = {
        "observed_on": "2026-10-01",
        "currency": "USD",
        "source": "synthetic-fixture",
        "metered_per_million": {
            _METERED_MODEL: {
                "input_per_million": "1.00",
                "output_per_million": "4.00",
            }
        },
    }
    sheet.update(overrides)
    path = tmp_path / "metered.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    return path


def _token_report(tmp_path: Path, *, basis: str) -> Path:
    """Run the hermetic harness, then rewrite only its token basis.

    The fake backend honestly reports ``byte_upper_bound``, so a
    ``provider_reported`` fixture has to be written: the point of the test is
    what a *citable* basis unlocks, and no hermetic run can produce one.
    """
    out = tmp_path / "report.json"
    assert cli.main(_reflect(tmp_path)) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    document["tokens_per_reflection"]["basis"] = basis
    out.write_text(json.dumps(document), encoding="utf-8")
    return out


def _metered_argv(tmp_path: Path, report: Path, out: Path, *extra: str) -> list[str]:
    """Build a metered ``cost`` invocation."""
    return [
        "cost",
        "--price-file",
        str(_metered_price_file(tmp_path)),
        "--today",
        "2026-10-07",
        "--metered-model",
        _METERED_MODEL,
        "--token-report",
        str(report),
        "--out",
        str(out),
        *extra,
    ]


def test_metered_cost_reports_per_1000_and_per_account_month(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1: ``cost`` prices a metered sheet at 100/mo, 10,000/mo and 20/account."""
    report = _token_report(tmp_path, basis="provider_reported")
    out = tmp_path / "metered-cost.json"
    assert cli.main(_metered_argv(tmp_path, report, out)) == 0
    estimate = json.loads(out.read_text(encoding="utf-8"))
    assert estimate["kind"] == "model"
    assert estimate["shape"] == "metered_tokens"
    assert estimate["token_basis"] == "provider_reported"
    volumes = {one["reflections_per_month"]: one for one in estimate["per_volume"]}
    assert set(volumes) == {100, 10_000}
    assert estimate["monthly_reflection_cap"] == 20
    assert Decimal(estimate["cost_per_account_month"]) > 0
    assert estimate["vendor_key_input_token_cap"] > 0
    assert estimate["vendor_key_output_token_cap"] > 0
    stdout = capsys.readouterr().out
    assert "shape=metered_tokens" in stdout
    assert "basis=provider_reported" in stdout
    assert "per_account_month=" in stdout


@pytest.mark.parametrize("basis", ["byte_upper_bound", "unreported", "not_measured"])
def test_metered_cost_refuses_an_uncitable_basis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], basis: str
) -> None:
    """AC3: no price is quoted from a bound or an absence, and nothing is written."""
    report = _token_report(tmp_path, basis=basis)
    out = tmp_path / "metered-cost.json"
    assert cli.main(_metered_argv(tmp_path, report, out)) == 2
    assert "provider-reported" in capsys.readouterr().err
    assert not out.exists()


def test_metered_cost_refuses_a_currency_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: a USD sheet asked for as EUR is refused, never converted."""
    report = _token_report(tmp_path, basis="provider_reported")
    out = tmp_path / "metered-cost.json"
    argv = _metered_argv(tmp_path, report, out, "--metered-currency", "EUR")
    assert cli.main(argv) == 2
    assert "currency" in capsys.readouterr().err
    assert not out.exists()


def test_metered_cost_refuses_a_stale_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC2: the dated-sheet rules bind the metered sheet too, with exit 2."""
    report = _token_report(tmp_path, basis="provider_reported")
    out = tmp_path / "metered-cost.json"
    argv = [
        "cost",
        "--price-file",
        str(_metered_price_file(tmp_path, observed_on="2026-01-01")),
        "--today",
        "2026-10-07",
        "--metered-model",
        _METERED_MODEL,
        "--token-report",
        str(report),
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 2
    assert "stale" in capsys.readouterr().err
    assert not out.exists()


def test_partial_metered_flags_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The metered pair is all or none, like the allowance trio."""
    argv = [
        "cost",
        "--price-file",
        str(_metered_price_file(tmp_path)),
        "--today",
        "2026-10-07",
        "--metered-model",
        _METERED_MODEL,
        "--out",
        str(tmp_path / "metered-cost.json"),
    ]
    assert cli.main(argv) == 2
    assert "--token-report" in capsys.readouterr().err


def test_metered_only_flag_without_the_pair_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A metered-only flag on the Machine-month path is refused, not ignored."""
    argv = [
        "cost",
        "--price-file",
        str(_price_file(tmp_path, "2026-10-01")),
        "--today",
        "2026-10-07",
        "--monthly-cap",
        "5",
        "--out",
        str(tmp_path / "cost.json"),
    ]
    assert cli.main(argv) == 2
    assert "--monthly-cap" in capsys.readouterr().err
