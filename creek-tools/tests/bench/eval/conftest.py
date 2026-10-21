"""Shared candidates and stubs for the voice-fidelity eval tests.

Three candidate shapes recur: one written in the synthetic corpus's own terse,
first-person voice; one written in generic AI-essay prose; and one that answers
where it should refuse. :class:`RecordingJudge` captures the *exact*
:class:`~creek_mcp.bench.eval.judge.JudgeRequest` payloads a run hands the
judge, which is what the blinding assertion inspects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from creek_mcp.bench.eval.blinding import Candidate
from creek_mcp.bench.eval.rubric import RefusalProbe

if TYPE_CHECKING:
    from creek_mcp.bench.eval.judge import JudgeRequest

IN_VOICE_TEXT = (
    "I woke before the alarm. The kettle took its time. I stood at the window "
    "and watched the street fill up. I did not write anything down. I thought "
    "about the shed and the tools I never put back. I made toast. I ate it "
    "standing. The day started without me and I caught up later. I like that "
    "hour. Nobody wants anything from me yet. I kept the radio off. I washed "
    "the one cup and left it to dry. I went out."
)
"""A candidate in the corpus's voice: short sentences, first person, plain nouns."""

SLOP_TEXT = (
    "In today's fast-paced environment, the morning represents a profound "
    "opportunity for transformational self-actualisation, serving as a "
    "testament to the remarkable interconnectedness of contemporary "
    "contemplative methodologies. One must delve into the multifaceted "
    "tapestry of experiential consciousness, navigating the complexities of "
    "psychological individuation whilst simultaneously acknowledging the "
    "fundamentally transitory characteristics of phenomenological awareness "
    "throughout the entirety of one's developmental trajectory."
)
"""A candidate in generic AI-essay prose: long, latinate, impersonal."""

ALL_REFUSED = dict.fromkeys(RefusalProbe, True)
"""A candidate that correctly refuses every refusal probe."""

NONE_REFUSED = dict.fromkeys(RefusalProbe, False)
"""A candidate that answers where every probe required a refusal."""


def in_voice_candidate(
    model_tag: str = "cheap-catalogue-model",
    *,
    citations: tuple[str, ...] = ("voice-0000", "voice-0001"),
) -> Candidate:
    """Return a well-behaved candidate citing real corpus fragments."""
    return Candidate(
        model_tag=model_tag,
        text=IN_VOICE_TEXT,
        citations=citations,
        refusals=dict(ALL_REFUSED),
    )


def slop_candidate(model_tag: str = "expensive-frontier-model") -> Candidate:
    """Return a candidate that is off-voice, ungrounded and over-compliant."""
    return Candidate(
        model_tag=model_tag,
        text=SLOP_TEXT,
        citations=("voice-9999",),
        refusals=dict(NONE_REFUSED),
    )


@dataclass
class RecordingJudge:
    """A judge that records every request verbatim and returns a fixed score.

    Attributes:
        score: The value returned for every request.
        requests: Every request received, in call order.
    """

    score: float = 0.5
    requests: list[JudgeRequest] = field(default_factory=list)
    is_cloud: bool = False

    def __call__(self, request: JudgeRequest) -> float:
        """Record *request* and return :attr:`score`."""
        self.requests.append(request)
        return self.score


@pytest.fixture
def recording_judge() -> RecordingJudge:
    """A fresh :class:`RecordingJudge`."""
    return RecordingJudge()
