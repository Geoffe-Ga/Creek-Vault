"""The four dimensions, scored separately and against real code paths.

Three properties matter beyond the arithmetic. Corpus health is the *existing*
``voice_authenticity`` module, called rather than re-implemented — asserted by
patching the shared function and watching the eval follow it. The voice-match
dimension is explicit about its dependency on voice evidence in the prompt
(#1880): without it the dimension reports ``unsupported`` rather than inventing
a pass or a failure. And every obedience component is exercised in *both*
directions, because #1911 was a component that could not fail: a free point
that lifted every obedience score toward the pass threshold. The measurement
that retired it is pinned here too, so the decision is auditable from the suite
and not only from a PR body.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest

from creek.config import AIStyleConfig
from creek.generate import voice_authenticity
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek.generate.ai_style.scanner import scan
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
    obedience_checks,
    score_grounding_accuracy,
    score_instruction_obedience,
    score_refusal_behaviour,
    score_voice_match,
)
from creek_mcp.bench.eval.judge import HermeticJudge, JudgeRequest
from creek_mcp.bench.eval.rubric import (
    Basis,
    Dimension,
    RefusalProbe,
    RubricVerdict,
    next_candidate,
    rubric_verdict,
)
from creek_mcp.bench.eval.voice_corpus import (
    DRAFTS_DIR,
    VoiceCorpus,
    build_voice_corpus,
)
from tests.bench.eval.conftest import (
    IN_VOICE_TEXT,
    SLOP_TEXT,
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


_PERMISSIVE: Final[DeskInstructions] = DeskInstructions(
    min_words=1,
    max_words=10_000,
    required_headings=(),
    banned_phrases=(),
)
"""An instruction surface no candidate can disobey, for isolating a component."""

_STRUCTURED: Final[DeskInstructions] = DeskInstructions(
    min_words=1,
    max_words=10_000,
    required_headings=("## Afterward",),
    banned_phrases=(),
)
"""Only the structure component is active, so a failure can only be structural."""

_FULL_SURFACE: Final[DeskInstructions] = DeskInstructions(
    min_words=DEFAULT_INSTRUCTIONS.min_words,
    max_words=DEFAULT_INSTRUCTIONS.max_words,
    required_headings=("## Afterward",),
    banned_phrases=DEFAULT_INSTRUCTIONS.banned_phrases,
)
"""All three components active at once, so one surface can separate two fixtures."""

_HEADED_TEXT: Final[str] = "## Afterward\n\nI shut the door. I slept."
"""Short, headed, and clean: satisfies structure and nothing else is asked."""

_TOO_SHORT_TEXT: Final[str] = "I slept."
"""Under the default floor of 40 words, and nothing else."""

_COMPLIANT_TEXT: Final[str] = f"## Afterward\n\n{IN_VOICE_TEXT}"
"""Obeys all three instructions of :data:`_FULL_SURFACE` at once."""

_DISOBEDIENT_TEXT: Final[str] = (
    "In today's fast-paced world one must delve into the tapestry."
)
"""Disobeys all three at once: too short, unheaded, and three banned phrases."""

_COMPONENT_DIRECTIONS: Final[
    tuple[tuple[ObedienceCheck, DeskInstructions, str, bool], ...]
] = (
    (ObedienceCheck.LENGTH, DEFAULT_INSTRUCTIONS, _TOO_SHORT_TEXT, False),
    (ObedienceCheck.LENGTH, DEFAULT_INSTRUCTIONS, IN_VOICE_TEXT, True),
    (ObedienceCheck.STRUCTURE, _STRUCTURED, IN_VOICE_TEXT, False),
    (ObedienceCheck.STRUCTURE, _STRUCTURED, _HEADED_TEXT, True),
    (ObedienceCheck.BANNED_PHRASES, DEFAULT_INSTRUCTIONS, SLOP_TEXT, False),
    (ObedienceCheck.BANNED_PHRASES, DEFAULT_INSTRUCTIONS, IN_VOICE_TEXT, True),
)
"""Every component, in both directions — the #1911 guard, stated as data."""

_ON_VOICE_DISTANCE: Final[float] = 0.0
"""Measured ``voice_distance`` of the on-voice fixture against this corpus."""

_OFF_VOICE_DISTANCE: Final[float] = 0.1012
"""Measured ``voice_distance`` of the off-voice fixture against this corpus."""

_DISTANCE_TOLERANCE: Final[float] = 5e-4
"""Slack on the pinned measurements; a real drift should red this, not round away."""

