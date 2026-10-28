"""Hardening of request-scoped BYOK below the HTTP layer (#1877, ADR-0015).

Two engine-level properties a writer's key relies on:

- **A rejected key is fatal for the run.** A ``401``/``403`` from the writer's
  provider is a typed :class:`~creek.classify.llm.providers.RequestKeyRejectedError`
  that the retry loop does not retry, and that stops the dispatch of every
  fragment not yet started, rather than three delayed vendor calls per
  fragment ending in a pass that calls itself done.
- **Re-atomization re-reads the tier.** A fragment the classification raised to
  INTIMATE is split and re-classified by the INTIMATE route, never by the cloud
  classifier that served it while it was still PERSONAL.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import PropertyMock, patch

import pytest

from creek.classify import classify_engine
from creek.classify.classify_engine import RequestScopedClassifier, run_classify
from creek.classify.llm import LLMClassificationResult, LLMClassifier
from creek.classify.llm.providers import Completion, RequestKeyRejectedError
from creek.config import CreekConfig, LLMConfig
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_REJECTED: Final[str] = "Anthropic API call failed: AuthenticationError (HTTP 401)"
"""The redacted message a rejected request key arrives as."""

_FILE_COUNT: Final[int] = 10
_WORKERS: Final[int] = 2
_UNBLOCK_SECONDS: Final[float] = 0.2


class _RejectingProvider:
    """A provider whose every call is the writer's key being rejected."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "rejecting-test-model"

    @property
    def available(self) -> bool:
        return True

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        del prompt, max_tokens, system
        self.calls += 1
        raise RequestKeyRejectedError(_REJECTED)


def _fragment(fragment_id: str, tier: PrivacyTier = PrivacyTier.PERSONAL) -> Fragment:
    return Fragment(
        id=fragment_id,
        title=fragment_id,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=tier,
    )


# --------------------------------------------------------------------------- #
# A rejected key: typed, never retried, fatal for the run
# --------------------------------------------------------------------------- #


def test_a_rejected_request_key_is_a_runtime_error() -> None:
    """Every existing ``except RuntimeError`` path keeps catching it."""
    assert issubclass(RequestKeyRejectedError, RuntimeError)


def test_the_retry_loop_never_retries_a_rejected_request_key() -> None:
    """One vendor call, no backoff sleep, and the typed error propagates."""
    provider = _RejectingProvider()
    classifier = LLMClassifier(LLMConfig(provider="anthropic"), provider=provider)

    with (
        patch("creek.classify.llm.orchestrator.time.sleep") as slept,
        pytest.raises(RequestKeyRejectedError),
    ):
        classifier.classify_with_reasoning(_fragment("frag-rejected-0001"))

    assert provider.calls == 1
    slept.assert_not_called()


def test_an_ordinary_provider_failure_is_still_retried() -> None:
    """The loop's historical behaviour for every other failure is unchanged."""

    class _Flaky(_RejectingProvider):
        def complete(
            self,
            prompt: str,
            *,
            max_tokens: int | None = None,
            system: str | None = None,
        ) -> Completion:
            del prompt, max_tokens, system
            self.calls += 1
            msg = "Anthropic API call failed: InternalServerError (HTTP 500)"
            raise RuntimeError(msg)

    provider = _Flaky()
    classifier = LLMClassifier(LLMConfig(provider="anthropic"), provider=provider)

    with patch("creek.classify.llm.orchestrator.time.sleep"):
        result = classifier.classify_with_reasoning(_fragment("frag-flaky-00001"))

    assert result.succeeded is False
    assert provider.calls == LLMClassifier.MAX_RETRIES


def test_a_failing_file_stops_the_dispatch_of_every_file_not_yet_started(
    tmp_path: Path,
) -> None:
    """The concurrent dispatcher cancels pending work once one file raises.

    ``executor.map`` alone would surface the error only after its executor's
    ``shutdown(wait=True)`` had run every queued file, so a rejected key would
    still cost one vendor call per fragment in the vault.
    """
    files = [tmp_path / f"{index:02d}.md" for index in range(_FILE_COUNT)]
    started: list[Path] = []
    release = threading.Event()

    def _process(md_file: Path) -> None:
        started.append(md_file)
        if md_file == files[0]:
            raise RequestKeyRejectedError(_REJECTED)
        release.wait(_UNBLOCK_SECONDS)

    with pytest.raises(RequestKeyRejectedError):
        classify_engine._dispatch_files(files, _process, workers=_WORKERS)

    assert len(started) < _FILE_COUNT


