"""Pure policy functions for the privacy-tier pass (issue #876).

``creek.classify.privacy_pass`` is the shared, side-effect-free policy
layer that the classify engine and the ``creek process`` pipeline both
call so a fragment never reaches the per-tier router — or the vault —
still carrying ``privacy_tier: unclassified``.

The four functions under test:

* :func:`needs_tier` — does this raw frontmatter still owe us a tier?
* :func:`escalate` — merge two candidate tiers, never lowering.
* :func:`apply_tier` — stamp a tier, honouring a manual override.
* :func:`reassess` — re-run the tier check after classification,
  escalate-only.

No vault, no I/O, no LLM: every case here is a direct call.
"""

from __future__ import annotations

import pytest

from creek.classify.privacy import PrivacyClassifier
from creek.classify.privacy_pass import apply_tier, escalate, needs_tier, reassess
from creek.models import (
    Authorship,
    Confidence,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
)

_NEUTRAL_BODY = "a plain note about the weather and the walk to the shops"


def _fragment(
    *,
    frag_id: str = "frag-passunit0001",
    platform: SourcePlatform = SourcePlatform.MARKDOWN,
    tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
    channel: str | None = None,
    author: Authorship = Authorship.SELF,
    voice: VoiceClassification | None = None,
) -> Fragment:
    """Build a fragment with just the axes :mod:`privacy_pass` reads.

    Args:
        frag_id: Fragment id.
        platform: Source platform driving the tier heuristic.
        tier: The tier already recorded on the fragment.
        channel: Optional Discord channel name.
        author: Authorship axis (INTIMATE is gated on ``SELF``).
        voice: Optional voice classification (confessional + conviction
            is the third INTIMATE trigger).

    Returns:
        The assembled :class:`~creek.models.Fragment`.
    """
    return Fragment(
        id=frag_id,
        title="A note",
        source=FragmentSource(platform=platform, author=author, channel=channel),
        privacy_tier=tier,
        voice=voice or VoiceClassification(),
    )


class TestNeedsTier:
    """``needs_tier`` is true exactly when no deliberate tier is on disk."""

    def test_absent_privacy_tier_key_needs_a_tier(self) -> None:
        """Frontmatter with no ``privacy_tier`` key at all still owes a tier."""
        assert needs_tier({}) is True

    def test_explicit_unclassified_needs_a_tier(self) -> None:
        """An explicit ``unclassified`` is the pipeline default, not a decision."""
        assert needs_tier({"privacy_tier": "unclassified"}) is True

    @pytest.mark.parametrize("tier", ["open", "personal", "intimate"])
    def test_explicit_real_tier_does_not_need_one(self, tier: str) -> None:
        """A deliberate tier on disk is a decision the pass must not overwrite."""
        assert needs_tier({"privacy_tier": tier}) is False

    def test_other_keys_do_not_confuse_the_check(self) -> None:
        """Unrelated frontmatter keys never make a tiered fragment look untiered."""
        raw = {"type": "fragment", "id": "frag-x", "privacy_tier": "personal"}
        assert needs_tier(raw) is False


