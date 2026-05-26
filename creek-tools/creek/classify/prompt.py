"""Prompt-level ontology detection (issue #350).

The fragment classifier in :mod:`creek.classify` answers
*what ontology dimensions does this **fragment** sit on?* Each
dimension yields exactly one canonical pick (e.g. ``frequency.primary
= F3``). That shape fits the curated-corpus side of the pipeline,
where every fragment has been atomised and is short enough that one
pick is honest.

When :mod:`creek.generate.drafts` accepts a free-form essay prompt
(``--seed-topic``, or one of the new modes proposed by epic #349) the
same single-pick approach is wrong on two counts: a prompt spans
multiple dimensions, and the composition step needs the weighting to
decide how heavily to lean on each per-dimension source slice. The
AND-intersection that today's filters apply on the corpus side empties
out — see the load-bearing #351 sub-issue.

This module produces the structured equivalent of a fragment
classification, applied to a prompt-as-input, with **weights** in
``[0.0, 1.0]`` per detected value:

* :class:`PromptOntology` — frozen dataclass mirroring the fragment
  dimensions, but each dimension is a tuple of
  :class:`WeightedDimension` entries sorted by weight descending.
* :func:`detect_ontology` — entry point that takes a prompt + config
  and returns a :class:`PromptOntology`. Dispatches through
  :class:`creek.classify.llm.LLMClassifier` so the configured provider
  (Ollama or Anthropic) is respected.
* :func:`build_prompt_ontology_prompt` /
  :func:`parse_prompt_ontology_response` — exposed so downstream
  composition code (#351, #352) can re-use the prompt template and
  parser without going through the LLM dispatch when a cached
  response is already in hand.

The frequency / colour / engagement blocks are pulled from the
canonical sources in :mod:`creek.classify.llm.prompts` so a rename or
re-gloss in one place reaches both detectors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

import yaml

from creek.classify.llm.parsing import _split_reasoning_and_yaml, _strip_code_fences
from creek.classify.llm.prompts import (
    _COLOR_BLOCK,
    _FREQUENCY_BLOCK,
    _FREQUENCY_COLOR_BLOCK,
    _sanitise_for_prompt,
)
from creek.models import (
    Dosage,
    Frequency,
    Mode,
    Orientation,
    Phase,
    VoiceRegister,
)

if TYPE_CHECKING:
    from creek.config import LLMConfig

logger = logging.getLogger(__name__)


_DimT = TypeVar("_DimT", bound=StrEnum)


@dataclass(frozen=True)
class WeightedDimension(Generic[_DimT]):
    """A single ontology-dimension value paired with a detection weight.

    Used inside :class:`PromptOntology` to model "this prompt
    activates Phase ``rising`` at strength 0.7 and Phase
    ``bottoming_out`` at strength 0.4." Weights live in
    ``[0.0, 1.0]``; the parser clamps any out-of-range value so a
    misbehaving LLM cannot inject negative or super-unit weights into
    downstream consumers.

    Attributes:
        value: The dimension's canonical enum member.
        weight: Detection strength in ``[0.0, 1.0]``.
    """

    value: _DimT
    weight: float


@dataclass(frozen=True)
class PromptOntology:
    """Weighted ontology profile detected from a free-form prompt.

    Mirrors the dimensions of :class:`creek.models.Fragment` but with
    each dimension carrying a tuple of weighted picks rather than a
    single canonical value. Sorted weight-descending within a
    dimension so the heaviest signal is at index 0.

    Attributes:
        prompt: The (possibly sanitised) prompt the detection ran on.
        frequencies: Weighted APTITUDE F-codes the prompt activates.
        phases: Weighted Archetypal Wavelength phases.
        modes: Weighted engagement modes.
        orientations: Weighted ``do`` / ``feel`` / ``do_feel`` picks.
        dosages: Weighted ``medicine`` / ``toxic`` / ``ambiguous`` picks.
        voice_registers: Weighted voice-register picks.
        overall_confidence: Detector-self-reported confidence in
            ``[0.0, 1.0]``; ``0.0`` when the provider was unreachable
            or the LLM declined to score itself.
        reasoning: The reasoning preamble emitted by the LLM ahead of
            the YAML payload. Empty when the call short-circuited or
            the model returned pure YAML.
    """

    prompt: str
    frequencies: tuple[WeightedDimension[Frequency], ...] = field(default=())
    phases: tuple[WeightedDimension[Phase], ...] = field(default=())
    modes: tuple[WeightedDimension[Mode], ...] = field(default=())
    orientations: tuple[WeightedDimension[Orientation], ...] = field(default=())
    dosages: tuple[WeightedDimension[Dosage], ...] = field(default=())
    voice_registers: tuple[WeightedDimension[VoiceRegister], ...] = field(default=())
    overall_confidence: float = 0.0
    reasoning: str = ""


PROMPT_ONTOLOGY_TEMPLATE: str = (
    """\
