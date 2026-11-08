"""MCP tools never serve provider error text (#2073, #2082, #2083).

``creek.draft`` and ``creek.classify`` were fixed by #2073; ``creek.author``
(#2082) and ``creek.reflect`` (#2083) by the same shape later, and their pins
live here too.

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

Two later sites follow:

- ``creek.author`` answered any desk failure with ``reason=str(exc)`` from its
  broad boundary catch, so a vendor 4xx message, an Ollama URL and a server
  config path all reached the client (#2082).
- ``creek.reflect`` caught only ``RuntimeError`` at its dial, so an Ollama
  ``httpx.HTTPError`` escaped as FastMCP ``isError`` text, URL included
  (#2083). Its operator record is the one exception to the traceback shape:
  the ``/v1`` refusal-log contract
  (``tests/test_api_reflect_refusal_log.py``) forbids provider text in *any*
  record on that path, so reflect logs the exception's type name only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest

from creek.author.client import AuthorLLMClient
from creek.classify.classify_engine import LLMProviderUnavailableError
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV, NonLoopbackOllamaError
from creek.classify.llm.providers import RequestKeyRejectedError
from creek.classify.llm.router import IntimateRoutingError
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.server import Transport, build_server
from creek_mcp.tools import author as author_mod
from creek_mcp.tools import classify as classify_mod
from creek_mcp.tools import draft as draft_mod
from creek_mcp.tools.classify import REQUEST_KEY_REJECTED_REASON, classify_tool
from tests.v1_api_support import seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.classify.llm.completion import Completion


_MARKER: Final[str] = "PROVIDER-LEAK-MARKER"
"""A token planted in every injected provider failure that must never be served."""

_URL: Final[str] = "http://10.9.8.7:11434/api/generate"
"""A non-loopback provider URL planted in the failure; never served either."""

_DRAFT_LOGGER: Final[str] = "creek_mcp.tools.draft"
"""The operator log ``creek.draft`` records its refusal on."""

_CLASSIFY_LOGGER: Final[str] = "creek_mcp.tools.classify"
"""The operator log ``creek.classify`` records its refusal on."""

_AUTHOR_LOGGER: Final[str] = "creek_mcp.tools.author"
"""The operator log ``creek.author`` records its failure on (#2082)."""

_REFLECT_LOGGER: Final[str] = "creek_mcp.tools.reflect"
"""The operator log ``creek.reflect`` records its dial refusal on (#2083)."""


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
    # A real ``(type, value, traceback)`` triple carrying the logged exception:
    # ``is not None`` alone would accept the ``False`` that
    # ``logger.exception(..., exc_info=False)`` stores, a record with no
    # traceback at all.
    assert isinstance(record.exc_info, tuple), record.exc_info
    assert type(record.exc_info[1]).__name__ == type_name
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


# --------------------------------------------------------------------------- #
# creek.author (#2082)
# --------------------------------------------------------------------------- #


def _author_reason(name: str) -> str | None:
    """Return one of :mod:`creek_mcp.tools.author`'s fixed failure constants.

    Read by name, defaulting to ``None``, so this module still collects — and
    each author test fails on its own assertion — against a tree that has not
    defined them yet.

    Args:
        name: The constant's attribute name.

    Returns:
        Its value, or ``None`` when the module does not define it.
    """
    value = getattr(author_mod, name, None)
    assert value is None or isinstance(value, str)
    return value


class _RaisingProvider:
    """A hermetic voice provider whose every completion raises one error.

    Attributes:
        calls: How many completions were attempted.
    """

    is_cloud = True
    model = "hermetic-raising"
    available = True

    def __init__(self, exc: Exception) -> None:
        """Raise *exc* from every completion.

        Args:
            exc: What the provider raises.
        """
        self._exc = exc
        self.calls = 0

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, system: str | None = None
    ) -> Completion:
        """Fail the way the provider did.

        Args:
            prompt: Unread.
            max_tokens: Unread.
            system: Unread.

        Raises:
            Exception: Always, the configured error.
        """
        del prompt, max_tokens, system
        self.calls += 1
        raise self._exc


def _call_author(
    vault: Path,
    factory: Callable[[object], AuthorLLMClient | None] | None = None,
    **arguments: object,
) -> Any:
    """Call the registered ``creek.author`` tool and return FastMCP's answer.

    Args:
        vault: The vault root.
        factory: The injected tier-keyed voice-client factory, if any.
        **arguments: Tool arguments beyond the query.

    Returns:
        FastMCP's raw ``call_tool`` answer.
    """
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, author_llm_factory=factory
    )
    return asyncio.run(server.call_tool("creek.author", {"query": "q", **arguments}))


def _status_error_in_voice() -> httpx.HTTPStatusError:
    """Return an Ollama ``500`` the voice node's provider raises.

    Returns:
        :func:`_http_status_error`'s error, named for the matrix below.
    """
    return _http_status_error()


