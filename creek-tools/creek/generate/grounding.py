"""Bidirectional grounding guard for ``creek draft`` (issue #355).

A draft has two opposite failure modes that the guard surfaces after
composition:

* **Too derivative** — at least one draft paragraph echoes a single
  source-fragment paragraph closely. Voice-true paraphrase that adds
  nothing the user has not already said.
* **Too ungrounded** — a majority of the draft's paragraphs have no
  conceptual anchor in any of the user's source fragments. Voice-true
  on the surface, conceptually invented underneath.

Both metrics are produced from the same paragraph-level cosine
similarity scan against the source corpus that fed the draft, so the
guard is deterministic, embedding-only, and adds no extra LLM hops to
the pipeline. The thresholds live in :class:`creek.config.DraftConfig`
under the ``draft:`` YAML section so an operator can calibrate them
against their own corpus without touching code.

Surfaces:

* The :class:`GroundingReport.summary_line` is printed to stderr by
  ``creek draft`` so the operator sees the verdict immediately.
* The draft's frontmatter records the two scalar scores plus a
  per-paragraph annotations list under the keys exported below — these
  are also what the ``draft-grounding`` lint check reads.

The module deliberately exposes its embedding callable so tests (and
future callers) can inject a deterministic stub instead of loading the
full sentence-transformer at unit-test time.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creek.config import DraftConfig

EmbeddingFn = Callable[[str], list[float]]
"""Signature for paragraph-level embedding callables.

The default implementation wraps :class:`creek.link.embeddings.EmbeddingLinker`
but tests inject a deterministic in-memory function so the guard's
math is exercised offline."""


DERIVATIVE_FRONTMATTER_KEY = "derivative_score"
"""Frontmatter key for the maximum paragraph similarity score."""

GROUNDING_FRONTMATTER_KEY = "grounding_score"
"""Frontmatter key for the grounded-paragraph fraction."""

PARAGRAPH_ANNOTATIONS_KEY = "paragraph_grounding"
"""Frontmatter key for the per-paragraph annotation list.

