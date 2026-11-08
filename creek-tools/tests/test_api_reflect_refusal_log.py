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

import httpx
import pytest
from starlette.testclient import TestClient

from creek.classify.llm.providers import ProviderRateLimitError
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.httpapi.reflect import REFUSAL_LOGGER_NAME, refusal_cause
from creek_mcp.tools.reflect import PROMPT_TOO_LONG_REASON, RATE_LIMITED_REASON
from tests.v1_api_support import REFLECTIONS_PATH, build_app, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from creek.models import PrivacyTier

_OK: Final[int] = 200

_ENTRY: Final[str] = "zz-refusal-log-entry-zz I walked to the river and made coffee."
"""The submitted entry. Distinctive, so "did the log echo it?" is checkable."""

_INTERNAL_DETAIL: Final[str] = "zz-provider-internals-zz"
"""An exception message that must never reach the log."""

_FAKE_KEY: Final[str] = "sk-ant-zzrefusallogfakekeyzz0123456789"
"""A key-shaped request key, so the vault parses it and the log must hide it."""

_OLLAMA_URL: Final[str] = "http://10.9.8.7:11434/api/generate"
"""A non-loopback Ollama URL an ``httpx`` error names; never logged (#2083)."""


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


def _unreachable_factory(tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
    """Build a completion whose Ollama endpoint cannot be reached (#2083).

    Args:
        tier: The routing tier, unused.
        max_tokens: The output ceiling, unused.

    Returns:
        A completion callable that raises an ``httpx`` transport error.
    """
    del tier, max_tokens

    def _dial(prompt: str) -> str:
        """Fail the transport, naming a URL and a detail the log must not repeat.

        Args:
            prompt: Unread.

        Raises:
            httpx.ConnectError: Always.
        """
        del prompt
        msg = f"connect failed for url {_OLLAMA_URL!r} {_INTERNAL_DETAIL}"
        raise httpx.ConnectError(msg)

    return _dial


def test_an_ollama_transport_error_logs_its_type_and_no_url(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An ``httpx`` dial failure is logged as a ``503`` refusal, type name only.

    Before #2083 the error escaped the tool and the boundary logged a ``500``;
    now it is the same closed-vocabulary line any outage gets, and nothing on
    any logger — tracebacks included — carries the URL or the message.
    """
    caplog.set_level(logging.DEBUG)

    _post(vault, _unreachable_factory)

    [record] = _refusal_lines(caplog)
    message = record.getMessage()
    assert "code=temporarily_unavailable" in message
    assert "cause=reflection unavailable: ConnectError" in message
    assert _INTERNAL_DETAIL not in caplog.text
    assert _OLLAMA_URL not in caplog.text


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
        ("reflection request key rejected", "reflection request key rejected"),
        (
            "reflection unavailable: IntimateRoutingError",
            "reflection unavailable: IntimateRoutingError",
        ),
        (
            "reflection unavailable: ConnectError",
            "reflection unavailable: ConnectError",
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


_SERVICE_UNAVAILABLE: Final[int] = 503


def test_a_syntax_invalid_vault_config_is_a_logged_503_without_its_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A vault config YAML cannot parse refuses ``503``, never a ``500`` (#2091).

    The production path, with no injected factory: building the reflection
    factory reads the vault's own config, and a YAML syntax error raises
    ``yaml.YAMLError`` — not a ``ValueError`` — which escaped the build's catch
    and reached the boundary as ``500 internal_error``. Its message names the
    file's path, so nothing it says may reach the response or any log record.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    config = vault / "00-Creek-Meta" / "creek_config.yaml"
    config.write_text("model_router: [unclosed\n  : : x\n", encoding="utf-8")
    caplog.set_level(logging.DEBUG)

    app = build_app(vault_path=vault)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            REFLECTIONS_PATH, json={"content": _ENTRY}, headers=headers(ceiling="open")
        )

    assert response.status_code == _SERVICE_UNAVAILABLE
    assert response.json()["code"] == "temporarily_unavailable"
    [record] = _refusal_lines(caplog)
    assert "cause=reflection unavailable: ParserError" in record.getMessage()
    for leaked in (str(config), "flow sequence"):
        assert leaked not in caplog.text, leaked
        assert leaked not in response.text, leaked


_TOO_MANY_REQUESTS: Final[int] = 429


def test_a_rate_limit_raised_while_building_keeps_the_unavailable_503(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A build-time rate limit is still ``503``, not the dial's ``429`` (#2091).

    The shared seam builder names *any* build failure by type, as this route
    always has; only a rate limit met at the dial is ``rate_limited``. Pinned
    so routing the build through the MCP tool's helper changed no wire code.
    """
    caplog.set_level(logging.WARNING, logger=REFUSAL_LOGGER_NAME)

    def _thunk() -> Callable[..., Callable[[str], str]]:
        """Fail to build the factory with a rate limit.

        Raises:
            ProviderRateLimitError: Always.
        """
        raise ProviderRateLimitError(_INTERNAL_DETAIL)

    app = build_app(vault_path=vault, reflect_llm_factory=_thunk)
    with TestClient(app) as client:
        response = client.post(
            REFLECTIONS_PATH, json={"content": _ENTRY}, headers=headers(ceiling="open")
        )

    assert response.status_code == _SERVICE_UNAVAILABLE
    assert response.status_code != _TOO_MANY_REQUESTS
    [record] = _refusal_lines(caplog)
    message = record.getMessage()
    assert "code=temporarily_unavailable" in message
    assert "cause=reflection unavailable: ProviderRateLimitError" in message
    assert _INTERNAL_DETAIL not in caplog.text


_YAML_INVALID_CONFIG: Final[str] = "model_router: [unclosed\n  : : x\n"
"""A config body YAML cannot parse; ``yaml`` says "flow sequence" about it."""

_YAML_LEAK: Final[str] = "flow sequence"
"""The parser's own wording, which names nothing a client may see."""


def _missing_creek_config(tmp_path: Path, vault_path: Path) -> Path:
    """Name a ``CREEK_CONFIG`` file that does not exist.

    Args:
        tmp_path: The test's scratch root.
        vault_path: The seeded vault, unused: nothing names it.

    Returns:
        A path under a distinctive directory, so a leak of it is checkable.
    """
    del vault_path
    return tmp_path / "zz-secret-config-dir" / "creek_config.yaml"


def _yaml_invalid_creek_config(tmp_path: Path, vault_path: Path) -> Path:
    """Write a ``CREEK_CONFIG`` that names the vault but YAML cannot parse.

    Args:
        tmp_path: The test's scratch root.
        vault_path: The vault the config would name if it parsed.

    Returns:
        The written config's path.
    """
    config = tmp_path / "zz-secret-config-dir" / "creek_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"vault_path: {vault_path}\n{_YAML_INVALID_CONFIG}", encoding="utf-8"
    )
    return config


def _post_reflection(app_kwargs: dict[str, object]) -> httpx.Response:
    """Send one open-ceiling reflection to an app built from *app_kwargs*.

    Args:
        app_kwargs: Passed through to :func:`build_app`.

    Returns:
        The response; a server exception is rendered, never raised.
    """
    app = build_app(**app_kwargs)
    with TestClient(app, raise_server_exceptions=False) as client:
        response: httpx.Response = client.post(
            REFLECTIONS_PATH, json={"content": _ENTRY}, headers=headers(ceiling="open")
        )
    return response


@pytest.mark.parametrize(
    "write_config",
    [_missing_creek_config, _yaml_invalid_creek_config],
    ids=["missing", "yaml-invalid"],
)
def test_an_unreadable_creek_config_without_a_vault_path_is_unavailable(
    tmp_path: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_config: Callable[[Path, Path], Path],
) -> None:
    """Production resolves no vault from a broken ``CREEK_CONFIG``: ``unavailable``.

    Every production entry point builds the app with no ``vault_path``, so the
    vault is resolved from ``CREEK_CONFIG``. A file that is missing or that
    YAML cannot parse resolves no vault, which is the operator-action
    ``503 unavailable`` (cause ``no_vault``), not the retryable
    ``temporarily_unavailable`` a vault whose own config fails to load earns.
    Pins the ``docs/api.md`` ``POST /v1/reflections`` row (#2091), and that
    neither the response nor any log record names the config's path.

    Args:
        tmp_path: The test's scratch root.
        vault: A seeded vault the broken config would otherwise name.
        monkeypatch: Points ``CREEK_CONFIG`` at the broken file.
        caplog: The log capture, at ``DEBUG`` on every logger.
        write_config: Builds the broken config and returns its path.
    """
    config = write_config(tmp_path, vault)
    monkeypatch.setenv("CREEK_CONFIG", str(config))
    caplog.set_level(logging.DEBUG)

    response = _post_reflection({})

    assert response.status_code == _SERVICE_UNAVAILABLE
    assert response.json()["code"] == "unavailable"
    [record] = _refusal_lines(caplog)
    message = record.getMessage()
    assert "code=unavailable" in message
    assert "cause=no_vault" in message
    for leaked in (str(config), "zz-secret-config-dir", _YAML_LEAK):
        assert leaked not in caplog.text, leaked
        assert leaked not in response.text, leaked


def test_a_stale_creek_config_beside_an_explicit_vault_is_temporarily_unavailable(
    tmp_path: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vault that resolves but whose config cannot load is retryable (#2091).

    With an explicit ``vault_path`` the vault resolves without reading
    ``CREEK_CONFIG``; building the reflection's factory then reads it and
    fails, which is the ``temporarily_unavailable`` refusal the
    ``docs/api.md`` row names for that case, type name only.

    Args:
        tmp_path: The test's scratch root.
        vault: The explicitly configured vault.
        monkeypatch: Points ``CREEK_CONFIG`` at a file that does not exist.
        caplog: The log capture, at ``DEBUG`` on every logger.
    """
    config = _missing_creek_config(tmp_path, vault)
    monkeypatch.setenv("CREEK_CONFIG", str(config))
    caplog.set_level(logging.DEBUG)

    response = _post_reflection({"vault_path": vault})

    assert response.status_code == _SERVICE_UNAVAILABLE
    assert response.json()["code"] == "temporarily_unavailable"
    [record] = _refusal_lines(caplog)
    assert "cause=reflection unavailable: FileNotFoundError" in record.getMessage()
    for leaked in (str(config), "zz-secret-config-dir"):
        assert leaked not in caplog.text, leaked
        assert leaked not in response.text, leaked
