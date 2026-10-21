"""Closed trial-outcome vocabulary, classification, and the trial probe.

Every trial ends in exactly one :class:`Outcome`. The report carries only that
enum — never an exception message, response body or model output — so the
classification below is the only place a failure's *content* is inspected,
and nothing it reads is kept.

:func:`probe_factory` exists because ``reflect_tool`` builds the LLM and calls
it inside one ``try`` that converts any ``RuntimeError`` (provider missing,
``IntimateRoutingError``) into a refusal dict. Without a probe the harness
would see only that dict and could not tell a model that is down from a
routing refusal. The probe wraps **both** the factory call and the returned
callable, records the classified outcome of the first failure, and re-raises
so reflect's own behaviour is unchanged.

The probe is also where the harness reads what the model *spent* (#1893): the
output ceiling reflect asked the factory for, and the provider's own token
usage when it reports any. Both are numbers; neither is content.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import httpx

from creek_mcp.bench.usage import TokenUsage, UsageReporting

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.models import PrivacyTier
    from creek_mcp.bench.protocols import LLMCallable, LLMFactory


class Outcome(StrEnum):
    """How one trial ended. Closed: the report schema admits nothing else."""

    OK = "ok"
    TIMEOUT = "timeout"
    OOM = "oom"
    DISK_FULL = "disk_full"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CONTEXT_OVERFLOW = "context_overflow"
    RATE_LIMITED = "rate_limited"
    INSUFFICIENT_QUOTA = "insufficient_quota"
    ERROR = "error"


class ProviderUnavailableError(RuntimeError):
    """The bench's local model endpoint could not be reached.

    A ``RuntimeError`` so ``reflect_tool`` degrades it to its ordinary
    "reflection unavailable" refusal, exactly as production does for a
    missing provider; the probe is what keeps the distinction.
    """


class ContextOverflowError(RuntimeError):
    """The prompt filled the pinned context window, so the runtime truncated it.

    Ollama answers a truncated prompt instead of failing; a trial that timed a
    shorter prompt than it reports must not count as ``ok``.
    """


class PoolRateLimitedError(RuntimeError):
    """The inference pool refused this trial for admission pressure (429).

    A pool answers ``rate_limited`` when it is serving as much as it will
    serve right now (ADR-0016 Decision 1 item 4). It is transient, and it is
    emphatically not a success: a refusal returns in milliseconds and would
    drag p95 *down* if it were counted as one.
    """


class PoolQuotaExhaustedError(RuntimeError):
    """The pool's vendor key has spent its cap (429, non-transient).

    Distinct from :class:`PoolRateLimitedError` because retrying cannot fix
    it: under ADR-0016 Decision 3 item 4 an exhausted per-activation cap
    answers ``insufficient_quota`` until the cap is raised or reset.
    """


_OLLAMA_OOM_MARKERS: Final[tuple[str, ...]] = (
    "requires more system memory",
    "out of memory",
)
"""Lower-cased fragments of Ollama's 5xx body when a model cannot be loaded."""

_DISK_FULL_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENOSPC, errno.EDQUOT})
"""``errno`` values meaning the filesystem (or the quota on it) is full."""

_SERVER_ERROR_FLOOR: Final[int] = 500
"""Lowest HTTP status that is a server-side failure."""

ANSWERED: Final[frozenset[str]] = frozenset({"ok", "empty"})
"""Reflect statuses meaning the model answered.

``empty`` (no verbatim-valid note survived parsing) is a *quality* result, not
a capacity failure: the model was reached and replied within the deadline.
Quality is scored separately (the report's ``quality_score`` hook)."""

TRIAL_FAILURES: Final[tuple[type[BaseException], ...]] = (
    MemoryError,
    OSError,
    httpx.HTTPError,
    RuntimeError,
    ValueError,
)
"""Exception types a trial may end in. Anything else is a harness bug.

Lives here beside :func:`classify_outcome`, which is what every caller does
with one, so the runner and the per-surface token pass share one list rather
than drifting apart."""