You are an ontology-detection assistant for the Creek knowledge organisation
system. A writer has supplied a free-form prompt (a sentence, paragraph, or
outline) that they want to draft an essay from. Your job is to infer how
this prompt distributes across the same classification dimensions Creek
uses for fragments — but **with weights** in [0.0, 1.0] rather than a
single canonical pick per dimension. A prompt that legitimately spans
multiple frequencies, multiple phases, or multiple registers should
emit multiple weighted entries.

DIMENSIONS:

1. **Frequencies** (APTITUDE F1-F10):
__FREQUENCY_BLOCK__

2. **Wavelength Phase**: rising, peaking, withdrawal, diminishing, \
bottoming_out, restoration

3. **Engagement Mode**: inhabit, express, collaborate, integrate, absorb

4. **Orientation**: do, feel, do_feel

5. **Dosage**: medicine, toxic, ambiguous

6. **Voice Register**: confessional, analytical, playful, prophetic, \
instructional, raw, conversational

Wavelength Color (Spiral Dynamics) is anchored to the primary
frequency for downstream visualisation only — you do not need to
score it here. Canonical vocabulary, for reference: __COLOR_BLOCK__.
The canonical frequency→color mapping is:
__FREQUENCY_COLOR_BLOCK__

PROTOCOL:

Step 1 — Reason briefly. Walk through which frequencies the prompt
activates and why, then phases, modes, orientation, dosage, register.
Two to four sentences.

Step 2 — Emit your detection as YAML inside a fenced ```yaml ... ```
block. Only list dimension values you genuinely detect; omit entries
that would have weight < 0.05 rather than padding the YAML. Use this
exact schema (no extra top-level keys):

```yaml
frequencies:
  - value: F3
    weight: 0.8
  - value: F5
    weight: 0.4
phases:
  - value: rising
    weight: 0.6
modes:
  - value: express
    weight: 0.5
orientations:
  - value: do_feel
    weight: 0.7
dosages:
  - value: toxic
    weight: 0.6
  - value: medicine
    weight: 0.3
voice_registers:
  - value: analytical
    weight: 0.5
overall_confidence: 0.7
```

``overall_confidence`` is your honest self-rating of the whole
detection. Calibrate to the FEAT-017 threshold of {threshold:.2f} —
above it means "I would stand behind this detection", below means
"I would defer to a human." Per-dimension weights below this
threshold will be ignored by downstream composition by default.

PROMPT:
{prompt}
""".replace("__FREQUENCY_BLOCK__", _FREQUENCY_BLOCK)
    .replace("__COLOR_BLOCK__", _COLOR_BLOCK)
    .replace("__FREQUENCY_COLOR_BLOCK__", _FREQUENCY_COLOR_BLOCK)
)
"""LLM prompt template for prompt-level ontology detection.

Placeholders:

- ``{threshold}``: ``LLMConfig.unclassified_threshold`` shown to the
  model so it calibrates its self-reported overall confidence
  honestly.
- ``{prompt}``: the operator's prompt, after
  :func:`_sanitise_for_prompt` neutralises injection vectors.

