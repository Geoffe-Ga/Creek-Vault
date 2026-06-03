"""Tests for the bidirectional grounding guard (issue #355).

The guard surfaces two failure modes after a draft is generated:

* **Too derivative** — at least one draft paragraph is a near-paraphrase
  of a single source-fragment paragraph (cosine similarity above the
  ``derivative_upper`` threshold).
* **Too ungrounded** — fewer than ``grounding_lower`` fraction of draft
  paragraphs have *any* source-fragment paragraph above the
  ``grounding_lower`` similarity floor.

Both signals come from the same paragraph-level cosine-similarity scan;
the tests pin the math, threshold plumbing, frontmatter shape, and
lint-check surface against synthetic fixtures so the behaviour stays
deterministic regardless of which sentence-transformer model is
configured. A deterministic injected embedding callable keeps every
test offline and reproducible.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

import pytest

from creek.config import DraftConfig
from creek.generate.grounding import (
    DERIVATIVE_FRONTMATTER_KEY,
    GROUNDING_FRONTMATTER_KEY,
    PARAGRAPH_ANNOTATIONS_KEY,
    EmbeddingFn,
    GroundingDimensionError,
    GroundingReport,
    GroundingThresholds,
    ParagraphScore,
    cosine_similarity,
    score_draft,
    split_paragraphs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# ---------------------------------------------------------------------------
# Deterministic embedding helpers
# ---------------------------------------------------------------------------


_VECTOR_DIM = 32
"""Dimensionality of the synthetic embeddings used in this file.

Small enough to keep the test deterministic without numpy precision
quirks; large enough that hashing-based vectors for distinct strings
are reliably near-orthogonal."""


def _hashed_vector(text: str) -> list[float]:
    """Return a deterministic, normalised vector keyed off *text*'s hash.

    Two identical strings always map to the same vector; two distinct
    strings map to near-orthogonal vectors, so cosine similarity
    cleanly separates "same paragraph" from "different paragraph"
    without depending on a real embedding model.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Stretch the 32-byte digest across the vector by hashing again
    # with a counter; this keeps the math entirely in-stdlib while
    # giving enough independent entropy to avoid accidental overlap.
    raw: list[int] = []
    counter = 0
    while len(raw) < _VECTOR_DIM:
        chunk = hashlib.sha256(digest + counter.to_bytes(2, "big")).digest()
        raw.extend(chunk[: _VECTOR_DIM - len(raw)])
        counter += 1
    centred = [b - 127.5 for b in raw[:_VECTOR_DIM]]
    norm = math.sqrt(sum(x * x for x in centred)) or 1.0
    return [x / norm for x in centred]


def _blended_vector(parts: Sequence[str], weights: Sequence[float]) -> list[float]:
    """Return a normalised convex combination of hashed vectors for *parts*.

    Used by tests that need to simulate "draft paragraph echoes source
    paragraph at strength W" without hand-tuning floating-point
    coordinates. The blend is convex (weights sum to 1) so the result
    sits on the unit sphere after normalisation.
    """
    vectors = [_hashed_vector(part) for part in parts]
    blended = [0.0] * _VECTOR_DIM
    for vec, weight in zip(vectors, weights, strict=True):
        for i, value in enumerate(vec):
            blended[i] += weight * value
    norm = math.sqrt(sum(x * x for x in blended)) or 1.0
    return [x / norm for x in blended]


def _fixture_embedder(table: dict[str, list[float]]) -> EmbeddingFn:
    """Build an :class:`EmbeddingFn` backed by a deterministic lookup *table*.

    Strings absent from *table* fall through to :func:`_hashed_vector`,
    which keeps every test free to add an explicit blend for the
    paragraphs whose similarity it cares about and leave the rest as
    "noise" without enumerating every distractor.
    """

    def _embed(text: str) -> list[float]:
        return table.get(text) or _hashed_vector(text)

    return _embed


