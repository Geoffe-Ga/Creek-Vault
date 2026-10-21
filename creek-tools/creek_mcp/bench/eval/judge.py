"""The judge seam: a deterministic hermetic default, an opt-in routed judge.

#1875 asks for "a blinded rubric plus a frontier-model judge". The two halves
are separable, and only the first runs in CI:

- :class:`HermeticJudge` is the default. It scores on four surface-rate
  features of the text alone — no model, key or network — so the whole eval
  runs in the default lane and two runs of one payload are byte-identical. It
  is crude, and says so: it ranks prose, it does not understand it.
- :class:`RoutedJudge` is the frontier option, selected explicitly. It resolves
  its provider through
  :meth:`creek.classify.llm.router.ModelRouter.resolve_role`, like every other
  stage, rather than constructing a client of its own. That is what keeps the
  INTIMATE chokepoint and the consent gate applying to it unchanged. Decision 4
  on #1849 authorizes **no spend yet**, so the only test that reaches a real
  judge is in the ``live`` lane, which CI never selects.

Both are handed a :class:`JudgeRequest`, which has no field for a model
identity — the blinding is structural, not a convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from creek.classify.llm.providers import build_provider
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.classify.llm.base import LLMProvider
    from creek.classify.llm.router import ModelRouter
    from creek.config import LLMConfig
    from creek_mcp.bench.eval.rubric import Dimension

JUDGE_ROLE: Final[str] = "voice_judge"
"""The Writing Desk role the routed judge resolves under.

An unset role falls back to the ``generation`` stage and then to the routing
``default``, so a vault that has configured nothing gets its local model rather
than a surprise cloud call.
"""

JUDGE_MAX_TOKENS: Final[int] = 16
"""Output ceiling: the judge answers with one number, not an essay."""

JUDGE_PROMPT: Final[str] = (
    "You are scoring one candidate passage against a reference passage for a "
    "voice-fidelity rubric (dimension {dimension}).\n"
    "You are not told which model wrote the candidate, and you must not guess.\n"
    "Score how closely the candidate matches the reference's VOICE — sentence "
    "rhythm, diction, person, and concreteness — not its subject matter and "
    "not its quality in the abstract.\n"
    "Answer with a single number between 0 and 1 and nothing else.\n\n"
    "REFERENCE:\n{reference}\n\nCANDIDATE ({candidate_id}):\n{candidate}\n"
)
"""The routed judge's prompt. Only ever filled with synthetic corpus text.

It deliberately does **not** name the rubric version. A version is a semver,
and a judge that merely restated the one it was handed put ``1.0.0`` in the
reply, out of which a lenient parse read a perfect ``1.0``. The version belongs
on the recorded score, not in the judge's input: it tells the judge nothing it
can act on, and :func:`_parse_score` now refuses such a reply anyway.
"""

_SCORE_REPLY: Final[re.Pattern[str]] = re.compile(
    r"(?:score\s*[:=]\s*)?(?P<score>[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\.?",
    re.IGNORECASE,
)
"""The whole of a readable reply: an optional label, one number, a full stop.

Matched with :meth:`re.Pattern.fullmatch` against the stripped reply, so a
number has to *be* the answer rather than merely appear in it. That is what
stops a semver — two dots — or a sentence from yielding a score.
"""
_FIRST_PERSON: Final[frozenset[str]] = frozenset(
    {"i", "me", "my", "mine", "myself", "i'm", "i'd", "i've", "i'll"}
)
_LONG_WORD_CHARS: Final[int] = 8
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"[.!?]+")
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z']+")

_UNAVAILABLE: Final[str] = "the routed judge's provider is not available"
_UNPARSABLE: Final[str] = "the routed judge's reply is not a single score in [0, 1]"


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """The exact payload a judge is handed. It has no identity field.

    Attributes:
        candidate_id: The candidate's opaque label.
        dimension: Which dimension is being judged.
        rubric_version: The rubric this score belongs to; a score is
            comparable only within one version.
        candidate_text: The candidate's output.
        reference_text: The synthetic corpus's own prose.
    """

    candidate_id: str
    dimension: Dimension
    rubric_version: str
    candidate_text: str
    reference_text: str


class Judge(Protocol):
    """Scores one blinded payload in ``[0, 1]``."""

    is_cloud: bool
    """Whether scoring sends the payload off-device."""

    def __call__(self, request: JudgeRequest) -> float:
        """Return *request*'s normalised score."""


class JudgeUnavailableError(RuntimeError):
    """A judge could not produce a score, and refused rather than guessing.

    Raised when a routed provider's prerequisites are unmet, or when its reply
    contains no number in the unit interval. A missing score must never
    collapse to ``0.0``: that is indistinguishable from a measured failure, and
    it is the failure that spends money on the next model up.
    """


def _words(text: str) -> list[str]:
    """Return the alphabetic words in *text*."""
    return _WORD.findall(text.lower())


def _mean_sentence_words(text: str) -> float:
    """Mean words per sentence; ``0.0`` for empty text."""
    words = _words(text)
    if not words:
        return 0.0
    sentences = max(
        len([part for part in _SENTENCE_END.split(text) if part.strip()]), 1
    )
    return len(words) / sentences


def _rate(text: str, matches: int) -> float:
    """Return *matches* per word of *text*; ``0.0`` for empty text."""
    words = _words(text)
    return matches / len(words) if words else 0.0


