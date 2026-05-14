"""Calibration of LLM classification quality (FEAT-017b).

Runs the configured classifier against a hand-labelled fixture
(``tests/fixtures/classification/calibration_set.yaml`` by default) and
reports per-dimension agreement rates. Downstream of FEAT-017a's
two-step prompt + few-shot pipeline.

The calibration engine is classifier-agnostic via the
:class:`SupportsClassifyWithReasoning` protocol — production use wires
in :class:`creek.classify.llm.LLMClassifier`; tests wire in a
deterministic stub. Locally, ``creek classify --calibrate`` runs the
real LLM against the fixture and prints the per-dimension agreement;
CI tests verify the calibration mechanism end-to-end against a
deterministic stub so the gate fires on regressions in the scoring
code itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import yaml

from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    SourcePlatform,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from creek.classify.llm import LLMClassificationResult

DIMENSIONS: Final[tuple[str, ...]] = (
    "frequency",
    "phase",
    "mode",
    "orientation",
    "dosage",
    "voice_register",
)
"""Dimensions scored by :func:`run_calibration`.

Matches the seven-dimensional classification schema minus
``voice.confidence`` (a meta-dimension carried in the YAML response
but not part of the agreement target — the model's confidence isn't
ground truth).
"""

DEFAULT_FLOORS: Final[dict[str, float]] = {
    "frequency": 0.60,
    "phase": 0.60,
    "mode": 0.40,
    "orientation": 0.40,
    "dosage": 0.40,
    "voice_register": 0.75,
}
"""Per-dimension agreement floors below which CI fails (FEAT-017).

The FEAT calibrated these for the v1.0 prompt against a target model
(Sonnet); a model swap may require a re-baseline. Mode / Orientation /
Dosage are the noisier dimensions and carry the looser 40% floor;
Voice Register is the firmest at 75%.
"""

_REQUIRED_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "title", "body", "expected"},
)
"""Top-level keys every calibration entry must carry."""


class CalibrationFloorBreachError(RuntimeError):
    """Raised when one or more per-dimension agreement floors are violated.

    The message names every breached dimension along with its observed
    rate and the floor it failed against, so an operator scanning a CI
    log can see all breaches at once rather than fixing one and
    re-running.
    """


@dataclass(frozen=True)
class CalibrationEntry:
    """One hand-labelled fragment from the calibration fixture.

    Attributes:
        id: Stable per-entry identifier (e.g. ``cal-001``).
        title: Short headline shown to the classifier.
        body: Representative fragment text shown to the classifier.
        expected: Hand-assigned ground-truth labels keyed by
            dimension name (see :data:`DIMENSIONS`). Missing keys are
            treated as "not scored for this dimension" by
            :func:`run_calibration`.
    """

    id: str
    title: str
    body: str
    expected: dict[str, str]


@dataclass(frozen=True)
class DimensionAgreement:
    """Match / total counts for one classification dimension.

    Attributes:
        dimension: Dimension name from :data:`DIMENSIONS`.
        matches: Count of entries the classifier got right.
        total: Count of entries that had a ground-truth label for
            this dimension. Always ≥ ``matches``.
    """

    dimension: str
    matches: int
    total: int

    @property
    def rate(self) -> float:
        """Agreement rate in ``[0.0, 1.0]``; ``0.0`` when ``total == 0``."""
        return self.matches / self.total if self.total else 0.0


@dataclass(frozen=True)
class CalibrationReport:
    """Per-dimension agreement summary for one calibration run.

    Attributes:
        entries: Number of fixture entries scored.
        agreements: One :class:`DimensionAgreement` per scored
            dimension, in :data:`DIMENSIONS` order.
    """

    entries: int
    agreements: tuple[DimensionAgreement, ...] = ()

    def for_dimension(self, name: str) -> DimensionAgreement:
        """Return the :class:`DimensionAgreement` for *name*.

        Args:
            name: A dimension from :data:`DIMENSIONS`.

        Returns:
            The matching :class:`DimensionAgreement`.

        Raises:
            KeyError: If *name* was not scored in this run.
        """
        for agreement in self.agreements:
            if agreement.dimension == name:
                return agreement
        msg = f"dimension {name!r} not scored in this calibration run"
        raise KeyError(msg)

    def render(self) -> str:
        """Render the report as a plain-text table for the CLI.

        Returns:
            Multi-line string with header + one row per dimension,
            terminated by an ``Entries scored: N`` line.
        """
        lines = ["Dimension       Match Total Rate"]
        for agreement in self.agreements:
            lines.append(
                f"{agreement.dimension:<15} {agreement.matches:>5} "
                f"{agreement.total:>5} {agreement.rate:>5.0%}",
            )
        lines.append(f"Entries scored: {self.entries}")
        return "\n".join(lines)


class SupportsClassifyWithReasoning(Protocol):
    """Subset of :class:`creek.classify.llm.LLMClassifier` calibration needs.

    Lets the calibration engine accept any object that produces an
    :class:`LLMClassificationResult` from a fragment — the real LLM
    classifier in production, a deterministic stub in tests.
    """

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Classify a fragment and return result + reasoning trace."""


