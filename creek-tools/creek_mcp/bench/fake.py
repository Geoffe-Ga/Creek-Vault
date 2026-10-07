"""The deterministic model behind ``--mode fake``.

It answers every prompt with one well-formed margin note whose quote is the
first words of the prompt's ENTRY section, so ``reflect_tool`` parses it,
verifies the quote as verbatim, and returns ``ok`` — the full reflect path
runs with no model, key or network. It is positively local
(``is_cloud = False``), so it passes :class:`LocalOnlyFactory` like a real
local runtime would.

Its latency is the harness's own overhead (corpus walk, prompt build, parse),
which is the useful floor a fake run measures.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from creek.models import PrivacyTier

_ENTRY_MARKER: Final[str] = "ENTRY:\n"
_QUOTE_WORDS: Final[int] = 3
_NOTE: Final[str] = "You keep returning to this image."


class FakeLLM:
    """A deterministic, local completion callable."""

    is_cloud: bool = False

    def __call__(self, prompt: str) -> str:
        """Return one note quoting the start of the prompt's entry."""
        entry = prompt.rsplit(_ENTRY_MARKER, 1)[-1]
        quote = " ".join(entry.split()[:_QUOTE_WORDS])
        return json.dumps(
            {"notes": [{"quote": quote, "kind": "pattern", "note": _NOTE}]}
        )


def fake_factory(tier: PrivacyTier, *, max_tokens: int) -> FakeLLM:
    """Build a :class:`FakeLLM`; tier and budget do not change its answer."""
    del tier, max_tokens
    return FakeLLM()
