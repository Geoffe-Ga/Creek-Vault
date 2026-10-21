"""The judge seam: a deterministic hermetic default and a routed cloud option.

The hermetic judge is the default so the whole eval runs in CI with no API
call. The routed judge is the frontier option #1875 asks for; it is reached
only through :meth:`creek.classify.llm.router.ModelRouter.resolve_role`, so the
INTIMATE chokepoint and consent gating apply to it unchanged. Nothing here
touches the network: the routed judge's provider is injected.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

import pytest

from creek.classify.llm.completion import Completion
from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier
from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.judge import (
    JUDGE_PROMPT,
    JUDGE_ROLE,
    HermeticJudge,
    JudgeRequest,
    JudgeUnavailableError,
    RoutedJudge,
)
from creek_mcp.bench.eval.rubric import Dimension
from tests.bench.eval.conftest import IN_VOICE_TEXT, SLOP_TEXT

_IDENTITY_FIELDS = frozenset({"model_tag", "model", "provider", "vendor"})


def _request(text: str, reference: str = IN_VOICE_TEXT) -> JudgeRequest:
    """Build a voice-match request for *text*."""
    return JudgeRequest(
        candidate_id="0123456789abcdef",
        dimension=Dimension.VOICE_MATCH,
        rubric_version=RUBRIC_VERSION,
        candidate_text=text,
        reference_text=reference,
    )


@dataclass
class _StubProvider:
    """A provider that answers with a fixed completion, never over a socket."""

    text: str = "0.82"
    available: bool = True
    is_cloud: bool = True
    model: str = "stub"
    prompts: list[str] | None = None

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Record *prompt* and return the canned completion."""
        del max_tokens, system
        if self.prompts is not None:
            self.prompts.append(prompt)
        return Completion(text=self.text)


def _local_router() -> ModelRouter:
    """A router whose every stage resolves to a local Ollama config."""
    return ModelRouter(LLMRoutingConfig(default=LLMConfig(provider="ollama")))


def test_judge_request_carries_no_model_identity_field() -> None:
    """Structural: the payload type has nowhere to put a candidate's name."""
    names = {field.name for field in fields(JudgeRequest)}
    assert not names & _IDENTITY_FIELDS


def test_hermetic_judge_is_local() -> None:
    """The default judge declares itself local, like the capacity fake does."""
    assert HermeticJudge().is_cloud is False


def test_hermetic_judge_is_deterministic() -> None:
    """Two calls with one payload give byte-identical scores."""
    judge = HermeticJudge()
    assert judge(_request(SLOP_TEXT)) == judge(_request(SLOP_TEXT))


def test_hermetic_judge_scores_in_the_unit_interval() -> None:
    """Every score the rubric consumes is normalised."""
    judge = HermeticJudge()
    for text in (IN_VOICE_TEXT, SLOP_TEXT, "", "one"):
        assert 0.0 <= judge(_request(text)) <= 1.0


def test_hermetic_judge_prefers_the_in_voice_candidate() -> None:
    """The judge is crude but not arbitrary: on-voice beats generic prose."""
    judge = HermeticJudge()
    assert judge(_request(IN_VOICE_TEXT)) > judge(_request(SLOP_TEXT))


def test_hermetic_judge_scores_an_identical_candidate_perfectly() -> None:
    """A candidate equal to the reference has zero measured divergence."""
    assert HermeticJudge()(_request(IN_VOICE_TEXT, IN_VOICE_TEXT)) == 1.0


def test_hermetic_judge_handles_an_empty_reference() -> None:
    """A degenerate payload must not divide by zero."""
    assert 0.0 <= HermeticJudge()(_request(IN_VOICE_TEXT, "")) <= 1.0


def test_hermetic_judge_ignores_the_opaque_id() -> None:
    """Score depends on the text alone, so a reseeded label cannot move it."""
    judge = HermeticJudge()
    first = _request(SLOP_TEXT)
    second = JudgeRequest(
        candidate_id="fedcba9876543210",
        dimension=first.dimension,
        rubric_version=first.rubric_version,
        candidate_text=first.candidate_text,
        reference_text=first.reference_text,
    )
    assert judge(first) == judge(second)


