"""The candidate-generation seam: a model and the corpus in, a ``Candidate`` out.

#1850's harness scores candidates; until #1989 nothing produced one, so every
scored :class:`~creek_mcp.bench.eval.blinding.Candidate` was hand-written in a
test. This module is the producer, and it is built around one injected seam so
the whole of it runs with no network:

- :class:`CandidateSource` — one method, prompt in, text out. The hermetic
  default is :class:`FakeCandidateSource`; the live adapter is
  :class:`ProviderSource`, built by :func:`routed_source` through
  :meth:`creek.classify.llm.router.ModelRouter.resolve_role` exactly as the
  routed judge is, so the routing chokepoint governs it unchanged.
- :func:`generate_candidate` — builds the prompt from the corpus and the
  Writing Desk instructions, asks the source once, and parses citations out of
  the reply. Citations are kept **as written**: one the corpus does not contain
  stays on the candidate so the grounding dimension scores it zero, which is
  the whole point of that dimension. :func:`unknown_citations` names them.

**Refusals are pipeline-side, not model-side.** The three refusal probes
measure whether *Creek's guards* refuse — the acute-distress guard and the tier
ceiling — not whether a model does. :data:`~…dimensions.CARE_PROBE_TEXT` is
never sent to any model, and an INTIMATE probe never reaches a provider by any
route (ADR-0016 Decision 5). So :func:`guard_refusals` runs the guard path and
records what it decided; it does not ask the model and believe the answer.
The consequence — that this dimension cannot discriminate between models — is
recorded in the ranked artifact itself
(:data:`~creek_mcp.bench.eval.ranking.NON_DISCRIMINATING`).

**Voice evidence is present by construction.** The prompt carries the corpus's
own entries, so the voice-match dimension is judged rather than reported
``unsupported``. That is a statement about *this* prompt, recorded as
:data:`PROMPT_CARRIES_VOICE_EVIDENCE`, and says nothing about the production
letter prompt #1880 describes.

Everything the prompt carries is the synthetic corpus written for the harness;
no real journal content can reach it, because the only corpus is the one
:func:`~creek_mcp.bench.eval.voice_corpus.build_voice_corpus` writes behind the
confinement gate.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Final, Protocol

from creek.care.guardrail import acute_distress_guard
from creek.classify.llm.providers import build_provider
from creek.classify.llm.router import ModelRouter
from creek.models import PrivacyTier
from creek_mcp.bench.eval.blinding import Candidate
from creek_mcp.bench.eval.dimensions import (
    CARE_PROBE_TEXT,
    PROBE_CEILING,
    PROBE_TIERS,
)
from creek_mcp.bench.eval.rubric import RefusalProbe
from creek_mcp.tier_ceiling import tier_allowed

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek.classify.llm.base import LLMProvider
    from creek.config import LLMConfig
    from creek_mcp.bench.eval.dimensions import DeskInstructions
    from creek_mcp.bench.eval.voice_corpus import VoiceCorpus

CANDIDATE_ROLE: Final[str] = "voice_drafter"
"""The Writing Desk role a live candidate resolves under — the voice drafter."""

PROMPT_CARRIES_VOICE_EVIDENCE: Final[bool] = True
"""Whether :func:`build_prompt` shows the model the writer's voice. It does."""

GENERATION_PROMPT: Final[str] = (
    "You are writing a short reflection in the voice of the person whose "
    "journal entries appear below.\n"
    "Match their voice: sentence rhythm, diction, person and concreteness. "
    "Write as them, in the first person.\n"
    "Write between {min_words} and {max_words} words.\n"
    "{structure}"
    "Never use any of these phrases: {banned}.\n"
    "Ground the reflection in the entries. Cite each entry you draw on by "
    "writing its id in double square brackets, for example [[{example}]]. "
    "Cite only ids that appear below.\n"
    "Answer with the reflection only.\n\n"
    "ENTRIES:\n{entries}\n"
)
"""The one generation prompt. Only ever filled with synthetic corpus text."""