def load_fixture(path: Path) -> tuple[CalibrationEntry, ...]:
    """Load and validate the calibration fixture at *path*.

    Args:
        path: Path to a YAML file holding a list of calibration entries.

    Returns:
        Tuple of :class:`CalibrationEntry`, one per fixture record, in
        document order.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the file is not a YAML list of dicts, or when
            any entry is missing a required key.
    """
    raw = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw) or []
    if not isinstance(parsed, list):
        msg = (
            "calibration fixture must be a YAML list at top level, "
            f"got {type(parsed).__name__}"
        )
        raise ValueError(msg)
    return tuple(_coerce_entry(idx, item) for idx, item in enumerate(parsed))


def _coerce_entry(index: int, item: object) -> CalibrationEntry:
    """Validate one parsed fixture entry into :class:`CalibrationEntry`.

    Args:
        index: Zero-based position in the fixture file, used in error
            messages so an operator can locate a malformed entry.
        item: One element from the parsed YAML list.

    Returns:
        Validated :class:`CalibrationEntry`.

    Raises:
        ValueError: When *item* is not a dict, lacks a required key,
            or carries an ``expected`` block that is not itself a dict.
    """
    if not isinstance(item, dict):
        msg = f"calibration entry #{index} is not a dict: got {type(item).__name__}"
        raise ValueError(msg)
    missing = _REQUIRED_ENTRY_KEYS - {str(k) for k in item}
    if missing:
        msg = f"calibration entry #{index} missing keys: {sorted(missing)}"
        raise ValueError(msg)
    expected = item["expected"]
    if not isinstance(expected, dict):
        msg = f"calibration entry #{index} has non-dict 'expected' block"
        raise ValueError(msg)
    return CalibrationEntry(
        id=str(item["id"]),
        title=str(item["title"]),
        body=str(item["body"]),
        expected={str(k): str(v) for k, v in expected.items()},
    )


def _fragment_for(entry: CalibrationEntry) -> Fragment:
    """Build a :class:`Fragment` shaped for the calibration classifier call.

    Uses ``Authorship.SELF`` and ``SourcePlatform.MARKDOWN`` so the
    fixture exercises the same code paths as a user-authored markdown
    note rather than a chatbot transcript (which would carry different
    privacy defaults).
    """
    return Fragment(
        id=entry.id,
        title=entry.title,
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            author=Authorship.SELF,
        ),
    )


