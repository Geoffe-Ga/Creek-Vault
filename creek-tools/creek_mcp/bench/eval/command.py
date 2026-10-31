"""``bench eval``: generate one candidate per model, score each, write a ranking.

The subcommand behind ``python -m creek_mcp.bench eval`` and
``./scripts/bench.sh eval``. :mod:`creek_mcp.bench.cli` registers it with
:func:`add_eval_arguments` and runs it with :func:`run_eval_command`; this
module owns the flags, the refusals and the orchestration so the capacity
CLI does not grow a second concern.

**Hermetic unless an operator says otherwise, twice.** The defaults are
``--mode fake`` (each model is a :class:`~…candidates.FakeCandidateSource`) and
``--judge hermetic``. Reaching a provider takes an explicit flag *and* named
credentials:

- ``--mode live`` needs ``--provider`` and ``--api-key-env``, and the variable
  that flag names must be set. The provider then applies its own cloud-egress
  consent gate before anything is sent.
- ``--judge routed`` needs ``--judge-provider`` and ``--judge-api-key-env`` on
  the same terms, and the artifact records ``judge: routed``.

A live-only flag given without its mode is refused rather than ignored: a flag
that looks applied but is not is the shape of a run someone misreads. Every
refusal is a fixed message naming the flag, never its value, and happens before
any provider is built.

**Bounded.** At most :data:`MAX_MODELS` distinct models, one completion each,
capped at ``--max-tokens`` (no more than :data:`MAX_TOKENS_CEILING`), plus at
most one judge call per model. Every source is built before the first call, so
a model with unmet prerequisites stops the run before any other model is
billed.

**Per-model runs.** Each model's candidate is scored by its own
:func:`~creek_mcp.bench.eval.runner.run_eval` against a freshly built copy of
the same deterministic corpus, so every row is that model's own verdict rather
than a share of a pooled one. The judge is still handed only a blinded
:class:`~creek_mcp.bench.eval.judge.JudgeRequest`; identities are attached to
rows only after scoring.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek.classify.llm.providers import build_provider
from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig
from creek_mcp.bench.eval.candidates import (
    PROMPT_CARRIES_VOICE_EVIDENCE,
    CandidateSource,
    FakeCandidateSource,
    build_prompt,
    generate_candidate,
    routed_source,
    unknown_citations,
)
from creek_mcp.bench.eval.dimensions import DEFAULT_INSTRUCTIONS, DeskInstructions
from creek_mcp.bench.eval.judge import HermeticJudge, Judge, RoutedJudge
from creek_mcp.bench.eval.ranking import (
    EvalMode,
    EvalRanking,
    ModelRow,
    rank_rows,
    write_ranking,
)
from creek_mcp.bench.eval.rubric import JudgeKind
from creek_mcp.bench.eval.runner import EvalPlan, EvalPlanError, run_eval
from creek_mcp.bench.eval.voice_corpus import build_voice_corpus

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

    from creek.classify.llm.base import LLMProvider
    from creek_mcp.bench.eval.voice_corpus import VoiceCorpus

MAX_MODELS: Final[int] = 5
"""Most models one run may name. The owner's pilot is two or three."""

DEFAULT_MAX_TOKENS: Final[int] = 800
"""Default output ceiling per candidate: room for a 400-word reflection."""

MAX_TOKENS_CEILING: Final[int] = 2048
"""Hard ceiling on ``--max-tokens``; a bounded run cannot be unbounded."""

LIVE_PROVIDERS: Final[tuple[str, ...]] = ("openai", "anthropic", "gemini")
"""Providers ``--mode live`` may name. Each reads a key and checks consent.

An OpenAI-compatible endpoint (a confidential-inference proxy, say) is
reached as ``openai`` with ``--api-base``.
"""

_GENERATION_DIR: Final[str] = "generation"
_MODEL_DIR: Final[str] = "model-{}"
_PROMPT_MODEL_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("provider", "--provider"),
    ("api_base", "--api-base"),
    ("api_key_env", "--api-key-env"),
)
_JUDGE_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("judge_provider", "--judge-provider"),
    ("judge_model", "--judge-model"),
    ("judge_api_base", "--judge-api-base"),
    ("judge_api_key_env", "--judge-api-key-env"),
)
_NO_MODELS: Final[str] = "--models must name at least one model"
_DUPLICATE: Final[str] = "--models must not repeat a model"
_TOO_MANY: Final[str] = f"--models may name at most {MAX_MODELS} models"
_BAD_BUDGET: Final[str] = f"--max-tokens must be between 1 and {MAX_TOKENS_CEILING}"
_JUDGE_MISMATCH: Final[str] = (
    "judge_kind must match the judge: a cloud judge is routed, a local one hermetic"
)