class TestEscalate:
    """``escalate`` returns the more restrictive of two tiers — the full matrix."""

    @pytest.mark.parametrize(
        ("current", "candidate", "expected"),
        [
            (PrivacyTier.OPEN, PrivacyTier.OPEN, PrivacyTier.OPEN),
            (PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.PERSONAL),
            (PrivacyTier.OPEN, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
            (PrivacyTier.PERSONAL, PrivacyTier.OPEN, PrivacyTier.PERSONAL),
            (PrivacyTier.PERSONAL, PrivacyTier.PERSONAL, PrivacyTier.PERSONAL),
            (PrivacyTier.PERSONAL, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
            (PrivacyTier.INTIMATE, PrivacyTier.OPEN, PrivacyTier.INTIMATE),
            (PrivacyTier.INTIMATE, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE),
            (PrivacyTier.INTIMATE, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
        ],
    )
    def test_escalate_matrix(
        self,
        current: PrivacyTier,
        candidate: PrivacyTier,
        expected: PrivacyTier,
    ) -> None:
        """open < personal < intimate; the winner is always the stricter side."""
        assert escalate(current, candidate) is expected

    def test_escalate_is_symmetric_in_its_arguments(self) -> None:
        """Order of arguments cannot change which tier wins.

        A merge that is not symmetric would let the *caller's* argument
        order decide whether an intimate fragment is lowered — precisely
        the failure mode ``escalate`` exists to make impossible.
        """
        for left in (PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE):
            for right in (PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE):
                assert escalate(left, right) is escalate(right, left)


class TestApplyTier:
    """``apply_tier`` stamps a tier without ever trampling a manual decision."""

    def test_assigns_tier_when_key_is_absent(self) -> None:
        """A fragment whose frontmatter has no tier key gets a real one."""
        fragment = _fragment(platform=SourcePlatform.JOURNAL)

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={},
            force=False,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_assigns_tier_when_explicitly_unclassified(self) -> None:
        """``privacy_tier: unclassified`` on disk is replaced with a real tier."""
        fragment = _fragment(platform=SourcePlatform.JOURNAL)

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={"privacy_tier": "unclassified"},
            force=False,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_manual_override_wins_without_force(self) -> None:
        """An explicit non-unclassified tier survives a non-force pass untouched.

        The heuristic would call this journal fragment ``intimate``; the
        operator said ``open``. Without ``--force`` the operator wins and
        nothing else on the fragment moves either.
        """
        fragment = _fragment(
            platform=SourcePlatform.JOURNAL,
            tier=PrivacyTier.OPEN,
        )

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={"privacy_tier": "open"},
            force=False,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.OPEN
        assert result.model_dump(mode="json") == fragment.model_dump(mode="json")

    def test_force_escalates_an_explicit_open_tier(self) -> None:
        """Under ``--force`` a light on-disk tier is raised to the heuristic's."""
        fragment = _fragment(
            platform=SourcePlatform.JOURNAL,
            tier=PrivacyTier.OPEN,
        )

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={"privacy_tier": "open"},
            force=True,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_force_never_lowers_an_existing_intimate_tier(self) -> None:
        """``--force`` merges escalate-only: intimate is never auto-downgraded.

        The heuristic would call an essay ``open``. A fragment already
        marked ``intimate`` must stay intimate even under ``--force`` —
        auto-lowering is the one direction that leaks content.
        """
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.INTIMATE,
        )

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={"privacy_tier": "intimate"},
            force=True,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_never_returns_unclassified(self) -> None:
        """Whatever the inputs, the assigned tier is a real one.

        The engine's invariant ("never persist ``unclassified``") is only
        as strong as this function; a chatbot fragment with no signals at
        all must still land on ``personal``, not fall through.
        """
        fragment = _fragment(platform=SourcePlatform.CHATGPT)

        result = apply_tier(
            fragment,
            _NEUTRAL_BODY,
            raw={},
            force=False,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.PERSONAL

    def test_recovery_keyword_in_body_reaches_the_classifier(self) -> None:
        """The body is threaded through — recovery keywords are body-only signals."""
        fragment = _fragment(platform=SourcePlatform.MARKDOWN)

        result = apply_tier(
            fragment,
            "ninety days of sobriety and the walk felt shorter today",
            raw={},
            force=False,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE


class TestReassess:
    """``reassess`` re-runs the check post-classification and only escalates."""

    def test_escalates_when_classification_hardens_the_signal(self) -> None:
        """Confessional + conviction from the classifier lifts personal → intimate."""
        fragment = _fragment(
            platform=SourcePlatform.MARKDOWN,
            tier=PrivacyTier.PERSONAL,
            voice=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            ),
        )

        result = reassess(fragment, _NEUTRAL_BODY, classifier=PrivacyClassifier())

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_never_lowers_an_intimate_fragment(self) -> None:
        """A lighter post-classification verdict cannot demote ``intimate``."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.INTIMATE,
        )

        result = reassess(fragment, _NEUTRAL_BODY, classifier=PrivacyClassifier())

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_never_lowers_a_personal_fragment_to_open(self) -> None:
        """An essay-platform fragment already at ``personal`` stays ``personal``."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.PERSONAL,
        )

        result = reassess(fragment, _NEUTRAL_BODY, classifier=PrivacyClassifier())

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.PERSONAL

    def test_leaves_a_matching_tier_alone(self) -> None:
        """When the recomputed tier equals the current one, nothing changes."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.OPEN,
        )

        result = reassess(fragment, _NEUTRAL_BODY, classifier=PrivacyClassifier())

        assert result.model_dump(mode="json") == fragment.model_dump(mode="json")
