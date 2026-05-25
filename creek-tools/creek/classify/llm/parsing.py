"""Response parsing for LLM-based fragment classification (FEAT-017).

Holds the YAML extraction and validation logic, enum parsing helpers,
and the per-section "apply" helpers that translate a validated LLM
response into Fragment classification fields.

The confidence-bias logic (Mode / Orientation / Dosage gated by
self-reported confidence) lives in :mod:`creek.classify.llm.calibration`
so this module stays focused on schema parsing, free of FEAT-017
biasing concerns.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TypeVar

import yaml

from creek.models import (
    Confidence,
    Dosage,
    Frequency,
    FrequencyClassification,
    VoiceClassification,
    VoiceRegister,
)

_EnumT = TypeVar("_EnumT", bound=StrEnum)

_DOSAGE_AMBIGUOUS_MARKERS: frozenset[str] = frozenset(
    {
        "ambiguous",
        "unclear",
        "mixed",
        "both",
        "uncertain",
    },
)
"""String values treated as ``Dosage.AMBIGUOUS``."""


_MAX_DESCRIPTOR_CHARS: int = 128
"""Cap on the length of the wavelength descriptor accepted from the LLM.

The descriptor is a short phrase from the Mode map (``"Gnosis"``,
``"Power-With"``, etc.); 128 characters comfortably covers every
documented value while bounding pathological responses that would
otherwise bloat fragment frontmatter on disk. See issue #319.
"""


_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"frequency", "wavelength", "voice", "confidence_scores"},
)
"""Top-level keys recognised in a documented LLM classification response.

