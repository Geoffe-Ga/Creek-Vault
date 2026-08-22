"""Pure policy functions for the praxis-potential pass (issue #877).

``creek.classify.praxis_pass`` is the shared, side-effect-free policy
layer that the classify engine and the ``creek process`` pipeline both
call so a fragment never reaches ``04-Praxis`` / ``08-Decisions``
generation still carrying the default ``praxis_potential: none``.

Before #877 **no** pipeline stage ever wrote the field, so all three
consumers that gate on ``explicit``
(:mod:`creek.generate.decisions`, :mod:`creek.generate.mining`,
:mod:`creek.generate.compost`) were structurally unreachable — 100% of
the 35,330-fragment demo vault read ``praxis_potential: none``.

The three functions under test:

* :func:`detect` — score a body and return ``explicit`` or ``none``.
* :func:`escalate` — merge two verdicts, never lowering.
* :func:`apply_praxis` — stamp the merged verdict onto a fragment.

The governing contract is **precision over recall**. The heuristic is
free and runs over every fragment in the vault, so a signal set that
over-fires does not "fix" the bug — it floods mining, compost and the
decisions report with noise that is strictly worse than the current
silence. Hence :data:`_EXPLICIT_AT` = 2: one *strong* marker (a task
checkbox, a ``Decision:`` line) or *two distinct* moderate first-person
commitments. A single bare "I will" must never fire.

The heuristic also never emits ``latent``: "this could become a
practice" is a judgment only the LLM can make (see
:func:`creek.classify.llm.parsing._apply_praxis`).

No vault, no I/O, no LLM: every case here is a direct call.
"""

from __future__ import annotations

import re

import pytest

from creek.classify.praxis_pass import (
    _EXPLICIT_AT,
    _PRAXIS_SIGNALS,
    PRAXIS_POTENTIAL_KEY,
    apply_praxis,
    detect,
    escalate,
)
from creek.models import (
    Fragment,
    FragmentSource,
    PraxisPotential,
    SourcePlatform,
)

_NEUTRAL_BODY = "a plain note about the weather and the walk to the shops"
"""A body carrying no praxis signal at all — the ``none`` control."""

_STRONG_BODIES: tuple[str, ...] = (
    "- [ ] call the dentist",
    "* [ ] call the dentist",
    "- [x] called the dentist",
    "- [X] called the dentist",
    "  - [ ] call the dentist",
    "Decision: move the standup to Tuesday",
    "Decisions: move the standup to Tuesday",
    "Praxis: ten minutes of silence before the first meeting",
    "Next step: call the dentist",
    "Next steps: call the dentist",
    "Action item: call the dentist",
    "Action items: call the dentist",
)
"""Bodies carrying exactly ONE strong (weight-2) marker each."""

_MODERATE_BODIES: tuple[str, ...] = (
    "I should call the dentist.",
    "I will call the dentist.",
    "I need to call the dentist.",
    "I'm going to call the dentist.",
    "Next time I book the appointment earlier.",
    "Next time we book the appointment earlier.",
    "From now on the standup starts at ten.",
    "The practice is ten minutes of silence.",
)
"""Bodies carrying exactly ONE moderate (weight-1) marker each."""

_NEAR_MISS_BODIES: tuple[str, ...] = (
    "You should call the dentist.",
    "It will rain tomorrow.",
    "They will call the dentist.",
    "She should call the dentist.",
    "We should probably eat something.",
)
"""Second-person / third-person lookalikes that must NOT fire.

``praxis_potential`` records the *owner's* commitments. Advice aimed at
someone else, and predictions about the world, are the single largest
class of false positive in a personal corpus full of chat logs.
"""


