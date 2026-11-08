"""MCP ``creek.draft`` / ``creek.classify`` never serve provider error text (#2073).

Both tools used to answer a provider failure with ``reason=str(exc)``. The two
sites carry different risk, and both are pinned here:

- ``creek.draft`` is the real leak. ``provider.complete`` raises vendor SDK
  errors as ``RuntimeError`` whose message carries the API's own 4xx text
  (:func:`creek.classify.llm.providers._error_detail`), and that message was the
  refusal reason. An Ollama ``httpx.HTTPError`` is not a ``RuntimeError`` at
  all, so it escaped the tool and FastMCP served it as
  ``Error executing tool creek.draft: ... for url 'http://...'``.
- ``creek.classify`` is hardening. Its one exception carries creek-written text
  today (the provider name and env-var remediation), but that is operator
  guidance, not something an MCP client may be handed, and the next raise site
  might not be so careful.

Each tool now answers a fixed reason and keeps the detail on the traceback of
its own operator log record, the shape #2065 gave ``creek.link`` and
``creek.report``. Every assertion reads the served payload *and* FastMCP's
serialized answer, because the leak lived in what the client received, not in
what the tool function returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest

from creek.classify.classify_engine import LLMProviderUnavailableError
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.providers import RequestKeyRejectedError
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.server import Transport, build_server
from creek_mcp.tools import classify as classify_mod
from creek_mcp.tools import draft as draft_mod
from creek_mcp.tools.classify import REQUEST_KEY_REJECTED_REASON, classify_tool
from tests.v1_api_support import seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_MARKER: Final[str] = "PROVIDER-LEAK-MARKER"
"""A token planted in every injected provider failure that must never be served."""

_URL: Final[str] = "http://10.9.8.7:11434/api/generate"
"""A non-loopback provider URL planted in the failure; never served either."""

_DRAFT_LOGGER: Final[str] = "creek_mcp.tools.draft"
"""The operator log ``creek.draft`` records its refusal on."""

_CLASSIFY_LOGGER: Final[str] = "creek_mcp.tools.classify"
"""The operator log ``creek.classify`` records its refusal on."""


def _draft_reason(name: str) -> str:
    """Return one of :mod:`creek_mcp.tools.draft`'s fixed refusal constants.

    Read by name so this module still collects, and fails per test, against a
    tree that has not defined them yet.

    Args:
        name: The constant's attribute name.

    Returns:
        Its value.
    """
    value = getattr(draft_mod, name)
    assert isinstance(value, str)
    return value


def _classify_unavailable_reason() -> str:
    """Return ``creek.classify``'s fixed provider-unavailable refusal.

    Returns:
        :data:`creek_mcp.tools.classify.LLM_PROVIDER_UNAVAILABLE_REASON`.
    """
    value = classify_mod.LLM_PROVIDER_UNAVAILABLE_REASON
    assert isinstance(value, str)
    return value


def _seed() -> IdeaSeed:
    """Return the sourceless ontology-tuple seed a bare vault mines."""
    return IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title="Unwalked ground",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="An ontology combination not yet inhabited.",
        score=0.5,
    )


@pytest.fixture(name="vault")
def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a vault whose miner surfaces one seed, outside both pin modes.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: pytest's patcher.

    Returns:
        The vault root.
    """
    vault = seed_vault(tmp_path / "vault")
    monkeypatch.setattr(
        draft_mod,
        "IdeaMiner",
        lambda **_kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda _self, _vault_path, *, current_phase: [_seed()]},
        )(),
    )
    monkeypatch.delenv(LOOPBACK_ONLY_ENV, raising=False)
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    return vault


def _raising(exc: Exception) -> Callable[[str], str]:
    """Return a draft LLM double that raises *exc* when it is called.

    Args:
        exc: What the provider raises.

    Returns:
        A prompt → text callable that never returns.
    """

    def _llm(_prompt: str) -> str:
        """Fail the way the provider did.

        Args:
            _prompt: Unread.

        Raises:
            Exception: Always, *exc*.
        """
        raise exc

    return _llm