_LADDER: Final[tuple[str, ...]] = ("cheap-model", "middle-model", "frontier-model")
"""A three-rung cost ladder, cheapest first, for the escalation assertion."""


def test_the_component_table_covers_every_check_in_both_directions() -> None:
    """A new component cannot be added without a fixture that fails it (#1911).

    This is the structural half of the #1911 fix: the parametrised table below
    is only a guard if it is complete, so completeness is asserted rather than
    maintained by hand.
    """
    covered = {
        (check, expected) for check, _desk, _text, expected in _COMPONENT_DIRECTIONS
    }
    assert covered == {
        (check, expected) for check in ObedienceCheck for expected in (True, False)
    }


@pytest.mark.parametrize(
    ("check", "instructions", "text", "expected"),
    _COMPONENT_DIRECTIONS,
    ids=[
        f"{check.value}-{'passes' if expected else 'fails'}"
        for check, _desk, _text, expected in _COMPONENT_DIRECTIONS
    ],
)
def test_every_obedience_component_is_exercised_in_both_directions(
    check: ObedienceCheck,
    instructions: DeskInstructions,
    text: str,
    expected: bool,
) -> None:
    """Each component reads ``True`` for one candidate and ``False`` for another."""
    blinded = blind((replace(in_voice_candidate(), text=text),), seed=1)
    checks = obedience_checks(blinded, instructions)
    assert checks[blinded[0].candidate_id][check] is expected


def test_no_obedience_component_is_constant_across_the_two_fixtures() -> None:
    """One surface separates a compliant candidate from a disobedient one.

    Every component flips between the two, which is what #1911's free point
    could not do: it read ``True`` for the off-voice fixture as well as the
    on-voice one.
    """
    compliant = replace(in_voice_candidate(), text=_COMPLIANT_TEXT)
    disobedient = replace(slop_candidate(), text=_DISOBEDIENT_TEXT)
    blinded = blind((compliant, disobedient), seed=1)
    checks = obedience_checks(blinded, _FULL_SURFACE)
    by_text = {candidate.text: checks[candidate.candidate_id] for candidate in blinded}
    assert by_text[_COMPLIANT_TEXT] == dict.fromkeys(ObedienceCheck, True)
    assert by_text[_DISOBEDIENT_TEXT] == dict.fromkeys(ObedienceCheck, False)


def test_one_violated_component_scores_below_the_pass_threshold() -> None:
    """Three honest components: one violated instruction fails the dimension.

    The arithmetic is the reason #1911's component had to go rather than be
    recalibrated. With four equally-weighted components a single violation
    scores ``0.75``, which clears ``PASS_THRESHOLD``; with three it scores
    ``2/3`` and does not.
    """
    candidate = replace(in_voice_candidate(), text=IN_VOICE_TEXT)
    score = score_instruction_obedience(blind((candidate,), seed=1), _FULL_SURFACE)
    assert score.score == pytest.approx(2 / 3)
    assert score.score < PASS_THRESHOLD


def test_the_ladder_does_not_stop_on_one_violated_component() -> None:
    """A single violated instruction must still buy the next rung up."""
    candidate = replace(in_voice_candidate(), text=IN_VOICE_TEXT)
    score = score_instruction_obedience(blind((candidate,), seed=1), _FULL_SURFACE)
    verdict = rubric_verdict((score,))
    assert verdict is RubricVerdict.FAILS
    assert next_candidate(_LADDER, _LADDER[0], verdict) == _LADDER[1]


