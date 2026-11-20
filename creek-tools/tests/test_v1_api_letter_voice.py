"""``POST /v1/letters`` writes in the owner's voice, and says when it does (#2126).

A letter is the owner's Higher Self expanding a margin note. When the vault
holds owner-authored style (skills, admitted register samples, the voice
fingerprint) the prompt carries it and the response says ``voice: "owner"``;
otherwise the prompt is unchanged and the response says
``voice: "higher_self"``. The field is served from
:data:`~creek_mcp.api.models.VOICE_SINCE_MINOR` and omitted below it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import frontmatter
from starlette.testclient import TestClient

from creek.care.guardrail import CARE_POLICY
from creek.models import PrivacyTier
from creek.reply_voice import HIGHER_SELF_STYLE, ReplyStyle, ReplyVoice
from creek.voice_drafts import (
    VoiceDraftDocument,
    create_voice_draft_if_absent,
    read_voice_draft,
)
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    VOICE_SINCE_MINOR,
    LetterResponse,
)
from creek_mcp.tools.letter import LetterSource, build_letter_prompt
from tests.test_reply_voice import OWNER_STYLE
from tests.v1_api_support import build_app, envelope, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

_EXTERNAL_ID: Final[str] = "adepthood-voicedraft-2126aa"
_ENTRY: Final[str] = "zz-entry-2126-zz I walked the long way home to hear the river."
_ANCHOR: Final[str] = "hear the river"
_NOTE: Final[str] = "zz-note-2126-zz You already know how to wait."
_LETTER: Final[str] = "zz-letter-2126-zz you already keep this practice."
_PRIOR_VOICE_MINOR: Final[str] = "0.34"
"""The minor immediately below the one that serves ``voice``.

Spelled rather than derived from :data:`VOICE_SINCE_MINOR`, so the boundary
is asserted rather than moved with it.
"""
_OWNER_INSTRUCTIONS: Final[tuple[str, ...]] = (
    "Write in lowercase only.",
    "Never use em-dashes.",
    "Keep every sentence short.",
)


class _Spy:
    """A tier-keyed letter factory recording every prompt."""

    def __init__(self) -> None:
        """Start with no prompts."""
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        """Return a completion that records its prompt and answers a letter."""
        del tier, max_tokens

        def _complete(prompt: str) -> str:
            self.prompts.append(prompt)
            return _LETTER

        return _complete


def _source() -> LetterSource:
    """Return one letter source."""
    return LetterSource(
        content=_ENTRY,
        note=_NOTE,
        anchor_text=_ANCHOR,
        kind="pattern",
        tier=PrivacyTier.PERSONAL,
        title=None,
    )


def _owner_skill(vault: Path) -> None:
    """Install the owner-authored voice-core skill the issue names."""
    path = vault / "creek-skills" / "meta" / "voice-core.SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OWNER_STYLE, encoding="utf-8")


def _post(
    vault: Path, spy: _Spy, *, minor: str = CONTRACT_MINOR, ceiling: str = "personal"
) -> httpx.Response:
    """POST one letter through the real adapter."""
    body: dict[str, Any] = {
        "content": _ENTRY,
        "note": {"kind": "pattern", "anchor_text": _ANCHOR, "note": _NOTE},
        "tier": "personal",
    }
    app = build_app(vault_path=vault, letter_llm_factory=lambda: spy)
    with TestClient(app) as client:
        return client.post(
            f"/v1/letters/{_EXTERNAL_ID}",
            json=body,
            headers=headers(ceiling=ceiling, minor=minor),
        )


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def test_the_higher_self_prompt_is_unchanged_without_owner_style() -> None:
    """No owner evidence: exactly today's prompt."""
    assert build_letter_prompt(_source()) == build_letter_prompt(
        _source(), HIGHER_SELF_STYLE
    )
    assert "owner's own style" not in build_letter_prompt(_source())


