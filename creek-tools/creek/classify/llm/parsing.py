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

from creek.classify.evidence import layer_determined_over
from creek.classify.praxis_pass import PRAXIS_POTENTIAL_KEY, escalate
from creek.models import (
    Confidence,
    Dosage,
    Frequency,
    FrequencyClassification,
    PraxisPotential,
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


_MAX_TEXTURES: int = 5
"""Cap on how many emotional-texture tags are accepted from the LLM.

``emotional_texture`` is a *handful* of tags naming how a fragment feels,
not a taxonomy: five is generous for the axis and bounds what a
pathological response can write into every fragment's on-disk YAML.

It bounds **LLM-derived** data only, and that is exactly the threat it
protects the consumer from: :mod:`creek.link.temporal` adds ``+0.1`` per
*shared* texture tag (``creek/link/temporal.py:106-112``) with no clamp
anywhere in the module, so an uncapped *response* would let one runaway
model answer dominate the temporal score of every fragment it touches.
:func:`_parse_emotional_texture` hard-caps the response at five, and
:func:`_merge_textures` admits at most
``max(0, _MAX_TEXTURES - len(recorded))`` of those, so no classify run
can raise that term.

It is **not** a bound on what an operator hand-wrote, and never was:
:class:`~creek.models.Fragment` imposes no cap on the field and the
consumer clamps nothing, so a hand-authored twenty-tag list already
contributes ``+2.0``. Trimming the recorded list here would not have
fixed the unclamped consumer — it would only have deleted the
operator's tags. Issue #1216 tracks clamping the consumer itself. See
issue #878.
"""


_MAX_TEXTURE_CHARS: int = 32
"""Cap on the length of a single emotional-texture tag (issue #878).

Same reasoning as :data:`_MAX_DESCRIPTOR_CHARS`: keep the signal while
bounding pathological model output. A tag is a word or two ("grief",
"quiet-resolve"); 32 characters covers the vocabulary with room to spare
and truncates rather than drops, so a long-winded answer still
contributes something.
"""


_TEXTURE_SECTION_KEY: str = "texture"
"""Top-level YAML key carrying the emotional-texture section (issue #878).

Deliberately **not** ``emotional_texture``. See
:data:`_ALLOWED_TOP_LEVEL_KEYS` — the allow-list is the SEC-004 injection
boundary and is never widened to a :class:`~creek.models.Fragment` field
name, so the section is named after the *dimension* and the field name
stays rejected at top level. Exactly the shape ``praxis:`` / ``potential:``
already uses to write ``praxis_potential``.
"""

_TEXTURE_LIST_KEY: str = "emotional"
"""Key inside the ``texture`` section holding the tag list (issue #878)."""

_EMOTIONAL_TEXTURE_FIELD: str = "emotional_texture"
"""The :class:`~creek.models.Fragment` field the ``texture`` section writes."""


_TEXTURE_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")
"""Match an internal whitespace run in a texture tag, to fold to ``-``.

Without the fold, ``"Deep   Grief"`` and ``"deep-grief"`` are two
distinct entries in the wavelength report's texture cloud and two
distinct misses for the ``+0.1``-per-shared-tag temporal link term.
Compiled once at import: this runs on every tag of every classified
fragment.
"""


_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "frequency",
        "wavelength",
        "voice",
        "confidence_scores",
        "praxis",
        _TEXTURE_SECTION_KEY,
    },
)
"""Top-level keys recognised in a documented LLM classification response.

Mirrors the sections in
:data:`creek.classify.llm.prompts.CLASSIFICATION_PROMPT`.
``confidence_scores`` was added by FEAT-017 to carry per-dimension
self-reported confidence for the default-unclassified bias on Mode /
Orientation / Dosage. ``praxis`` was added by issue #877 to carry the
``latent`` verdict no keyword heuristic can produce. ``texture`` was
added by issue #878 to carry ``emotional_texture``, which had no producer
at all. Update both when adding a new section —
:func:`validate_response` will otherwise reject the LLM's output as
unexpected.

This allow-list is the SEC-004 injection boundary, so it is widened one
key at a time and never to a :class:`~creek.models.Fragment` field name:
``privacy_tier`` in particular stays rejected, or a successful prompt
injection could talk the classifier into re-tiering intimate content as
``open``.

That rule is why #878's section is ``texture:`` and not
``emotional_texture:`` — naming a section after the field it writes is
precisely the shape the rule forbids, and a bare top-level
``emotional_texture:`` therefore still fails validation.
"""