# ---------------------------------------------------------------------------
# Paragraph splitting + cosine helper
# ---------------------------------------------------------------------------


class TestSplitParagraphs:
    """``split_paragraphs`` underpins both scores; pin its shape."""

    def test_splits_on_blank_lines(self) -> None:
        """A blank-line-separated body produces one entry per paragraph."""
        body = (
            "Para one.\n\nPara two with two sentences. Still para two.\n\nPara three."
        )
        assert split_paragraphs(body) == [
            "Para one.",
            "Para two with two sentences. Still para two.",
            "Para three.",
        ]

    def test_skips_empty_segments(self) -> None:
        """Triple-blank or trailing newlines must not yield empty paragraphs."""
        body = "\n\nAlpha.\n\n\n\nBeta.\n\n\n"
        assert split_paragraphs(body) == ["Alpha.", "Beta."]

    def test_strips_surrounding_whitespace(self) -> None:
        """Indented paragraphs lose only the outer whitespace, not inner runs."""
        body = "  Alpha\n  trailing.\n\n   Beta   "
        assert split_paragraphs(body) == ["Alpha\n  trailing.", "Beta"]

    def test_returns_empty_for_blank_body(self) -> None:
        """A body of only whitespace produces no paragraphs at all."""
        assert split_paragraphs("   \n\n\t\n") == []


class TestCosineSimilarity:
    """The guard's math is paragraph-level cosine; pin the helper."""

    def test_identical_vectors_score_one(self) -> None:
        """Cosine of a vector with itself is exactly 1.0."""
        vec = _hashed_vector("alpha")
        assert cosine_similarity(vec, vec) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_vectors_score_zero(self) -> None:
        """Two unit vectors on perpendicular axes have cosine 0.0."""
        zeros = [0.0] * _VECTOR_DIM
        a = zeros.copy()
        b = zeros.copy()
        a[0] = 1.0
        b[1] = 1.0
        assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_zero_vector_returns_zero(self) -> None:
        """A zero-norm vector cannot resonate; cosine collapses to 0.0."""
        zeros = [0.0] * _VECTOR_DIM
        other = _hashed_vector("anything")
        assert cosine_similarity(zeros, other) == 0.0
        assert cosine_similarity(other, zeros) == 0.0

    def test_near_zero_vector_returns_zero(self) -> None:
        """A near-zero (norm ~1e-30) vector collapses to 0.0, not a huge cosine.

        An exact-zero check would let a sum-of-squares ≈ 1e-30 vector
        divide into an enormous but finite cosine; the epsilon floor
        treats it as "no resonance" instead.
        """
        # Norm = sqrt(_VECTOR_DIM) * 1e-30 ≈ 5.7e-30, far below the
        # 1e-12 epsilon — but emphatically not exactly zero.
        tiny = [1e-30] * _VECTOR_DIM
        unit = _hashed_vector("a normal paragraph")
        assert cosine_similarity(tiny, unit) == 0.0
        assert cosine_similarity(unit, tiny) == 0.0

    def test_length_mismatch_raises_clean_error(self) -> None:
        """Unequal-length vectors raise :class:`GroundingDimensionError`."""
        with pytest.raises(GroundingDimensionError, match="length mismatch"):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# DraftConfig wiring
# ---------------------------------------------------------------------------