def test_the_owner_prompt_keeps_care_first_and_the_higher_self_framing() -> None:
    """Care policy, then the framing, then the owner's style, then the material."""
    style = ReplyStyle(ReplyVoice.OWNER, ("zz-style-zz " + OWNER_STYLE,))

    prompt = build_letter_prompt(_source(), style)

    assert prompt.startswith(CARE_POLICY)
    assert "their own Higher Self's voice" in prompt
    assert "in the owner's own style, as described below" in prompt
    assert (
        prompt.index("Higher Self's voice")
        < prompt.index("zz-style-zz")
        < prompt.index("<note>")
    )
    for instruction in _OWNER_INSTRUCTIONS:
        assert instruction in prompt


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def test_owner_style_reaches_the_provider_prompt_and_voice_is_owner(
    tmp_path: Path,
) -> None:
    """Lowercase, no em-dashes, short sentences: all reach the provider."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    spy = _Spy()

    response = _post(vault, spy)

    assert response.status_code == 200, response.text
    parsed = LetterResponse.model_validate(envelope(response))
    assert parsed.voice == "owner"
    assert len(spy.prompts) == 1
    for instruction in _OWNER_INSTRUCTIONS:
        assert instruction in spy.prompts[0]


def test_without_owner_evidence_the_letter_is_the_higher_self(
    tmp_path: Path,
) -> None:
    """Never a refusal: the letter is written, and says whose voice it is."""
    vault = seed_vault(tmp_path / "vault")
    spy = _Spy()

    response = _post(vault, spy)

    assert response.status_code == 200, response.text
    assert envelope(response)["voice"] == "higher_self"
    assert "owner's own style" not in spy.prompts[0]


def test_the_voice_is_stored_with_the_letter_and_returned_on_a_repeat(
    tmp_path: Path,
) -> None:
    """An unchanged letter reports the voice it was written in, with no dial."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    spy = _Spy()

    first = envelope(_post(vault, spy))
    (vault / "creek-skills" / "meta" / "voice-core.SKILL.md").unlink()
    again = envelope(_post(vault, spy))

    assert first["action"] == "created"
    assert again["action"] == "unchanged"
    assert first["voice"] == again["voice"] == "owner"
    assert len(spy.prompts) == 1
    record = read_voice_draft(vault_path=vault, external_id=_EXTERNAL_ID)
    assert record is not None
    assert record.voice == "owner"


def test_a_draft_stored_without_a_voice_reads_as_the_higher_self(
    tmp_path: Path,
) -> None:
    """Every letter before #2126 was written in the generic Higher Self voice."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=VoiceDraftDocument(
            content=_LETTER, title=None, tier=PrivacyTier.PERSONAL
        ),
        existing_is_admitted=lambda _tier: True,
    )
    post = frontmatter.load(
        str(next((vault / "11-Other-Authors" / "ai-as-user").glob("*.md")))
    )
    assert "voice" not in post["voice_draft"]

    body = envelope(_post(vault, _Spy()))

    assert body["action"] == "unchanged"
    assert body["voice"] == "higher_self"


def test_a_prior_minor_is_not_served_the_voice_field(tmp_path: Path) -> None:
    """Closed response schemas: a 0.34 consumer must not see an unknown key."""
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)

    response = _post(vault, _Spy(), minor=_PRIOR_VOICE_MINOR)

    assert response.status_code == 200, response.text
    assert "voice" not in envelope(response)
    assert VOICE_SINCE_MINOR == "0.35"


def test_a_prior_minor_gets_the_pre_2126_prompt_even_with_owner_evidence(
    tmp_path: Path,
) -> None:
    """A caller that cannot read ``voice`` is never sent prose it cannot label.

    The owner style is applied only from :data:`VOICE_SINCE_MINOR`: below it the
    provider prompt is byte-identical to the generic Higher Self prompt, and the
    letter is stored as written in that voice, so a later 0.35 repeat says so.
    """
    vault = seed_vault(tmp_path / "vault")
    _owner_skill(vault)
    spy = _Spy()

    response = _post(vault, spy, minor=_PRIOR_VOICE_MINOR)

    assert response.status_code == 200, response.text
    assert spy.prompts == [build_letter_prompt(_source())]
    for instruction in _OWNER_INSTRUCTIONS:
        assert instruction not in spy.prompts[0]
    record = read_voice_draft(vault_path=vault, external_id=_EXTERNAL_ID)
    assert record is not None
    assert record.voice == "higher_self"
    assert envelope(_post(vault, spy))["voice"] == "higher_self"


def test_owner_register_samples_above_the_ceiling_never_reach_the_prompt(
    tmp_path: Path,
) -> None:
    """An ``open`` caller's letter is not grounded in personal samples."""
    from tests.test_revoice_evidence import FIXTURES, write_sample

    vault = seed_vault(tmp_path / "vault")
    for index in (1, 2, 3, 4):
        write_sample(
            vault,
            f"personal-{index}",
            (FIXTURES / "owner-samples" / "personal.txt").read_text() * 3,
            tier=PrivacyTier.PERSONAL,
        )
    spy = _Spy()
    body: dict[str, Any] = {
        "content": _ENTRY,
        "note": {"kind": "pattern", "anchor_text": _ANCHOR, "note": _NOTE},
        "tier": "open",
    }
    app = build_app(vault_path=vault, letter_llm_factory=lambda: spy)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/letters/{_EXTERNAL_ID}",
            json=body,
            headers=headers(ceiling="open"),
        )

    assert envelope(response)["voice"] == "higher_self"
    assert "Owner style profile" not in spy.prompts[0]
