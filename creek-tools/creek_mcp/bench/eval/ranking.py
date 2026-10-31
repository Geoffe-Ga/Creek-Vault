"""The ranked artifact ``bench eval`` writes: one row per model, and its caveats.

Each row is one model's own :func:`~creek_mcp.bench.eval.runner.run_eval`
result — the four dimension scores, each with its basis, and the rubric
verdict — plus the count of citations the model invented and that run's
prompt-bearing :class:`~creek_mcp.bench.eval.record.EvalRecord`.

**Ranking is not a weighted score.** The rubric deliberately never collapses
the four dimensions into one figure, so neither does the order. Rows are
ranked by :data:`RANKING_BASIS`: verdict first, then how many dimensions
cleared the pass line, then the *weakest* discriminating score (a model is as
good as its worst dimension, which is the rubric's own logic), then the model
tag so a tie never depends on the order models were named in.

**A column that looks like evidence and is not is worse than a missing
column.** Refusal behaviour is derived from Creek's guards, not from the
model (see :mod:`creek_mcp.bench.eval.candidates`), so it scores the same for
every candidate. :data:`NON_DISCRIMINATING` records that *in the artifact*, as
a field, and the ranking key excludes it.

The artifact is deterministic: no timestamp, no path, keys in a fixed order,
so two runs at one seed write byte-identical files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.bench.eval import PASS_THRESHOLD, RUBRIC_VERSION
from creek_mcp.bench.eval.rubric import Basis, Dimension, RubricVerdict

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from creek_mcp.bench.eval.record import EvalRecord
    from creek_mcp.bench.eval.rubric import JudgeKind, QualityScore

RANKING_KIND: Final[str] = "voice_fidelity_ranking"
"""The artifact's ``kind``, distinguishing it from a bare eval record."""

RANKING_BASIS: Final[str] = (
    "verdict (passes, insufficient_evidence, fails); then dimensions at or "
    "above the pass threshold, most first; then the weakest discriminating "
    "score, highest first; then model tag"
)
"""How :func:`rank_rows` orders rows, stated in the artifact."""

_VERDICT_ORDER: Final[dict[RubricVerdict, int]] = {
    RubricVerdict.PASSES: 0,
    RubricVerdict.INSUFFICIENT_EVIDENCE: 1,
    RubricVerdict.FAILS: 2,
}


class EvalMode(StrEnum):
    """Where candidate text came from."""

    FAKE = "fake"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class NonDiscriminatingDimension:
    """A dimension that is scored but cannot tell two models apart.

    Attributes:
        dimension: Which dimension.
        source: Where its evidence comes from.
        reason: Why it cannot discriminate between models.
    """

    dimension: Dimension
    source: str
    reason: str

    def to_json_dict(self) -> dict[str, Any]:
        """Return the JSON form, with ``discriminates_between_models: false``."""
        return {
            "dimension": self.dimension.value,
            "discriminates_between_models": False,
            "source": self.source,
            "reason": self.reason,
        }


NON_DISCRIMINATING: Final[tuple[NonDiscriminatingDimension, ...]] = (
    NonDiscriminatingDimension(
        dimension=Dimension.REFUSAL_BEHAVIOUR,
        source="pipeline_guards",
        reason=(
            "Refusals are recorded by running Creek's acute-distress guard and "
            "tier ceiling, which refuse before any model is asked; the probes "
            "are never sent to a model. Every candidate therefore scores the "
            "same, and this column is not evidence about the model."
        ),
    ),
)
"""Dimensions the artifact flags as non-discriminating, and why."""

_EXCLUDED: Final[frozenset[Dimension]] = frozenset(
    caveat.dimension for caveat in NON_DISCRIMINATING
)


@dataclass(frozen=True, slots=True)
class ModelRow:
    """One model's result in the ranking.

    Attributes:
        model_tag: Which model; harness-side, attached after judging.
        quality: That model's closed score.
        record: That model's prompt-bearing record.
        invented_citations: Citations the model made that the corpus does not
            contain.
    """

    model_tag: str
    quality: QualityScore
    record: EvalRecord
    invented_citations: int

    def to_json_dict(self, rank: int) -> dict[str, Any]:
        """Return this row's JSON form at *rank*."""
        return {
            "rank": rank,
            "model_tag": self.model_tag,
            "verdict": self.quality.verdict.value,
            "dimensions": {
                dimension.value: {
                    "basis": score.basis.value,
                    "score": score.score,
                    "probes": score.probes,
                }
                for dimension, score in self.quality.per_dimension.items()
            },
            "invented_citations": self.invented_citations,
            "record": self.record.to_json_dict(),
        }


def _rank_key(row: ModelRow) -> tuple[int, int, float, str]:
    """Return *row*'s sort key; see :data:`RANKING_BASIS`."""
    scores = row.quality.per_dimension
    passing = sum(
        1
        for score in scores.values()
        if score.basis is not Basis.UNSUPPORTED and score.score >= PASS_THRESHOLD
    )
    weakest = min(
        (
            score.score
            for dimension, score in scores.items()
            if dimension not in _EXCLUDED and score.basis is not Basis.UNSUPPORTED
        ),
        default=0.0,
    )
    return (_VERDICT_ORDER[row.quality.verdict], -passing, -weakest, row.model_tag)


def rank_rows(rows: Iterable[ModelRow]) -> tuple[ModelRow, ...]:
    """Return *rows* ordered by :data:`RANKING_BASIS`.

    Args:
        rows: One row per model, in any order.

    Returns:
        The rows, best first.
    """
    return tuple(sorted(rows, key=_rank_key))


@dataclass(frozen=True, slots=True)
class EvalRanking:
    """One ``bench eval`` run's ranked artifact.

    Attributes:
        mode: Whether candidates came from a fake or a live source.
        judge: Which judge scored voice match — ``routed`` whenever a cloud
            judge was used, never ``hermetic``.
        seed: The presentation seed every model was scored at.
        corpus_entries: How many synthetic entries were used.
        generation_prompt: The exact prompt every model was given.
        rows: The ranked rows, best first.
    """

    mode: EvalMode
    judge: JudgeKind
    seed: int
    corpus_entries: int
    generation_prompt: str
    rows: tuple[ModelRow, ...]

    def to_json_dict(self) -> dict[str, Any]:
        """Return the JSON form, run facts first and rows last."""
        return {
            "kind": RANKING_KIND,
            "rubric_version": RUBRIC_VERSION,
            "mode": self.mode.value,
            "judge": self.judge.value,
            "seed": self.seed,
            "corpus_entries": self.corpus_entries,
            "ranking_basis": RANKING_BASIS,
            "non_discriminating_dimensions": [
                caveat.to_json_dict() for caveat in NON_DISCRIMINATING
            ],
            "generation_prompt": self.generation_prompt,
            "rows": [
                row.to_json_dict(rank) for rank, row in enumerate(self.rows, start=1)
            ],
        }


def write_ranking(ranking: EvalRanking, path: Path) -> None:
    """Serialize *ranking* as JSON to *path* — the ranking's only serializer."""
    path.write_text(
        json.dumps(ranking.to_json_dict(), indent=2) + "\n", encoding="utf-8"
    )
