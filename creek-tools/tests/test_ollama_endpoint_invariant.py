"""Static check: every Ollama dial goes through the loopback chokepoint (#1849).

:func:`creek.classify.llm.local_boundary.ollama_endpoint` is the only place an
``ollama_url`` may be turned into a request URL, so the container-mode
loopback refusal cannot be bypassed by a new raw f-string dial. A grep is the
whole check, so it stays fast and obvious when it fires.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = (_ROOT / "creek", _ROOT / "creek_mcp")
_ALLOWED = _ROOT / "creek" / "classify" / "llm" / "local_boundary.py"
_RAW_DIAL = re.compile(r"ollama_url\s*}")


def test_no_raw_ollama_url_interpolation() -> None:
    """Only the boundary module may interpolate ``ollama_url`` into a URL."""
    offenders = [
        f"{path.relative_to(_ROOT)}:{number}"
        for package in _SCANNED
        for path in sorted(package.rglob("*.py"))
        if path != _ALLOWED
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _RAW_DIAL.search(line)
    ]

    assert not offenders, (
        "raw ollama_url interpolation bypasses the loopback boundary: "
        + ", ".join(offenders)
        + " — build the URL with local_boundary.ollama_endpoint instead."
    )


def test_the_boundary_module_is_the_one_interpolation_site() -> None:
    """Guard the guard: the allowed file must exist and hold the dial."""
    assert _RAW_DIAL.search(_ALLOWED.read_text(encoding="utf-8"))


_DIAL_HELPERS = frozenset({"ollama_endpoint"})
_HTTPX_DIALS = frozenset(
    {"Client", "AsyncClient", "get", "post", "put", "request", "stream"}
)


def _module_calls(path: Path) -> list[ast.Call]:
    """Return every call expression in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _called_name(call: ast.Call) -> str | None:
    """Return the bare or attribute name a call invokes, if it has one."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_httpx_dial(call: ast.Call) -> bool:
    """Return whether *call* is ``httpx.<Client|get|post|...>(...)``."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "httpx"
        and func.attr in _HTTPX_DIALS
    )


def test_ollama_urls_are_only_dialled_inside_the_boundary() -> None:
    """No module outside the boundary may build an Ollama URL to dial itself.

    Callers use ``ollama_get`` / ``ollama_post``, so the URL check and the
    proxy-free transport cannot be separated.
    """
    offenders = [
        f"{path.relative_to(_ROOT)}:{call.lineno}"
        for package in _SCANNED
        for path in sorted(package.rglob("*.py"))
        if path != _ALLOWED
        for call in _module_calls(path)
        if _called_name(call) in _DIAL_HELPERS
    ]

    assert not offenders, (
        "Ollama URL built outside the boundary: "
        + ", ".join(offenders)
        + " — dial with local_boundary.ollama_get / ollama_post instead."
    )


def test_every_boundary_client_ignores_environment_proxies() -> None:
    """Each httpx dial in the boundary passes ``trust_env=False``.

    Under ``trust_env=True`` httpx routes even a loopback URL through
    ``HTTP_PROXY`` / ``ALL_PROXY``, so the provider labelled local would send
    its prompt to the proxy host.
    """
    dials = [call for call in _module_calls(_ALLOWED) if _is_httpx_dial(call)]
    trusting = [
        call.lineno
        for call in dials
        if not any(
            keyword.arg == "trust_env"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
    ]

    assert dials, "the boundary no longer constructs its own httpx client"
    assert not trusting, f"boundary httpx dials honour proxy env at {trusting}"
