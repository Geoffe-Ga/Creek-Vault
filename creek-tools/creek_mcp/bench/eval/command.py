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

**Owner-run real-voice check (#1875).** ``--corpus-vault PATH`` prompts and
scores against a bounded sample of the owner's own vault instead of the
synthetic corpus (:mod:`creek_mcp.bench.eval.vault_corpus`). It is refused
unless ``--mode live``, ``--i-own-this-vault``, ``--review-out`` and
``--unblind-out`` are all given, the judge is hermetic, ``CI`` is unset, the
path is a Creek vault, and neither private output exists yet — every one of
those checked before the vault is read or a provider is built. The ``--out``
ranking is then content-free; the candidate texts go to the ``0600`` review
file under blinded labels (:mod:`creek_mcp.bench.eval.review`).

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
from creek.models import PrivacyTier
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
    CorpusSource,
    EvalMode,
    EvalRanking,
    ModelRow,
    rank_rows,
    write_ranking,
)
from creek_mcp.bench.eval.review import (
    render_review,
    unblind_key,
    write_private,
    write_unblind_key,
)
from creek_mcp.bench.eval.rubric import JudgeKind
from creek_mcp.bench.eval.runner import (
    EvalPlan,
    EvalPlanError,
    run_eval,
    score_against,
)
from creek_mcp.bench.eval.vault_corpus import (
    VAULT_MAX_ENTRIES,
    VaultCorpus,
    load_vault_corpus,
)
from creek_mcp.bench.eval.voice_corpus import build_voice_corpus
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

    from creek.classify.llm.base import LLMProvider
    from creek_mcp.bench.eval.blinding import Candidate
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
_VAULT_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("review_out", "--review-out"),
    ("unblind_out", "--unblind-out"),
)
_VAULT_ONLY: Final[str] = "{} applies only with --corpus-vault"
_UNDER_CI: Final[str] = (
    "--corpus-vault is refused under CI: it reads a real vault and runs only "
    "on the owner's own machine"
)
_NO_ACK: Final[str] = (
    "--corpus-vault requires --i-own-this-vault: the vault's open and personal "
    "prose is sent to the named provider"
)
_NOT_LIVE: Final[str] = "--corpus-vault requires --mode live"
_ROUTED_JUDGE: Final[str] = (
    "--corpus-vault takes only --judge hermetic: a routed judge would send the "
    "vault's prose to a second provider"
)
_NEEDS_OUTPUT: Final[str] = "--corpus-vault requires {}"
_OUTPUT_EXISTS: Final[str] = (
    "{} names a file that already exists; it is never overwritten"
)
_NOT_DISTINCT: Final[str] = "--out, --review-out and --unblind-out must be distinct"
_NOT_A_VAULT: Final[str] = "--corpus-vault does not name a Creek vault"
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
        vault: The owner-run vault sample, when ``--corpus-vault`` is used;
            ``None`` for the synthetic corpus.
    """

    sources: Mapping[str, CandidateSource]
    judge: Judge
    judge_kind: JudgeKind = JudgeKind.HERMETIC
    mode: EvalMode = EvalMode.FAKE
    seed: int = 0
    entries: int | None = None
    instructions: DeskInstructions = field(default=DEFAULT_INSTRUCTIONS)
    vault: VaultCorpus | None = None


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
    parser.add_argument(
        "--corpus-vault",
        type=Path,
        default=None,
        help="owner-run: sample this Creek vault instead of the synthetic corpus",
    )
    parser.add_argument(
        "--i-own-this-vault",
        action="store_true",
        help="acknowledge the vault is yours and its open/personal prose is sent",
    )
    parser.add_argument(
        "--review-out", type=Path, default=None, help="private 0600 review file"
    )
    parser.add_argument(
        "--unblind-out", type=Path, default=None, help="private 0600 unblinding key"
    )


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


def _live_credentials(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[str, str] | None:
    """Return the live ``(provider, key_env)``, or ``None`` for a fake run.

    Raises:
        EvalPlanError: When a live-only flag is given without ``--mode live``,
            or a live run lacks its credentials.
    """
    if args.mode != EvalMode.LIVE:
        _refuse_unused(args, _PROMPT_MODEL_FLAGS, "--mode live")
        return None
    return _require_credentials(
        args.provider, args.api_key_env, ("--provider", "--api-key-env"), environ
    )


def _sources(
    args: argparse.Namespace,
    models: tuple[str, ...],
    credentials: tuple[str, str] | None,
    factory: Callable[[LLMConfig], LLMProvider],
    tier: PrivacyTier,
) -> dict[str, CandidateSource]:
    """Build one source per model: fake by default, routed under ``live``."""
    if credentials is None:
        return {tag: FakeCandidateSource(tag) for tag in models}
    provider, key_env = credentials
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
            tier=tier,
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


def _private_outputs(args: argparse.Namespace) -> None:
    """Refuse missing, colliding or already-existing private output paths."""
    for attribute, flag in _VAULT_FLAGS:
        path: Path | None = getattr(args, attribute)
        if path is None:
            raise EvalPlanError(_NEEDS_OUTPUT.format(flag))
        if path.exists() or path.is_symlink():
            raise EvalPlanError(_OUTPUT_EXISTS.format(flag))
    targets = {
        args.out.resolve(),
        args.review_out.resolve(),
        args.unblind_out.resolve(),
    }
    if len(targets) != 1 + len(_VAULT_FLAGS):
        raise EvalPlanError(_NOT_DISTINCT)


def _corpus_vault(args: argparse.Namespace, environ: Mapping[str, str]) -> Path | None:
    """Return the ``--corpus-vault`` path once every opt-in holds, else ``None``.

    Nothing is read from the vault here beyond whether its Creek marker exists.

    Raises:
        EvalPlanError: On any refused or incomplete opt-in; see the module
            docstring for the full list.
    """
    vault: Path | None = args.corpus_vault
    if vault is None:
        if args.i_own_this_vault:
            raise EvalPlanError(_VAULT_ONLY.format("--i-own-this-vault"))
        _refuse_unused(args, _VAULT_FLAGS, "--corpus-vault")
        return None
    if environ.get("CI", "").strip():
        raise EvalPlanError(_UNDER_CI)
    if not args.i_own_this_vault:
        raise EvalPlanError(_NO_ACK)
    if args.mode != EvalMode.LIVE:
        raise EvalPlanError(_NOT_LIVE)
    if args.judge != JudgeKind.HERMETIC:
        raise EvalPlanError(_ROUTED_JUDGE)
    _private_outputs(args)
    if not vault_available(vault):
        raise EvalPlanError(_NOT_A_VAULT)
    return vault


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
        VaultCorpusError: When ``--corpus-vault`` cannot supply a bounded,
            admissible sample.
    """
    models = _models(args.models)
    if not 1 <= args.max_tokens <= MAX_TOKENS_CEILING:
        raise EvalPlanError(_BAD_BUDGET)
    source_env = os.environ if environ is None else environ
    factory = build_provider if provider_factory is None else provider_factory
    vault_path = _corpus_vault(args, source_env)
    judge, judge_kind = _judge(args, source_env, factory)
    credentials = _live_credentials(args, source_env)
    vault = (
        None
        if vault_path is None
        else load_vault_corpus(
            vault_path,
            seed=args.seed,
            max_entries=VAULT_MAX_ENTRIES if args.entries is None else args.entries,
        )
    )
    tier = PrivacyTier.OPEN if vault is None else vault.tier
    return RankedEvalPlan(
        sources=_sources(args, models, credentials, factory, tier),
        judge=judge,
        judge_kind=judge_kind,
        mode=EvalMode(args.mode),
        seed=args.seed,
        entries=None if vault is not None else args.entries,
        vault=vault,
    )