class TestDraftConfig:
    """Thresholds must be configurable through the ``draft:`` YAML section."""

    def test_defaults_match_issue(self) -> None:
        """The defaults are 0.85 / 0.30 / 0.30 (backwards-compatible)."""
        cfg = DraftConfig()
        assert cfg.derivative_upper == pytest.approx(0.85)
        assert cfg.grounding_lower == pytest.approx(0.30)
        assert cfg.grounding_fraction_lower == pytest.approx(0.30)

    def test_thresholds_are_clamped_to_unit_interval(self) -> None:
        """Cosine-similarity thresholds must stay in [0.0, 1.0]."""
        with pytest.raises(ValueError, match="derivative_upper"):
            DraftConfig(derivative_upper=1.5)
        with pytest.raises(ValueError, match="grounding_lower"):
            DraftConfig(grounding_lower=-0.1)
        with pytest.raises(ValueError, match="grounding_fraction_lower"):
            DraftConfig(grounding_fraction_lower=1.1)

    def test_thresholds_can_be_overridden(self) -> None:
        """Operators must be able to tighten or loosen all three knobs."""
        cfg = DraftConfig(
            derivative_upper=0.9,
            grounding_lower=0.5,
            grounding_fraction_lower=0.2,
        )
        assert cfg.derivative_upper == pytest.approx(0.9)
        assert cfg.grounding_lower == pytest.approx(0.5)
        assert cfg.grounding_fraction_lower == pytest.approx(0.2)

    def test_grounding_knobs_are_independent(self) -> None:
        """The per-paragraph floor and the fraction floor set independently."""
        cfg = DraftConfig(grounding_lower=0.4, grounding_fraction_lower=0.2)
        assert cfg.grounding_lower == pytest.approx(0.4)
        assert cfg.grounding_fraction_lower == pytest.approx(0.2)

    def test_max_tokens_defaults_to_none(self) -> None:
        """``max_tokens`` defaults to None so the provider default is kept."""
        assert DraftConfig().max_tokens is None

    def test_max_tokens_must_be_positive(self) -> None:
        """A non-positive ``max_tokens`` is rejected at config-parse time."""
        with pytest.raises(ValueError, match="max_tokens"):
            DraftConfig(max_tokens=0)

    def test_max_tokens_can_be_set(self) -> None:
        """Operators may pin a default token ceiling for longer drafts."""
        assert DraftConfig(max_tokens=4096).max_tokens == 4096

    def test_cohesion_defaults_off(self) -> None:
        """The cohesion pass must default off so merging changes nothing."""
        assert DraftConfig().cohesion is False

    def test_cohesion_can_be_enabled(self) -> None:
        """Operators may persist the opt-in cohesion pass in ``draft:`` config."""
        assert DraftConfig(cohesion=True).cohesion is True

    def test_creekconfig_exposes_draft_section(self) -> None:
        """The top-level ``draft:`` section must hang off ``CreekConfig``."""
        from creek.config import CreekConfig

        cfg = CreekConfig()
        assert isinstance(cfg.draft, DraftConfig)
        assert cfg.draft.derivative_upper == pytest.approx(0.85)

    def test_thresholds_from_config(self) -> None:
        """``GroundingThresholds.from_config`` mirrors the :class:`DraftConfig`."""
        thresholds = GroundingThresholds.from_config(
            DraftConfig(
                derivative_upper=0.72,
                grounding_lower=0.45,
                grounding_fraction_lower=0.25,
            ),
        )
        assert thresholds.derivative_upper == pytest.approx(0.72)
        assert thresholds.grounding_lower == pytest.approx(0.45)
        assert thresholds.grounding_fraction_lower == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# score_draft
# ---------------------------------------------------------------------------


_DEFAULT_THRESHOLDS = GroundingThresholds(
    derivative_upper=0.85,
    grounding_lower=0.30,
    grounding_fraction_lower=0.30,
)


