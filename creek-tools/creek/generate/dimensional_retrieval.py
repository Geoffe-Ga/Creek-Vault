"""Per-dimension corpus retrieval for draft generation.

The fragment classifier hands :mod:`creek.generate.drafts` one canonical
ontology pick per dimension. The original draft pipeline ANDed those
picks together when assembling source material — every fragment had to
match every active dimension — and on a real-world vault the
intersection emptied out, blocking generation entirely.

This module implements the load-bearing replacement: each active
dimension fetches its own corpus slice independently, the slices
**union** (not intersect), and each slice carries a weight so the
composition step can lean on the heaviest signals without dropping the
lighter ones.

The public surface is small on purpose:

* :data:`DimensionKey` — ``(kind, value)`` tuples that identify a slice
  ("phase x peaking", "mode x express"). Sortable and hashable so they
  flow cleanly through dict-of-dimensions data structures and
  frontmatter serialisation.
* :class:`DimensionSlice` — frozen record of one slice's fragments + weight.
* :func:`assemble_per_dimension_corpus` — returns ``{key: slice}`` for
  every dimension the user requested (explicit flags or detected
  :class:`PromptOntology`). Empty dimensions are kept in the mapping
  with an empty fragment tuple so callers can warn about them
  specifically per the #351 acceptance criterion.
* :class:`AllDimensionsEmptyError` — raised by
  :func:`raise_if_all_empty` when every attempted dimension produced
  zero matches; the message names the attempted dimensions so the
  operator can widen filters or pour in more sources.

The matching predicate is intentionally identical to the existing
``_fragment_matches_dimensions`` in :mod:`creek.generate.drafts` so the
two retrieval paths behave consistently on the same vault. Topic
substring matching is layered on top per-slice when ``SeedSpec.topic``
is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.models import Fragment, Frequency, Mode, Phase

if TYPE_CHECKING:
    from creek.classify.prompt import PromptOntology
    from creek.generate.drafts import SeedSpec


DimensionKey = tuple[str, str]
"""``(kind, value)`` identifying a corpus slice.

``kind`` is one of ``"phase"``, ``"mode"``, ``"frequency"``; ``value``
is the canonical enum's ``.value`` (e.g., ``"peaking"``, ``"express"``,
``"F3"``). Sorting / hashing are inherited from the tuple, which keeps
serialisation predictable.
"""


_EXPLICIT_FLAG_WEIGHT: float = 1.0
"""Weight assigned to a dimension activated by an explicit CLI flag.