Mirrors the sections in
:data:`creek.classify.llm.prompts.CLASSIFICATION_PROMPT`.
``confidence_scores`` was added by FEAT-017 to carry per-dimension
self-reported confidence for the default-unclassified bias on Mode /
Orientation / Dosage. Update both when adding a new section —
:func:`validate_response` will otherwise reject the LLM's output as
unexpected.
"""


_YAML_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:yaml)?\s*\n(.*?)```",
    flags=re.DOTALL,
)
"""Match a fenced YAML block; group 1 is the inner YAML text."""

_YAML_HEAD_RE: re.Pattern[str] = re.compile(
    r"^(frequency|wavelength|voice):", re.MULTILINE
)
"""Match the start of an unfenced YAML payload (a documented top-level key)."""


def _split_reasoning_and_yaml(response: str) -> tuple[str, str]:
    r"""Split a two-step LLM response into reasoning preamble and YAML payload.

    Tolerates three response shapes the model produces in practice:

    1. A fenced ``\`\`\`yaml ... \`\`\`\`` block preceded by reasoning prose.
    2. Reasoning prose followed by bare YAML starting at column 0.
    3. Pure YAML with no reasoning (backwards-compat with the
       pre-FEAT-017 prompt).

    Args:
        response: Raw LLM response text.

    Returns:
        ``(reasoning, yaml_text)``. ``reasoning`` is the stripped
        preamble, possibly empty. ``yaml_text`` is the YAML block,
        with code fences stripped if present.
    """
    fenced = _YAML_FENCE_RE.search(response)
    if fenced is not None:
        return response[: fenced.start()].strip(), fenced.group(1).strip()
    head = _YAML_HEAD_RE.search(response)
    if head is not None:
        return response[: head.start()].strip(), response[head.start() :].strip()
    return "", response.strip()


def _parse_enum(
    value: object,
    enum_type: type[_EnumT],
    default: _EnumT,
) -> _EnumT:
    """Parse a value into a StrEnum member with a fallback default.

    Args:
        value: Raw value to parse (converted to string).
        enum_type: The StrEnum subclass to match against.
        default: Fallback value when no match is found.

    Returns:
        The matching enum member or the default.
    """
    if value is None:
        return default
    val_str = str(value).lower().strip()
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return default


def _parse_optional_enum(
    value: object,
    enum_type: type[_EnumT],
) -> _EnumT | None:
    """Parse a value into an optional StrEnum member.

    Args:
        value: Raw value to parse (converted to string).
        enum_type: The StrEnum subclass to match against.

    Returns:
        The matching enum member or ``None``.
    """
    if value is None:
        return None
    val_str = str(value).lower().strip()
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return None


def _parse_descriptor(value: object) -> str:
    """Parse and normalise the wavelength ``descriptor`` field (issue #319).

    The descriptor is the only free-form field in
    :class:`creek.models.WavelengthClassification` — it carries a
    short phrase from the Mode map like ``"Gnosis"`` or
    ``"Power-With"``. Stripping whitespace prevents indexers from
    treating ``" Gnosis"`` and ``"Gnosis"`` as two distinct values,
    capping the length protects against pathological model output
    that would bloat the on-disk YAML, and the non-string fallback
    keeps a single junk response from crashing the entire batch.

    Args:
        value: Raw value from the LLM's ``wavelength.descriptor``
            field. May be any YAML scalar (str, int, None, bool, …).

    Returns:
        A whitespace-stripped string of at most
        :data:`_MAX_DESCRIPTOR_CHARS` characters. Empty string when
        the input is ``None`` or not a string.
    """
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if len(stripped) > _MAX_DESCRIPTOR_CHARS:
        return stripped[:_MAX_DESCRIPTOR_CHARS]
    return stripped


def _parse_dosage(value: object) -> Dosage:
    """Parse a dosage value, treating ambiguous markers specially.

    Values like ``"unclear"``, ``"mixed"``, or ``"both"`` map to
    ``Dosage.AMBIGUOUS`` rather than ``Dosage.UNCLASSIFIED``.

    Args:
        value: Raw dosage value from the LLM response.

    Returns:
        The parsed ``Dosage`` enum member.
    """
    if value is None:
        return Dosage.UNCLASSIFIED
    val_str = str(value).lower().strip()
    if val_str in _DOSAGE_AMBIGUOUS_MARKERS:
        return Dosage.AMBIGUOUS
    return _parse_enum(val_str, Dosage, Dosage.UNCLASSIFIED)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output.

    Args:
        text: Raw text that may contain triple-backtick fences.

    Returns:
        The text with code fences removed.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    filtered = [line for line in lines if not line.strip().startswith("```")]
    return "\n".join(filtered)


def validate_response(response_text: str) -> dict[str, object]:
    """Parse and strictly validate a YAML response from the LLM.

    Strips markdown code fences, then parses the result with
    ``yaml.safe_load_all`` so a multi-document stream — a classic
    LLM-output-spoofing payload — is detected explicitly rather
    than silently swallowing the first document. Top-level keys
    outside the documented schema (``frequency``, ``wavelength``,
    ``voice``) are also rejected so a successful injection cannot
    smuggle in fields like ``privacy_tier`` (see SEC-004).

    Args:
        response_text: Raw YAML text from the LLM.

    Returns:
        Parsed dictionary with classification data.

    Raises:
        ValueError: If the text is not a single YAML dict, or
            contains undocumented top-level keys.
    """
    text = _strip_code_fences(response_text)
    docs = list(yaml.safe_load_all(text))
    if len(docs) > 1:
        msg = f"multi-document YAML response rejected ({len(docs)} documents)"
        raise ValueError(msg)
    parsed: object = docs[0] if docs else None
    if not isinstance(parsed, dict):
        msg = f"Expected YAML dict, got {type(parsed).__name__}"
        raise ValueError(msg)  # noqa: TRY004  # ValueError matches the documented schema-validation contract; callers catch ValueError.
    keys = {str(k) for k in parsed}
    extras = keys - _ALLOWED_TOP_LEVEL_KEYS
    if extras:
        msg = f"unexpected top-level keys in LLM response: {sorted(extras)}"
        raise ValueError(msg)
    return {str(k): v for k, v in parsed.items()}


def _apply_frequency(
    data: dict[str, object],
    updates: dict[str, object],
) -> None:
    """Extract frequency classification from parsed data.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with frequency updates.
    """
    freq_data = data.get("frequency")
    if not isinstance(freq_data, dict):
        return
    primary = _parse_enum(
        freq_data.get("primary"),
        Frequency,
        Frequency.UNCLASSIFIED,
    )
    raw_secondary = freq_data.get("secondary")
    secondary: list[Frequency] = []
    if isinstance(raw_secondary, list):
        for item in raw_secondary:
            parsed = _parse_optional_enum(item, Frequency)
            if parsed is not None and parsed != Frequency.UNCLASSIFIED:
                secondary.append(parsed)
    updates["frequency"] = FrequencyClassification(
        primary=primary,
        secondary=secondary,
    )


def _apply_voice(
    data: dict[str, object],
    updates: dict[str, object],
) -> None:
    """Extract voice classification from parsed data.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with voice updates.
    """
    voice_data = data.get("voice")
    if not isinstance(voice_data, dict):
        return
    updates["voice"] = VoiceClassification(
        voice_register=_parse_optional_enum(
            voice_data.get("voice_register"),
            VoiceRegister,
        ),
        confidence=_parse_optional_enum(
            voice_data.get("confidence"),
            Confidence,
        ),
    )
