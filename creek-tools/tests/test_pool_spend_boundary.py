"""Pool spend metering at the boundary, the runtimes and ``--check pool`` (#2007).

The meter itself is tested in ``tests/test_pool_spend_meter.py``. This module
proves the seams: every generation and embeddings call through
:mod:`creek.classify.llm.pool_boundary` reaches the meter, whichever vendor
the Machine names; both serving runtimes install the meter over the vault's
own volume; and ``python -m creek_mcp.container_health --check pool`` prints
the month's spend without changing its exit code.

Every dial here reaches a loopback listener this module opened, or nothing.
No vendor is contacted, nothing is billed, and every figure is synthetic.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Final

import pytest

from creek.classify.llm import pool_meter
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.pool_boundary import (
    POOL_URL_ENV,
    POOL_VENDOR_ENV,
    PoolUnreachableError,
    pool_chat_completion,
    pool_embed,
    pool_generate,
    pool_list_models,
)
from creek.classify.llm.pool_meter import (
    CREEK_RUN_POOL_VENDOR,
    LEDGER_RELPATH,
    UNANSWERED_COMPLETION_ESTIMATE,
    PoolSpendMeter,
    PoolUsage,
    SpendState,
    active_pool_meter,
    pool_spend_meter,
)
from creek.classify.llm.pool_prices import PriceTable
from creek.config import CONFIG_PATH_ENV_VAR
from creek_mcp import container_health as health
from creek_mcp.container_health import ProbeResult, ProbeStatus, ProbeTarget
from creek_mcp.container_runtime import PORT_ENV, VAULT_PATH_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

MODEL: Final[str] = "example-chat-1"
EMBED_MODEL: Final[str] = "example-embed-1"
PROMPT_CANARY: Final[str] = "journal-canary-a41f plaintext entry"
CHAT_PATH: Final[str] = "/v1/chat/completions"
GENERATE_PATH: Final[str] = "/v1/generate"
EMBED_PATH: Final[str] = "/v1/embeddings"
MODELS_PATH: Final[str] = "/v1/models"
HANG_UP: Final[int] = -1
"""A route status meaning: read the request, then close without answering."""


class FakePool:
    """A loopback listener answering each path from a route table."""

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        """Serve *routes* on an ephemeral loopback port."""
        self.routes = routes
        pool = self

        class Handler(BaseHTTPRequestHandler):
            """Answer from the route table."""

            def log_message(self, *_args: object) -> None:
                """Stay silent."""

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                status, payload = pool.routes.get(self.path, (404, {}))
                if status == HANG_UP:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # stdlib handler name
                self._answer()

            def do_POST(self) -> None:  # stdlib handler name
                self._answer()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The loopback origin it answers on."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    """A fake pool pinned as the Machine's destination, no vendor named."""
    fake = FakePool({})
    monkeypatch.setenv(POOL_URL_ENV, fake.url)
    monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)
    yield fake
    fake.close()


def _ledger(vault: Path) -> dict[str, object]:
    """Return the decoded ledger."""
    decoded: dict[str, object] = json.loads(
        (vault / LEDGER_RELPATH).read_text(encoding="utf-8")
    )
    return decoded


def _counts(vault: Path, vendor: str, model: str) -> dict[str, int]:
    """Return one model's counts from the ledger."""
    vendors = _ledger(vault)["vendors"]
    assert isinstance(vendors, dict)
    counts: dict[str, int] = vendors[vendor]["models"][model]
    return counts


def _chat_body() -> dict[str, object]:
    """A chat body carrying the canary."""
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT_CANARY}],
        "max_tokens": 64,
    }


