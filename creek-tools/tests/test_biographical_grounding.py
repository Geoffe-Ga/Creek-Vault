"""Tests for the ungrounded-biographical-fact guard (issue #515).

Drafts invent first-person biographical facts ("Not the LDS Christ I was
handed as a kid—") that no source fragment supports. The paragraph-level
grounding guard (#355) misses these because a single invented sentence rides
inside an otherwise on-topic paragraph. This module pins:

* :func:`creek.generate.grounding.split_sentences` /
  :func:`creek.generate.grounding.is_biographical_sentence` — the conservative
  first-person biographical heuristic.
* :func:`creek.generate.grounding.scan_biographical_sentences` — the
  sentence-level cosine scan that flags an ungrounded biographical claim and,
  crucially, does NOT flag a grounded one (no false positive).
* :func:`creek.author.checks.check_biographical_grounding` and its wiring into
  :meth:`creek.author.reflection.ReflectionNode.review` (the desk path).
* The no-fabrication prompt steer in both the draft and voice ``## Ask``
  blocks (structure tests).

A deterministic injected embedding callable keeps every test offline.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

from creek.author.checks import check_biographical_grounding
from creek.author.models import EvidenceBundle, EvidenceClaim
from creek.author.reflection import ReflectionNode
from creek.author.voice import _ask_section
from creek.generate.drafts import _NO_FABRICATION_STEER, _compose_ask_section
from creek.generate.grounding import (
    BiographicalGroundingFinding,
    is_biographical_sentence,
    scan_biographical_sentences,
    split_sentences,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from creek.generate.drafts import DraftGenerator
    from creek.generate.grounding import EmbeddingFn

# The literal fabrication from the issue body — the canonical fixture sentence.
_LDS_SENTENCE = "Not the LDS Christ I was handed as a kid."

# A grounded first-person biographical claim that DOES trace to a source.
_GROUNDED_SENTENCE = "I grew up in a small town on the prairie."


_VECTOR_DIM = 32


def _hashed_vector(text: str) -> list[float]:
    """Return a deterministic, normalised vector keyed off *text*'s hash.

    Identical strings map to the same vector; distinct strings map to
    near-orthogonal vectors so cosine cleanly separates "grounded" from
    "ungrounded" without a real embedding model.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw: list[int] = []
    counter = 0
    while len(raw) < _VECTOR_DIM:
        chunk = hashlib.sha256(digest + counter.to_bytes(2, "big")).digest()
        raw.extend(chunk[: _VECTOR_DIM - len(raw)])
        counter += 1
    centred = [b - 127.5 for b in raw[:_VECTOR_DIM]]
    norm = math.sqrt(sum(x * x for x in centred)) or 1.0
    return [x / norm for x in centred]


def _aliasing_embedder(*, alias: tuple[str, str]) -> EmbeddingFn:
    """Embed two texts identically so they score cosine 1.0 against each other.

    *alias* is a ``(a, b)`` pair both mapped to the same vector; every other
    string falls through to :func:`_hashed_vector` and stays near-orthogonal.
    This lets a test declare "this sentence is grounded by this source" without
    hand-tuning coordinates.
    """
    a, b = alias
    shared = _hashed_vector(a)

    def _embed(text: str) -> list[float]:
        if text in (a, b):
            return shared
        return _hashed_vector(text)

    return _embed


# ---------------------------------------------------------------------------
# split_sentences + is_biographical_sentence
# ---------------------------------------------------------------------------


class TestSentenceSplitting:
    """``split_sentences`` isolates a claim from the paragraph hiding it."""

    def test_splits_on_terminal_punctuation(self) -> None:
        """A run-on paragraph splits into one entry per sentence."""
        body = "The wave rises. Then it falls! Does it return?"
        assert split_sentences(body) == [
            "The wave rises.",
            "Then it falls!",
            "Does it return?",
        ]

    def test_drops_headings_and_blank_lines(self) -> None:
        """Markdown headings and blank lines collapse away."""
        body = "# Heading\n\nA real sentence here.\n\n## Another\n"
        assert split_sentences(body) == ["A real sentence here."]


class TestBiographicalHeuristic:
    """The heuristic is conservative: biography yes, opinion no."""

    def test_matches_lds_fixture_sentence(self) -> None:
        """The issue's literal fixture sentence is biographical."""
        assert is_biographical_sentence(_LDS_SENTENCE) is True

    def test_matches_grew_up_and_childhood_markers(self) -> None:
        """``I grew up`` / ``as a kid`` / ``when I was`` are biographical."""
        assert is_biographical_sentence("I grew up on a farm.") is True
        assert is_biographical_sentence("Back when I was a child.") is True
        assert is_biographical_sentence("As a kid, the world felt huge.") is True

    def test_opinion_is_not_biographical(self) -> None:
        """A bare first-person opinion must NOT match (false-positive guard)."""
        assert is_biographical_sentence("I think pluralism matters.") is False
        assert is_biographical_sentence("I love this idea.") is False
        assert is_biographical_sentence("I want to explore the tension.") is False


