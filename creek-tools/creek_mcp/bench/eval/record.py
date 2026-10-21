"""The eval's own artifact: the prompts and raw scores, kept out of the report.

#1875 asks for the prompts and scores to be recorded.
:class:`~creek_mcp.bench.report.BenchReport` cannot carry them: every string in
it is closed by construction, and ``tests/bench/test_report_schema_invariant.py``
fails on any free-text field anywhere in its tree. Weakening that to satisfy a
reporting requirement would trade a privacy invariant for a convenience.

So there are two artifacts. The *score* rides in the report — enums, numbers
and a semver. The *prompts* ride here, in a file the eval writes itself, which
is honest about being prompt-bearing. Everything in it is synthetic by
construction: the reference is
:data:`~creek_mcp.bench.eval.voice_corpus.VOICE_ENTRIES`, written for this
harness, and the candidates are supplied by a caller who was handed nothing
else. The record never carries a model identity — the payloads it holds are the
blinded ones the judge saw.

:func:`write_record` is the only serializer and :func:`load_record` the only
reader, mirroring ``write_report`` / ``load_report`` in the capacity harness.
The reader validates the score through pydantic rather than trusting the file,
so a hand-edited artifact is refused instead of being read back as evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from creek_mcp.bench.eval.judge import JudgeRequest
from creek_mcp.bench.eval.rubric import Dimension, QualityScore

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class EvalRecord:
    """One eval run's reproducible, prompt-bearing artifact.

    Attributes:
        quality: The same score that goes into the benchmark report.
        requests: Every payload handed to the judge, in call order — blinded,
            so no model identity appears.
        corpus_entries: How many synthetic entries the run scored against.
    """

    quality: QualityScore
    requests: tuple[JudgeRequest, ...]
    corpus_entries: int

    def to_json_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form, seed and rubric version first.

        Both are at the top level because a score is comparable only within one
        rubric version and one seed, and a reader should not have to dig for
        either.
        """
        return {
            "kind": self.quality.kind,
            "rubric_version": self.quality.rubric_version,
            "seed": self.quality.seed,
            "judge": self.quality.judge.value,
            "candidates": self.quality.candidates,
            "corpus_entries": self.corpus_entries,
            "verdict": self.quality.verdict.value,
            "per_dimension": {
                dimension.value: {
                    "basis": score.basis.value,
                    "score": score.score,
                    "probes": score.probes,
                }
                for dimension, score in self.quality.per_dimension.items()
            },
            "requests": [
                {
                    "candidate_id": request.candidate_id,
                    "dimension": request.dimension.value,
                    "rubric_version": request.rubric_version,
                    "candidate_text": request.candidate_text,
                    "reference_text": request.reference_text,
                }
                for request in self.requests
            ],
        }


def write_record(record: EvalRecord, path: Path) -> None:
    """Serialize *record* as JSON to *path* — the eval's only serializer."""
    path.write_text(
        json.dumps(record.to_json_dict(), indent=2) + "\n", encoding="utf-8"
    )


def _quality_from(payload: dict[str, Any]) -> QualityScore:
    """Rebuild and fully validate the quality score from a read artifact."""
    return QualityScore.model_validate(
        {
            "kind": payload["kind"],
            "rubric_version": payload["rubric_version"],
            "seed": payload["seed"],
            "judge": payload["judge"],
            "candidates": payload["candidates"],
            "verdict": payload["verdict"],
            "per_dimension": {
                dimension: {"dimension": dimension} | score
                for dimension, score in payload["per_dimension"].items()
            },
        }
    )


def load_record(path: Path) -> EvalRecord:
    """Read an artifact written by :func:`write_record`, validating it fully.

    The score goes back through
    :class:`~creek_mcp.bench.eval.rubric.QualityScore`, so an out-of-range
    number, an unknown dimension or a missing field is a refusal rather than a
    value a reader would mistake for a measurement.

    Args:
        path: The artifact to read.

    Returns:
        The reconstructed record.

    Raises:
        ValueError: When the artifact is not a valid record. Pydantic's
            ``ValidationError`` is a ``ValueError``, so a hand-edited score
            arrives here rather than downstream.
        KeyError: When a top-level field is absent entirely.
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return EvalRecord(
        quality=_quality_from(payload),
        requests=tuple(
            JudgeRequest(
                candidate_id=request["candidate_id"],
                dimension=Dimension(request["dimension"]),
                rubric_version=request["rubric_version"],
                candidate_text=request["candidate_text"],
                reference_text=request["reference_text"],
            )
            for request in payload["requests"]
        ),
        corpus_entries=payload["corpus_entries"],
    )
