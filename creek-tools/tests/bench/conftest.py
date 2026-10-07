"""Shared fakes for the capacity-harness tests.

:class:`FakeModel` is a deterministic, positively-local model with knobs for
the failure modes the harness must classify, and a *gather* gate that holds
each call until a given number are in flight at once — which is how the
concurrency test proves calls really overlapped rather than ran serially.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.bench import HARNESS_VERSION
from creek_mcp.bench.local_only import LocalOnlyFactory
from creek_mcp.bench.metadata import RunMetadata
from creek_mcp.bench.runner import Backend, BenchPlan, Workload

if TYPE_CHECKING:
    from creek.models import PrivacyTier
    from creek_mcp.bench.protocols import LLMCallable

GATHER_TIMEOUT_S = 1.0
"""How long a gated call waits for its peers before giving up."""

CANARY = "zq-canary-7f3e"
"""A string that must never appear in a report, a log line or CLI output."""

_ENTRY_MARKER = "\nENTRY:\n"


def quote_from(prompt: str) -> str:
    """Return the first three words of the prompt's ENTRY section."""
    entry = prompt.rsplit(_ENTRY_MARKER, 1)[-1]
    return " ".join(entry.split()[:3])


@dataclass
class FakeModel:
    """A configurable fake model behind a reflect-shaped factory.

    Attributes:
        is_cloud: What built callables declare; ``None`` omits the attribute.
        call_exc: Raised by the completion call when set.
        factory_exc: Raised by the factory itself when set.
        factory_fails_after: Builds that succeed before *factory_exc* applies
            (1 lets the runner's preflight build through).
        raise_first_only: Raise *call_exc* on the first call only.
        echo_canary: Put :data:`CANARY` and the whole prompt in the response.
        gather: Hold each call until this many are in flight (0 disables).
    """

    is_cloud: bool | None = False
    call_exc: BaseException | None = None
    factory_exc: BaseException | None = None
    factory_fails_after: int = 0
    raise_first_only: bool = False
    echo_canary: bool = False
    gather: int = 0
    calls: int = 0
    builds: int = 0
    max_in_flight: int = 0
    max_tokens_seen: list[int] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    _in_flight: int = 0
    _lock: threading.Condition = field(default_factory=threading.Condition)

    def factory(self, tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        """Build a completion callable (the reflect factory shape)."""
        del tier
        self.builds += 1
        self.max_tokens_seen.append(max_tokens)
        if self.factory_exc is not None and self.builds > self.factory_fails_after:
            raise self.factory_exc
        if self.is_cloud is None:
            return _Undeclared(self)
        return _Declared(self, is_cloud=self.is_cloud)

    def _enter(self) -> None:
        """Count this call in flight and wait for the gather threshold."""
        with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self._lock.notify_all()
            if self.gather:
                self._lock.wait_for(
                    lambda: self._in_flight >= self.gather, GATHER_TIMEOUT_S
                )

    def _leave(self) -> None:
        """Count this call out."""
        with self._lock:
            self._in_flight -= 1

    def complete(self, prompt: str) -> str:
        """Answer *prompt* with one verbatim-anchored note, or fail."""
        self.events.append("trial")
        self._enter()
        try:
            self.prompts.append(prompt)
            first = self.calls == 1
            if self.call_exc is not None and (first or not self.raise_first_only):
                raise self.call_exc
            note = {"quote": quote_from(prompt), "kind": "pattern", "note": "ok"}
            payload: dict[str, object] = {"notes": [note]}
            if self.echo_canary:
                payload["essay"] = f"{CANARY} {prompt}"
            return json.dumps(payload)
        finally:
            self._leave()


class _Undeclared:
    """A completion callable that says nothing about where it runs."""

    def __init__(self, model: FakeModel) -> None:
        """Bind *model*."""
        self._model = model

    def __call__(self, prompt: str) -> str:
        """Delegate to the fake model."""
        return self._model.complete(prompt)


class _Declared(_Undeclared):
    """A completion callable that declares ``is_cloud``."""

    def __init__(self, model: FakeModel, *, is_cloud: bool) -> None:
        """Bind *model* and the declared flag."""
        super().__init__(model)
        self.is_cloud = is_cloud


def run_metadata(**overrides: object) -> RunMetadata:
    """Build fake-mode run metadata, overridden by *overrides*."""
    fields: dict[str, object] = {
        "mode": "fake",
        "provider": "fake",
        "grounding": "none",
        "model_tag": "fake",
        "num_ctx": 4096,
        "num_predict": 128,
        "harness_version": HARNESS_VERSION,
    }
    fields.update(overrides)
    return RunMetadata.model_validate(fields)


@dataclass
class Recorded:
    """Calls the backend hooks received, in order."""

    log: list[str] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)


BASE_WORKLOAD = Workload(
    entries=3,
    words_per_entry=8,
    query_words=8,
    cold_trials=1,
    warm_trials=2,
    concurrency_levels=(1,),
)
"""A small workload: one cold, two warm, one serial concurrency trial."""


def make_plan(
    model: FakeModel,
    recorded: Recorded | None = None,
    *,
    metadata: RunMetadata | None = None,
    **workload: Any,
) -> BenchPlan:
    """Build a plan over *model* whose hooks log into *recorded*."""
    record = recorded if recorded is not None else Recorded()
    model.events = record.log

    def _evict() -> None:
        record.log.append("evict")

    def _resident() -> int | None:
        return None

    def _sleep(seconds: float) -> None:
        record.slept.append(seconds)
        record.log.append("sleep")

    return BenchPlan(
        metadata=metadata if metadata is not None else run_metadata(),
        workload=replace(BASE_WORKLOAD, **workload),
        backend=Backend(
            factory=LocalOnlyFactory(model.factory, provider_name="fake"),
            evict=_evict,
            resident_bytes=_resident,
        ),
        sleeper=_sleep,
    )


@pytest.fixture
def fake_model() -> FakeModel:
    """A well-behaved fake model."""
    return FakeModel()
