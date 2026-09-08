"""Every tier gate in this unit reads the tier canonically (#1752, #1742, #1743).

``Fragment.model_config`` sets ``use_enum_values=True``, so ``privacy_tier``
holds a plain ``str`` at runtime even though its annotation says
:class:`~creek.models.PrivacyTier`. Because :class:`PrivacyTier` is a
``StrEnum``, ``==`` against a member is ``True`` for the bare string while
``is`` is ``False`` — and mypy sees neither, because the annotation is the
lie. The four defects these issues name are that one foot-gun's yield, not
four independent mistakes.

Every assertion here is therefore a **type** pin (``type(...) is
PrivacyTier``), an **identity** pin (``is PrivacyTier.PERSONAL``), or an
exact-string equality on a redaction output. Deliberately never an enum
``==``: an ``==`` assertion passes today over all four bugs, which is
precisely how they survived #1489.

Two construction traps, both hit while probing, and both fatal to the value
of a test that steps in them:

* Build the unrecognised-tier fragment with ``model_copy(update=...)`` on a
  **validated** fragment. ``Fragment.model_validate`` rejects
  ``'super-secret'`` outright, so a vault file is vacuous; and
  ``model_construct(**model_dump())`` leaves ``source`` a plain ``dict``, so
  ``_eligible_register`` dies on ``fragment.source.author`` before it ever
  reaches the gate under test — a fake red.
* Voice fields live at ``fragment.voice.voice_register`` /
  ``fragment.voice.confidence``, not at the top level, and ``voice_weight``
  must be ``> 0``. A fragment missing any of them is refused two gates
  earlier and the voice assertions pass vacuously.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from creek.author.agents import fragment_tier_map
from creek.classify.classify_engine import TierClassifiers
from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    fragment_tier,
    tier_of,
    tier_within_override,
)
from creek.compile.engine import _fragment_excerpt_for_prompt, _routing_tier_for
from creek.generate.voice import _eligible_register
from creek.models import Fragment, PrivacyTier
from creek_mcp.tier_ceiling import TierCeiling, routing_tier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from creek.classify.llm.orchestrator import LLMClassifier

_UNRECOGNISED = "super-secret"
"""A tier string no table in the codebase has ever heard of."""

_BASE: dict[str, Any] = {
    "id": "f",
    "title": "T",
    "source": {"platform": "journal", "author": "self"},
    "privacy_tier": "intimate",
    "voice_weight": 1.0,
    "voice": {"voice_register": "confessional", "confidence": "settled"},
}
"""Frontmatter for a fragment that clears every voice-corpus gate but the tier one."""


def _valid(tier: str = "intimate") -> Fragment:
    """Return a fully validated fragment carrying *tier*.

    Args:
        tier: A tier string ``Fragment.model_validate`` accepts.

    Returns:
        The validated fragment.
    """
    return Fragment.model_validate(_BASE | {"privacy_tier": tier})


def _corrupt() -> Fragment:
    """Return a fragment carrying an unrecognised tier, via the only channel that can.

    Returns:
        A validated fragment whose ``privacy_tier`` was overwritten with
        :data:`_UNRECOGNISED` through ``model_copy(update=...)`` — the shape
        ``model_validate`` refuses to build and the only bypass production
        has (four sites, all writing genuine members today).
    """
    return _valid().model_copy(update={"privacy_tier": _UNRECOGNISED})


def _seed_fragment(vault: Path, frag_id: str, tier: PrivacyTier) -> None:
    """Write one minimal fragment file stamped with *tier* under ``01-Fragments``.

    Args:
        vault: Vault root to seed under.
        frag_id: Fragment id; also the file stem.
        tier: The tier to stamp into the frontmatter.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "t"\n'
        f"privacy_tier: {tier.value}\n"
        f"source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )


class TestFragmentTierIsCanonical:
    """``fragment_tier`` honours its own ``-> PrivacyTier`` annotation (#1752 root)."""

    @pytest.mark.parametrize("tier", list(PrivacyTier))
    def test_returns_a_member_for_every_tier_in_the_product(
        self, tier: PrivacyTier
    ) -> None:
        """A member comes back for every tier, not the model's bare ``str``.

        The type pin is the whole test. ``fragment_tier(...) ==
        PrivacyTier.INTIMATE`` passes today over the bug, because a
        ``StrEnum`` member equals its own value.

        Args:
            tier: Each member of the full ``PrivacyTier`` product.
        """
        fragment = _valid(tier.value)

        read = fragment_tier(fragment, {"privacy_tier": tier.value})

        assert type(read) is PrivacyTier

    def test_missing_key_still_fails_all_the_way_closed(self) -> None:
        """No ``privacy_tier`` key in the raw file still reads as INTIMATE.

        The distinction between a *missing* key and an *explicit*
        ``unclassified`` is load-bearing policy (#876/#961): the former is a
        file nobody has vouched for and fails closed to INTIMATE, the latter
        ranks with ``personal``. Coercing the other return must not collapse
        the two.
        """
        fragment = _valid("unclassified")

        assert fragment_tier(fragment, {}) is PrivacyTier.INTIMATE
        assert fragment_tier(fragment, {"privacy_tier": "unclassified"}) is (
            PrivacyTier.UNCLASSIFIED
        )

    @pytest.mark.parametrize(
        "raw_tier", [*(t.value for t in PrivacyTier), _UNRECOGNISED, "public"]
    )
    def test_agrees_with_tier_of_tier_for_tier(self, raw_tier: str) -> None:
        """The two halves of one fail-closed reading cannot drift apart.

        ``tier_of`` fails closed on a tier string it does not recognise;
        ``fragment_tier`` on a tier that was never written down. Asserting
        they agree everywhere else is what lets ``fragment_tier`` delegate
        rather than grow a second copy of the guard.

        Args:
            raw_tier: Every recognised tier value, the legacy ``public``
                alias, and an unrecognised string.
        """
        fragment = _valid().model_copy(update={"privacy_tier": raw_tier})

        assert fragment_tier(fragment, {"privacy_tier": raw_tier}) is tier_of(fragment)


class TestRoutingVocabulary:
    """``routing_tier`` answers in the routing vocabulary — for strings too (#1752)."""

    def test_an_ordinary_unclassified_fragment_routes_as_personal(self) -> None:
        """The live defect: no bypass, no corruption, just an unclassified note.

        Every not-yet-classified pipeline-written fragment carries
        ``privacy_tier: unclassified``. Fed through ``fragment_tier`` it
        arrived at ``routing_tier`` as the bare string ``'unclassified'``,
        which ``_routable_tier``'s ``is`` comparison did not recognise, so
        the ``max`` returned it verbatim — a fourth key
        :class:`~creek.classify.llm.router.ModelRouter` has no rule for.
        """
        unc = _valid("unclassified")

        assert (
            routing_tier(
                TierCeiling.OPEN,
                fragment_tier(unc, {"privacy_tier": "unclassified"}),
            )
            is PrivacyTier.PERSONAL
        )

    def test_an_unrecognised_string_routes_local_only(self) -> None:
        """An unrecognised tier fails closed to INTIMATE, i.e. local-only.

        This is the half a bare ``is``-to-``==`` swap would fail. Without
        coercion the string keeps sensitivity 2 from ``tier_sensitivity`` but
        is returned *verbatim* by the ``max`` under any sub-intimate ceiling,
        and ``ModelRouter._enforce_local_for_intimate`` reads ``tier !=
        PrivacyTier.INTIMATE`` as ``True`` and permits cloud — the exact hole
        ``_routable_tier``'s docstring says the invariant exists to close.
        """
        assert (
            routing_tier(TierCeiling.OPEN, cast("PrivacyTier", _UNRECOGNISED))
            is PrivacyTier.INTIMATE
        )

    @pytest.mark.parametrize("form", [PrivacyTier.UNCLASSIFIED, "unclassified"])
    def test_unclassified_normalises_in_both_forms(self, form: object) -> None:
        """The enum member and the bare string normalise identically.

        Args:
            form: The member, then the runtime shape a ``Fragment``
                actually carries.
        """
        assert (
            routing_tier(TierCeiling.OPEN, cast("PrivacyTier", form))
            is PrivacyTier.PERSONAL
        )


