"""A failed *weighted* classification must not be stamped ``llm`` (#1330).

The weighted-classification path
(:func:`creek.classify.classify_engine._classify_one_weighted` ->
:func:`creek.classify.weighted.classify_weighted`) fails soft to a bare
``WeightedFragmentClassification()`` in three places — whitespace-only body,
provider unavailable, and the ``(RuntimeError, OSError, ValueError)`` catch —
and the engine has no ``succeeded`` check to notice. The consequences, all
observed end-to-end on a tmp vault at HEAD:

* ``classification_method: llm`` and ``classification_provider: ollama`` are
  stamped for a call that produced nothing;
* the rule classifier's surviving verdict (``frequency.primary: F3``) is
  overwritten with ``unclassified``;
* a fabricated all-zero ``weighted`` profile is persisted;
* the fragment id is appended to the OPS-001 resume ledger; and
* the next non-``--force`` run therefore reports ``preserved_llm=1`` and skips
  the fragment permanently, because a prior ``llm`` stamp means "already paid
  for".

This is the weighted twin of :mod:`tests.test_classify_failed_llm_provenance`
(#744), which pinned the same contract on the single-pick LLM path at
``classify_engine.py:1519-1529``. Every assertion here is on the **persisted
frontmatter** (or on the run summary / the vault's file tree), never on mock
call counts.

Test seam: ``classify_weighted`` builds its *own* :class:`LLMClassifier` via a
function-local import, which is a different object from the engine's, and
``_assert_classifiers_available`` aborts the run up front if the engine's
classifier is unavailable. So both ends are patched: the engine's class is
swapped for :class:`_EngineStub` (available, and loud if the single-pick path
is ever reached), while the *failure* is injected onto the real class that
``classify_weighted`` constructs for itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.constants import CLASSIFICATION_PROVIDER_KEY
from creek.classify.llm.orchestrator import LLMClassificationResult
from creek.config import (
    ClassificationConfig,
    CreekConfig,
    LLMConfig,
    LLMRoutingConfig,
)
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

# ---- Patch targets ----

_ENGINE_CLASSIFIER: Final[str] = "creek.classify.classify_engine.LLMClassifier"
"""The engine's own classifier reference — swapped for a stub wholesale."""

_AVAILABILITY: Final[str] = "creek.classify.llm.LLMClassifier._check_availability"
"""Availability hook on the real class ``classify_weighted`` builds itself."""

_INVOKE: Final[str] = "creek.classify.llm.LLMClassifier.invoke_prompt"
"""Transport hook on the real class ``classify_weighted`` builds itself."""

# ---- Fixture data ----

FRAGMENT_ID: Final[str] = "frag-0000weighted"
"""Id of the single seeded fragment; also what the resume ledger would log."""

F3_BODY: Final[str] = "I feel rage and anger and power and dominance and conflict."
"""A body the rule classifier scores as ``F3`` at confidence 0.56."""

F3_TWO_PARAGRAPH_BODY: Final[str] = (
    "I feel rage and anger and power and dominance and conflict.\n\n"
    "Anger and rage keep returning as dominance and conflict."
)
"""Two single-sentence paragraphs, so the FEAT-021 splitter has real work."""

WHITESPACE_BODY: Final[str] = "   \n   "
"""A body that trips ``classify_weighted``'s whitespace short-circuit."""

PROGRESS_LOG_PARTS: Final[tuple[str, str, str]] = (
    "00-Creek-Meta",
    "Processing-Log",
    "llm-progress.jsonl",
)
"""Vault-relative path of the OPS-001 resume ledger."""

VALID_WEIGHTED_RESPONSE: Final[str] = """The fragment lands at F3 / Red.

```yaml
frequencies:
  - value: F3
    weight: 0.8
phases:
  - value: rising
    weight: 0.7
overall_confidence: 0.7
```
"""
"""A well-formed weighted response, shaped like ``_weighted_yaml_payload()``."""


# ---- Provider stubs and failure injection ----


