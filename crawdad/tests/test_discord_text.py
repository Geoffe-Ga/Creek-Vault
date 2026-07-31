"""Tests for the shared Discord text contracts."""

from __future__ import annotations

import ast
from pathlib import Path

from crawdad.discord_text import (
    _DISCORD_REPLY_LIMIT,
    _MCP_UNAVAILABLE_REPLY,
    _truncate_for_discord,
)


def test_truncation_keeps_the_existing_wire_contract() -> None:
    """The shared helper preserves the old cap and marker."""
    assert (
        _MCP_UNAVAILABLE_REPLY == "creek-tools is unreachable; try again in a moment."
    )
    assert _truncate_for_discord("ok") == "ok"
    reply = _truncate_for_discord("x" * (_DISCORD_REPLY_LIMIT + 1))
    assert len(reply) == _DISCORD_REPLY_LIMIT
    assert reply.endswith("...")


def test_shared_symbols_have_one_definition() -> None:
    """A second copy in a production module must fail this guard."""
    source_root = Path(__file__).parents[1] / "crawdad"
    definitions = {
        "_DISCORD_REPLY_LIMIT": 0,
        "_MCP_UNAVAILABLE_REPLY": 0,
        "_truncate_for_discord": 0,
    }
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else [node]
                )
                for target in targets:
                    name = (
                        target.id
                        if isinstance(target, ast.Name)
                        else target.name
                        if isinstance(target, ast.FunctionDef)
                        else None
                    )
                    if name in definitions:
                        definitions[name] += 1
    assert definitions == {
        "_DISCORD_REPLY_LIMIT": 1,
        "_MCP_UNAVAILABLE_REPLY": 1,
        "_truncate_for_discord": 1,
    }
