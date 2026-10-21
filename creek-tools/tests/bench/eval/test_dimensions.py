"""The four dimensions, scored separately and against real code paths.

Two properties matter beyond the arithmetic. The deterministic obedience
checks are the *existing* ``voice_fidelity`` / ``voice_authenticity`` modules,
called rather than re-implemented — asserted by patching the shared functions
and watching the eval follow them. And the voice-match dimension is explicit
about its dependency on voice evidence in the prompt (#1880): without it the
dimension reports ``unsupported`` rather than inventing a pass or a failure.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from creek.generate import voice_authenticity
from creek.generate.voice_authenticity import (
    AICorpusLeak,
    AudienceMix,
    VoiceAuthenticityReport,
)
from creek.lint.checks import voice_fidelity
from creek_mcp.bench.eval import PASS_THRESHOLD, RUBRIC_VERSION
from creek_mcp.bench.eval.blinding import blind
from creek_mcp.bench.eval.dimensions import (
    DEFAULT_INSTRUCTIONS,
    DeskInstructions,
    EvalCorpusError,
    ObedienceCheck,
    corpus_health,
    expected_refusals,
    score_grounding_accuracy,
    score_instruction_obedience,
    score_refusal_behaviour,
    score_voice_match,
)
from creek_mcp.bench.eval.judge import HermeticJudge, JudgeRequest
from creek_mcp.bench.eval.rubric import Basis, Dimension, RefusalProbe
from creek_mcp.bench.eval.voice_corpus import VoiceCorpus, build_voice_corpus
from tests.bench.eval.conftest import (
    RecordingJudge,
    in_voice_candidate,
    slop_candidate,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from creek.lint._result import CheckResult


@pytest.fixture
def corpus(tmp_path: Path) -> VoiceCorpus:
    """A built synthetic voice corpus."""
    return build_voice_corpus(tmp_path / "voice")


# --------------------------------------------------------------------------
# (a) voice match
# --------------------------------------------------------------------------


def test_voice_match_is_unsupported_without_voice_evidence() -> None:
    """#1880: the letter prompt carries no voice evidence, so say so."""
    requests: list[JudgeRequest] = []
    score = score_voice_match(
        blind((in_voice_candidate(),), seed=1),
        reference="I woke early.",
        judge=HermeticJudge(),
        evidence_present=False,
        requests=requests,
    )
    assert score.basis is Basis.UNSUPPORTED
    assert score.probes == 0
    assert not requests, "an unsupported dimension must not spend a judge call"


def test_voice_match_scores_when_evidence_is_present(corpus: VoiceCorpus) -> None:
    """With evidence in the prompt the dimension is judged, not skipped."""
    requests: list[JudgeRequest] = []
    score = score_voice_match(
        blind((in_voice_candidate(),), seed=1),
        reference=corpus.voice_sample,
        judge=HermeticJudge(),
        evidence_present=True,
        requests=requests,
    )
    assert score.basis is Basis.JUDGE
    assert score.dimension is Dimension.VOICE_MATCH
    assert len(requests) == 1


def test_voice_match_judge_payload_is_blinded(corpus: VoiceCorpus) -> None:
    """Asserted on the exact payload: no model tag in any recorded field."""
    judge = RecordingJudge()
    candidates = (in_voice_candidate("alpha-tiny"), slop_candidate("beta-large"))
    requests: list[JudgeRequest] = []
    score_voice_match(
        blind(candidates, seed=2),
        reference=corpus.voice_sample,
        judge=judge,
        evidence_present=True,
        requests=requests,
    )
    recorded = " ".join(
        str(value)
        for request in judge.requests
        for value in (
            request.candidate_id,
            request.dimension.value,
            request.rubric_version,
            request.candidate_text,
            request.reference_text,
        )
    )
    assert judge.requests
    for candidate in candidates:
        assert candidate.model_tag not in recorded
    assert requests == judge.requests


def test_voice_match_records_the_rubric_version(corpus: VoiceCorpus) -> None:
    """A score is comparable only within one rubric version, so it rides along."""
    requests: list[JudgeRequest] = []
    score_voice_match(
        blind((in_voice_candidate(),), seed=1),
        reference=corpus.voice_sample,
        judge=HermeticJudge(),
        evidence_present=True,
        requests=requests,
    )
    assert requests[0].rubric_version == RUBRIC_VERSION


def test_voice_match_averages_over_candidates(corpus: VoiceCorpus) -> None:
    """Every candidate is judged, and the dimension reports the mean."""
    judge = RecordingJudge(score=0.5)
    requests: list[JudgeRequest] = []
    score = score_voice_match(
        blind((in_voice_candidate(), slop_candidate()), seed=3),
        reference=corpus.voice_sample,
        judge=judge,
        evidence_present=True,
        requests=requests,
    )
    assert score.probes == 2
    assert score.score == pytest.approx(0.5)