def test_an_answered_chat_completion_is_metered_as_reported(
    pool: FakePool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar's ``usage`` block lands on the named vendor's ledger line."""
    monkeypatch.setenv(POOL_VENDOR_ENV, "privatemode")
    pool.routes[CHAT_PATH] = (
        200,
        {"model": MODEL, "usage": {"prompt_tokens": 31, "completion_tokens": 9}},
    )

    with pool_spend_meter(tmp_path) as meter:
        response = pool_chat_completion(_chat_body(), budget=5.0)
        (spend,) = meter.report()

    assert response.status == 200
    counts = _counts(tmp_path, "privatemode", MODEL)
    assert counts["prompt_tokens"] == 31
    assert counts["completion_tokens"] == 9
    assert counts["estimated_calls"] == 0
    assert spend.state is SpendState.PRICE_UNPINNED
    assert PROMPT_CANARY not in json.dumps(_ledger(tmp_path))


def test_the_creek_run_pool_is_metered_under_its_own_id(
    pool: FakePool, tmp_path: Path
) -> None:
    """No vendor named: the O3 pool's tokens are counted, never priced."""
    pool.routes[GENERATE_PATH] = (200, {"usage": {"prompt_tokens": 5}})

    with pool_spend_meter(tmp_path):
        pool_generate({"model": MODEL, "prompt": PROMPT_CANARY}, budget=5.0)

    counts = _counts(tmp_path, CREEK_RUN_POOL_VENDOR, MODEL)
    assert counts["prompt_tokens"] == 5
    assert counts["estimated_calls"] == 1


def test_an_embeddings_call_is_metered_as_embedding_tokens(
    pool: FakePool, tmp_path: Path
) -> None:
    """Embeddings report prompt tokens; they are booked as embedding tokens."""
    pool.routes[EMBED_PATH] = (
        200,
        {"model": EMBED_MODEL, "data": [], "usage": {"prompt_tokens": 17}},
    )

    with pool_spend_meter(tmp_path):
        pool_embed({"model": EMBED_MODEL, "input": [PROMPT_CANARY]}, budget=5.0)

    counts = _counts(tmp_path, CREEK_RUN_POOL_VENDOR, EMBED_MODEL)
    assert counts["embedding_tokens"] == 17
    assert counts["prompt_tokens"] == 0


@pytest.mark.parametrize("status", [401, 429, 503])
def test_a_refused_call_is_not_metered(
    pool: FakePool, tmp_path: Path, status: int
) -> None:
    """A vendor refusal bills nothing."""
    pool.routes[CHAT_PATH] = (status, {"error": {"code": "insufficient_quota"}})

    with pool_spend_meter(tmp_path):
        response = pool_chat_completion(_chat_body(), budget=5.0)

    assert response.status == status
    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_a_dispatched_call_that_got_no_answer_is_counted_conservatively(
    pool: FakePool, tmp_path: Path
) -> None:
    """The vendor may have billed a body it received: counted, flagged."""
    pool.routes[CHAT_PATH] = (HANG_UP, None)

    with pool_spend_meter(tmp_path), pytest.raises(PoolUnreachableError):
        pool_chat_completion(_chat_body(), budget=5.0)

    counts = _counts(tmp_path, CREEK_RUN_POOL_VENDOR, MODEL)
    assert counts["estimated_calls"] == 1
    assert counts["completion_tokens"] == 64
    assert counts["prompt_tokens"] >= len(PROMPT_CANARY)


def test_an_unanswered_call_without_a_ceiling_uses_the_named_estimate(
    pool: FakePool, tmp_path: Path
) -> None:
    """No ``max_tokens`` in the body: the documented planning figure."""
    pool.routes[GENERATE_PATH] = (HANG_UP, None)

    with pool_spend_meter(tmp_path), pytest.raises(PoolUnreachableError):
        pool_generate({"model": MODEL, "prompt": PROMPT_CANARY}, budget=5.0)

    counts = _counts(tmp_path, CREEK_RUN_POOL_VENDOR, MODEL)
    assert counts["completion_tokens"] == UNANSWERED_COMPLETION_ESTIMATE


def test_a_dial_that_never_connected_costs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing was sent, so nothing is counted."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(POOL_URL_ENV, f"http://127.0.0.1:{port}")
    monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)

    with pool_spend_meter(tmp_path), pytest.raises(PoolUnreachableError):
        pool_chat_completion(_chat_body(), budget=1.0)

    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_the_identity_listing_is_not_metered(pool: FakePool, tmp_path: Path) -> None:
    """Only content calls are billable; the listing is metadata."""
    pool.routes[MODELS_PATH] = (200, {"data": [], "usage": {"prompt_tokens": 99}})

    with pool_spend_meter(tmp_path):
        pool_list_models(budget=5.0)

    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_without_an_installed_meter_the_boundary_writes_nothing(
    pool: FakePool, tmp_path: Path
) -> None:
    """A self-hosted caller, or any test, meters nothing by default."""
    pool.routes[CHAT_PATH] = (200, {"usage": {"prompt_tokens": 1}})

    pool_chat_completion(_chat_body(), budget=5.0)

    assert active_pool_meter() is None
    assert not any(tmp_path.iterdir())


