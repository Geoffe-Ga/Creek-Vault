"""The rule pass must merge its verdict, not replace the fragment (#1331).

``RuleClassifier._build_updates`` rebuilds ``WavelengthClassification`` and
``VoiceClassification`` **from scratch** whenever any one of their axes
matched, so every axis the matchers were silent about is reset to its
default. A phase-only body therefore blanks mode, orientation, dosage,
colour and descriptor; a register-only body blanks a persisted
``confidence``.

This is the rules-layer twin of the weighted-path defect closed by #1309 /
PR #1402, and it mirrors that fix's shape: a dimension the classifier said
nothing about keeps whatever the fragment already carried, while frequency
keeps its deliberate asymmetry (replaced wholesale when a primary is picked,
because ``secondary`` is a list and cannot be merged field-wise; left alone
entirely when no primary is picked). See
:meth:`creek.classify.weighted.WeightedFragmentClassification.merge_onto`.

**Why this is a privacy defect and not merely lossy bookkeeping.**
:meth:`~creek.classify.privacy.PrivacyClassifier._is_high_confidence_confessional`
escalates to ``INTIMATE`` only when ``voice_register`` is ``confessional``
**and** ``confidence`` is ``conviction``. On a confessional body the rule
pass supplies the missing half and destroys the half already on disk in the
same operation, so the escalation that should fire never does. Every writer
of ``privacy_tier`` is escalate-only, which makes this a **missed
escalation** — a fail-open — not a lowered tier. Accordingly every privacy
assertion below asserts the tier goes **UP** to ``INTIMATE``; an assertion
that "the tier did not go down" passes against the bug and proves nothing.

The bodies used here are characterised empirically, one matcher each. That
characterisation is load-bearing: if a body silently starts firing a second
matcher these tests quietly begin measuring something else, so each constant
records both what it fires and what it deliberately does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.evidence import layer_determined_over
from creek.classify.llm import LLMClassifier
from creek.classify.privacy import PrivacyClassifier
from creek.classify.reatomize import _is_accepted
from creek.classify.rules import RuleClassifier
from creek.config import CreekConfig, LLMConfig
from creek.models import (
    Authorship,
    Color,
    Confidence,
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_FRAGMENT_ID: Final[str] = "frag-1331-rules-merge"
"""Stable id so the on-disk tests can find the file they seeded."""

_TITLE: Final[str] = "A quiet note"
"""A title every rule matcher is silent about.

Load-bearing. The matchers score the *title* alongside the body, so a
descriptive title poisons the fixture: an earlier lane's "Evidence bearing
fragment" tripped ``_match_voice_register`` on the word "evidence", firing
the voice guard in a test whose body was meant to be inert.
"""

_DESCRIPTOR: Final[str] = "Social Anxiety"
"""The persisted wavelength descriptor — a free-text axis no matcher writes."""

_PHASE_ONLY_BODY: Final[str] = (
    "The peak arrived. At its peak the whole thing reached a peak of stillness."
)
"""Fires the phase matcher (``peaking``) and nothing else.

Deliberately silent for mode, frequency, voice register and confidence, so
whatever the fragment already carries on those five axes is the only thing
that can explain their value after the pass.
"""

_MODE_ONLY_BODY: Final[str] = (
    "Expressing it, creating it, articulating it: expressing again and creating again."
)
"""Fires the mode matcher (``express``) and nothing else.

Deliberately silent for phase — which is the point: phase and mode share one
``OR`` guard, so a mode-only match is what exposes the cross-axis
destruction inside :class:`~creek.models.WavelengthClassification`.
"""

_REGISTER_ONLY_BODY: Final[str] = (
    "I have to admit something. I confess I was afraid, and honestly I still am."
)
"""Fires the voice-register matcher (``confessional``) and nothing else.

