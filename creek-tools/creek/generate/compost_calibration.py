"""Recall/precision scoring for the compost detector (FEAT-028).

The compost pipeline is a two-stage filter: an embedding-similarity
gate (:mod:`creek.generate.compost_embedding`) followed by an LLM
verifier (:mod:`creek.generate.compost_verifier`). FEAT-018 shipped the
detector; FEAT-028 closes the calibration loop so the operator can
answer the actual question — *given a labelled fixture, how well is the
pipeline doing?* — and gate CI on regressions.

The scoring path is classifier-agnostic via the same
:class:`SupportsVerifyCompost` protocol the production detector uses,
so tests wire in a deterministic stub and CI never makes live LLM
calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import starmap
from pathlib import Path  # used at runtime as a parameter type
from typing import TYPE_CHECKING

import yaml

from creek.generate.compost_verifier import CompostVerdict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from creek.generate.compost_verifier import SupportsVerifyCompost


_REQUIRED_ENTRY_KEYS: frozenset[str] = frozenset({"id", "title", "body", "expected"})
"""Top-level keys every compost-calibration entry must carry."""

DEFAULT_FIXTURE_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "tests"
    / "fixtures"
    / "compost-calibration.yaml"
)
"""Packaged calibration fixture; matches the classify-calibration convention.