def _call_draft(vault: Path, factory: Callable[[object], Callable[[str], str]]) -> Any:
    """Call the registered ``creek.draft`` tool and return FastMCP's answer.

    Args:
        vault: The vault root.
        factory: The injected tier-keyed draft LLM factory.

    Returns:
        FastMCP's raw ``call_tool`` answer.
    """
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, draft_llm_factory=factory
    )
    return asyncio.run(server.call_tool("creek.draft", {}))


def _structured(answer: Any) -> dict[str, Any]:
    """Return the structured payload of a FastMCP answer.

    Args:
        answer: FastMCP's ``call_tool`` answer.

    Returns:
        The tool's dict.
    """
    structured = answer[1] if isinstance(answer, tuple) else answer
    assert isinstance(structured, dict), structured
    return structured


def _assert_no_leak(answer: Any, *extra: str) -> None:
    """Assert neither the marker, the URL nor any *extra* reached the client.

    Args:
        answer: FastMCP's answer, or a tool's return value.
        *extra: Further strings the answer must not carry.
    """
    structured = _structured(answer)
    rendered = (json.dumps(structured), str(answer))
    for leaked in (_MARKER, _URL, *extra):
        for text in rendered:
            assert leaked not in text, (leaked, text)


def _assert_one_operator_record(
    caplog: pytest.LogCaptureFixture, logger_name: str, type_name: str
) -> None:
    """Assert exactly one record on *logger_name*, naming the type and no detail.

    Args:
        caplog: pytest's log capture.
        logger_name: The tool's module logger.
        type_name: The exception class name the record must carry.
    """
    records = [record for record in caplog.records if record.name == logger_name]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    (record,) = records
    assert record.exc_info is not None
    assert type_name in record.getMessage()
    assert _MARKER not in record.getMessage()
    assert _URL not in record.getMessage()


