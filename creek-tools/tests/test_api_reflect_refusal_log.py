"""``POST /v1/reflections`` names why it refused, and nothing else.

A refused reflection reaches its consumer as one constant per code, which is
right for the wire and useless to an operator: a managed vault that refuses
every keyed reflection looks identical, in every log, to one that never got the
key. These tests pin the one line that tells them apart — the wire code, a
closed-vocabulary cause, and whether a request key arrived — and pin that the
line carries no entry text, no exception message and no key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import pytest
from starlette.testclient import TestClient

from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.httpapi.reflect import REFUSAL_LOGGER_NAME, refusal_cause
from creek_mcp.tools.reflect import PROMPT_TOO_LONG_REASON, RATE_LIMITED_REASON
from tests.v1_api_support import REFLECTIONS_PATH, build_app, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx

    from creek.models import PrivacyTier

_OK: Final[int] = 200

_ENTRY: Final[str] = "zz-refusal-log-entry-zz I walked to the river and made coffee."
"""The submitted entry. Distinctive, so "did the log echo it?" is checkable."""

_INTERNAL_DETAIL: Final[str] = "zz-provider-internals-zz"
"""An exception message that must never reach the log."""

_FAKE_KEY: Final[str] = "sk-ant-zzrefusallogfakekeyzz0123456789"
"""A key-shaped request key, so the vault parses it and the log must hide it."""


def _raising_factory(tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
    """Refuse to build a model callable, the way an unavailable provider does.

    Args:
        tier: The routing tier, unused.
        max_tokens: The output ceiling, unused.

    Returns:
        Never returns.

    Raises:
        RuntimeError: Always, carrying a detail the log must not repeat.
    """
    del tier, max_tokens
    raise RuntimeError(_INTERNAL_DETAIL)


def _empty_factory(tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
    """Answer every completion with nothing, an answer rather than a refusal.

    Args:
        tier: The routing tier, unused.
        max_tokens: The output ceiling, unused.

    Returns:
        A completion callable that returns an empty turn.
    """
    del tier, max_tokens
    return lambda prompt: ""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault."""
    yield seed_vault(tmp_path / "vault")


def _post(
    vault_path: Path,
    factory: Callable[..., Callable[[str], str]],
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Send one open-ceiling reflection request over *factory*.

    Args:
        vault_path: The vault the app is built over.
        factory: The tier-keyed LLM factory the app is given.
        extra_headers: Headers to add, such as a request key.

    Returns:
        The response.
    """
    app = build_app(vault_path=vault_path, reflect_llm_factory=lambda: factory)
    with TestClient(app) as client:
        response: httpx.Response = client.post(
            REFLECTIONS_PATH,
            json={"content": _ENTRY},
            headers={**headers(ceiling="open"), **(extra_headers or {})},
        )
    return response


def _refusal_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the records the refusal logger emitted.

    Args:
        caplog: The log capture.

    Returns:
        Every record on :data:`REFUSAL_LOGGER_NAME`, in order.
    """
    return [r for r in caplog.records if r.name == REFUSAL_LOGGER_NAME]


def test_a_provider_failure_logs_its_code_cause_and_no_key(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A keyless provider failure is one WARNING naming the cause and ``none``."""
    caplog.set_level(logging.WARNING, logger=REFUSAL_LOGGER_NAME)

    _post(vault, _raising_factory)

    [record] = _refusal_lines(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "code=temporarily_unavailable" in message
    assert "cause=reflection unavailable: RuntimeError" in message
    assert "request_key=none" in message
    assert "request_id=" in message


def test_a_request_key_is_named_by_provider_and_never_by_value(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal that carried a key says which vendor's, and never the key."""
    caplog.set_level(logging.WARNING, logger=REFUSAL_LOGGER_NAME)

    _post(
        vault,
        _raising_factory,
        {PROVIDER_HEADER: "anthropic", PROVIDER_KEY_HEADER: _FAKE_KEY},
    )

    [record] = _refusal_lines(caplog)
    assert "request_key=anthropic" in record.getMessage()
    assert _FAKE_KEY not in caplog.text


def test_a_malformed_request_key_is_logged_as_its_own_cause(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Key headers the vault cannot parse are named, so a dropped key is no mystery."""
    caplog.set_level(logging.WARNING, logger=REFUSAL_LOGGER_NAME)

    _post(vault, _empty_factory, {PROVIDER_HEADER: "anthropic"})

    [record] = _refusal_lines(caplog)
    message = record.getMessage()
    assert "code=invalid_request" in message
    assert "cause=invalid_request_key" in message


def test_the_log_carries_no_entry_text_and_no_exception_message(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The line is closed-vocabulary: nothing the writer or the provider wrote."""
    caplog.set_level(logging.DEBUG)

    _post(vault, _raising_factory)

    assert _refusal_lines(caplog)
    assert "zz-refusal-log-entry-zz" not in caplog.text
    assert _INTERNAL_DETAIL not in caplog.text


def test_an_answered_reflection_logs_no_refusal(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty answer is an answer: the refusal logger stays silent."""
    caplog.set_level(logging.DEBUG, logger=REFUSAL_LOGGER_NAME)

    response = _post(vault, _empty_factory)

    assert response.status_code == _OK
    assert _refusal_lines(caplog) == []


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        (PROMPT_TOO_LONG_REASON, PROMPT_TOO_LONG_REASON),
        (RATE_LIMITED_REASON, RATE_LIMITED_REASON),
        (
            "reflection unavailable: IntimateRoutingError",
            "reflection unavailable: IntimateRoutingError",
        ),
        ("reflection unavailable: not an identifier!", "unrecognised"),
        ("a reason this module has never published", "unrecognised"),
    ],
)
def test_the_cause_is_closed_vocabulary(reason: str, cause: str) -> None:
    """Only published reasons and bare exception type names are logged verbatim.

    Args:
        reason: A tool refusal reason.
        cause: What the log may say about it.
    """
    assert refusal_cause(reason) == cause