The Frequency / Colour / Frequency→Colour blocks are pulled directly
from :mod:`creek.classify.llm.prompts` to honour the
single-source-of-truth principle (issue #350 acceptance criterion).
Re-naming a frequency or re-mapping a colour in the canonical prompt
reaches this template automatically.
"""


_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "frequencies",
        "phases",
        "modes",
        "orientations",
        "dosages",
        "voice_registers",
        "overall_confidence",
    },
)


def build_prompt_ontology_prompt(
    prompt: str,
    *,
    unclassified_threshold: float,
) -> str:
    """Build the LLM prompt for detecting ontology on a free-form essay seed.

    The operator-supplied prompt is passed through
    :func:`creek.classify.llm.prompts._sanitise_for_prompt` so YAML
    document separators and HTML comments cannot smuggle a second
    "response" into the model's context window — see SEC-004 for the
    underlying threat model. The threshold value is substituted into
    the template so the LLM calibrates its self-reported confidence
    against the same FEAT-017 floor the fragment classifier uses.

    Args:
        prompt: Raw operator-supplied essay seed.
        unclassified_threshold: ``LLMConfig.unclassified_threshold``
            value; shown to the model verbatim with two decimals.

    Returns:
        The fully-formatted prompt string, ready to send to a provider.
    """
    safe = _sanitise_for_prompt(prompt)
    return PROMPT_ONTOLOGY_TEMPLATE.format(
        threshold=unclassified_threshold,
        prompt=safe,
    )


def _coerce_weight(value: object) -> float:
    """Coerce a YAML-loaded weight value into ``[0.0, 1.0]``.

    A non-numeric value is treated as zero rather than raising — the
    parser is intentionally lenient on per-entry weights so one bad
    entry cannot drop the whole detection. The clamp keeps downstream
    composition arithmetic stable (negative weights would invert
    rankings; weights > 1 would amplify a single dimension over the
    others without bound).

    Args:
        value: Raw YAML scalar; expected to be a float-like number.

    Returns:
        A float in ``[0.0, 1.0]``.
    """
    if isinstance(value, bool):
        return 0.0
    if not isinstance(value, (int, float)):
        return 0.0
    weight = float(value)
    if weight < 0.0:
        return 0.0
    if weight > 1.0:
        return 1.0
    return weight


def _parse_enum_value(value: object, enum_type: type[_DimT]) -> _DimT | None:
    """Resolve a YAML scalar to a :class:`StrEnum` member, or ``None``.

    Used by :func:`_parse_dimension` to skip unknown values silently
    rather than raise — the LLM may emit a value that doesn't map to
    a canonical enum member (a typo, an outdated alias), and the
    detector's job is to return what it can rather than abort.

    Args:
        value: Raw YAML scalar.
        enum_type: The target :class:`StrEnum` subclass.

    Returns:
        The matching enum member, or ``None`` if no match exists.
    """
    if value is None:
        return None
    val_str = str(value).lower().strip()
    if not val_str:
        return None
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return None


def _parse_dimension(
    data: dict[str, object],
    key: str,
    enum_type: type[_DimT],
) -> tuple[WeightedDimension[_DimT], ...]:
    """Parse a single dimension's list of ``{value, weight}`` entries.

    Tolerates the common failure modes the LLM produces in practice:

    * the dimension key is missing entirely (return empty tuple);
    * the dimension's value is not a list (return empty tuple);
    * an individual entry lacks a ``value`` field or has an unknown
      value (drop the entry);
    * an individual entry lacks a ``weight`` field (admit at zero so
      the caller still sees the model picked it).

    Entries are returned sorted by weight descending so the heaviest
    signal sits at index 0; ties are stable on enum declaration order.

    Args:
        data: Parsed top-level YAML dict.
        key: Top-level key for this dimension (``"frequencies"`` etc.).
        enum_type: Target :class:`StrEnum` subclass for the dimension.

    Returns:
        Tuple of weighted entries, sorted by weight descending.
    """
    raw = data.get(key)
    if not isinstance(raw, list):
        return ()
    parsed: list[WeightedDimension[_DimT]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        enum_value = _parse_enum_value(entry.get("value"), enum_type)
        if enum_value is None:
            continue
        weight = _coerce_weight(entry.get("weight"))
        parsed.append(WeightedDimension(value=enum_value, weight=weight))
    parsed.sort(key=lambda wd: wd.weight, reverse=True)
    return tuple(parsed)


def _load_yaml_dict(yaml_text: str) -> dict[str, object]:
    """Parse the YAML payload and assert the documented shape.

    Mirrors :func:`creek.classify.llm.parsing.validate_response` but
    against the prompt-ontology schema. Strips markdown fences first
    so a model that nests its YAML inside ```yaml ... ``` round-trips
    correctly. Rejects multi-document payloads (a classic
    LLM-output-spoofing vector) and any top-level keys outside
    :data:`_ALLOWED_TOP_LEVEL_KEYS`.

    Args:
        yaml_text: The YAML body extracted from the LLM response.

    Returns:
        The parsed dict.

    Raises:
        ValueError: On multi-document payloads, non-dict roots, or
            unexpected top-level keys.
    """
    text = _strip_code_fences(yaml_text)
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        msg = f"Invalid YAML in prompt-ontology response: {exc}"
        raise ValueError(msg) from exc
    if len(docs) > 1:
        msg = f"multi-document YAML response rejected ({len(docs)} documents)"
        raise ValueError(msg)
    parsed: object = docs[0] if docs else None
    if not isinstance(parsed, dict):
        msg = f"Expected YAML dict, got {type(parsed).__name__}"
        raise ValueError(msg)  # noqa: TRY004
    keys = {str(k) for k in parsed}
    extras = keys - _ALLOWED_TOP_LEVEL_KEYS
    if extras:
        msg = f"unexpected top-level keys in prompt-ontology response: {sorted(extras)}"
        raise ValueError(msg)
    return {str(k): v for k, v in parsed.items()}


def parse_prompt_ontology_response(
    response: str,
    *,
    prompt: str,
) -> PromptOntology:
    """Parse an LLM response into a :class:`PromptOntology`.

    Splits reasoning from YAML using the same helper the fragment
    classifier uses, then dispatches to per-dimension parsers. The
    ``prompt`` argument is echoed onto the returned dataclass so
    downstream code can persist the operator's seed alongside the
    detection result without an extra plumbing argument.

    Args:
        response: Raw LLM response text.
        prompt: The original prompt, echoed onto the returned
            :class:`PromptOntology` for downstream provenance.

    Returns:
        The parsed :class:`PromptOntology`.

    Raises:
        ValueError: When the YAML payload is malformed, multi-document,
            or contains unexpected top-level keys.
    """
    reasoning, yaml_text = _split_reasoning_and_yaml(response)
    data = _load_yaml_dict(yaml_text)
    return PromptOntology(
        prompt=prompt,
        frequencies=_parse_dimension(data, "frequencies", Frequency),
        phases=_parse_dimension(data, "phases", Phase),
        modes=_parse_dimension(data, "modes", Mode),
        orientations=_parse_dimension(data, "orientations", Orientation),
        dosages=_parse_dimension(data, "dosages", Dosage),
        voice_registers=_parse_dimension(data, "voice_registers", VoiceRegister),
        overall_confidence=_coerce_weight(data.get("overall_confidence")),
        reasoning=reasoning,
    )


def detect_ontology(prompt: str, config: LLMConfig) -> PromptOntology:
    """Infer the weighted ontology profile of a free-form essay prompt.

    Builds the prompt template, dispatches it through an
    :class:`~creek.classify.llm.LLMClassifier` (which picks Ollama or
    Anthropic per ``config.provider``), and parses the YAML response
    into a :class:`PromptOntology`. Short-circuits to an empty
    :class:`PromptOntology` when the prompt is whitespace-only, when
    the provider is unavailable, or when the call raises — the caller
    can then decide whether to abort or to proceed without ontology
    guidance.

    Args:
        prompt: Free-form operator-supplied seed (sentence, paragraph,
            or outline).
        config: LLM provider configuration (provider, model, threshold).

    Returns:
        The detected :class:`PromptOntology`; empty (all-zeros) when
        detection could not run.
    """
    if not prompt.strip():
        return PromptOntology(prompt=prompt)

    from creek.classify.llm import LLMClassifier

    classifier = LLMClassifier(config)
    if not classifier.available:
        logger.warning(
            "LLM provider %r unavailable — returning empty PromptOntology",
            config.provider,
        )
        return PromptOntology(prompt=prompt)

    built = build_prompt_ontology_prompt(
        prompt,
        unclassified_threshold=config.unclassified_threshold,
    )
    try:
        response = classifier.invoke_prompt(built)
        parsed = parse_prompt_ontology_response(response, prompt=prompt)
    except (RuntimeError, OSError, ValueError) as exc:
        # ValueError covers malformed YAML and schema violations from
        # the parser; RuntimeError/OSError cover provider transport
        # failures. Both collapse to "no signal" so the caller can
        # decide whether to proceed without ontology guidance.
        logger.warning(
            "Prompt ontology detection failed (%s); returning empty result",
            exc,
        )
        return PromptOntology(prompt=prompt)
    return parsed


__all__ = [
    "PROMPT_ONTOLOGY_TEMPLATE",
    "PromptOntology",
    "WeightedDimension",
    "build_prompt_ontology_prompt",
    "detect_ontology",
    "parse_prompt_ontology_response",
]
