"""Static check: every Ollama dial goes through the loopback chokepoint (#1849).

:func:`creek.classify.llm.local_boundary.ollama_endpoint` is the only place an
``ollama_url`` may be turned into a request URL, so the container-mode
loopback refusal cannot be bypassed by a new raw f-string dial. A grep is the
whole check, so it stays fast and obvious when it fires.
"""

from __future__ import annotations

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