@dataclass(frozen=True, slots=True)
class RankedEvalPlan:
    """What ``bench eval`` runs.

    Attributes:
        sources: One candidate source per model tag, in the order named.
        judge: The voice-match judge.
        judge_kind: Which judge this is; must agree with ``judge.is_cloud``.
        mode: Whether the sources are fake or live, recorded in the artifact.
        seed: Presentation seed, shared by every model's run.
        entries: Synthetic corpus entries, inside #1875's 10-20 range.
        instructions: The Writing Desk instructions stated and scored.
    """

    sources: Mapping[str, CandidateSource]
    judge: Judge
    judge_kind: JudgeKind = JudgeKind.HERMETIC
    mode: EvalMode = EvalMode.FAKE
    seed: int = 0
    entries: int | None = None
    instructions: DeskInstructions = field(default=DEFAULT_INSTRUCTIONS)


def add_eval_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``eval`` subcommand's flags on *parser*."""
    parser.add_argument(
        "--models", required=True, help="comma-separated model tags to rank"
    )
    parser.add_argument("--mode", choices=("fake", "live"), default="fake")
    parser.add_argument("--judge", choices=("hermetic", "routed"), default="hermetic")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--entries", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--provider", choices=LIVE_PROVIDERS, default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument(
        "--api-key-env", default=None, help="NAME of the variable holding the key"
    )
    parser.add_argument("--judge-provider", choices=LIVE_PROVIDERS, default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--judge-api-key-env", default=None)
    parser.add_argument("--out", type=Path, required=True)


def _models(text: str) -> tuple[str, ...]:
    """Parse and bound ``--models``.

    Raises:
        EvalPlanError: When the list is empty, repeats a model, or is too long.
    """
    models = tuple(part.strip() for part in text.split(",") if part.strip())
    if not models:
        raise EvalPlanError(_NO_MODELS)
    if len(set(models)) != len(models):
        raise EvalPlanError(_DUPLICATE)
    if len(models) > MAX_MODELS:
        raise EvalPlanError(_TOO_MANY)
    return models


def _refuse_unused(
    args: argparse.Namespace, flags: tuple[tuple[str, str], ...], reason: str
) -> None:
    """Refuse any of *flags* given where *reason* says it would be ignored."""
    for attribute, flag in flags:
        if getattr(args, attribute) is not None:
            msg = f"{flag} applies only with {reason}"
            raise EvalPlanError(msg)


def _require_credentials(
    provider: str | None,
    key_env: str | None,
    flags: tuple[str, str],
    environ: Mapping[str, str],
) -> tuple[str, str]:
    """Return ``(provider, key_env)`` once both are given and the key is set.

    Raises:
        EvalPlanError: Naming the missing flag, or saying the named variable
            is unset — never the variable's value.
    """
    provider_flag, key_flag = flags
    if provider is None:
        msg = f"{provider_flag} is required to reach a provider"
        raise EvalPlanError(msg)
    if key_env is None:
        msg = f"{key_flag} is required to reach a provider"
        raise EvalPlanError(msg)
    if not environ.get(key_env, "").strip():
        msg = f"the variable named by {key_flag} is unset"
        raise EvalPlanError(msg)
    return provider, key_env


def _sources(
    args: argparse.Namespace,
    models: tuple[str, ...],
    environ: Mapping[str, str],
    factory: Callable[[LLMConfig], LLMProvider],
) -> dict[str, CandidateSource]:
    """Build one source per model: fake by default, routed under ``live``."""
    if args.mode != EvalMode.LIVE:
        _refuse_unused(args, _PROMPT_MODEL_FLAGS, "--mode live")
        return {tag: FakeCandidateSource(tag) for tag in models}
    provider, key_env = _require_credentials(
        args.provider, args.api_key_env, ("--provider", "--api-key-env"), environ
    )
    return {
        tag: routed_source(
            LLMConfig(
                provider=provider,
                model=tag,
                api_base=args.api_base,
                api_key_env=key_env,
            ),
            max_tokens=args.max_tokens,
            provider_factory=factory,
        )
        for tag in models
    }


def _judge(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    factory: Callable[[LLMConfig], LLMProvider],
) -> tuple[Judge, JudgeKind]:
    """Build the judge: hermetic by default, routed only when asked and keyed."""
    if args.judge != JudgeKind.ROUTED:
        _refuse_unused(args, _JUDGE_FLAGS, "--judge routed")
        return HermeticJudge(), JudgeKind.HERMETIC
    provider, key_env = _require_credentials(
        args.judge_provider,
        args.judge_api_key_env,
        ("--judge-provider", "--judge-api-key-env"),
        environ,
    )
    config = LLMConfig(
        provider=provider,
        model=args.judge_model,
        api_base=args.judge_api_base,
        api_key_env=key_env,
    )
    return RoutedJudge(ModelRouter(config), provider_factory=factory), JudgeKind.ROUTED


def plan_from_args(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    provider_factory: Callable[[LLMConfig], LLMProvider] | None = None,
) -> RankedEvalPlan:
    """Turn parsed ``eval`` flags into a plan, refusing before any provider is built.

    Args:
        args: Flags registered by :func:`add_eval_arguments`.
        environ: Where named key variables are looked up; ``os.environ`` when
            omitted.
        provider_factory: Builds a provider from a config; ``build_provider``
            when omitted. Nothing is called on a provider here.

    Returns:
        The plan.

    Raises:
        EvalPlanError: On any refused flag combination.
    """
    models = _models(args.models)
    if not 1 <= args.max_tokens <= MAX_TOKENS_CEILING:
        raise EvalPlanError(_BAD_BUDGET)
    source_env = os.environ if environ is None else environ
    factory = build_provider if provider_factory is None else provider_factory
    judge, judge_kind = _judge(args, source_env, factory)
    return RankedEvalPlan(
        sources=_sources(args, models, source_env, factory),
        judge=judge,
        judge_kind=judge_kind,
        mode=EvalMode(args.mode),
        seed=args.seed,
        entries=args.entries,
    )


def _score_model(
    plan: RankedEvalPlan,
    corpus: VoiceCorpus,
    tag: str,
    root: Path,
) -> ModelRow:
    """Generate *tag*'s candidate and score it in its own run."""
    candidate = generate_candidate(corpus, plan.instructions, tag, plan.sources[tag])
    result = run_eval(
        EvalPlan(
            candidates=(candidate,),
            judge=plan.judge,
            judge_kind=plan.judge_kind,
            seed=plan.seed,
            entries=plan.entries,
            instructions=plan.instructions,
            voice_evidence_present=PROMPT_CARRIES_VOICE_EVIDENCE,
        ),
        root,
    )
    return ModelRow(
        model_tag=tag,
        quality=result.quality,
        record=result.record,
        invented_citations=len(unknown_citations(candidate.citations, corpus)),
    )


def run_ranked_eval(plan: RankedEvalPlan, scratch: Path) -> EvalRanking:
    """Generate, score and rank every model in *plan*.

    Args:
        plan: What to run.
        scratch: Absent or empty directory strictly under the system temp
            dir; each model's corpus is built beneath it.

    Returns:
        The ranked artifact.

    Raises:
        EvalPlanError: When the plan names no model, or its judge kind
            contradicts its judge — checked before any candidate is generated.
    """
    if not plan.sources:
        raise EvalPlanError(_NO_MODELS)
    expected = JudgeKind.ROUTED if plan.judge.is_cloud else JudgeKind.HERMETIC
    if plan.judge_kind is not expected:
        raise EvalPlanError(_JUDGE_MISMATCH)
    corpus = build_voice_corpus(scratch / _GENERATION_DIR, entries=plan.entries)
    rows = [
        _score_model(plan, corpus, tag, scratch / _MODEL_DIR.format(index))
        for index, tag in enumerate(plan.sources)
    ]
    return EvalRanking(
        mode=plan.mode,
        judge=plan.judge_kind,
        seed=plan.seed,
        corpus_entries=corpus.entry_count,
        generation_prompt=build_prompt(corpus, plan.instructions),
        rows=rank_rows(rows),
    )


def run_eval_command(args: argparse.Namespace) -> str:
    """Run ``bench eval`` from parsed flags and write the ranking to ``--out``.

    Args:
        args: Flags registered by :func:`add_eval_arguments`.

    Returns:
        The one-line summary the CLI prints.
    """
    plan = plan_from_args(args)
    with tempfile.TemporaryDirectory(prefix="creek-bench-eval-") as scratch:
        ranking = run_ranked_eval(plan, Path(scratch))
    write_ranking(ranking, args.out)
    top = ranking.rows[0]
    return (
        f"mode={ranking.mode.value} judge={ranking.judge.value} "
        f"models={len(ranking.rows)} top={top.model_tag} "
        f"verdict={top.quality.verdict.value} report={args.out}"
    )
