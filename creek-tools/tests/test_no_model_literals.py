"""Static check: no LLM model-ID literals live outside the provider module.

Model IDs move, so the per-provider ``DEFAULT_MODELS`` matrix in
:mod:`creek.classify.llm.providers` is the contract. Other modules must read
the resolved value (or accept a model argument), never reference the literal
string — for Claude, OpenAI ``gpt-*``, or Gemini ``gemini-*``. Mirrors
CrawDad's ``test_no_model_literals.py`` so both packages enforce the same
one-place-edit rule.

The check is a simple grep so it remains fast and trivially debuggable
when it fires.
"""

from __future__ import annotations

import re
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "creek"
_ALLOWED_FILE = _PACKAGE_ROOT / "classify" / "llm" / "providers.py"
_MODEL_PATTERN = re.compile(
    r"claude-(?:haiku|sonnet|opus|fable)-\d|gpt-\d|gemini-\d", re.IGNORECASE
)


def test_no_model_id_literals_outside_providers() -> None:
    """No ``creek/*.py`` (except the provider module) contains a model ID."""
    offenders: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        if path == _ALLOWED_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        match = _MODEL_PATTERN.search(text)
        if match:
            offenders.append(f"{path.relative_to(_PACKAGE_ROOT)}: {match.group(0)!r}")
    assert not offenders, (
        "model-ID literals found outside the provider module: "
        + ", ".join(offenders)
        + " — read DEFAULT_MODELS from creek.classify.llm.providers instead."
    )