_YAML_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:yaml)?\s*\n(.*?)```",
    flags=re.DOTALL,
)
"""Match a fenced YAML block; group 1 is the inner YAML text."""

_YAML_HEAD_RE: re.Pattern[str] = re.compile(
    r"^(frequency|wavelength|voice):", re.MULTILINE
)
"""Match the start of an unfenced YAML payload (a documented top-level key).

Deliberately NOT widened with ``praxis`` (issue #877) or ``texture``
(issue #878): the schema lists both after the three keys above, so
neither can ever be the *first* key of a payload, and adding an
alternative here would let a stray line of reasoning prose beginning
``praxis:`` or ``texture:`` be mistaken for the start of the YAML —
unrelated risk for zero benefit.
"""


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
    outside the documented schema (see
    :data:`_ALLOWED_TOP_LEVEL_KEYS`) are also rejected so a
    successful injection cannot smuggle in fields like
    ``privacy_tier`` (see SEC-004).

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


def _determined_secondaries(raw: object) -> list[Frequency]:
    """Collect the secondary frequencies a response actually decided (#1637).

    Anything that is not a recognisable, non-sentinel :class:`Frequency`
    is dropped: a non-list ``secondary:`` (a bare scalar, ``null``, a
    mapping), an entry that matches no member, and the
    ``unclassified`` sentinel itself. The result is therefore "the
    secondaries this response determined", and an empty result means the
    response determined none — which :func:`_apply_frequency` reads as
    silence rather than as an instruction to clear.

    Args:
        raw: The response's ``secondary`` value, unvalidated.

    Returns:
        The determined secondary frequencies, in response order.
    """
    if not isinstance(raw, list):
        return []
    determined: list[Frequency] = []
    for item in raw:
        parsed = _parse_optional_enum(item, Frequency)
        if parsed is not None and parsed != Frequency.UNCLASSIFIED:
            determined.append(parsed)
    return determined


def _apply_frequency(
    data: dict[str, object],
    updates: dict[str, object],
    current: FrequencyClassification,
) -> None:
    """Layer the response's frequency verdict over the recorded one (#1637).

    Before #1637 this rebuilt the block wholesale: any ``frequency:``
    mapping at all, however empty, wrote a fresh
    :class:`~creek.models.FrequencyClassification` whose ``primary`` fell
    back to the ``unclassified`` sentinel. A response naming only
    ``secondary``, an empty ``frequency: {}``, or a ``primary`` the
    parser could not recognise therefore replaced a recorded primary with
    the sentinel. This was the last legacy writer on the single-pick LLM
    path still doing that (#1331 fixed voice, #1421 wavelength).

    The rule, stated in full:

    - **A determined primary wins, and takes its secondaries with it.**
      ``primary`` is parsed through :func:`_parse_optional_enum` rather
      than :func:`_parse_enum` precisely so "the model named nothing",
      "the model named junk", and "the model named ``unclassified``" all
      collapse to the same answer — *not determined* — instead of being
      laundered into a sentinel that then overwrites the record. This is
      the reading #1421 settled for the FEAT-017 downgrade: do not adopt
      the noisy pick, rather than erase what we knew.
    - **THE DELIBERATE ASYMMETRY.** When the primary *is* determined,
      ``secondary`` is replaced wholesale with whatever this response
      parsed — including the empty list.
      :attr:`~creek.models.FrequencyClassification.secondary` is a list,
      and a list cannot be merged field-wise the way the scalar
      wavelength and voice axes can, so stale secondaries from an earlier
      verdict are cleared rather than accumulated. The identical
      asymmetry is spelled out on the weighted path at
      :meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`
      and on the rules path at
      ``creek.classify.rules.RuleClassifier._layer_over_fragment``; keep
      the three in sync.
    - **That asymmetry is why this axis cannot use**
      :func:`~creek.classify.evidence.layer_determined_over`. That helper
      dumps with ``exclude_defaults=True``, and ``secondary``'s default
      *is* the empty list — so a primary-only response would have its
      deliberate clearing silently dropped and the stale secondaries
      would survive. Frequency is a sibling of the layering rule, not a
      caller of it.
    - **A response that determines neither axis writes no key at all**,
      because
      :meth:`~creek.classify.llm.orchestrator.LLMClassifier._apply_classification`
      returns the input fragment itself when ``updates`` is empty and
      callers read that object identity as "this run marked nothing". A
      value-equal copy would blur that signal.

    One deliberate cost, the mirror of the one ``descriptor`` carries in
    :func:`~creek.classify.llm.calibration._apply_wavelength`: a response
    can no longer clear a recorded primary back to ``unclassified``, nor
    clear the secondaries without also naming a primary. Un-classifying
    a fragment is an operator edit to the frontmatter, not something a
    model response is entitled to do by falling silent.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with the merged frequency block. Left
            untouched unless this response determined at least one axis.
        current: The frequency block already on the fragment. Supplies
            the primary when this response names none. Never mutated.
    """
    freq_data = data.get("frequency")
    if not isinstance(freq_data, dict):
        return
    primary = _parse_optional_enum(freq_data.get("primary"), Frequency)
    if primary == Frequency.UNCLASSIFIED:
        primary = None
    secondary = _determined_secondaries(freq_data.get("secondary"))
    if primary is None and not secondary:
        return
    updates["frequency"] = FrequencyClassification(
        primary=current.primary if primary is None else primary,
        secondary=secondary,
    )


def _apply_praxis(
    data: dict[str, object],
    updates: dict[str, object],
    current: PraxisPotential,
) -> None:
    """Extract the praxis-potential verdict from parsed data (issue #877).

    The LLM can only ever *raise* this axis, and "raise" is measured
    against *the verdict the fragment already carries* — not merely
    against ``none``. The merge therefore runs through the single
    canonical :func:`creek.classify.praxis_pass.escalate`, which takes the
    higher of the two on ``none < latent < explicit``. Refusing to write
    only when the parsed value is ``none`` would not be enough: ``latent``
    is also weaker than ``explicit``, and a model answering ``latent`` for
    a fragment already at ``explicit`` would demote it. Nothing downstream
    can repair that — the keyword heuristic
    (:func:`creek.classify.praxis_pass.detect`) re-derives from the body
    and only ever proposes ``explicit`` or ``none``, so a verdict that
    came from judgment rather than keywords keeps the demotion to disk.

    The escalating direction still stands: ``latent`` over ``none`` and
    ``explicit`` over ``latent`` are both written. ``latent`` is the
    verdict this branch exists for — no regular expression can see "there
    is a practice hiding in here that the author has not named".

    An unchanged verdict must write **no key at all**, because
    :meth:`~creek.classify.llm.orchestrator.LLMClassifier._apply_classification`
    returns the input fragment itself when ``updates`` is empty. Writing
    the merged value back whenever the model merely agrees would allocate
    a fresh model copy per fragment and blur the "did this run change
    anything?" signal callers read from object identity.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with the praxis update. Left untouched
            unless the merge raised the axis above *current*.
        current: The verdict already recorded on the fragment. The merge
            never returns anything weaker than this.
    """
    section = data.get("praxis")
    if not isinstance(section, dict):
        return
    potential = _parse_enum(
        section.get("potential"),
        PraxisPotential,
        PraxisPotential.NONE,
    )
    merged = escalate(current, potential)
    if merged is current:
        return
    # ``.value`` because ``Fragment`` uses ``use_enum_values=True`` but
    # ``model_copy`` — which consumes this dict — bypasses that coercion,
    # and YAML's SafeDumper cannot represent a bare StrEnum member.
    updates[PRAXIS_POTENTIAL_KEY] = merged.value


def _parse_texture_tag(item: object) -> str | None:
    """Normalise one raw emotional-texture item, or reject it (issue #878).

    Strip, lowercase, fold internal whitespace runs to ``-``, then
    truncate. Truncating rather than dropping mirrors
    :func:`_parse_descriptor`: a long-winded tag still carries signal,
    whereas a dropped one carries none.

    Args:
        item: One entry of the LLM's ``texture.emotional`` list. May be
            any YAML scalar or container.

    Returns:
        The normalised tag, or ``None`` when *item* is not a string or
        normalises to nothing.
    """
    if not isinstance(item, str):
        return None
    collapsed = _TEXTURE_WHITESPACE_RE.sub("-", item.strip().lower())
    if not collapsed:
        return None
    return collapsed[:_MAX_TEXTURE_CHARS]


def _parse_emotional_texture(value: object) -> list[str]:
    """Parse and sanitise the LLM's ``texture.emotional`` list (issue #878).

    A non-string **item** is dropped, not the whole list. Models emit
    ``[grief, 42, {mood: sad}]`` often enough that discarding the list on
    the first offender would cost most of the signal this axis exists to
    capture; dropping only the offenders keeps the usable half. A
    non-list *value* is a different failure — there is no usable half —
    and yields the empty list.

    Order is first-seen so the :data:`_MAX_TEXTURES` truncation is
    deterministic across re-runs; a set-based implementation would churn
    the same fragment's frontmatter on every classify.

    Args:
        value: Raw value from the LLM's ``texture.emotional`` field.

    Returns:
        Up to :data:`_MAX_TEXTURES` normalised, deduplicated tags. Empty
        when the value is not a list or nothing survived sanitisation.
    """
    if not isinstance(value, list):
        return []
    parsed: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = _parse_texture_tag(item)
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        parsed.append(tag)
        if len(parsed) == _MAX_TEXTURES:
            break
    return parsed


def _merge_textures(current: list[str], candidate: list[str]) -> list[str]:
    """Union *current* and *candidate*, existing-first, capped (issue #878).

    Never a replacement: a model that saw only part of a long fragment
    must not be able to delete tags an earlier run — or the operator —
    put on record. Both halves of that are total.

    **Preservation is verbatim.** *current*, minus its exact duplicates,
    is always a *prefix* of the result — however long the list is and
    however long any single entry is. No normalisation and no length
    check is applied to the recorded side, so a hand-authored
    200-character texture survives byte-for-byte. The only entry of
    *current* that can be absent is an exact duplicate of an earlier
    one.

    **Growth is bounded.** At most ``max(0, _MAX_TEXTURES - len(prefix))``
    entries of *candidate* are admitted on top of that prefix — exactly
    that many when *candidate* offers enough distinct new tags — so
    :data:`_MAX_TEXTURES` ceilings
    what a classification may *add* rather than limiting the field's
    length. A list already past the ceiling keeps every tag and gains
    none, which makes repeated classifications idempotent rather than a
    ratchet.

    Unlike :func:`creek.classify.tags_pass.merge`, the recorded side is
    **not** re-normalised. ``emotional_texture`` has exactly one producer
    — this parser, which normalises everything it writes — so a
    re-normalisation pass here could only ever rewrite a value the
    operator hand-edited on purpose.

    Args:
        current: The tags already recorded on the fragment.
        candidate: The sanitised tags from this response.

    Returns:
        *current* with its exact duplicates removed, in order, followed
        by the admitted new tags.
    """
    merged = list(dict.fromkeys(current))
    seen = set(merged)
    room = max(_MAX_TEXTURES - len(merged), 0)
    for tag in candidate:
        if room == 0:
            break
        if tag in seen:
            continue
        seen.add(tag)
        merged.append(tag)
        room -= 1
    return merged


def _apply_texture(
    data: dict[str, object],
    updates: dict[str, object],
    current: list[str],
) -> None:
    """Extract the emotional-texture tags from parsed data (issue #878).

    ``emotional_texture`` shipped with a ``default_factory=list`` and no
    producer: 2000/2000 sampled fragments of the operator's
    35,330-fragment vault carried ``emotional_texture: []``, which left
    the ``+0.1``-per-shared-tag term in :mod:`creek.link.temporal` dead
    and the wavelength report's texture cloud permanently reading "*No
    emotional texture tags recorded.*". This section rides inside the
    **existing** classification response — no new call, no new round
    trip, and it inherits the Intimate-never-cloud routing of the call it
    rides on for free.

    An unchanged union must write **no key at all**, because
    :meth:`~creek.classify.llm.orchestrator.LLMClassifier._apply_classification`
    returns the input fragment itself when ``updates`` is empty. That is
    also what supplies the acceptance criterion's "falls back to ``[]``":
    the model's ``default_factory`` answers, rather than this parser
    stamping an empty list over the fragment.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with the texture update. Left untouched
            unless the union added something.
        current: The tags already recorded on the fragment. The merge
            never drops one.
    """
    section = data.get(_TEXTURE_SECTION_KEY)
    if not isinstance(section, dict):
        return
    candidate = _parse_emotional_texture(section.get(_TEXTURE_LIST_KEY))
    merged = _merge_textures(current, candidate)
    if merged == current:
        return
    updates[_EMOTIONAL_TEXTURE_FIELD] = merged


def _apply_voice(
    data: dict[str, object],
    updates: dict[str, object],
    current: VoiceClassification,
) -> None:
    """Layer the response's voice verdict over the recorded one (#1331).

    Takes the fragment's prior value for the same reason
    :func:`_apply_praxis` (#877) and :func:`_apply_texture` (#878) do.
    Rebuilding the whole block from the response nulls whichever axis
    the model was silent about: a response carrying ``voice:
    {voice_register: confessional}`` and no ``confidence`` used to erase
    a persisted ``conviction`` — half of the INTIMATE trigger read by
    :meth:`~creek.classify.privacy.PrivacyClassifier._is_high_confidence_confessional`
    — so the escalation that evidence should unlock never fired. That is
    a fail-open, and it is the single-pick LLM path's copy of the defect
    fixed one layer earlier in
    :meth:`~creek.classify.rules.RuleClassifier.classify`.

    Unlike the wavelength axes there is no FEAT-017 gating question
    here: ``voice_register`` and ``confidence`` are not in
    :data:`~creek.classify.llm.calibration._BIASED_DIMENSIONS`, so no
    self-reported confidence score is entitled to downgrade them to a
    sentinel. A model silent about an axis has said nothing, and silence
    must not erase evidence.

    A response with no ``voice`` block at all stays a **no-op** — no key
    is written, rather than a wholly-default verdict being merged — so
    :meth:`~creek.classify.llm.orchestrator.LLMClassifier._apply_classification`
    can still recognise a run that marked nothing.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with the merged voice block.
        current: The voice classification already on the fragment. Only
            the axes this response actually decided are overlaid on it.
    """
    voice_data = data.get("voice")
    if not isinstance(voice_data, dict):
        return
    updates["voice"] = layer_determined_over(
        prior=current,
        determined=VoiceClassification(
            voice_register=_parse_optional_enum(
                voice_data.get("voice_register"),
                VoiceRegister,
            ),
            confidence=_parse_optional_enum(
                voice_data.get("confidence"),
                Confidence,
            ),
        ),
    )
