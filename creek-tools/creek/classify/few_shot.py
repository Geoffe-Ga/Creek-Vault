"""Few-shot example loading and prompt assembly for LLM classification (FEAT-017).

The bundled ``creek/classify/examples/<dimension>.yaml`` fixtures hold
hand-curated example fragments for each classification dimension
(frequency, phase, mode, dosage, register). This module loads them
once, samples a deterministic-per-fragment subset, and renders them
into the prompt block injected before the request itself.

A stable hash of the fragment ID seeds the sample so the same fragment
always sees the same few-shot block (reproducible classifications)
while different fragments rotate through the corpus (broader coverage
across a vault).
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Final

import yaml

logger = logging.getLogger(__name__)

DIMENSIONS: Final[tuple[str, ...]] = (
    "frequency",
    "phase",
    "mode",
    "dosage",
    "register",
)
"""Classification dimensions backed by example fixtures."""

EXAMPLES_PER_DIMENSION: Final[int] = 3
"""Examples included per dimension in a rendered few-shot block.

Three keeps the prompt under the ~12 KiB ceiling even with body
truncation while still showing the model multiple cases per axis.
"""

_RESOURCES_PACKAGE: Final[str] = "creek.classify.examples"
_BODY_CAP_CHARS: Final[int] = 240
"""Characters retained from each example body before injection.

Caps the prompt growth from few-shot examples so a long body in a
fixture cannot push the full prompt past
:data:`creek.classify.llm._MAX_PROMPT_CONTENT_CHARS` and crowd out the
actual fragment being classified.
"""


@dataclass(frozen=True)
class FewShotExample:
    """A single labelled example for the few-shot prompt block.

    Attributes:
        title: Short headline of the example fragment.
        body: Representative excerpt (capped at
            :data:`_BODY_CAP_CHARS` when rendered).
        label: Canonical enum value for the dimension this example
            illustrates (e.g. ``"F3"`` for frequency, ``"rising"`` for
            phase).
        rationale: One-sentence explanation of why this label fits;
            shown to the model so the reasoning pattern is part of the
            example, not just the answer.
    """

    title: str
    body: str
    label: str
    rationale: str


@lru_cache(maxsize=1)
def _load_all_examples() -> dict[str, tuple[FewShotExample, ...]]:
    """Load every dimension's fixture file into immutable tuples.

    Cached so the YAML round-trip happens at most once per process.

    Returns:
        Mapping ``{dimension: (example, ...)}`` for every dimension in
        :data:`DIMENSIONS`. Missing or empty files are reported as a
        warning and yield an empty tuple — the prompt builder will
        silently skip dimensions with no examples rather than fail
        classification.
    """
    loaded: dict[str, tuple[FewShotExample, ...]] = {}
    for dim in DIMENSIONS:
        try:
            raw = (
                resources.files(_RESOURCES_PACKAGE)
                .joinpath(f"{dim}.yaml")
                .read_text(
                    encoding="utf-8",
                )
            )
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            logger.warning("Few-shot fixture for %s not found: %s", dim, exc)
            loaded[dim] = ()
            continue
        parsed = yaml.safe_load(raw) or []
        if not isinstance(parsed, list):
            logger.warning("Few-shot fixture %s.yaml is not a list; ignoring", dim)
            loaded[dim] = ()
            continue
        loaded[dim] = tuple(_coerce(entry) for entry in parsed if _is_valid(entry))
    return loaded


def _is_valid(entry: object) -> bool:
    """Return ``True`` when *entry* is a dict carrying all required keys."""
    if not isinstance(entry, dict):
        return False
    return all(
        isinstance(entry.get(k), str) for k in ("title", "body", "label", "rationale")
    )


def _coerce(entry: object) -> FewShotExample:
    """Convert a validated dict to :class:`FewShotExample`.

    Args:
        entry: Already validated by :func:`_is_valid` immediately
            before this call. Re-checked here so the type narrowing
            survives without an ``assert``.

    Returns:
        Immutable example record.

    Raises:
        TypeError: If *entry* is not a mapping. Should be unreachable
            because :func:`_is_valid` is the only gate.
    """
    if not isinstance(entry, dict):
        msg = "_coerce called with non-dict entry"
        raise TypeError(msg)
    return FewShotExample(
        title=str(entry["title"]),
        body=str(entry["body"]),
        label=str(entry["label"]),
        rationale=str(entry["rationale"]),
    )


def examples_for(dimension: str) -> tuple[FewShotExample, ...]:
    """Return every example fixture for *dimension*.

    Args:
        dimension: One of :data:`DIMENSIONS`.

    Returns:
        The dimension's full example tuple, or an empty tuple if the
        dimension is unknown.
    """
    return _load_all_examples().get(dimension, ())


def _seed_for(fragment_id: str) -> int:
    """Compute a stable integer seed from a fragment ID.

    Args:
        fragment_id: Stable per-fragment identifier (e.g. ``frag-abc123``).

    Returns:
        Non-negative integer suitable for seeding :mod:`random`.
    """
    digest = hashlib.sha256(fragment_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def sample_examples(
    fragment_id: str,
    *,
    per_dimension: int = EXAMPLES_PER_DIMENSION,
) -> dict[str, tuple[FewShotExample, ...]]:
    """Pick a deterministic-per-fragment sample of examples for each dimension.

    The sample is identical across repeated calls for the same
    ``fragment_id`` (so re-classifying a fragment uses the same
    examples) but varies across fragment IDs so a vault as a whole
    rotates through the available fixture corpus.

    Args:
        fragment_id: Stable per-fragment identifier; seeds the
            random sample so the choice is deterministic per fragment.
        per_dimension: Maximum examples per dimension. Capped at the
            number of fixtures available for the dimension.

    Returns:
        Mapping ``{dimension: (example, ...)}`` with at most
        ``per_dimension`` entries per dimension, in randomised order.
    """
    # The seed is a deterministic hash of the fragment ID. The PRNG is
    # used only to rotate example fixtures across a vault — never for
    # security-sensitive choices — so the standard ``random.Random``
    # is appropriate here.
    rng = random.Random(_seed_for(fragment_id))  # nosec B311
    chosen: dict[str, tuple[FewShotExample, ...]] = {}
    for dim, pool in _load_all_examples().items():
        if not pool:
            chosen[dim] = ()
            continue
        size = min(per_dimension, len(pool))
        chosen[dim] = tuple(rng.sample(pool, size))
    return chosen


def render_block(samples: dict[str, tuple[FewShotExample, ...]]) -> str:
    """Render a sampled few-shot block as plain prompt text.

    The block uses dimension headings and one ``- title / label /
    rationale / body`` quad per example. Bodies are truncated to
    :data:`_BODY_CAP_CHARS` so a long fixture cannot starve the prompt
    of room for the actual fragment.

    Args:
        samples: Output of :func:`sample_examples`.

    Returns:
        Multi-line prompt block. Empty string if no samples have any
        examples (e.g. fixtures absent at runtime).
    """
    lines: list[str] = []
    for dim in DIMENSIONS:
        chosen = samples.get(dim, ())
        if not chosen:
            continue
        lines.append(f"## {dim.title()} examples")
        for ex in chosen:
            body = (
                ex.body
                if len(ex.body) <= _BODY_CAP_CHARS
                else ex.body[:_BODY_CAP_CHARS] + "…"
            )
            lines.extend(
                (
                    f"- title: {ex.title}",
                    f"  label: {ex.label}",
                    f"  rationale: {ex.rationale}",
                    f"  body: {body}",
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip()
