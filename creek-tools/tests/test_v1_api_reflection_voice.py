"""``POST /v1/reflections`` notes speak in the owner's voice, and say so (#2126).

The essay-free margin notes are built from the same owner style as letters
(:func:`creek.author.reply_voice.reply_style`). With owner evidence the prompt
carries it and the response says ``voice: "owner"``; without it the prompt is
unchanged and the response says ``voice: "higher_self"``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from starlette.testclient import TestClient

from creek.classify.llm.pool_boundary import POOL_PROMPT_TOKEN_CAP
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import reflect as reflect_module
from creek_mcp.tools.reflect import reflect_tool
from tests.test_reply_voice import OWNER_STYLE
from tests.v1_api_support import (
    CONTRACT_MINOR,
    REFLECTIONS_PATH,
    build_app,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from creek.models import PrivacyTier

_ENTRY: Final[str] = (
    "zz-reflect-2126-zz I keep saying yes to things I do not want, and then "
    "resenting the person who asked."
)
_VERBATIM: Final[str] = "I keep saying yes to things I do not want"
_TURN: Final[str] = json.dumps(
    {"notes": [{"quote": _VERBATIM, "kind": "pattern", "note": "you notice it."}]}
)
_PRIOR_VOICE_MINOR: Final[str] = "0.34"
_DEFAULT_MAX_NOTES: Final[int] = 3
"""``ReflectionRequest.max_notes``'s default, which ``_post`` leaves unset."""
_OWNER_INSTRUCTIONS: Final[tuple[str, ...]] = (
    "Write in lowercase only.",
    "Never use em-dashes.",
    "Keep every sentence short.",
)
_CHARS_PER_TOKEN_OVERSHOOT: Final[int] = 8
"""Characters per cap token: comfortably over any token estimate."""


class _Spy:
    """A tier-keyed reflection factory recording every prompt."""

    def __init__(self) -> None:
        """Start with no prompts."""
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        """Return a completion that records its prompt and answers one note."""
        del tier, max_tokens

        def _complete(prompt: str) -> str:
            self.prompts.append(prompt)
            return _TURN

        return _complete


def _owner_skill(vault: Path, body: str = OWNER_STYLE) -> None:
    """Install an owner-authored voice-core skill."""
    path = vault / "creek-skills" / "meta" / "voice-core.SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _post(vault: Path, spy: _Spy, *, minor: str = CONTRACT_MINOR) -> httpx.Response:
    """POST one inline reflection through the real adapter."""
    app = build_app(vault_path=vault, reflect_llm_factory=lambda: spy)
    with TestClient(app) as client:
        return client.post(
            REFLECTIONS_PATH,
            json={"content": _ENTRY},
            headers=headers(ceiling="personal", minor=minor),
        )


def test_owner_style_reaches_the_reflection_prompt_and_voice_is_owner(
    tmp_path: Path,
) -> None:
    """The owner's instructions reach the provider, after the care policy."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    spy = _Spy()

    response = _post(vault, spy)

    assert response.status_code == 200, response.text
    body = envelope(response)
    assert body["voice"] == "owner"
    assert [note["quote"] for note in body["notes"]] == [_VERBATIM]
    prompt = spy.prompts[0]
    assert "in the writer's own style, as described below" in prompt
    for instruction in _OWNER_INSTRUCTIONS:
        assert instruction in prompt
    assert prompt.index("CARE BOUNDARY") < prompt.index(_OWNER_INSTRUCTIONS[0])
    assert prompt.index(_OWNER_INSTRUCTIONS[0]) < prompt.index("ENTRY:")


def test_without_owner_evidence_the_reflection_is_the_higher_self(
    tmp_path: Path,
) -> None:
    """Never a refusal: the notes come back, and say whose voice they are in."""
    vault = seed_vault(tmp_path / "vault")
    spy = _Spy()

    body = envelope(_post(vault, spy))

    assert body["voice"] == "higher_self"
    assert "own style" not in spy.prompts[0]


def test_a_prior_minor_is_not_served_the_voice_field(tmp_path: Path) -> None:
    """Closed response schemas: a 0.34 consumer must not see an unknown key."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)

    response = _post(vault, _Spy(), minor=_PRIOR_VOICE_MINOR)

    assert response.status_code == 200, response.text
    assert "voice" not in envelope(response)


def test_a_prior_minor_gets_the_pre_2126_prompt_even_with_owner_evidence(
    tmp_path: Path,
) -> None:
    """Below ``VOICE_SINCE_MINOR`` the provider prompt carries no owner style."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    spy = _Spy()

    response = _post(vault, spy, minor=_PRIOR_VOICE_MINOR)

    assert response.status_code == 200, response.text
    assert spy.prompts == [
        reflect_module._build_prompt(_ENTRY, [], max_notes=_DEFAULT_MAX_NOTES)
    ]
    for instruction in _OWNER_INSTRUCTIONS:
        assert instruction not in spy.prompts[0]


def test_an_owner_style_too_large_for_the_cap_falls_back_to_the_higher_self(
    tmp_path: Path,
) -> None:
    """Voice is dropped before the entry is refused: the entry always wins."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault, "x " * (POOL_PROMPT_TOKEN_CAP * _CHARS_PER_TOKEN_OVERSHOOT))
    spy = _Spy()

    response = _post(vault, spy)

    assert response.status_code == 200, response.text
    assert envelope(response)["voice"] == "higher_self"
    assert len(spy.prompts) == 1
    assert "own style" not in spy.prompts[0]


def test_the_mcp_tool_reports_the_voice_too(tmp_path: Path) -> None:
    """``creek.reflect`` and ``/v1`` share one result, ``voice`` included."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)

    result = reflect_tool(
        vault_path=vault,
        llm_factory=_Spy(),
        content=_ENTRY,
        retrieve=lambda _q, _v, _o: [],
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "ok"
    assert result["voice"] == "owner"