# ---------------------------------------------------------------------------
# scan_biographical_sentences
# ---------------------------------------------------------------------------


class TestScanBiographicalSentences:
    """The sentence-level cosine scan flags fabrication, spares grounded prose."""

    def test_flags_ungrounded_biographical_claim(self) -> None:
        """The LDS fixture, absent from all sources, is flagged."""
        body = f"Christ is a pattern, not a person. {_LDS_SENTENCE}"
        findings = scan_biographical_sentences(
            body,
            source_texts=["Christ shows up as a recurring pattern in my notes."],
            embedding_fn=_hashed_vector,
            threshold=0.30,
        )
        assert [f.sentence for f in findings] == [_LDS_SENTENCE]
        assert isinstance(findings[0], BiographicalGroundingFinding)
        assert findings[0].max_similarity < 0.30

    def test_grounded_biographical_claim_not_flagged(self) -> None:
        """A biographical claim that IS in a source produces NO finding."""
        embedder = _aliasing_embedder(
            alias=(_GROUNDED_SENTENCE, "prairie childhood source")
        )
        findings = scan_biographical_sentences(
            _GROUNDED_SENTENCE,
            source_texts=["prairie childhood source"],
            embedding_fn=embedder,
            threshold=0.30,
        )
        assert findings == []

    def test_no_biographical_sentence_short_circuits(self) -> None:
        """A body with only opinions never touches the embedder."""

        def _boom(_text: str) -> list[float]:
            msg = "embedder must not be called when no sentence is biographical"
            raise AssertionError(msg)

        findings = scan_biographical_sentences(
            "I think the wave is beautiful. I love the idea.",
            source_texts=["anything"],
            embedding_fn=_boom,
            threshold=0.30,
        )
        assert findings == []

    def test_no_sources_scores_zero_and_flags(self) -> None:
        """With no sources a biographical claim is ungrounded by definition."""
        findings = scan_biographical_sentences(
            _LDS_SENTENCE,
            source_texts=[],
            embedding_fn=_hashed_vector,
            threshold=0.30,
        )
        assert len(findings) == 1
        assert findings[0].max_similarity == 0.0


# ---------------------------------------------------------------------------
# Desk path: check_biographical_grounding + ReflectionNode.review
# ---------------------------------------------------------------------------


class TestDeskBiographicalCheck:
    """The desk check turns ungrounded biography into a HIGH finding."""

    def test_flags_lds_fixture(self) -> None:
        """The LDS fixture, absent from the evidence, yields a HIGH finding."""
        evidence = EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim="Christ recurs as a pattern in the corpus.",
                    source_fragments=["frag-a"],
                )
            ]
        )
        findings = check_biographical_grounding(
            f"A pattern, not a person. {_LDS_SENTENCE}",
            evidence,
            embedding_fn=_hashed_vector,
            grounding_lower=0.30,
        )
        assert len(findings) == 1
        assert findings[0].dimension == "biographical_grounding"
        assert findings[0].severity == "HIGH"
        assert "LDS" in findings[0].message

    def test_grounded_claim_no_finding(self) -> None:
        """A biographical claim matching a claim's text produces no finding."""
        evidence = EvidenceBundle(
            claims=[
                EvidenceClaim(claim=_GROUNDED_SENTENCE, source_fragments=["frag-a"])
            ]
        )
        embedder = _aliasing_embedder(alias=(_GROUNDED_SENTENCE, _GROUNDED_SENTENCE))
        findings = check_biographical_grounding(
            _GROUNDED_SENTENCE,
            evidence,
            embedding_fn=embedder,
            grounding_lower=0.30,
        )
        assert findings == []

    def test_dormant_without_embedder(self) -> None:
        """No embedder → the check is dormant (mirrors voice fidelity)."""
        evidence = EvidenceBundle(
            claims=[EvidenceClaim(claim="x", source_fragments=["f"])]
        )
        assert (
            check_biographical_grounding(
                _LDS_SENTENCE,
                evidence,
                embedding_fn=None,
                grounding_lower=0.30,
            )
            == []
        )

    def test_voice_core_grounds_the_claim(self) -> None:
        """A claim that traces only to the voice-core brief is not flagged."""
        evidence = EvidenceBundle(
            claims=[EvidenceClaim(claim="unrelated", source_fragments=["f"])]
        )
        brief = "the voice-core brief that grounds the claim"
        embedder = _aliasing_embedder(alias=(_GROUNDED_SENTENCE, brief))
        findings = check_biographical_grounding(
            _GROUNDED_SENTENCE,
            evidence,
            embedding_fn=embedder,
            grounding_lower=0.30,
            voice_core=brief,
        )
        assert findings == []


