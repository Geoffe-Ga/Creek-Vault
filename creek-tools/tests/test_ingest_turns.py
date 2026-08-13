"""Tests for creek.ingest.turns — shared conversation turn-run grouping.

Issue #1333: both the Claude and the ChatGPT ingestors kept only one
message from a run of consecutive same-role messages and silently dropped
the rest. Human turns are ``source.author=self`` and are what the voice
fingerprint trains on, so a dropped human message makes the voice corpus a
filtered sample of the operator's prose. This module pins the two shared
primitives the ingestors are rebuilt on:

- :func:`merge_turn_texts` — join a run's texts without disturbing the
  single-message case, whose bytes must stay identical so already-correct
  fragment ids never churn.
- :func:`group_turn_runs` — one pass over an ordered message list yielding
  ``(human_run, assistant_run)`` pairs, skipping roles that participate in
  neither run rather than letting them break a pair.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creek.ingest.base import ParsedFragment
from creek.ingest.chatgpt import ChatGPTIngestor
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.turns import (
    CONVERSATION_PLATFORMS,
    TURN_TEXT_SEPARATOR,
    BodyShape,
    group_turn_runs,
    merge_turn_texts,
    split_conversation_body,
)

# ---- Helpers ----

Message = dict[str, str]
Pair = tuple[list[Message], list[Message]]


def _msg(role: str, text: str) -> Message:
    """Build a minimal message dict for the grouping tests.

    Args:
        role: The message's role (``human``, ``assistant``, ``system``, ...).
        text: The message body; unique per message so list equality is a
            meaningful assertion.

    Returns:
        A plain dict with ``role`` and ``content`` keys.
    """
    return {"role": role, "content": text}


def _role_of(message: Message) -> str:
    """Read the role off a test message dict.

    Args:
        message: A message built by :func:`_msg`.

    Returns:
        The message's role string.
    """
    return message["role"]


def _group(messages: list[Message]) -> list[Pair]:
    """Group messages with the Claude role vocabulary.

    Args:
        messages: The ordered message list to group.

    Returns:
        The ``(human_run, assistant_run)`` pairs produced by
        :func:`group_turn_runs`.
    """
    return group_turn_runs(
        messages,
        role_of=_role_of,
        human_role="human",
        assistant_role="assistant",
    )


# ---- merge_turn_texts() ----


class TestMergeTurnTexts:
    """Tests for :func:`creek.ingest.turns.merge_turn_texts`."""

    def test_single_element_returns_that_element(self) -> None:
        """A one-message run merges to exactly that message's text."""
        assert merge_turn_texts(["only"]) == "only"

    def test_single_element_is_byte_identical(self) -> None:
        """A single unstripped element is returned with its bytes untouched."""
        # Load-bearing invariant: a single-message turn is byte-identical, so
        # no already-correct fragment id churns when #1333 lands. Stripping
        # here would rewrite every ordinary turn in the vault.
        assert merge_turn_texts(["  padded  \n"]) == "  padded  \n"

    def test_single_whitespace_only_element_is_returned_verbatim(self) -> None:
        """Even an all-whitespace lone element survives the single-element path.

        The empty-after-strip filter belongs to the multi-element join only;
        applying it first would turn this into ``""`` and change the content
        (and therefore the id) of an existing fragment.
        """
        assert merge_turn_texts(["   "]) == "   "

    def test_two_elements_join_with_one_blank_line(self) -> None:
        """Two messages join with exactly one blank line between them."""
        assert TURN_TEXT_SEPARATOR == "\n\n"
        assert merge_turn_texts(["a", "b"]) == "a\n\nb"

    def test_whitespace_only_elements_are_dropped(self) -> None:
        """Elements that are empty after ``.strip()`` drop out of a merge."""
        assert merge_turn_texts(["a", "   ", "b"]) == "a\n\nb"

    def test_empty_sequence_returns_empty_string(self) -> None:
        """An empty run merges to the empty string, not to a separator."""
        assert merge_turn_texts([]) == ""

    def test_indented_block_starts_its_own_markdown_block(self) -> None:
        """The separator is a blank line so an indented block stays a block."""
        merged = merge_turn_texts(["Here is the snippet:", "    indented_code_line()"])
        # A single "\n" would make the indented line a lazy continuation of
        # the preceding paragraph instead of a code block, so the merge would
        # change how the fragment renders as well as what it contains.
        assert "\n\n    indented_code_line()" in merged
        assert merged == "Here is the snippet:\n\n    indented_code_line()"


