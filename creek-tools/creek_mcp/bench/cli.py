"""Command line for the capacity harness: ``python -m creek_mcp.bench``.

Two subcommands:

- ``reflect`` runs the capacity sweeps and writes a :class:`BenchReport`.
  ``--mode fake`` (the default) is hermetic; ``--mode live`` drives an
  operator-supplied Ollama and requires ``--model``, ``--digest`` and
  ``--git-sha`` (``scripts/bench.sh`` supplies the last from the checkout).
- ``cost`` models cost from an operator price sheet: a Fly Machine-month per
  account by default, or — with ``--metered-model`` and ``--token-report`` —
  ADR-0016's metered per-token pool, priced from the *measured* token block in
  a benchmark report. A report whose basis is not ``provider_reported`` is
  refused: a fake run's byte bound and an absent measurement are not prices.

Exit codes: ``0`` success; ``2`` refusal — a fixed message naming the flag or
field at fault, never the value; ``1`` any other failure, named by exception
type only. Every refusal happens before a request is sent or a corpus is
written. The corpus lives in a :class:`tempfile.TemporaryDirectory` that is
removed when the run ends.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import ValidationError

from creek_mcp.bench import HARNESS_VERSION
from creek_mcp.bench.cost import (
    DEFAULT_MONTHLY_REFLECTION_CAP,
    Allocation,
    AllowanceUse,
    DutyBand,
    cost_bands,
    load_price_sheet,
    metered_estimate,
)
from creek_mcp.bench.fake import FakePool, fake_factory
from creek_mcp.bench.local_only import (
    LocalOnlyFactory,
    refuse_cloud_tag,
    require_local_target,
)
from creek_mcp.bench.metadata import RunMetadata
from creek_mcp.bench.ollama_client import BenchOllamaClient
from creek_mcp.bench.pool_client import BenchPoolClient
from creek_mcp.bench.report import load_report, write_report
from creek_mcp.bench.runner import Backend, BenchPlan, Workload, run_bench

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek_mcp.bench.cost import PriceSheet

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_REFUSED: Final[int] = 2

_DEFAULT_OLLAMA_URL: Final[str] = "http://127.0.0.1:11434"
_DEFAULT_POOL_URL: Final[str] = "http://127.0.0.1:8080"
_DEFAULT_NUM_CTX: Final[int] = 4096
_DEFAULT_NUM_PREDICT: Final[int] = 128
_DEFAULT_DUTY_LOW: Final[str] = "0.05"
_DEFAULT_DUTY_HIGH: Final[str] = "1"
_FAKE_MODEL_TAG: Final[str] = "fake"
_FAKE_POOL_VENDOR: Final[str] = "fake"
_POOL_URL_FLAG: Final[str] = "--pool-url"
_CORPUS_DIR: Final[str] = "corpus"
_LIVE_REQUIRES: Final[tuple[tuple[str, str], ...]] = (
    ("model", "--model"),
    ("digest", "--digest"),
    ("git_sha", "--git-sha"),
)
_POOL_REQUIRES: Final[tuple[tuple[str, str], ...]] = (
    ("pool_identity", "--pool-identity"),
)
_POOL_LIVE_REQUIRES: Final[tuple[tuple[str, str], ...]] = (
    ("pool_vendor", "--pool-vendor"),
)
_ALLOWANCE_FLAGS: Final[tuple[str, ...]] = (
    "--allowance",
    "--seconds-per-reflection",
    "--linger-seconds",
)
_METERED_FLAGS: Final[tuple[str, ...]] = ("--metered-model", "--token-report")
_METERED_ONLY_FLAGS: Final[tuple[tuple[str, str, object], ...]] = (
    ("metered_currency", "--metered-currency", "USD"),
    ("monthly_cap", "--monthly-cap", DEFAULT_MONTHLY_REFLECTION_CAP),
    ("tokens_spent_input", "--tokens-spent-input", 0),
    ("tokens_spent_output", "--tokens-spent-output", 0),
)
"""Flags that mean nothing to the Machine-month shape, with their defaults.

