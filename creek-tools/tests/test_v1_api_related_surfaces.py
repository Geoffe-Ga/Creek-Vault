"""Threads on reflections; praxis, eddies and threads on letters (#2127).

``POST /v1/reflections`` gains ``related_threads`` beside the 0.9 fields, and
``POST /v1/letters/{external_id}`` gains all three. Every one is chosen by
:func:`creek_mcp.compiled_pages.related_compiled`: a page is named only when
the entry's own seed fragments belong to it (never padded), only when every
fragment it was compiled from is within the caller's ceiling, and only as its
title and a few labels — never its body. Each key is omitted when nothing
qualifies and for a caller declaring a minor below
:data:`~creek_mcp.api.models.RELATED_SURFACES_SINCE_MINOR`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from starlette.testclient import TestClient

from creek.models import PrivacyTier
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    RELATED_SURFACES_SINCE_MINOR,
    LetterResponse,
    ReflectionResponse,
)
from creek_mcp.compiled_pages import RelatedCompiled
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.reflect import related_for_content
from tests.test_api_reflect import _GOOD_TURN, _FactorySpy
from tests.v1_api_support import build_app, envelope, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

_PRIOR_MINOR: Final[str] = "0.35"
"""The minor immediately below the one that serves the #2127 fields.

Spelled rather than derived, so the boundary is asserted rather than moved.
"""
_ENTRY_REF: Final[str] = "frag-2127wire0001"
_OTHER_REF: Final[str] = "frag-2127wire0002"
_ENTRY: Final[str] = "I keep saying yes to things I do not want, and the firm knows it."
_THREAD: Final[str] = "Leaving the firm"
_THREAD_ID: Final[str] = "thread-2127wire01"
_EDDY: Final[str] = "Rest and Ruin"
_PRAXIS: Final[str] = "Rest before the collapse"
_BODY_MARKER: Final[str] = "zz-compiled-body-2127-zz"
_LETTER_ID: Final[str] = "adepthood-voicedraft-2127aa"
_LETTER: Final[str] = "zz-letter-2127-zz you already know the way out."
_THREAD_ROW: Final[dict[str, str]] = {
    "id": _THREAD_ID,
    "title": _THREAD,
    "status": "active",
}


def _fragment(vault: Path, frag_id: str, tier: str) -> None:
    """Write one fragment linked to the thread and the eddy."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content=_ENTRY,
                type="fragment",
                id=frag_id,
                title="A fragment",
                source={"platform": "journal", "author": "self"},
                privacy_tier=tier,
                threads=[f"[[{_THREAD}]]"],
                eddies=[f"[[{_EDDY}]]"],
            )
        ),
        encoding="utf-8",
    )