def test_obedience_does_not_call_the_voice_fidelity_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1911: the dimension must not run a check whose result cannot move a score.

    Scoring through a ``voice_fidelity.run`` that explodes is the only way to
    pin the *absence* of a call site; a reinstated delegation reds this test.
    """

    def _explode(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
        del vault_path, since
        raise AssertionError("voice-fidelity check reached")

    monkeypatch.setattr(voice_fidelity, "run", _explode)
    score = score_instruction_obedience(
        blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert score.probes == len(ObedienceCheck)


def test_the_shipped_voice_check_cannot_separate_the_fixtures(
    corpus: VoiceCorpus,
) -> None:
    """The measurement that retired #1911's component, pinned in the suite.

    ``creek.lint.checks.voice_fidelity.run`` flags a draft only once its
    ``voice_distance`` clears ``voice_distance_upper``. At candidate length the
    off-voice fixture does not get close, so the check returned no findings for
    *either* fixture and the component was a constant. If a scanner or ceiling
    change ever makes it discriminate here, this test reds and the decision in
    ``dimensions.py``'s module docstring is up for review again.
    """
    drafts = corpus.root / DRAFTS_DIR
    for name, text in (("on-voice", IN_VOICE_TEXT), ("off-voice", SLOP_TEXT)):
        (drafts / f"{name}.md").write_text(text, encoding="utf-8")
    result = voice_fidelity.run(corpus.root)
    assert result.findings == []
    config = AIStyleConfig()
    fingerprint = load_fingerprint(corpus.root, config)
    on_voice = scan(IN_VOICE_TEXT, fingerprint=fingerprint, config=config)
    off_voice = scan(SLOP_TEXT, fingerprint=fingerprint, config=config)
    assert on_voice.voice_distance == pytest.approx(
        _ON_VOICE_DISTANCE, abs=_DISTANCE_TOLERANCE
    )
    assert off_voice.voice_distance == pytest.approx(
        _OFF_VOICE_DISTANCE, abs=_DISTANCE_TOLERANCE
    )
    # The signal exists; it is simply far too small for the shipped ceiling.
    assert on_voice.voice_distance < off_voice.voice_distance
    assert off_voice.voice_distance < config.voice_distance_upper


def test_obedience_scores_an_in_voice_candidate_above_the_threshold() -> None:
    """A compliant draft passes every deterministic component."""
    score = score_instruction_obedience(
        blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert score.basis is Basis.DETERMINISTIC
    assert score.score >= PASS_THRESHOLD
    assert score.probes == len(ObedienceCheck)


def test_obedience_probes_count_candidates_times_components() -> None:
    """``probes`` is the real unit count, so a dropped component shows up in it."""
    score = score_instruction_obedience(
        blind((in_voice_candidate(), slop_candidate()), seed=3), DEFAULT_INSTRUCTIONS
    )
    assert score.probes == 2 * len(ObedienceCheck)


def test_obedience_penalises_banned_phrases() -> None:
    """The slop candidate uses several banned phrases and loses that component."""
    good = score_instruction_obedience(
        blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    bad = score_instruction_obedience(
        blind((slop_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert bad.score < good.score


def test_obedience_penalises_a_length_violation() -> None:
    """Length is part of the Writing Desk instruction surface."""
    strict = DeskInstructions(
        min_words=10_000,
        max_words=20_000,
        required_headings=(),
        banned_phrases=(),
    )
    score = score_instruction_obedience(blind((in_voice_candidate(),), seed=1), strict)
    assert score.score < 1.0


def test_obedience_penalises_a_missing_heading() -> None:
    """Structure is checked against the instructions, not guessed."""
    score = score_instruction_obedience(
        blind((in_voice_candidate(),), seed=1), _STRUCTURED
    )
    assert score.score < 1.0


def test_obedience_credits_a_candidate_that_meets_every_component() -> None:
    """Control for the tests above: each component can be satisfied at once.

    Asserted per component rather than as ``score == 1.0``: a mean of ``1.0``
    cannot tell three honest passes from two passes plus a component that is
    incapable of failing, which is what #1911 hid.
    """
    candidate = replace(in_voice_candidate(), text=_HEADED_TEXT)
    blinded = blind((candidate,), seed=1)
    assert obedience_checks(blinded, _STRUCTURED) == {
        blinded[0].candidate_id: dict.fromkeys(ObedienceCheck, True)
    }
    assert score_instruction_obedience(blinded, _STRUCTURED).score == 1.0


def test_obedience_leaves_the_corpus_drafts_directory_alone(
    corpus: VoiceCorpus,
) -> None:
    """Scoring instructions is a pure read; it writes nothing into the vault."""
    score_instruction_obedience(
        blind((in_voice_candidate(),), seed=1), DEFAULT_INSTRUCTIONS
    )
    assert list((corpus.root / DRAFTS_DIR).glob("*.md")) == []


def test_obedience_credits_the_off_voice_fixture_when_nothing_is_disobeyed() -> None:
    """Obedience is about instructions, not voice — and now says so honestly.

    This was #1911's RED: under a surface the off-voice fixture cannot disobey,
    the dimension already scored ``1.0``, which looked like three honest passes
    and was in fact two passes plus a component incapable of failing. The score
    is unchanged and now means what it says. Voice is dimension (a)'s subject.
    """
    score = score_instruction_obedience(blind((slop_candidate(),), seed=1), _PERMISSIVE)
    assert score.score == 1.0


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