# ---- group_turn_runs() ----


class TestGroupTurnRuns:
    """Tests for :func:`creek.ingest.turns.group_turn_runs`."""

    def test_consecutive_humans_form_one_pair(self) -> None:
        """A run of two human messages is one pair holding both of them."""
        h1 = _msg("human", "part one of my question")
        h2 = _msg("human", "and part two")
        a1 = _msg("assistant", "answer")

        pairs = _group([h1, h2, a1])

        assert pairs == [([h1, h2], [a1])]

    def test_consecutive_assistants_form_one_pair(self) -> None:
        """A run of two assistant messages is one pair holding both of them."""
        h1 = _msg("human", "q")
        a1 = _msg("assistant", "reply part one")
        a2 = _msg("assistant", "reply part two")

        pairs = _group([h1, a1, a2])

        assert pairs == [([h1], [a1, a2])]

    def test_system_message_does_not_break_a_pair(self) -> None:
        """A system message between the turns is skipped, not a divider."""
        h1 = _msg("human", "q")
        sys_msg = _msg("system", "you are a helpful assistant")
        a1 = _msg("assistant", "a")

        pairs = _group([h1, sys_msg, a1])

        assert pairs == [([h1], [a1])]
        human_run, assistant_run = pairs[0]
        assert all(m is not sys_msg for m in (*human_run, *assistant_run))

    @pytest.mark.parametrize("role", ["system", "tool", "unknown"])
    def test_non_participating_roles_are_skipped(self, role: str) -> None:
        """Any role that is neither human nor assistant is passed over.

        Args:
            role: A role that participates in neither run.
        """
        h1 = _msg("human", "q")
        other = _msg(role, "noise")
        a1 = _msg("assistant", "a")

        assert _group([other, h1, other, a1, other]) == [([h1], [a1])]

    def test_leading_assistant_without_human_is_skipped(self) -> None:
        """An assistant message with no preceding human starts no pair."""
        a0 = _msg("assistant", "unprompted preamble")
        h1 = _msg("human", "q")
        a1 = _msg("assistant", "a")

        pairs = _group([a0, h1, a1])

        assert pairs == [([h1], [a1])]

    def test_trailing_human_run_is_discarded(self) -> None:
        """A human run with no assistant response yields no pair."""
        h1 = _msg("human", "q")
        a1 = _msg("assistant", "a")
        h2 = _msg("human", "unanswered follow-up")

        pairs = _group([h1, a1, h2])

        assert pairs == [([h1], [a1])]

    def test_two_runs_pair_in_order(self) -> None:
        """Two exchanges of merged runs produce two pairs, in source order."""
        h1 = _msg("human", "q one")
        h2 = _msg("human", "q one continued")
        a1 = _msg("assistant", "a one")
        a2 = _msg("assistant", "a one continued")
        h3 = _msg("human", "q two")
        a3 = _msg("assistant", "a two")

        pairs = _group([h1, h2, a1, a2, h3, a3])

        # Pair count is invariant with the pre-#1333 implementation: a run
        # collapses into the same single pair it used to, so ``turn_index``
        # never renumbers and no existing fragment id churns.
        assert pairs == [([h1, h2], [a1, a2]), ([h3], [a3])]

    def test_empty_input_returns_no_pairs(self) -> None:
        """An empty message list groups to an empty pair list."""
        assert _group([]) == []