def _compiled_layer(vault: Path, *, member_tier: str = "open") -> None:
    """Seed a thread, an eddy and a praxis compiled from two fragments.

    The first fragment is always ``open``; *member_tier* is the second's, so
    it is the only thing that varies between the admitted and withheld cases.
    """
    _fragment(vault, _ENTRY_REF, "open")
    _fragment(vault, _OTHER_REF, member_tier)
    threads = vault / "02-Threads" / "Active"
    threads.mkdir(parents=True, exist_ok=True)
    (threads / "thread.md").write_text(
        f"---\ntype: thread\nid: {_THREAD_ID}\ntitle: {_THREAD}\nstatus: active\n"
        f"fragment_count: 2\n---\n\n{_BODY_MARKER} thread body.\n",
        encoding="utf-8",
    )
    eddies = vault / "03-Eddies"
    eddies.mkdir(parents=True, exist_ok=True)
    (eddies / "eddy.md").write_text(
        f"---\ntype: eddy\nid: eddy-2127000001\ntitle: {_EDDY}\n"
        "formed: 2026-03-04\nfragment_count: 2\n"
        f"description: Where rest and ruin meet.\n---\n\n{_BODY_MARKER} eddy.\n",
        encoding="utf-8",
    )
    praxis = vault / "04-Praxis" / "Daily"
    praxis.mkdir(parents=True, exist_ok=True)
    (praxis / "praxis.md").write_text(
        f"---\ntype: praxis\nid: prax-2127000001\ntitle: {_PRAXIS}\n"
        "praxis_type: practice\nstatus: active\n"
        f"derived_from:\n  - {_ENTRY_REF}\n  - {_OTHER_REF}\n---\n\nRest first.\n",
        encoding="utf-8",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a freshly seeded vault."""
    return seed_vault(tmp_path / "vault")


# --------------------------------------------------------------------------- #
# Reflections: related_threads
# --------------------------------------------------------------------------- #


def _reflect(
    vault: Path, *, ceiling: str = "open", minor: str = CONTRACT_MINOR
) -> httpx.Response:
    """Reflect on the ``open`` fragment through the real adapter."""
    spy = _FactorySpy(_GOOD_TURN)
    app = build_app(vault_path=vault, reflect_llm_factory=lambda: spy)
    with TestClient(app) as client:
        return client.post(
            "/v1/reflections",
            json={"entry_ref": _ENTRY_REF},
            headers=headers(ceiling=ceiling, minor=minor),
        )


def test_a_touched_thread_reaches_a_reflection(vault: Path) -> None:
    """The control: the thread the entry belongs to is named."""
    _compiled_layer(vault)

    response = _reflect(vault)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["related_threads"] == [_THREAD_ROW]
    assert ReflectionResponse.model_validate(payload).related_threads is not None
    assert _BODY_MARKER not in response.text


@pytest.mark.parametrize(
    ("ceiling", "member_tier"),
    [("open", "personal"), ("open", "intimate"), ("personal", "intimate")],
)
def test_a_thread_above_the_ceiling_is_never_named(
    vault: Path, ceiling: str, member_tier: str
) -> None:
    """One above-ceiling member withholds the thread's title from the wire."""
    _compiled_layer(vault, member_tier=member_tier)

    response = _reflect(vault, ceiling=ceiling)

    assert response.status_code == 200, response.text
    assert "related_threads" not in response.json()
    assert _THREAD not in response.text


def test_a_reflection_touching_nothing_carries_no_thread(vault: Path) -> None:
    """No compiled page the entry belongs to: the key is absent, not padded."""
    _fragment(vault, _ENTRY_REF, "open")

    payload = _reflect(vault).json()

    for key in ("related_threads", "related_eddies", "related_praxis"):
        assert key not in payload


def test_a_prior_minor_is_not_served_related_threads(vault: Path) -> None:
    """A 0.35 consumer validates a closed schema without the key."""
    _compiled_layer(vault)

    payload = _reflect(vault, minor=_PRIOR_MINOR).json()

    assert "related_threads" not in payload
    assert [row["title"] for row in payload["related_eddies"]] == [_EDDY]
    assert RELATED_SURFACES_SINCE_MINOR == "0.36"


# --------------------------------------------------------------------------- #
# Letters: related_praxis, related_eddies, related_threads
# --------------------------------------------------------------------------- #


class _LetterSpy:
    """A tier-keyed letter factory recording every prompt."""

    def __init__(self) -> None:
        """Start with no prompts."""
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        """Return a completion that records its prompt."""
        del tier, max_tokens

        def _complete(prompt: str) -> str:
            self.prompts.append(prompt)
            return _LETTER

        return _complete


def _letter(
    vault: Path,
    *,
    ceiling: str = "open",
    minor: str = CONTRACT_MINOR,
    spy: _LetterSpy | None = None,
    lookup: Callable[..., RelatedCompiled] | None = None,
) -> httpx.Response:
    """POST one letter through the real adapter."""
    factory = spy or _LetterSpy()
    body: dict[str, Any] = {
        "content": _ENTRY,
        "note": {"kind": "pattern", "anchor_text": "saying yes", "note": "Notice."},
        "tier": "open",
    }
    app = build_app(vault_path=vault, letter_llm_factory=lambda: factory)
    if lookup is not None:
        app.state.letter_related_lookup = lookup
    with TestClient(app) as client:
        return client.post(
            f"/v1/letters/{_LETTER_ID}",
            json=body,
            headers=headers(ceiling=ceiling, minor=minor),
        )


def test_a_letter_names_the_praxis_eddies_and_threads_its_entry_touches(
    vault: Path,
) -> None:
    """The real path: the entry's nearest fragments seed all three surfaces."""
    _compiled_layer(vault)
    spy = _LetterSpy()

    response = _letter(vault, spy=spy)

    assert response.status_code == 200, response.text
    payload = envelope(response)
    parsed = LetterResponse.model_validate(payload)
    assert parsed.letter == _LETTER
    assert payload["related_threads"] == [_THREAD_ROW]
    assert [row["title"] for row in payload["related_eddies"]] == [_EDDY]
    assert [row["title"] for row in payload["related_praxis"]] == [_PRAXIS]
    assert _BODY_MARKER not in response.text
    assert all(_BODY_MARKER not in prompt for prompt in spy.prompts)


def test_a_repeat_letter_carries_the_same_related_surfaces(vault: Path) -> None:
    """An ``unchanged`` letter is decorated from the same entry, no model call."""
    _compiled_layer(vault)
    spy = _LetterSpy()

    first = envelope(_letter(vault, spy=spy))
    again = envelope(_letter(vault, spy=spy))

    assert again["action"] == "unchanged"
    assert again["related_threads"] == first["related_threads"] == [_THREAD_ROW]
    assert len(spy.prompts) == 1


@pytest.mark.parametrize(
    ("ceiling", "member_tier"),
    [("open", "personal"), ("open", "intimate"), ("personal", "intimate")],
)
def test_a_letter_never_names_a_page_compiled_above_the_ceiling(
    vault: Path, ceiling: str, member_tier: str
) -> None:
    """Every surface is withheld whole when one contributor is above it."""
    _compiled_layer(vault, member_tier=member_tier)

    response = _letter(vault, ceiling=ceiling)

    assert response.status_code == 200, response.text
    payload = envelope(response)
    for key in ("related_threads", "related_eddies", "related_praxis"):
        assert key not in payload
    for title in (_THREAD, _EDDY, _PRAXIS):
        assert title not in response.text


def test_a_letter_whose_entry_touches_nothing_carries_no_related_keys(
    vault: Path,
) -> None:
    """No compiled page the entry belongs to: all three keys absent."""
    _fragment(vault, _ENTRY_REF, "open")

    payload = envelope(_letter(vault))

    for key in ("related_threads", "related_eddies", "related_praxis"):
        assert key not in payload


def test_a_prior_minor_letter_is_not_served_related_keys_nor_looked_up(
    vault: Path,
) -> None:
    """A 0.35 consumer gets its own shape, and no corpus walk is spent on it."""
    _compiled_layer(vault)
    calls: list[object] = []

    def _lookup(*args: object) -> RelatedCompiled:
        calls.append(args)
        return RelatedCompiled([], [], [_THREAD_ROW])

    payload = envelope(_letter(vault, minor=_PRIOR_MINOR, lookup=_lookup))

    for key in ("related_threads", "related_eddies", "related_praxis"):
        assert key not in payload
    assert calls == []


def test_the_letter_lookup_runs_at_the_callers_ceiling_over_the_entry(
    vault: Path,
) -> None:
    """The seam receives the vault, the entry, the ceiling and the entry tier."""
    calls: list[tuple[object, ...]] = []

    def _lookup(*args: object) -> RelatedCompiled:
        calls.append(args)
        return RelatedCompiled([], [], [_THREAD_ROW])

    payload = envelope(_letter(vault, ceiling="personal", lookup=_lookup))

    assert payload["related_threads"] == [_THREAD_ROW]
    assert calls == [(vault, _ENTRY, TierCeiling.PERSONAL, PrivacyTier.OPEN)]


def test_a_failing_letter_lookup_degrades_to_no_related_keys(vault: Path) -> None:
    """Enrichment never vetoes the letter it decorates."""

    def _lookup(*_args: object) -> RelatedCompiled:
        raise RuntimeError

    response = _letter(vault, lookup=_lookup)

    assert response.status_code == 200, response.text
    assert "related_threads" not in envelope(response)


def test_a_letter_refusal_runs_no_lookup(vault: Path) -> None:
    """An above-ceiling letter is refused before anything is looked up."""
    calls: list[object] = []

    def _lookup(*args: object) -> RelatedCompiled:
        calls.append(args)
        return RelatedCompiled([], [], [])

    app = build_app(vault_path=vault, letter_llm_factory=_LetterSpy)
    app.state.letter_related_lookup = _lookup
    body = {
        "content": _ENTRY,
        "note": {"kind": "pattern", "anchor_text": "saying yes", "note": "Notice."},
        "tier": "personal",
    }
    with TestClient(app) as client:
        response = client.post(
            f"/v1/letters/{_LETTER_ID}", json=body, headers=headers(ceiling="open")
        )

    assert response.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------- #
# The production lookup: the reflection seed gate, over raw entry text
# --------------------------------------------------------------------------- #


def test_related_for_content_selects_through_the_entrys_nearest_fragments(
    vault: Path,
) -> None:
    """The letter's default lookup is the reflection grounder's seed set."""
    _compiled_layer(vault)

    related = related_for_content(_ENTRY, vault, TierCeiling.OPEN, PrivacyTier.OPEN)

    assert related.threads == [_THREAD_ROW]
    assert [row["title"] for row in related.eddies] == [_EDDY]


def test_related_for_content_is_empty_when_no_route_may_ground(vault: Path) -> None:
    """A route decision that refuses grounding embeds nothing and seeds nothing."""
    _compiled_layer(vault)

    def _refuse(_tier: PrivacyTier) -> None:
        raise RuntimeError

    related = related_for_content(
        _ENTRY, vault, TierCeiling.OPEN, PrivacyTier.OPEN, grounding_route=_refuse
    )

    assert related == RelatedCompiled([], [], [])


def test_related_for_content_degrades_to_nothing_on_an_unreadable_vault(
    tmp_path: Path,
) -> None:
    """A vault that does not exist yields nothing rather than raising."""
    related = related_for_content(
        _ENTRY, tmp_path / "missing", TierCeiling.OPEN, PrivacyTier.OPEN
    )

    assert related == RelatedCompiled([], [], [])


def test_the_production_letter_lookup_grounds_through_the_reflection_route(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the production factory, the reflection's embedding route decides."""
    from creek_mcp import server
    from creek_mcp.httpapi import letters

    _compiled_layer(vault)
    decided: list[PrivacyTier] = []

    def _route(_vault: Path, _key: object) -> Callable[[PrivacyTier], None]:
        def _decide(tier: PrivacyTier) -> None:
            decided.append(tier)

        return _decide

    monkeypatch.setattr(server, "_build_reflect_grounding_route", _route)

    related = letters._default_related_lookup(
        vault,
        _ENTRY,
        TierCeiling.PERSONAL,
        PrivacyTier.OPEN,
        session=None,
        request_key=None,
        production=True,
    )

    assert decided == [PrivacyTier.PERSONAL]
    assert related.threads == [_THREAD_ROW]


# --------------------------------------------------------------------------- #
# Related surfaces are gated independently of the owner voice (#2126)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("owner_style", [True, False], ids=["owner", "higher_self"])
def test_a_letter_carries_related_surfaces_on_either_voice_path(
    vault: Path, owner_style: bool
) -> None:
    """At 0.36 the related keys ship whether or not the owner's voice applied."""
    from tests.test_v1_api_letter_voice import _owner_skill

    _compiled_layer(vault)
    if owner_style:
        _owner_skill(vault)

    payload = envelope(_letter(vault))

    assert payload["voice"] == ("owner" if owner_style else "higher_self")
    assert payload["related_threads"] == [_THREAD_ROW]
    assert [row["title"] for row in payload["related_praxis"]] == [_PRAXIS]


def test_a_voice_minor_caller_gets_the_owner_voice_but_no_related_keys(
    vault: Path,
) -> None:
    """0.35 reads ``voice`` (owner style applied) and is spared the 0.36 keys."""
    from tests.test_v1_api_letter_voice import _owner_skill

    _compiled_layer(vault)
    _owner_skill(vault)

    letter = envelope(_letter(vault, minor=_PRIOR_MINOR))
    reflection = _reflect(vault, minor=_PRIOR_MINOR).json()

    assert letter["voice"] == "owner"
    assert reflection["voice"] == "owner"
    for key in ("related_threads", "related_eddies", "related_praxis"):
        assert key not in letter
    assert "related_threads" not in reflection