Explicit flags carry no LLM-derived weight, so they enter the per-
dimension blend at full strength. This is the documented invariant in
the #351 issue body: "weight 1.0 for explicit flags".
"""


@dataclass(frozen=True)
class DimensionSlice:
    """One dimension's corpus contribution to the per-dimension blend.

    Attributes:
        key: The ``(kind, value)`` identifier (e.g., ``("phase",
            "peaking")``).
        weight: Per-dimension weight in ``[0.0, 1.0]``. ``1.0`` for
            explicit CLI flags; the
            :class:`creek.classify.prompt.PromptOntology` weight for
            detected dimensions.
        fragment_ids: Tuple of matched fragment IDs, in first-seen
            order across the loaded fragment map. Empty when the
            dimension matched nothing.
    """

    key: DimensionKey
    weight: float
    fragment_ids: tuple[str, ...]

    @property
    def has_matches(self) -> bool:
        """Return ``True`` when the slice contains at least one fragment."""
        return bool(self.fragment_ids)


class AllDimensionsEmptyError(ValueError):
    """Raised when every attempted dimension produced zero matches.

    The error message lists the attempted dimensions so the operator
    can decide whether to widen the filters, ingest more sources, or
    accept that the requested ontology corner is genuinely empty.
    """


def _fragment_matches_value(fragment: Fragment, key: DimensionKey) -> bool:
    """Return ``True`` when *fragment* matches the dimension *key*.

    Compares against the StrEnum ``.value`` representation so the check
    succeeds against both the in-memory enum form and the on-disk
    string form (``Fragment`` is serialised with
    ``use_enum_values=True``).
    """
    kind, value = key
    if kind == "phase":
        return str(fragment.wavelength.phase) == value
    if kind == "mode":
        return str(fragment.wavelength.mode) == value
    if kind == "frequency":
        return str(fragment.frequency.primary) == value
    return False


def _topic_matches(fragment: Fragment, body: str, topic: str | None) -> bool:
    """Return ``True`` when *topic* is ``None`` or appears in title/body.

    A ``None`` topic is treated as a no-op filter so callers can chain
    the topic check unconditionally without an extra branch.
    """
    if topic is None:
        return True
    needle = topic.strip().lower()
    if not needle:
        return True
    if needle in fragment.title.lower():
        return True
    return needle in body.lower()


def _explicit_dimension_keys(spec: SeedSpec) -> list[DimensionKey]:
    """Return ``DimensionKey`` entries derived from *spec*'s explicit flags.

    The order is deterministic — phases, then modes, then frequencies —
    so downstream dict iteration and frontmatter serialisation are
    stable across runs.
    """
    keys: list[DimensionKey] = []
    keys.extend(("phase", phase.value) for phase in spec.phases)
    keys.extend(("mode", mode.value) for mode in spec.modes)
    keys.extend(("frequency", freq.value) for freq in spec.frequencies)
    return keys


def _ontology_dimension_keys(
    ontology: PromptOntology | None,
    *,
    confidence_threshold: float,
) -> list[tuple[DimensionKey, float]]:
    """Return weighted ``DimensionKey`` entries from *ontology*.

    Filters out entries whose weight is below *confidence_threshold* so
    the LLM's noise floor does not pollute the blend. Drops
    ``UNCLASSIFIED`` enum members because they would match every
    on-disk fragment and defeat the purpose of the per-dimension
    filter.
    """
    if ontology is None:
        return []
    weighted: list[tuple[DimensionKey, float]] = []
    weighted.extend(
        _filter_weighted(
            ontology.phases,
            "phase",
            Phase.UNCLASSIFIED,
            confidence_threshold,
        ),
    )
    weighted.extend(
        _filter_weighted(
            ontology.modes,
            "mode",
            Mode.UNCLASSIFIED,
            confidence_threshold,
        ),
    )
    weighted.extend(
        _filter_weighted(
            ontology.frequencies,
            "frequency",
            Frequency.UNCLASSIFIED,
            confidence_threshold,
        ),
    )
    return weighted


def _filter_weighted(
    entries: tuple[object, ...],
    kind: str,
    unclassified: object,
    threshold: float,
) -> list[tuple[DimensionKey, float]]:
    """Drop below-threshold and unclassified entries from a weighted dimension list.

    Helper for :func:`_ontology_dimension_keys`; pulled out so the
    cyclomatic complexity of the caller stays under the project floor.
    """
    out: list[tuple[DimensionKey, float]] = []
    for entry in entries:
        weight = getattr(entry, "weight", 0.0)
        value = getattr(entry, "value", None)
        if value is None or value == unclassified:
            continue
        if weight < threshold:
            continue
        out.append(((kind, str(value)), float(weight)))
    return out


def assemble_per_dimension_corpus(
    spec: SeedSpec,
    loaded: dict[str, tuple[Fragment, str]],
    *,
    ontology: PromptOntology | None = None,
    confidence_threshold: float = 0.0,
) -> dict[DimensionKey, DimensionSlice]:
    """Return one corpus slice per active dimension.

    Each active dimension — explicit CLI flag or above-threshold
    :class:`~creek.classify.prompt.PromptOntology` entry — fetches its
    own slice of *loaded* independently. Sets union: a fragment that
    matches more than one dimension appears in each matching slice
    rather than being deduplicated. The composition step (the LLM
    prompt template) labels each slice so the model can weave them
    rather than receiving a pre-blended block.

    Topic substring matching, when ``spec.topic`` is set, is applied to
    every slice — a fragment only enters a slice if it both matches
    the dimension and contains the topic. Topic-only specs (no
    dimensional flags / ontology) return an empty mapping; the caller
    falls back to the legacy single-dimension path.

    Args:
        spec: The user's manual seed specification.
        loaded: ``{fragment_id: (Fragment, body)}`` map for the vault.
        ontology: Optional detected :class:`PromptOntology` whose
            weighted dimensions augment the explicit flags. Explicit
            flags always win over the ontology when both name the same
            ``(kind, value)`` — the explicit weight of ``1.0`` is the
            stronger commitment.
        confidence_threshold: Minimum ontology weight to admit; entries
            below the threshold are silently dropped. Defaults to
            ``0.0`` so callers without a calibration value see every
            ontology entry the parser returned.

    Returns:
        ``{DimensionKey: DimensionSlice}`` for every attempted
        dimension. Empty slices are retained so callers can report
        them specifically per the #351 acceptance criterion. An empty
        outer mapping means no dimensions were active.
    """
    weighted_keys = _merge_dimension_sources(spec, ontology, confidence_threshold)
    if not weighted_keys:
        return {}
    slices: dict[DimensionKey, DimensionSlice] = {}
    for key, weight in weighted_keys:
        matched_ids = _match_slice(key, spec.topic, loaded)
        slices[key] = DimensionSlice(
            key=key,
            weight=weight,
            fragment_ids=matched_ids,
        )
    return slices


def _merge_dimension_sources(
    spec: SeedSpec,
    ontology: PromptOntology | None,
    confidence_threshold: float,
) -> list[tuple[DimensionKey, float]]:
    """Combine explicit-flag dimensions with ontology dimensions.

    Explicit flags take precedence: when both sources name the same
    ``(kind, value)`` key, the explicit ``1.0`` weight is kept and the
    ontology's weighted entry is discarded.
    """
    explicit = [(key, _EXPLICIT_FLAG_WEIGHT) for key in _explicit_dimension_keys(spec)]
    explicit_keys = {key for key, _ in explicit}
    ontology_entries = [
        (key, weight)
        for key, weight in _ontology_dimension_keys(
            ontology, confidence_threshold=confidence_threshold
        )
        if key not in explicit_keys
    ]
    return explicit + ontology_entries


def _match_slice(
    key: DimensionKey,
    topic: str | None,
    loaded: dict[str, tuple[Fragment, str]],
) -> tuple[str, ...]:
    """Return matched fragment IDs for a single dimension *key*."""
    matched: list[str] = []
    for fragment, body in loaded.values():
        if not _fragment_matches_value(fragment, key):
            continue
        if not _topic_matches(fragment, body, topic):
            continue
        matched.append(fragment.id)
    return tuple(matched)


def empty_dimensions(
    slices: dict[DimensionKey, DimensionSlice],
) -> list[DimensionKey]:
    """Return the dimension keys whose slice produced zero matches.

    Used by :mod:`creek.generate.drafts` to emit a per-dimension
    warning before proceeding — the #351 acceptance criterion that "if
    a single dimension has zero matches, the system warns about that
    dimension specifically but proceeds with the others".
    """
    return [key for key, slice_ in slices.items() if not slice_.has_matches]


def union_fragment_ids(
    slices: dict[DimensionKey, DimensionSlice],
) -> tuple[str, ...]:
    """Return the union of fragment IDs across *slices*, first-seen ordered.

    Iteration follows the deterministic key order in the slices
    mapping (explicit flags first, then ontology), so the union is
    stable across runs given the same input.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for slice_ in slices.values():
        for fid in slice_.fragment_ids:
            if fid in seen_set:
                continue
            seen_set.add(fid)
            seen.append(fid)
    return tuple(seen)


