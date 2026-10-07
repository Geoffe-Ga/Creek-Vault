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
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import httpx

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


_OLLAMA_OOM_MARKERS: Final[tuple[str, ...]] = (
    "requires more system memory",
    "out of memory",
)
"""Lower-cased fragments of Ollama's 5xx body when a model cannot be loaded."""

_DISK_FULL_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENOSPC, errno.EDQUOT})
"""``errno`` values meaning the filesystem (or the quota on it) is full."""

_SERVER_ERROR_FLOOR: Final[int] = 500
"""Lowest HTTP status that is a server-side failure."""


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
    """

    clock: Callable[[], float]
    outcome: Outcome | None = None
    generation_s: float | None = None

    def record(self, exc: BaseException) -> None:
        """Keep the classified outcome of the first failure only."""
        if self.outcome is None:
            self.outcome = classify_outcome(exc)


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
        try:
            llm = factory(tier, max_tokens=max_tokens)
        except Exception as exc:
            probe.record(exc)
            raise

        def _probed_llm(prompt: str) -> str:
            start = probe.clock()
            try:
                return llm(prompt)
            except Exception as exc:
                probe.record(exc)
                raise
            finally:
                probe.generation_s = probe.clock() - start

        return _probed_llm

    return _probed_factory