# --------------------------------------------------------------------------
# (b) obedience to Writing Desk instructions
# --------------------------------------------------------------------------


def test_obedience_calls_the_shared_voice_fidelity_check(
    corpus: VoiceCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eval exercises ``creek.lint.checks.voice_fidelity.run`` itself."""
    seen: list[Path] = []
    original = voice_fidelity.run

    def _spy(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
        seen.append(vault_path)
        return original(vault_path, since=since)

    monkeypatch.setattr(voice_fidelity, "run", _spy)
    score_instruction_obedience(
        corpus, blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert seen == [corpus.root]


def test_obedience_fails_loudly_if_the_shared_check_is_removed(
    corpus: VoiceCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicated logic would survive this; a real call site does not."""

    def _broken(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
        del vault_path, since
        raise AssertionError("shared check reached")

    monkeypatch.setattr(voice_fidelity, "run", _broken)
    with pytest.raises(AssertionError, match="shared check reached"):
        score_instruction_obedience(
            corpus, blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
        )


def test_obedience_scores_an_in_voice_candidate_above_the_threshold(
    corpus: VoiceCorpus,
) -> None:
    """A compliant draft passes every deterministic component."""
    score = score_instruction_obedience(
        corpus, blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert score.basis is Basis.DETERMINISTIC
    assert score.score >= PASS_THRESHOLD
    assert score.probes == len(ObedienceCheck)


def test_obedience_penalises_banned_phrases(corpus: VoiceCorpus) -> None:
    """The slop candidate uses several banned phrases and loses that component."""
    good = score_instruction_obedience(
        corpus, blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    bad = score_instruction_obedience(
        corpus, blind((slop_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert bad.score < good.score


def test_obedience_penalises_a_length_violation(corpus: VoiceCorpus) -> None:
    """Length is part of the Writing Desk instruction surface."""
    strict = DeskInstructions(
        min_words=10_000,
        max_words=20_000,
        required_headings=(),
        banned_phrases=(),
    )
    score = score_instruction_obedience(
        corpus, blind((in_voice_candidate(),), seed=1), strict
    )
    assert score.score < 1.0


def test_obedience_penalises_a_missing_heading(corpus: VoiceCorpus) -> None:
    """Structure is checked against the instructions, not guessed."""
    structured = DeskInstructions(
        min_words=1,
        max_words=10_000,
        required_headings=("## Afterward",),
        banned_phrases=(),
    )
    score = score_instruction_obedience(
        corpus, blind((in_voice_candidate(),), seed=1), structured
    )
    assert score.score < 1.0


def test_obedience_credits_a_candidate_that_meets_the_structure(
    corpus: VoiceCorpus,
) -> None:
    """Control for the test above: the heading check can be satisfied."""
    candidate = replace(
        in_voice_candidate(), text="## Afterward\n\nI shut the door. I slept."
    )
    structured = DeskInstructions(
        min_words=1,
        max_words=10_000,
        required_headings=("## Afterward",),
        banned_phrases=(),
    )
    score = score_instruction_obedience(corpus, blind((candidate,), seed=1), structured)
    assert score.score == 1.0


def test_obedience_writes_drafts_only_inside_the_corpus(
    corpus: VoiceCorpus,
) -> None:
    """Drafts are written inside the temp corpus, under its opaque id."""
    blinded = blind((in_voice_candidate(),), seed=1)
    score_instruction_obedience(corpus, blinded, DEFAULT_INSTRUCTIONS)
    drafts = sorted(corpus.root.glob("07-Voice/Drafts/*.md"))
    assert [path.stem for path in drafts] == [blinded[0].candidate_id]


# --------------------------------------------------------------------------
# corpus health, via the shared voice-authenticity report
# --------------------------------------------------------------------------


def test_corpus_health_calls_the_shared_voice_authenticity_report(
    corpus: VoiceCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eval exercises ``build_voice_authenticity_report`` itself."""
    seen: list[Path] = []
    original = voice_authenticity.build_voice_authenticity_report

    def _spy(vault_path: Path, *, draft_path: Path | None) -> VoiceAuthenticityReport:
        seen.append(vault_path)
        return original(vault_path, draft_path=draft_path)

    monkeypatch.setattr(voice_authenticity, "build_voice_authenticity_report", _spy)
    report = corpus_health(corpus)
    assert seen == [corpus.root]
    assert report.audience_mix.total == corpus.entry_count


def _stub_report(*, leaked: int, eligible: int) -> VoiceAuthenticityReport:
    """Build a voice-authenticity report with the given corpus shape."""
    return VoiceAuthenticityReport(
        audience_mix=AudienceMix(by_tier={"open": eligible}, weighting_active=False),
        ai_corpus_leak=AICorpusLeak(leaked=leaked, eligible_total=eligible),
        deslop=None,
    )


def test_corpus_health_refuses_an_empty_eligible_corpus(
    corpus: VoiceCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoring against no voice-eligible writing would assert nothing."""
    monkeypatch.setattr(
        voice_authenticity,
        "build_voice_authenticity_report",
        lambda _vault, *, draft_path: _stub_report(leaked=0, eligible=0),
    )
    with pytest.raises(EvalCorpusError, match="eligible"):
        corpus_health(corpus)


def test_corpus_health_refuses_a_corpus_leaking_ai_prose(
    corpus: VoiceCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus fused with AI prose cannot be the reference for voice match."""
    monkeypatch.setattr(
        voice_authenticity,
        "build_voice_authenticity_report",
        lambda _vault, *, draft_path: _stub_report(leaked=1, eligible=3),
    )
    with pytest.raises(EvalCorpusError, match="AI"):
        corpus_health(corpus)


# --------------------------------------------------------------------------
# (c) grounding accuracy
# --------------------------------------------------------------------------


def test_grounding_scores_known_fragments_fully(corpus: VoiceCorpus) -> None:
    """Citing fragments the corpus contains is perfect grounding."""
    candidate = in_voice_candidate(citations=("voice-0000", "voice-0003"))
    score = score_grounding_accuracy(corpus, blind((candidate,), seed=1))
    assert score.score == 1.0
    assert score.basis is Basis.DETERMINISTIC
    assert score.dimension is Dimension.GROUNDING_ACCURACY


def test_grounding_scores_an_invented_citation_zero(corpus: VoiceCorpus) -> None:
    """A citation the corpus does not contain scores zero (AC5)."""
    candidate = in_voice_candidate(citations=("voice-9999",))
    score = score_grounding_accuracy(corpus, blind((candidate,), seed=1))
    assert score.score == 0.0


def test_grounding_scores_a_half_invented_citation_set(corpus: VoiceCorpus) -> None:
    """Partial credit is proportional, not all-or-nothing per candidate."""
    candidate = in_voice_candidate(citations=("voice-0000", "voice-9999"))
    score = score_grounding_accuracy(corpus, blind((candidate,), seed=1))
    assert score.score == pytest.approx(0.5)


def test_grounding_scores_an_uncited_reflection_zero(corpus: VoiceCorpus) -> None:
    """An ungrounded reflection has no grounding accuracy to credit."""
    candidate = in_voice_candidate(citations=())
    score = score_grounding_accuracy(corpus, blind((candidate,), seed=1))
    assert score.score == 0.0


# --------------------------------------------------------------------------
# (d) refusal behaviour
# --------------------------------------------------------------------------


def test_every_probe_expects_a_refusal_from_the_real_guards() -> None:
    """Expectations are computed from production code, not hardcoded."""
    expected = expected_refusals()
    assert set(expected) == set(RefusalProbe)
    assert all(expected.values())


def test_refusal_scores_a_fully_refusing_candidate_perfectly() -> None:
    """Care guard, above-ceiling prompt and INTIMATE fragment all refused."""
    score = score_refusal_behaviour(blind((in_voice_candidate(),), seed=1))
    assert score.score == 1.0
    assert score.probes == len(RefusalProbe)
    assert score.basis is Basis.DETERMINISTIC
    assert score.dimension is Dimension.REFUSAL_BEHAVIOUR


def test_refusal_scores_an_answer_in_place_of_a_refusal_zero() -> None:
    """Answering every probe is a total failure on this dimension."""
    score = score_refusal_behaviour(blind((slop_candidate(),), seed=1))
    assert score.score == 0.0


def test_refusal_gives_partial_credit_per_probe() -> None:
    """Each probe is scored on its own, so one miss is not total failure."""
    partial = dict.fromkeys(RefusalProbe, True)
    partial[RefusalProbe.INTIMATE_FRAGMENT] = False
    candidate = replace(in_voice_candidate(), refusals=partial)
    score = score_refusal_behaviour(blind((candidate,), seed=1))
    assert score.score == pytest.approx(1 - 1 / len(RefusalProbe))


def test_refusal_treats_an_unreported_probe_as_not_refused() -> None:
    """Silence is not a refusal; an omitted probe scores zero."""
    candidate = replace(in_voice_candidate(), refusals={})
    score = score_refusal_behaviour(blind((candidate,), seed=1))
    assert score.score == 0.0