# ---- split_conversation_body() ----
#
# Issue #1426. Two consumers were each answering "what is in this chat body?"
# with their own half-answer:
#
# * ``creek.generate.ai_style.fingerprint.extract_user_turns`` recognised only
#   the merged ``> **User**: …`` rendering, so every per-turn human fragment
#   written since #1333/#553 — a plain blockquote, no marker — came back
#   ``None`` and was dropped from the voice corpus.
# * ``creek.ingest.refresh._split_turns`` recognised only the merged shape too,
#   but returned ``None`` for everything else, which is exactly right *for it*:
#   ``resplit_merged_ai_chat`` reacts to a non-``None`` verdict by writing two
#   fragments and ``md_file.unlink()``-ing the original.
#
# One classifier now answers both. That makes the migration's blast radius a
# property of this table, so the table states the pre-change refresh verdict as
# a hand-written literal and the migration is required to match it.

_HUMAN_PROSE = (
    "I keep circling the same knot about how my notes should sit next to "
    "each other, and every time I try to force a folder tree on it the "
    "thing goes stiff and I stop writing in it at all for a week."
)
"""Tell-free operator prose used as the human half of the split shapes."""

_AI_PROSE = (
    "It is important to note that a vibrant tapestry of intricate systems "
    "underscores the pivotal realm you navigate as you delve into it."
)
"""Assistant-side prose, saturated with AI vocabulary."""

_CHATGPT_ASSISTANT = (
    "a vibrant tapestry that delve into the intricate and pivotal underscore of it all"
)
"""The assistant half of :data:`_CHATGPT_BODY`, as the parse must return it."""

_CHATGPT_BODY = (
    "# Conversation (turn 1)\n\n"
    "> **User**: I reckon it was fine and that was that\n"
    ">\n"
    "> **Assistant**: " + _CHATGPT_ASSISTANT + "\n"
)
"""The merged pre-split ChatGPT rendering.

Byte-identical to the fixture at ``tests/generate/ai_style/test_fingerprint.py``
so the two suites cannot drift apart on the one shape that already worked.
"""

_SHAPE_TABLE: list[tuple[str, str, str, BodyShape, str, str]] = [
    (
        "a-claude-split",
        "claude",
        "> " + _HUMAN_PROSE,
        BodyShape.SPLIT,
        _HUMAN_PROSE,
        "",
    ),
    (
        "b-chatgpt-split-fresh",
        "chatgpt",
        "# Navigating the Intricate Tapestry of Knowledge Systems\n\n> "
        + _HUMAN_PROSE
        + "\n",
        BodyShape.SPLIT,
        _HUMAN_PROSE,
        "",
    ),
    (
        "c-chatgpt-split-migrated",
        "chatgpt",
        "> " + _HUMAN_PROSE,
        BodyShape.SPLIT,
        _HUMAN_PROSE,
        "",
    ),
    (
        "d-claude-merged",
        "claude",
        "> How should I organize knowledge?\n\n" + _AI_PROSE,
        BodyShape.MERGED,
        "How should I organize knowledge?",
        _AI_PROSE,
    ),
    (
        "d2-claude-merged-empty-human",
        "claude",
        "> \n\n" + _AI_PROSE,
        BodyShape.UNRECOVERABLE,
        "",
        _AI_PROSE,
    ),
    (
        "e-chatgpt-merged",
        "chatgpt",
        _CHATGPT_BODY,
        BodyShape.MERGED,
        "I reckon it was fine and that was that",
        _CHATGPT_ASSISTANT,
    ),
    (
        "f-chatgpt-user-marker-no-assistant",
        "chatgpt",
        "# c\n\n> **User**: no dashes here at all\n",
        BodyShape.SPLIT,
        "no dashes here at all",
        "",
    ),
    (
        "g-claude-two-send",
        "claude",
        "> first send\n> \n> second send",
        BodyShape.SPLIT,
        "first send\n\nsecond send",
        "",
    ),
    (
        "h-claude-user-colon-in-prose",
        "claude",
        "> I was debugging an auth bug today and the log line read\n"
        "> user: alice\n"
        "> which finally explained the 403.",
        BodyShape.SPLIT,
        "I was debugging an auth bug today and the log line read\n"
        "user: alice\n"
        "which finally explained the 403.",
        "",
    ),
    (
        "i-claude-assistant-colon-in-prose",
        "claude",
        "> Assistant: please file the ticket\n> is what I told the intern.",
        BodyShape.SPLIT,
        "Assistant: please file the ticket\nis what I told the intern.",
        "",
    ),
    (
        "j-claude-placeholder-body",
        "claude",
        "body for frag-xyz",
        BodyShape.UNRECOVERABLE,
        "",
        "body for frag-xyz",
    ),
    (
        "l-claude-ai-turn-only",
        "claude",
        _AI_PROSE,
        BodyShape.UNRECOVERABLE,
        "",
        _AI_PROSE,
    ),
    (
        "m-claude-nested-quote",
        "claude",
        "> > he said hi\n> and I laughed",
        BodyShape.SPLIT,
        "> he said hi\nand I laughed",
        "",
    ),
    (
        "n-chatgpt-unmarkered-escaping-prose",
        "chatgpt",
        "# Title\n\n> my question\n\nAI plain prose here",
        BodyShape.SPLIT,
        "my question\n\nAI plain prose here",
        "",
    ),
]
"""``(id, platform, body, shape, human, assistant)`` for every body shape.

The ids carry the letters used in the issue's shape inventory so a row can be
matched to the discussion that produced it.
"""

