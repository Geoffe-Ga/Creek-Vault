"""Measured prompt and output tokens per reflection surface (#1893).

ADR-0016 sized every cost figure from one unmeasured planning assumption:
2,500 prompt tokens and 128 output tokens per reflection. The pilot's monthly
ceiling, the per-1,000-reflections cost and the per-activation vendor-key cap
all derive from it, and the owner's ratification note says plainly that it is
"placeholder arithmetic until #1850 measures real token counts". This module
measures them.

**Three surfaces, separately.** The cap formula and the cost figures are
per-surface, and grounding changes the prompt materially, so the pass measures
the ungrounded reflection, the grounded reflection (every grounding slot
filled, as the production grounder would fill it) and the letter route on its
own far larger output budget. Each goes through the shipped path —
:func:`~creek_mcp.tools.reflect.reflect_tool` and
:func:`~creek_mcp.tools.letter.write_letter` — over the synthetic corpus. This
measures what ships; it changes no prompt and no cap.

**The overhead allowance becomes a measurement.**
:data:`PROMPT_OVERHEAD_TOKENS` was an asserted upper bound over the template's
*byte* length. :attr:`TokensPerReflection.prompt_overhead_tokens` is what the
provider actually counted for the grounded template around a one-word entry,
so a template that outgrows the allowance is reported rather than silently
truncating a sweep.

**No spend.** Every count here is measurable in ``--mode fake`` and on a pinned
local runtime; nothing in this module reaches a vendor. The counts carry their
:class:`~creek_mcp.bench.usage.TokenBasis`, so a cost line can tell a citable
figure from a fake run's upper bound.

**Counts, never content.** Each surface contributes four integers, a boolean
and an outcome enum. The prompt that produced them is not kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt

from creek.care.guardrail import acute_distress_guard
from creek.models import PrivacyTier
from creek.voice_drafts import VoiceDraftWriteResult
from creek_mcp.bench.corpus import (
    CONSUMER,
    entry_text,
    filled_grounding,
    no_grounding,
)
from creek_mcp.bench.outcome import (
    TRIAL_FAILURES,
    Outcome,
    TrialProbe,
    classify_outcome,
    probe_factory,
    reflect_outcome,
)
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.letter import LetterCall, LetterSource, write_letter
from creek_mcp.tools.reflect import reflect_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from creek_mcp.bench.protocols import LLMFactory

logger = logging.getLogger(__name__)

PROMPT_OVERHEAD_TOKENS: Final[int] = 1536
"""Token allowance for reflect's prompt around the entry.

An upper bound, not an estimate: it covers the template's *byte* length with
every grounding slot filled and the largest note budget (1,448 bytes at
``a5d28a5``), and a byte-level tokenizer never spends less than one byte per
token. ``tests/bench/test_runner.py`` fails if reflect's template outgrows it,
and :attr:`TokensPerReflection.prompt_overhead_within_bound` reports whether
the *measured* overhead has reached it.
"""

ADR_0016_ASSUMED_PROMPT_TOKENS: Final[int] = 2500
"""ADR-0016's planning assumption for prompt tokens per reflection.

Recorded beside the measurement, not used by it: a figure quoting this number
is an unmeasured placeholder, and carrying it in the report is what lets a cost
line show which of the two it cited.
"""

ADR_0016_ASSUMED_OUTPUT_TOKENS: Final[int] = 128
"""ADR-0016's planning assumption for output tokens per reflection."""

_OVERHEAD_PROBE_WORDS: Final[int] = 1
"""Entry size for the overhead probe: one word, so the prompt is nearly all
template. The word is counted *in* the measured overhead, which can only make
the comparison against the allowance stricter."""

_LETTER_EXTERNAL_ID: Final[str] = "bench-token-measurement"
"""The Voice Draft slot the letter surface writes into, in the temp corpus."""

_LETTER_NOTE: Final[str] = "You keep returning to this image."
"""The synthetic margin note the letter surface expands; neutral by design."""

_LETTER_KIND: Final[str] = "pattern"
_ANCHOR_WORDS: Final[int] = 6
"""Words of the synthetic entry the synthetic note anchors to."""

_UNGROUNDED_OFFSET: Final[int] = 0
_GROUNDED_OFFSET: Final[int] = 1
_LETTER_OFFSET: Final[int] = 2
_OVERHEAD_OFFSET: Final[int] = 3
"""Entry-index offsets, so no two surfaces measure the same text."""


class Surface(StrEnum):
    """A model-calling surface whose token cost is sized on its own.

    Attributes:
        REFLECTION_UNGROUNDED: ``reflect_tool`` with no grounding lines.
        REFLECTION_GROUNDED: ``reflect_tool`` with every grounding slot
            filled, which is what a vault with a built index pays.
        LETTER: ``write_letter``, whose output ceiling is an order of
            magnitude above a reflection's.
    """

    REFLECTION_UNGROUNDED = "reflection_ungrounded"
    REFLECTION_GROUNDED = "reflection_grounded"
    LETTER = "letter"