def _score_model(
    plan: RankedEvalPlan,
    corpus: VoiceCorpus,
    tag: str,
    root: Path,
) -> tuple[ModelRow, Candidate]:
    """Generate *tag*'s candidate and score it in its own run."""
    candidate = generate_candidate(corpus, plan.instructions, tag, plan.sources[tag])
    eval_plan = EvalPlan(
        candidates=(candidate,),
        judge=plan.judge,
        judge_kind=plan.judge_kind,
        seed=plan.seed,
        entries=plan.entries,
        instructions=plan.instructions,
        voice_evidence_present=PROMPT_CARRIES_VOICE_EVIDENCE,
    )
    # A vault sample is scored in place — it is never rebuilt or written;
    # the synthetic corpus is rebuilt per model, as it always was.
    result = (
        run_eval(eval_plan, root)
        if plan.vault is None
        else score_against(eval_plan, corpus)
    )
    row = ModelRow(
        model_tag=tag,
        quality=result.quality,
        record=result.record,
        invented_citations=len(unknown_citations(candidate.citations, corpus)),
    )
    return row, candidate


def run_ranked_eval(plan: RankedEvalPlan, scratch: Path) -> EvalRanking:
    """Generate, score and rank every model in *plan*.

    Args:
        plan: What to run.
        scratch: Absent or empty directory strictly under the system temp
            dir; each model's synthetic corpus is built beneath it.

    Returns:
        The ranked artifact.

    Raises:
        EvalPlanError: When the plan names no model, or its judge kind
            contradicts its judge — checked before any candidate is generated.
    """
    ranking, _candidates = rank_with_candidates(plan, scratch)
    return ranking


