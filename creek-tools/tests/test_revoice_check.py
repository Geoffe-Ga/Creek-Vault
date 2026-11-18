"""Creek, not its clients, decides whether a revoice actually applied a voice.

Regression for the 22 KB analytical-essay revoice that returned ``ok`` while
the "rewrite" was the source with citations stripped, punctuation and spelling
touched, and a few words compressed. Cosmetic edits must not count as voicing,
and a generic canonical skill must not count as the owner's voice.
"""

from __future__ import annotations

import pytest

from creek.author.revoice_check import (
    MIN_REWRITTEN_RATIO,
    UnvoicedReason,
    VoiceConditioning,
    check_revoice,
    normalized_tokens,
    rewritten_ratio,
)

SOURCE = (
    "Attention is a scarce resource, and most institutions spend it badly [1]. "
    "The argument here is simple: when a system rewards visible activity, "
    "people optimise for being seen rather than for doing the work (Smith, 2019). "
    "That behaviour is not a moral failing; it is a rational response to the "
    "incentives in front of them.[^2]\n\n"
    "## Implications\n\n"
    "If we want different behaviour, we have to change what gets noticed. "
    "See [the report](https://example.org/report) for the underlying data.\n\n"
    "[^2]: A footnote that a cleanup pass may delete wholesale."
)

COSMETIC = (
    "Attention is a scarce resource and most institutions spend it badly. "
    "The argument here is simple. When a system rewards visible activity, "
    "people optimize for being seen rather than doing the work. "
    "That behavior is not a moral failing: it is a rational response to the "
    "incentives in front of them.\n\n"
    "## Implications\n\n"
    "If we want different behavior, we have to change what gets noticed. "
    "See the report for the underlying data."
)

REWRITTEN = (
    "Here's what I keep coming back to: we only have so much attention, and "
    "the places I've worked burn through it. Reward people for looking busy "
    "and they'll get very good at looking busy. I don't blame them for that. "
    "They're reading the room correctly.\n\n"
    "## Implications\n\n"
    "So the lever isn't exhortation. It's changing what we bother to notice. "
    "The numbers behind this are in the report."
)


def test_normalization_discards_citations_links_footnotes_and_punctuation() -> None:
    """Citation and punctuation cleanup leaves only the prose's own words."""
    tokens = normalized_tokens(SOURCE)
    assert "1" not in tokens
    assert "smith" not in tokens
    assert "2019" not in tokens
    assert "https" not in tokens
    assert "footnote" not in tokens
    assert tokens[:3] == ["attention", "is", "a"]
    assert "report" in tokens  # link text survives, its URL does not


def test_cosmetic_cleanup_is_not_a_rewrite() -> None:
    """Spelling, punctuation, citation removal and trimming stay under the floor."""
    assert rewritten_ratio(SOURCE, COSMETIC) < MIN_REWRITTEN_RATIO


def test_pure_compression_is_not_a_rewrite() -> None:
    """Dropping sentences rewrites nothing that survives."""
    compressed = (
        "Attention is a scarce resource, and most institutions spend it badly. "
        "If we want different behaviour, we have to change what gets noticed."
    )
    assert rewritten_ratio(SOURCE, compressed) == pytest.approx(0.0)


def test_genuine_rewrite_clears_the_floor() -> None:
    """New phrasing throughout is measured as rewritten."""
    assert rewritten_ratio(SOURCE, REWRITTEN) > 0.6


def test_identical_and_empty_outputs() -> None:
    """An unchanged passage scores zero; an empty one cannot divide by zero."""
    assert rewritten_ratio(SOURCE, SOURCE) == pytest.approx(0.0)
    assert rewritten_ratio(SOURCE, "") == pytest.approx(0.0)


def test_owner_style_and_real_rewrite_is_voice_applied() -> None:
    """Both owner conditioning and material change are needed for success."""
    check = check_revoice(
        SOURCE,
        REWRITTEN,
        owner_style_skills=("voice-core",),
        voice_skills=("voice-core",),
    )
    assert check.voice_applied
    assert check.conditioning is VoiceConditioning.OWNER
    assert check.reasons == ()


def test_cosmetic_output_reports_insufficient_rewrite() -> None:
    """The essay regression: owner skills present, but the prose did not move."""
    check = check_revoice(
        SOURCE,
        COSMETIC,
        owner_style_skills=("voice-core",),
        voice_skills=("voice-core",),
    )
    assert not check.voice_applied
    assert check.reasons == (UnvoicedReason.INSUFFICIENT_REWRITE,)
    assert check.opening_similarity > 0.9


@pytest.mark.parametrize(
    ("voice_skills", "conditioning"),
    [
        (("voice-core", "register-analytical"), VoiceConditioning.GENERIC),
        ((), VoiceConditioning.NONE),
    ],
)
def test_without_owner_style_no_rewrite_counts_as_voiced(
    voice_skills: tuple[str, ...], conditioning: VoiceConditioning
) -> None:
    """A canonical template is the same for every owner, so it is not their voice."""
    check = check_revoice(
        SOURCE, REWRITTEN, owner_style_skills=(), voice_skills=voice_skills
    )
    assert not check.voice_applied
    assert check.conditioning is conditioning
    assert check.reasons == (UnvoicedReason.NO_OWNER_STYLE,)


def test_both_reasons_are_reported_together() -> None:
    """A client sees every reason at once, in a stable order."""
    check = check_revoice(SOURCE, SOURCE, owner_style_skills=(), voice_skills=())
    assert check.reasons == (
        UnvoicedReason.NO_OWNER_STYLE,
        UnvoicedReason.INSUFFICIENT_REWRITE,
    )


def test_as_dict_is_content_free_and_json_ready() -> None:
    """The wire projection carries names, numbers and codes, never prose."""
    check = check_revoice(
        SOURCE,
        COSMETIC,
        owner_style_skills=("voice-core",),
        voice_skills=("voice-core", "register-analytical"),
    )
    payload = check.as_dict()
    assert payload == {
        "voice_applied": False,
        "conditioning": "owner",
        "owner_style_skills": ["voice-core"],
        "rewritten_ratio": payload["rewritten_ratio"],
        "min_rewritten_ratio": MIN_REWRITTEN_RATIO,
        "opening_similarity": payload["opening_similarity"],
        "reasons": ["insufficient_rewrite"],
    }
    assert isinstance(payload["rewritten_ratio"], float)
    assert "Attention" not in repr(payload)
