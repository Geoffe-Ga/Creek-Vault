"""Lazily exported MCP tool implementations.

Importing one tool submodule must not import all of its siblings. The HTTP API
uses the same behavioural functions but only a subset of the MCP package; eager
re-exports made its base installation import the optional author/NumPy stack
before the API could even bind (#1772). Attribute access still exposes the
historical package API used by :mod:`creek_mcp.server`.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Final

_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "author_tool": ("creek_mcp.tools.author", "author_tool"),
    "classify_tool": ("creek_mcp.tools.classify", "classify_tool"),
    "compile_tool": ("creek_mcp.tools.compile", "compile_tool"),
    "draft_tool": ("creek_mcp.tools.draft", "draft_tool"),
    "entry_classification_tool": (
        "creek_mcp.tools.classify_entry",
        "entry_classification_tool",
    ),
    "handshake_tool": ("creek_mcp.tools.handshake", "handshake_tool"),
    "ingest_tool": ("creek_mcp.tools.ingest", "ingest_tool"),
    "journal_ingest_tool": ("creek_mcp.tools.journal", "journal_ingest_tool"),
    "link_tool": ("creek_mcp.tools.link", "link_tool"),
    "lint_tool": ("creek_mcp.tools.lint", "lint_tool"),
    "mine_tool": ("creek_mcp.tools.mine", "mine_tool"),
    "purge_classifications_tool": (
        "creek_mcp.tools.purge",
        "purge_classifications_tool",
    ),
    "purge_daterange_tool": (
        "creek_mcp.tools.purge",
        "purge_daterange_tool",
    ),
    "purge_fragment_tool": ("creek_mcp.tools.purge", "purge_fragment_tool"),
    "purge_source_tool": ("creek_mcp.tools.purge", "purge_source_tool"),
    "purge_vault_tool": ("creek_mcp.tools.purge", "purge_vault_tool"),
    "redact_scan_tool": ("creek_mcp.tools.redact", "redact_scan_tool"),
    "reflect_tool": ("creek_mcp.tools.reflect", "reflect_tool"),
    "report_tool": ("creek_mcp.tools.report", "report_tool"),
    "save_tool": ("creek_mcp.tools.save", "save_tool"),
    "skills_refresh_tool": ("creek_mcp.tools.skills", "skills_refresh_tool"),
    "state_read_tool": ("creek_mcp.tools.state_read", "state_read_tool"),
    "state_render_tool": ("creek_mcp.tools.state", "state_render_tool"),
    "upload_tool": ("creek_mcp.tools.upload", "upload_tool"),
    "wheel_tool": ("creek_mcp.tools.wheel", "wheel_tool"),
}
"""Public attribute to source-module mappings for :func:`__getattr__`."""

# PEP 526 annotations make the historical names visible to whole-program
# static analyzers without creating runtime bindings. Attribute lookup still
# reaches ``__getattr__`` and imports only the one requested submodule; the
# adjacent stub supplies each export's precise signature to type checkers.
author_tool: Any
classify_tool: Any
compile_tool: Any
draft_tool: Any
entry_classification_tool: Any
handshake_tool: Any
ingest_tool: Any
journal_ingest_tool: Any
link_tool: Any
lint_tool: Any
mine_tool: Any
purge_classifications_tool: Any
purge_daterange_tool: Any
purge_fragment_tool: Any
purge_source_tool: Any
purge_vault_tool: Any
redact_scan_tool: Any
reflect_tool: Any
report_tool: Any
save_tool: Any
skills_refresh_tool: Any
state_read_tool: Any
state_render_tool: Any
upload_tool: Any
wheel_tool: Any

__all__: list[str] = []
__all__.extend(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Import and cache one historical tool export on first access.

    Args:
        name: Package attribute requested by an importer.

    Returns:
        The tool callable exported under *name*.

    Raises:
        AttributeError: When *name* is not part of the package API.
    """
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