Each entry is a mapping ``{index, max_similarity, is_derivative,
is_grounded}`` so a reader can see *which* paragraph tripped a flag
without re-running the guard."""


@dataclass(frozen=True)
class GroundingThresholds:
    """Resolved grounding-guard thresholds, decoupled from Pydantic.

    The dataclass form keeps the score-computation code free of
    Pydantic imports (the grounding module is on the hot path during
    ``creek draft``) while still validating bounds at construction time.

    Attributes:
        derivative_upper: Cosine-similarity ceiling above which a draft
            paragraph is flagged as a paraphrase.
        grounding_lower: Cosine-similarity floor a draft paragraph must
            clear against *some* source paragraph to count as grounded.
            The same value gates the grounded-fraction summary: drafts
            whose grounded fraction sits below this value are flagged.
    """

    derivative_upper: float
    grounding_lower: float

    def __post_init__(self) -> None:
        """Reject thresholds outside ``[0.0, 1.0]`` at construction time."""
        if not 0.0 <= self.derivative_upper <= 1.0:
            msg = f"derivative_upper must be in [0.0, 1.0]; got {self.derivative_upper}"
            raise ValueError(msg)
        if not 0.0 <= self.grounding_lower <= 1.0:
            msg = f"grounding_lower must be in [0.0, 1.0]; got {self.grounding_lower}"
            raise ValueError(msg)

    @classmethod
    def from_config(cls, config: DraftConfig) -> GroundingThresholds:
        """Build thresholds from a :class:`~creek.config.DraftConfig` instance."""
        return cls(
            derivative_upper=config.derivative_upper,
            grounding_lower=config.grounding_lower,
        )


@dataclass(frozen=True)
class ParagraphScore:
    """Per-paragraph annotation surfaced in the frontmatter.

    Attributes:
        index: Zero-based position of the paragraph in the draft body.
        text: The paragraph's raw text — kept for human-readable lint
            findings and not echoed in the frontmatter payload to avoid
            doubling the file size.
        max_similarity: Highest cosine similarity recorded between this
            paragraph and *any* source-fragment paragraph.
        is_derivative: ``True`` when :attr:`max_similarity` met or
            exceeded :attr:`GroundingThresholds.derivative_upper`.
        is_grounded: ``True`` when :attr:`max_similarity` met or
            exceeded :attr:`GroundingThresholds.grounding_lower`.
    """

    index: int
    text: str
    max_similarity: float
    is_derivative: bool
    is_grounded: bool

    def to_annotation(self) -> dict[str, object]:
        """Return the frontmatter-friendly mapping for this paragraph.

        ``text`` is intentionally omitted; an operator who needs the
        paragraph body can read the draft itself. Keeping the
        annotations slim avoids ballooning every draft file with two
        copies of the body.
        """
        return {
            "index": self.index,
            "max_similarity": round(self.max_similarity, 4),
            "is_derivative": self.is_derivative,
            "is_grounded": self.is_grounded,
        }


@dataclass(frozen=True)
class GroundingReport:
    """The guard's verdict for one draft.

    Attributes:
        derivative_score: Maximum :attr:`ParagraphScore.max_similarity`
            across the draft. ``0.0`` when the draft has no paragraphs.
        grounding_score: Fraction of paragraphs whose ``max_similarity``
            cleared :attr:`GroundingThresholds.grounding_lower`. ``0.0``
            when the draft has no paragraphs.
        paragraph_scores: One :class:`ParagraphScore` per draft
            paragraph, in document order.
        thresholds: Echo of the thresholds the scores were computed
            against. Kept on the report so the lint check and the
            stderr summary can describe *why* a flag fired without
            re-loading the config.
    """

    derivative_score: float
    grounding_score: float
    paragraph_scores: tuple[ParagraphScore, ...]
    thresholds: GroundingThresholds
    # Deliberately field-less: the cached flag booleans are derived from
    # ``derivative_score`` and ``grounding_score`` so re-deserialising
    # the report (e.g. after a YAML round-trip in the lint check) never
    # produces a stale verdict. See :attr:`is_flagged_derivative`.
    _: tuple[()] = field(default=(), repr=False)

    @property
    def is_flagged_derivative(self) -> bool:
        """``True`` when at least one paragraph crossed the upper bound."""
        return self.derivative_score >= self.thresholds.derivative_upper

    @property
    def is_flagged_grounding(self) -> bool:
        """``True`` when the grounded fraction fell below the lower bound.

        Empty drafts (no paragraph scores) cannot be evaluated and so
        are never flagged — a paragraph-less body indicates an upstream
        bug, not a grounding failure.
        """
        if not self.paragraph_scores:
            return False
        return self.grounding_score < self.thresholds.grounding_lower

    @property
    def is_flagged(self) -> bool:
        """Either failure mode trips the overall flag."""
        return self.is_flagged_derivative or self.is_flagged_grounding

    def summary_line(self) -> str:
        """Return the stderr one-liner ``creek draft`` prints after composing.

        The string is human-tuned to be glanceable: scores up front, a
        bracketed verdict at the end. Stable wording is part of the
        contract because the walkthrough and the integration tests
        match on it.
        """
        verdict_parts: list[str] = []
        if self.is_flagged_derivative:
            verdict_parts.append("too derivative")
        if self.is_flagged_grounding:
            verdict_parts.append("too ungrounded")
        verdict = (
            f"flagged: {' + '.join(verdict_parts)}"
            if verdict_parts
            else "within bounds"
        )
        return (
            f"grounding guard: derivative={self.derivative_score:.2f} "
            f"(upper {self.thresholds.derivative_upper:.2f}), "
            f"grounding={self.grounding_score:.2f} "
            f"(lower {self.thresholds.grounding_lower:.2f}) — {verdict}"
        )

    def to_frontmatter(self) -> dict[str, object]:
        """Render the report as the frontmatter payload ``save_draft`` writes.

        The two scalar scores are rounded to four decimals so YAML diffs
        do not churn on the trailing-precision noise that
        sentence-transformer outputs naturally produce.
        """
        return {
            DERIVATIVE_FRONTMATTER_KEY: round(self.derivative_score, 4),
            GROUNDING_FRONTMATTER_KEY: round(self.grounding_score, 4),
            PARAGRAPH_ANNOTATIONS_KEY: [
                score.to_annotation() for score in self.paragraph_scores
            ],
        }


def split_paragraphs(body: str) -> list[str]:
    """Split *body* into non-empty paragraphs on blank-line boundaries.

    A paragraph is any run of non-blank lines separated from its
    neighbours by one or more blank lines. Surrounding whitespace is
    stripped but inner indentation is preserved so quoted or fenced
    blocks survive.
    """
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in body.splitlines():
        if line.strip():
            buffer.append(line)
            continue
        if buffer:
            paragraphs.append("\n".join(buffer).strip())
            buffer = []
    if buffer:
        paragraphs.append("\n".join(buffer).strip())
    return [p for p in paragraphs if p]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity in ``[-1.0, 1.0]`` for two equal-length vectors.

    Zero-norm vectors collapse to ``0.0`` rather than raising — the
    guard treats an embedding-less paragraph as "no resonance with
    anything" without aborting the score for the whole draft.
    """
    if len(a) != len(b):
        msg = f"Vector length mismatch: {len(a)} vs {len(b)}"
        raise ValueError(msg)
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if 0.0 in (norm_a, norm_b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (norm_a * norm_b)


def _score_one_paragraph(
    paragraph: str,
    source_vectors: list[list[float]],
    *,
    embedding_fn: EmbeddingFn,
    thresholds: GroundingThresholds,
    index: int,
) -> ParagraphScore:
    """Compute one :class:`ParagraphScore` against the pre-embedded sources."""
    if not source_vectors:
        return ParagraphScore(
            index=index,
            text=paragraph,
            max_similarity=0.0,
            is_derivative=False,
            is_grounded=False,
        )
    draft_vec = embedding_fn(paragraph)
    best = max(cosine_similarity(draft_vec, src) for src in source_vectors)
    return ParagraphScore(
        index=index,
        text=paragraph,
        max_similarity=best,
        is_derivative=best >= thresholds.derivative_upper,
        is_grounded=best >= thresholds.grounding_lower,
    )


def score_draft(
    draft_body: str,
    *,
    source_texts: Sequence[str],
    embedding_fn: EmbeddingFn,
    thresholds: GroundingThresholds,
) -> GroundingReport:
    """Compute a :class:`GroundingReport` for *draft_body*.

    Each paragraph of the draft is embedded once and compared against
    the embeddings of every paragraph extracted from *source_texts*.
    The maximum per-paragraph similarity drives both metrics:

    * The draft's ``derivative_score`` is the maximum across all
      paragraphs — one near-paraphrase trips the upper bound.
    * The draft's ``grounding_score`` is the fraction of paragraphs
      whose max similarity met or exceeded
      :attr:`GroundingThresholds.grounding_lower`.

    Args:
        draft_body: The composed draft text (frontmatter excluded).
        source_texts: Raw source-fragment bodies that fed the draft
            prompt. They are paragraph-split internally so the caller
            can pass whole fragment bodies without preprocessing.
        embedding_fn: Callable returning a vector for one text string.
            Tests inject a deterministic stub; production wires this
            to :func:`default_embedding_fn`.
        thresholds: Resolved guard thresholds. Echoed on the returned
            report.

    Returns:
        A populated :class:`GroundingReport`. When *draft_body* contains
        no paragraphs the scores collapse to ``0.0`` and no flags fire
        — there is nothing to evaluate.
    """
    draft_paragraphs = split_paragraphs(draft_body)
    if not draft_paragraphs:
        return GroundingReport(
            derivative_score=0.0,
            grounding_score=0.0,
            paragraph_scores=(),
            thresholds=thresholds,
        )

    source_paragraphs: list[str] = []
    for text in source_texts:
        source_paragraphs.extend(split_paragraphs(text))
    source_vectors = [embedding_fn(p) for p in source_paragraphs]

    scores = tuple(
        _score_one_paragraph(
            paragraph,
            source_vectors,
            embedding_fn=embedding_fn,
            thresholds=thresholds,
            index=index,
        )
        for index, paragraph in enumerate(draft_paragraphs)
    )

    derivative = max((s.max_similarity for s in scores), default=0.0)
    grounded_count = sum(1 for s in scores if s.is_grounded)
    grounding = grounded_count / len(scores)

    return GroundingReport(
        derivative_score=derivative,
        grounding_score=grounding,
        paragraph_scores=scores,
        thresholds=thresholds,
    )


def default_embedding_fn(
    config: object | None = None,
) -> EmbeddingFn:
    """Build the production embedding callable for the grounding guard.

    Wraps :class:`creek.link.embeddings.EmbeddingLinker` lazily so the
    sentence-transformer is loaded only when ``creek draft`` actually
    runs the guard — unit tests inject their own callable and never
    pay the import cost.

    Args:
        config: Optional :class:`~creek.config.EmbeddingsConfig`. When
            ``None`` the default Pydantic settings are used (``all-MiniLM-L6-v2``
            with the standard similarity threshold). Typed as ``object``
            to keep ``creek.link.embeddings`` an at-call-site import and
            avoid a top-level dependency cycle.

    Returns:
        A callable conforming to :data:`EmbeddingFn` that returns a
        single embedding vector per string.
    """
    from creek.config import EmbeddingsConfig
    from creek.link.embeddings import EmbeddingLinker

    cfg = config if isinstance(config, EmbeddingsConfig) else EmbeddingsConfig()
    linker = EmbeddingLinker(cfg)

    def _embed(text: str) -> list[float]:
        return linker.generate_embedding(text)

    return _embed