def format_dimension_label(key: DimensionKey) -> str:
    """Return a human-readable prompt label for *key*.

    "phase x peaking" → "the peaking phase"; "mode x express" → "the
    express stance"; "frequency x F3" → "the F3 frequency". The phrasing
    matches the #351 issue body's worked example so the prompt template
    reads as the issue specified.
    """
    kind, value = key
    if kind == "phase":
        return f"the {value} phase"
    if kind == "mode":
        return f"the {value} stance"
    if kind == "frequency":
        return f"the {value} frequency"
    return f"{kind} {value}"


def raise_if_all_empty(
    slices: dict[DimensionKey, DimensionSlice],
) -> None:
    """Raise :class:`AllDimensionsEmptyError` when every slice is empty.

    A non-empty *slices* mapping where every slice has zero fragments
    means the operator's requested dimensions found nothing across the
    whole vault — a clearer diagnostic than the previous
    AND-intersection failure mode. The error names each attempted
    dimension so the operator can decide which filter to drop or which
    sources to ingest.

    Args:
        slices: The result of :func:`assemble_per_dimension_corpus`.

    Raises:
        AllDimensionsEmptyError: When *slices* is non-empty and every
            entry has zero matched fragments.
    """
    if not slices:
        return
    if any(slice_.has_matches for slice_ in slices.values()):
        return
    labels = ", ".join(format_dimension_label(key) for key in slices)
    msg = (
        f"No source material in any attempted dimension: {labels}. "
        "Try widening the filters or pouring in more sources."
    )
    raise AllDimensionsEmptyError(msg)


__all__ = [
    "AllDimensionsEmptyError",
    "DimensionKey",
    "DimensionSlice",
    "assemble_per_dimension_corpus",
    "empty_dimensions",
    "format_dimension_label",
    "raise_if_all_empty",
    "union_fragment_ids",
]
