"""MCP ``creek.link`` / ``creek.report`` refuse an unloadable embedding model (#2065).

:meth:`creek.link.embeddings.EmbeddingLinker.load_model` turns any loader
failure into :class:`~creek.link.embeddings.EmbeddingModelUnavailableError`
whose message names the configured model, the cache directory and the
third-party error verbatim (``Underlying error: ...``). Neither tool caught it,
so FastMCP 1.x rendered the uncaught exception as an ``isError`` result carrying
``str(exc)`` — operator remediation text, a host path and whatever a proxy or
HuggingFace client wrote, handed to an MCP client. Both tools now answer the
fixed, content-free ``embedding model unavailable`` refusal and keep the detail
on the traceback of their own operator log record.

Which paths reach the loader, and therefore which are pinned here:

- ``creek.link`` — all three embedding stages. ``embeddings`` (the tool's
  default), ``eddies`` and ``threads`` each reach
  ``creek.link.link_engine._load_or_compute_embeddings`` and embed every
  uncached fragment before writing anything, so a refusal leaves no partial
  write. ``temporal`` needs no vectors.
- ``creek.report unnamed`` — ``UnnamedDigestGenerator.detect_unnamed_clusters``
  embeds the week's fragments before the digest or history file is written.
- ``creek.report paradox`` / ``synchronicity`` are **cache-only**: both read the
  parquet cache through ``EmbeddingLinker.load_cache`` and score it with
  ``find_resonances``, and neither ever calls ``load_model``. They cannot raise
  this error, so they need no refusal; the last test below pins that, so the day
  one of them starts embedding live it fails here rather than leaking.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.config import CreekConfig, EmbeddingsConfig
from creek.link import embeddings as embeddings_module
from creek.link.embeddings import EmbeddingRefusedError
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.server import Transport, build_server
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import link as link_mod
from creek_mcp.tools import report as report_mod
from creek_mcp.tools.link import link_tool
from creek_mcp.tools.report import report_tool
from tests.v1_api_support import seed_vault

if TYPE_CHECKING:
    from pathlib import Path


_MARKER: Final[str] = "EMBED-LEAK-MARKER"
"""A token planted in the injected loader failure that must never be served."""

_REASON: Final[str] = "embedding model unavailable"
"""The fixed refusal both tools answer with."""

_LINK_LOGGER: Final[str] = "creek_mcp.tools.link"
"""The operator log ``creek.link`` records the refusal on."""

_REPORT_LOGGER: Final[str] = "creek_mcp.tools.report"
"""The operator log ``creek.report`` records the refusal on."""

_UNNAMED_DIR: Final[tuple[str, ...]] = ("10-Liminal", "Unnamed")
"""Where the ``unnamed`` report reads its fragments from."""

_UNNAMED_HISTORY: Final[tuple[str, ...]] = (
    "00-Creek-Meta",
    "Processing-Log",
    "unnamed-history.json",
)
"""The append-only record an ``unnamed`` run writes beside its digest."""

_EMBEDDING_METHODS: Final[tuple[str, ...]] = ("embeddings", "eddies", "threads")
"""Every ``creek.link`` stage that reaches the embedding model."""


class _Setup:
    """What one test needs to know about its vault and its sentinels.

    Attributes:
        vault: The vault root.
        cache_dir: The configured model-cache directory — a path the refusal
            must not echo.
        loads: Every model name the patched loader was asked for.
    """

    def __init__(self, vault: Path, cache_dir: Path, loads: list[str]) -> None:
        """Bind the three fields.

        Args:
            vault: The vault root.
            cache_dir: The sentinel cache directory.
            loads: The loader's call record.
        """
        self.vault = vault
        self.cache_dir = cache_dir
        self.loads = loads

    def leaks(self) -> tuple[str, ...]:
        """Return every string the refusal must not carry.

        Returns:
            The marker, the engine's remediation vocabulary, the model name,
            the cache directory and the vault path.
        """
        return (
            _MARKER,
            "Underlying error",
            "sentence-transformers",
            EmbeddingsConfig().model,
            str(self.cache_dir),
            str(self.vault),
        )


def _write_note(directory: Path, name: str, body: str) -> None:
    """Write one self-authored, open-tier fragment dated today.

    ``ingested`` is pinned to noon *today* so the ``unnamed`` report's
    this-week window always contains it, whatever the wall clock.

    Args:
        directory: Where the file lands.
        name: The file stem and fragment id suffix.
        body: The fragment's prose.
    """
    directory.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": f"frag-{name}",
        "title": name,
        "source": {"platform": "markdown", "author": "self"},
        "privacy_tier": "open",
        "ingested": datetime.combine(date.today(), time(12)).isoformat(),
    }
    (directory / f"{name}.md").write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


@pytest.fixture(name="setup")
def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Setup:
    """Return a vault whose embedding model cannot load.

    The loader fails the way a blocked download does, with the marker in its
    text; both tools' config resolvers return a config whose ``cache_dir`` is a
    sentinel path, so the engine's message names it.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: pytest's patcher.

    Returns:
        The vault, the sentinel cache directory and the loader's call record.
    """
    vault = seed_vault(tmp_path / "vault")
    cache_dir = tmp_path / "model-cache-sentinel"
    config = CreekConfig(
        vault_path=vault,
        embeddings=EmbeddingsConfig(cache_dir=str(cache_dir)),
    )
    loads: list[str] = []

    def _fail(model_name: str, _cache_folder: str | None = None) -> object:
        """Record the load, then fail the way a blocked download does.

        Args:
            model_name: The configured model name.
            _cache_folder: Unread.

        Raises:
            OSError: Always, carrying the marker.
        """
        loads.append(model_name)
        msg = f"ProxyError: 403 {_MARKER}"
        raise OSError(msg)

    monkeypatch.setattr(embeddings_module, "_load_sentence_transformer", _fail)
    monkeypatch.setattr(link_mod, "load_vault_config", lambda _vault: config)
    monkeypatch.setattr(report_mod, "_vault_config", lambda _request: config)
    notes = vault / "01-Fragments" / "Notes"
    for index in range(3):
        _write_note(notes, f"f{index}", f"a quiet river at dusk, entry {index}")
    return _Setup(vault, cache_dir, loads)


def _audited_tools(vault: Path) -> list[str]:
    """Return the ``tool`` of every MCP audit entry under *vault*.

    Args:
        vault: The vault root.

    Returns:
        Tool names in append order; empty when nothing was audited.
    """
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [
        str(json.loads(line)["tool"])
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_fixed_refusal(result: object, setup: _Setup) -> None:
    """Assert *result* is the fixed refusal and carries none of the leak strings.

    Args:
        result: A tool's return value, or FastMCP's serialized answer.
        setup: The test's sentinels.
    """
    assert isinstance(result, dict), result
    assert result["status"] == "refused"
    assert result["reason"] == _REASON
    rendered = str(result)
    for leaked in setup.leaks():
        assert leaked not in rendered, leaked


def _assert_one_operator_record(
    caplog: pytest.LogCaptureFixture, logger_name: str, type_name: str
) -> None:
    """Assert exactly one record on *logger_name* names the type and no detail.

    Args:
        caplog: pytest's log capture.
        logger_name: The tool's module logger.
        type_name: The exception class name the record must carry.
    """
    records = [
        record
        for record in caplog.records
        if record.name == logger_name and type_name in record.getMessage()
    ]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    (record,) = records
    assert record.exc_info is not None
    assert _MARKER not in record.getMessage()
    assert "Underlying error" not in record.getMessage()


@pytest.mark.parametrize("method", _EMBEDDING_METHODS)
def test_link_tool_refuses_an_unloadable_embedding_model_without_leaking(
    setup: _Setup, caplog: pytest.LogCaptureFixture, method: str
) -> None:
    """Every embedding stage answers the fixed refusal, and audits nothing."""
    caplog.set_level(logging.WARNING, logger=_LINK_LOGGER)

    result = link_tool(vault_path=setup.vault, method=method)

    assert setup.loads, f"{method} never reached the embedding model"
    _assert_fixed_refusal(result, setup)
    _assert_one_operator_record(caplog, _LINK_LOGGER, "EmbeddingModelUnavailableError")
    assert "creek.link" not in _audited_tools(setup.vault)


def test_link_tool_refuses_a_refused_embedding_route(
    setup: _Setup, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-local route's refusal subclass earns the same fixed refusal."""
    caplog.set_level(logging.WARNING, logger=_LINK_LOGGER)

    def _refuse(**_kwargs: object) -> object:
        """Refuse the way a pool route that produced no vector does.

        Args:
            **_kwargs: The engine's arguments, unread.

        Raises:
            EmbeddingRefusedError: Always, carrying the marker.
        """
        msg = f"route refused {_MARKER}"
        raise EmbeddingRefusedError(msg)

    monkeypatch.setattr(link_mod, "run_link", _refuse)

    result = link_tool(vault_path=setup.vault, method="eddies")

    _assert_fixed_refusal(result, setup)
    _assert_one_operator_record(caplog, _LINK_LOGGER, "EmbeddingRefusedError")


