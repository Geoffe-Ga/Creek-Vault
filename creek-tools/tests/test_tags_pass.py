"""Pure policy functions for the hashtag ``tags`` pass (issue #878).

``creek.classify.tags_pass`` is the shared, side-effect-free policy layer
that the ingest chokepoint (:func:`creek.ingest.base.assemble_ingested_fragment`)
and the classify engine both call so a fragment never reaches the Tag
Garden still carrying the default ``tags: []``.

Before #878 :attr:`creek.models.Fragment.tags` had essentially **no
producer** — the sole writer was
:mod:`creek.clean.context`, which appends the literal ``low-priority`` in
one non-default clean mode. Empirically, 2000/2000 sampled fragments of
the operator's 35,330-fragment vault carried ``tags: []``, and
``00-Creek-Meta/Tag-Garden.md`` read ``*No tags found in vault.*``. Three
consumers were therefore dead: :mod:`creek.generate.tags` (the Tag
Garden), :mod:`creek.lint.checks.tags` (the orphan-tag lint) and the
tag-driven branches of :mod:`creek.generate.compost`.

The three functions under test:

* :func:`extract` — pull hashtags out of a markdown body.
* :func:`merge` — union two tag lists, never losing one.
* :func:`apply_tags` — stamp the merged list onto a fragment.

The governing contract is **precision over recall**, for a reason this
corpus makes concrete. Discord exports render channel mentions as
``#<snowflake>``: in a 3000-file sample of the operator's vault
``#c762760820929069057`` appears 688 times and ``#c942223408115122187``
539 times. A naive ``#\\w+`` extractor would make those the two most
common "tags" in the entire vault and bury every real one. Two rules
exist solely to kill that class — a candidate needs at least **two**
alphabetic characters, and may not contain a run of **eight or more**
consecutive digits — and each has its own named test below.

Normalisation is deliberately shallow: lowercase, ``_`` → ``-``,
collapse repeated ``-``, strip the ends. camelCase is **not** split, so
``#ArchetypalWavelength`` becomes ``archetypalwavelength`` rather than
``archetypal-wavelength``; splitting it would be a guess, and a wrong
guess fragments the tag vocabulary it is supposed to consolidate.

No vault, no I/O, no LLM: every case here is a direct call.
"""

from __future__ import annotations

import re

import pytest

from creek.classify.tags_pass import (
    _CODE_FENCE_RE,
    _INLINE_CODE_RE,
    _MAX_TAGS,
    _TAG_RE,
    TAGS_KEY,
    apply_tags,
    extract,
    has_unrecorded_tags,
    merge,
)
from creek.models import (
    Fragment,
    FragmentSource,
    SourcePlatform,
)

_NEUTRAL_BODY = "a plain note about the weather and the walk to the shops"
"""A body carrying no hashtag at all — the empty-list control."""

_POSITIVE_CASES: tuple[tuple[str, list[str]], ...] = (
    ("a note about #recovery", ["recovery"]),
    ("filed under #APTITUDE", ["aptitude"]),
    ("the #family-business again", ["family-business"]),
    ("the #Family_Business again", ["family-business"]),
    ("see #project/creek for the plan", ["project/creek"]),
    ("an #e2e run", ["e2e"]),
    ("some #i18n work", ["i18n"]),
    ("living with #c-ptsd", ["c-ptsd"]),
)
"""``(body, expected tags)`` for every documented positive shape.

Covers the four normalisation rules (lowercase, ``_`` → ``-``, dash
preservation, nested Obsidian ``/`` preservation) and the two short-tag
shapes — ``#e2e`` and ``#i18n`` — that a "must be mostly letters" rule
would wrongly discard.
"""

_SNOWFLAKE_BODIES: tuple[str, ...] = (
    "posted in #c762760820929069057 last night",
    "and again in #c942223408115122187 this morning",
)
"""Real Discord channel-mention snowflakes from the operator's vault.

688 and 539 occurrences respectively in a 3000-file sample. These are
the single largest false-positive class this extractor must reject.
"""