@pytest.mark.parametrize(
    ("make_error", "type_name"),
    [
        (
            lambda: RuntimeError(
                "Anthropic API call failed: BadRequestError (HTTP 400): "
                f"{_MARKER} {_URL}"
            ),
            "RuntimeError",
        ),
        (
            lambda: httpx.ConnectError(f"connect failed for url {_URL!r} {_MARKER}"),
            "ConnectError",
        ),
        (_status_error_in_voice, "HTTPStatusError"),
    ],
    ids=["runtime", "connect", "status"],
)
def test_mcp_author_refuses_a_provider_failure_without_its_text(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    make_error: Callable[[], Exception],
    type_name: str,
) -> None:
    """A failed voice call is the fixed "provider call failed" reason (#2082).

    The error is raised by the provider the voice client wraps, so it crosses
    the real desk — conductor, voice node, client — before the MCP catch.
    """
    caplog.set_level(logging.WARNING, logger=_AUTHOR_LOGGER)
    provider = _RaisingProvider(make_error())
    client = AuthorLLMClient(provider)

    answer = _call_author(vault, lambda _tier: client)

    structured = _structured(answer)
    assert provider.calls >= 1, "the voice node never dialled; the test is vacuous"
    assert structured["status"] == "error"
    assert structured["reason"] == _author_reason("AUTHOR_PROVIDER_FAILED_REASON")
    _assert_no_leak(answer, "BadRequestError")
    _assert_one_operator_record(caplog, _AUTHOR_LOGGER, type_name)