class _EngineStub:
    """Stands in for the *engine's* ``LLMClassifier`` (the #1330 test seam).

    Only two things are asked of it: pass
    :func:`~creek.classify.classify_engine._assert_classifiers_available`'s
    up-front check, and expose the ``config`` that
    ``_classify_one_weighted`` forwards to ``classify_weighted``. The
    single-pick entry point raises, so a silent fall-through off the weighted
    path fails loudly instead of quietly passing these tests.
    """

    def __init__(self, config: LLMConfig) -> None:
        """Retain the routed config the weighted path will re-use.

        Args:
            config: The per-stage config the ModelRouter resolved.
        """
        self.config = config

    @property
    def available(self) -> bool:
        """Report the engine-side provider as reachable.

        Returns:
            Always ``True`` — the failure under test is injected further
            down, inside ``classify_weighted``'s own classifier.
        """
        return True

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Fail loudly: the weighted run must never reach the single-pick call.

        Args:
            fragment: Unused; the call is a contract violation.
            content: Unused; the call is a contract violation.

        Returns:
            Never returns.

        Raises:
            AssertionError: Always.
        """
        del fragment, content
        msg = "weighted run must not fall through to classify_with_reasoning"
        raise AssertionError(msg)


class _ReatomizingEngineStub(_EngineStub):
    """An :class:`_EngineStub` whose single-pick call *succeeds*.

    FEAT-023 re-atomization re-enters ``_classify_one`` **without** the
    weighted flag (``classify_engine.py:1607-1614``), so the child classifier
    legitimately uses the single-pick entry point. Letting it succeed keeps
    the re-atomization test's red/green signal about child *files*, not about
    a stub raising.
    """

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Return *fragment* unchanged as a successful classification.

        Args:
            fragment: The fragment to hand straight back.
            content: Unused body text.

        Returns:
            A succeeded :class:`LLMClassificationResult` wrapping *fragment*.
        """
        del content
        return LLMClassificationResult(fragment=fragment, reasoning="")


def _raise_transport_error(self: object, prompt: str) -> str:
    """Simulate a provider transport failure.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        Never returns.

    Raises:
        OSError: Always.
    """
    del self, prompt
    msg = "network down"
    raise OSError(msg)


def _return_malformed_yaml(self: object, prompt: str) -> str:
    """Return a payload the weighted parser rejects with ``ValueError``.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        The malformed payload pinned by ``tests/test_weighted.py:908``.
    """
    del self, prompt
    return "```yaml\nnot: a: valid: yaml: ::\n```"


def _return_valid_weighted_yaml(self: object, prompt: str) -> str:
    """Return a well-formed weighted classification payload.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        :data:`VALID_WEIGHTED_RESPONSE`.
    """
    del self, prompt
    return VALID_WEIGHTED_RESPONSE


def _forbid_invocation(self: object, prompt: str) -> str:
    """Fail loudly if the provider is contacted at all.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        Never returns.

    Raises:
        AssertionError: Always.
    """
    del self, prompt
    msg = "the provider must not be contacted for a whitespace-only body"
    raise AssertionError(msg)


_RESPONDERS: Final[dict[str, Callable[[object, str], str]]] = {
    "transport_error": _raise_transport_error,
    "malformed_yaml": _return_malformed_yaml,
}
"""Per-mode ``invoke_prompt`` replacements; keyed by the parametrize id."""


@contextmanager
def _failing_weighted_provider(mode: str) -> Iterator[None]:
    """Make ``classify_weighted``'s own classifier fail in the named way.

    Args:
        mode: ``"provider_unavailable"`` (the availability check says no),
            ``"transport_error"`` (``OSError`` out of the provider call), or
            ``"malformed_yaml"`` (a payload the parser rejects).

    Yields:
        ``None`` while the failure is injected.
    """
    if mode == "provider_unavailable":
        with patch(_AVAILABILITY, return_value=False):
            yield
        return
    responder = _RESPONDERS[mode]
    with patch(_AVAILABILITY, return_value=True), patch(_INVOKE, new=responder):
        yield


# ---- Vault + config helpers ----


def _weighted_config(*, reatomize: bool = False) -> CreekConfig:
    """Build a local-provider config that forces the weighted LLM path.

    ``confidence_threshold=1.0`` is above anything the rule classifier can
    score, so every fragment reaches the LLM branch of ``_classify_one``;
    ``ollama`` keeps both the non-Intimate and Intimate routes local so the
    run never needs credentials or a routing fallback.

    Args:
        reatomize: Whether to enable the FEAT-023 re-atomization opt-in.

    Returns:
        The assembled :class:`CreekConfig`.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="ollama", model="qwen3:8b"),
        ),
        classification=ClassificationConfig(
            weighted_classification=True,
            reatomize=reatomize,
            confidence_threshold=1.0,
        ),
    )


def _seed(vault: Path, body: str) -> Path:
    """Write one unclassified fragment carrying *body* into *vault*.

    Args:
        vault: Vault root (created on demand).
        body: Markdown body for the fragment.

    Returns:
        Path to the freshly-written fragment file.
    """
    return write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=FRAGMENT_ID,
            title="Weighted provenance",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=body,
    )


def _fragment_files(vault: Path) -> list[Path]:
    """Return every markdown file under the vault's fragment tree.

    Args:
        vault: Vault root.

    Returns:
        Sorted list of ``01-Fragments/**/*.md`` paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