def _extract_label(fragment: Fragment, dimension: str) -> str | None:
    """Return the classified label for *dimension* on *fragment*.

    Returns:
        The label as a string when assigned, ``None`` when the
        classifier left this dimension unclassified (the unclassified
        sentinel signals "no opinion" and should not count as
        agreement against any ground-truth label).

    Raises:
        AssertionError: When *dimension* is not one of
            :data:`DIMENSIONS`. Surfaces a missing dispatch branch at
            test time rather than silently reporting 0% agreement for
            a misnamed dimension.
    """
    if dimension == "frequency":
        return _none_if_unclassified(fragment.frequency.primary)
    if dimension == "phase":
        return _none_if_unclassified(fragment.wavelength.phase)
    if dimension == "mode":
        return _none_if_unclassified(fragment.wavelength.mode)
    if dimension == "orientation":
        return _none_if_unclassified(fragment.wavelength.orientation)
    if dimension == "dosage":
        return _none_if_unclassified(fragment.wavelength.dosage)
    if dimension == "voice_register":
        # Asymmetry vs `_none_if_unclassified`: `VoiceRegister` has no
        # `UNCLASSIFIED` enum member — the schema uses `None` to mean
        # "no opinion" (see FEAT-017a's voice classification). So we
        # only need to map None → None; there is no sentinel string to
        # collapse. If a future schema adds VoiceRegister.UNCLASSIFIED,
        # route this through `_none_if_unclassified` instead.
        register = fragment.voice.voice_register
        return None if register is None else str(register)
    msg = f"unhandled dimension: {dimension!r}"
    raise AssertionError(msg)


def _none_if_unclassified(value: object) -> str | None:
    """Coerce *value* to its string form, mapping ``unclassified`` to ``None``."""
    if value is None:
        return None
    text = str(value)
    return None if text == "unclassified" else text


def run_calibration(
    classifier: SupportsClassifyWithReasoning,
    entries: Iterable[CalibrationEntry],
) -> CalibrationReport:
    """Score *classifier* against every entry in *entries*.

    For each entry, invokes
    :meth:`SupportsClassifyWithReasoning.classify_with_reasoning` with
    a freshly-built fragment, then tallies per-dimension agreement
    against the entry's hand-labelled ground truth. Entries that omit
    a dimension from their ``expected`` block are skipped for that
    dimension (counts decrement, not match-as-failure).

    Args:
        classifier: Any object satisfying
            :class:`SupportsClassifyWithReasoning`.
        entries: Calibration entries to score.

    Returns:
        :class:`CalibrationReport` summarising counts and rates per
        dimension.
    """
    materialised = tuple(entries)
    matches = dict.fromkeys(DIMENSIONS, 0)
    totals = dict.fromkeys(DIMENSIONS, 0)
    for entry in materialised:
        result = classifier.classify_with_reasoning(
            _fragment_for(entry),
            content=entry.body,
        )
        for dim in DIMENSIONS:
            expected = entry.expected.get(dim)
            if expected is None:
                continue
            totals[dim] += 1
            actual = _extract_label(result.fragment, dim)
            if actual is not None and actual == expected:
                matches[dim] += 1
    agreements = tuple(
        DimensionAgreement(dimension=dim, matches=matches[dim], total=totals[dim])
        for dim in DIMENSIONS
    )
    return CalibrationReport(entries=len(materialised), agreements=agreements)


def assert_floors(
    report: CalibrationReport,
    floors: dict[str, float] | None = None,
) -> None:
    """Raise :class:`CalibrationFloorBreachError` if any dimension falls below floor.

    Dimensions absent from *floors* are not gated. Dimensions absent
    from *report* are skipped silently — a fixture that doesn't
    exercise a dimension shouldn't fail the gate.

    Args:
        report: Output of :func:`run_calibration`.
        floors: Mapping ``dimension -> minimum-rate``. ``None`` uses
            :data:`DEFAULT_FLOORS`.

    Raises:
        CalibrationFloorBreachError: When at least one dimension is below
            its configured floor. The message lists every breach.
    """
    effective = DEFAULT_FLOORS if floors is None else floors
    breaches: list[str] = []
    for dim, floor in effective.items():
        try:
            agreement = report.for_dimension(dim)
        except KeyError:
            continue
        if agreement.total == 0:
            continue
        if agreement.rate < floor:
            breaches.append(
                f"{dim}: {agreement.rate:.0%} "
                f"({agreement.matches}/{agreement.total}) < {floor:.0%}",
            )
    if breaches:
        msg = "calibration floors breached:\n  " + "\n  ".join(breaches)
        raise CalibrationFloorBreachError(msg)
