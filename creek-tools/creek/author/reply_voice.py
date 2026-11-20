"""The owner's style for ``/v1`` letters and reflections (#2126).

A letter or a margin note is the owner's Higher Self speaking. When the vault
holds evidence of how the owner actually writes, the prompt carries it and the
response says ``voice: "owner"``; when it holds none, the prompt is the
generic Higher Self one it always was and the response says
``voice: "higher_self"``. A missing voice is never a refusal: a letter is still
useful without one.

The style comes from the Writing Desk voice stack, never from a new source:

* **Owner-authored style skills** for voice-core and the
  :data:`REPLY_REGISTER` register, through
  :func:`creek.author.voice.owner_style_sections`, which projects every skill
  onto source-free style and drops canonical signatures (the same text for
  every vault).
* **The owner's register samples**, admitted at the caller's ceiling and
  reduced to a numeric profile by
  :func:`creek.author.revoice_grounding.owner_grounding`. No sample words are
  sent.
* **The persisted voice fingerprint** (``creek report --type fingerprint``),
  rendered by :func:`creek.generate.ai_style.preamble.build_style_preamble`.
  It is measured from every non-intimate self-authored fragment, so it is used
  only when the caller's ceiling admits ``personal``; and a thin one renders
  generic advice, so it counts only when it is not thin.

**Only the owner's own writing.** Every source above is screened for
self-authorship: register samples need ``author: self`` and a positive
``voice_weight``, the fingerprint walks self-authored ``01-Fragments`` only,
and skills are files the owner wrote. Letters this vault wrote earlier are
Voice Drafts under ``11-Other-Authors/ai-as-user`` with AI authorship and a
zero voice weight, and reflections are never stored, so no earlier AI output
and no borrowed author can become the owner's voice.

The vocabulary (:class:`~creek.reply_voice.ReplyVoice`,
:class:`~creek.reply_voice.ReplyStyle`) lives in :mod:`creek.reply_voice`,
outside this package, so the ``/v1`` adapters can name it without importing
``creek.author`` at cold start; they import :func:`reply_style` lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from creek.author.revoice_grounding import owner_grounding
from creek.author.revoice_style import render_profile
from creek.author.voice import owner_style_sections
from creek.classify.privacy_filter import tier_within_override
from creek.config import CONFIG_LOAD_FAILURES, load_vault_config
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek.generate.ai_style.preamble import build_style_preamble
from creek.models import PrivacyTier, VoiceRegister
from creek.reply_voice import HIGHER_SELF_STYLE, ReplyStyle, ReplyVoice

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride

REPLY_REGISTER: Final[str] = VoiceRegister.CONVERSATIONAL.value
"""The register a letter or margin note is written in: plain, to the reader."""

FINGERPRINT_CORPUS_TIER: Final[PrivacyTier] = PrivacyTier.PERSONAL
"""The most sensitive tier the persisted fingerprint is measured from.

``creek report --type fingerprint`` builds it without intimate fragments, so
its corpus ranks at most ``personal`` (``unclassified`` ranks with it).
"""


def _fingerprint_section(vault: Path, override: PrivacyTierOverride) -> str:
    """Return the fingerprint's voice targets, or ``""`` when it may not apply.

    Withheld below a ``personal`` ceiling, when thin, when prompt prevention is
    off, and when the vault config cannot load: none of these is a refusal.
    """
    if not tier_within_override(FINGERPRINT_CORPUS_TIER, override):
        return ""
    try:
        ai_style = load_vault_config(vault).ai_style
    except CONFIG_LOAD_FAILURES:
        return ""
    fingerprint = load_fingerprint(vault, ai_style)
    if fingerprint.is_thin(ai_style.min_fingerprint_fragments):
        return ""
    return build_style_preamble(fingerprint, ai_style)


def reply_style(vault: Path, *, override: PrivacyTierOverride) -> ReplyStyle:
    """Return the owner's style for one letter or reflection.

    Args:
        vault: The vault root.
        override: The admission ceiling matching the caller's declared one.

    Returns:
        :data:`HIGHER_SELF_STYLE` when the vault holds no owner evidence
        admitted at *override*; otherwise the owner's style sections.
    """
    sections = [
        style
        for _name, style in owner_style_sections(
            vault, phase=None, register=REPLY_REGISTER
        )
    ]
    profile = owner_grounding(vault, override=override, register=REPLY_REGISTER)
    if profile.profile is not None:
        sections.append(render_profile(profile.profile))
    fingerprint = _fingerprint_section(vault, override)
    if fingerprint:
        sections.append(fingerprint)
    if not sections:
        return HIGHER_SELF_STYLE
    return ReplyStyle(ReplyVoice.OWNER, tuple(sections))
