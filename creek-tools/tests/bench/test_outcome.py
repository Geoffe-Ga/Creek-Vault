"""Trial outcome classification and the probe that survives reflect's swallow.

``classify_outcome`` maps a raised exception onto a closed vocabulary. Its
order is load-bearing: ``TimeoutError`` subclasses ``OSError``, so a timeout
must be recognised before the disk-full check or it would be misfiled. The
probe wraps both the factory call and the returned callable, because
``reflect_tool`` builds the LLM *inside* the ``try`` that turns a
``RuntimeError`` into a refusal dict.
"""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING

import httpx
import pytest

from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier
from creek_mcp.bench.outcome import (
    ContextOverflowError,
    Outcome,
    ProviderUnavailableError,
    TrialProbe,
    classify_outcome,
    probe_factory,
)

if TYPE_CHECKING:
    from creek_mcp.bench.protocols import LLMCallable

_REQUEST = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")


def _status_error(status: int, body: str) -> httpx.HTTPStatusError:
    """Build the error ``raise_for_status`` raises for *status* and *body*."""
    response = httpx.Response(status, text=body, request=_REQUEST)
    return httpx.HTTPStatusError("server error", request=_REQUEST, response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (None, Outcome.OK),
        (MemoryError(), Outcome.OOM),
        (OSError(errno.ENOSPC, "no space"), Outcome.DISK_FULL),
        (OSError(errno.EDQUOT, "quota"), Outcome.DISK_FULL),
        (OSError(errno.EACCES, "denied"), Outcome.ERROR),
        (TimeoutError(), Outcome.TIMEOUT),
        (TimeoutError(errno.ENOSPC, "timed out"), Outcome.TIMEOUT),
        (httpx.ReadTimeout("slow", request=_REQUEST), Outcome.TIMEOUT),
        (httpx.ConnectError("refused", request=_REQUEST), Outcome.PROVIDER_UNAVAILABLE),
        (ProviderUnavailableError("down"), Outcome.PROVIDER_UNAVAILABLE),
        (ContextOverflowError("full"), Outcome.CONTEXT_OVERFLOW),
        (
            _status_error(500, '{"error":"model requires more system memory"}'),
            Outcome.OOM,
        ),
        (_status_error(500, '{"error":"something else"}'), Outcome.ERROR),
        (_status_error(404, "out of memory"), Outcome.ERROR),
        (IntimateRoutingError("no local backend"), Outcome.ERROR),
        (ValueError("bad"), Outcome.ERROR),
    ],
)
def test_outcomes_classified(exc: BaseException | None, expected: Outcome) -> None:
    """Each failure lands in exactly one closed outcome."""
    assert classify_outcome(exc) is expected


def test_enum_is_closed() -> None:
    """The vocabulary is exactly the six outcomes the report may carry."""
    assert {member.value for member in Outcome} == {
        "ok",
        "timeout",
        "oom",
        "disk_full",
        "provider_unavailable",
        "context_overflow",
        "error",
    }


class _Clock:
    """A clock that advances by one second per reading."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current reading, then advance."""
        reading = self.now
        self.now += 1.0
        return reading


def test_probe_records_generation_time_and_first_outcome_only() -> None:
    """The probe times the callable and keeps the first failure it saw."""
    probe = TrialProbe(clock=_Clock())

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens

        def _llm(prompt: str) -> str:
            del prompt
            raise MemoryError

        return _llm

    wrapped = probe_factory(_factory, probe)
    llm = wrapped(PrivacyTier.OPEN, max_tokens=8)
    with pytest.raises(MemoryError):
        llm("prompt")
    probe.record(ValueError())
    assert probe.outcome is Outcome.OOM
    assert probe.generation_s == 1.0


def test_probe_success_leaves_outcome_unset() -> None:
    """A successful call records a duration and no outcome."""
    probe = TrialProbe(clock=_Clock())

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens

        def _llm(prompt: str) -> str:
            return prompt.upper()

        return _llm

    llm = probe_factory(_factory, probe)(PrivacyTier.OPEN, max_tokens=8)
    assert llm("ok") == "OK"
    assert probe.outcome is None
    assert probe.generation_s == 1.0


def test_probe_records_a_factory_failure() -> None:
    """An exception raised while *building* the LLM is still classified."""
    probe = TrialProbe(clock=_Clock())

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens
        raise ProviderUnavailableError("down")

    with pytest.raises(ProviderUnavailableError):
        probe_factory(_factory, probe)(PrivacyTier.OPEN, max_tokens=8)
    assert probe.outcome is Outcome.PROVIDER_UNAVAILABLE
    assert probe.generation_s is None