class TestScoreDraftDerivative:
    """A draft that paraphrases a single source must score above the upper bound."""

    def test_paraphrase_heavy_draft_is_flagged(self) -> None:
        """A draft where every paragraph echoes one source scores ~1.0 derivative."""
        source_para = "The compost folder catches abandoned drafts."
        another_source = "Threads bind fragments into narrative currents."
        # Each draft paragraph is the exact text of a source paragraph;
        # the embedder maps them to the same vector, so cosine = 1.0.
        draft_body = f"{source_para}\n\n{source_para}\n\n{another_source}"
        table = {
            source_para: _hashed_vector(source_para),
            another_source: _hashed_vector(another_source),
        }
        report = score_draft(
            draft_body,
            source_texts=[source_para, another_source],
            embedding_fn=_fixture_embedder(table),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert report.derivative_score == pytest.approx(1.0, abs=1e-6)
        assert report.is_flagged_derivative is True
        # All paragraphs are perfect echoes — grounding is also at 1.0.
        assert report.grounding_score == pytest.approx(1.0, abs=1e-6)
        assert report.is_flagged_grounding is False
        assert report.is_flagged is True

    def test_partial_paraphrase_picked_up_by_max(self) -> None:
        """Even one near-paraphrase paragraph trips the derivative flag."""
        echoed_source = "Resonances connect fragments by meaning."
        novel_para = "An entirely new musing about composting."
        unrelated_source = "Eddies cluster topics that recur."
        # Two of three draft paragraphs are unrelated; one mirrors a
        # source exactly. The derivative score is the *max*, so the
        # flag fires even though most paragraphs are fine.
        draft_body = f"{novel_para}\n\n{echoed_source}\n\nanother novel line"
        table = {
            echoed_source: _hashed_vector(echoed_source),
            unrelated_source: _hashed_vector(unrelated_source),
            novel_para: _hashed_vector(novel_para),
            "another novel line": _hashed_vector("another novel line"),
        }
        report = score_draft(
            draft_body,
            source_texts=[echoed_source, unrelated_source],
            embedding_fn=_fixture_embedder(table),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert report.derivative_score == pytest.approx(1.0, abs=1e-6)
        assert report.is_flagged_derivative is True


class TestScoreDraftGrounding:
    """A draft built from voice signatures with no real source must trip grounding."""

    def test_invented_draft_is_flagged_ungrounded(self) -> None:
        """Three invented paragraphs, no source overlap → grounding = 0.0."""
        # Sources exist, but the draft paragraphs do not echo them at all.
        sources = ["source one body", "source two body"]
        draft_body = (
            "An invented opening untouched by any source.\n\n"
            "A second invented paragraph still untouched.\n\n"
            "And a third invented closing."
        )
        report = score_draft(
            draft_body,
            source_texts=sources,
            embedding_fn=_fixture_embedder({}),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        # Synthetic hashes are near-orthogonal, so grounded fraction = 0.
        assert report.grounding_score == pytest.approx(0.0, abs=1e-6)
        assert report.is_flagged_grounding is True
        assert report.is_flagged is True
        # Derivative score is the max similarity — well below the upper bound.
        assert report.derivative_score < _DEFAULT_THRESHOLDS.derivative_upper

    def test_grounded_fraction_uses_lower_threshold(self) -> None:
        """A paragraph counts as grounded once it crosses ``grounding_lower``."""
        anchored_source = "Anchored source paragraph"
        draft_paragraph_close = "Mostly the anchored source paragraph"
        draft_paragraph_far = "Totally unrelated other content"
        # Blend the close paragraph 80% toward the source so its cosine
        # similarity comfortably clears the 0.30 lower bound without
        # touching the 0.85 upper bound.
        table = {
            anchored_source: _hashed_vector(anchored_source),
            draft_paragraph_close: _blended_vector(
                [anchored_source, draft_paragraph_close],
                [0.8, 0.2],
            ),
            draft_paragraph_far: _hashed_vector(draft_paragraph_far),
        }
        draft_body = f"{draft_paragraph_close}\n\n{draft_paragraph_far}"
        report = score_draft(
            draft_body,
            source_texts=[anchored_source],
            embedding_fn=_fixture_embedder(table),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        # One of two paragraphs is grounded → fraction = 0.5.
        assert report.grounding_score == pytest.approx(0.5, abs=1e-6)
        # 0.5 is above the 0.30 fraction floor → no grounding flag.
        assert report.is_flagged_grounding is False


class TestGroundingKnobIndependence:
    """The per-paragraph floor and the fraction floor act independently."""

    def _half_grounded_report(
        self,
        thresholds: GroundingThresholds,
    ) -> GroundingReport:
        """Score a two-paragraph draft where exactly one paragraph is grounded.

        The close paragraph blends 80% toward its source (cosine well
        above 0.30 yet below 0.85); the far paragraph is orthogonal. So
        the grounded *fraction* is exactly 0.5 regardless of the
        fraction floor under test.
        """
        anchored_source = "Anchored source paragraph for independence"
        close = "Mostly the anchored source paragraph for independence"
        far = "Totally unrelated independence content"
        table = {
            anchored_source: _hashed_vector(anchored_source),
            close: _blended_vector([anchored_source, close], [0.8, 0.2]),
            far: _hashed_vector(far),
        }
        return score_draft(
            f"{close}\n\n{far}",
            source_texts=[anchored_source],
            embedding_fn=_fixture_embedder(table),
            thresholds=thresholds,
        )

    def test_fraction_floor_flags_when_per_paragraph_floor_passes(self) -> None:
        """Every grounded paragraph clears the per-paragraph floor, yet the
        grounded fraction (0.5) sits below a strict fraction floor (0.6) → flag."""
        thresholds = GroundingThresholds(
            derivative_upper=0.85,
            grounding_lower=0.30,
            grounding_fraction_lower=0.60,
        )
        report = self._half_grounded_report(thresholds)
        assert report.grounding_score == pytest.approx(0.5, abs=1e-6)
        # 0.5 < 0.60 fraction floor → flagged even though the grounded
        # paragraph individually cleared the 0.30 per-paragraph floor.
        assert report.is_flagged_grounding is True

    def test_lenient_fraction_floor_passes_same_draft(self) -> None:
        """The identical 0.5-grounded draft passes under a lenient fraction floor.

        Holding the per-paragraph floor fixed and only loosening the
        fraction floor flips the verdict — proving the fraction knob is
        what drives the flag, independent of the per-paragraph knob.
        """
        thresholds = GroundingThresholds(
            derivative_upper=0.85,
            grounding_lower=0.30,
            grounding_fraction_lower=0.40,
        )
        report = self._half_grounded_report(thresholds)
        assert report.grounding_score == pytest.approx(0.5, abs=1e-6)
        # 0.5 ≥ 0.40 fraction floor → not flagged.
        assert report.is_flagged_grounding is False

    def test_per_paragraph_floor_changes_fraction_independently(self) -> None:
        """Tightening only the per-paragraph floor drops a paragraph out of
        the grounded set, flipping the verdict while the fraction floor holds."""
        # With grounding_lower=0.30 the close paragraph (cosine ~0.97) is
        # grounded → fraction 0.5 ≥ 0.40 → not flagged.
        lenient_para = GroundingThresholds(
            derivative_upper=0.99,
            grounding_lower=0.30,
            grounding_fraction_lower=0.40,
        )
        assert self._half_grounded_report(lenient_para).is_flagged_grounding is False
        # Raising the per-paragraph floor above the close paragraph's
        # cosine (~0.97) un-grounds it → fraction 0.0 < 0.40 → flagged,
        # even though the fraction floor never moved.
        strict_para = GroundingThresholds(
            derivative_upper=0.99,
            grounding_lower=0.98,
            grounding_fraction_lower=0.40,
        )
        strict_report = self._half_grounded_report(strict_para)
        assert strict_report.grounding_score == pytest.approx(0.0, abs=1e-6)
        assert strict_report.is_flagged_grounding is True


class TestScoreDraftDimensionMismatch:
    """A mismatched embedding callable surfaces a clean guard error."""

    def test_dimension_mismatch_raises_clean_grounding_error(self) -> None:
        """Vectors of differing lengths abort with an actionable message.

        Simulates two embedding models on one run: source paragraphs get
        a 4-dim vector, draft paragraphs a 3-dim vector. Rather than a
        raw ``Vector length mismatch`` traceback from deep in the loop,
        the guard raises :class:`GroundingDimensionError` with a message
        an operator can act on.
        """

        def _ragged_embed(text: str) -> list[float]:
            return [1.0, 0.0, 0.0, 0.0] if text == "src para" else [1.0, 0.0, 0.0]

        with pytest.raises(GroundingDimensionError) as excinfo:
            score_draft(
                "draft para",
                source_texts=["src para"],
                embedding_fn=_ragged_embed,
                thresholds=_DEFAULT_THRESHOLDS,
            )
        message = str(excinfo.value)
        assert "differing lengths" in message
        assert "embedding" in message.lower()


class TestScoreDraftBalanced:
    """The happy path — every paragraph grounded, none paraphrased — must pass."""

    def test_balanced_draft_passes_both_thresholds(self) -> None:
        """Recombination above ``grounding_lower`` and below ``derivative_upper``."""
        sources = [
            "Source one about composting drafts",
            "Source two about thread emergence",
        ]
        # Every draft paragraph is a 0.5/0.5 mix of one source and a
        # novel string — both grounded and novel.
        balanced_a = "Balanced paragraph alpha"
        balanced_b = "Balanced paragraph beta"
        table = {
            sources[0]: _hashed_vector(sources[0]),
            sources[1]: _hashed_vector(sources[1]),
            balanced_a: _blended_vector([sources[0], balanced_a], [0.5, 0.5]),
            balanced_b: _blended_vector([sources[1], balanced_b], [0.5, 0.5]),
        }
        draft_body = f"{balanced_a}\n\n{balanced_b}"
        report = score_draft(
            draft_body,
            source_texts=sources,
            embedding_fn=_fixture_embedder(table),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert report.grounding_score == pytest.approx(1.0, abs=1e-6)
        # 0.5-blend cosine sits around 0.5 — between the two thresholds.
        assert _DEFAULT_THRESHOLDS.grounding_lower < report.derivative_score
        assert report.derivative_score < _DEFAULT_THRESHOLDS.derivative_upper
        assert report.is_flagged is False


class TestScoreDraftEdgeCases:
    """Empty bodies and missing sources must degrade safely."""

    def test_no_paragraphs_returns_zero_scores(self) -> None:
        """An empty body produces a report with both scores at 0.0 and no flags."""
        report = score_draft(
            "",
            source_texts=["any"],
            embedding_fn=_fixture_embedder({}),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert report.derivative_score == 0.0
        assert report.grounding_score == 0.0
        assert report.paragraph_scores == ()
        assert report.is_flagged is False

    def test_no_sources_flags_grounding_only(self) -> None:
        """Zero source paragraphs → grounding_score = 0 and the grounding flag fires."""
        report = score_draft(
            "A lone paragraph.",
            source_texts=[],
            embedding_fn=_fixture_embedder({}),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert report.derivative_score == 0.0
        assert report.grounding_score == 0.0
        assert report.is_flagged_grounding is True
        assert report.is_flagged_derivative is False


# ---------------------------------------------------------------------------
# Frontmatter shape + summary line
# ---------------------------------------------------------------------------


class TestReportSerialisation:
    """Exact shape of the frontmatter additions the issue calls out."""

    def test_to_frontmatter_has_expected_keys(self) -> None:
        """The mapping contains every documented frontmatter key."""
        report = GroundingReport(
            derivative_score=0.42,
            grounding_score=0.75,
            paragraph_scores=(
                ParagraphScore(
                    index=0,
                    text="alpha",
                    max_similarity=0.42,
                    is_derivative=False,
                    is_grounded=True,
                ),
            ),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        payload = report.to_frontmatter()
        assert payload[DERIVATIVE_FRONTMATTER_KEY] == pytest.approx(0.42)
        assert payload[GROUNDING_FRONTMATTER_KEY] == pytest.approx(0.75)
        annotations = payload[PARAGRAPH_ANNOTATIONS_KEY]
        assert isinstance(annotations, list)
        assert annotations[0]["index"] == 0
        assert annotations[0]["max_similarity"] == pytest.approx(0.42)
        assert annotations[0]["is_derivative"] is False
        assert annotations[0]["is_grounded"] is True

    def test_summary_line_includes_both_scores(self) -> None:
        """The stderr one-liner must echo both scores and the flag verdict."""
        report = GroundingReport(
            derivative_score=0.91,
            grounding_score=0.12,
            paragraph_scores=(),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        line = report.summary_line()
        assert "derivative" in line.lower()
        assert "grounding" in line.lower()
        assert "0.91" in line
        assert "0.12" in line
        # The summary must mention that the draft was flagged.
        assert "flag" in line.lower()

    def test_summary_line_passes_when_balanced(self) -> None:
        """A balanced draft's summary must not claim a flag fired."""
        report = GroundingReport(
            derivative_score=0.4,
            grounding_score=0.8,
            paragraph_scores=(),
            thresholds=_DEFAULT_THRESHOLDS,
        )
        line = report.summary_line()
        # When no flag fires the line should mention "within bounds".
        assert "within" in line.lower() or "ok" in line.lower()


# ---------------------------------------------------------------------------
# DraftGenerator integration — the guard wired into the draft pipeline
# ---------------------------------------------------------------------------


def _build_guard_vault(tmp_path: Path, body: str) -> Path:
    """Materialise a minimal vault with one fragment whose body is *body*.

    Centralised because every guard-wiring test below needs the same
    layout (vault root → ``01-Fragments/frag-001.md`` with the body the
    test wants the source paragraph to be).
    """
    from datetime import UTC, datetime

    import frontmatter

    from creek.models import (
        Confidence,
        Fragment,
        FragmentSource,
        Frequency,
        FrequencyClassification,
        PraxisPotential,
        SourcePlatform,
        VoiceClassification,
        WavelengthClassification,
    )

    vault = tmp_path
    (vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    (vault / "07-Voice" / "Drafts").mkdir(parents=True, exist_ok=True)
    frag = Fragment(
        id="frag-001",
        title="Source title",
        source=FragmentSource(
            platform=SourcePlatform.CLAUDE,
            original_file="scratch.md",
        ),
        created=datetime(2026, 3, 1, tzinfo=UTC),
        ingested=datetime(2026, 3, 1, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F1),
        wavelength=WavelengthClassification(),
        voice=VoiceClassification(confidence=Confidence.SETTLED),
        praxis_potential=PraxisPotential.LATENT,
    )
    target = vault / "01-Fragments" / "frag-001.md"
    post = frontmatter.Post(content=body, **frag.model_dump(mode="json"))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return vault


def _make_idea(title: str = "Echo essay") -> object:
    """Return a synthetic :class:`IdeaSeed` for the wiring tests."""
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.RESONANCE_CHAIN,
        title=title,
        source_fragments=("frag-001",),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="Draft for the grounding guard wiring tests.",
        score=0.0,
    )


class TestDraftGeneratorGuardWiring:
    """The guard must populate the new :class:`Draft` fields end-to-end."""

    def test_guard_runs_when_embedding_fn_is_wired(self, tmp_path: Path) -> None:
        """A wired embedding callable populates the new ``Draft`` fields."""
        from creek.generate.drafts import DraftGenerator

        source_paragraph = "The source paragraph."
        vault = _build_guard_vault(tmp_path, source_paragraph)
        skills_root = vault / "skills"
        skills_root.mkdir()
        table = {source_paragraph: _hashed_vector(source_paragraph)}
        gen = DraftGenerator(
            llm=lambda _p: source_paragraph,
            skills_root=skills_root,
            embedding_fn=_fixture_embedder(table),
            grounding_thresholds=_DEFAULT_THRESHOLDS,
        )
        draft = gen.generate_draft(_make_idea(), vault_path=vault)
        assert draft.derivative_score == pytest.approx(1.0, abs=1e-6)
        assert draft.grounding_score == pytest.approx(1.0, abs=1e-6)
        assert len(draft.paragraph_grounding) == 1
        assert draft.paragraph_grounding[0]["is_derivative"] is True

    def test_guard_skipped_when_no_embedding_fn(self, tmp_path: Path) -> None:
        """No embedding callable → the guard fields stay ``None``/empty."""
        from creek.generate.drafts import DraftGenerator

        vault = _build_guard_vault(tmp_path, "Source body.")
        skills_root = vault / "skills"
        skills_root.mkdir()
        gen = DraftGenerator(
            llm=lambda _p: "An invented draft body.",
            skills_root=skills_root,
        )
        draft = gen.generate_draft(_make_idea(), vault_path=vault)
        assert draft.derivative_score is None
        assert draft.grounding_score is None
        assert draft.paragraph_grounding == ()

    def test_save_draft_writes_guard_frontmatter(self, tmp_path: Path) -> None:
        """``save_draft`` mirrors the scores into the saved markdown frontmatter."""
        import frontmatter

        from creek.generate.drafts import DraftGenerator

        source_paragraph = "Saved source paragraph."
        vault = _build_guard_vault(tmp_path, source_paragraph)
        skills_root = vault / "skills"
        skills_root.mkdir()
        table = {source_paragraph: _hashed_vector(source_paragraph)}
        gen = DraftGenerator(
            llm=lambda _p: source_paragraph,
            skills_root=skills_root,
            embedding_fn=_fixture_embedder(table),
            grounding_thresholds=_DEFAULT_THRESHOLDS,
        )
        draft = gen.generate_draft(_make_idea(title="Saved echo"), vault_path=vault)
        saved_path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(saved_path))
        assert post.metadata["derivative_score"] == pytest.approx(1.0, abs=1e-3)
        assert post.metadata["grounding_score"] == pytest.approx(1.0, abs=1e-3)
        annotations = post.metadata["paragraph_grounding"]
        assert isinstance(annotations, list)
        assert annotations[0]["is_derivative"] is True

    def test_save_draft_omits_guard_fields_when_unscored(
        self,
        tmp_path: Path,
    ) -> None:
        """Unscored drafts do not leak ``derivative_score`` keys into frontmatter."""
        import frontmatter

        from creek.generate.drafts import DraftGenerator

        vault = _build_guard_vault(tmp_path, "Source.")
        skills_root = vault / "skills"
        skills_root.mkdir()
        gen = DraftGenerator(
            llm=lambda _p: "An unscored draft body.",
            skills_root=skills_root,
        )
        draft = gen.generate_draft(_make_idea(), vault_path=vault)
        saved_path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(saved_path))
        assert "derivative_score" not in post.metadata
        assert "grounding_score" not in post.metadata
        assert "paragraph_grounding" not in post.metadata

    def test_guard_prints_summary_to_stderr(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The guard prints a one-line summary to stderr for the operator."""
        from creek.generate.drafts import DraftGenerator

        vault = _build_guard_vault(tmp_path, "Source body.")
        skills_root = vault / "skills"
        skills_root.mkdir()
        gen = DraftGenerator(
            llm=lambda _p: "An invented but distinct draft body.",
            skills_root=skills_root,
            embedding_fn=_fixture_embedder({}),
            grounding_thresholds=_DEFAULT_THRESHOLDS,
        )
        gen.generate_draft(_make_idea(), vault_path=vault)
        captured = capsys.readouterr()
        assert "grounding guard:" in captured.err
        assert "derivative=" in captured.err
        assert "grounding=" in captured.err


class TestDefaultEmbeddingFn:
    """``default_embedding_fn`` builds the production callable lazily."""

    def test_returns_a_callable(self) -> None:
        """The factory returns a callable without loading the transformer."""
        from creek.generate.grounding import default_embedding_fn

        fn = default_embedding_fn()
        assert callable(fn)
