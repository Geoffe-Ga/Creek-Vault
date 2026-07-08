"""`creek classify --method llm` sizes its worker pool per classifier (#764, #783).

The engine's classify loop was serial — every fragment's (network-bound) LLM
call ran one after another, so raising ``max_concurrent`` did nothing and a
full-vault backfill took hours instead of minutes (#764). Making the loop
concurrent surfaced a second bug (#783): INTIMATE fragments are redirected to
a local provider by
:class:`~creek.classify.classify_engine.TierClassifiers` (#666), but the
executor pool was sized by only the non-Intimate ``classification`` stage's
``max_concurrent`` — so a mixed-tier vault applied the cloud stage's width to
local-model calls too, oversubscribing the local provider.

These tests pin both fixes:

- with ``max_concurrent = N`` the engine runs N fragments' classify calls
  **concurrently** (proven deterministically with a ``threading.Barrier`` of N
  parties — a serial loop can never trip it, only overlapping calls can);
- with ``max_concurrent = 1`` it stays strictly serial (backward-compatible);
- concurrent runs are still correct: every fragment is classified and counted
  exactly once (no lost ``counts`` updates);
- INTIMATE-tier (local-provider) calls are capped by the local stage's own
  ``max_concurrent``, never by the cloud ``classification`` stage's width
  (#783), and the two tiers' caps are independent — a run can genuinely
  saturate the cloud knob while staying under a smaller local one;
- a single shared classifier (the pre-#666/#783 backward-compatible shape,
  where Intimate and non-Intimate resolve to the same provider) still sizes
  the pool by that one config's ``max_concurrent`` — unchanged from #764;
- when no local backend exists to route Intimate fragments to, the run still
  succeeds as long as the vault holds none (the routing error stays deferred).

The LLM is faked — no network, no real provider.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, ClassVar

import pytest

import creek.classify.classify_engine as engine
from creek.classify.classify_engine import run_classify
from creek.classify.llm import LLMClassificationResult
from creek.config import (
    ClassificationConfig,
    CreekConfig,
    LLMConfig,
    LLMRoutingConfig,
)
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
    """Fake ``LLMClassifier`` that reports concurrency through per-provider probes.

    :class:`~creek.classify.classify_engine.TierClassifiers` (#666) constructs
    one instance per resolved provider config (non-Intimate vs Intimate), so a
    single class-level probe cannot tell cloud calls apart from local ones.
    Instead, each instance looks up its probe by ``self.config.provider`` —
    letting a mixed-tier test assert on cloud and local concurrency
    independently (#783).
    """

    probes: ClassVar[dict[str, _Probe]] = {}

    def __init__(self, config: LLMConfig) -> None:
        """Capture the resolved config; its ``provider`` selects this call's probe."""
        self.config = config

    @property
    def available(self) -> bool:
        """Always reachable — availability is not under test here."""
        return True

    def classify_with_reasoning(
        self, fragment: Fragment, content: str
    ) -> LLMClassificationResult:
        """Register the call against this instance's provider-keyed probe."""
        _ = content
        probe = _ProbingClassifier.probes.get(self.config.provider)
        assert probe is not None, (
            f"no probe registered for provider {self.config.provider!r}"
        )
        probe.enter()
        return LLMClassificationResult(fragment=fragment, reasoning="")


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the engine's ``LLMClassifier`` for the concurrency-probing fake."""
    _ProbingClassifier.probes = {}
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _ProbingClassifier
    )


def _config(max_concurrent: int, *, reatomize: bool = False) -> CreekConfig:
    """A config whose classification stage carries *max_concurrent*."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**_LOCAL),
            classification=LLMConfig(**_CLOUD, max_concurrent=max_concurrent),
        ),
        classification=ClassificationConfig(reatomize=reatomize),
    )


def _config_mixed(
    *, cloud_max_concurrent: int, local_max_concurrent: int
) -> CreekConfig:
    """Cloud ``classification`` and local ``default`` stages, independently sized.

    The two stages resolve to distinct providers (anthropic vs ollama), so
    :class:`~creek.classify.classify_engine.TierClassifiers` builds two
    distinct classifier instances — the shape #783 must size two independent
    concurrency caps for.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**_LOCAL, max_concurrent=local_max_concurrent),
            classification=LLMConfig(**_CLOUD, max_concurrent=cloud_max_concurrent),
        ),
        classification=ClassificationConfig(),
    )


