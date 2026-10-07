"""The content-free record of one benchmark trial.

A :class:`Trial` holds only enums and numbers. It is what the runner returns
in place of the reflection response, so no entry text, prompt or model output
can be carried forward into a summary or the report.

Like :mod:`creek.models`, this module does not use postponed annotations:
pydantic resolves field types at class creation, so they must be importable
at runtime.
"""

from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
)

from creek_mcp.bench.outcome import Outcome


class Sweep(StrEnum):
    """Which capacity question a trial belongs to."""

    COLD_WARM = "cold_warm"
    CONTEXT = "context"
    CONCURRENCY = "concurrency"
    IDLE = "idle"


class Phase(StrEnum):
    """The model's residency state when the trial began."""

    COLD = "cold"
    WARM = "warm"
    RESUME = "resume"


class Trial(BaseModel):
    """One timed reflection, reduced to what the report may carry.

    Attributes:
        sweep: The sweep that ran it.
        phase: Cold (model evicted first), warm, or resume after idle.
        concurrency: How many reflections were in flight together.
        input_words: Words in the reflected entry. Words, not tokens: a word
            is at least one token, so the real token count is never smaller.
        latency_s: Wall-clock seconds for the whole reflection.
        generation_s: Seconds inside the model call alone, or ``None`` when
            the model was never reached. ``latency_s - generation_s`` is the
            grounding-and-overhead share of the shared deadline.
        outcome: How it ended.
        model_resident_bytes: The model's resident size reported by the
            runtime after the trial, or ``None`` when unknown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sweep: Sweep
    phase: Phase
    concurrency: PositiveInt
    input_words: NonNegativeInt
    latency_s: NonNegativeFloat
    generation_s: NonNegativeFloat | None
    outcome: Outcome
    model_resident_bytes: NonNegativeInt | None