def test_creek_link_over_fastmcp_is_a_refusal_not_an_iserror(setup: _Setup) -> None:
    """Through FastMCP the default call is a structured refusal, not ``isError``.

    FastMCP 1.x wraps an uncaught tool exception as
    ``ToolError(f"Error executing tool {name}: {e}")`` and serves it as an
    ``isError`` result, which is how the engine's message reached the client.
    """
    server = build_server(transport=Transport.STDIO, vault_path=setup.vault)

    answer = asyncio.run(server.call_tool("creek.link", {"method": "embeddings"}))

    structured = answer[1] if isinstance(answer, tuple) else answer
    _assert_fixed_refusal(structured, setup)
    serialized = str(answer)
    for leaked in setup.leaks():
        assert leaked not in serialized, leaked


def test_report_tool_refuses_unnamed_when_the_embedding_model_cannot_load(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    """``unnamed`` refuses before writing its digest, history or audit entry."""
    caplog.set_level(logging.WARNING, logger=_REPORT_LOGGER)
    unnamed = setup.vault.joinpath(*_UNNAMED_DIR)
    for index in range(2):
        _write_note(unnamed, f"u{index}", f"a shape with no name yet, {index}")

    result = report_tool(
        vault_path=setup.vault,
        report_type="unnamed",
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert setup.loads, "unnamed never reached the embedding model"
    _assert_fixed_refusal(result, setup)
    assert not (unnamed / "Digests").exists()
    assert not setup.vault.joinpath(*_UNNAMED_HISTORY).exists()
    assert "creek.report" not in _audited_tools(setup.vault)
    _assert_one_operator_record(
        caplog, _REPORT_LOGGER, "EmbeddingModelUnavailableError"
    )


@pytest.mark.parametrize("report_type", ["paradox", "synchronicity"])
def test_cache_only_reports_never_load_the_embedding_model(
    setup: _Setup, report_type: str
) -> None:
    """``paradox`` and ``synchronicity`` read the parquet cache and nothing else.

    With a loader that would fail, both still succeed and the loader is never
    asked for a model, which is why neither needs the refusal ``unnamed`` does.
    """
    result = report_tool(
        vault_path=setup.vault,
        report_type=report_type,
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert result["status"] == "ok"
    assert setup.loads == []
