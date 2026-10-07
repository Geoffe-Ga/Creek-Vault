"""Every string the report can hold is closed by construction.

Rather than enumerate today's fields, this walks the report's generated JSON
schema: every string-typed node must be an enum, a constant, a space-free
pattern, or a date. A future free-text field anywhere in the tree — a
``prompt: str`` on a summary, a license text on the metadata — fails here by
name, before any test has to think of the leak it would cause.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from creek_mcp.bench.cost import CostEstimate, PriceSheet
from creek_mcp.bench.report import BenchReport

_CLOSED_FORMATS = {"date", "date-time"}


def _string_leaves(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    """Return every schema node of ``type: string`` with its JSON path."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if node.get("type") == "string":
            found.append((path, node))
        for key, value in node.items():
            found.extend(_string_leaves(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_string_leaves(value, f"{path}[{index}]"))
    return found


def _is_closed(node: dict[str, Any]) -> bool:
    """Return whether a string schema node admits only closed values."""
    if "enum" in node or "const" in node:
        return True
    if node.get("format") in _CLOSED_FORMATS:
        return True
    pattern = node.get("pattern")
    return isinstance(pattern, str) and " " not in pattern and "\\s" not in pattern


def _open_strings(model: type[BaseModel]) -> list[str]:
    """Return the paths of every open string field in *model*'s schema."""
    schema = model.model_json_schema()
    return [path for path, node in _string_leaves(schema) if not _is_closed(node)]


def test_every_string_leaf_is_closed() -> None:
    """No free-text field exists anywhere in the benchmark report."""
    leaves = _string_leaves(BenchReport.model_json_schema())
    assert leaves, "the walker found no string fields at all"
    assert _open_strings(BenchReport) == []


def test_cost_estimate_strings_are_closed() -> None:
    """The cost estimate is shareable evidence too, and equally closed."""
    assert _open_strings(CostEstimate) == []


def _models(model: type[BaseModel]) -> set[type[BaseModel]]:
    """Return *model* and every model nested in its fields."""
    seen: set[type[BaseModel]] = set()
    pending = [model]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for field in current.model_fields.values():
            pending.extend(_nested(field.annotation))
    return seen


def _nested(annotation: Any) -> list[type[BaseModel]]:
    """Return the model classes reachable inside a field annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [
        model for arg in getattr(annotation, "__args__", ()) for model in _nested(arg)
    ]


def test_extra_forbidden_and_frozen_everywhere() -> None:
    """Every model in the report and cost trees is closed and immutable."""
    models = _models(BenchReport) | _models(CostEstimate) | _models(PriceSheet)
    assert len(models) >= 6
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert model.model_config.get("frozen") is True, model.__name__
