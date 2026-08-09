"""Bidirectional grounding guard for ``creek draft``.

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

Both surfaces are conditional on the embedding model actually loading.
``creek draft`` and the MCP ``creek.draft`` tool wire the guard via
:func:`default_embedding_fn`, which loads a sentence-transformer on
first call; when that fails,
:meth:`creek.generate.drafts.DraftGenerator._disable_grounding` skips
the guard for the rest of the run, prints ``grounding guard skipped:``
to stderr, and the draft is saved with no scores rather than lost
(#1040).

The module deliberately exposes its embedding callable so tests (and
future callers) can inject a deterministic stub instead of loading the
full sentence-transformer at unit-test time.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, final

if TYPE_CHECKING:
    from creek.config import DraftConfig


class ParagraphAnnotation(TypedDict):
    """Frontmatter shape for a single paragraph's grounding annotation.

    The four fields mirror :class:`ParagraphScore` minus the raw text
    (which lives in the draft body itself). Declaring this as a
    ``TypedDict`` lets ``mypy --strict`` flag key typos and missing
    fields at the call site instead of waiting for a YAML-decode
    failure downstream.
    """

    index: int
    max_similarity: float
    is_derivative: bool
    is_grounded: bool


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
        grounding_lower: Per-paragraph cosine-similarity floor a draft
            paragraph must clear against *some* source paragraph to count
            as grounded.
        grounding_fraction_lower: Minimum acceptable fraction of grounded
            paragraphs across the whole draft. Independent of
            :attr:`grounding_lower`: the former decides whether one
            paragraph is grounded, the latter how many grounded
            paragraphs the draft as a whole must reach.
    """

    derivative_upper: float
    grounding_lower: float
    grounding_fraction_lower: float

    def __post_init__(self) -> None:
        """Reject thresholds outside ``[0.0, 1.0]`` at construction time."""
        if not 0.0 <= self.derivative_upper <= 1.0:
            msg = f"derivative_upper must be in [0.0, 1.0]; got {self.derivative_upper}"
            raise ValueError(msg)
        if not 0.0 <= self.grounding_lower <= 1.0:
            msg = f"grounding_lower must be in [0.0, 1.0]; got {self.grounding_lower}"
            raise ValueError(msg)
        if not 0.0 <= self.grounding_fraction_lower <= 1.0:
            msg = (
                "grounding_fraction_lower must be in [0.0, 1.0]; "
                f"got {self.grounding_fraction_lower}"
            )
            raise ValueError(msg)

    @classmethod
    def from_config(cls, config: DraftConfig) -> GroundingThresholds:
        """Build thresholds from a :class:`~creek.config.DraftConfig` instance."""
        return cls(
            derivative_upper=config.derivative_upper,
            grounding_lower=config.grounding_lower,
            grounding_fraction_lower=config.grounding_fraction_lower,
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

    def to_annotation(self) -> ParagraphAnnotation:
        """Return the frontmatter-friendly mapping for this paragraph.

        ``text`` is intentionally omitted; an operator who needs the
        paragraph body can read the draft itself. Keeping the
        annotations slim avoids ballooning every draft file with two
        copies of the body.
        """
        return ParagraphAnnotation(
            index=self.index,
            max_similarity=round(self.max_similarity, 4),
            is_derivative=self.is_derivative,
            is_grounded=self.is_grounded,
        )


@final
@dataclass(frozen=True)
class GroundingReport:
    """The guard's verdict for one draft.

    The flag booleans are always derived from ``derivative_score`` and
    ``grounding_score`` via :attr:`is_flagged_derivative` /
    :attr:`is_flagged_ungrounded` so a YAML round-trip in the lint
    check never produces a stale verdict. The ``@final`` decorator
    prevents subclasses from introducing fields that would silently
    bypass that derivation.

    Attributes:
        derivative_score: Maximum :attr:`ParagraphScore.max_similarity`
            across the draft. ``0.0`` when the draft has no paragraphs.
        grounding_score: Fraction of paragraphs whose ``max_similarity``
            cleared :attr:`GroundingThresholds.grounding_lower`. ``0.0``
            when the draft has no paragraphs. Compared against
            :attr:`GroundingThresholds.grounding_fraction_lower` to set
            the grounding flag.
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

    @property
    def is_flagged_derivative(self) -> bool:
        """``True`` when at least one paragraph crossed the upper bound."""
        return self.derivative_score >= self.thresholds.derivative_upper

    @property
    def is_flagged_grounding(self) -> bool:
        """``True`` when the grounded fraction fell below the fraction floor.

        Compares :attr:`grounding_score` against
        :attr:`GroundingThresholds.grounding_fraction_lower`, the
        whole-draft knob — distinct from the per-paragraph
        :attr:`GroundingThresholds.grounding_lower` that decided which
        paragraphs counted as grounded in the first place.

        Empty drafts (no paragraph scores) cannot be evaluated and so
        are never flagged — a paragraph-less body indicates an upstream
        bug, not a grounding failure.
        """
        if not self.paragraph_scores:
            return False
        return self.grounding_score < self.thresholds.grounding_fraction_lower

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
            f"(lower {self.thresholds.grounding_fraction_lower:.2f}) — {verdict}"
        )

    def to_frontmatter(self) -> dict[str, object]:
        """Render the report as the frontmatter payload ``save_draft`` writes.

        Delegates to :func:`build_grounding_frontmatter` so the
        ``Draft``-stored fields (which the lint check reads) and the
        live :class:`GroundingReport` (which the generator emits)
        serialise identically.
        """
        return build_grounding_frontmatter(
            derivative_score=self.derivative_score,
            grounding_score=self.grounding_score,
            paragraph_annotations=tuple(
                score.to_annotation() for score in self.paragraph_scores
            ),
        )


def build_grounding_frontmatter(
    *,
    derivative_score: float | None,
    grounding_score: float | None,
    paragraph_annotations: Sequence[ParagraphAnnotation],
) -> dict[str, object]:
    """Build the partial frontmatter payload for grounding guard fields.

    Single source of truth for how the three grounding fields appear in
    on-disk draft frontmatter: callers either pass a live
    :class:`GroundingReport` via :meth:`GroundingReport.to_frontmatter`
    or pass the previously-stored :class:`~creek.generate.drafts.Draft`
    fields via ``save_draft``. Either path produces the same shape so a
    rename of a frontmatter key only happens here.

    ``None`` scalars and empty annotation tuples are skipped so a
    pre-guard draft (saved before the guard ran) never grows zero-value
    fields that would mislead the lint check into running on them.

    Args:
        derivative_score: Scalar from a finished guard run, or ``None``
            when the guard did not run.
        grounding_score: Scalar from a finished guard run, or ``None``
            when the guard did not run.
        paragraph_annotations: Per-paragraph annotation entries. Pass
            an empty sequence (or ``()``) when the guard did not run.

    Returns:
        A dict carrying only the keys whose values are populated. The
        caller merges this into the rest of the frontmatter post.
    """
    payload: dict[str, object] = {}
    if derivative_score is not None:
        payload[DERIVATIVE_FRONTMATTER_KEY] = round(derivative_score, 4)
    if grounding_score is not None:
        payload[GROUNDING_FRONTMATTER_KEY] = round(grounding_score, 4)
    if paragraph_annotations:
        payload[PARAGRAPH_ANNOTATIONS_KEY] = list(paragraph_annotations)
    return payload


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


# ---------------------------------------------------------------------------
# Sentence-level biographical grounding (issue #515)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
"""Boundary between sentences: whitespace following terminal punctuation.

Conservative on purpose — this regex itself never splits on a bare newline or
a comma, so a multi-clause biographical sentence ("Not the LDS Christ I was
handed as a kid—the one I actually believe in.") is scored as one unit rather
than fragmented into clauses that each lose their grounding context.

Note: newline handling is performed before this regex ever runs, in the
soft-wrap join pass of :func:`split_sentences`. Consecutive non-blank lines
inside one paragraph are folded into a single flowing block (joined on a
space) first, so a sentence soft-wrapped across two markdown lines reaches
this regex whole rather than truncated at the newline."""


_STRUCTURAL_LINE_RE = re.compile(r"^(?:#{1,6}\s|\s*(?:[-*+]|\d+\.)\s)")
"""A markdown line that is structural, not flowing prose.

Matches an ATX heading (``# `` … ``###### ``) or a list item (``- ``, ``* ``,
``+ ``, or ``1. ``). Such a line is a hard boundary: it is never folded into an
adjacent text line by the soft-wrap join, so a heading stays dropped and a list
marker stays separate from the paragraph beside it."""


_BIOGRAPHICAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # First-person upbringing / past-state markers, deliberately narrow so
    # ordinary first-person prose never trips the guard. A bare "I was wrong"
    # or "I had an idea" is opinion, not biography — so "i was" / "i had" only
    # qualify in specific biographical forms (raised, born, brought up, handed,
    # a <kid/child/...>). The bare "i was brought" and non-first-person
    # "growing up" forms were removed (#519): they matched non-biographical
    # idioms ("brought here by my editor", "growing up shapes identity") that
    # the narrower "i was brought up" / "i grew up" patterns already cover.
    # Childhood time-markers ("as a kid", "when i was") round out the set.
    # Opinions ("I think", "I want") never match.
    re.compile(r"\bi was raised\b", re.IGNORECASE),
    re.compile(r"\bi was born\b", re.IGNORECASE),
    re.compile(r"\bi was brought up\b", re.IGNORECASE),
    re.compile(r"\bi was handed\b", re.IGNORECASE),
    re.compile(
        r"\bi was an? (?:kid|child|boy|girl|teenager|teen"
        r"|baby|infant|toddler|youngster)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bi grew up\b", re.IGNORECASE),
    re.compile(r"\bi used to\b", re.IGNORECASE),
    re.compile(r"\bas a kid\b", re.IGNORECASE),
    re.compile(r"\bas a child\b", re.IGNORECASE),
    re.compile(r"\bwhen i was\b", re.IGNORECASE),
    re.compile(r"\bmy childhood\b", re.IGNORECASE),
    re.compile(r"\bmy upbringing\b", re.IGNORECASE),
)
"""First-person biographical surface markers (issue #515).

A sentence matching any of these is treated as asserting a biographical fact
about the owner — an upbringing, a birth, a childhood state, a past habit —
that must trace to a source. The patterns are deliberately narrow: bare
``i was`` / ``i had`` are excluded because they cover ordinary first-person
prose ("I was wrong", "I had an idea"); only their genuinely biographical
forms (``i was raised/born/brought up/handed``, ``i was a kid``, ``i grew
up``, ``i used to``) and childhood time-markers (``as a kid``, ``when i was``,
``my childhood/upbringing``) qualify. The bare ``i was brought`` and
non-first-person ``growing up`` were dropped (#519) as false-positive prone:
they matched non-biographical idioms ("brought here by my editor", "growing up
shapes identity") already covered by ``i was brought up`` / ``i grew up``.
Opinions ("I think", "I believe", "I love") deliberately never match: those
are voice, not unverifiable biography."""


@final
@dataclass(frozen=True)
class BiographicalGroundingFinding:
    """One first-person biographical sentence that no source supports.

    Attributes:
        sentence: The exact biographical sentence flagged, verbatim from the
            draft body, so the operator (or a downstream finding) can quote it.
        max_similarity: Highest cosine similarity recorded between the
            sentence and any source paragraph (sources + the voice-core brief).
            ``0.0`` when there were no sources to score against.
    """

    sentence: str
    max_similarity: float


def _logical_blocks(body: str) -> list[str]:
    """Fold *body*'s physical lines into flowing logical blocks.

    Consecutive non-blank prose lines inside one paragraph are a single
    soft-wrapped sentence-or-more and are joined on a space. A blank line is a
    hard paragraph boundary that flushes the current block, and a structural
    line — a heading or list marker (:data:`_STRUCTURAL_LINE_RE`) — is its own
    boundary that is dropped from the prose stream rather than folded into a
    neighbour.
    """
    blocks: list[str] = []
    buffer: list[str] = []

    def _flush() -> None:
        if buffer:
            blocks.append(" ".join(buffer))
            buffer.clear()

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or _STRUCTURAL_LINE_RE.match(stripped):
            _flush()
            continue
        buffer.append(stripped)
    _flush()
    return blocks


def split_sentences(body: str) -> list[str]:
    """Split *body* into trimmed, non-empty sentences.

    Sentences are separated on whitespace following terminal punctuation
    (``.``, ``!``, ``?``); blank lines, headings, and list markers collapse
    away. The split is intentionally coarse — its only job is to isolate a
    first-person biographical claim from the rest of an otherwise on-topic
    paragraph so the grounding scan scores the claim, not the paragraph that
    hides it.

    Soft-wrapped lines are folded into logical blocks *before* splitting (see
    :func:`_logical_blocks`): consecutive non-blank prose lines inside one
    paragraph join on a space, so a sentence soft-wrapped across two markdown
    lines reaches :data:`_SENTENCE_SPLIT_RE` whole rather than truncated at the
    newline. Blank lines remain hard paragraph boundaries, and a heading or
    list marker is its own boundary that never merges into adjacent prose.
    """
    sentences: list[str] = []
    for block in _logical_blocks(body):
        sentences.extend(
            part.strip() for part in _SENTENCE_SPLIT_RE.split(block) if part.strip()
        )
    return sentences


def is_biographical_sentence(sentence: str) -> bool:
    """Return whether *sentence* asserts a first-person biographical fact.

    Conservative heuristic (issue #515): only genuinely biographical
    upbringing / childhood / past-state forms match — "I was raised", "I was
    born", "I was handed", "I grew up", "I used to", "I was a kid", and
    childhood time-markers like "as a kid" or "when I was" (see
    :data:`_BIOGRAPHICAL_PATTERNS`). Ordinary first-person prose ("I was
    wrong", "I had an idea") and bare opinions ("I think the world is cruel")
    deliberately never match, which keeps grounded first-person voice from
    tripping the guard.
    """
    return any(pattern.search(sentence) for pattern in _BIOGRAPHICAL_PATTERNS)


def scan_biographical_sentences(
    body: str,
    *,
    source_texts: Sequence[str],
    embedding_fn: EmbeddingFn,
    threshold: float,
) -> list[BiographicalGroundingFinding]:
    """Flag first-person biographical sentences ungrounded in any source.

    For each sentence in *body* that :func:`is_biographical_sentence`
    matches, embed it and take its maximum cosine similarity against every
    paragraph of *source_texts* (the source fragments plus the voice-core
    brief). A sentence whose best similarity falls *below* *threshold* is
    surfaced as a :class:`BiographicalGroundingFinding`.

    The scan reuses the same cosine machinery as :func:`score_draft` but at
    sentence granularity, so a single invented first-person claim riding
    inside an otherwise on-topic paragraph is caught (the paragraph-level
    guard misses it). It adds no LLM hop — only embedding calls.

    Args:
        body: The composed draft / review body (frontmatter excluded).
        source_texts: Source-fragment bodies plus any voice-core brief text
            the claim may legitimately trace to. Paragraph-split internally.
        embedding_fn: Callable returning a vector for one text string.
        threshold: Cosine floor a biographical sentence must clear against
            some source paragraph to count as grounded. Typically the
            configured per-paragraph ``grounding_lower``.

    Returns:
        One finding per ungrounded biographical sentence, in document order.
        An empty list when no biographical sentence is below threshold.

    Raises:
        GroundingDimensionError: When *embedding_fn* returns vectors of
            differing lengths for sentence and source paragraphs.
    """
    candidates = [s for s in split_sentences(body) if is_biographical_sentence(s)]
    if not candidates:
        return []
    source_paragraphs: list[str] = []
    for text in source_texts:
        source_paragraphs.extend(split_paragraphs(text))
    source_vectors = [embedding_fn(p) for p in source_paragraphs]
    findings: list[BiographicalGroundingFinding] = []
    for sentence in candidates:
        if source_vectors:
            sentence_vec = embedding_fn(sentence)
            best = max(cosine_similarity(sentence_vec, src) for src in source_vectors)
        else:
            best = 0.0
        if best < threshold:
            findings.append(
                BiographicalGroundingFinding(sentence=sentence, max_similarity=best)
            )
    return findings


_NORM_EPSILON = 1e-12
"""Norm floor below which a vector is treated as zero-norm.

An exact-zero check (``norm == 0.0``) only catches an all-zeros vector;
a degenerate near-zero embedding (sum-of-squares ≈ 1e-30) would slip
through and divide into a large but finite — and meaningless — cosine.
Collapsing any sub-epsilon norm to "no resonance" keeps the guard's
output bounded for every embedding the public :data:`EmbeddingFn`
injection point might produce."""


class GroundingDimensionError(ValueError):
    """Raised when draft and source embeddings have mismatched dimensions.

    The guard compares draft-paragraph vectors against source-paragraph
    vectors with :func:`cosine_similarity`, which requires equal-length
    inputs. A length mismatch means the injected :data:`EmbeddingFn`
    returned vectors of different sizes for different texts — almost
    always two different embedding models on the two code paths. Catching
    the low-level length error here and re-raising this subclass turns a
    bare ``Vector length mismatch: 384 vs 768`` into an actionable
    message at the guard boundary instead of a raw traceback from deep in
    the scoring loop.
    """


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity in ``[-1.0, 1.0]`` for two equal-length vectors.

    Vectors whose norm falls below :data:`_NORM_EPSILON` collapse to
    ``0.0`` rather than raising or dividing — the guard treats an
    embedding-less (or degenerate near-zero) paragraph as "no resonance
    with anything" without aborting the score for the whole draft.

    Raises:
        GroundingDimensionError: When *a* and *b* have different lengths.
    """
    if len(a) != len(b):
        msg = f"Vector length mismatch: {len(a)} vs {len(b)}"
        raise GroundingDimensionError(msg)
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < _NORM_EPSILON or norm_b < _NORM_EPSILON:
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

    Raises:
        GroundingDimensionError: When the injected ``embedding_fn``
            returns vectors of differing lengths for draft and source
            paragraphs — surfaced as a clean message rather than a raw
            length-mismatch traceback from inside the scoring loop.
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

    try:
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
    except GroundingDimensionError as exc:
        msg = (
            "grounding guard could not score the draft: the embedding "
            "function returned vectors of differing lengths for draft and "
            "source paragraphs. This usually means two different embedding "
            f"models were used on the same run ({exc})."
        )
        raise GroundingDimensionError(msg) from exc

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
