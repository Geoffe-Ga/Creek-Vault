"""Regression tests for the BUG-009 fix.

``Fragment.voice_proxy_eligible`` is now a derived property: it cannot
drift from ``privacy_tier`` and ``source.author``. Constructing a
fragment with INTIMATE tier — even *without* going through
``PrivacyClassifier.enforce_tier`` — must produce
``voice_proxy_eligible == False``.
"""

from __future__ import annotations

import pytest

from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)


def _make_fragment(
    *,
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
    author: Authorship = Authorship.SELF,
) -> Fragment:
    """Construct a Fragment without routing through any classifier."""
    return Fragment(
        id="frag-bug009test",
        title="t",
        source=FragmentSource(platform=SourcePlatform.JOURNAL, author=author),
        privacy_tier=privacy_tier,
    )


def test_intimate_tier_disables_voice_proxy_without_enforce() -> None:
    """Hand-constructing an INTIMATE fragment is enough to opt out (BUG-009)."""
    frag = _make_fragment(privacy_tier=PrivacyTier.INTIMATE)
    assert frag.voice_proxy_eligible is False


def test_personal_tier_self_author_is_eligible() -> None:
    """A PERSONAL self-authored fragment is voice-proxy eligible."""
    frag = _make_fragment(privacy_tier=PrivacyTier.PERSONAL)
    assert frag.voice_proxy_eligible is True


def test_public_tier_self_author_is_eligible() -> None:
    """A PUBLIC self-authored fragment is voice-proxy eligible."""
    frag = _make_fragment(privacy_tier=PrivacyTier.OPEN)
    assert frag.voice_proxy_eligible is True


@pytest.mark.parametrize(
    "author",
    [Authorship.AI, Authorship.OTHER, Authorship.COLLABORATIVE],
)
def test_non_self_author_is_never_eligible(author: Authorship) -> None:
    """Non-self authorship excludes the fragment regardless of tier."""
    frag = _make_fragment(privacy_tier=PrivacyTier.OPEN, author=author)
    assert frag.voice_proxy_eligible is False


def test_voice_proxy_eligible_has_no_setter() -> None:
    """The derived property cannot be reassigned (BUG-009 invariant).

    Pydantic emits a ``ValidationError`` when an attribute setter is
    routed at a computed field; callers must update the underlying
    ``privacy_tier`` / ``source.author`` instead.
    """
    frag = _make_fragment(privacy_tier=PrivacyTier.OPEN)
    with pytest.raises((AttributeError, ValueError)):
        frag.voice_proxy_eligible = False  # type: ignore[misc]


def test_tier_mutation_flips_eligibility() -> None:
    """Mutating ``privacy_tier`` to INTIMATE updates eligibility immediately."""
    frag = _make_fragment(privacy_tier=PrivacyTier.PERSONAL)
    assert frag.voice_proxy_eligible is True
    frag.privacy_tier = PrivacyTier.INTIMATE
    assert frag.voice_proxy_eligible is False