class SurfaceTokens(BaseModel):
    """One surface's measured token cost for a single turn.

    Attributes:
        surface: Which surface was measured.
        prompt_tokens: The provider's prompt count, or ``None`` unreported.
        output_tokens: The provider's output count, or ``None`` unreported.
        max_output_tokens: The ceiling the surface asked the factory for.
        output_cap_exceeded: Whether the measured output ran past that
            ceiling. Reported, never clipped.
        outcome: How the measuring turn ended.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: Surface
    prompt_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    max_output_tokens: NonNegativeInt | None
    output_cap_exceeded: bool
    outcome: Outcome


class TokensPerReflection(BaseModel):
    """The measured token block every cost figure must cite.

    Attributes:
        basis: Where the counts came from, and so whether a cost line may
            quote them. :attr:`~creek_mcp.bench.usage.TokenBasis.NOT_MEASURED`
            means the pass did not run.
        per_surface: One measurement per :class:`Surface`; empty when the pass
            did not run.
        prompt_overhead_tokens: Measured tokens for the grounded template
            around a one-word entry, or ``None`` when unreported.
        prompt_overhead_bound: :data:`PROMPT_OVERHEAD_TOKENS`, the allowance
            the context sweep subtracts from ``num_ctx``.
        prompt_overhead_within_bound: Whether the measured overhead is at or
            below that allowance; ``None`` when nothing was measured. ``False``
            means the allowance has become the binding constraint rather than a
            ceiling, and the context sweep's sizes are no longer safe.
        assumed_prompt_tokens: :data:`ADR_0016_ASSUMED_PROMPT_TOKENS`, carried
            so the figure this block replaces stays visible beside it.
        assumed_output_tokens: :data:`ADR_0016_ASSUMED_OUTPUT_TOKENS`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: TokenBasis
    per_surface: dict[Surface, SurfaceTokens]
    prompt_overhead_tokens: NonNegativeInt | None
    prompt_overhead_bound: PositiveInt
    prompt_overhead_within_bound: bool | None
    assumed_prompt_tokens: PositiveInt
    assumed_output_tokens: PositiveInt


@dataclass(frozen=True, slots=True)
class TokenMeasurement:
    """What the per-surface pass needs.

    Attributes:
        vault_path: The synthetic corpus root, used as the vault.
        factory: The run's model factory, wrapped per surface by the probe.
        clock: Monotonic clock, for the probe's own timing.
        seed: The corpus seed, so the measured entries are reproducible.
        index: First unused entry index; each surface takes one after it.
        words: Words in each measured entry.
    """

    vault_path: Path
    factory: LLMFactory
    clock: Callable[[], float]
    seed: int
    index: int
    words: int

    def entry(self, offset: int, words: int) -> str:
        """Return fresh entry text for the surface at *offset*."""
        return entry_text(seed=self.seed, index=self.index + offset, words=words)


@dataclass(frozen=True, slots=True)
class _Measured:
    """One surface's report, plus where its counts came from.

    Attributes:
        tokens: The integers the report carries.
        basis: The basis the provider labelled its counts with, or ``None``
            when it reported none.
    """

    tokens: SurfaceTokens
    basis: TokenBasis | None


def _measured(surface: Surface, probe: TrialProbe, outcome: Outcome) -> _Measured:
    """Reduce one measuring turn to the integers the report may carry."""
    usage = probe.usage
    output = usage.output_tokens if usage is not None else None
    cap = probe.max_output_tokens
    return _Measured(
        tokens=SurfaceTokens(
            surface=surface,
            prompt_tokens=usage.prompt_tokens if usage is not None else None,
            output_tokens=output,
            max_output_tokens=cap,
            output_cap_exceeded=output is not None and cap is not None and output > cap,
            outcome=outcome,
        ),
        basis=usage.basis if usage is not None else None,
    )


def _measure_reflection(
    request: TokenMeasurement, surface: Surface, *, offset: int, words: int
) -> _Measured:
    """Measure one reflection turn, grounded or not, at *offset*.

    Only the exception's type name is ever logged: a message can carry model
    output or entry text.
    """
    probe = TrialProbe(clock=request.clock)
    grounded = surface is not Surface.REFLECTION_UNGROUNDED
    exc: BaseException | None = None
    result: dict[str, Any] | None = None
    try:
        result = reflect_tool(
            vault_path=request.vault_path,
            llm_factory=probe_factory(request.factory, probe),
            content=request.entry(offset, words),
            retrieve=filled_grounding if grounded else no_grounding,
            care_guard=acute_distress_guard,
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer=CONSUMER,
        )
    except TRIAL_FAILURES as caught:
        exc = caught
        logger.debug("bench token surface raised %s", type(caught).__name__)
    return _measured(surface, probe, reflect_outcome(probe, exc, result))