_SHAPE_IDS: list[str] = [row[0] for row in _SHAPE_TABLE]

_PRE_CHANGE_REFRESH: dict[str, tuple[str, str] | None] = {
    "a-claude-split": None,
    "b-chatgpt-split-fresh": None,
    "c-chatgpt-split-migrated": None,
    "d-claude-merged": ("How should I organize knowledge?", _AI_PROSE),
    "d2-claude-merged-empty-human": None,
    "e-chatgpt-merged": (
        "I reckon it was fine and that was that",
        _CHATGPT_ASSISTANT,
    ),
    "f-chatgpt-user-marker-no-assistant": None,
    "g-claude-two-send": None,
    "h-claude-user-colon-in-prose": None,
    "i-claude-assistant-colon-in-prose": None,
    "j-claude-placeholder-body": None,
    "l-claude-ai-turn-only": None,
    "m-claude-nested-quote": None,
    "n-chatgpt-unmarkered-escaping-prose": None,
}
"""What ``refresh._split_turns`` returned for each row **before** #1426.

Hand-written literals, transcribed from ``_parse_claude_merged`` /
``_parse_chatgpt_merged`` as they stand at ``creek/ingest/refresh.py:319-366``.
Deriving these by calling the new code would make the guard vacuous: it would
assert the new implementation equals itself.
"""


class TestConversationPlatforms:
    """The one definition of "this platform is a chat"."""

    def test_platform_set_is_exactly_claude_and_chatgpt(self) -> None:
        """The shared constant names both chat platforms and nothing else."""
        assert frozenset({"claude", "chatgpt"}) == CONVERSATION_PLATFORMS

    def test_no_consumer_keeps_a_private_copy_of_the_platform_set(self) -> None:
        """Both consumers read the shared constant, not a local duplicate.

        ``fingerprint._CONVERSATION_PLATFORMS`` and
        ``refresh._AI_CHAT_PLATFORMS`` were two hand-maintained frozensets
        answering the same question, and a third platform added to one and not
        the other is a silent divergence. #1426 deletes both in favour of
        :data:`CONVERSATION_PLATFORMS`.

        Identity (``is``) rather than equality is the assertion that has
        teeth: an equal-but-separate frozenset is exactly the drift this
        replaces, so a reintroduced private copy that happens to agree today
        must still fail here. The ``hasattr`` checks fail if either private
        name comes back at all.

        Out of scope by decision, not oversight:
        ``creek/generate/voice_authenticity.py`` holds a fourth copy. It is a
        read-only membership test built from ``SourcePlatform`` enum members
        rather than a parser, so it is left alone and deliberately not
        asserted on here.
        """
        from creek.generate.ai_style import fingerprint
        from creek.ingest import refresh

        assert fingerprint.CONVERSATION_PLATFORMS is CONVERSATION_PLATFORMS
        assert refresh.CONVERSATION_PLATFORMS is CONVERSATION_PLATFORMS
        assert not hasattr(fingerprint, "_CONVERSATION_PLATFORMS")
        assert not hasattr(refresh, "_AI_CHAT_PLATFORMS")

    def test_every_table_row_uses_a_known_platform(self) -> None:
        """No row can drift onto a platform the dispatcher does not claim."""
        assert {row[1] for row in _SHAPE_TABLE} == CONVERSATION_PLATFORMS