_NEGATIVE_BODIES: tuple[str, ...] = (
    "# Heading",
    "## Heading",
    "#123",
    "see https://x.com/a#anchor for the thread",
    "the foo#bar identifier",
    "###",
    "a bare # on its own",
    "#-leading-dash",
)
"""Bodies whose ``#`` must NOT produce a tag.

The markdown headings are the reason the ``#`` must be followed
immediately by a letter; the URL fragment and ``foo#bar`` are the reason
it must be preceded by whitespace or a line start.
"""


def _fragment(
    *,
    frag_id: str = "frag-tagsunit0001",
    title: str = "A note",
    tags: list[str] | None = None,
) -> Fragment:
    """Build a fragment with just the axis :mod:`tags_pass` reads.

    Args:
        frag_id: Fragment id.
        title: Fragment title (never scanned — hashtags are body-only,
            and a title like "# Hello" is a heading, not a tag).
        tags: The tags already recorded on the fragment.

    Returns:
        The assembled :class:`~creek.models.Fragment`.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        tags=tags if tags is not None else [],
    )


class TestExtractionTable:
    """The pattern table is the whole policy; pin its documented shape."""

    def test_tags_key_names_a_real_fragment_field(self) -> None:
        """The exported key must match the field the writers serialise.

        Every consumer reads ``tags`` off frontmatter. A typo'd constant
        would stamp a *new* key and leave the real field at its default,
        reproducing the #878 bug while looking fixed.
        """
        assert TAGS_KEY == "tags"
        assert TAGS_KEY in Fragment.model_fields

    def test_patterns_are_precompiled(self) -> None:
        """Patterns are compiled once at import, not per fragment.

        This table is applied to every body in a 35k-fragment vault, on
        both the ingest and the classify path; a per-call ``re.compile``
        is the difference between a free pass and a measurable one.
        """
        for pattern in (_TAG_RE, _CODE_FENCE_RE, _INLINE_CODE_RE):
            assert isinstance(pattern, re.Pattern)

    def test_max_tags_is_thirty_two(self) -> None:
        """``_MAX_TAGS`` is 32 — the per-fragment ceiling.

        Pinned explicitly rather than left implicit in the truncation
        test: the cap is what stops a single pathological body (a
        tag-index note, a scraped page) from writing hundreds of keys
        into one fragment's frontmatter.
        """
        assert _MAX_TAGS == 32


class TestExtractPositives:
    """What the extractor must find, and what it must normalise it to."""

    @pytest.mark.parametrize(("body", "expected"), _POSITIVE_CASES)
    def test_documented_shapes_extract_and_normalise(
        self,
        body: str,
        expected: list[str],
    ) -> None:
        """Each documented positive shape yields its normalised tag."""
        assert extract(body) == expected

    def test_camel_case_is_deliberately_not_split(self) -> None:
        """``#ArchetypalWavelength`` lowercases; it does not become two words.

        Pinned so nobody "improves" this later. Splitting on case is a
        guess, and a wrong guess (``#APTITUDE`` → ``a-p-t-i-t-u-d-e``,
        ``#c-PTSD`` → ``c-p-t-s-d``) fragments the very vocabulary the
        Tag Garden exists to consolidate.
        """
        assert extract("on #ArchetypalWavelength today") == ["archetypalwavelength"]

    def test_repeated_dashes_collapse_and_the_tail_is_stripped(self) -> None:
        """``#foo--bar-`` normalises to ``foo-bar``.

        Without the collapse, ``#foo--bar`` and ``#foo-bar`` would be two
        distinct tags in the garden that no reader could tell apart.
        """
        assert extract("filed as #foo--bar- here") == ["foo-bar"]

    def test_underscores_and_dashes_normalise_to_the_same_tag(self) -> None:
        """``#Family_Business`` and ``#family-business`` are one tag, not two.

        Extracted from one body so the dedup and the normalisation are
        proven together: a normaliser that ran *after* dedup would emit
        the tag twice.
        """
        assert extract("#Family_Business and #family-business") == ["family-business"]

    def test_first_seen_order_is_preserved(self) -> None:
        """Tags come back in body order, not sorted and not reversed.

        Body order is what makes the ingest test's
        ``== ["recovery", "writing"]`` a real assertion rather than an
        accident of alphabetisation.
        """
        assert extract("#writing then #recovery then #apple") == [
            "writing",
            "recovery",
            "apple",
        ]

    def test_the_same_tag_twice_in_one_body_is_recorded_once(self) -> None:
        """Repetition is emphasis, not a second tag — and case does not matter."""
        assert extract("#recovery is #Recovery is #RECOVERY") == ["recovery"]

    def test_a_tag_starting_a_later_line_is_found(self) -> None:
        """The line anchor is per-line, not per-body (``re.MULTILINE``).

        Real notes put their tags on a trailing line of their own; a
        body-start anchor would miss essentially every one of them.
        """
        assert extract("Some notes about the week.\n\n#recovery #writing") == [
            "recovery",
            "writing",
        ]

    def test_a_tag_at_the_maximum_length_survives(self) -> None:
        """A 64-character tag is inside the bound and is kept."""
        longest = "a" * 64

        assert extract(f"filed under #{longest} here") == [longest]

    def test_the_per_fragment_cap_truncates_the_tail(self) -> None:
        """A body with 40 distinct tags yields the first ``_MAX_TAGS``.

        First-seen order decides which survive, so the truncation is
        deterministic across re-runs — a set-based implementation would
        churn the frontmatter of the same fragment on every ingest.
        """
        candidates = [f"tag-a{i:02d}" for i in range(40)]
        body = " ".join(f"#{tag}" for tag in candidates)

        result = extract(body)

        assert result == candidates[:_MAX_TAGS]
        assert len(result) == 32


class TestExtractRejectsDiscordSnowflakes:
    """The precision rules that keep a Discord export out of the garden."""

    @pytest.mark.parametrize("body", _SNOWFLAKE_BODIES)
    def test_channel_mention_snowflakes_yield_no_tags(self, body: str) -> None:
        """The two most common ``#``-tokens in the vault produce nothing.

        ``#c762760820929069057`` (688 occurrences in a 3000-file sample)
        and ``#c942223408115122187`` (539) are Discord channel mentions.
        Under a naive ``#\\w+`` extractor they would be the top two tags
        in the entire Tag Garden, which is a worse outcome than the empty
        garden #878 is fixing.
        """
        assert extract(body) == []

    def test_a_candidate_needs_at_least_two_alphabetic_characters(self) -> None:
        """One letter is not a word — it is a Discord id prefix.

        This is the load-bearing half of the snowflake rule: every
        channel mention is exactly one letter followed by digits. The
        companion assertion pins that the rule is "two letters", not
        "mostly letters", so ``#e2e`` and ``#i18n`` still survive.
        """
        assert extract("in #c762760820929069057 last night") == []
        assert extract("an #a1 identifier") == []
        assert extract("an #e2e run and some #i18n work") == ["e2e", "i18n"]

    def test_a_run_of_eight_or_more_digits_is_rejected(self) -> None:
        """Long digit runs are ids, timestamps and phone numbers, not tags.

        The boundary is asserted from both sides so an off-by-one cannot
        pass: seven consecutive digits is still a tag, eight is not.
        """
        assert extract("build #ab1234567 passed") == ["ab1234567"]
        assert extract("build #ab12345678 passed") == []


class TestExtractNegatives:
    """What the extractor must stay quiet about."""

    @pytest.mark.parametrize("body", _NEGATIVE_BODIES)
    def test_documented_negatives_yield_no_tags(self, body: str) -> None:
        """Headings, bare numbers, URL fragments and infixes are not tags."""
        assert extract(body) == []

    def test_neutral_body_yields_no_tags(self) -> None:
        """A plain note carries no tags."""
        assert extract(_NEUTRAL_BODY) == []

    def test_empty_body_yields_no_tags(self) -> None:
        """An empty body cannot be scanned and must not crash."""
        assert extract("") == []

    def test_a_fenced_code_block_is_stripped_before_scanning(self) -> None:
        """A comment inside a fenced block is code, not a tag.

        Technical notes are full of shell and Python fences whose
        comments read exactly like hashtags. The prose tag outside the
        fence is asserted in the same call, so an implementation that
        stripped the *whole* body would fail too.
        """
        body = (
            "Notes on the build. #recovery\n\n"
            "```python\n"
            "x = 1  #comment\n"
            "colour = 'red'  #fixme\n"
            "```\n"
        )

        assert extract(body) == ["recovery"]

    def test_an_inline_code_span_is_stripped_before_scanning(self) -> None:
        """``#fff`` inside backticks is a CSS colour, not a tag.

        It passes every other rule — leading letter, three alphabetic
        characters, no digit run — so stripping inline spans is the only
        thing standing between the garden and every hex colour in the
        vault.
        """
        body = "the background is `#fff` but the theme is #dark"

        assert extract(body) == ["dark"]

    def test_a_tag_over_the_maximum_length_is_rejected(self) -> None:
        """65 characters is past the bound and is dropped, not truncated.

        Truncating would invent a tag the author never wrote and silently
        collide two different runaway tokens onto one garden entry.
        """
        assert extract("filed under #" + "a" * 65 + " here") == []


class TestMerge:
    """``merge`` unions two tag lists and never loses one."""

    def test_union_puts_the_existing_tags_first(self) -> None:
        """Recorded tags keep their position; new ones append.

        Stable order is what keeps a re-ingest from rewriting the
        frontmatter of every unchanged fragment in the vault.
        """
        assert merge(["writing"], ["recovery"]) == ["writing", "recovery"]

    def test_never_loses_the_clean_passes_low_priority_tag(self) -> None:
        """``low-priority`` survives a merge that adds body tags.

        ``creek.clean.context`` appends this literal in ``low_priority``
        context mode (``creek/clean/context.py:177``), which makes it the
        one tag that already exists on real vault fragments today. If the
        merge replaced rather than unioned, this pass would silently
        delete the only tag the pipeline ever wrote.
        """
        assert merge(["low-priority"], ["recovery"]) == ["low-priority", "recovery"]

    def test_existing_tags_are_renormalised_through_the_same_normaliser(self) -> None:
        """``Recovery`` on disk becomes ``recovery`` on the way through.

        Obsidian frontmatter written by hand carries arbitrary case.
        Normalising only the *new* side would leave the garden counting
        ``Recovery`` and ``recovery`` as two separate tags forever.
        """
        assert merge(["Recovery", "Family_Business"], []) == [
            "recovery",
            "family-business",
        ]

    def test_deduplicates_across_the_two_lists(self) -> None:
        """A body tag that repeats a recorded one is not appended twice."""
        assert merge(["Recovery"], ["recovery", "writing"]) == ["recovery", "writing"]

    def test_never_drops_an_existing_tag_the_extractor_would_reject(self) -> None:
        """Filtering is :func:`extract`'s job, not :func:`merge`'s.

        The recorded list may hold anything an operator or an earlier
        tool wrote. A merge that re-applied the extraction filters would
        silently delete hand-authored vault content the moment a
        re-classify ran over it — the one failure mode "never-lose"
        exists to prevent.
        """
        assert merge(["c762760820929069057"], ["recovery"]) == [
            "c762760820929069057",
            "recovery",
        ]

    def test_a_list_exactly_at_the_cap_keeps_everything_and_gains_nothing(
        self,
    ) -> None:
        """At the cap, the record is untouched and no candidate is admitted.

        Existing tags are first, so a fragment already at the ceiling
        loses the *new* candidates rather than the ones already on disk.
        This is the equality case only; :class:`TestMergeBoundsGrowth`
        sweeps both sides of the boundary, because a cap tested from one
        side alone cannot tell "admitted nothing" from "deleted the
        tail".
        """
        existing = [f"tag-a{i:02d}" for i in range(_MAX_TAGS)]

        assert merge(existing, ["recovery"]) == existing

    def test_two_empty_lists_merge_to_an_empty_list(self) -> None:
        """The degenerate case is empty, not ``[""]``."""
        assert merge([], []) == []


class TestMergeNeverTruncatesTheRecord:
    """A recorded list past the cap is kept whole, not silently trimmed.

    :data:`_MAX_TAGS` is a **growth ceiling**, not a length limit: it
    bounds how many tags an automated pass may *add*, and says nothing
    about how many an operator may have written by hand. Every other
    test in this file exercises the cap at ``len(current) == _MAX_TAGS``
    only, and a one-sided boundary is no boundary at all — a merge that
    trims at the ceiling is indistinguishable there and deletes vault
    content one item further along.

    That deletion is unrecoverable and unattributable. ``tags`` is a
    field operators hand-edit in Obsidian, both writers run over the
    whole vault repeatedly, and :func:`apply_tags` writes the shortened
    list straight back to the file while reporting the fragment as
    successfully tagged.
    """

    def test_a_recorded_list_over_the_cap_keeps_every_tag(self) -> None:
        """Forty recorded tags stay forty, and nothing new is admitted.

        The module's never-lose rule stated at the one input shape that
        can violate it. Asserted against the whole list rather than its
        length, so an implementation that held the count at forty by
        substituting candidates for the evicted tail fails too.
        """
        current = [f"tag-a{i:02d}" for i in range(40)]

        result = merge(current, ["fresh-one", "fresh-two"])

        assert result == current
        assert "fresh-one" not in result

    def test_apply_tags_leaves_an_over_cap_fragment_untouched(self) -> None:
        """An over-cap fragment comes back unchanged and uncopied.

        This is the shape that reaches disk. Returning a copy is exactly
        what makes the classify engine rewrite the file, so a merge that
        trims here does not merely compute a short list — it commits the
        deletion to the operator's vault. Identity is asserted alongside
        content because "same tags" and "no rewrite" are two different
        promises and both are owed.
        """
        current = [f"tag-a{i:02d}" for i in range(40)]
        fragment = _fragment(tags=current)
        body = "notes on #alpha #beta #gamma #delta #epsilon"

        result = apply_tags(fragment, body)

        assert result.tags == current
        assert result is fragment
        assert fragment.tags == current

    def test_an_over_cap_fragment_survives_a_second_pass(self) -> None:
        """Running the documented backfill twice still holds all forty tags.

        The module docstring's own remediation for an untagged vault is
        an explicit, paid ``creek classify --method llm --force``, and
        operators are told to re-run it. A merge that trims at the cap
        makes that published command destructive on its first pass and
        permanent on its second — no later run puts the eight deleted
        tags back. Two passes are asserted so "the loss is recoverable
        by re-running" cannot be claimed.
        """
        current = [f"tag-a{i:02d}" for i in range(40)]
        body = "notes on #alpha #beta"

        once = apply_tags(_fragment(tags=current), body)
        twice = apply_tags(once, body)

        assert twice.tags == current


class TestMergeBoundsGrowth:
    """The ceiling bounds what a pass may *add*, swept from both sides.

    The bound itself still has to hold: without it one pathological body
    — a hand-written tag-index note, a scraped page whose navigation
    survived conversion — writes hundreds of entries into a single
    fragment's YAML frontmatter. Stating the rule as "admit exactly
    ``max(0, 32 - len(current))`` new tags" keeps that protection while
    making it impossible to express as a deletion, and makes repeated
    passes idempotent by construction.

    Every expectation here is a **literal**. An expectation computed
    from :data:`_MAX_TAGS` moves with the constant and therefore cannot
    catch an off-by-one in the cap arithmetic, which is the whole class
    of bug this covers.
    """

    @pytest.mark.parametrize(
        ("existing_count", "expected_added"),
        [(0, 32), (1, 31), (31, 1), (32, 0), (33, 0), (40, 0), (100, 0)],
    )
    def test_the_merge_admits_exactly_the_number_of_new_tags_the_cap_leaves_room_for(
        self,
        existing_count: int,
        expected_added: int,
    ) -> None:
        """Room for new tags is ``max(0, 32 - len(current))``, exactly.

        Swept across the boundary from both sides — 31, 32, 33 — because
        the defect being pinned is a one-sided test, not a wrong
        constant. Each case also asserts the recorded tags come back as a
        prefix, which is what separates "admitted nothing" from "deleted
        the tail": both leave the same *number* of new entries, and only
        one of them is correct.
        """
        existing = [f"kept-{i:03d}" for i in range(existing_count)]
        candidate = [f"fresh-{i:03d}" for i in range(200)]

        result = merge(existing, candidate)

        assert len(set(result) - set(existing)) == expected_added
        assert result[: len(existing)] == existing

    def test_a_short_list_never_grows_past_the_cap(self) -> None:
        """Ten recorded tags plus two hundred candidates yields exactly 32.

        The ceiling still binds in the direction it was written for. The
        literal 32 is what makes this test able to fail: a ``<=`` read as
        ``<``, or a stray ``+ 1`` in the room calculation, changes this
        number and nothing else in the suite notices.
        """
        existing = [f"kept-{i:03d}" for i in range(10)]
        candidate = [f"fresh-{i:03d}" for i in range(200)]

        assert len(merge(existing, candidate)) == 32

    @pytest.mark.parametrize("existing_count", [10, 40])
    def test_repeated_merges_never_ratchet_the_list_upward(
        self,
        existing_count: int,
    ) -> None:
        """``merge(merge(c, x), x) == merge(c, x)``, under and over the cap.

        Feeding a merged list back in is what the pipeline actually
        does: every re-ingest and re-classify hands the previous result
        back as *current*. Without this fixed point an under-cap list
        would creep upward on each pass and an over-cap one downward,
        and either way the frontmatter of an unchanged fragment churns
        forever. Both sides of the ceiling are swept for the same reason
        as above.
        """
        current = [f"kept-{i:03d}" for i in range(existing_count)]
        candidate = [f"fresh-{i:03d}" for i in range(200)]

        once = merge(current, candidate)

        assert merge(once, candidate) == once

    def test_apply_tags_never_grows_a_fragment_past_the_cap(self) -> None:
        """A fifty-hashtag body cannot inflate a thirty-tag fragment past 32.

        The security half of the ceiling. Fragment bodies are unbounded,
        attacker-influenced content and this pass runs over every one of
        them on both the ingest and the classify path, so "preserve
        whatever is recorded" must not become a licence to append
        whatever the body offers. The recorded thirty are asserted in
        order in the same call, so satisfying the bound by evicting them
        is not an option either.
        """
        existing = [f"kept-{i:03d}" for i in range(30)]
        body = " ".join(f"#body-{i:03d}" for i in range(50))

        result = apply_tags(_fragment(tags=existing), body)

        assert len(result.tags) == 32
        assert result.tags[:30] == existing


class TestMergePreservationExceptions:
    """The only three ways a recorded entry may differ from what was typed.

    :func:`merge` preserves the *tag*, not the bytes: it returns
    :func:`_normalise`'s spelling of each recorded entry. So an entry
    can be re-spelled, can fold into an earlier entry sharing that
    spelling, or can normalise away entirely — and none of the three is
    a lost tag. Pinned so the never-lose rule asserted by
    :class:`TestMergeNeverTruncatesTheRecord` is *exact*: an invariant
    with undocumented exceptions is not an invariant, and the next
    reader needs to know which shortfalls are the contract and which are
    the bug.
    """

    def test_a_lone_recorded_entry_is_returned_re_spelled_not_verbatim(self) -> None:
        """One ``Family_Business`` comes back as ``family-business``.

        The exception that is easy to miss, because it needs no second
        entry to trigger it and no entry is dropped: a *single*
        non-canonical tag is still rewritten. Documenting only the
        fold-into-a-duplicate and the normalises-to-empty cases would
        make the preservation rule read as byte-for-byte, which it is
        not — and a rule stated more strongly than the code honours it
        is the kind of mismatch review is meant to catch.
        """
        assert merge(["Family_Business"], []) == ["family-business"]
        assert merge(["Recovery"], []) == ["recovery"]

    def test_two_spellings_of_one_recorded_tag_collapse_without_losing_it(
        self,
    ) -> None:
        """``Recovery``/``recovery``/``RECOVERY`` are one tag, and it survives.

        The tag is never lost — only the duplicate spellings are.
        Counting this as a loss would force the merge to keep three
        garden entries no reader could tell apart, which is the exact
        vocabulary fragmentation the normaliser exists to prevent.
        """
        assert merge(["Recovery", "recovery", "RECOVERY"], []) == ["recovery"]

    def test_a_recorded_entry_that_normalises_to_nothing_is_dropped(
        self,
    ) -> None:
        """``---``, ``_`` and ``-`` are separators, not tags.

        The only exception that removes an entry outright, and it removes
        nothing that was ever a tag. Each normalises to the empty string,
        and recording ``""`` in frontmatter would put an unnameable entry
        in the Tag Garden that no lint, query or hand-edit could ever
        reach. A real tag sits alongside them in the same call, so an
        implementation that threw the whole list away on the first
        offender fails too.
        """
        assert merge(["---", "_", "-", "recovery"], []) == ["recovery"]


class TestApplyTags:
    """``apply_tags`` stamps the merged list, union-only."""

    def test_stamps_extracted_tags_on_a_default_fragment(self) -> None:
        """A body with hashtags lifts a fragment off the empty default."""
        result = apply_tags(_fragment(), "notes on #recovery and #writing")

        assert result.tags == ["recovery", "writing"]

    def test_stored_values_are_plain_strings(self) -> None:
        """Every stored tag is a ``str``, ready for YAML's SafeDumper."""
        result = apply_tags(_fragment(), "notes on #recovery")

        assert [type(tag) for tag in result.tags] == [str]

    def test_returns_the_same_object_when_nothing_is_added(self) -> None:
        """No new tag means no copy — identity, not just equality.

        The engine counts "tags extracted" by comparing before/after, and
        both writers run this over every fragment in the vault. An
        unconditional ``model_copy`` would blur that counter and allocate
        a fresh model per fragment for nothing.
        """
        fragment = _fragment()

        assert apply_tags(fragment, _NEUTRAL_BODY) is fragment

    def test_returns_the_same_object_when_the_body_only_repeats_recorded_tags(
        self,
    ) -> None:
        """A re-run over an unchanged body is a genuine no-op."""
        fragment = _fragment(tags=["recovery"])

        assert apply_tags(fragment, "still about #recovery") is fragment

    def test_renormalises_a_hand_edited_tag_even_with_no_body_tags(self) -> None:
        """``Recovery`` in frontmatter is repaired on the way through.

        This is the path the ingest chokepoint relies on for Obsidian
        source notes whose ``tags:`` block was written by hand and whose
        body carries no hashtag at all.
        """
        result = apply_tags(_fragment(tags=["Recovery"]), _NEUTRAL_BODY)

        assert result.tags == ["recovery"]

    def test_never_drops_a_tag_already_on_the_fragment(self) -> None:
        """A body tag is added beside the recorded ones, never instead of them."""
        result = apply_tags(_fragment(tags=["low-priority"]), "about #recovery")

        assert result.tags == ["low-priority", "recovery"]

    def test_is_idempotent(self) -> None:
        """Applying twice equals applying once, and the second call is a no-op.

        ``creek classify`` is re-run over the whole vault routinely; a
        pass that kept producing new objects (or reordered the list) would
        churn every file's frontmatter forever.
        """
        body = "notes on #recovery and #writing"
        once = apply_tags(_fragment(), body)

        twice = apply_tags(once, body)

        assert twice is once
        assert twice.model_dump(mode="json") == once.model_dump(mode="json")

    def test_touches_no_other_fragment_field(self) -> None:
        """Only ``tags`` moves — the rest round-trips unchanged."""
        fragment = _fragment()

        result = apply_tags(fragment, "notes on #recovery")

        before = fragment.model_dump(mode="json")
        after = result.model_dump(mode="json")
        assert after.pop(TAGS_KEY) == ["recovery"]
        assert before.pop(TAGS_KEY) == []
        assert after == before


class TestHasUnrecordedTags:
    """``has_unrecorded_tags`` answers the question ``creek fill`` asks.

    :func:`creek.cli._scan_fill_gaps` compares a body's hashtags against
    the recorded list with raw string sets, so a fragment carrying
    ``tags: [Recovery]`` beside a body reading ``#recovery`` is reported
    as a backfillable gap. The operator then pays for a ``creek classify
    --method llm --force`` run over the vault that changes nothing,
    because :func:`merge` already considers those two the same tag.

    The comparison has to happen in normalised space, and normalisation
    is knowledge only this module has — which is why the predicate lives
    here rather than being re-derived at the call site.

    The predicate was imported inside each test while it did not yet
    exist, so a missing helper was reported as four precise failures
    rather than one collection error that hid every other case in this
    file. Now that it exists, the import sits with the module's others.
    """

    def test_a_case_only_difference_is_not_an_unrecorded_tag(self) -> None:
        """``tags: [Recovery]`` already records the body's ``#recovery``.

        The exact false positive that bills the operator for an empty
        paid run. ``is False`` rather than ``not ...`` so a helper that
        returned an empty list — falsy, but not an answer — fails here.
        """
        assert has_unrecorded_tags(["Recovery"], "a note about #recovery") is False

    def test_an_underscore_spelling_is_not_an_unrecorded_tag(self) -> None:
        """``family_business`` already records the body's ``#family-business``.

        The second half of the normalisation. ``_`` → ``-`` has to be
        applied to both sides or to neither, and hand-written Obsidian
        frontmatter carries both spellings freely.
        """
        assert (
            has_unrecorded_tags(["family_business"], "the #family-business again")
            is False
        )

    def test_a_genuinely_missing_tag_is_reported(self) -> None:
        """A body tag with no recorded counterpart is still a real gap.

        The precision fix must not become a mute. This hint is the only
        surface that tells an operator the tags axis is unbacked, so a
        helper that answered ``False`` unconditionally would "fix" the
        false positive by deleting the feature.
        """
        assert has_unrecorded_tags(["recovery"], "and also #grief") is True

    def test_a_body_with_no_hashtags_reports_nothing(self) -> None:
        """A plain note with nothing recorded is not a gap.

        The degenerate shape both arguments can take at once — and the
        shape 2000/2000 sampled fragments of the operator's vault
        actually have, so it is the case this predicate answers most.
        """
        assert has_unrecorded_tags([], _NEUTRAL_BODY) is False