_STRUCTURE: Final[str] = "Use exactly these headings, each on its own line: {}.\n"
_ENTRY_SEPARATOR: Final[str] = "\n\n"
_CITATION: Final[re.Pattern[str]] = re.compile(r"\[\[(?P<id>[^\[\]\s]+)\]\]")
"""A citation is an id in double square brackets, as the prompt asks."""

_PROMPT_ENTRY: Final[re.Pattern[str]] = re.compile(
    r"^\[(?P<id>[^\[\]\s]+)\] (?P<body>.+)$", re.MULTILINE
)
"""One ``[id] body`` line of :data:`GENERATION_PROMPT`'s entry list."""

_FAKE_CITES: Final[int] = 2
_FAKE_OPENING: Final[str] = "I went back through the week tonight."
_FAKE_CLOSING: Final[str] = (
    "I did not make much of it. I wrote it down and left it there. I put the "
    "kettle on. I stood at the window until it boiled. Then I went to bed and "
    "I slept."
)
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s")

_NO_ENTRIES: Final[str] = "the prompt carries no corpus entries to cite"
_UNAVAILABLE: Final[str] = "the candidate model's provider is not available"


class CandidateUnavailableError(RuntimeError):
    """A source could not produce a candidate, and refused rather than guessing.

    An empty candidate would score as a measured failure on every dimension,
    which is precisely the reading that buys a more expensive model.
    """


class CandidateSource(Protocol):
    """Turns one generation prompt into one candidate text."""

    def generate(self, prompt: str) -> str:
        """Return the model's reply to *prompt*."""


def build_prompt(corpus: VoiceCorpus, instructions: DeskInstructions) -> str:
    """Return the generation prompt for *corpus* under *instructions*.

    Every entry appears under its fragment id, so the ids the grounding
    dimension accepts are exactly the ids the model was shown.

    Args:
        corpus: The built synthetic corpus.
        instructions: The Writing Desk instruction surface to state.

    Returns:
        The fully formatted prompt.
    """
    bodies = corpus.voice_sample.split(_ENTRY_SEPARATOR)
    entries = "\n".join(
        f"[{ident}] {body}"
        for ident, body in zip(corpus.fragment_ids, bodies, strict=True)
    )
    structure = (
        _STRUCTURE.format(", ".join(instructions.required_headings))
        if instructions.required_headings
        else ""
    )
    return GENERATION_PROMPT.format(
        min_words=instructions.min_words,
        max_words=instructions.max_words,
        structure=structure,
        banned=", ".join(f'"{phrase}"' for phrase in instructions.banned_phrases),
        example=corpus.fragment_ids[0],
        entries=entries,
    )


def parse_citations(text: str) -> tuple[str, ...]:
    """Return the ids *text* cites, in first-cited order, without repeats.

    Args:
        text: A candidate's reply.

    Returns:
        Every double-bracketed id, whether or not the corpus contains it.
    """
    return tuple(dict.fromkeys(match["id"] for match in _CITATION.finditer(text)))


def unknown_citations(citations: Sequence[str], corpus: VoiceCorpus) -> tuple[str, ...]:
    """Return the citations *corpus* does not contain — invented ones.

    Args:
        citations: A candidate's citations.
        corpus: The corpus the candidate was shown.

    Returns:
        The cited ids absent from ``corpus.fragment_ids``, in order.
    """
    known = frozenset(corpus.fragment_ids)
    return tuple(citation for citation in citations if citation not in known)


def _pipeline_refuses(probe: RefusalProbe) -> bool:
    """Return whether Creek's own guard refuses *probe*. No model is asked."""
    if probe is RefusalProbe.CARE_GUARD:
        return acute_distress_guard(CARE_PROBE_TEXT) is not None
    return not tier_allowed(PROBE_TIERS[probe], PROBE_CEILING)


def guard_refusals() -> dict[RefusalProbe, bool]:
    """Run the guard path for every refusal probe and record its decision.

    Returns:
        One entry per :class:`~creek_mcp.bench.eval.rubric.RefusalProbe`:
        ``True`` when the pipeline's guard refuses before any model is asked.
    """
    return {probe: _pipeline_refuses(probe) for probe in RefusalProbe}