def rank_with_candidates(
    plan: RankedEvalPlan, scratch: Path
) -> tuple[EvalRanking, tuple[Candidate, ...]]:
    """Like :func:`run_ranked_eval`, also returning each model's candidate.

    The candidates are in the order the models were named, which is the order
    :func:`~creek_mcp.bench.eval.review.render_review` blinds them in.

    Args:
        plan: What to run.
        scratch: As for :func:`run_ranked_eval`.

    Returns:
        The ranked artifact, and the candidates behind it.

    Raises:
        EvalPlanError: As for :func:`run_ranked_eval`.
    """
    if not plan.sources:
        raise EvalPlanError(_NO_MODELS)
    expected = JudgeKind.ROUTED if plan.judge.is_cloud else JudgeKind.HERMETIC
    if plan.judge_kind is not expected:
        raise EvalPlanError(_JUDGE_MISMATCH)
    if plan.vault is None:
        corpus = build_voice_corpus(scratch / _GENERATION_DIR, entries=plan.entries)
        source, counts = CorpusSource.SYNTHETIC, None
    else:
        corpus = plan.vault.corpus
        source, counts = CorpusSource.VAULT, plan.vault.counts.to_json_dict()
    scored = [
        _score_model(plan, corpus, tag, scratch / _MODEL_DIR.format(index))
        for index, tag in enumerate(plan.sources)
    ]
    ranking = EvalRanking(
        mode=plan.mode,
        judge=plan.judge_kind,
        seed=plan.seed,
        corpus_entries=corpus.entry_count,
        generation_prompt=build_prompt(corpus, plan.instructions),
        rows=rank_rows(row for row, _candidate in scored),
        corpus_source=source,
        vault_counts=counts,
    )
    return ranking, tuple(candidate for _row, candidate in scored)


def _write_private_outputs(
    args: argparse.Namespace,
    plan: RankedEvalPlan,
    candidates: tuple[Candidate, ...],
) -> None:
    """Write the owner's review file and unblinding key, both ``0600``."""
    if plan.vault is None:
        return
    write_private(
        args.review_out, render_review(candidates, seed=plan.seed, vault=plan.vault)
    )
    write_unblind_key(args.unblind_out, unblind_key(candidates, seed=plan.seed))


def run_eval_command(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    provider_factory: Callable[[LLMConfig], LLMProvider] | None = None,
) -> str:
    """Run ``bench eval`` from parsed flags and write the ranking to ``--out``.

    Under ``--corpus-vault`` the ranking is content-free, and the candidate
    texts go to the private ``--review-out`` file with their key in
    ``--unblind-out``.

    Args:
        args: Flags registered by :func:`add_eval_arguments`.
        environ: As for :func:`plan_from_args`.
        provider_factory: As for :func:`plan_from_args`.

    Returns:
        The one-line summary the CLI prints. It names no file content.
    """
    plan = plan_from_args(args, environ=environ, provider_factory=provider_factory)
    with tempfile.TemporaryDirectory(prefix="creek-bench-eval-") as scratch:
        ranking, candidates = rank_with_candidates(plan, Path(scratch))
    _write_private_outputs(args, plan, candidates)
    write_ranking(ranking, args.out)
    top = ranking.rows[0]
    return (
        f"mode={ranking.mode.value} judge={ranking.judge.value} "
        f"corpus={ranking.corpus_source.value} "
        f"models={len(ranking.rows)} top={top.model_tag} "
        f"verdict={top.quality.verdict.value} report={args.out}"
    )
