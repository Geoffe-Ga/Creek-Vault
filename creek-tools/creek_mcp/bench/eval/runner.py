"""Run the voice-fidelity eval: build the corpus, blind, score, record.

One entry point, :func:`run_eval`. It builds the synthetic corpus under the
shared confinement gate, checks the corpus can serve as a reference through the
shipped voice-authenticity probe, blinds the candidates from the plan's seed,
scores all four dimensions, and returns both artifacts — the closed
:class:`~creek_mcp.bench.eval.rubric.QualityScore` for the benchmark report and
the prompt-bearing :class:`~creek_mcp.bench.eval.record.EvalRecord`.

Two properties a reader should be able to check by eye:

- **Reproducible.** Two runs of one plan at one seed produce identical scores.
  The seed selects presentation order and the opaque labels; every scorer reads
  the candidate's text, so the score of a fixed output does not move when the
  seed does.
- **Hermetic by default.** The plan's judge defaults to nothing — the caller
  passes one — and the only judge that runs in the default test lane is
  :class:`~creek_mcp.bench.eval.judge.HermeticJudge`. A
  :class:`~creek_mcp.bench.eval.judge.RoutedJudge` is a deliberate choice,
  recorded in the artifact as ``judge: routed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from creek_mcp.bench.eval.blinding import Candidate, blind
from creek_mcp.bench.eval.dimensions import (
    DEFAULT_INSTRUCTIONS,
    DeskInstructions,
    corpus_health,
    score_grounding_accuracy,
    score_instruction_obedience,
    score_refusal_behaviour,
    score_voice_match,
)
from creek_mcp.bench.eval.record import EvalRecord
from creek_mcp.bench.eval.rubric import (
    Dimension,
    DimensionScore,
    JudgeKind,
    QualityScore,
    next_candidate,
    rubric_verdict,
)
from creek_mcp.bench.eval.voice_corpus import VoiceCorpus, build_voice_corpus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from creek_mcp.bench.eval.judge import Judge, JudgeRequest

_NO_CANDIDATES: Final[str] = "an eval needs at least one candidate"
_JUDGE_MISMATCH: Final[str] = (
    "judge_kind must match the judge: a cloud judge is routed, a local one hermetic"
)


class EvalPlanError(ValueError):
    """The requested eval cannot be run as specified."""


@dataclass(frozen=True, slots=True)
class EvalPlan:
    """What to evaluate, against which judge, at which seed.

    Attributes:
        candidates: The candidate outputs under test, with their identities.
        judge: The judge for the voice-match dimension.
        judge_kind: Which judge this is, recorded in both artifacts. A caller
            passing a :class:`~creek_mcp.bench.eval.judge.RoutedJudge` states
            ``ROUTED`` so the artifact does not claim a hermetic run, and
            :func:`run_eval` refuses the plan if the declaration contradicts
            the judge's own ``is_cloud``. An artifact that claimed a hermetic
            run over a cloud judge would be worse than no artifact.
        seed: Presentation seed; selects order and labels, never a score.
        entries: Synthetic corpus entries, inside #1875's 10-20 range.
        instructions: The Writing Desk instruction surface to check.
        voice_evidence_present: Whether the prompt the candidates were given
            carried evidence of the writer's voice. Defaults to ``False``,
            which is the honest reading of #1880 at this commit: the letter
            prompt carries none, so voice match reports ``unsupported`` until
            a caller can say otherwise. The default is deliberately the
            conservative one — an eval that silently scored voice match
            against a prompt with no voice in it would read as evidence.
    """

    candidates: tuple[Candidate, ...]
    judge: Judge
    judge_kind: JudgeKind = JudgeKind.HERMETIC
    seed: int = 0
    entries: int | None = None
    instructions: DeskInstructions = field(default=DEFAULT_INSTRUCTIONS)
    voice_evidence_present: bool = False


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Both artifacts from one run, plus the corpus they were scored against.

    Attributes:
        quality: The closed score, for :class:`~creek_mcp.bench.report.BenchReport`.
        record: The prompt-bearing artifact, for :func:`…record.write_record`.
        corpus: The synthetic corpus the run built.
    """

    quality: QualityScore
    record: EvalRecord
    corpus: VoiceCorpus

    def next_model(self, ladder: Sequence[str], current: str) -> str | None:
        """Return the next model to try on *ladder*, or ``None`` to stop.

        A thin pass-through to :func:`…rubric.next_candidate` so a caller
        holding a result does not have to re-derive the verdict. A pass stops
        the climb: the owner's bar is explicitly not frontier.

        Args:
            ladder: Candidate models, cheapest first.
            current: The rung this result evaluated.

        Returns:
            The next rung up, or ``None``.

        Raises:
            ValueError: When *current* is not on *ladder*.
        """
        return next_candidate(ladder, current, self.quality.verdict)


def run_eval(plan: EvalPlan, corpus_root: Path) -> EvalResult:
    """Score every candidate in *plan* on all four dimensions.

    Args:
        plan: What to evaluate.
        corpus_root: Absent or empty directory strictly under the system temp
            dir for the synthetic corpus; the caller owns its removal.

    Returns:
        The quality score, the prompt-bearing record, and the corpus.

    Raises:
        EvalPlanError: When the plan names no candidate, or its declared
            ``judge_kind`` contradicts the judge it carries.
        CorpusConfinementError: When *corpus_root* is not admissible; nothing
            is written.
        EvalCorpusError: When the built corpus cannot serve as a reference.
        JudgeUnavailableError: When a routed judge could not produce a score.
    """
    if not plan.candidates:
        raise EvalPlanError(_NO_CANDIDATES)
    expected_kind = JudgeKind.ROUTED if plan.judge.is_cloud else JudgeKind.HERMETIC
    if plan.judge_kind is not expected_kind:
        raise EvalPlanError(_JUDGE_MISMATCH)
    corpus = build_voice_corpus(corpus_root, entries=plan.entries)
    corpus_health(corpus)
    blinded = blind(plan.candidates, seed=plan.seed)
    requests: list[JudgeRequest] = []
    scores: dict[Dimension, DimensionScore] = {
        score.dimension: score
        for score in (
            score_voice_match(
                blinded,
                reference=corpus.voice_sample,
                judge=plan.judge,
                evidence_present=plan.voice_evidence_present,
                requests=requests,
            ),
            score_instruction_obedience(corpus, blinded, plan.instructions),
            score_grounding_accuracy(corpus, blinded),
            score_refusal_behaviour(blinded),
        )
    }
    quality = QualityScore(
        seed=plan.seed,
        judge=plan.judge_kind,
        candidates=len(plan.candidates),
        per_dimension=scores,
        verdict=rubric_verdict(scores.values()),
    )
    return EvalResult(
        quality=quality,
        record=EvalRecord(
            quality=quality,
            requests=tuple(requests),
            corpus_entries=corpus.entry_count,
        ),
        corpus=corpus,
    )