def _comma_rate(text: str) -> float:
    """Commas per word."""
    return _rate(text, text.count(","))


def _first_person_rate(text: str) -> float:
    """First-person pronouns per word."""
    return _rate(text, sum(1 for word in _words(text) if word in _FIRST_PERSON))


def _long_word_rate(text: str) -> float:
    """Words of :data:`_LONG_WORD_CHARS` characters or more, per word."""
    return _rate(text, sum(1 for word in _words(text) if len(word) >= _LONG_WORD_CHARS))


_FEATURES: Final[tuple[Callable[[str], float], ...]] = (
    _mean_sentence_words,
    _comma_rate,
    _first_person_rate,
    _long_word_rate,
)
"""Surface features the hermetic judge compares.

Chosen because they separate a terse first-person journal voice from generic
AI-essay prose with no model and no training: rhythm, punctuation habit,
person, and latinate vocabulary. They are a proxy, not a reading.
"""


def _symmetric_divergence(left: float, right: float) -> float:
    """Return the normalised symmetric difference of two rates, in ``[0, 1]``."""
    total = abs(left) + abs(right)
    return abs(left - right) / total if total else 0.0


class HermeticJudge:
    """A deterministic, offline judge over surface features of the text.

    Positively local (``is_cloud = False``), like the capacity harness's
    :class:`~creek_mcp.bench.fake.FakeLLM`, so a hermetic eval never has to be
    excused from the no-cloud rule.
    """

    is_cloud: bool = False

    def __call__(self, request: JudgeRequest) -> float:
        """Return ``1 - mean feature divergence`` between candidate and reference.

        Args:
            request: The blinded payload. Only the two texts are read, so a
                reseeded ``candidate_id`` cannot move the score.

        Returns:
            A score in ``[0, 1]``; ``1.0`` when the candidate's measured
            profile equals the reference's.
        """
        divergences = [
            _symmetric_divergence(
                feature(request.candidate_text), feature(request.reference_text)
            )
            for feature in _FEATURES
        ]
        return 1.0 - sum(divergences) / len(divergences)


def _parse_score(text: str) -> float:
    """Return the score *text* consists of, in ``[0, 1]``.

    The parse is strict because :data:`JUDGE_PROMPT` asks for "a single number
    between 0 and 1 and nothing else" under a :data:`JUDGE_MAX_TOKENS` ceiling,
    so anything else is a judge that did not follow the instruction — not a
    reply to be mined.

    Accepted, after stripping surrounding whitespace: a decimal number in the
    unit interval (``0``, ``1``, ``0.35``, ``.5``), optionally behind a
    ``score:`` or ``score =`` label and optionally followed by a single full
    stop.

    Refused: prose around the number ("the score is 0.1"), several numbers, a
    dotted version such as ``1.0.0``, a value outside ``[0, 1]``, and an empty
    or whitespace-only reply.

    Raises:
        JudgeUnavailableError: In every refused case. A reply the harness
            cannot read is a refusal, never a guessed zero — and never, as a
            lenient scan once made it, a fabricated ``1.0`` read out of the
            rubric version.
    """
    match = _SCORE_REPLY.fullmatch(text.strip())
    if match is not None:
        value = float(match.group("score"))
        if 0.0 <= value <= 1.0:
            return value
    raise JudgeUnavailableError(_UNPARSABLE)


class RoutedJudge:
    """A frontier-model judge, resolved through the shared model router.

    It is never the default: a caller selects it explicitly and records
    :attr:`~creek_mcp.bench.eval.rubric.JudgeKind.ROUTED` in the artifact. The
    provider comes from
    :meth:`creek.classify.llm.router.ModelRouter.resolve_role` at
    :data:`JUDGE_ROLE` with tier ``OPEN``, so the routing chokepoint — and the
    ``Intimate``-never-cloud rule behind it — governs this call exactly as it
    governs classification and generation. Nothing here constructs a client.
    """

    is_cloud: bool = True

    def __init__(
        self,
        router: ModelRouter,
        *,
        role: str = JUDGE_ROLE,
        provider_factory: Callable[[LLMConfig], LLMProvider] = build_provider,
    ) -> None:
        """Bind the router and the role this judge resolves under.

        Args:
            router: The shared model router.
            role: The Writing Desk role to resolve; defaults to
                :data:`JUDGE_ROLE`.
            provider_factory: Builds a provider from a resolved config.
                Injected so a test can exercise the routing without a socket;
                production callers take the default.
        """
        self._router = router
        self._role = role
        self._provider_factory = provider_factory

    def __call__(self, request: JudgeRequest) -> float:
        """Ask the routed model to score *request*.

        Args:
            request: The blinded payload. Its text is synthetic corpus and
                candidate prose; the eval has no other kind.

        Returns:
            The parsed score in ``[0, 1]``.

        Raises:
            JudgeUnavailableError: When the provider's prerequisites are unmet
                or its reply carries no readable score.
        """
        config = self._router.resolve_role(self._role, PrivacyTier.OPEN)
        provider = self._provider_factory(config)
        if not provider.available:
            raise JudgeUnavailableError(_UNAVAILABLE)
        completion = provider.complete(
            JUDGE_PROMPT.format(
                dimension=request.dimension.value,
                reference=request.reference_text,
                candidate=request.candidate_text,
                candidate_id=request.candidate_id,
            ),
            max_tokens=JUDGE_MAX_TOKENS,
        )
        return _parse_score(completion.text)
