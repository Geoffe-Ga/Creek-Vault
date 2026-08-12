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

import pytest

from creek.ingest.turns import (
    TURN_TEXT_SEPARATOR,
    group_turn_runs,
    merge_turn_texts,
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