Deliberately silent for confidence — the exact shape that makes this a
privacy defect: the pass supplies half the INTIMATE trigger and destroys the
other half in the same breath. Also silent for phase, mode and frequency,
and carries no recovery keyword, so ``INTIMATE`` here can only ever come
from the confessional+conviction rule.
"""

_CONFIDENCE_ONLY_BODY: Final[str] = (
    "I believe this. I know this. It is definitely and certainly the case."
)
"""Fires the confidence matcher (``settled``) and nothing else.

Deliberately silent for voice register — the mirror image of
``_REGISTER_ONLY_BODY``, covering the other half of the voice ``OR`` guard.
"""

_FREQ_ONLY_BODY: Final[str] = (
    "Power and dominance and control, power over power, dominance again, control again."
)
"""Fires the frequency matcher (primary ``F3``) and nothing else.

Deliberately silent for phase, mode, register and confidence, so it isolates
the one axis whose replacement is *correct* from the axes whose replacement
is the bug.
"""

_RULE_INERT_BODY: Final[str] = (
    "A plain paragraph about gardening tools and afternoon light."
)
"""Fires no matcher at all — the whole-pass no-op control."""


def _seeded_fragment(
    *,
    voice: VoiceClassification | None = None,
    frequency: FrequencyClassification | None = None,
) -> Fragment:
    """Build a fragment already carrying a full classification.

    The wavelength block is populated on all six axes and the voice block on
    both, so any axis that comes back at its default after a rule pass was
    destroyed by that pass rather than never set.

    Args:
        voice: Voice block to persist; defaults to ``analytical`` +
            ``conviction`` — an ``ESSAY``/``SELF`` fragment that is *not*
            INTIMATE, but that holds the ``conviction`` half of the trigger.
        frequency: Frequency block to persist; defaults to an empty one.

    Returns:
        The seeded :class:`~creek.models.Fragment`.
    """
    if voice is None:
        voice = VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
    if frequency is None:
        frequency = FrequencyClassification()
    return Fragment(
        id=_FRAGMENT_ID,
        title=_TITLE,
        source=FragmentSource(platform=SourcePlatform.ESSAY, author=Authorship.SELF),
        frequency=frequency,
        wavelength=WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            orientation=Orientation.FEEL,
            dosage=Dosage.MEDICINE,
            color=Color.GREEN,
            descriptor=_DESCRIPTOR,
        ),
        voice=voice,
    )


def _assert_wavelength(
    fragment: Fragment,
    *,
    phase: Phase,
    mode: Mode,
) -> None:
    """Assert all six wavelength axes of a seeded fragment at once.

    Every axis is named explicitly rather than spot-checking the two the
    issue emphasises: the recurring failure mode on this defect is a fix that
    restores ``mode`` and ``descriptor`` and silently keeps resetting
    ``orientation``, ``dosage`` or ``color``.

    Args:
        fragment: The fragment to inspect.
        phase: The expected phase (the only axis a phase match may change).
        mode: The expected mode (the only axis a mode match may change).
    """
    assert fragment.wavelength.phase == phase
    assert fragment.wavelength.mode == mode
    assert fragment.wavelength.orientation == Orientation.FEEL
    assert fragment.wavelength.dosage == Dosage.MEDICINE
    assert fragment.wavelength.color == Color.GREEN
    assert fragment.wavelength.descriptor == _DESCRIPTOR


def _assert_voice(
    fragment: Fragment,
    *,
    register: VoiceRegister,
    confidence: Confidence,
) -> None:
    """Assert both voice axes of a seeded fragment.

    Args:
        fragment: The fragment to inspect.
        register: The expected voice register.
        confidence: The expected confidence stance.
    """
    assert fragment.voice.voice_register == register
    assert fragment.voice.confidence == confidence


class TestWavelengthMerge:
    """A wavelength match overlays one axis; it does not rebuild the block."""

    def test_a_phase_match_leaves_the_other_five_axes_standing(self) -> None:
        """Phase advances to ``peaking`` and nothing else moves.

        At HEAD the ``OR`` guard at ``rules.py:785`` constructs a fresh
        ``WavelengthClassification(phase=..., mode=...)``, so five of six axes
        come back at their defaults — ``mode``, ``orientation``, ``dosage``
        and ``color`` at ``unclassified`` and ``descriptor`` at ``""``.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_PHASE_ONLY_BODY,
        )
        # What the rules DID determine is adopted...
        assert result.wavelength.phase == Phase.PEAKING
        # ...and everything they were silent about survives.
        _assert_wavelength(result, phase=Phase.PEAKING, mode=Mode.EXPRESS)

    def test_a_mode_match_does_not_blank_the_persisted_phase(self) -> None:
        """Mode is set from the body while the on-record phase survives.

        The cross-destruction case. Phase and mode share a single ``OR``
        guard, so at HEAD a body that speaks only to mode still rebuilds the
        whole block and resets a perfectly good ``rising`` to the model
        default ``unclassified``.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_MODE_ONLY_BODY,
        )
        assert result.wavelength.mode == Mode.EXPRESS
        _assert_wavelength(result, phase=Phase.RISING, mode=Mode.EXPRESS)

    def test_the_voice_block_is_untouched_by_a_wavelength_match(self) -> None:
        """A phase-only body has no standing to rewrite the voice axes."""
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_PHASE_ONLY_BODY,
        )
        _assert_voice(
            result,
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )


class TestVoiceMerge:
    """A voice match overlays one axis; it does not rebuild the block."""

    def test_a_register_match_keeps_the_persisted_confidence(self) -> None:
        """Register becomes ``confessional`` and ``conviction`` stays put.

        At HEAD ``rules.py:791`` builds
        ``VoiceClassification(voice_register=confessional, confidence=None)``
        — the destruction that costs the fragment its INTIMATE escalation.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_REGISTER_ONLY_BODY,
        )
        _assert_voice(
            result,
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )

    def test_a_confidence_match_keeps_the_persisted_register(self) -> None:
        """Confidence becomes ``settled`` and ``analytical`` stays put.

        The other half of the same ``OR`` guard: at HEAD a body that speaks
        only to confidence nulls the register.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_CONFIDENCE_ONLY_BODY,
        )
        _assert_voice(
            result,
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )

    def test_the_wavelength_block_is_untouched_by_a_voice_match(self) -> None:
        """A register-only body has no standing to rewrite the wavelength."""
        result = RuleClassifier().classify(
            _seeded_fragment(),
            content=_REGISTER_ONLY_BODY,
        )
        _assert_wavelength(result, phase=Phase.RISING, mode=Mode.EXPRESS)


class TestFrequencyAsymmetry:
    """Frequency is replaced wholesale, and only when a primary is picked."""

    def test_a_frequency_match_replaces_frequency_and_touches_nothing_else(
        self,
    ) -> None:
        """``F3`` lands, stale secondaries clear, voice and wavelength hold.

        The asymmetry is deliberate and mirrors
        :meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`:
        ``secondary`` is a list, which cannot be merged field-wise the way
        the scalar axes can, so a fresh primary clears it rather than
        accumulating verdicts. Pinned here so nobody later "completes" the
        merge by making frequency behave like the other two blocks.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(
                frequency=FrequencyClassification(
                    primary=Frequency.F1,
                    secondary=[Frequency.F5],
                ),
            ),
            content=_FREQ_ONLY_BODY,
        )
        assert result.frequency.primary == Frequency.F3
        assert result.frequency.secondary == []
        _assert_wavelength(result, phase=Phase.RISING, mode=Mode.EXPRESS)
        _assert_voice(
            result,
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )


class TestMergeIsNotInaction:
    """The merge preserves prior evidence without disabling the classifier."""

    def test_an_inert_body_changes_nothing(self) -> None:
        """No matcher fires, so the whole classification comes back as-is."""
        seeded = _seeded_fragment(
            frequency=FrequencyClassification(
                primary=Frequency.F3,
                secondary=[Frequency.F5],
            ),
        )
        result = RuleClassifier().classify(seeded, content=_RULE_INERT_BODY)
        assert result.frequency.primary == Frequency.F3
        assert result.frequency.secondary == [Frequency.F5]
        _assert_wavelength(result, phase=Phase.RISING, mode=Mode.EXPRESS)
        _assert_voice(
            result,
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )

    def test_a_fragment_with_no_prior_classification_still_gets_classified(
        self,
    ) -> None:
        """A fresh fragment is classified normally by the merged pass.

        The guard against the degenerate "fix": a pass that preserved
        everything by determining nothing would satisfy every other test in
        this module. The rules must still write what they actually found.
        """
        fresh = Fragment(
            id="frag-1331-fresh",
            title=_TITLE,
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=Authorship.SELF,
            ),
        )
        # Precondition: nothing on record to preserve.
        assert fresh.wavelength.phase == Phase.UNCLASSIFIED

        result = RuleClassifier().classify(fresh, content=_PHASE_ONLY_BODY)
        assert result.wavelength.phase == Phase.PEAKING
        # The axes the body is silent about stay at their defaults rather
        # than being invented.
        assert result.wavelength.mode == Mode.UNCLASSIFIED
        assert result.wavelength.descriptor == ""


class TestPrivacyEscalation:
    """The destroyed evidence is half of the INTIMATE trigger."""

    def test_the_rule_pass_escalates_a_confessional_fragment_to_intimate(
        self,
    ) -> None:
        """Supplying ``confessional`` over a persisted ``conviction`` buries it.

        This asserts an escalation **UP** — ``OPEN`` before the pass,
        ``INTIMATE`` after — and that direction is the whole point. Every
        writer of ``privacy_tier`` is escalate-only, so "the tier did not go
        down" is true at HEAD and true after the fix and distinguishes
        nothing; the defect is a **missed** escalation, a fail-open. The
        baseline is asserted first so the test proves its own premise rather
        than assuming it.

        At HEAD the rule pass returns ``{confessional, None}`` and
        ``_is_high_confidence_confessional`` short-circuits on the ``None``,
        so the fragment stays ``OPEN`` and is eligible for cloud egress.
        """
        fragment = _seeded_fragment()
        privacy = PrivacyClassifier()

        # Premise: as persisted (analytical + conviction) this ESSAY is OPEN,
        # so INTIMATE below is a genuine escalation and not the status quo.
        assert (
            privacy.classify_tier(fragment, content=_REGISTER_ONLY_BODY)
            == PrivacyTier.OPEN
        )

        result = RuleClassifier().classify(fragment, content=_REGISTER_ONLY_BODY)
        assert (
            privacy.classify_tier(result, content=_REGISTER_ONLY_BODY)
            == PrivacyTier.INTIMATE
        )

    def test_supplied_and_persisted_evidence_reach_the_same_tier(self) -> None:
        """Same evidence, same tier, however the two halves arrived.

        Arm A already holds both halves on disk and needs no verdict from
        the rules. Arm B holds ``conviction`` on disk and has the rules
        supply ``confessional``. The privacy heuristic reads a fragment, not
        a provenance, so the two must land on the same tier — at HEAD they
        diverge, because arm B's rule pass eats the half it did not supply.
        """
        rules = RuleClassifier()
        privacy = PrivacyClassifier()

        already_confessional = _seeded_fragment(
            voice=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            ),
        )
        tier_persisted = privacy.classify_tier(
            rules.classify(already_confessional, content=_RULE_INERT_BODY),
            content=_RULE_INERT_BODY,
        )
        tier_supplied = privacy.classify_tier(
            rules.classify(_seeded_fragment(), content=_REGISTER_ONLY_BODY),
            content=_REGISTER_ONLY_BODY,
        )

        assert tier_persisted == PrivacyTier.INTIMATE
        assert tier_supplied == PrivacyTier.INTIMATE
        assert tier_persisted == tier_supplied

    @pytest.mark.parametrize(
        "stance",
        [Confidence.MUSING, Confidence.EXPLORING],
    )
    def test_a_tentative_stance_is_not_manufactured_into_intimate(
        self,
        stance: Confidence,
    ) -> None:
        """A confessional register over a tentative stance stays out of INTIMATE.

        Under the escalate-only ratchet a manufactured escalation is
        permanent burial, so over-protection is as much a defect as
        under-protection. The stance is asserted to have *survived* as well:
        without that assertion the test would pass at HEAD for the wrong
        reason — the field having been destroyed rather than respected.

        Args:
            stance: The tentative confidence already on the fragment.
        """
        result = RuleClassifier().classify(
            _seeded_fragment(
                voice=VoiceClassification(
                    voice_register=VoiceRegister.ANALYTICAL,
                    confidence=stance,
                ),
            ),
            content=_REGISTER_ONLY_BODY,
        )

        # The register really was supplied, and the stance really survived,
        # so the tier below is a judgement and not an accident of erasure.
        assert result.voice.voice_register == VoiceRegister.CONFESSIONAL
        assert result.voice.confidence == stance
        assert (
            PrivacyClassifier().classify_tier(result, content=_REGISTER_ONLY_BODY)
            != PrivacyTier.INTIMATE
        )


class TestTheMergePrimitive:
    """``layer_determined_over`` and the invariant that makes it sound."""

    @pytest.mark.parametrize(
        ("model", "label"),
        [
            (FrequencyClassification, "frequency"),
            (WavelengthClassification, "wavelength"),
            (VoiceClassification, "voice"),
        ],
    )
    def test_every_default_is_a_not_determined_sentinel(
        self,
        model: type[FrequencyClassification]
        | type[WavelengthClassification]
        | type[VoiceClassification],
        label: str,
    ) -> None:
        """A default-constructed block dumps to ``{}`` under ``exclude_defaults``.

        The executable form of the precondition the whole merge rests on. If a
        future field lands with a default that is a real value rather than an
        absence — the way ``Fragment.voice_weight`` defaults to ``1.0`` — this
        goes red here instead of silently teaching every classifier pass to
        stamp that default over prior evidence again.

        Args:
            model: The classification block under test.
            label: Human-readable name, so a failure names the block.
        """
        assert model().model_dump(exclude_defaults=True) == {}, label

    def test_a_wholly_undetermined_verdict_changes_nothing(self) -> None:
        """A pass that decided nothing leaves every axis on record intact."""
        prior = WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            orientation=Orientation.FEEL,
            dosage=Dosage.MEDICINE,
            color=Color.GREEN,
            descriptor=_DESCRIPTOR,
        )
        merged = layer_determined_over(
            prior=prior,
            determined=WavelengthClassification(),
        )
        assert merged.model_dump() == prior.model_dump()

    def test_determined_axes_win_and_silent_axes_defer(self) -> None:
        """The two halves of the rule, on one call."""
        merged = layer_determined_over(
            prior=VoiceClassification(
                voice_register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.CONVICTION,
            ),
            determined=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
            ),
        )
        assert merged.voice_register == VoiceRegister.CONFESSIONAL
        assert merged.confidence == Confidence.CONVICTION

    def test_neither_operand_is_mutated(self) -> None:
        """The merge is pure — callers may reuse both inputs afterwards."""
        prior = VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        determined = VoiceClassification(voice_register=VoiceRegister.CONFESSIONAL)

        layer_determined_over(prior=prior, determined=determined)

        assert prior.voice_register == VoiceRegister.ANALYTICAL
        assert prior.confidence == Confidence.CONVICTION
        assert determined.voice_register == VoiceRegister.CONFESSIONAL
        assert determined.confidence is None

    def test_it_is_correct_over_a_mixed_str_and_enum_prior(self) -> None:
        """``use_enum_values=True`` leaves a prior part ``str``, part enum.

        The classification models set ``use_enum_values=True``, so a field
        passed to the constructor is stored as its plain ``str`` while a
        defaulted field keeps the enum member. A prior is therefore a mix of
        the two depending on how it was built, and any merge that compared
        values by hand would have to be right on both. This one performs no
        comparison at all — pinned here so that stays true.
        """
        prior = WavelengthClassification(phase=Phase.RISING, descriptor=_DESCRIPTOR)
        assert isinstance(prior.phase, str)
        assert isinstance(prior.orientation, Orientation)

        merged = layer_determined_over(
            prior=prior,
            determined=WavelengthClassification(mode=Mode.EXPRESS),
        )
        assert merged.phase == Phase.RISING
        assert merged.mode == Mode.EXPRESS
        assert merged.descriptor == _DESCRIPTOR
        assert merged.orientation == Orientation.UNCLASSIFIED


class TestSinglePickLLMVoiceMerge:
    """The same fail-open, one layer on: ``_apply_voice`` (#1331)."""

    @staticmethod
    def _classify(response: str, fragment: Fragment) -> Fragment:
        """Drive ``LLMClassifier.classify`` over a canned response.

        Args:
            response: The YAML the provider is made to return.
            fragment: The fragment to classify.

        Returns:
            The classified fragment.
        """
        classifier = LLMClassifier(config=LLMConfig())
        classifier._available = True
        with patch.object(LLMClassifier, "_invoke_llm", return_value=response):
            return classifier.classify(fragment)

    def test_a_response_silent_on_confidence_keeps_the_persisted_one(self) -> None:
        """A partial ``voice:`` block must not null the other axis.

        The rules-layer fix alone does not close the fail-open on
        ``--method llm``: the rule pass preserves ``conviction`` and the
        single-pick parse then destroyed it a moment later. At HEAD this
        returned ``confidence=None`` and the fragment silently failed to
        escalate.
        """
        result = self._classify(
            "voice:\n  voice_register: confessional\n",
            _seeded_fragment(),
        )
        _assert_voice(
            result,
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        assert (
            PrivacyClassifier().classify_tier(result, content=_RULE_INERT_BODY)
            == PrivacyTier.INTIMATE
        )

    def test_a_response_with_no_voice_block_is_still_a_no_op(self) -> None:
        """No ``voice:`` key means no verdict — not a wholly-default merge.

        The early return matters to the caller: ``_apply_classification``
        reads an empty ``updates`` dict as "this run marked nothing", so
        writing a merged-but-unchanged block would be a lie about provenance.
        """
        result = self._classify(
            "frequency:\n  primary: F6\n",
            _seeded_fragment(),
        )
        _assert_voice(
            result,
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        assert result.frequency.primary == Frequency.F6

    def test_what_the_model_did_say_still_wins(self) -> None:
        """The merge must not stop the model from changing its mind."""
        result = self._classify(
            "voice:\n  voice_register: playful\n  confidence: musing\n",
            _seeded_fragment(),
        )
        _assert_voice(
            result,
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.MUSING,
        )


class TestReatomizeAcceptance:
    """Characterises the one downstream behaviour this fix moves (#1331)."""

    def test_an_inherited_phase_now_survives_a_mode_only_child(self) -> None:
        """A re-atomized child keeps the phase it inherited, so it is accepted.

        ``_is_accepted`` rejects any fragment whose ``wavelength.phase`` is
        ``unclassified``, and a split child inherits its parent's whole
        wavelength block. At HEAD a child whose keyword pass fired *mode but
        not phase* had that inherited phase wiped and was rejected, driving
        the recursion a level deeper; a child with **no** keyword hits at all
        kept its phase and was accepted. The two cases now agree, which is
        the point of recording this: the change is a consistency fix, not a
        loosening. ``classification.reatomize`` is opt-in and off by default,
        so the blast radius is bounded.
        """
        child = RuleClassifier().classify(
            _seeded_fragment(),
            content=_MODE_ONLY_BODY,
        )
        assert child.wavelength.phase == Phase.RISING
        assert _is_accepted(child, 1.0, 0.5) is False, (
            "frequency is still unclassified, so acceptance must not turn on "
            "phase alone"
        )

        with_frequency = child.model_copy(
            update={"frequency": FrequencyClassification(primary=Frequency.F3)},
        )
        assert _is_accepted(with_frequency, 1.0, 0.5) is True


def _seed_vault(vault: Path) -> Path:
    """Write one fragment carrying full evidence into *vault*.

    Args:
        vault: Vault root (created on demand).

    Returns:
        Path to the freshly-written fragment file.
    """
    return write_fragment_file(
        vault=vault,
        fragment=_seeded_fragment(),
        body=_REGISTER_ONLY_BODY,
    )


def _run_rules(vault: Path) -> int:
    """Run a forced rules classify over *vault*.

    Args:
        vault: Vault root holding the seeded fragment.

    Returns:
        The number of fragments the run classified.
    """
    return run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=True,
    ).classified


class TestEvidenceOnDisk:
    """What the written frontmatter says after a real ``run_classify``."""

    def test_a_rules_run_writes_the_merged_evidence_and_the_tier(
        self,
        tmp_path: Path,
    ) -> None:
        """The file on disk keeps ``conviction`` and gains ``intimate``.

        Asserted against the written file rather than the returned
        :class:`~creek.models.Fragment` because the write path is
        ``new_metadata.update(fragment.model_dump(mode="json"))`` with **no**
        ``exclude_none``: a nulled confidence really does land in the
        frontmatter as ``confidence: null``, and an in-memory assertion
        cannot see that.

        Args:
            tmp_path: Pytest-provided scratch directory.
        """
        vault = tmp_path / "vault"
        md = _seed_vault(vault)

        assert _run_rules(vault) == 1

        meta = frontmatter.load(md).metadata
        # The half the rules supplied.
        assert meta["voice"]["voice_register"] == "confessional"
        # The half they had no business touching.
        assert meta["voice"]["confidence"] == "conviction"
        # The body says nothing about wavelength, so all six axes stand.
        assert meta["wavelength"]["phase"] == "rising"
        assert meta["wavelength"]["mode"] == "express"
        assert meta["wavelength"]["orientation"] == "feel"
        assert meta["wavelength"]["dosage"] == "medicine"
        assert meta["wavelength"]["color"] == "green"
        assert meta["wavelength"]["descriptor"] == _DESCRIPTOR
        # And the escalation the restored evidence unlocks actually fires.
        assert meta["privacy_tier"] == "intimate"

    def test_a_second_rules_run_keeps_the_tier_and_its_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        """Re-running does not undo the burial or erase what justifies it.

        The engine's stated invariant is that an escalation prevents every
        *future* egress for a fragment. At HEAD it prevents none: each run
        nulls the confidence again, so the tier never rises and the fragment
        routes to the cloud every single time. A tier whose evidence is gone
        is also one re-ingest away from being unexplainable.

        Args:
            tmp_path: Pytest-provided scratch directory.
        """
        vault = tmp_path / "vault"
        md = _seed_vault(vault)

        _run_rules(vault)
        _run_rules(vault)

        meta = frontmatter.load(md).metadata
        assert meta["privacy_tier"] == "intimate"
        assert meta["voice"]["voice_register"] == "confessional"
        assert meta["voice"]["confidence"] == "conviction"
        assert meta["wavelength"]["descriptor"] == _DESCRIPTOR