def _is_oom_response(exc: BaseException) -> bool:
    """Return whether *exc* is an Ollama 5xx reporting it ran out of memory.

    Only the boolean survives: the body is matched against fixed markers and
    then dropped, so no response text can reach the report or a log.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code < _SERVER_ERROR_FLOOR:
        return False
    body = exc.response.text.lower()
    return any(marker in body for marker in _OLLAMA_OOM_MARKERS)


def classify_outcome(exc: BaseException | None) -> Outcome:
    """Map a trial's exception (or ``None``) onto the closed outcome enum.

    The order is load-bearing. ``TimeoutError`` subclasses ``OSError``, so it is
    tested before the disk-full check; otherwise a timeout carrying an
    ``ENOSPC`` errno would be misfiled. ``IntimateRoutingError`` is a privacy
    refusal, not an availability failure, so it falls through to ``error``.

    Args:
        exc: The exception the trial raised, or ``None`` when it succeeded.

    Returns:
        The single outcome for the trial.
    """
    if exc is None:
        return Outcome.OK
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return Outcome.TIMEOUT
    if isinstance(exc, MemoryError) or _is_oom_response(exc):
        return Outcome.OOM
    if isinstance(exc, OSError) and exc.errno in _DISK_FULL_ERRNOS:
        return Outcome.DISK_FULL
    if isinstance(exc, httpx.ConnectError | ProviderUnavailableError):
        return Outcome.PROVIDER_UNAVAILABLE
    if isinstance(exc, ContextOverflowError):
        return Outcome.CONTEXT_OVERFLOW
    if isinstance(exc, PoolRateLimitedError):
        return Outcome.RATE_LIMITED
    if isinstance(exc, PoolQuotaExhaustedError):
        return Outcome.INSUFFICIENT_QUOTA
    return Outcome.ERROR


@dataclass
class TrialProbe:
    """What one trial's model seam observed.

    Attributes:
        clock: Monotonic clock used to time the completion call.
        outcome: The classified outcome of the first failure at the seam, or
            ``None`` when nothing at the seam failed.
        generation_s: Seconds spent inside the completion callable, or
            ``None`` when it was never reached.
        usage: The provider's own token report for the first completion, or
            ``None`` when the backend reported none (or raised before it
            could). Never derived — see :mod:`creek_mcp.bench.usage`.
        max_output_tokens: The output ceiling the factory was asked to build
            with, or ``None`` when the factory was never reached.
    """

    clock: Callable[[], float]
    outcome: Outcome | None = None
    generation_s: float | None = None
    usage: TokenUsage | None = None
    max_output_tokens: int | None = None

    def record(self, exc: BaseException) -> None:
        """Keep the classified outcome of the first failure only."""
        if self.outcome is None:
            self.outcome = classify_outcome(exc)

    def record_usage(self, llm: object) -> None:
        """Keep the first usage report *llm* offers, if it offers one.

        First-report-wins, like :meth:`record`: one trial is one reflection,
        and a second turn's counts would silently replace the measured one.
        """
        if self.usage is None and isinstance(llm, UsageReporting):
            self.usage = llm.token_usage()


def probe_factory(factory: LLMFactory, probe: TrialProbe) -> LLMFactory:
    """Wrap *factory* so *probe* sees failures reflect would otherwise swallow.

    Args:
        factory: The factory ``reflect_tool`` would have been given.
        probe: Receives the classified failure and the generation time.

    Returns:
        A factory of the same shape whose failures are recorded, then
        re-raised unchanged.
    """

    def _probed_factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        probe.max_output_tokens = max_tokens
        try:
            llm = factory(tier, max_tokens=max_tokens)
        except Exception as exc:
            probe.record(exc)
            raise

        def _probed_llm(prompt: str) -> str:
            start = probe.clock()
            try:
                completion = llm(prompt)
            except Exception as exc:
                probe.record(exc)
                raise
            else:
                probe.record_usage(llm)
                return completion
            finally:
                probe.generation_s = probe.clock() - start

        return _probed_llm

    return _probed_factory


def reflect_outcome(
    probe: TrialProbe, exc: BaseException | None, result: dict[str, Any] | None
) -> Outcome:
    """Decide a reflection's outcome from what the probe and reflect each saw.

    The probe wins: it saw the exception ``reflect_tool`` turned into a refusal
    dict, which is the whole reason it exists.

    Args:
        probe: The seam's observations for this turn.
        exc: The exception the call raised, or ``None``.
        result: ``reflect_tool``'s response, or ``None`` when it raised.

    Returns:
        The single outcome for the turn.
    """
    if probe.outcome is not None:
        return probe.outcome
    if exc is not None:
        return classify_outcome(exc)
    if result is not None and result.get("status") in ANSWERED:
        return Outcome.OK
    return Outcome.ERROR