def test_mcp_author_dry_run_refuses_a_planning_failure_without_its_text(
    vault: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run branch shares the catch, so it answers the fixed reason too."""
    caplog.set_level(logging.WARNING, logger=_AUTHOR_LOGGER)

    def _failing_plan(**_kwargs: object) -> dict[str, Any]:
        """Fail to plan the way a provider-backed specialist does.

        Args:
            **_kwargs: The planner's arguments, unread.

        Raises:
            RuntimeError: Always, carrying the marker and the URL.
        """
        msg = f"{_MARKER} {_URL}"
        raise RuntimeError(msg)

    monkeypatch.setattr(author_mod, "plan_author", _failing_plan)

    answer = _call_author(vault, dry_run=True)

    structured = _structured(answer)
    assert structured["status"] == "error"
    assert structured["dry_run"] is True
    assert structured["reason"] == _author_reason("AUTHOR_PROVIDER_FAILED_REASON")
    _assert_no_leak(answer)
    _assert_one_operator_record(caplog, _AUTHOR_LOGGER, "RuntimeError")


def test_mcp_author_refuses_a_desk_failure_without_its_path(
    vault: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-provider desk failure is the fixed desk reason, no server path."""
    caplog.set_level(logging.WARNING, logger=_AUTHOR_LOGGER)
    server_path = "/srv/creek/private/creek_config.yaml"

    def _stale_config(**_kwargs: object) -> object:
        """Fail the way a stale ``CREEK_CONFIG`` does.

        Args:
            **_kwargs: The desk's arguments, unread.

        Raises:
            FileNotFoundError: Always, naming a server path and the marker.
        """
        msg = f"CREEK_CONFIG points to {server_path} {_MARKER}"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(author_mod, "run_author", _stale_config)

    answer = _call_author(vault)

    structured = _structured(answer)
    assert structured["status"] == "error"
    desk_reason = _author_reason("AUTHOR_DESK_FAILED_REASON")
    assert structured["reason"] == desk_reason
    assert desk_reason != _author_reason("AUTHOR_PROVIDER_FAILED_REASON")
    _assert_no_leak(answer, server_path, "CREEK_CONFIG")
    _assert_one_operator_record(caplog, _AUTHOR_LOGGER, "FileNotFoundError")


def test_mcp_author_keeps_the_routers_own_reason(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``IntimateRoutingError`` is the residual: its creek-written text stays.

    The router builds that message itself — the configured provider's name and
    the remedy, never vendor or corpus text — and the desk's operator needs it
    to act, so it is deliberately not widened, as in ``creek.draft``.
    """
    caplog.set_level(logging.WARNING, logger=_AUTHOR_LOGGER)
    refusal = IntimateRoutingError("anthropic")

    def _no_local_backend(_tier: object) -> AuthorLLMClient:
        """Refuse to egress the way the router's chokepoint does.

        Args:
            _tier: Unread.

        Raises:
            IntimateRoutingError: Always.
        """
        raise refusal

    answer = _call_author(vault, _no_local_backend)

    structured = _structured(answer)
    assert structured["status"] == "error"
    assert structured["reason"] == str(refusal)
    _assert_one_operator_record(caplog, _AUTHOR_LOGGER, "IntimateRoutingError")


def test_mcp_author_refuses_a_rejected_request_key_distinctly(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A vendor-refused writer's key answers the shared fixed reason (#1877)."""
    caplog.set_level(logging.WARNING, logger=_AUTHOR_LOGGER)
    client = AuthorLLMClient(
        _RaisingProvider(RequestKeyRejectedError(f"{_MARKER} (HTTP 401)"))
    )

    answer = _call_author(vault, lambda _tier: client)

    structured = _structured(answer)
    assert structured["status"] == "error"
    assert structured["reason"] == REQUEST_KEY_REJECTED_REASON
    assert (
        _author_reason("AUTHOR_PROVIDER_FAILED_REASON") != REQUEST_KEY_REJECTED_REASON
    )
    _assert_no_leak(answer)
    _assert_one_operator_record(caplog, _AUTHOR_LOGGER, "RequestKeyRejectedError")


# --------------------------------------------------------------------------- #
# creek.reflect (#2083)
# --------------------------------------------------------------------------- #


def _reflect_factory(exc: Exception) -> Callable[..., Callable[[str], str]]:
    """Return a tier-keyed reflect factory whose completion raises *exc*.

    Args:
        exc: What the provider raises mid-dial.

    Returns:
        A ``factory(tier, *, max_tokens)`` that builds a raising callable.
    """

    def _factory(_tier: object, *, max_tokens: int) -> Callable[[str], str]:
        """Build the completion callable.

        Args:
            _tier: Unread.
            max_tokens: Unread.

        Returns:
            A callable that raises *exc*.
        """
        del max_tokens
        return _raising(exc)

    return _factory


def _assert_one_type_name_record(
    caplog: pytest.LogCaptureFixture, logger_name: str, type_name: str
) -> None:
    """Assert one WARNING on *logger_name* naming the type, with no traceback.

    Reflect's dial record carries no ``exc_info``: the ``/v1`` refusal-log
    contract forbids provider text anywhere in the captured log, tracebacks
    included.

    Args:
        caplog: pytest's log capture.
        logger_name: The tool's module logger.
        type_name: The exception class name the record must carry.
    """
    records = [record for record in caplog.records if record.name == logger_name]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    (record,) = records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert type_name in record.getMessage()
    assert _MARKER not in record.getMessage()
    assert _URL not in record.getMessage()
    assert _MARKER not in caplog.text
    assert _URL not in caplog.text


@pytest.mark.parametrize(
    ("make_error", "type_name"),
    [
        (
            lambda: httpx.ConnectError(f"connect failed for url {_URL!r} {_MARKER}"),
            "ConnectError",
        ),
        (_http_status_error, "HTTPStatusError"),
        (NonLoopbackOllamaError, "NonLoopbackOllamaError"),
    ],
    ids=["connect-error", "http-status-error", "non-loopback"],
)
def test_mcp_reflect_refuses_an_ollama_transport_error_without_its_url(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    make_error: Callable[[], httpx.HTTPError],
    type_name: str,
) -> None:
    """An ``httpx`` dial failure is a structured refusal, not ``isError`` text.

    ``httpx.HTTPError`` is not a ``RuntimeError``, so before #2083 it escaped
    ``reflect_tool`` and FastMCP served ``Error executing tool creek.reflect:
    ...`` with the URL in it — here it would raise out of ``call_tool``.
    """
    caplog.set_level(logging.DEBUG, logger=_REFLECT_LOGGER)
    factory = _reflect_factory(make_error())
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, reflect_llm_factory=lambda: factory
    )

    answer = asyncio.run(
        server.call_tool("creek.reflect", {"content": "I walked to the river."})
    )

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == f"reflection unavailable: {type_name}"
    _assert_no_leak(answer)
    _assert_one_type_name_record(caplog, _REFLECT_LOGGER, type_name)


def test_mcp_reflect_logs_a_runtime_dial_failure_by_type_name_only(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The pre-existing ``RuntimeError`` path gains the same one-line record."""
    caplog.set_level(logging.DEBUG, logger=_REFLECT_LOGGER)
    factory = _reflect_factory(RuntimeError(f"{_MARKER} {_URL}"))
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, reflect_llm_factory=lambda: factory
    )

    answer = asyncio.run(
        server.call_tool("creek.reflect", {"content": "I walked to the river."})
    )

    structured = _structured(answer)
    assert structured["status"] == "refused"
    assert structured["reason"] == "reflection unavailable: RuntimeError"
    _assert_no_leak(answer)
    _assert_one_type_name_record(caplog, _REFLECT_LOGGER, "RuntimeError")


# --------------------------------------------------------------------------- #
# creek.reflect: an unloadable vault config (#2091)
# --------------------------------------------------------------------------- #

_UNLOADABLE_CONFIGS: Final[list[tuple[str, str]]] = [
    ("stale-creek-config", "FileNotFoundError"),
    ("schema-invalid", "ValidationError"),
    ("yaml-syntax-invalid", "ParserError"),
]
"""Each way a served vault's config fails to load, and the type it raises.

A YAML syntax error raises ``yaml.YAMLError`` (here ``ParserError``), which is
*not* a ``ValueError``: the ``(RuntimeError, OSError, ValueError)`` tuple #2091
first proposed would leave that case escaping, its message naming the path.
"""


def _unloadable_config(
    case: str, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Arrange a vault config that fails to load the way *case* names.

    Args:
        case: One of :data:`_UNLOADABLE_CONFIGS`' case ids.
        vault: The served vault.
        tmp_path: pytest's per-test directory, outside the vault.
        monkeypatch: pytest's patcher.

    Returns:
        The config path, which must never be served or logged.
    """
    if case == "stale-creek-config":
        path = tmp_path / "secret" / "creek_config.yaml"
        monkeypatch.setenv("CREEK_CONFIG", str(path))
        return path
    if case == "schema-invalid":
        path = tmp_path / "bad" / "creek_config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("not_a_real_key_xyz: 1\n", encoding="utf-8")
        monkeypatch.setenv("CREEK_CONFIG", str(path))
        return path
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    path = vault / "00-Creek-Meta" / "creek_config.yaml"
    path.write_text("model_router: [unclosed\n  : : x\n", encoding="utf-8")
    return path


_CONFIG_LEAKS: Final[tuple[str, ...]] = (
    "CREEK_CONFIG",
    "creek_config.yaml",
    "flow sequence",
    "validation error",
)
"""Fragments of each config-load exception's message; none may be served."""


@pytest.mark.parametrize(
    ("case", "type_name"),
    _UNLOADABLE_CONFIGS,
    ids=[case for case, _ in _UNLOADABLE_CONFIGS],
)
def test_mcp_reflect_refuses_an_unloadable_config_without_its_path(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
    type_name: str,
) -> None:
    """A config that cannot load is a type-name refusal, not ``isError`` text.

    Before #2091 ``creek.reflect`` built its LLM factory and grounding route
    before ``reflect_tool`` ran, outside every catch, so FastMCP served
    ``Error executing tool creek.reflect: CREEK_CONFIG points to /...`` — here
    ``call_tool`` would raise ``ToolError``. The ``yaml-syntax-invalid`` case
    stays red under the issue's original ``(RuntimeError, OSError,
    ValueError)`` tuple, because ``ParserError`` is not a ``ValueError``.

    The refusal is decided before ``reflect_tool`` runs, so — like ``/v1``'s
    build refusal — it appends no audit line: nothing was reflected, and the
    audit append lives inside the tool.
    """
    config_path = _unloadable_config(case, vault, tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG, logger=_REFLECT_LOGGER)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(
        server.call_tool("creek.reflect", {"content": "I walked to the river."})
    )

    payload = _structured(answer)
    assert payload["status"] == "refused"
    assert payload["tool"] == "creek.reflect"
    assert payload["reason"] == f"reflection unavailable: {type_name}"
    _assert_no_leak(answer, str(config_path), *_CONFIG_LEAKS)
    _assert_one_type_name_record(caplog, _REFLECT_LOGGER, type_name)
    for leaked in (str(config_path), *_CONFIG_LEAKS[1:]):
        assert leaked not in caplog.text, leaked
    assert not (vault / MCP_AUDIT_RELPATH).exists()


@pytest.mark.parametrize("case", [case for case, _ in _UNLOADABLE_CONFIGS], ids=str)
def test_mcp_reflect_with_an_injected_factory_never_builds_the_route(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    """An injected factory gets no grounding route, as on ``/v1`` (#2091).

    Before #2091 the MCP surface still built the production grounding route
    beside an injected factory, so an unloadable config failed the call with
    ``ToolError`` through the route alone. ``POST /v1/reflections`` has never
    built that route for an injected factory. Now neither surface does, so the
    config is never read and the call reaches the injected factory's own dial,
    whose failure is the ordinary type-name refusal.
    """
    config_path = _unloadable_config(case, vault, tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG, logger=_REFLECT_LOGGER)
    factory = _reflect_factory(RuntimeError(_MARKER))
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, reflect_llm_factory=lambda: factory
    )

    answer = asyncio.run(
        server.call_tool("creek.reflect", {"content": "I walked to the river."})
    )

    payload = _structured(answer)
    assert payload["status"] == "refused"
    assert payload["reason"] == "reflection unavailable: RuntimeError"
    _assert_no_leak(answer, str(config_path), *_CONFIG_LEAKS)
    _assert_one_type_name_record(caplog, _REFLECT_LOGGER, "RuntimeError")