Given without the metered pair they would be silently ignored, and a cost flag
that looks applied but is not is exactly the shape of a wrong number someone
budgets against. Refused instead.
"""
_FAILURES: Final[tuple[type[BaseException], ...]] = (
    OSError,
    RuntimeError,
    MemoryError,
    httpx.HTTPError,
)


class _RefusalError(ValueError):
    """A flag-level refusal raised before any work starts."""


def _int_list(text: str) -> tuple[int, ...]:
    """Parse ``1,2,4`` into ``(1, 2, 4)``; an empty string is no levels."""
    try:
        return tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        msg = "expected comma-separated integers"
        raise argparse.ArgumentTypeError(msg) from exc


def _add_workload_flags(parser: argparse.ArgumentParser) -> None:
    """Register the sweep-shaping flags of ``reflect``."""
    defaults = Workload()
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--entries", type=int, default=defaults.entries)
    parser.add_argument("--words-per-entry", type=int, default=defaults.words_per_entry)
    parser.add_argument("--query-words", type=int, default=defaults.query_words)
    parser.add_argument("--cold-trials", type=int, default=defaults.cold_trials)
    parser.add_argument("--warm-trials", type=int, default=defaults.warm_trials)
    parser.add_argument(
        "--context-sizes", type=_int_list, default=defaults.context_sizes
    )
    parser.add_argument(
        "--concurrency", type=_int_list, default=defaults.concurrency_levels
    )
    parser.add_argument("--idle-cycles", type=int, default=defaults.idle_cycles)
    parser.add_argument("--idle-seconds", type=float, default=defaults.idle_seconds)


def _add_reflect(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the ``reflect`` subcommand."""
    parser = subparsers.add_parser("reflect", help="run the capacity sweeps")
    parser.add_argument("--mode", choices=("fake", "live"), default="fake")
    parser.add_argument(
        "--provider",
        choices=("ollama", "pool"),
        default="ollama",
        help="which runtime to measure; 'pool' drives the shared inference pool",
    )
    parser.add_argument("--grounding", choices=("none", "default"), default=None)
    parser.add_argument("--ollama-url", default=_DEFAULT_OLLAMA_URL)
    parser.add_argument("--pool-url", default=_DEFAULT_POOL_URL)
    parser.add_argument(
        "--pool-identity",
        default=None,
        help="the identity every pool request names and every answer must echo",
    )
    parser.add_argument(
        "--pool-vendor", default=None, help="short label for whose pool was measured"
    )
    parser.add_argument(
        "--allow-remote-host",
        action="store_true",
        help="measure an endpoint that is not loopback or private (recorded)",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--digest", default=None)
    parser.add_argument("--git-sha", default=None)
    parser.add_argument("--num-ctx", type=int, default=_DEFAULT_NUM_CTX)
    parser.add_argument("--num-predict", type=int, default=_DEFAULT_NUM_PREDICT)
    parser.add_argument(
        "--cpu-kind", choices=("shared", "performance", "unknown"), default="unknown"
    )
    parser.add_argument(
        "--model-store-path",
        type=Path,
        default=None,
        help="directory holding the model weights; its free disk is reported",
    )
    parser.add_argument("--out", type=Path, required=True)
    _add_workload_flags(parser)


def _add_cost(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``cost`` subcommand."""
    parser = subparsers.add_parser("cost", help="model USD per account-month")
    parser.add_argument("--price-file", type=Path, required=True)
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("--duty-low", type=Decimal, default=Decimal(_DEFAULT_DUTY_LOW))
    parser.add_argument(
        "--duty-high", type=Decimal, default=Decimal(_DEFAULT_DUTY_HIGH)
    )
    parser.add_argument("--cpu-kind", choices=("shared", "performance"), default=None)
    for flag in ("--cpus", "--memory-mb", "--rootfs-gb", "--volume-gb"):
        parser.add_argument(flag, type=int, default=None)
    parser.add_argument("--allowance", type=int, default=None)
    parser.add_argument("--seconds-per-reflection", type=Decimal, default=None)
    parser.add_argument("--linger-seconds", type=Decimal, default=None)
    parser.add_argument(
        "--metered-model",
        default=None,
        help="price ADR-0016's per-token pool for this model id",
    )
    parser.add_argument(
        "--token-report",
        type=Path,
        default=None,
        help="benchmark report whose measured token block prices the pool",
    )
    parser.add_argument("--metered-currency", choices=("USD", "EUR"), default="USD")
    parser.add_argument(
        "--monthly-cap", type=int, default=DEFAULT_MONTHLY_REFLECTION_CAP
    )
    parser.add_argument("--tokens-spent-input", type=int, default=0)
    parser.add_argument("--tokens-spent-output", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m creek_mcp.bench", description=__doc__.splitlines()[0]
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_reflect(subparsers)
    _add_cost(subparsers)
    return parser


def _require_flags(
    args: argparse.Namespace, required: tuple[tuple[str, str], ...], reason: str
) -> None:
    """Refuse when any flag *reason* makes mandatory was not supplied.

    Raises:
        _RefusalError: Naming the first absent flag, never its value.
    """
    for attribute, flag in required:
        if getattr(args, attribute) is None:
            msg = f"{flag} is required for {reason}"
            raise _RefusalError(msg)


def _provider(args: argparse.Namespace) -> str:
    """Return which provider actually serves the trials.

    ``--provider`` names the runtime to measure; the hermetic mode substitutes
    the local fake for Ollama, but *not* for the pool, whose refusal and
    identity paths are exactly what the fake pool exists to exercise.
    """
    if args.provider == "pool":
        return "pool"
    return "ollama" if args.mode == "live" else "fake"


def _model_tag(args: argparse.Namespace) -> str:
    """Return the model tag the run drove, defaulting to the fake's."""
    return args.model or _FAKE_MODEL_TAG


def _endpoint_scope(args: argparse.Namespace, provider: str) -> str:
    """Classify where a live run's requests go; a fake run sends none.

    A pool endpoint is deliberately remote (ADR-0016 Decision 6), so the same
    check runs against ``--pool-url`` and the operator opts in with
    ``--allow-remote-host``; the report then records ``remote``.
    """
    if args.mode != "live":
        return "fake"
    if provider == "pool":
        return require_local_target(
            args.pool_url,
            _model_tag(args),
            allow_remote=args.allow_remote_host,
            flag=_POOL_URL_FLAG,
        )
    return require_local_target(
        args.ollama_url, args.model, allow_remote=args.allow_remote_host
    )


def _metadata(args: argparse.Namespace) -> RunMetadata:
    """Build run metadata, refusing a run missing a pinning flag."""
    live = args.mode == "live"
    provider = _provider(args)
    if live:
        _require_flags(args, _LIVE_REQUIRES, "--mode live")
    if provider == "pool":
        _require_flags(args, _POOL_REQUIRES, "--provider pool")
        if live:
            _require_flags(args, _POOL_LIVE_REQUIRES, "--provider pool --mode live")
        refuse_cloud_tag(_model_tag(args))
    pooled = provider == "pool"
    return RunMetadata.model_validate(
        {
            "mode": args.mode,
            "provider": provider,
            "endpoint_scope": _endpoint_scope(args, provider),
            "grounding": args.grounding or ("default" if live else "none"),
            "model_tag": _model_tag(args) if live or pooled else _FAKE_MODEL_TAG,
            "pool_vendor": (args.pool_vendor or _FAKE_POOL_VENDOR) if pooled else None,
            "pool_identity": args.pool_identity if pooled else None,
            "digest": args.digest,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "harness_version": HARNESS_VERSION,
            "git_sha": args.git_sha,
        }
    )


def _pool_backend(args: argparse.Namespace) -> Backend:
    """Build the pool backend; hermetic unless the mode is live.

    In the hermetic mode the client dials an in-process :class:`FakePool`, so
    every sweep and every refusal path runs with no vendor contacted and
    nothing billed (ADR-0016 Decision 4). The live path is the same client
    with no transport injected.

    :func:`_run_reflect` builds the metadata first, so ``--pool-identity`` is
    already known to be present by the time this runs; that ordering is what
    makes the pin non-optional, so do not reorder it.
    """
    fake = (
        None
        if args.mode == "live"
        else FakePool(
            identity=args.pool_identity,
            model_tag=_model_tag(args),
            measurement_digest=args.digest,
        )
    )
    client = BenchPoolClient(
        args.pool_url,
        _model_tag(args),
        identity=args.pool_identity,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        measurement_digest=args.digest,
        transport=None if fake is None else fake.transport(),
    )
    return Backend(
        factory=LocalOnlyFactory(client.factory(), provider_name="pool"),
        evict=client.evict,
        resident_bytes=client.resident_bytes,
        pin=client.verify_pinned,
    )


def _backend(args: argparse.Namespace) -> Backend:
    """Build the fake, Ollama or pool backend; performs no I/O."""
    if _provider(args) == "pool":
        return _pool_backend(args)
    if args.mode == "fake":
        return Backend(
            factory=LocalOnlyFactory(fake_factory, provider_name="fake"),
            evict=lambda: None,
            resident_bytes=lambda: None,
        )
    client = BenchOllamaClient(
        args.ollama_url,
        args.model,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
    )
    return Backend(
        factory=LocalOnlyFactory(client.factory(), provider_name="ollama"),
        evict=client.evict,
        resident_bytes=client.resident_bytes,
        pin=client.verify_digest,
    )


def _workload(args: argparse.Namespace) -> Workload:
    """Build the workload from the sweep flags."""
    return Workload(
        seed=args.seed,
        entries=args.entries,
        words_per_entry=args.words_per_entry,
        query_words=args.query_words,
        cold_trials=args.cold_trials,
        warm_trials=args.warm_trials,
        context_sizes=args.context_sizes,
        concurrency_levels=args.concurrency,
        idle_cycles=args.idle_cycles,
        idle_seconds=args.idle_seconds,
    )


def _run_reflect(args: argparse.Namespace) -> str:
    """Run the sweeps and write the report; return the summary line."""
    store = args.model_store_path
    if store is not None and not store.is_dir():
        msg = "--model-store-path must be an existing directory"
        raise _RefusalError(msg)
    metadata = _metadata(args)
    plan = BenchPlan(
        metadata=metadata, workload=_workload(args), backend=_backend(args)
    )
    with tempfile.TemporaryDirectory(prefix="creek-bench-") as scratch:
        report = run_bench(
            plan,
            Path(scratch) / _CORPUS_DIR,
            cpu_kind=args.cpu_kind,
            model_store=store,
        )
    write_report(report, args.out)
    return f"verdict={report.verdict.value} report={args.out}"


def _allowance(args: argparse.Namespace) -> AllowanceUse | None:
    """Build the allowance from its three flags, all or none."""
    values = (args.allowance, args.seconds_per_reflection, args.linger_seconds)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        msg = f"{', '.join(_ALLOWANCE_FLAGS)} must be given together"
        raise _RefusalError(msg)
    return AllowanceUse(
        reflections_per_month=args.allowance,
        seconds_per_reflection=args.seconds_per_reflection,
        linger_seconds=args.linger_seconds,
    )


def _allocation(args: argparse.Namespace) -> Allocation:
    """Build the allocation from the Fly defaults plus any override flags."""
    overrides = {
        "cpu_kind": args.cpu_kind,
        "cpus": args.cpus,
        "memory_mb": args.memory_mb,
        "rootfs_gb": args.rootfs_gb,
        "volume_gb": args.volume_gb,
    }
    return Allocation.from_fly_defaults(
        {name: value for name, value in overrides.items() if value is not None}
    )


def _run_cost(args: argparse.Namespace) -> str:
    """Model the cost and write the estimate; return the summary line."""
    metered = _metered_requested(args)
    allowance = None if metered else _allowance(args)
    sheet = load_price_sheet(args.price_file, today=args.today or date.today())
    if metered:
        return _write_metered(args, sheet)
    estimate = cost_bands(
        sheet,
        _allocation(args),
        DutyBand(low=args.duty_low, high=args.duty_high),
        allowance=allowance,
    )
    args.out.write_text(estimate.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return (
        f"kind={estimate.kind} low_usd={estimate.low_usd} "
        f"high_usd={estimate.high_usd} report={args.out}"
    )


def _metered_requested(args: argparse.Namespace) -> bool:
    """Return whether the metered estimate was asked for, all flags or none.

    Raises:
        _RefusalError: When only one of the metered pair was given, or a
            metered-only flag was given without the pair.
    """
    values = (args.metered_model, args.token_report)
    if not any(value is None for value in values):
        return True
    for attribute, flag, default in _METERED_ONLY_FLAGS:
        if getattr(args, attribute) != default:
            msg = f"{flag} applies only with {', '.join(_METERED_FLAGS)}"
            raise _RefusalError(msg)
    if all(value is None for value in values):
        return False
    msg = f"{', '.join(_METERED_FLAGS)} must be given together"
    raise _RefusalError(msg)


def _write_metered(args: argparse.Namespace, sheet: PriceSheet) -> str:
    """Price the metered pool from the report's measured block and write it.

    The report is read for one field — its token block — and nothing else: a
    cost figure must cite a measurement rather than restate an assumption.
    """
    estimate = metered_estimate(
        sheet,
        model_id=args.metered_model,
        currency=args.metered_currency,
        tokens=load_report(args.token_report).tokens_per_reflection,
        monthly_reflection_cap=args.monthly_cap,
        input_tokens_spent=args.tokens_spent_input,
        output_tokens_spent=args.tokens_spent_output,
    )
    args.out.write_text(estimate.model_dump_json(indent=2) + "\n", encoding="utf-8")
    per_1000 = ", ".join(
        f"{one.reflections_per_month}/mo={one.cost_per_1000_reflections}"
        for one in estimate.per_volume
    )
    return (
        f"kind={estimate.kind} shape={estimate.shape} "
        f"basis={estimate.token_basis.value} currency={estimate.currency} "
        f"per_1000[{per_1000}] "
        f"per_account_month={estimate.cost_per_account_month} "
        f"report={args.out}"
    )


def _refusal_text(exc: ValueError) -> str:
    """Name what was refused without echoing any supplied value."""
    if isinstance(exc, ValidationError):
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        return f"invalid value for: {', '.join(fields) or 'arguments'}"
    return str(exc)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harness CLI and return its exit code."""
    args = _parser().parse_args(argv)
    command = _run_reflect if args.command == "reflect" else _run_cost
    try:
        line = command(args)
    except ValueError as exc:
        print(f"refused: {_refusal_text(exc)}", file=sys.stderr)
        return EXIT_REFUSED
    except _FAILURES as exc:
        print(f"failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED
    print(line)
    return EXIT_OK
