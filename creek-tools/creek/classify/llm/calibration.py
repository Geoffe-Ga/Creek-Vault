"""FEAT-017 confidence-bias gating for LLM classification.

Hosts the per-dimension confidence threshold logic that downgrades
Mode / Orientation / Dosage to ``unclassified`` when the model's
self-reported confidence falls below ``LLMConfig.unclassified_threshold``.

Frequency, Phase, and Voice Register are not biased — they are more
stable signals empirically, and gating them would be more cost than
benefit. Keeping the bias here (rather than in :mod:`parsing`) lets
each module stay focused on one concern: parsing handles raw
deserialisation, this module handles the FEAT-017 calibration.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from creek.classify.evidence import layer_determined_over
from creek.classify.llm.parsing import (
    _parse_descriptor,
    _parse_dosage,
    _parse_enum,
)
from creek.models import (
    Color,
    Dosage,
    Mode,
    Orientation,
    Phase,
    WavelengthClassification,
)

_EnumT = TypeVar("_EnumT", bound=StrEnum)

_BIASED_DIMENSIONS: frozenset[str] = frozenset({"mode", "orientation", "dosage"})
"""Dimensions gated by ``LLMConfig.unclassified_threshold`` (FEAT-017).

Frequency, Phase, and Voice Register are not biased because they are
more stable signals empirically; using them is the point of having an
LLM pass at all.

**What the downgrade means, settled by issue #1421.** The gate has two
possible readings once the wavelength block stopped being rebuilt
wholesale: "do not adopt the noisy pick" or "erase what we knew". This
module means the first. :func:`_biased_enum`'s own contract is that a
sub-threshold confidence "overrides the model's *pick* with ``default``"
— it speaks about the verdict this pass produces, not about the record
the fragment already carries. Since ``default`` is always the enum's
``unclassified`` member, and that member is the sentinel
:func:`~creek.classify.evidence.layer_determined_over` drops from a
sparse verdict, layering delivers that reading with no extra code: the
noisy pick never reaches the fragment, and a mode an earlier run
established stands untouched. Keeping "erase" would now require
deliberately writing the sentinel back, and nothing justifies spending a
low-confidence *guess* to destroy evidence gathered elsewhere.
"""


def _confidence_score(scores: object, dimension: str) -> float | None:
    """Return the per-dimension confidence value from a ``confidence_scores`` map.

    Args:
        scores: The ``confidence_scores`` value from the parsed YAML
            (any type — the LLM may emit garbage).
        dimension: ``"mode"``, ``"orientation"``, or ``"dosage"``.

    Returns:
        The float in ``[0.0, 1.0]`` when reported and parseable, else
        ``None`` (treated by the bias as "no score reported — keep
        the model's pick").
    """
    if not isinstance(scores, dict):
        return None
    value = scores.get(dimension)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _biased_enum(
    value: object,
    enum_type: type[_EnumT],
    default: _EnumT,
    *,
    score: float | None,
    threshold: float,
) -> _EnumT:
    """Apply :data:`_BIASED_DIMENSIONS` gating to a regular enum parse.

    A reported confidence strictly below the threshold overrides the
    model's pick with ``default`` (always the ``unclassified`` member
    of the enum). When ``score`` is ``None`` (no value reported), the
    bias does not fire — the model's pick stands.
    """
    if score is not None and score < threshold:
        return default
    return _parse_enum(value, enum_type, default)


def _biased_dosage(
    value: object,
    *,
    score: float | None,
    threshold: float,
) -> Dosage:
    """Apply :data:`_BIASED_DIMENSIONS` gating to dosage parsing.

    A separate helper because dosage uses :func:`_parse_dosage` (which
    treats ambiguity markers specially) rather than the plain
    :func:`_parse_enum` used by mode/orientation.
    """
    if score is not None and score < threshold:
        return Dosage.UNCLASSIFIED
    return _parse_dosage(value)


def _apply_wavelength(
    data: dict[str, object],
    updates: dict[str, object],
    current: WavelengthClassification,
    *,
    unclassified_threshold: float,
) -> None:
    """Layer the response's wavelength verdict over the recorded one (#1421).

    Mode, Orientation, and Dosage are downgraded to ``unclassified``
    when the model's self-reported per-dimension confidence (read from
    ``data['confidence_scores'][dimension]``) is below
    ``unclassified_threshold``. A missing or unparseable score leaves
    the model's pick intact — the bias only fires when the model
    explicitly reports low confidence.

    Phase, Color, and Descriptor are **not** gated by the bias.
    FEAT-017 calls phase a stable signal (gating it would be more
    cost than benefit); issue #319 added color and descriptor to the
    extracted set so the wavelength block reaches disk complete.
    Color tracks the primary frequency (a stable signal) and
    descriptor is a free-form label, not a categorical pick — neither
    benefits from the confidence-bias gating that protects the noisy
    mode/orientation/dosage axes.

    The verdict this builds is *sparse* — every axis the response was
    silent about, and every axis the FEAT-017 gate downgraded, carries
    the model's ``unclassified`` / ``""`` sentinel — so it is layered
    onto *current* through
    :func:`~creek.classify.evidence.layer_determined_over` rather than
    assigned. Before #1421 it was assigned, which meant a response
    naming only ``phase`` blanked the other five axes and a single
    low-confidence score blanked the recorded mode, orientation and
    dosage as well. See :data:`_BIASED_DIMENSIONS` for why the gate's
    sentinel is now read as "do not adopt", not "erase".

    One deliberate cost: ``descriptor`` uses ``""`` both as its "not
    determined" sentinel and as its empty value, so a response can no
    longer clear a recorded descriptor by sending an empty one. Clearing
    is an operator edit to the frontmatter, not something a model
    response is entitled to do.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with the merged wavelength block.
        current: The wavelength classification already on the fragment.
            Only the axes this response actually decided are overlaid
            on it. Never mutated.
        unclassified_threshold: Minimum confidence to keep the model's
            pick for Mode / Orientation / Dosage; below this the
            dimension is not adopted.
    """
    wave_data = data.get("wavelength")
    if not isinstance(wave_data, dict):
        return
    scores = data.get("confidence_scores")
    determined = WavelengthClassification(
        phase=_parse_enum(wave_data.get("phase"), Phase, Phase.UNCLASSIFIED),
        mode=_biased_enum(
            wave_data.get("mode"),
            Mode,
            Mode.UNCLASSIFIED,
            score=_confidence_score(scores, "mode"),
            threshold=unclassified_threshold,
        ),
        orientation=_biased_enum(
            wave_data.get("orientation"),
            Orientation,
            Orientation.UNCLASSIFIED,
            score=_confidence_score(scores, "orientation"),
            threshold=unclassified_threshold,
        ),
        dosage=_biased_dosage(
            wave_data.get("dosage"),
            score=_confidence_score(scores, "dosage"),
            threshold=unclassified_threshold,
        ),
        # Issue #319: color + descriptor were silently dropped pre-fix,
        # so every classified fragment landed with
        # ``wavelength.color: unclassified`` and
        # ``wavelength.descriptor: ''``. Neither is gated by the FEAT-017
        # confidence bias — color tracks frequency (a stable signal) and
        # descriptor is a free-form label, not a categorical pick.
        color=_parse_enum(
            wave_data.get("color"),
            Color,
            Color.UNCLASSIFIED,
        ),
        descriptor=_parse_descriptor(wave_data.get("descriptor")),
    )
    updates["wavelength"] = layer_determined_over(
        prior=current,
        determined=determined,
    )