This sits inside the ``tests/`` tree because (like
``classification/calibration_set.yaml``) it ships with the source
distribution but is not part of a wheel install. Operators running from
a wheel must pass ``--fixture`` explicitly.
"""


class CompostFloorBreachError(RuntimeError):
    """Raised when the calibration report falls below a configured floor.

    Mirrors :class:`creek.classify.calibration.CalibrationFloorBreachError`:
    one exception listing every breached metric so a single CI run
    surfaces all breaches at once rather than fixing-and-re-running.
    """


@dataclass(frozen=True)
class CompostCalibrationEntry:
    """One hand-labelled fragment from the compost-calibration fixture.

    Attributes:
        id: Stable per-entry identifier (e.g. ``cc-001``).
        title: Short headline shown to the embedding gate / verifier.
        body: Fragment body shown to the embedding gate / verifier.
        expected: Hand-assigned label — ``True`` means the fragment is
            real compost (the pipeline should flag it), ``False`` means
            it is a false-positive-risk negative (the pipeline should
            *not* flag it).
    """

    id: str
    title: str
    body: str
    expected: bool


@dataclass(frozen=True)
class CompostEntryScore:
    """One scored entry: ground-truth label vs. pipeline decision.

    Attributes:
        entry_id: The fixture entry's ``id``.
        expected: Ground truth.
        similarity: Cosine similarity from the embedding gate.
        embedding_passed: Did the entry clear ``embedding_threshold``?
        verifier_verdict: Verifier output, or ``None`` when the verifier
            was skipped (embedding-only mode, or the entry was filtered
            out before reaching the verifier).
        flagged: Did the pipeline ultimately flag the entry as compost?
            ``True`` for canonical compost OR review-queue verdicts
            (``yes`` / ``ambiguous``); ``False`` otherwise. Mirrors how
            the production :class:`~creek.generate.compost.CompostTracker`
            treats ``ambiguous`` as still surfaced (for_review=True).
        landed_in_review: ``True`` when the verifier returned
            ``ambiguous`` and the entry would route to
            ``10-Liminal/Compost/Review/``.
    """

    entry_id: str
    expected: bool
    similarity: float
    embedding_passed: bool
    verifier_verdict: CompostVerdict | None
    flagged: bool
    landed_in_review: bool


@dataclass(frozen=True)
class CompostCalibrationReport:
    """Confusion matrix + recall/precision/F1/FPR for one calibration run.

    Attributes:
        entries: Total entries scored.
        true_positives: Real compost the pipeline flagged.
        false_positives: Non-compost the pipeline flagged (operator
            noise).
        true_negatives: Non-compost the pipeline correctly ignored.
        false_negatives: Real compost the pipeline missed (silent loss).
        embedding_passed: Count that cleared the embedding gate.
        verified_yes: Count the verifier accepted (``yes``).
        verified_no: Count the verifier rejected (``no``) — saved by
            the second stage from being false positives.
        verified_ambiguous: Count routed to the review queue.
        per_entry: Per-entry breakdown for the JSON sidecar; not
            included in the human-readable table.
    """

    entries: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    embedding_passed: int
    verified_yes: int
    verified_no: int
    verified_ambiguous: int
    per_entry: tuple[CompostEntryScore, ...] = field(default_factory=tuple)

    @property
    def recall(self) -> float:
        """``TP / (TP + FN)``; ``0.0`` when no real positives exist."""
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def precision(self) -> float:
        """``TP / (TP + FP)``; ``0.0`` when nothing was flagged."""
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of recall and precision; ``0.0`` when both are ``0``."""
        if self.recall == 0.0 == self.precision:
            return 0.0
        return 2 * self.recall * self.precision / (self.recall + self.precision)

    @property
    def false_positive_rate(self) -> float:
        """``FP / (FP + TN)``; ``0.0`` when no real negatives exist."""
        denominator = self.false_positives + self.true_negatives
        return self.false_positives / denominator if denominator else 0.0

    def render(self) -> str:
        """Render the report as a plain-text table for the CLI."""
        lines = [
            "Compost calibration",
            "===================",
            f"Entries scored:     {self.entries}",
            "",
            "Confusion matrix",
            "----------------",
            f"  True  positives:  {self.true_positives}",
            f"  False positives:  {self.false_positives}",
            f"  True  negatives:  {self.true_negatives}",
            f"  False negatives:  {self.false_negatives}",
            "",
            "Metrics",
            "-------",
            f"  Recall:           {self.recall:.2%}",
            f"  Precision:        {self.precision:.2%}",
            f"  F1:               {self.f1:.2%}",
            f"  False-pos. rate:  {self.false_positive_rate:.2%}",
            "",
            "Per-stage hits",
            "--------------",
            f"  Embedding gate:   {self.embedding_passed}/{self.entries}",
            f"  Verifier yes:     {self.verified_yes}",
            f"  Verifier no:      {self.verified_no} (saved from FP)",
            f"  Verifier review:  {self.verified_ambiguous}",
        ]
        return "\n".join(lines)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable summary plus per-entry breakdown."""
        return {
            "entries": self.entries,
            "confusion_matrix": {
                "true_positives": self.true_positives,
                "false_positives": self.false_positives,
                "true_negatives": self.true_negatives,
                "false_negatives": self.false_negatives,
            },
            "metrics": {
                "recall": self.recall,
                "precision": self.precision,
                "f1": self.f1,
                "false_positive_rate": self.false_positive_rate,
            },
            "per_stage": {
                "embedding_passed": self.embedding_passed,
                "verified_yes": self.verified_yes,
                "verified_no": self.verified_no,
                "verified_ambiguous": self.verified_ambiguous,
            },
            "per_entry": [
                {
                    "id": entry.entry_id,
                    "expected": entry.expected,
                    "similarity": entry.similarity,
                    "embedding_passed": entry.embedding_passed,
                    "verifier_verdict": (
                        entry.verifier_verdict.value
                        if entry.verifier_verdict is not None
                        else None
                    ),
                    "flagged": entry.flagged,
                    "landed_in_review": entry.landed_in_review,
                }
                for entry in self.per_entry
            ],
        }


def load_fixture(path: Path) -> tuple[CompostCalibrationEntry, ...]:
    """Load and validate the compost-calibration fixture at *path*.

    Args:
        path: YAML file with a top-level list of entries.

    Returns:
        Tuple of :class:`CompostCalibrationEntry` in document order.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the file is not a list of dicts, or when any
            entry is missing a required key or has a non-boolean
            ``expected`` value.
    """
    if not path.exists():
        msg = f"Compost calibration fixture not found: {path}"
        raise FileNotFoundError(msg)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        msg = (
            "compost-calibration fixture must be a YAML list at top level, "
            f"got {type(raw).__name__}"
        )
        raise ValueError(msg)  # noqa: TRY004  # public API contract: tests assert ValueError
    return tuple(starmap(_coerce_entry, enumerate(raw)))


def _coerce_entry(index: int, item: object) -> CompostCalibrationEntry:
    """Validate one parsed fixture entry into a :class:`CompostCalibrationEntry`."""
    if not isinstance(item, dict):
        msg = (
            f"compost-calibration entry #{index} is not a dict: "
            f"got {type(item).__name__}"
        )
        raise ValueError(msg)  # noqa: TRY004  # public API contract: tests assert ValueError
    missing = _REQUIRED_ENTRY_KEYS - {str(k) for k in item}
    if missing:
        msg = f"compost-calibration entry #{index} missing keys: {sorted(missing)}"
        raise ValueError(msg)
    expected = item["expected"]
    if not isinstance(expected, bool):
        msg = (
            f"compost-calibration entry #{index} ('expected') must be a bool, "
            f"got {type(expected).__name__}"
        )
        raise ValueError(msg)  # noqa: TRY004  # public API contract: tests assert ValueError
    return CompostCalibrationEntry(
        id=str(item["id"]),
        title=str(item["title"]),
        body=str(item["body"]),
        expected=bool(expected),
    )


def score_compost(
    entries: Iterable[CompostCalibrationEntry],
    *,
    similarity_fn: Callable[[str], float],
    verifier: SupportsVerifyCompost | None,
    embedding_threshold: float = 0.6,
) -> CompostCalibrationReport:
    """Run the compost pipeline on *entries* and tally recall / precision / F1 / FPR.

    Mirrors the production
    :meth:`creek.generate.compost.CompostTracker._detect_abandonment_fragments`
    decision tree so the calibration matches what the detector actually
    does: embedding gate → optional verifier → flagged (yes /
    ambiguous) or not (below threshold, or verifier ``no``).

    Args:
        entries: Iterable of fixture entries to score.
        similarity_fn: Closure mapping the entry's ``title\\nbody`` text to
            its maximum cosine similarity against the exemplar set.
        verifier: LLM verifier or stub. When ``None``, the embedding
            gate alone determines the flag.
        embedding_threshold: Minimum similarity to advance to the
            verifier (matches
            :class:`~creek.generate.compost.CompostTracker`'s default).

    Returns:
        A populated :class:`CompostCalibrationReport`.
    """
    per_entry = [
        _score_one(
            entry,
            similarity_fn=similarity_fn,
            verifier=verifier,
            embedding_threshold=embedding_threshold,
        )
        for entry in entries
    ]
    return _summarise(per_entry)


def _score_one(
    entry: CompostCalibrationEntry,
    *,
    similarity_fn: Callable[[str], float],
    verifier: SupportsVerifyCompost | None,
    embedding_threshold: float,
) -> CompostEntryScore:
    """Score a single entry through the two-stage pipeline."""
    text = f"{entry.title}\n{entry.body}".strip() if entry.body else entry.title
    similarity = similarity_fn(text)
    embedding_passed = similarity >= embedding_threshold
    if not embedding_passed:
        return CompostEntryScore(
            entry_id=entry.id,
            expected=entry.expected,
            similarity=similarity,
            embedding_passed=False,
            verifier_verdict=None,
            flagged=False,
            landed_in_review=False,
        )
    if verifier is None:
        return CompostEntryScore(
            entry_id=entry.id,
            expected=entry.expected,
            similarity=similarity,
            embedding_passed=True,
            verifier_verdict=None,
            flagged=True,
            landed_in_review=False,
        )
    result = verifier.verify(title=entry.title, body=entry.body)
    flagged = result.verdict in (CompostVerdict.YES, CompostVerdict.AMBIGUOUS)
    return CompostEntryScore(
        entry_id=entry.id,
        expected=entry.expected,
        similarity=similarity,
        embedding_passed=True,
        verifier_verdict=result.verdict,
        flagged=flagged,
        landed_in_review=result.verdict == CompostVerdict.AMBIGUOUS,
    )


_VERDICT_COUNT_KEYS: dict[CompostVerdict, str] = {
    CompostVerdict.YES: "verified_yes",
    CompostVerdict.NO: "verified_no",
    CompostVerdict.AMBIGUOUS: "verified_ambiguous",
}


def _summarise(scores: Sequence[CompostEntryScore]) -> CompostCalibrationReport:
    """Aggregate per-entry scores into the final report in a single pass."""
    counts: dict[str, int] = {
        "true_positives": 0,
        "false_positives": 0,
        "true_negatives": 0,
        "false_negatives": 0,
        "embedding_passed": 0,
        "verified_yes": 0,
        "verified_no": 0,
        "verified_ambiguous": 0,
    }
    for score in scores:
        counts[_confusion_key(score)] += 1
        if score.embedding_passed:
            counts["embedding_passed"] += 1
        if score.verifier_verdict is not None:
            counts[_VERDICT_COUNT_KEYS[score.verifier_verdict]] += 1
    return CompostCalibrationReport(
        entries=len(scores),
        per_entry=tuple(scores),
        **counts,
    )


def _confusion_key(score: CompostEntryScore) -> str:
    """Return the matrix bucket name for *score* (TP / FP / TN / FN)."""
    if score.flagged:
        return "true_positives" if score.expected else "false_positives"
    return "false_negatives" if score.expected else "true_negatives"


def write_json_sidecar(report: CompostCalibrationReport, path: Path) -> None:
    """Write the JSON sidecar to *path*, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def assert_floors(
    report: CompostCalibrationReport,
    *,
    floor_recall: float | None = None,
    floor_precision: float | None = None,
) -> None:
    """Raise :class:`CompostFloorBreachError` if any floor is violated.

    Args:
        report: Output of :func:`score_compost`.
        floor_recall: Minimum acceptable recall in ``[0.0, 1.0]``, or
            ``None`` to skip the recall gate.
        floor_precision: Minimum acceptable precision in ``[0.0, 1.0]``,
            or ``None`` to skip the precision gate.

    Raises:
        CompostFloorBreachError: When at least one floor is breached.
            The message lists every breach so a single CI run surfaces
            all of them.
    """
    breaches: list[str] = []
    if floor_recall is not None and report.recall < floor_recall:
        breaches.append(f"recall: {report.recall:.2%} < {floor_recall:.2%}")
    if floor_precision is not None and report.precision < floor_precision:
        breaches.append(
            f"precision: {report.precision:.2%} < {floor_precision:.2%}",
        )
    if breaches:
        msg = "compost calibration floors breached:\n  " + "\n  ".join(breaches)
        raise CompostFloorBreachError(msg)
