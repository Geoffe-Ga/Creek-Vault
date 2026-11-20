"""``/v1`` end to end: classify fills ``04-Praxis``, reflections publish it (#2122).

Before #2122, ``ReflectionResponse.related_praxis`` was empty for every vault:
:mod:`creek_mcp.compiled_pages` reads ``04-Praxis/`` pages, and no code path
ever wrote one. This drives the whole seam a ``/v1`` client sees, over the real
ASGI app and a vault built by the ``creek init`` scaffold:

1. a fragment that writes a practice down, tiered ``personal`` the way
   ``POST /v1/uploads`` tiers one;
2. ``POST /v1/classifications`` (``rules``) — the praxis pass marks it
   ``explicit`` and, in the same locked pass, writes the ``04-Praxis/Daily``
   page;
3. ``POST /v1/reflections`` on that fragment — and ``related_praxis`` now
   names the page.

The model is stubbed: no network, no live provider.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from starlette.testclient import TestClient

from tests.v1_api_support import (
    CLASSIFICATIONS_PATH,
    REFLECTIONS_PATH,
    build_app,
    headers,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.models import PrivacyTier

pytestmark = [pytest.mark.e2e]

_HTTP_OK: Final[int] = 200

_FRAGMENT_ID: Final[str] = "frag-2122-v1-sit"

_BODY: Final[str] = (
    "From now on, I'm committing to a 10-minute morning sit.\n"
    "I will keep the cushion by the window."
)
"""Explicit practice language: two distinct sentence-anchored commitments."""

_QUOTE: Final[str] = "I'm committing to a 10-minute morning sit"
"""A verbatim span of :data:`_BODY`, so the stub's note survives the quote gate."""

_PRAXIS_TITLE: Final[str] = "From now on, I'm committing to a 10-minute morning sit."
"""The page title: the fragment's first sentence."""

_CEILING: Final[str] = "personal"


def _stub_factory() -> Callable[..., Callable[[str], str]]:
    """Return an LLM factory whose completion is one well-formed note."""
    turn = json.dumps(
        {"notes": [{"quote": _QUOTE, "kind": "pattern", "note": "A daily sit."}]}
    )

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        _ = (tier, max_tokens)
        return lambda _prompt: turn

    return _factory


def _seed(vault: Path) -> None:
    """Write the practice fragment, tiered and unclassified, into *vault*."""
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{_FRAGMENT_ID}.md").write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content=_BODY,
                type="fragment",
                id=_FRAGMENT_ID,
                title="Cushion notes",
                source={"platform": "markdown", "author": "self"},
                privacy_tier=_CEILING,
            )
        ),
        encoding="utf-8",
    )


def _reflect(api: TestClient) -> dict[str, Any]:
    """Reflect on the seeded fragment and return the decoded payload."""
    response = api.post(
        REFLECTIONS_PATH,
        json={"entry_ref": _FRAGMENT_ID},
        headers=headers(ceiling=_CEILING),
    )
    assert response.status_code == _HTTP_OK, response.text
    payload: dict[str, Any] = response.json()
    return payload


def test_v1_classification_fills_praxis_and_reflections_return_it(
    synthetic_vault: Path,
) -> None:
    """Classify over ``/v1``, then ``related_praxis`` names the written page."""
    _seed(synthetic_vault)
    app = build_app(
        vault_path=synthetic_vault,
        reflect_llm_factory=lambda: _stub_factory(),
    )
    with TestClient(app) as api:
        # Control: nothing to publish before the pass runs.
        assert "related_praxis" not in _reflect(api)

        classified = api.post(
            CLASSIFICATIONS_PATH,
            json={"method": "rules"},
            headers=headers(ceiling=_CEILING),
        )
        assert classified.status_code == _HTTP_OK, classified.text
        assert classified.json()["complete"] is True

        pages = sorted((synthetic_vault / "04-Praxis" / "Daily").glob("*.md"))
        assert len(pages) == 1
        meta = frontmatter.load(str(pages[0])).metadata
        assert meta["derived_from"] == [_FRAGMENT_ID]
        assert meta["privacy_tier"] == _CEILING

        related = _reflect(api)["related_praxis"]

    assert [row["title"] for row in related] == [_PRAXIS_TITLE]
    assert related[0]["praxis_type"] == "habit"
    assert related[0]["status"] == "proposed"