def test_a_pinned_price_alerts_through_the_boundary(
    pool: FakePool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end: reported tokens, a pinned table, one alert at EUR 5."""
    monkeypatch.setenv(POOL_VENDOR_ENV, "privatemode")
    prices = PriceTable.model_validate(
        {
            "schema_version": 1,
            "vendors": {
                "privatemode": {
                    "status": "pinned",
                    "currency": "EUR",
                    "source_url": "https://prices.example.test/",
                    "read_on": "2026-10-30",
                    "models": {
                        MODEL: {
                            "prompt_per_million": "1",
                            "completion_per_million": "1",
                        }
                    },
                }
            },
        }
    )
    pool.routes[CHAT_PATH] = (
        200,
        {"usage": {"prompt_tokens": 2_500_000, "completion_tokens": 0}},
    )
    caplog.set_level("INFO", logger="creek.pool")

    with pool_spend_meter(tmp_path, prices=prices) as meter:
        for _ in range(3):
            pool_chat_completion(_chat_body(), budget=5.0)
        (spend,) = meter.report()

    assert spend.state is SpendState.THRESHOLD_REACHED
    assert caplog.text.count('"alert": "monthly_budget_departure"') == 1


# --------------------------------------------------------------------------
# Both serving runtimes install the meter over the vault's own volume.
# --------------------------------------------------------------------------


def test_the_fly_runtime_meters_into_the_vault_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fly vault serves with a meter whose ledger is on its own volume."""
    from creek_mcp import fly_vault_runtime as runtime
    from creek_mcp.fly_vault_runtime import FlyVaultSettings

    vault = tmp_path / "vault"
    vault.mkdir()
    replay = tmp_path / "replay-state"
    replay.write_text("s" * 64, encoding="ascii")
    replay.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            "FLY_APP_NAME": "creek-vault-pilot",
            "FLY_MACHINE_ID": "machine-001",
            "FLY_REGION": "iad",
            "FLY_PRIVATE_IP": "fdaa:0:1::2",
            "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
            "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay),
        }
    )
    observed: list[PoolSpendMeter | None] = []
    monkeypatch.setattr(runtime, "_load_replay_state", lambda _path: "s" * 64)
    monkeypatch.setattr(runtime, "prepare_vault", _prepared(vault))
    monkeypatch.setattr(runtime, "load_consumer_secret", _secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _v: None)
    monkeypatch.setattr(runtime, "announce_ungated_consumers", lambda _v: None)
    monkeypatch.setattr(runtime, "agent_verifier_for", lambda *_a: None)
    monkeypatch.setattr(runtime, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime, "serve", lambda _app, _args: observed.append(active_pool_meter())
    )

    runtime.run(settings)

    (meter,) = observed
    assert meter is not None
    assert meter._ledger_path == vault / LEDGER_RELPATH
    assert active_pool_meter() is None


def test_the_container_runtime_meters_into_the_vault_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plain container serves with the same meter over its mount."""
    from creek_mcp import container_runtime as runtime
    from creek_mcp.container_runtime import ContainerSettings

    vault = tmp_path / "vault"
    vault.mkdir()
    settings = ContainerSettings(vault_path=vault)
    observed: list[PoolSpendMeter | None] = []
    # run() exports these three; setting them first lets monkeypatch restore them.
    monkeypatch.setenv("CREEK_VAULT_PATH", str(vault))
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(vault / "creek.yaml"))
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    monkeypatch.setattr(runtime, "prepare_vault", _prepared(vault))
    monkeypatch.setattr(runtime, "_require_tls_file", lambda _p, _l: None)
    monkeypatch.setattr(runtime, "load_consumer_secret", _secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _v: None)
    monkeypatch.setattr(runtime, "announce_ungated_consumers", lambda _v: None)
    monkeypatch.setattr(runtime, "agent_verifier_for", lambda *_a: None)
    monkeypatch.setattr(runtime, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime, "serve", lambda _app, _args: observed.append(active_pool_meter())
    )

    runtime.run(settings)

    (meter,) = observed
    assert meter is not None
    assert meter._ledger_path == vault / LEDGER_RELPATH
    assert active_pool_meter() is None


class _Prepared:
    """Stand-in for a prepared vault: only its config path is read."""

    def __init__(self, vault: Path) -> None:
        """Point at *vault*'s config."""
        self.config_path = vault / "creek.yaml"