class TestReflectionNodeWiring:
    """The reflection node surfaces the biographical finding end-to-end."""

    def test_review_revises_on_lds_fixture(self) -> None:
        """``review`` returns REVISE with a biographical_grounding finding."""
        evidence = EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim="Christ is a recurring pattern.",
                    source_fragments=["frag-a"],
                )
            ]
        )
        result = ReflectionNode().review(
            f"Christ is a recurring pattern. {_LDS_SENTENCE}",
            evidence,
            embedding_fn=_hashed_vector,
            grounding_lower=0.30,
        )
        assert result.decision == "REVISE"
        assert any(f.dimension == "biographical_grounding" for f in result.findings)

    def test_review_dormant_without_embedder(self) -> None:
        """Without an embedder the node never raises a biographical finding."""
        evidence = EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim="Christ is a recurring pattern.",
                    source_fragments=["frag-a"],
                )
            ]
        )
        result = ReflectionNode().review(
            f"Christ is a recurring pattern. {_LDS_SENTENCE}",
            evidence,
        )
        assert not any(f.dimension == "biographical_grounding" for f in result.findings)


# ---------------------------------------------------------------------------
# Draft path: DraftGenerator._build_guard_report surfaces the warning
# ---------------------------------------------------------------------------


class TestDraftGuardWarning:
    """The draft path warns on an ungrounded biographical sentence (stderr)."""

    def _generator(self, tmp_path: Path, *, voice_core: str) -> DraftGenerator:
        """Build a :class:`DraftGenerator` wired with the test embedder."""
        from creek.generate.drafts import DraftGenerator
        from creek.generate.grounding import GroundingThresholds

        return DraftGenerator(
            llm=lambda _prompt: "unused",
            skills_root=tmp_path,
            voice_core=voice_core,
            embedding_fn=_hashed_vector,
            grounding_thresholds=GroundingThresholds(
                derivative_upper=0.9,
                grounding_lower=0.30,
                grounding_fraction_lower=0.5,
            ),
        )

    def test_flagged_sentence_prints_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An ungrounded LDS sentence prints a ``grounding guard:`` warning."""
        from creek.generate.grounding import GroundingThresholds

        generator = self._generator(tmp_path, voice_core="")
        generator._warn_ungrounded_biographical(
            body=f"Christ is a pattern.\n\n{_LDS_SENTENCE}",
            source_texts=["Christ is a recurring pattern."],
            embedding_fn=_hashed_vector,
            thresholds=GroundingThresholds(
                derivative_upper=0.9,
                grounding_lower=0.30,
                grounding_fraction_lower=0.5,
            ),
        )
        err = capsys.readouterr().err
        assert "ungrounded first-person biographical claim" in err
        assert "LDS" in err

    def test_voice_core_grounds_and_suppresses_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A claim grounded by the voice-core brief prints no warning."""
        from creek.generate.grounding import GroundingThresholds

        embedder = _aliasing_embedder(alias=(_GROUNDED_SENTENCE, _GROUNDED_SENTENCE))
        generator = self._generator(tmp_path, voice_core=_GROUNDED_SENTENCE)
        generator._warn_ungrounded_biographical(
            body=_GROUNDED_SENTENCE,
            source_texts=["unrelated source body"],
            embedding_fn=embedder,
            thresholds=GroundingThresholds(
                derivative_upper=0.9,
                grounding_lower=0.30,
                grounding_fraction_lower=0.5,
            ),
        )
        err = capsys.readouterr().err
        assert "biographical claim" not in err


# ---------------------------------------------------------------------------
# Prompt steer structure tests
# ---------------------------------------------------------------------------


_STEER_PHRASE = "never invent events, my upbringing, or another person's motives"


class TestPromptSteer:
    """The no-fabrication instruction is pinned into both prompt surfaces."""

    def test_draft_ask_carries_steer_in_every_mode(self) -> None:
        """Every ``_compose_ask_section`` variant ends with the steer."""
        from creek.generate.mining import IdeaSeed, MiningStrategy

        idea = IdeaSeed(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            title="Christ",
            source_fragments=("frag-a",),
            threads=(),
            eddies=(),
            frequency_affinity=(),
            brief_description="A draft about the pattern.",
            score=0.8,
        )
        for kwargs in (
            {"per_dimension": False},
            {"per_dimension": True},
            {"per_dimension": False, "twist": True},
        ):
            section = _compose_ask_section(idea, **kwargs)
            assert _STEER_PHRASE in section
        assert _STEER_PHRASE in _NO_FABRICATION_STEER

    def test_voice_ask_carries_steer(self) -> None:
        """The voice agent's ``## Ask`` block carries the steer."""
        section = _ask_section("What is Christ?", None, None)
        assert _STEER_PHRASE in section