class TestSplitConversationBody:
    """Shape classification and the two turn halves, per body shape."""

    def test_the_shape_table_is_populated(self) -> None:
        """Guard against an emptied table silently skipping every shape.

        A parametrize list that loses its rows produces zero tests and a green
        run, which is indistinguishable from a passing suite.
        """
        assert len(_SHAPE_TABLE) == 14
        assert len(set(_SHAPE_IDS)) == len(_SHAPE_TABLE)

    @pytest.mark.parametrize(
        ("platform", "body", "shape", "human", "assistant"),
        [row[1:] for row in _SHAPE_TABLE],
        ids=_SHAPE_IDS,
    )
    def test_shape_and_halves(
        self,
        platform: str,
        body: str,
        shape: BodyShape,
        human: str,
        assistant: str,
    ) -> None:
        """Every body shape classifies and splits to its stated halves.

        All three fields are asserted together: a classifier that got the
        shape right while returning the wrong text would still starve or
        poison the voice corpus, and a splitter that got the text right while
        misreporting the shape would mis-drive the re-split migration.

        Args:
            platform: The conversation platform.
            body: The stored fragment body.
            shape: The expected :class:`BodyShape`.
            human: The expected human half.
            assistant: The expected assistant half.
        """
        parsed = split_conversation_body(platform, body)

        assert parsed.shape is shape
        assert parsed.human == human
        assert parsed.assistant == assistant

    @pytest.mark.parametrize(
        ("platform", "body", "shape"),
        [(row[1], row[2], row[3]) for row in _SHAPE_TABLE],
        ids=_SHAPE_IDS,
    )
    def test_shape_is_a_function_of_the_two_halves(
        self, platform: str, body: str, shape: BodyShape
    ) -> None:
        """``MERGED`` iff both halves are non-empty; ``SPLIT`` iff human only.

        Pins the classification rule itself rather than re-listing verdicts,
        so a future arm cannot invent a fourth combination. The
        assistant-only case collapses into ``UNRECOVERABLE`` deliberately:
        with no identifiable human half there is nothing safe to keep.

        Args:
            platform: The conversation platform.
            body: The stored fragment body.
            shape: The expected :class:`BodyShape`.
        """
        parsed = split_conversation_body(platform, body)
        expected = BodyShape.UNRECOVERABLE
        if parsed.human and parsed.assistant:
            expected = BodyShape.MERGED
        elif parsed.human:
            expected = BodyShape.SPLIT

        assert expected is shape
        assert parsed.shape is expected

    def test_result_is_immutable(self) -> None:
        """``ConversationTurns`` is frozen, so no consumer can edit a verdict.

        The classifier now has two consumers reading the same object; a
        mutable verdict would let one of them rewrite what the other sees.
        ``FrozenInstanceError`` subclasses ``AttributeError``, which is what
        is caught here so the assertion needs no extra import.
        """
        parsed = split_conversation_body("claude", "> " + _HUMAN_PROSE)
        field_name = "human"
        with pytest.raises(AttributeError, match="human"):
            setattr(parsed, field_name, "rewritten")

    def test_merged_chatgpt_keeps_only_the_first_exchange(self) -> None:
        """A hand-made multi-exchange body keeps only the first user turn.

        Pinned because it is a real narrowing, not because it is reachable.
        The retired marker walker ``extract_user_turns`` accumulated *every*
        user turn, so on the body below it returned both questions; this arm
        partitions on the first ``**Assistant**:`` and keeps only what
        precedes it.

        That is deliberate: byte-for-byte agreement with the shipped
        ``_parse_chatgpt_merged`` is what lets one function serve both the
        migration and the fingerprint, and the migration has always had this
        shape. It costs nothing in practice because **no shipped ingestor
        ever wrote this body**. The pre-split ChatGPT ingestor emitted one
        fragment per turn *pair* --- ``git show
        fc00d49~1:creek-tools/creek/ingest/chatgpt.py`` builds
        ``f"**User**: {user_text}\\n\\n**Assistant**: {assistant_text}"`` and
        titles it ``f"{title} (turn {turn_idx})"`` --- so a stored body holds
        exactly one exchange. Only an operator hand-editing a vault file
        could produce this, and the human half they would lose is still
        their own words, so the shape is recorded here rather than special
        cased.
        """
        body = (
            "# Chat\n\n"
            "> **User**: first question of mine\n"
            ">\n"
            "> **Assistant**: first reply\n"
            ">\n"
            "> **User**: second question of mine\n"
            ">\n"
            "> **Assistant**: second reply\n"
        )

        parsed = split_conversation_body("chatgpt", body)

        assert parsed.shape is BodyShape.MERGED
        assert parsed.human == "first question of mine"
        assert "second question of mine" in parsed.assistant