def _prepared(vault: Path) -> object:
    """Return a ``prepare_vault`` replacement for *vault*."""
    return lambda _settings: _Prepared(vault)


class _Secret:
    """Stand-in consumer secret."""

    tokens = ("t" * 32,)

    def reloading_verifier(self, _path: object) -> object:
        """Return an opaque verifier."""
        return object()


def _secret(_path: object) -> _Secret:
    """Return the stand-in secret."""
    return _Secret()


# --------------------------------------------------------------------------
# --check pool prints the month's spend; its exit code is the probe's alone.
# --------------------------------------------------------------------------


def _check_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int | str | None, list[str]]:
    """Run ``--check pool`` over a ready probe and return (code, lines)."""
    monkeypatch.setenv(VAULT_PATH_ENV, str(tmp_path))
    monkeypatch.setenv(PORT_ENV, "8080")
    monkeypatch.setattr(
        health, "probe", lambda _s, _t: ProbeResult(ProbeStatus.POOL_READY)
    )
    with pytest.raises(SystemExit) as caught:
        health.main(["--check", ProbeTarget.POOL.value])
    return caught.value.code, capsys.readouterr().out.splitlines()


def test_check_pool_prints_no_usage_on_a_fresh_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first line is the probe state; the spend line follows it."""
    code, lines = _check_pool(tmp_path, monkeypatch, capsys)

    assert code == 0
    assert lines[0] == "pool-ready"
    assert lines[1].startswith("pool-spend month=")
    assert lines[1].endswith("state=no-usage")
    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_check_pool_prints_month_to_date_tokens_and_unpinned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With the shipped table, tokens are shown and EUR is withheld."""
    PoolSpendMeter(tmp_path).record("privatemode", MODEL, PoolUsage(12, 5, 0))

    code, lines = _check_pool(tmp_path, monkeypatch, capsys)

    assert code == 0
    assert "vendor=privatemode" in lines[1]
    assert "state=price-unpinned" in lines[1]
    assert "prompt_tokens=12 completion_tokens=5" in lines[1]
    assert "spend=" not in lines[1]


def test_check_pool_names_unpriced_models_beside_a_lower_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partly priced month shows a lower bound and the models to pin (#2052)."""
    table = tmp_path / "prices.json"
    table.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vendors": {
                    "privatemode": {
                        "status": "pinned",
                        "currency": "EUR",
                        "source_url": "https://prices.example.test/",
                        "read_on": "2026-10-30",
                        "models": {MODEL: {"prompt_per_million": "1"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pool_meter, "PRICE_TABLE_PATH", table)
    vault = tmp_path / "vault"
    meter = PoolSpendMeter(vault)
    meter.record("privatemode", MODEL, PoolUsage(6_000_000, 0, 0))
    meter.record("privatemode", EMBED_MODEL, PoolUsage(0, 0, 7))

    code, lines = _check_pool(vault, monkeypatch, capsys)

    assert code == 0
    assert "state=price-partial" in lines[1]
    assert "spend_lower_bound=6.00" in lines[1]
    assert " spend=" not in lines[1]
    assert lines[1].endswith(f"unpriced_models=1 unpriced_model_ids={EMBED_MODEL}")


def test_check_pool_reports_spend_even_when_the_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreachable pool keeps its own exit code and still shows spend."""
    monkeypatch.setenv(VAULT_PATH_ENV, str(tmp_path))
    monkeypatch.setenv(PORT_ENV, "8080")
    monkeypatch.setattr(
        health, "probe", lambda _s, _t: ProbeResult(ProbeStatus.POOL_UNREACHABLE)
    )

    with pytest.raises(SystemExit) as caught:
        health.main(["--check", "pool"])

    lines = capsys.readouterr().out.splitlines()
    assert caught.value.code == 31
    assert lines[0] == "pool-unreachable"
    assert lines[1].startswith("pool-spend ")


def test_other_targets_print_one_line_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Docker healthcheck's ``ready`` output is unchanged."""
    monkeypatch.setenv(VAULT_PATH_ENV, str(tmp_path))
    monkeypatch.setenv(PORT_ENV, "8080")
    monkeypatch.setattr(
        health, "probe", lambda _s, _t: ProbeResult(ProbeStatus.V1_READY)
    )

    with pytest.raises(SystemExit):
        health.main(["--check", "ready"])

    assert capsys.readouterr().out.splitlines() == ["v1-ready"]