def _config_single_provider(max_concurrent: int) -> CreekConfig:
    """Both stages resolve to the same local config (pre-#666 backward-compat shape).

    ``classification`` is left unset so it falls back to ``default``; the
    Intimate route also resolves to that same local config (already local, so
    the ``ModelRouter`` never redirects it) — ``TierClassifiers.distinct()``
    returns exactly one classifier, the original #764 single-provider case.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**_LOCAL, max_concurrent=max_concurrent),
        ),
        classification=ClassificationConfig(),
    )


def _config_cloud_only(*, classification_max_concurrent: int) -> CreekConfig:
    """Cloud ``classification`` AND cloud ``default`` — Intimate route deferred.

    With no local backend to redirect Intimate content to, ``TierClassifiers``
    captures the :class:`~creek.classify.llm.router.IntimateRoutingError`
    instead of raising immediately (#666) — a run with no Intimate fragments
    must still succeed, sized by the sole (non-Intimate) classifier.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**_CLOUD),
            classification=LLMConfig(
                **_CLOUD, max_concurrent=classification_max_concurrent
            ),
        ),
        classification=ClassificationConfig(),
    )


def _write_fragments(
    vault: Path,
    count: int,
    *,
    tier: PrivacyTier = PrivacyTier.OPEN,
    prefix: str = "frag-concurrency",
) -> None:
    """Write *count* classifiable, rules-unclassified fragments at *tier*.

    ``prefix`` keeps fragment IDs distinct when a single vault mixes multiple
    tiers within one test.
    """
    for i in range(count):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"{prefix}{i:02d}",
                title=f"{prefix}{i:02d}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                privacy_tier=tier,
            ),
            body=_BODY,
        )


def test_llm_classify_runs_calls_concurrently(tmp_path: Path) -> None:
    """With ``max_concurrent = 4``, four classify calls overlap in flight."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 4)
    _ProbingClassifier.probes["anthropic"] = _Probe(parties=4)

    summary = run_classify(
        vault_path=vault, config=_config(4), method="llm", force=True
    )

    probe = _ProbingClassifier.probes["anthropic"]
    assert not probe.serialized, "classify ran serially — barrier never tripped"
    assert probe.max_active == 4  # all four in flight at once
    assert summary.total == 4
    assert summary.classified == 4  # every fragment counted once, no lost updates


def test_max_concurrent_one_is_serial(tmp_path: Path) -> None:
    """With ``max_concurrent = 1`` the loop stays strictly serial (unchanged)."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 3)
    _ProbingClassifier.probes["anthropic"] = _Probe()  # sleep-based overlap detection

    summary = run_classify(
        vault_path=vault, config=_config(1), method="llm", force=True
    )

    assert _ProbingClassifier.probes["anthropic"].max_active == 1  # never >1 in flight
    assert summary.classified == 3