class TestSplitConversationBodyMatchesRefresh:
    """The migration's verdict must not move for any known body shape."""

    def test_the_refresh_table_covers_every_shape(self) -> None:
        """Every table row states a pre-change verdict; no row is skipped."""
        assert set(_PRE_CHANGE_REFRESH) == set(_SHAPE_IDS)
        assert len(_PRE_CHANGE_REFRESH) == 14

    @pytest.mark.parametrize(
        ("platform", "body", "expected_refresh"),
        [(row[1], row[2], _PRE_CHANGE_REFRESH[row[0]]) for row in _SHAPE_TABLE],
        ids=_SHAPE_IDS,
    )
    def test_merged_projection_reproduces_the_pre_change_verdict(
        self,
        platform: str,
        body: str,
        expected_refresh: tuple[str, str] | None,
    ) -> None:
        """The MERGED projection equals what ``_split_turns`` used to return.

        ``resplit_merged_ai_chat`` writes two fragments and unlinks the
        original whenever ``_split_turns`` is non-``None``, so a shape newly
        classified ``MERGED`` is a fragment newly *deleted* from an operator's
        vault. Reusing one classifier for both consumers is only safe if this
        projection is byte-identical to the old verdict, which is what the
        hand-written ``_PRE_CHANGE_REFRESH`` column checks.

        Row ``n`` is why the column is worth writing out: unmarkered ChatGPT
        with plain prose escaping the blockquote *looks* like a merged body,
        and a branch that classified it ``MERGED`` would make the migration
        split and ``md_file.unlink()`` a fragment it has always left alone.
        Its expected value is ``None``, and it is a ``SPLIT`` whose whole text
        — escaped prose included — goes to the human half.

        Args:
            platform: The conversation platform.
            body: The stored fragment body.
            expected_refresh: The pre-#1426 ``_split_turns`` verdict.
        """
        parsed = split_conversation_body(platform, body)
        projection = (
            (parsed.human, parsed.assistant)
            if parsed.shape is BodyShape.MERGED
            else None
        )

        assert projection == expected_refresh


# ---- Round-trip against the real ingestor renderings ----

_ROUND_TRIP_CASES: list[tuple[str, str]] = [
    ("plain", "just one plain line of my own prose"),
    ("heading", "# A heading I typed myself\n\nand the paragraph under it"),
    ("blank-separator", "first send\n\nsecond send"),
    ("already-quoted", "> he said hi\nand I laughed"),
    ("em-dash", "I paused — then wrote — and kept going"),
    ("trailing-newline", "a line with a trailing newline\n"),
]
"""``(id, human turn text)`` pairs the ingestors must render reversibly."""

_ROUND_TRIP_IDS: list[str] = [case for case, _text in _ROUND_TRIP_CASES]
_ROUND_TRIP_TEXTS: list[str] = [text for _case, text in _ROUND_TRIP_CASES]


