"""Conversation turn grouping shared by the chat ingestors (#1333).

A "turn" in a chat export is not one message. Splitting a thought across
two sends before the model replies is ordinary usage, and so is a model
that answers in two. Both the Claude and the ChatGPT export walks
therefore meet *runs* of consecutive same-role messages, and both used
to keep exactly one message per run and drop the rest — silently, with
no error, no counter, and no trace in the vault.

The human half of that loss is the expensive one. Human turns are
written as ``source.author = self`` fragments, and those are the only
fragments the voice fingerprint is trained on
(:meth:`creek.generate.voice.VoiceExemplarCollector._eligible_register`),
so a dropped first half meant the fingerprint learned from a filtered
sample of how the operator writes.

This module holds the two primitives that fix it once for both
ingestors rather than twice, differently:

* :func:`group_turn_runs` — segment a message list into
  ``(human_run, assistant_run)`` pairs.
* :func:`merge_turn_texts` — join one run's texts into one turn body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_Message = TypeVar("_Message")

TURN_TEXT_SEPARATOR: Final[str] = "\n\n"
"""Separator joining the messages of one same-role run into one body.

A blank line, not a bare newline. Each sent message is a *block-level*
unit, and a blank line is the only separator CommonMark treats as a
block boundary: under a single newline an indented code block or a list
sent as its own message becomes a lazy continuation of the preceding
paragraph, and two sentences run together into one. The blank line also
survives :meth:`creek.ingest.claude.ClaudeIngestor.convert_to_markdown`,
which prefixes every line of a human turn with ``"> "`` — the separator
becomes a quoted blank line and keeps two paragraphs inside one
blockquote, where a soft break would have fused them.

No *visible* delimiter (a ``---`` rule, a ``[continued]`` marker) is
inserted, because whatever goes here is indistinguishable downstream
from text the operator actually wrote: it is embedded, classified, and
weighed by the voice fingerprint as their prose.
"""


def merge_turn_texts(texts: Sequence[str]) -> str:
    """Join the texts of one same-role run into a single turn body.

    A **one-element run is returned byte-identically** — not stripped,
    not normalised, not re-wrapped. That is the load-bearing case rather
    than the degenerate one: fragment ids hash the content
    (:func:`creek.ingest.base.generate_fragment_id`), so any change to
    an already-correct single-message turn would re-mint its id and
    orphan the fragment already sitting in the vault. Every turn the
    drop never touched must come out of here exactly as it went in.

    A multi-element run drops the parts that are empty once stripped —
    an empty send contributes no words, only blank lines — and joins the
    rest with :data:`TURN_TEXT_SEPARATOR`.

    Args:
        texts: The extracted texts of one run's messages, in order.

    Returns:
        The merged turn body; ``""`` for an empty run.
    """
    if len(texts) == 1:
        return texts[0]
    return TURN_TEXT_SEPARATOR.join(text for text in texts if text.strip())


def group_turn_runs(
    messages: Sequence[_Message],
    *,
    role_of: Callable[[_Message], str],
    human_role: str,
    assistant_role: str,
) -> list[tuple[list[_Message], list[_Message]]]:
    """Segment a message list into ``(human_run, assistant_run)`` pairs.

    One pass. Consecutive human messages accumulate into a run; the
    assistant messages answering them accumulate into the next run; the
    pair is flushed when a human speaks again or the input ends.

    Three deliberate behaviours, all of which the two callers previously
    had to state for themselves and one of which they disagreed on:

    * **Any other role is skipped without breaking the pair.** A system
      prompt or a tool result between a question and its answer is not a
      turn and must not prevent one. ChatGPT's index walk required
      strict adjacency, so ``user, system, assistant`` produced *no*
      fragment at all — contradicting its own docstring, which already
      said system messages were skipped.
    * **A leading assistant run is dropped.** With nobody to answer,
      there is no turn.
    * **A trailing human run is dropped.** A question the model never
      answered has no pair. Both ingestors already did this and both
      docstrings already promised it.

    For every input that previously yielded pairs at all, the number of
    pairs is unchanged: a run collapses into the same single pair it
    used to, so ``turn_index`` never renumbers.

    Args:
        messages: The conversation's messages, in order.
        role_of: Extracts a message's role. Claude reads a ``role`` key
            and ChatGPT a nested ``author.role``, so the accessor is a
            parameter rather than a shared assumption about the shape.
        human_role: The role naming the owner's own messages
            (``"human"`` for Claude, ``"user"`` for ChatGPT).
        assistant_role: The role naming the model's messages.

    Returns:
        The ``(human_run, assistant_run)`` pairs, in order. Both lists of
        every returned pair are non-empty.
    """
    pairs: list[tuple[list[_Message], list[_Message]]] = []
    human_run: list[_Message] = []
    assistant_run: list[_Message] = []

    for message in messages:
        role = role_of(message)
        if role == human_role:
            if assistant_run:
                pairs.append((human_run, assistant_run))
                human_run, assistant_run = [], []
            human_run.append(message)
        elif role == assistant_role and human_run:
            assistant_run.append(message)

    if assistant_run:
        pairs.append((human_run, assistant_run))
    return pairs
