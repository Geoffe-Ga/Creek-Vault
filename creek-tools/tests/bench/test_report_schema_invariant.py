"""Every string the report can hold is closed by construction.

Rather than enumerate today's fields, this walks the report's generated JSON
schema: every string-typed node must be an enum, a constant, a space-free
pattern, or a date. A future free-text field anywhere in the tree — a
``prompt: str`` on a summary, a license text on the metadata — fails here by
name, before any test has to think of the leak it would cause.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from creek_mcp.bench.cost import CostEstimate, PriceSheet
from creek_mcp.bench.report import BenchReport

_CLOSED_FORMATS = {"date", "date-time"}
_FREE_TEXT_PROBES = (
    "the quick brown fox jumps over the lazy dog",
    "zq-canary-7f3e leaked into a field",
    "I keep circling the same fear.",
)
"""Free text a closed pattern must not match anywhere (search semantics).

A behavioural test rather than a syntactic one: ``^.*$``, ``^[^\\n]*$`` and an
unanchored ``[a-z]+`` contain no literal space yet admit whole sentences.
"""


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
    if not isinstance(pattern, str):
        return False
    return not any(re.search(pattern, probe) for probe in _FREE_TEXT_PROBES)


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


class _OpenDotStar(BaseModel):
    """A throwaway model with a pattern that admits any line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: Annotated[str, Field(pattern=r"^.*$")]


class _OpenNegatedClass(BaseModel):
    """A throwaway model whose pattern excludes only newlines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: Annotated[str, Field(pattern=r"^[^\n]*$")]


class _OpenUnanchored(BaseModel):
    """A throwaway model whose pattern is unanchored, so it matches inside text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: Annotated[str, Field(pattern=r"[a-z]+")]


class _Closed(BaseModel):
    """A throwaway model with a genuinely closed identifier pattern."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ident: Annotated[str, Field(pattern=r"^[a-f0-9]{8}$")]


@pytest.mark.parametrize("model", [_OpenDotStar, _OpenNegatedClass, _OpenUnanchored])
def test_guard_flags_patterns_that_admit_free_text(model: type[BaseModel]) -> None:
    """A pattern with no literal space can still admit whole sentences."""
    assert _open_strings(model) != []


def test_guard_accepts_a_closed_identifier_pattern() -> None:
    """Control: a fixed-alphabet anchored pattern is closed."""
    assert _open_strings(_Closed) == []