def test_mcp_draft_refuses_a_provider_failure_without_its_text(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A vendor 4xx from the generate call is the fixed "call failed" refusal."""
    caplog.set_level(logging.WARNING, logger=_DRAFT_LOGGER)
    msg = f"Anthropic API call failed: BadRequestError (HTTP 400): {_MARKER} {_URL}"
    llm = _raising(RuntimeError(msg))

    answer = _call_draft(vault, lambda _tier: llm)

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == _draft_reason("DRAFT_PROVIDER_FAILED_REASON")
    _assert_no_leak(answer, "BadRequestError")
    _assert_one_operator_record(caplog, _DRAFT_LOGGER, "RuntimeError")


def _http_status_error() -> httpx.HTTPStatusError:
    """Return the error ``response.raise_for_status()`` raises on an Ollama 500.

    Returns:
        An :class:`httpx.HTTPStatusError` whose message names :data:`_URL`.
    """
    request = httpx.Request("POST", _URL)
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError(
        f"Server error '500' for url {_URL!r} {_MARKER}",
        request=request,
        response=response,
    )


@pytest.mark.parametrize(
    ("make_error", "type_name"),
    [
        (
            lambda: httpx.ConnectError(f"connect failed for url {_URL!r} {_MARKER}"),
            "ConnectError",
        ),
        (_http_status_error, "HTTPStatusError"),
    ],
    ids=["connect-error", "http-status-error"],
)
def test_mcp_draft_refuses_an_ollama_transport_error_without_its_url(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    make_error: Callable[[], httpx.HTTPError],
    type_name: str,
) -> None:
    """An ``httpx`` error is a structured refusal, not FastMCP ``isError`` text.

    ``httpx.HTTPError`` is not a ``RuntimeError``, so before #2073 it escaped
    ``draft_tool`` and FastMCP served ``Error executing tool creek.draft: ...``
    with the URL in it — here it would raise out of ``call_tool`` instead.
    """
    caplog.set_level(logging.WARNING, logger=_DRAFT_LOGGER)
    llm = _raising(make_error())

    answer = _call_draft(vault, lambda _tier: llm)

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == _draft_reason("DRAFT_PROVIDER_FAILED_REASON")
    _assert_no_leak(answer)
    _assert_one_operator_record(caplog, _DRAFT_LOGGER, type_name)


def test_mcp_draft_refuses_a_rejected_request_key_distinctly(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A vendor-refused writer's key keeps its own fixed reason (#1877)."""
    caplog.set_level(logging.WARNING, logger=_DRAFT_LOGGER)
    llm = _raising(RequestKeyRejectedError(f"{_MARKER} (HTTP 401)"))

    answer = _call_draft(vault, lambda _tier: llm)

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == REQUEST_KEY_REJECTED_REASON
    assert REQUEST_KEY_REJECTED_REASON not in {
        _draft_reason("DRAFT_PROVIDER_FAILED_REASON"),
        _draft_reason("DRAFT_PROVIDER_UNAVAILABLE_REASON"),
    }
    _assert_no_leak(answer)
    _assert_one_operator_record(caplog, _DRAFT_LOGGER, "RequestKeyRejectedError")


def test_mcp_draft_factory_runtime_error_does_not_leak(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that cannot be built is the fixed "unavailable" refusal."""
    caplog.set_level(logging.WARNING, logger=_DRAFT_LOGGER)

    def _unbuildable(_tier: object) -> Callable[[str], str]:
        """Fail to build the provider the way a constructor does.

        Args:
            _tier: Unread.

        Raises:
            RuntimeError: Always, carrying the marker and the URL.
        """
        msg = f"{_MARKER} {_URL}"
        raise RuntimeError(msg)

    answer = _call_draft(vault, _unbuildable)

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == _draft_reason("DRAFT_PROVIDER_UNAVAILABLE_REASON")
    _assert_no_leak(answer)
    _assert_one_operator_record(caplog, _DRAFT_LOGGER, "RuntimeError")


def _unavailable(**_kwargs: object) -> dict[str, Any]:
    """Refuse the way ``run_classify`` does when its provider is unreachable.

    Args:
        **_kwargs: The engine's arguments, unread.

    Raises:
        LLMProviderUnavailableError: Always, with the marker and URL as detail.
    """
    raise LLMProviderUnavailableError(provider="ollama", detail=f"{_MARKER} {_URL}")


def test_mcp_classify_refuses_provider_unavailability_without_its_text(
    vault: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's CLI message — provider name included — never reaches MCP."""
    caplog.set_level(logging.WARNING, logger=_CLASSIFY_LOGGER)
    monkeypatch.setattr(classify_mod, "run_classify", _unavailable)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(server.call_tool("creek.classify", {"method": "llm"}))

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == _classify_unavailable_reason()
    _assert_no_leak(answer, "ollama")
    _assert_one_operator_record(caplog, _CLASSIFY_LOGGER, "LLMProviderUnavailableError")


def test_classify_fallback_reason_is_the_fixed_constant(
    vault: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed vault's rules fallback records the fixed reason, not the text."""
    caplog.set_level(logging.WARNING, logger=_CLASSIFY_LOGGER)
    engine = classify_mod.run_classify

    def _llm_unavailable(**kwargs: Any) -> object:
        """Refuse the ``llm`` pass and run the ``rules`` pass for real.

        Args:
            **kwargs: The engine's arguments.

        Returns:
            The real engine's summary for a ``rules`` pass.
        """
        if kwargs["method"] == "llm":
            _unavailable()
        return engine(**kwargs)

    monkeypatch.setattr(classify_mod, "run_classify", _llm_unavailable)

    result = classify_tool(vault_path=vault, method="llm", fallback_to_rules=True)

    assert result["status"] == "ok"
    assert result["fallback_from"] == "llm"
    assert result["fallback_reason"] == _classify_unavailable_reason()
    _assert_no_leak(result, "ollama")
    _assert_one_operator_record(caplog, _CLASSIFY_LOGGER, "LLMProviderUnavailableError")