# ---- T1: provenance across all three provider-side failure modes ----


@pytest.mark.parametrize(
    "failure_mode",
    ["provider_unavailable", "transport_error", "malformed_yaml"],
)
def test_failed_weighted_call_keeps_honest_rules_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """A failed weighted call stamps ``rules`` and preserves the rules verdict.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
        failure_mode: Which of the three soft-failure paths to trigger.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = _seed(vault, F3_BODY)

    with _failing_weighted_provider(failure_mode):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    # The provenance tells the truth about what actually classified this.
    assert meta.get("classification_method") == "rules"
    assert CLASSIFICATION_PROVIDER_KEY not in meta
    # No fabricated all-zero profile is persisted.
    assert meta.get("weighted") is None
    # The rules verdict that actually stands survives the failed call.
    assert meta["frequency"]["primary"] == "F3"
    assert meta["frequency"]["primary"] != "unclassified"
    # We skip the LLM *call*, never the write: the fragment is still persisted.
    assert summary.classified == 1


# ---- T2: the success control ----


def test_successful_weighted_call_still_stamps_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real weighted classification still stamps ``llm`` + its provider.

    The narrowness control for T1: only the *failed* call loses the ``llm``
    provenance.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = _seed(vault, F3_BODY)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_return_valid_weighted_yaml),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    assert meta.get("classification_method") == "llm"
    assert meta.get(CLASSIFICATION_PROVIDER_KEY) == "ollama"
    assert meta["weighted"]["frequencies"] == [{"value": "F3", "weight": 0.8}]
    assert meta["weighted"]["overall_confidence"] == 0.7
    # And the legacy single-pick fields are derived from that profile.
    assert meta["frequency"]["primary"] == "F3"
    assert summary.classified == 1


# ---- T3: the OPS-001 resume ledger ----


def test_failed_weighted_call_leaves_the_resume_ledger_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No progress-ledger entry is written for a call that produced nothing.

    An entry here is a claim that a paid LLM call succeeded for this
    fragment; at HEAD the failed weighted run appends
    ``{"id": "frag-0000weighted"}``.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    _seed(vault, F3_BODY)

    with _failing_weighted_provider("transport_error"):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    ledger = vault.joinpath(*PROGRESS_LOG_PARTS)
    recorded = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    assert FRAGMENT_ID not in recorded


# ---- T4: the fragment stays eligible ----


def test_failed_weighted_fragment_is_reclassifiable_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed weighted run must not make the fragment "already paid for".

    The acceptance criterion the whole severity rests on: because the failed
    run stamps ``llm`` today, ``_record_if_preserved`` skips the fragment on
    every subsequent non-``--force`` run — permanently. At HEAD the second run
    reports ``preserved_llm=1, classified=0``.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    _seed(vault, F3_BODY)
    config = _weighted_config()

    with _failing_weighted_provider("provider_unavailable"):
        first = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=True,
        )
        second = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    assert first.classified == 1
    assert second.preserved_llm == 0
    assert second.classified == 1


# ---- T5: no re-atomization off a failed run ----


def test_failed_weighted_call_spawns_no_reatomized_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed weighted call must not trigger FEAT-023 decomposition.

    ``classify_engine.py:1082`` gates re-atomization on ``not was_skipped``.
    While the failed weighted path reports "the LLM was invoked", a vault gets
    child fragments carved out of a classification that never happened.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub whose
            single-pick call succeeds (the re-atomization child classifier
            legitimately uses it).
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _ReatomizingEngineStub)
    vault = tmp_path / "vault"
    _seed(vault, F3_TWO_PARAGRAPH_BODY)

    with _failing_weighted_provider("transport_error"):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(reatomize=True),
            method="llm",
            force=True,
        )

    # No child was written — and none *failed* to write either, so the count
    # below cannot be green for the wrong reason.
    assert summary.errors == ()
    assert len(_fragment_files(vault)) == 1


# ---- T6: the whitespace short-circuit ----


def test_whitespace_body_is_not_stamped_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only body never reaches a provider, so it is not ``llm``.

    ``classify_weighted`` short-circuits before it builds a classifier, so
    claiming ``classification_method: llm`` (with a provider, and a fabricated
    empty profile) is the same lie the three provider-side failures tell. The
    ``invoke_prompt`` patch asserts the short-circuit really is a
    short-circuit.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = _seed(vault, WHITESPACE_BODY)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_forbid_invocation),
    ):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    assert meta.get("classification_method") == "rules"
    assert CLASSIFICATION_PROVIDER_KEY not in meta
    assert meta.get("weighted") is None
