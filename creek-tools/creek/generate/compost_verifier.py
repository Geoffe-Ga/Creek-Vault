"""LLM verifier for compost candidates (FEAT-018 stage 2).

The embedding gate in :mod:`creek.generate.compost_embedding` casts a
wide net. Fragments that cross the similarity threshold are sent here
for precision: the LLM is asked whether this text really describes a
user *abandoning an idea or project*, returning ``yes`` / ``no`` /
``ambiguous`` plus a one-sentence reason.

Ambiguous verdicts route to the operator's compost review queue
(``10-Liminal/Compost/Review``) rather than being filed canonically.

The :class:`SupportsVerifyCompost` protocol lets the
:class:`creek.generate.compost.CompostTracker` accept either the real
:class:`LLMCompostVerifier` or a deterministic stub in tests, mirroring
the calibration seam in :mod:`creek.classify.calibration`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from creek.classify.llm import AnthropicProvider

logger = logging.getLogger(__name__)


class CompostVerdict(StrEnum):
    """Three-state verifier outcome.

    ``YES`` and ``NO`` are decisive; ``AMBIGUOUS`` routes the fragment
    to a review queue for human triage rather than the canonical
    compost folder.
    """

    YES = "yes"
    NO = "no"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CompostVerifierResult:
    """One verifier verdict for one fragment.

    Attributes:
        verdict: Decisive yes/no or ambiguous.
        reasoning: One-sentence explanation surfaced in the compost
            note's frontmatter (``verifier_reasoning`` key) so the
            operator can audit decisions later.
    """

    verdict: CompostVerdict
    reasoning: str


class SupportsVerifyCompost(Protocol):
    """Subset of verifier surface the compost tracker needs.

    Any object exposing :meth:`verify` with this signature is accepted
    by :class:`creek.generate.compost.CompostTracker`. Real verifier
    wraps :class:`creek.classify.llm.AnthropicProvider`; tests pass a
    deterministic stub.
    """

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Classify *title*/*body* as compost / not / ambiguous."""


_PROMPT_TEMPLATE: str = (
    "You are auditing a personal-journal fragment to decide whether it describes "
    "the *abandonment of an idea, project, or thread of thought*. Abandonment "
    "includes: explicit release, drift away by loss of interest, displacement "
    "by something else, exhaustion of attention, and self-honest naming of "
    "indefinite postponement. Abandonment does NOT include: temporary "
    "frustration mid-flow, deciding to take a break, completing the work, or "
    "expressing a paradox.\n"
    "\n"
    "Title: {title}\n"
    "\n"
    "Body:\n"
    "{body}\n"
    "\n"
    "Respond with EXACTLY this format on three lines:\n"
    "VERDICT: <yes|no|ambiguous>\n"
    "REASONING: <one sentence, no more than 25 words>\n"
    "\n"
    "Use ``ambiguous`` when the fragment plausibly reads either way and a "
    "human should triage it."
)

_VERDICT_RE = re.compile(r"^VERDICT:\s*(yes|no|ambiguous)\s*$", re.IGNORECASE)
_REASONING_RE = re.compile(r"^REASONING:\s*(.+?)\s*$", re.IGNORECASE)


def _parse_verifier_response(text: str) -> CompostVerifierResult:
    """Parse the LLM's three-line response into a :class:`CompostVerifierResult`.

    Falls back to ``AMBIGUOUS`` with the raw response as the reason
    when the format is violated, so a malformed response routes to the
    review queue rather than silently dropping the fragment.
    """
    verdict: CompostVerdict | None = None
    reasoning = ""
    for line in text.splitlines():
        verdict_match = _VERDICT_RE.match(line.strip())
        if verdict_match:
            verdict = CompostVerdict(verdict_match.group(1).lower())
            continue
        reasoning_match = _REASONING_RE.match(line.strip())
        if reasoning_match:
            reasoning = reasoning_match.group(1)
    if verdict is None:
        logger.warning(
            "Compost verifier returned unparseable response; routing to review.",
        )
        return CompostVerifierResult(
            verdict=CompostVerdict.AMBIGUOUS,
            reasoning=f"Unparseable verifier response: {text[:120]}",
        )
    return CompostVerifierResult(verdict=verdict, reasoning=reasoning or "(no reason)")


class LLMCompostVerifier:
    """Default :class:`SupportsVerifyCompost` impl backed by Anthropic.

    Wraps an :class:`creek.classify.llm.AnthropicProvider` and parses
    the three-line response format defined by ``_PROMPT_TEMPLATE``.
    Reuses the existing classify infrastructure rather than building
    a parallel transport layer.
    """

    def __init__(self, provider: AnthropicProvider) -> None:
        """Initialise the verifier.

        Args:
            provider: A constructed :class:`AnthropicProvider`. Caller
                is responsible for handling the ``ANTHROPIC_API_KEY``
                and ``CREEK_ANTHROPIC_CONSENT`` env-var preconditions.
        """
        self._provider = provider

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Send *title* + *body* to the LLM and parse the response.

        Args:
            title: Fragment title shown verbatim in the prompt.
            body: Fragment body (truncated by the prompt template
                itself if the caller chooses; this method passes it
                through unchanged).

        Returns:
            A :class:`CompostVerifierResult`. Malformed LLM responses
            yield :attr:`CompostVerdict.AMBIGUOUS` so the fragment
            routes to review rather than being dropped.
        """
        prompt = _PROMPT_TEMPLATE.format(title=title.strip() or "(untitled)", body=body)
        response = self._provider.call(prompt)
        return _parse_verifier_response(response)