def test_routed_judge_resolves_through_the_router_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontier judge is a routed stage, never a direct client."""
    seen: list[tuple[str, PrivacyTier | None]] = []
    router = _local_router()
    original = router.resolve_role

    def _spy(role: str, tier: PrivacyTier | None = None) -> LLMConfig:
        seen.append((role, tier))
        return original(role, tier)

    monkeypatch.setattr(router, "resolve_role", _spy)
    judge = RoutedJudge(router, provider_factory=lambda _cfg: _StubProvider())
    assert judge(_request(SLOP_TEXT)) == pytest.approx(0.82)
    assert seen == [(JUDGE_ROLE, PrivacyTier.OPEN)]


def test_routed_judge_is_declared_cloud() -> None:
    """It is opt-in precisely because it leaves the device."""
    assert RoutedJudge(
        _local_router(), provider_factory=lambda _c: _StubProvider()
    ).is_cloud


def test_routed_judge_sends_only_the_blinded_payload() -> None:
    """The prompt carries the candidate's text and id, never its model name."""
    prompts: list[str] = []
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(prompts=prompts),
    )
    judge(_request(SLOP_TEXT))
    assert prompts and SLOP_TEXT in prompts[0]
    assert "stub" not in prompts[0]


def test_routed_judge_refuses_an_unavailable_provider() -> None:
    """A missing key is a loud refusal, not a silently zero score."""
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(available=False),
    )
    with pytest.raises(JudgeUnavailableError):
        judge(_request(SLOP_TEXT))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("0.4", 0.4), ("score: 1.0", 1.0), ("1", 1.0), ("0", 0.0), ("  0.25  ", 0.25)],
)
def test_routed_judge_parses_a_numeric_verdict(answer: str, expected: float) -> None:
    """The judge's reply is reduced to one number in the unit interval."""
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(text=answer),
    )
    assert judge(_request(SLOP_TEXT)) == pytest.approx(expected)


@pytest.mark.parametrize("answer", ["no score here", "", "7.5", "-2"])
def test_routed_judge_refuses_an_unparsable_verdict(answer: str) -> None:
    """A reply that is not a unit score is a refusal, never a guess."""
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(text=answer),
    )
    with pytest.raises(JudgeUnavailableError, match="score"):
        judge(_request(SLOP_TEXT))


@pytest.mark.parametrize(
    "answer",
    [
        "Rubric version 1.0.0, dimension voice. score: 0.35",
        "Per rubric 1.0.0 the score is 0.1",
        "1.0.0",
        "The candidate scores 1.0 on voice but 0.2 on grounding.",
    ],
)
def test_routed_judge_refuses_a_chatty_or_version_echoing_verdict(answer: str) -> None:
    """A prose reply must refuse, never yield a number mined out of its text.

    ``1.0.0`` is the rubric version, and a lenient scan read a perfect ``1.0``
    out of it — a fabricated pass in the direction that looks like success.
    """
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(text=answer),
    )
    with pytest.raises(JudgeUnavailableError, match="score"):
        judge(_request(SLOP_TEXT))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("0.35", 0.35),
        ("0.35.", 0.35),
        ("score: 0.35", 0.35),
        ("score = 0.35", 0.35),
        ("Score: 0.35", 0.35),
        ("0", 0.0),
        ("1", 1.0),
        ("0.0", 0.0),
        ("1.0", 1.0),
        (".5", 0.5),
    ],
)
def test_routed_judge_accepts_a_bare_score_reply(answer: str, expected: float) -> None:
    """The replies the prompt actually asks for all still parse."""
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(text=answer),
    )
    assert judge(_request(SLOP_TEXT)) == pytest.approx(expected)


@pytest.mark.parametrize("answer", ["1.5", "1.0001", "-0.2", "", "   ", "\n\t"])
def test_routed_judge_refuses_an_out_of_range_or_empty_verdict(answer: str) -> None:
    """Just over the boundary, negative, and blank are all refusals."""
    judge = RoutedJudge(
        _local_router(),
        provider_factory=lambda _cfg: _StubProvider(text=answer),
    )
    with pytest.raises(JudgeUnavailableError, match="score"):
        judge(_request(SLOP_TEXT))


def test_the_judge_prompt_cannot_collide_with_a_score() -> None:
    """The prompt hands the judge no version-shaped string to echo back."""
    assert "{rubric_version}" not in JUDGE_PROMPT
    assert RUBRIC_VERSION not in JUDGE_PROMPT