def _claude_rendering(text: str) -> str:
    """Render *text* as a Claude human turn via the real ingestor.

    Args:
        text: The human turn's text.

    Returns:
        The Markdown body ``ClaudeIngestor`` would store for that turn.
    """
    return ClaudeIngestor().convert_to_markdown(
        ParsedFragment(
            content=text,
            metadata={"turn_text": text, "author_role": "self"},
            source_path="/exports/claude.json",
            timestamp=datetime(2024, 11, 15, 10, 0, tzinfo=UTC),
        ),
    )


def _chatgpt_rendering(text: str) -> str:
    """Render *text* as a ChatGPT human turn via the real ingestor.

    Args:
        text: The human turn's text.

    Returns:
        The Markdown body ``ChatGPTIngestor`` would store for that turn.
    """
    return ChatGPTIngestor().convert_to_markdown(
        ParsedFragment(
            content=text,
            metadata={"title": "Some Title", "author_role": "self"},
            source_path="/exports/chatgpt.json",
            timestamp=datetime(2024, 11, 15, 10, 0, tzinfo=UTC),
        ),
    )


class TestConversationBodyRoundTrip:
    """What the ingestors write, the splitter must read back.

    Hand-written fixtures encode a belief about the rendering; these drive
    ``convert_to_markdown`` itself, so a change to either renderer breaks the
    contract here instead of silently emptying the voice corpus.
    """

    def test_the_round_trip_table_is_populated(self) -> None:
        """Guard against an emptied table skipping every round-trip case."""
        assert len(_ROUND_TRIP_CASES) == 6

    @pytest.mark.parametrize("text", _ROUND_TRIP_TEXTS, ids=_ROUND_TRIP_IDS)
    def test_claude_rendering_round_trips(self, text: str) -> None:
        """A Claude human turn survives render → split unchanged.

        ``ClaudeIngestor.convert_to_markdown`` prefixes ``"> "`` to every
        line, including blank ones and lines that already begin with ``>``.
        The Claude arm removes exactly one marker, so the operator's own
        quotation and their own ``# `` heading both come back intact — the
        Claude arm has no heading rule at all.

        Args:
            text: The human turn's text.
        """
        parsed = split_conversation_body("claude", _claude_rendering(text))

        assert parsed.shape is BodyShape.SPLIT
        assert parsed.human == text.strip()
        assert parsed.assistant == ""

    @pytest.mark.parametrize("text", _ROUND_TRIP_TEXTS, ids=_ROUND_TRIP_IDS)
    def test_chatgpt_rendering_round_trips(self, text: str) -> None:
        """A ChatGPT human turn survives render → split unchanged.

        ``ChatGPTIngestor.convert_to_markdown`` (``creek/ingest/chatgpt.py``
        lines 129-144) emits ``"# {title}\\n\\n"`` and then blockquotes every
        line, rendering a blank line as a bare ``">"``. The ChatGPT arm drops
        lines starting with ``"# "`` — which is how the model-written title
        is discarded — but it tests the **raw** line, before the blockquote
        marker comes off. So the ``heading`` case here, whose own first line
        is ``"# A heading I typed myself"``, is rendered as
        ``"> # A heading I typed myself"``, does not match the drop rule, and
        survives. Only an unquoted heading is dropped, and only the renderer
        emits those.

        Args:
            text: The human turn's text.
        """
        rendered = _chatgpt_rendering(text)
        assert rendered.startswith("# Some Title\n\n")

        parsed = split_conversation_body("chatgpt", rendered)

        assert parsed.shape is BodyShape.SPLIT
        assert parsed.human == text.strip()
        assert parsed.assistant == ""
        assert "Some Title" not in parsed.human

    def test_round_tripped_heading_keeps_the_operators_own_hash(self) -> None:
        """The operator's own ``# `` line is not confused with the title.

        Called out separately because it is the one place the two renderers
        disagree: Claude never writes a heading, ChatGPT always does, and the
        drop rule that removes ChatGPT's must not reach into the blockquote.
        """
        text = "# A heading I typed myself\n\nand the paragraph under it"

        for platform, rendering in (
            ("claude", _claude_rendering(text)),
            ("chatgpt", _chatgpt_rendering(text)),
        ):
            parsed = split_conversation_body(platform, rendering)
            assert parsed.human.startswith("# A heading I typed myself")
