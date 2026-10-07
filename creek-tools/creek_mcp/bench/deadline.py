"""The deadline a reflection must fit, and the verdict on whether it does.

**One shared budget, derived, never restated.** A ``POST /v1/reflections`` is
shed by whichever fires first: the HTTP server's request deadline
(:data:`creek_mcp.httpapi.middleware.limits.DEFAULT_TIMEOUT_SECONDS`) or the
Ollama provider's request timeout
(:attr:`creek.classify.llm.providers.OllamaProvider.REQUEST_TIMEOUT`).
Grounding, embedding and generation all spend from that one budget — the model
has no separate allowance — so the harness judges the *whole* reflection
against the tighter of the two. Both are read through their module or class
at call time, so a change to either moves the verdict with it and a test can
patch them.

The Adepthood client imposes its own total deadline on the same call (per-try
timeout times attempts in ``creek_vault_client.py``). That lives in another
repository and is documented here rather than imported; the server-side budget
above is the one this process enforces.

Splitting the budget into per-stage allowances is an owner decision (the
sync-versus-async protocol question in D04), not something this harness
enacts. What it does provide is the evidence: each trial records the model
call's share of the latency separately (``Trial.generation_s``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from creek.classify.llm import providers
from creek_mcp.httpapi.middleware import limits

if TYPE_CHECKING:
    from creek_mcp.bench.stats import LatencySummary

MAX_FIT_ERROR_RATE: Final[float] = 0.0
"""The error rate an envelope may show and still fit: none.

A trial that ended in timeout, out-of-memory, disk-full or an unavailable
model is a reflection a user did not get, however fast it failed.
"""


class Verdict(StrEnum):
    """Whether a group of trials fits the deadline."""

    FITS = "fits"
    EXCEEDS = "exceeds"
    INSUFFICIENT_DATA = "insufficient_data"


def deadline_budget_seconds() -> float:
    """Return the seconds one reflection may take before it is shed.

    Returns:
        The tighter of the server request deadline and the Ollama request
        timeout, read at call time.
    """
    return min(limits.DEFAULT_TIMEOUT_SECONDS, providers.OllamaProvider.REQUEST_TIMEOUT)


def deadline_verdict(summary: LatencySummary, budget: float | None = None) -> Verdict:
    """Judge *summary* against the deadline.

    Args:
        summary: Aggregates over the trials being judged.
        budget: Seconds allowed; defaults to :func:`deadline_budget_seconds`.

    Returns:
        ``insufficient_data`` when no trial succeeded; ``exceeds`` when p95 is
        over the budget or any trial failed; otherwise ``fits``. The budget is
        inclusive: a p95 exactly at it fits.
    """
    limit = deadline_budget_seconds() if budget is None else budget
    if summary.p95_s is None:
        return Verdict.INSUFFICIENT_DATA
    if summary.p95_s > limit or summary.error_rate > MAX_FIT_ERROR_RATE:
        return Verdict.EXCEEDS
    return Verdict.FITS
