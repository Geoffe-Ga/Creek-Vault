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
    """``reassess`` re-runs the check post-classification and only escalates.

    Since issue #1105 the pass is gated on a required ``baseline`` —
    the heuristic's verdict on the fragment as it was loaded, *before*
    this run classified anything. The pass acts only when this run's
    classification made the heuristic **strictly more restrictive** than
    that baseline, and returns the very same fragment object otherwise.

    The predicate is deliberately ``>`` and not ``!=``: a merely
    *different* verdict includes a **weaker** one, and acting on a
    weakening verdict would let a flip-flopping model raise an
    operator's deliberate tier off evidence that pointed the other way.
    """

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

        # baseline: pre-classification this fragment carried no voice, so the
        # heuristic answered PERSONAL — the verdict below genuinely hardens it.
        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.PERSONAL,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_never_lowers_an_intimate_fragment(self) -> None:
        """A lighter post-classification verdict cannot demote ``intimate``."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.INTIMATE,
        )

        # baseline: no prior heuristic verdict, so the essay's OPEN candidate
        # counts as hardening and the escalate-only merge really runs — which
        # is what keeps this test's "never lowers" claim under test.
        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.UNCLASSIFIED,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE

    def test_never_lowers_a_personal_fragment_to_open(self) -> None:
        """An essay-platform fragment already at ``personal`` stays ``personal``."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.PERSONAL,
        )

        # baseline: as above — UNCLASSIFIED lets the OPEN candidate through the
        # gate so the merge, not the gate, is what refuses to lower the tier.
        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.UNCLASSIFIED,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.PERSONAL

    def test_leaves_a_matching_tier_alone(self) -> None:
        """When the recomputed tier equals the current one, nothing changes."""
        fragment = _fragment(
            platform=SourcePlatform.ESSAY,
            tier=PrivacyTier.OPEN,
        )

        # baseline: the heuristic said OPEN before classification and says OPEN
        # after, so nothing hardened and the pass has nothing to do.
        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.OPEN,
            classifier=PrivacyClassifier(),
        )

        assert result.model_dump(mode="json") == fragment.model_dump(mode="json")

    def test_hardened_signal_also_flips_voice_proxy_eligibility(self) -> None:
        """The escalation carries the derived eligibility flag with it (#1105).

        ``voice_proxy_eligible`` is a computed field over ``privacy_tier``
        + ``source.author``, and it is the actual damage of the bug: a
        confessional fragment left at ``personal`` advertises itself as a
        legitimate input to voice-proxy generation, so private material
        can be echoed back out in generated prose. Pinning the flag as
        well as the tier means an implementation that escalates without
        going through ``enforce_tier`` cannot pass.
        """
        fragment = _fragment(
            platform=SourcePlatform.MARKDOWN,
            tier=PrivacyTier.PERSONAL,
            voice=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            ),
        )
        assert fragment.voice_proxy_eligible is True

        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.PERSONAL,
            classifier=PrivacyClassifier(),
        )

        assert result is not fragment
        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE
        assert result.voice_proxy_eligible is False
        # The input is never mutated: the caller's copy still reads personal.
        assert PrivacyTier(fragment.privacy_tier) is PrivacyTier.PERSONAL

    def test_declines_when_the_candidate_matches_the_baseline(self) -> None:
        """An unchanged signal leaves an operator's lighter tier alone (#1105).

        This journal fragment reads INTIMATE to the heuristic both before
        and after classification — the run produced no *new* privacy
        signal — while the tier on record is the operator's deliberate
        ``personal``. Without the baseline gate the escalate-only merge
        would raise it, which is how the previous ``owns_tier`` proxy
        earned its place; the gate has to keep that guarantee while
        dropping the proxy. Identity (``is``) is asserted rather than
        equality because the contract is "the same object back", which no
        accidental re-stamp can satisfy.
        """
        fragment = _fragment(
            platform=SourcePlatform.JOURNAL,
            tier=PrivacyTier.PERSONAL,
        )

        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.INTIMATE,
            classifier=PrivacyClassifier(),
        )

        assert result is fragment
        assert PrivacyTier(result.privacy_tier) is PrivacyTier.PERSONAL

    def test_declines_when_the_candidate_weakened_from_the_baseline(self) -> None:
        """A *weaker* verdict may not raise a tier either (#1105).

        This is the case that separates the decided ``>`` predicate from
        a naive ``!=`` one. The fragment was loaded looking INTIMATE to
        the heuristic (self-authored, confessional + conviction on disk);
        this run's classification came back merely analytical, so the
        candidate is PERSONAL — *different* from the baseline, but
        different in the weakening direction. Under ``!=`` the pass would
        fire and the escalate-only merge would raise the operator's
        ``open`` to ``personal`` on evidence that pointed the other way.
        Under ``>`` it declines, and the operator's tier survives.
        """
        fragment = _fragment(
            platform=SourcePlatform.MARKDOWN,
            tier=PrivacyTier.OPEN,
            voice=VoiceClassification(
                voice_register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.MUSING,
            ),
        )
        # Precondition: this fixture's candidate really is PERSONAL — markdown
        # is neither the ESSAY nor the DISCORD branch, and an analytical /
        # musing voice misses the third INTIMATE trigger, so ``classify_tier``
        # falls through to its PERSONAL default.
        assert (
            PrivacyClassifier().classify_tier(fragment, content=_NEUTRAL_BODY)
            is PrivacyTier.PERSONAL
        )

        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.INTIMATE,
            classifier=PrivacyClassifier(),
        )

        assert result is fragment
        assert PrivacyTier(result.privacy_tier) is PrivacyTier.OPEN

    def test_a_hardened_signal_still_never_lowers_the_stored_tier(self) -> None:
        """Passing the gate is not licence to lower what is on disk (#1105).

        The gate compares the *heuristic's* two verdicts; the merge that
        follows compares against the tier actually on the fragment. Those
        are different quantities, and this fragment holds them apart: the
        candidate (PERSONAL) hardens relative to the baseline (OPEN), so
        the pass runs — but the stored tier is INTIMATE, so the merge
        must keep INTIMATE. An implementation that assigned the candidate
        outright once the gate opened would silently downgrade intimate
        content, the one direction that leaks it.

        The platform is ``markdown``, not the ``essay`` an earlier sketch
        of this case used: a self-authored essay derives OPEN, which does
        not harden past an OPEN baseline, so that shape would never have
        opened the gate at all and the test would have been vacuous.
        """
        fragment = _fragment(
            platform=SourcePlatform.MARKDOWN,
            tier=PrivacyTier.INTIMATE,
            voice=VoiceClassification(
                voice_register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.MUSING,
            ),
        )

        result = reassess(
            fragment,
            _NEUTRAL_BODY,
            baseline=PrivacyTier.OPEN,
            classifier=PrivacyClassifier(),
        )

        assert PrivacyTier(result.privacy_tier) is PrivacyTier.INTIMATE
        assert result.voice_proxy_eligible is False
