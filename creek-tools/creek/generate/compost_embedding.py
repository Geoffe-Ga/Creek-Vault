"""Exemplar-based embedding similarity gate for compost detection (FEAT-018).

Replaces the literal-phrase ``ABANDONMENT_KEYWORDS`` regex with a
semantic gate. A curated list of *exemplars* — short prose passages
that capture the texture of idea-abandonment across registers — is
loaded from a packaged YAML file. At detection time the embedding
linker encodes each fragment's text and the exemplar bodies, then the
gate returns the maximum cosine similarity. Fragments above the
configured threshold (default 0.6, deliberately wide) advance to the
LLM verifier in :mod:`creek.generate.compost_verifier`.

The design intentionally couples loosely: the loader returns simple
:class:`CompostExemplar` dataclasses, and :func:`make_similarity_fn`
returns a closure that callers can swap out for a deterministic stub
in tests.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek.link.embeddings import EmbeddingLinker

logger = logging.getLogger(__name__)

PACKAGED_EXEMPLARS_PATH: Path = Path(__file__).parent / "exemplars" / "compost.yaml"
"""Location of the packaged default exemplar set."""

_REQUIRED_KEYS: frozenset[str] = frozenset({"title", "body", "texture", "rationale"})
"""Keys every exemplar entry must carry."""


@dataclass(frozen=True)
class CompostExemplar:
    """One curated abandonment-texture exemplar.

    Attributes:
        title: Human-readable label (not used for matching).
        body: The text the embedding gate matches fragment text against.
        texture: One-word descriptor (e.g. ``circling``, ``releasing``).
        rationale: One-sentence explanation of why this exemplar
            represents compost; surfaced in the calibration report.
    """

    title: str
    body: str
    texture: str
    rationale: str


def load_exemplars(path: Path | None = None) -> tuple[CompostExemplar, ...]:
    """Load and validate compost exemplars from *path* (or the packaged default).

    Args:
        path: Path to a YAML file holding a list of exemplar entries.
            When ``None``, loads :data:`PACKAGED_EXEMPLARS_PATH`.

    Returns:
        Tuple of :class:`CompostExemplar`, one per YAML record.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the YAML is empty, the wrong shape, or any
            entry is missing a required key.
    """
    target = path or PACKAGED_EXEMPLARS_PATH
    if not target.exists():
        msg = f"Compost exemplars file not found: {target}"
        raise FileNotFoundError(msg)

    with target.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, list) or not raw:
        msg = f"Compost exemplars file {target} must contain a non-empty list."
        raise ValueError(msg)

    exemplars: list[CompostExemplar] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"Exemplar {index} in {target} is not a mapping."
            raise ValueError(msg)  # noqa: TRY004  # preserves documented ValueError schema contract
        missing = _REQUIRED_KEYS - entry.keys()
        if missing:
            msg = (
                f"Exemplar {index} in {target} is missing required keys: "
                f"{sorted(missing)}"
            )
            raise ValueError(msg)
        exemplars.append(
            CompostExemplar(
                title=str(entry["title"]),
                body=str(entry["body"]),
                texture=str(entry["texture"]),
                rationale=str(entry["rationale"]),
            ),
        )
    return tuple(exemplars)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity between two equal-length vectors.

    Returns ``0.0`` for either-zero vectors so callers can use the
    result directly as a sort key without a degenerate-magnitude
    guard.
    """
    dot: float = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm_a: float = math.sqrt(math.fsum(x * x for x in a))
    norm_b: float = math.sqrt(math.fsum(y * y for y in b))
    if 0.0 in (norm_a, norm_b):
        return 0.0
    return dot / (norm_a * norm_b)


def make_similarity_fn(
    exemplars: Sequence[CompostExemplar],
    linker: EmbeddingLinker,
) -> Callable[[str], float]:
    """Return a closure mapping text → max cosine similarity to exemplars.

    Exemplar embeddings are computed once on the first call and cached
    in the closure, so repeated invocations over a fragment list pay
    the encode cost only for the fragment text itself.

    Args:
        exemplars: Curated abandonment-texture exemplars.
        linker: An :class:`EmbeddingLinker` configured with the same
            model that will encode fragment text. Production callers
            wire in the project-wide linker; tests pass a fake.

    Returns:
        A callable that, given any string, returns the maximum cosine
        similarity to any exemplar body, in ``[0.0, 1.0]``.
    """
    exemplar_vectors: list[list[float]] = []

    def _ensure_exemplar_vectors() -> None:
        if exemplar_vectors:
            return
        for exemplar in exemplars:
            exemplar_vectors.append(linker.generate_embedding(exemplar.body))

    def similarity(text: str) -> float:
        if not text.strip():
            return 0.0
        _ensure_exemplar_vectors()
        if not exemplar_vectors:
            return 0.0
        fragment_vector = linker.generate_embedding(text)
        return max(
            _cosine_similarity(fragment_vector, exemplar_vector)
            for exemplar_vector in exemplar_vectors
        )

    return similarity
