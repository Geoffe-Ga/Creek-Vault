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
from creek_mcp.bench.usage import TokenBasis, TokenUsage

if TYPE_CHECKING:
    from creek_mcp.bench.protocols import LLMCallable, LLMFactory

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


class _ReportingLLM:
    """A completion callable that reports its own token usage."""

    def __init__(self, usage: TokenUsage | None) -> None:
        """Bind the usage this callable will report."""
        self._usage = usage

    def __call__(self, prompt: str) -> str:
        """Echo *prompt* back."""
        return prompt

    def token_usage(self) -> TokenUsage | None:
        """Return the bound usage."""
        return self._usage


def _usage_factory(usage: TokenUsage | None) -> LLMFactory:
    """Return a factory building a :class:`_ReportingLLM` reporting *usage*."""

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens
        return _ReportingLLM(usage)

    return _factory


def test_probe_captures_the_providers_usage_and_the_requested_budget() -> None:
    """The probe is where the harness can see what reflect_tool hides."""
    probe = TrialProbe(clock=_Clock())
    usage = TokenUsage(
        prompt_tokens=1234, output_tokens=56, basis=TokenBasis.PROVIDER_REPORTED
    )
    llm = probe_factory(_usage_factory(usage), probe)(PrivacyTier.OPEN, max_tokens=128)
    assert llm("prompt") == "prompt"
    assert probe.usage == usage
    assert probe.max_output_tokens == 128


def test_probe_leaves_usage_unset_when_the_backend_reports_none() -> None:
    """Nothing is estimated for a provider that reports no counts."""
    probe = TrialProbe(clock=_Clock())
    llm = probe_factory(_usage_factory(None), probe)(PrivacyTier.OPEN, max_tokens=64)
    llm("prompt")
    assert probe.usage is None
    assert probe.max_output_tokens == 64


def test_probe_leaves_usage_unset_for_a_callable_that_cannot_report() -> None:
    """A plain callable is not interrogated for an attribute it lacks."""
    probe = TrialProbe(clock=_Clock())

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens

        def _llm(prompt: str) -> str:
            return prompt

        return _llm

    probe_factory(_factory, probe)(PrivacyTier.OPEN, max_tokens=8)("x")
    assert probe.usage is None


def test_probe_records_the_budget_even_when_the_call_fails() -> None:
    """A failed trial still says which ceiling reflect asked for."""
    probe = TrialProbe(clock=_Clock())
    usage = TokenUsage(
        prompt_tokens=9, output_tokens=9, basis=TokenBasis.PROVIDER_REPORTED
    )

    class _FailingReporter(_ReportingLLM):
        """Reports usage, but never completes a turn to report it for."""

        def __call__(self, prompt: str) -> str:
            """Fail the way an out-of-memory runtime does."""
            del prompt
            raise MemoryError

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens
        return _FailingReporter(usage)

    llm = probe_factory(_factory, probe)(PrivacyTier.OPEN, max_tokens=32)
    with pytest.raises(MemoryError):
        llm("prompt")
    assert probe.max_output_tokens == 32
    assert probe.usage is None


class _StreamingLLM:
    """A completion callable that reports when its first token arrived."""

    def __init__(self, ttft_s: float | None) -> None:
        """Bind the first-token time this callable will report."""
        self._ttft_s = ttft_s

    def __call__(self, prompt: str) -> str:
        """Echo *prompt* back."""
        return prompt

    def time_to_first_token(self) -> float | None:
        """Return the bound first-token time."""
        return self._ttft_s


def _streaming_factory(ttft_s: float | None) -> LLMFactory:
    """Return a factory building a :class:`_StreamingLLM` reporting *ttft_s*."""

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens
        return _StreamingLLM(ttft_s)

    return _factory


def test_probe_captures_a_streaming_backends_first_token_time() -> None:
    """R2's TTFT is the backend's own figure, read at the seam."""
    probe = TrialProbe(clock=_Clock())
    llm = probe_factory(_streaming_factory(0.75), probe)(PrivacyTier.OPEN, max_tokens=8)
    llm("prompt")
    assert probe.ttft_s == 0.75


def test_probe_leaves_ttft_unset_for_a_non_streaming_backend() -> None:
    """A callable with no first-token accessor is never interrogated."""
    probe = TrialProbe(clock=_Clock())

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        del tier, max_tokens

        def _llm(prompt: str) -> str:
            return prompt

        return _llm

    probe_factory(_factory, probe)(PrivacyTier.OPEN, max_tokens=8)("x")
    assert probe.ttft_s is None


def test_probe_leaves_ttft_unset_when_the_backend_streamed_nothing() -> None:
    """A streaming backend that reports no first token states nothing."""
    probe = TrialProbe(clock=_Clock())
    llm = probe_factory(_streaming_factory(None), probe)(PrivacyTier.OPEN, max_tokens=8)
    llm("prompt")
    assert probe.ttft_s is None


def test_probe_keeps_the_first_turns_first_token_time() -> None:
    """First-report-wins, like the usage report: one trial is one turn."""
    probe = TrialProbe(clock=_Clock())
    llm = probe_factory(_streaming_factory(0.25), probe)(PrivacyTier.OPEN, max_tokens=8)
    llm("one")
    probe.record_ttft(_StreamingLLM(9.0))
    assert probe.ttft_s == 0.25