class TestCompileExcerptGate:
    """``_fragment_excerpt_for_prompt`` reads the canonical tier (#1742)."""

    def test_an_unrecognised_tier_never_yields_the_body(self) -> None:
        """FEAT-003: an intimate body must never reach the LLM prompt.

        Before the fix this returned the full body verbatim — the fragment
        went into ``_build_prompt``'s ``body:`` field — while ``tier_of`` on
        the same fragment reported INTIMATE.
        """
        assert (
            _fragment_excerpt_for_prompt(_corrupt(), "FULL INTIMATE BODY TEXT")
            == "[Intimate-tier summary: T]"
        )

    def test_the_personal_branch_still_summarises(self) -> None:
        """A deletion-killer for the PERSONAL branch, not a mutation-killer.

        No input can distinguish a fixed PERSONAL comparison from an unfixed
        one once the INTIMATE one is fixed: ``tier_of`` differs from the raw
        attribute only on the legacy ``public`` alias, which
        ``model_validate`` normalises to ``open`` before it can reach a
        fragment, and an unrecognised string is caught one branch earlier.
        What this pins is that the branch still *exists*.
        """
        personal = _valid().model_copy(update={"privacy_tier": "personal"})

        assert _fragment_excerpt_for_prompt(personal, "BODY") == (
            "[Personal-tier summary: T]"
        )

    def test_the_tier_is_read_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both branches read one binding, which is what makes "both sites" true.

        Structural rather than behavioural on purpose. Since the two
        comparisons cannot be told apart by any input, correctness of the
        second is guaranteed by there being only one read to get wrong.
        """
        from creek.compile import engine

        calls: list[Fragment] = []
        real = engine.tier_of

        def _counting(fragment: Fragment) -> PrivacyTier:
            calls.append(fragment)
            return real(fragment)

        monkeypatch.setattr(engine, "tier_of", _counting)
        personal = _valid().model_copy(update={"privacy_tier": "personal"})

        assert engine._fragment_excerpt_for_prompt(personal, "BODY") == (
            "[Personal-tier summary: T]"
        )
        assert len(calls) == 1

    def test_the_keyless_residual_is_caught_by_the_routing_gate(self) -> None:
        """A keyless file still yields its body here — and still compiles local-only.

        The excerpt gate and the routing gate disagree about a fragment whose
        *file* carries no ``privacy_tier`` key: ``tier_of`` sees the model's
        ``unclassified`` default and lets the body through, because the raw
        frontmatter this function would need is dropped by ``compile_to_vault``
        before ``compile_fragments`` is reached. ``_routing_tier_for`` is the
        protection — it reads through ``fragment_tier``, which *does* get the
        raw mapping, and fails the whole compile closed to INTIMATE, forcing
        local routing. Documented in the excerpt function's docstring and
        pinned here rather than closed, because threading ``raw`` through
        would change a public signature with no privacy gain.
        """
        keyless = _valid("unclassified")

        assert _fragment_excerpt_for_prompt(keyless, "BODY") == "BODY"
        assert _routing_tier_for([(keyless, "BODY", {})], []) is PrivacyTier.INTIMATE


class TestVoiceConsentGate:
    """``_eligible_register`` reads the canonical tier (#1743)."""

    def test_an_unrecognised_tier_is_refused(self) -> None:
        """A tier nobody can vouch for must not seed the voice corpus.

        Canonical-reader consistency, not a reachable exposure: #1743's
        stated reason — that the loader is an injection channel — is false.
        The loader is ``_load_fragment_with_body``, and the value it writes
        comes from ``raw_privacy_tier``, which returns a genuine member on
        every branch.
        """
        assert _eligible_register(_corrupt(), allow_intimate=False) is None

    def test_an_unclassified_model_is_still_admitted(self) -> None:
        """Refusing UNCLASSIFIED here would refuse every unclassified vault's corpus.

        The consent gate takes a validated fragment and never sees raw
        frontmatter, so it cannot tell "no key on disk" from "the key said
        ``unclassified``". Pinned here as well as at
        ``tests/test_voice_exemplars.py`` because the canonical-reader change
        runs straight through it.
        """
        unc = _valid().model_copy(update={"privacy_tier": "unclassified"})

        assert _eligible_register(unc, allow_intimate=False) == "confessional"

    @pytest.mark.parametrize("allow_intimate", [False, True])
    def test_a_genuine_member_behaves_as_before(self, allow_intimate: bool) -> None:
        """The one fragment in the process whose field is not a ``str``.

        ``_load_fragment_with_body`` overwrites ``privacy_tier`` with
        ``raw_privacy_tier(metadata)`` through ``model_copy``, which does not
        re-run ``use_enum_values``, so that fragment carries an actual
        member. Refused at ``allow_intimate=False``, admitted at ``True`` —
        the operator's opt-in to their own intimate writing survives.

        Args:
            allow_intimate: The operator's opt-in, both ways.
        """
        member = _valid().model_copy(update={"privacy_tier": PrivacyTier.INTIMATE})

        result = _eligible_register(member, allow_intimate=allow_intimate)

        assert result == ("confessional" if allow_intimate else None)


class TestAdmissionAndClassifierGates:
    """The two gates that took a raw attribute straight into a bare table lookup."""

    @pytest.mark.parametrize(
        ("override", "admitted"),
        [
            (PrivacyTierOverride.OPEN, False),
            (PrivacyTierOverride.PERSONAL, False),
            (PrivacyTierOverride.INTIMATE, True),
            (PrivacyTierOverride.ALL, True),
        ],
    )
    def test_an_unrecognised_tier_is_admitted_only_where_intimate_is(
        self, override: PrivacyTierOverride, admitted: bool
    ) -> None:
        """An unknown tier ranks *with* intimate rather than raising.

        ``_TIER_RANK[tier]`` was a bare index, so an unrecognised tier raised
        ``KeyError`` across a caller's boundary at the three rank-comparing
        ceilings, and returned ``True`` at ``ALL`` through the early return.
        Reading through ``tier_sensitivity`` gives rank 2 — the same rank as
        INTIMATE — so it is refused at ``open``/``personal`` and admitted at
        ``intimate``. Not blanket refusal: ``ALL`` means admit everything,
        this function is admission and not routing, and INTIMATE enforcement
        lives in the routing gate.

        Args:
            override: Each of the four ceilings.
            admitted: Whether an unrecognised tier is admitted there.
        """
        assert (
            tier_within_override(cast("PrivacyTier", _UNRECOGNISED), override)
            is admitted
        )

    @pytest.mark.parametrize("tier", list(PrivacyTier))
    def test_recognised_tiers_are_admitted_exactly_as_before(
        self, tier: PrivacyTier
    ) -> None:
        """The rank cutoff is unchanged for every tier the table knows.

        Args:
            tier: Each member of the full ``PrivacyTier`` product.
        """
        assert tier_within_override(tier, PrivacyTierOverride.PERSONAL) is (
            tier is not PrivacyTier.INTIMATE
        )

    def test_a_bare_string_intimate_reaches_the_local_classifier(self) -> None:
        """``TierClassifiers.for_tier`` must not route intimate by identity.

        ``'intimate' is not PrivacyTier.INTIMATE`` is ``True``, so the bare
        string the model actually carries selected ``non_intimate`` — the
        possibly-cloud classifier — for intimate content. Every caller reads
        through ``tier_of`` today, so this was safe by caller discipline
        alone; that is exactly the tripwire the issue names.
        """
        classifiers = TierClassifiers(
            non_intimate=cast("LLMClassifier", "NON-INTIMATE"),
            intimate=cast("LLMClassifier", "INTIMATE"),
            routing_error=None,
        )

        assert classifiers.for_tier(cast("PrivacyTier", "intimate")) == "INTIMATE"


class TestEveryGateAgreesOnAnUnrecognisedTier:
    """One corrupt fragment through every gate in the unit, most-restrictive at each.

    The behavioural replacement for a source-shape guard. A syntactic
    matcher over ``fragment.privacy_tier`` reads would have caught the two
    comparison sites and missed both the bare ``return`` in ``fragment_tier``
    and the parameter comparison in ``_routable_tier`` — two of the four —
    while reading as "this cannot come back". This covers six of six by
    behaviour instead.
    """

    def test_the_root_reader_fails_closed(self) -> None:
        """``fragment_tier`` reports INTIMATE for a tier it cannot recognise."""
        assert (
            fragment_tier(_corrupt(), {"privacy_tier": _UNRECOGNISED})
            is PrivacyTier.INTIMATE
        )

    def test_routing_is_local_only(self) -> None:
        """The routing key is INTIMATE, so the call cannot leave the machine."""
        assert (
            routing_tier(
                TierCeiling.ALL,
                fragment_tier(_corrupt(), {"privacy_tier": _UNRECOGNISED}),
            )
            is PrivacyTier.INTIMATE
        )

    def test_the_compile_prompt_carries_no_body(self) -> None:
        """The compile prompt gets a title-only summary."""
        assert _fragment_excerpt_for_prompt(_corrupt(), "SECRET") == (
            "[Intimate-tier summary: T]"
        )

    def test_the_voice_corpus_refuses_it(self) -> None:
        """The voice corpus refuses it without the operator's intimate opt-in."""
        assert _eligible_register(_corrupt(), allow_intimate=False) is None

    def test_admission_refuses_it_below_intimate(self) -> None:
        """The hard admission cutoff refuses it at the default ceiling."""
        assert (
            tier_within_override(
                cast("PrivacyTier", _UNRECOGNISED), PrivacyTierOverride.OPEN
            )
            is False
        )

    def test_classification_routes_it_to_the_local_classifier(self) -> None:
        """Classification of the corrupt fragment goes to the intimate route."""
        classifiers = TierClassifiers(
            non_intimate=cast("LLMClassifier", "NON-INTIMATE"),
            intimate=cast("LLMClassifier", "INTIMATE"),
            routing_error=None,
        )

        assert (
            classifiers.for_tier(
                fragment_tier(_corrupt(), {"privacy_tier": _UNRECOGNISED})
            )
            == "INTIMATE"
        )


class TestTheOneNonEscalatingConsequence:
    """The legacy ``public`` alias now reads as OPEN rather than failing closed.

    Recorded as a decision, not discovered later. Reading through the
    canonical helpers means ``PrivacyTier._missing_`` (INC-003) applies, so
    ``'public'`` — previously an unrecognised string that failed closed to
    INTIMATE at ``fragment_tier`` — now resolves to ``OPEN``. It is the one
    change in this unit that is not an escalation, and it is unreachable
    from a validated fragment.
    """

    def test_no_validated_fragment_can_carry_public(self) -> None:
        """Pydantic normalises the alias before a fragment can ever hold it."""
        validated = Fragment.model_validate(_BASE | {"privacy_tier": "public"})

        assert validated.privacy_tier == "open"

    def test_the_alias_reads_as_open_through_the_canonical_readers(self) -> None:
        """Every other canonical reader already read it this way.

        ``tier_of``, ``raw_privacy_tier`` and ``frontmatter_tier`` all apply
        exactly this reading, so the change ends a divergence rather than
        creating one. Reachable only through ``model_copy(update=...)``,
        which no production site uses this way.
        """
        aliased = _valid().model_copy(update={"privacy_tier": "public"})

        assert fragment_tier(aliased, {"privacy_tier": "public"}) is PrivacyTier.OPEN


class TestAdmittedCorpusTierMap:
    """``fragment_tier_map`` honours its ``dict[str, PrivacyTier]`` annotation."""

    def test_the_map_holds_members_not_bare_strings(self, tmp_path: Path) -> None:
        """The second annotation lie: the values were plain ``str``.

        Behaviour-neutral for its consumer
        (:func:`creek.author.conductor` checks membership by value), and the
        admitted/last-wins semantics the leak gate deliberately diverges from
        are untouched.

        Args:
            tmp_path: Vault root for the seeded corpus.
        """
        _seed_fragment(tmp_path, "frag-a", PrivacyTier.OPEN)

        tier_map = fragment_tier_map(tmp_path, PrivacyTierOverride.OPEN)

        assert tier_map == {"frag-a": PrivacyTier.OPEN}
        assert [type(v) for v in tier_map.values()] == [PrivacyTier]
