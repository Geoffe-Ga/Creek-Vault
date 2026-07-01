"""`creek classify --method llm` honours ``max_concurrent`` (#764).

The engine's classify loop was serial — every fragment's (network-bound) LLM
call ran one after another, so raising ``max_concurrent`` did nothing and a
full-vault backfill took hours instead of minutes. These tests pin the fix:

- with ``max_concurrent = N`` the engine runs N fragments' classify calls
  **concurrently** (proven deterministically with a ``threading.Barrier`` of N
  parties — a serial loop can never trip it, only overlapping calls can);
- with ``max_concurrent = 1`` it stays strictly serial (backward-compatible);
- concurrent runs are still correct: every fragment is classified and counted
  exactly once (no lost ``counts`` updates).

The LLM is faked — no network, no real provider.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.llm import LLMClassificationResult
from creek.config import CreekConfig, LLMConfig, LLMRoutingConfig
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

# Long enough that rules leave it UNCLASSIFIED, so the LLM path actually runs.
_BODY = "a reflection worth keeping with enough words to classify cleanly"
_CLOUD = {"provider": "anthropic", "model": "claude-haiku-4-5"}
_LOCAL = {"provider": "ollama", "model": "qwen3:8b"}


class _Probe:
    """Observes concurrency of the faked classify calls.

    With ``parties`` set, callers rendezvous on a barrier — if fewer than
    ``parties`` calls are ever in flight at once (a serial loop), the barrier
    times out and ``serialized`` is set. Without it, a short sleep makes overlap
    observable via ``max_active``.
    """

    def __init__(self, parties: int | None = None, timeout: float = 10.0) -> None:
        """Arm the probe with an optional N-party rendezvous barrier."""
        self._barrier = threading.Barrier(parties, timeout=timeout) if parties else None
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.serialized = False

    def enter(self) -> None:
        """Record one in-flight call and rendezvous / dwell to expose overlap."""
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self._barrier is not None:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                self.serialized = True
        else:
            time.sleep(0.15)
        with self._lock:
            self.active -= 1


class _ProbingClassifier:
    """Fake ``LLMClassifier`` that reports concurrency through :data:`probe`."""

    probe: _Probe | None = None

    def __init__(self, config: LLMConfig) -> None:
        """Capture the resolved config (its ``max_concurrent`` drives the pool)."""
        self.config = config

    @property
    def available(self) -> bool:
        """Always reachable — availability is not under test here."""
        return True

    def classify_with_reasoning(
        self, fragment: Fragment, content: str
    ) -> LLMClassificationResult:
        """Register the call with the probe and echo the fragment back."""
        _ = content
        assert _ProbingClassifier.probe is not None
        _ProbingClassifier.probe.enter()
        return LLMClassificationResult(fragment=fragment, reasoning="")


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the engine's ``LLMClassifier`` for the concurrency-probing fake."""
    _ProbingClassifier.probe = None
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _ProbingClassifier
    )


def _config(max_concurrent: int) -> CreekConfig:
    """A config whose classification stage carries *max_concurrent*."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**_LOCAL),
            classification=LLMConfig(**_CLOUD, max_concurrent=max_concurrent),
        )
    )


def _write_fragments(vault: Path, count: int) -> None:
    """Write *count* classifiable, rules-unclassified OPEN fragments."""
    for i in range(count):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-concurrency{i:02d}",
                title=f"frag-concurrency{i:02d}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                privacy_tier=PrivacyTier.OPEN,
            ),
            body=_BODY,
        )


def test_llm_classify_runs_calls_concurrently(tmp_path: Path) -> None:
    """With ``max_concurrent = 4``, four classify calls overlap in flight."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 4)
    _ProbingClassifier.probe = _Probe(parties=4)

    summary = run_classify(
        vault_path=vault, config=_config(4), method="llm", force=True
    )

    probe = _ProbingClassifier.probe
    assert not probe.serialized, "classify ran serially — barrier never tripped"
    assert probe.max_active == 4  # all four in flight at once
    assert summary.total == 4
    assert summary.classified == 4  # every fragment counted once, no lost updates


def test_max_concurrent_one_is_serial(tmp_path: Path) -> None:
    """With ``max_concurrent = 1`` the loop stays strictly serial (unchanged)."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 3)
    _ProbingClassifier.probe = _Probe()  # sleep-based overlap detection

    summary = run_classify(
        vault_path=vault, config=_config(1), method="llm", force=True
    )

    assert _ProbingClassifier.probe.max_active == 1  # never more than one in flight
    assert summary.classified == 3


def test_concurrent_run_classifies_every_fragment(tmp_path: Path) -> None:
    """A wide run over more fragments than workers still classifies them all."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 12)
    _ProbingClassifier.probe = _Probe()  # sleep probe; just checking correctness

    summary = run_classify(
        vault_path=vault, config=_config(4), method="llm", force=True
    )

    assert summary.total == 12
    assert summary.classified == 12
    assert _ProbingClassifier.probe.max_active >= 2  # genuinely overlapped
    # Every file carries the llm provenance stamp.
    stamped = [
        p
        for p in (vault / "01-Fragments").rglob("*.md")
        if "classification_method: llm" in p.read_text(encoding="utf-8")
    ]
    assert len(stamped) == 12
