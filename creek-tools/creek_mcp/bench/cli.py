"""Command line for the capacity harness: ``python -m creek_mcp.bench``.

Two subcommands:

- ``reflect`` runs the capacity sweeps and writes a :class:`BenchReport`.
  ``--mode fake`` (the default) is hermetic; ``--mode live`` drives an
  operator-supplied Ollama and requires ``--model``, ``--digest`` and
  ``--git-sha`` (``scripts/bench.sh`` supplies the last from the checkout).
- ``cost`` models USD per account-month from an operator price sheet.

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
    Allocation,
    AllowanceUse,
    DutyBand,
    cost_bands,
    load_price_sheet,
)
from creek_mcp.bench.fake import fake_factory
from creek_mcp.bench.local_only import LocalOnlyFactory, require_local_target
from creek_mcp.bench.metadata import RunMetadata
from creek_mcp.bench.ollama_client import BenchOllamaClient
from creek_mcp.bench.report import write_report
from creek_mcp.bench.runner import Backend, BenchPlan, Workload, run_bench

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_REFUSED: Final[int] = 2

_DEFAULT_OLLAMA_URL: Final[str] = "http://127.0.0.1:11434"
_DEFAULT_NUM_CTX: Final[int] = 4096
_DEFAULT_NUM_PREDICT: Final[int] = 128
_DEFAULT_DUTY_LOW: Final[str] = "0.05"
_DEFAULT_DUTY_HIGH: Final[str] = "1"
_FAKE_MODEL_TAG: Final[str] = "fake"
_CORPUS_DIR: Final[str] = "corpus"
_LIVE_REQUIRES: Final[tuple[tuple[str, str], ...]] = (
    ("model", "--model"),
    ("digest", "--digest"),
    ("git_sha", "--git-sha"),
)
_ALLOWANCE_FLAGS: Final[tuple[str, ...]] = (
    "--allowance",
    "--seconds-per-reflection",
    "--linger-seconds",
)
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
    parser.add_argument("--grounding", choices=("none", "default"), default=None)
    parser.add_argument("--ollama-url", default=_DEFAULT_OLLAMA_URL)
    parser.add_argument(
        "--allow-remote-host",
        action="store_true",
        help="measure an --ollama-url that is not loopback or private (recorded)",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--digest", default=None)
    parser.add_argument("--git-sha", default=None)
    parser.add_argument("--num-ctx", type=int, default=_DEFAULT_NUM_CTX)
    parser.add_argument("--num-predict", type=int, default=_DEFAULT_NUM_PREDICT)
    parser.add_argument(
        "--cpu-kind", choices=("shared", "performance", "unknown"), default="unknown"
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


def _metadata(args: argparse.Namespace) -> RunMetadata:
    """Build run metadata, refusing a live run missing a pinning flag."""
    live = args.mode == "live"
    if live:
        for attribute, flag in _LIVE_REQUIRES:
            if getattr(args, attribute) is None:
                msg = f"{flag} is required for --mode live"
                raise _RefusalError(msg)
    grounding = args.grounding or ("default" if live else "none")
    scope = (
        require_local_target(
            args.ollama_url, args.model, allow_remote=args.allow_remote_host
        )
        if live
        else "fake"
    )
    return RunMetadata.model_validate(
        {
            "mode": args.mode,
            "provider": "ollama" if live else "fake",
            "endpoint_scope": scope,
            "grounding": grounding,
            "model_tag": args.model if live else _FAKE_MODEL_TAG,
            "digest": args.digest,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "harness_version": HARNESS_VERSION,
            "git_sha": args.git_sha,
        }
    )


def _backend(args: argparse.Namespace) -> Backend:
    """Build the fake or live backend; performs no I/O."""
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
    plan = BenchPlan(
        metadata=_metadata(args), workload=_workload(args), backend=_backend(args)
    )
    with tempfile.TemporaryDirectory(prefix="creek-bench-") as scratch:
        report = run_bench(plan, Path(scratch) / _CORPUS_DIR, cpu_kind=args.cpu_kind)
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
    allowance = _allowance(args)
    sheet = load_price_sheet(args.price_file, today=args.today or date.today())
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