def _letter_source(request: TokenMeasurement) -> LetterSource:
    """Build the synthetic letter source: the caller's own entry and note."""
    content = request.entry(_LETTER_OFFSET, request.words)
    return LetterSource(
        content=content,
        note=_LETTER_NOTE,
        anchor_text=" ".join(content.split()[:_ANCHOR_WORDS]),
        kind=_LETTER_KIND,
        tier=PrivacyTier.OPEN,
        title=None,
    )


def _measure_letter(request: TokenMeasurement) -> _Measured:
    """Measure one letter turn through the shipped ``write_letter`` path."""
    probe = TrialProbe(clock=request.clock)
    call = LetterCall(
        vault_path=request.vault_path,
        external_id=_LETTER_EXTERNAL_ID,
        ceiling=TierCeiling.OPEN,
        consumer=CONSUMER,
        build_factory=lambda: probe_factory(request.factory, probe),
        care_guard=acute_distress_guard,
    )
    outcome = Outcome.OK
    try:
        written = write_letter(call, _letter_source(request))
    except TRIAL_FAILURES as caught:
        logger.debug("bench letter surface raised %s", type(caught).__name__)
        outcome = probe.outcome or classify_outcome(caught)
    else:
        if not isinstance(written, VoiceDraftWriteResult):
            outcome = probe.outcome or Outcome.ERROR
    return _measured(Surface.LETTER, probe, outcome)


def _measure_overhead(request: TokenMeasurement) -> _Measured:
    """Measure the grounded template's own prompt tokens, around one word."""
    return _measure_reflection(
        request,
        Surface.REFLECTION_GROUNDED,
        offset=_OVERHEAD_OFFSET,
        words=_OVERHEAD_PROBE_WORDS,
    )


def _basis(measured: Sequence[_Measured]) -> TokenBasis:
    """Return the basis the run's counts carry, or ``UNREPORTED``.

    One backend serves a whole run, so the first *reporting* surface's basis is
    the run's; a surface that measured nothing does not get to label the run,
    so a single omitted counter cannot demote a run whose other turns were
    counted. A run where none reported is unreported, not estimated.
    """
    for one in measured:
        if one.basis is not None and one.basis is not TokenBasis.UNREPORTED:
            return one.basis
    return TokenBasis.UNREPORTED


def measure_tokens_per_reflection(request: TokenMeasurement) -> TokensPerReflection:
    """Measure every surface once and assemble the run-level token block.

    One untimed turn per surface, warm, after the sweeps: this measures what a
    reflection *costs*, not how long it takes, so it neither evicts the model
    nor contributes a :class:`~creek_mcp.bench.trial.Trial`.

    Args:
        request: The corpus, factory and entry sizes to measure against.

    Returns:
        The block ADR-0016's cost figures must cite in place of the 2,500 + 128
        planning assumption.
    """
    surfaces = [
        _measure_reflection(
            request,
            Surface.REFLECTION_UNGROUNDED,
            offset=_UNGROUNDED_OFFSET,
            words=request.words,
        ),
        _measure_reflection(
            request,
            Surface.REFLECTION_GROUNDED,
            offset=_GROUNDED_OFFSET,
            words=request.words,
        ),
        _measure_letter(request),
    ]
    probed = _measure_overhead(request)
    overhead = probed.tokens.prompt_tokens
    return TokensPerReflection(
        basis=_basis([*surfaces, probed]),
        per_surface={one.tokens.surface: one.tokens for one in surfaces},
        prompt_overhead_tokens=overhead,
        prompt_overhead_bound=PROMPT_OVERHEAD_TOKENS,
        prompt_overhead_within_bound=(
            None if overhead is None else overhead <= PROMPT_OVERHEAD_TOKENS
        ),
        assumed_prompt_tokens=ADR_0016_ASSUMED_PROMPT_TOKENS,
        assumed_output_tokens=ADR_0016_ASSUMED_OUTPUT_TOKENS,
    )


def unmeasured_tokens() -> TokensPerReflection:
    """Return the block a run that skipped the pass reports.

    Explicit rather than absent: ``NOT_MEASURED`` says the pass did not run,
    which a downstream cost figure must treat differently from a backend that
    ran and reported nothing.
    """
    return TokensPerReflection(
        basis=TokenBasis.NOT_MEASURED,
        per_surface={},
        prompt_overhead_tokens=None,
        prompt_overhead_bound=PROMPT_OVERHEAD_TOKENS,
        prompt_overhead_within_bound=None,
        assumed_prompt_tokens=ADR_0016_ASSUMED_PROMPT_TOKENS,
        assumed_output_tokens=ADR_0016_ASSUMED_OUTPUT_TOKENS,
    )