def generate_candidate(
    corpus: VoiceCorpus,
    instructions: DeskInstructions,
    model_tag: str,
    source: CandidateSource,
) -> Candidate:
    """Ask *source* once and return the candidate the harness will score.

    Args:
        corpus: The synthetic corpus the prompt is built from.
        instructions: The Writing Desk instructions the prompt states.
        model_tag: Which model *source* stands for. Harness-side only; it is
            not in the prompt and never reaches the judge.
        source: The injected call seam.

    Returns:
        The candidate: the reply verbatim, every citation it makes (known or
        not), and the guard path's refusal record.
    """
    text = source.generate(build_prompt(corpus, instructions))
    return Candidate(
        model_tag=model_tag,
        text=text,
        citations=parse_citations(text),
        refusals=guard_refusals(),
    )


def _first_sentence(body: str) -> str:
    """Return the first sentence of an entry body."""
    return _SENTENCE_END.split(body.strip(), maxsplit=1)[0]


class FakeCandidateSource:
    """A deterministic, offline source behind ``--mode fake``.

    It answers with a short first-person reflection that quotes the first
    sentence of two entries from the prompt and cites them. Which two depends
    only on the model tag, so two tags produce different but reproducible
    candidates. It is not evidence of anything about a model; a fake run's
    artifact says ``mode: fake``.
    """

    def __init__(self, model_tag: str) -> None:
        """Bind the model tag that selects which entries are cited."""
        self._digest = hashlib.sha256(model_tag.encode()).digest()

    def generate(self, prompt: str) -> str:
        """Return the fake reflection for *prompt*.

        Raises:
            CandidateUnavailableError: When *prompt* carries no entries.
        """
        entries = [
            (match["id"], match["body"]) for match in _PROMPT_ENTRY.finditer(prompt)
        ]
        if not entries:
            raise CandidateUnavailableError(_NO_ENTRIES)
        start = self._digest[0] % len(entries)
        picked = [entries[(start + step) % len(entries)] for step in range(_FAKE_CITES)]
        middle = " ".join(
            f"{_first_sentence(body)} [[{ident}]]" for ident, body in picked
        )
        return f"{_FAKE_OPENING} {middle} {_FAKE_CLOSING}"


class ProviderSource:
    """Adapts an :class:`~creek.classify.llm.base.LLMProvider` to the seam.

    One completion per candidate, capped at ``max_tokens`` — the bound that
    keeps a live run's spend proportional to the number of models named.
    """

    def __init__(self, provider: LLMProvider, *, max_tokens: int) -> None:
        """Bind the provider and the per-candidate output ceiling."""
        self._provider = provider
        self._max_tokens = max_tokens

    def generate(self, prompt: str) -> str:
        """Ask the provider for one completion of *prompt*.

        Raises:
            CandidateUnavailableError: When the provider's prerequisites are
                unmet; nothing is sent.
        """
        if not self._provider.available:
            raise CandidateUnavailableError(_UNAVAILABLE)
        return self._provider.complete(prompt, max_tokens=self._max_tokens).text


def routed_source(
    config: LLMConfig,
    *,
    max_tokens: int,
    provider_factory: Callable[[LLMConfig], LLMProvider] = build_provider,
) -> ProviderSource:
    """Build a live source for one candidate model through the model router.

    The config is resolved at :data:`CANDIDATE_ROLE` with tier ``OPEN`` — the
    synthetic corpus is open by construction — so the router's chokepoint
    applies exactly as it does to every other generation stage.

    Args:
        config: The candidate model's provider config.
        max_tokens: Output ceiling per candidate.
        provider_factory: Builds the provider; injected so tests never dial.

    Returns:
        The live source.
    """
    resolved = ModelRouter(config).resolve_role(CANDIDATE_ROLE, PrivacyTier.OPEN)
    return ProviderSource(provider_factory(resolved), max_tokens=max_tokens)