def test_reatomize_forces_serial_even_with_high_max_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-atomization keeps the loop serial (shared VaultWriter) despite mc>1."""
    # Stub the re-atomization tail so only the pool-width invariant is exercised.
    monkeypatch.setattr(engine, "_maybe_reatomize_and_persist", lambda **_: None)
    vault = tmp_path / "vault"
    _write_fragments(vault, 3)
    _ProbingClassifier.probes["anthropic"] = _Probe()  # sleep probe

    summary = run_classify(
        vault_path=vault,
        config=_config(4, reatomize=True),
        method="llm",
        force=True,
    )

    assert _ProbingClassifier.probes["anthropic"].max_active == 1  # serialized
    assert summary.classified == 3


def test_concurrent_run_classifies_every_fragment(tmp_path: Path) -> None:
    """A wide run over more fragments than workers still classifies them all."""
    vault = tmp_path / "vault"
    _write_fragments(vault, 12)
    _ProbingClassifier.probes["anthropic"] = _Probe()  # sleep probe; correctness only

    summary = run_classify(
        vault_path=vault, config=_config(4), method="llm", force=True
    )

    assert summary.total == 12
    assert summary.classified == 12
    probe = _ProbingClassifier.probes["anthropic"]
    assert probe.max_active >= 2  # genuinely overlapped
    # Every file carries the llm provenance stamp.
    stamped = [
        p
        for p in (vault / "01-Fragments").rglob("*.md")
        if "classification_method: llm" in p.read_text(encoding="utf-8")
    ]
    assert len(stamped) == 12


def test_intimate_calls_capped_by_local_max_concurrent_not_cloud(
    tmp_path: Path,
) -> None:
    """INTIMATE fragments must not inherit the cloud stage's width (#783).

    Local ``default`` max_concurrent=2; cloud ``classification``
    max_concurrent=8. Every fragment here is INTIMATE, so every classify call
    routes to the LOCAL classifier (#666). Sizing the pool by only the cloud
    stage's width — today's bug — lets local calls run far wider than 2; peak
    in-flight LOCAL calls must never exceed the local knob.
    """
    vault = tmp_path / "vault"
    _write_fragments(vault, 6, tier=PrivacyTier.INTIMATE, prefix="frag-intimate")
    _ProbingClassifier.probes["ollama"] = _Probe()  # sleep-based max-active counter

    summary = run_classify(
        vault_path=vault,
        config=_config_mixed(cloud_max_concurrent=8, local_max_concurrent=2),
        method="llm",
        force=True,
    )

    local_probe = _ProbingClassifier.probes["ollama"]
    assert local_probe.max_active <= 2, (
        f"local (ollama) calls peaked at {local_probe.max_active} in flight — "
        "the cloud classification stage's max_concurrent=8 leaked into the "
        "local provider's concurrency cap"
    )
    assert summary.total == 6
    assert summary.classified == 6


def test_cloud_and_local_tiers_saturate_independently(tmp_path: Path) -> None:
    """The cloud and local caps are independent, not collapsed to ``min()`` (#783).

    Mixing 3 OPEN (cloud) and 4 INTIMATE (local) fragments: the cloud
    classifier must genuinely reach its own max_concurrent=3 (a Barrier
    proves overlap, not just "didn't crash"), while the local classifier —
    sharing the same run — never exceeds its own, smaller, max_concurrent=2.
    A naive fix that shares one semaphore sized at ``min(3, 2) == 2`` for both
    would cap the cloud side at 2 too and the ``Barrier(3)`` below would time
    out instead.
    """
    vault = tmp_path / "vault"
    _write_fragments(vault, 3, tier=PrivacyTier.OPEN, prefix="frag-cloud")
    _write_fragments(vault, 4, tier=PrivacyTier.INTIMATE, prefix="frag-intimate")
    _ProbingClassifier.probes["anthropic"] = _Probe(parties=3)
    _ProbingClassifier.probes["ollama"] = _Probe()  # sleep-based max-active counter

    summary = run_classify(
        vault_path=vault,
        config=_config_mixed(cloud_max_concurrent=3, local_max_concurrent=2),
        method="llm",
        force=True,
    )

    cloud_probe = _ProbingClassifier.probes["anthropic"]
    local_probe = _ProbingClassifier.probes["ollama"]
    assert not cloud_probe.serialized, "cloud calls ran serially — barrier not tripped"
    assert cloud_probe.max_active == 3  # all three cloud calls genuinely overlapped
    assert local_probe.max_active <= 2  # local cap held despite the wider cloud knob
    assert summary.total == 7
    assert summary.classified == 7


def test_single_shared_classifier_pool_width_unchanged(tmp_path: Path) -> None:
    """A single shared classifier (pre-#666 shape) behaves exactly like #764.

    When ``classification`` falls back to ``default`` and the Intimate route
    resolves to that same local config, ``TierClassifiers.distinct()`` returns
    one classifier — the pool must still be sized by (and capped at) that
    single config's ``max_concurrent``, with no regression from the #783
    per-tier plumbing.
    """
    vault = tmp_path / "vault"
    _write_fragments(vault, 6, tier=PrivacyTier.OPEN, prefix="frag-single")
    _ProbingClassifier.probes["ollama"] = _Probe()  # sleep-based max-active counter

    summary = run_classify(
        vault_path=vault,
        config=_config_single_provider(max_concurrent=3),
        method="llm",
        force=True,
    )

    probe = _ProbingClassifier.probes["ollama"]
    assert probe.max_active <= 3  # never exceeds the single config's own knob
    assert probe.max_active >= 2  # genuinely overlapped, not accidentally serial
    assert summary.total == 6
    assert summary.classified == 6


def test_deferred_intimate_route_runs_with_no_intimate_fragments(
    tmp_path: Path,
) -> None:
    """No local backend to route Intimate to is fine when nothing is Intimate.

    Both ``classification`` and ``default`` are cloud, so
    ``TierClassifiers.intimate`` is ``None`` and the routing error is
    deferred (#666). The vault holds only OPEN fragments, so the deferred
    error never fires and the pool is sized by the sole (non-Intimate)
    classifier alone.
    """
    vault = tmp_path / "vault"
    _write_fragments(vault, 4, tier=PrivacyTier.OPEN, prefix="frag-deferred")
    _ProbingClassifier.probes["anthropic"] = _Probe(parties=4)

    summary = run_classify(
        vault_path=vault,
        config=_config_cloud_only(classification_max_concurrent=4),
        method="llm",
        force=True,
    )

    probe = _ProbingClassifier.probes["anthropic"]
    assert not probe.serialized, "classify ran serially — barrier never tripped"
    assert probe.max_active == 4
    assert summary.total == 4
    assert summary.classified == 4
    assert summary.errors == ()