def test_a_request_scoped_run_ends_at_the_first_rejection(tmp_path: Path) -> None:
    """A rejected key stops the pass: one refused call per in-flight file at most.

    Before #1877 every fragment in the vault made three delayed calls and the
    pass reported itself complete. Here no fragment is retried and the files
    not yet started are never dispatched.
    """
    vault = tmp_path / "vault"
    for index in range(_FILE_COUNT):
        write_fragment_file(
            vault=vault,
            fragment=_fragment(f"frag-rejected-{index:04d}", PrivacyTier.OPEN),
            body="An ordinary note about the weather and the garden.",
        )
    provider = _RejectingProvider()
    config = CreekConfig()
    config.classification.confidence_threshold = 1.0
    config.llm.default.max_concurrent = 1
    stage = LLMConfig(provider="anthropic", max_concurrent=1)

    with (
        patch("creek.classify.llm.orchestrator.time.sleep") as slept,
        pytest.raises(RequestKeyRejectedError),
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
            request_scoped=RequestScopedClassifier(
                provider=provider, config=stage, intimate_route_serves=False
            ),
        )

    assert 1 <= provider.calls < _FILE_COUNT
    slept.assert_not_called()


# --------------------------------------------------------------------------- #
# Re-atomization follows the tier the classification arrived at
# --------------------------------------------------------------------------- #

_MULTI_PARAGRAPH_BODY: Final[str] = (
    "First paragraph carries some loose musing about ordinary subject matter.\n"
    "\n"
    "Second paragraph shifts register entirely and pulls in another thread.\n"
    "\n"
    "Third paragraph closes with yet another distinct slice of content."
)
"""Three paragraphs, so the FEAT-021 splitter yields several children."""


def _raised_to_intimate(fragment: Fragment, *_: Any, **__: Any) -> Fragment:
    """Stand-in for ``reassess`` finding new INTIMATE evidence in the verdict."""
    return fragment.model_copy(update={"privacy_tier": PrivacyTier.INTIMATE})


def test_a_fragment_raised_to_intimate_is_reatomized_off_the_cloud(
    tmp_path: Path,
) -> None:
    """Children of a newly-INTIMATE fragment never reach the writer's provider.

    The root is classified by the request-scoped (cloud) classifier while it is
    PERSONAL; ``reassess`` then raises it to INTIMATE. Every classification the
    re-atomization makes after that must resolve through the INTIMATE route.
    """
    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault, fragment=_fragment("frag-raised-00001"), body=_MULTI_PARAGRAPH_BODY
    )
    config = CreekConfig()
    config.classification.reatomize = True
    config.classification.confidence_threshold = 1.0
    served: list[str] = []

    def _classify(
        self: LLMClassifier, fragment: Fragment, content: str = ""
    ) -> LLMClassificationResult:
        del content
        served.append(self.config.provider)
        return LLMClassificationResult(fragment=fragment, reasoning="")

    with (
        patch.object(
            LLMClassifier, "available", new_callable=PropertyMock, return_value=True
        ),
        patch.object(LLMClassifier, "classify_with_reasoning", _classify),
        patch.object(classify_engine, "reassess", _raised_to_intimate),
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
            request_scoped=RequestScopedClassifier(
                provider=_RejectingProvider(),
                config=LLMConfig(provider="anthropic"),
                intimate_route_serves=True,
            ),
        )

    assert served[0] == "anthropic"
    assert len(served) > 1, "re-atomization classified nothing to assert on"
    assert "anthropic" not in served[1:]


def test_a_raised_fragment_with_no_intimate_route_reatomizes_by_rules(
    tmp_path: Path,
) -> None:
    """No local route to re-resolve to: the children take the rules pass.

    Raising the deferred routing error mid-run would abort the writer's whole
    pass over one fragment; handing the children to the cloud classifier would
    be the egress this re-resolution exists to prevent.
    """
    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault, fragment=_fragment("frag-raised-00002"), body=_MULTI_PARAGRAPH_BODY
    )
    config = CreekConfig()
    config.classification.reatomize = True
    config.classification.confidence_threshold = 1.0
    config.llm.default = LLMConfig(provider="anthropic")
    served: list[str] = []

    def _classify(
        self: LLMClassifier, fragment: Fragment, content: str = ""
    ) -> LLMClassificationResult:
        del content
        served.append(self.config.provider)
        return LLMClassificationResult(fragment=fragment, reasoning="")

    with (
        patch.object(
            LLMClassifier, "available", new_callable=PropertyMock, return_value=True
        ),
        patch.object(LLMClassifier, "classify_with_reasoning", _classify),
        patch.object(classify_engine, "reassess", _raised_to_intimate),
    ):
        summary = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
            request_scoped=RequestScopedClassifier(
                provider=_RejectingProvider(),
                config=LLMConfig(provider="anthropic"),
                intimate_route_serves=True,
            ),
        )

    assert served == ["anthropic"]
    assert summary.errors == ()