def _fragment(
    *,
    frag_id: str = "frag-praxisunit01",
    title: str = "A note",
    potential: PraxisPotential = PraxisPotential.NONE,
) -> Fragment:
    """Build a fragment with just the axis :mod:`praxis_pass` reads.

    Args:
        frag_id: Fragment id.
        title: Fragment title (never scored — the signals are body-only).
        potential: The praxis potential already recorded on the fragment.

    Returns:
        The assembled :class:`~creek.models.Fragment`.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        praxis_potential=potential,
    )


class TestSignalTable:
    """The signal table is the whole policy; pin its documented shape."""

    def test_praxis_potential_key_names_a_real_fragment_field(self) -> None:
        """The exported key must match the field the writers serialise.

        Every consumer reads ``praxis_potential`` off frontmatter. A
        typo'd constant would stamp a *new* key and leave the real field
        at its default, reproducing the #877 bug while looking fixed.
        """
        assert PRAXIS_POTENTIAL_KEY == "praxis_potential"
        assert PRAXIS_POTENTIAL_KEY in Fragment.model_fields

    def test_signals_are_precompiled_patterns(self) -> None:
        """Patterns are compiled once at import, not per fragment.

        This table is applied to every body in a 35k-fragment vault; a
        per-call ``re.compile`` is the difference between a free pass and
        a measurable one.
        """
        assert _PRAXIS_SIGNALS, "the signal table must not be empty"
        for pattern in _PRAXIS_SIGNALS:
            assert isinstance(pattern, re.Pattern)

    def test_only_two_documented_weights_exist(self) -> None:
        """Weights are exactly 1 (moderate) and 2 (strong) — both present."""
        weights = set(_PRAXIS_SIGNALS.values())
        assert weights == {1, 2}

    def test_explicit_threshold_is_two(self) -> None:
        """``_EXPLICIT_AT`` is 2: one strong marker, or two moderate hits.

        Lowering this to 1 is the single change that would turn the
        heuristic from a fix into a noise generator, so it is pinned
        explicitly rather than left implicit in the behavioural tests.
        """
        assert _EXPLICIT_AT == 2


class TestDetectStrongSignals:
    """One strong marker (weight 2) is enough on its own."""

    @pytest.mark.parametrize("body", _STRONG_BODIES)
    def test_single_strong_marker_is_explicit(self, body: str) -> None:
        """Each documented strong marker reaches the threshold alone."""
        assert detect(_fragment(), body) is PraxisPotential.EXPLICIT

    def test_strong_marker_fires_on_a_later_line(self) -> None:
        """The line anchor is per-line, not per-body (``re.MULTILINE``).

        Real notes bury the checkbox under a paragraph of prose; a
        body-start anchor would miss essentially every real fragment.
        """
        body = "Some notes about the week ahead.\n\n- [ ] call the dentist"
        assert detect(_fragment(), body) is PraxisPotential.EXPLICIT

    def test_checkbox_must_start_the_line(self) -> None:
        """A mid-line ``- [ ]`` is prose, not a task."""
        assert detect(_fragment(), "see the list - [ ] item") is PraxisPotential.NONE

    def test_checkbox_marker_must_be_blank_or_x(self) -> None:
        """``- [z]`` is not a task checkbox and must not score."""
        assert detect(_fragment(), "- [z] not a checkbox") is PraxisPotential.NONE

    def test_label_must_start_the_line(self) -> None:
        """``The decision: …`` mid-sentence is narration, not a heading.

        Without the line anchor, every retrospective sentence that
        mentions a decision would stamp the fragment ``explicit``.
        """
        body = "The decision: move the standup to Tuesday, was made in April."
        assert detect(_fragment(), body) is PraxisPotential.NONE


class TestDetectModerateSignals:
    """Moderate markers (weight 1) need company before they fire."""

    @pytest.mark.parametrize("body", _MODERATE_BODIES)
    def test_one_moderate_hit_alone_stays_none(self, body: str) -> None:
        """A lone first-person commitment is NOT enough — the core contract.

        This is the most important assertion in the file. "I will call
        the dentist" appears in a substantial fraction of every chat log
        ever written; firing on it would mark most of the vault
        ``explicit`` and drown ``04-Praxis`` / ``08-Decisions`` in noise.
        Precision beats recall: the bug being fixed is "nothing ever
        fires", and the failure mode being avoided is "everything does".
        """
        assert detect(_fragment(), body) is PraxisPotential.NONE

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("I should call the dentist.", "From now on the standup starts at ten."),
            ("I need to sleep earlier.", "Next time I book the appointment earlier."),
            ("I will send the note.", "The practice is ten minutes of silence."),
            ("I'm going to stretch daily.", "Next time we book the room first."),
        ],
    )
    def test_two_distinct_moderate_hits_are_explicit(
        self,
        first: str,
        second: str,
    ) -> None:
        """Two different weight-1 markers sum to the threshold."""
        assert detect(_fragment(), f"{first}\n{second}") is PraxisPotential.EXPLICIT

    def test_one_idiom_repeated_is_still_a_single_signal(self) -> None:
        """Scoring counts distinct patterns, not occurrences of one pattern.

        Two repetitions of the same idiom are a verbal tic, not a second
        piece of evidence. Under occurrence-counting this body would
        score 2 and reach ``explicit``, which would mark ``explicit``
        every chat log where the author says "I should" twice — the
        over-firing failure mode the threshold exists to prevent.
        """
        body = "I should call the dentist.\nI should book the boiler service."

        assert detect(_fragment(), body) is PraxisPotential.NONE

    def test_moderate_marker_must_be_anchored_to_a_sentence_start(self) -> None:
        """Mid-sentence "I should" / "I will" are reported speech, not commitment.

        Two unanchored lookalikes in one line: an implementation that
        matched them anywhere in the body would score 2 and return
        ``explicit``, which is exactly the over-firing this test forbids.
        """
        body = "He said I should call the dentist and that I will regret it."
        assert detect(_fragment(), body) is PraxisPotential.NONE

    def test_moderate_marker_fires_after_a_sentence_boundary(self) -> None:
        """A commitment starting a new sentence mid-line still counts.

        Paired with the anchoring test above, this pins the anchor as
        "sentence or line start" rather than "line start only" — prose
        fragments routinely carry two sentences per line.
        """
        body = "The week got away from me. I should call the dentist. "
        body += "From now on the standup starts at ten."
        assert detect(_fragment(), body) is PraxisPotential.EXPLICIT


class TestDetectNegatives:
    """What the heuristic must stay quiet about."""

    @pytest.mark.parametrize("body", _NEAR_MISS_BODIES)
    def test_near_miss_phrasings_never_fire(self, body: str) -> None:
        """Second/third-person lookalikes score zero."""
        assert detect(_fragment(), body) is PraxisPotential.NONE

    def test_neutral_body_is_none(self) -> None:
        """A plain note carries no praxis potential."""
        assert detect(_fragment(), _NEUTRAL_BODY) is PraxisPotential.NONE

    def test_empty_body_is_none(self) -> None:
        """An empty body cannot be scored and must not crash."""
        assert detect(_fragment(), "") is PraxisPotential.NONE

    @pytest.mark.parametrize(
        "body",
        [
            *_STRONG_BODIES,
            *_MODERATE_BODIES,
            *_NEAR_MISS_BODIES,
            _NEUTRAL_BODY,
            "",
            "Maybe I could start meditating one of these days.",
        ],
    )
    def test_heuristic_never_emits_latent(self, body: str) -> None:
        """``latent`` is an LLM judgment; the keyword pass may not guess it.

        ``latent`` means "there is a practice hiding in here that the
        author has not named". No regex can see that, and a heuristic
        that guessed it would poison the one signal the LLM path exists
        to provide.
        """
        assert detect(_fragment(), body) is not PraxisPotential.LATENT


class TestEscalate:
    """``escalate`` returns the higher of two verdicts — the full matrix."""

    @pytest.mark.parametrize(
        ("current", "candidate", "expected"),
        [
            (PraxisPotential.NONE, PraxisPotential.NONE, PraxisPotential.NONE),
            (PraxisPotential.NONE, PraxisPotential.LATENT, PraxisPotential.LATENT),
            (PraxisPotential.NONE, PraxisPotential.EXPLICIT, PraxisPotential.EXPLICIT),
            (PraxisPotential.LATENT, PraxisPotential.NONE, PraxisPotential.LATENT),
            (PraxisPotential.LATENT, PraxisPotential.LATENT, PraxisPotential.LATENT),
            (
                PraxisPotential.LATENT,
                PraxisPotential.EXPLICIT,
                PraxisPotential.EXPLICIT,
            ),
            (PraxisPotential.EXPLICIT, PraxisPotential.NONE, PraxisPotential.EXPLICIT),
            (
                PraxisPotential.EXPLICIT,
                PraxisPotential.LATENT,
                PraxisPotential.EXPLICIT,
            ),
            (
                PraxisPotential.EXPLICIT,
                PraxisPotential.EXPLICIT,
                PraxisPotential.EXPLICIT,
            ),
        ],
    )
    def test_escalate_matrix(
        self,
        current: PraxisPotential,
        candidate: PraxisPotential,
        expected: PraxisPotential,
    ) -> None:
        """none < latent < explicit; the winner is always the higher side."""
        assert escalate(current, candidate) is expected

    def test_escalate_is_symmetric_in_its_arguments(self) -> None:
        """Argument order cannot decide whether a verdict is lowered.

        A non-symmetric merge would let the caller's argument order
        silently demote an ``explicit`` fragment back to ``none`` — the
        one direction :func:`escalate` exists to make impossible.
        """
        ladder = (
            PraxisPotential.NONE,
            PraxisPotential.LATENT,
            PraxisPotential.EXPLICIT,
        )
        for left in ladder:
            for right in ladder:
                assert escalate(left, right) is escalate(right, left)


class TestApplyPraxis:
    """``apply_praxis`` stamps the merged verdict, escalate-only."""

    def test_stamps_explicit_on_a_signal_bearing_body(self) -> None:
        """A checkbox body lifts a default fragment to ``explicit``."""
        result = apply_praxis(_fragment(), "- [ ] call the dentist")

        assert PraxisPotential(result.praxis_potential) is PraxisPotential.EXPLICIT

    def test_stored_value_is_a_plain_string(self) -> None:
        """The stored value is ``"explicit"``, not a bare enum member.

        ``Fragment.model_config`` sets ``use_enum_values=True``, but
        ``model_copy`` bypasses that coercion — and every consumer
        compares ``str(frag.praxis_potential) ==
        PraxisPotential.EXPLICIT.value`` (see
        ``creek/generate/decisions.py:307``). Storing the member rather
        than its ``.value`` also hands ``yaml.SafeDumper`` something it
        cannot represent on the tier-only write path.
        """
        result = apply_praxis(_fragment(), "- [ ] call the dentist")

        assert type(result.praxis_potential) is str
        assert result.praxis_potential == PraxisPotential.EXPLICIT.value
        assert str(result.praxis_potential) == PraxisPotential.EXPLICIT.value

    def test_returns_the_same_object_when_nothing_escalates(self) -> None:
        """No escalation means no copy — identity, not just equality.

        The engine counts "praxis marked" by comparing before/after, and
        the pipeline runs this over every fragment in the vault. An
        unconditional ``model_copy`` would both blur that counter and
        allocate a fresh model per fragment for nothing.
        """
        fragment = _fragment()

        assert apply_praxis(fragment, _NEUTRAL_BODY) is fragment

    def test_returns_the_same_object_when_already_explicit(self) -> None:
        """An already-``explicit`` fragment with a neutral body is untouched."""
        fragment = _fragment(potential=PraxisPotential.EXPLICIT)

        assert apply_praxis(fragment, _NEUTRAL_BODY) is fragment

    def test_never_demotes_an_explicit_fragment(self) -> None:
        """A body with no signals cannot lower a verdict already on record.

        The LLM (or the operator) may have set ``explicit`` from evidence
        the keyword table cannot see; a later rules run must not undo it.
        """
        fragment = _fragment(potential=PraxisPotential.EXPLICIT)

        result = apply_praxis(fragment, _NEUTRAL_BODY)

        assert PraxisPotential(result.praxis_potential) is PraxisPotential.EXPLICIT

    def test_never_demotes_a_latent_fragment(self) -> None:
        """``latent`` survives a rules pass that finds nothing."""
        fragment = _fragment(potential=PraxisPotential.LATENT)

        result = apply_praxis(fragment, _NEUTRAL_BODY)

        assert PraxisPotential(result.praxis_potential) is PraxisPotential.LATENT

    def test_raises_latent_to_explicit(self) -> None:
        """A ``latent`` fragment whose body carries a task becomes ``explicit``."""
        fragment = _fragment(potential=PraxisPotential.LATENT)

        result = apply_praxis(fragment, "- [ ] call the dentist")

        assert PraxisPotential(result.praxis_potential) is PraxisPotential.EXPLICIT

    def test_is_idempotent(self) -> None:
        """Applying twice equals applying once, and the second call is a no-op.

        ``creek classify`` is re-run over the whole vault routinely; a
        pass that kept producing new objects (or new values) on every run
        would churn every file's frontmatter forever.
        """
        body = "- [ ] call the dentist"
        once = apply_praxis(_fragment(), body)

        twice = apply_praxis(once, body)

        assert twice is once
        assert twice.model_dump(mode="json") == once.model_dump(mode="json")

    def test_touches_no_other_fragment_field(self) -> None:
        """Only ``praxis_potential`` moves — the rest round-trips unchanged."""
        fragment = _fragment()

        result = apply_praxis(fragment, "- [ ] call the dentist")

        before = fragment.model_dump(mode="json")
        after = result.model_dump(mode="json")
        assert after.pop(PRAXIS_POTENTIAL_KEY) == PraxisPotential.EXPLICIT.value
        assert before.pop(PRAXIS_POTENTIAL_KEY) == PraxisPotential.NONE.value
        assert after == before
