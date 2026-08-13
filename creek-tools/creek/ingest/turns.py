"""Conversation turns: grouping them on the way in, reading them back out.

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

Ingest-side primitives (#1333), which fix that once for both ingestors
rather than twice, differently:

* :func:`group_turn_runs` — segment a message list into
  ``(human_run, assistant_run)`` pairs.
* :func:`merge_turn_texts` — join one run's texts into one turn body.

Reading a stored body back (#1426)
----------------------------------

:func:`split_conversation_body` is the other direction: given a body
already written to the vault, recover which part of it the operator
wrote. Five shapes are on disk in a long-lived vault, and any of them
can turn up in the same ``01-Fragments`` tree:

(a) **Claude split human.** Every line blockquoted, no heading — what
    :meth:`creek.ingest.claude.ClaudeIngestor.convert_to_markdown`
    writes for a human turn since per-turn attribution.
(b) **ChatGPT split human, fresh ingest.** A ``# {AI title}`` heading —
    the model's conversation title, not the operator's words —
    followed by an all-blockquoted turn.
(c) **Human fragment from the** :func:`~creek.ingest.refresh.resplit_merged_ai_chat`
    **migration.** ``>``-joined with **no heading at all**, even when
    ``source.platform`` is ``chatgpt``: the migration re-renders the
    human half itself rather than replaying the ingestor, so a ChatGPT
    fragment can carry the Claude-looking shape (a).
(d) **Claude merged** (pre-#1333). The blockquoted human turn followed
    by the model's plain, unquoted prose. No speaker markers anywhere.
(e) **ChatGPT merged** (pre-#1333). A ``# Title`` heading plus the
    literal ``**User**:`` / ``**Assistant**:`` markers inside one
    blockquote.

:attr:`BodyShape.MERGED` means "both halves were recovered". That one
predicate is simultaneously

* the migration's *split this fragment* test — ``resplit_merged_ai_chat``
  acts only on a body whose two halves it can name, and
* the fingerprint's *keep the human half* rule — the corpus takes
  ``human`` whatever the shape,

which is why there is one function here and not one per consumer. Two
functions answering the same question is what let the fingerprint go on
recognising only shape (e) after #1333 made (a) the common case.

An **assistant-only** body is :attr:`BodyShape.UNRECOVERABLE` and must
stay excluded from both consumers. It is reachable, not hypothetical:
the merged Claude rendering blockquoted whatever
``ClaudeIngestor._extract_text`` (``creek/ingest/claude.py:50-71``)
returned for the human send, and that is ``""`` for an image-only or
tool-only send, leaving ``"> \\n\\n{AI reply}"`` on disk. Contributing
the whole body when the human half cannot be identified would admit
that entire model reply to the voice corpus at 217.4 AI-vocabulary hits
per 1000 words. When the human half cannot be found, contribute
nothing.

Both arms take a blockquote marker off with ``line[1:].lstrip()``, which
removes **exactly one**. The rendering adds one marker, so the parse
removes one: every writer of a stored body interpolates ``f"> {line}"``,
so a line the operator began with ``>`` themselves comes back as
``"> > he said hi"`` and must unwind to ``"> he said hi"``.

This is deliberately *not*
:func:`creek.generate.ai_style.fingerprint._strip_quote`'s greedy
``line.lstrip(">").strip()`` — but the reason is parity, not a measured
difference, and the tempting justification for it is false. On every
body a shipped writer can produce the two are **identical**, because
``lstrip(">")`` halts at the space that ``f"> {line}"`` always inserts:
``"> > he said hi"`` yields ``"> he said hi"`` under both. They diverge
only on a line beginning ``">>"`` with no space, which neither ingestor
nor :func:`creek.ingest.refresh._build_split_fragment` can emit. The
one-marker form is kept because it is the shipped
``refresh._parse_claude_merged`` semantics this module absorbed, and
byte-parity with the migration's verdict is a tested requirement; it is
also the more conservative reading of a hand-edited vault file.

Two deliberate absences in :func:`_split_chatgpt`, both of which look
like oversights until you check what the renderer emits:

* **No blockquote-shape test.**
  :meth:`creek.ingest.chatgpt.ChatGPTIngestor.convert_to_markdown`
  (``creek/ingest/chatgpt.py:129-144``) blockquotes *both* roles, so
  blockquote-ness carries zero role information for ChatGPT. The only
  ChatGPT role signals that exist are the ``**User**:`` /
  ``**Assistant**:`` literals and the fragment's ``source.author``.
* **No "unmarkered ChatGPT is SPLIT iff nothing escapes the
  blockquote" branch.** Such a branch was absent from the shipped
  ``refresh._parse_chatgpt_merged`` this arm replaces, and adding it
  now would make
  ``resplit_merged_ai_chat`` split — and ``md_file.unlink()`` — a
  fragment it has always left alone. An unmarkered ChatGPT body with
  prose outside the quote is ``SPLIT``, and all of its text, escaped
  prose included, goes to the human half.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_Message = TypeVar("_Message")

CONVERSATION_PLATFORMS: Final[frozenset[str]] = frozenset({"claude", "chatgpt"})
"""The ``source.platform`` values whose bodies interleave two speakers.

The single definition of "this platform is a chat". It replaced a
private frozenset in the re-split migration and another in the voice
fingerprint (#1426): two hand-maintained copies of one fact, where a
third platform added to one and not the other is a silent divergence
that shows up only as a quietly wrong voice corpus.
"""

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


# --- Reading a stored conversation body back (#1426) ------------------------


class BodyShape(enum.Enum):
    """How much of a stored conversation body could be attributed.

    A plain :class:`enum.Enum`, deliberately not a ``StrEnum``: a member
    that compares equal to a bare string collapses into whatever other
    string-keyed vocabulary it is handed to, and the two consumers here
    already pass platform names and author names around as strings.
    Identity (``is``) comparison is the intended use.

    Attributes:
        MERGED: Both halves were recovered — the body fuses a human turn
            and the model's reply, as pre-#1333 vaults stored them.
        SPLIT: A human half only. The per-turn shape written since
            #1333/#553, and the shape the migration leaves behind.
        UNRECOVERABLE: No human half could be identified. Either the body
            is the model's alone or it is not a conversation turn at all.
    """

    MERGED = enum.auto()
    SPLIT = enum.auto()
    UNRECOVERABLE = enum.auto()


@dataclass(frozen=True)
class ConversationTurns:
    """The two speakers' halves of one stored conversation body.

    Frozen because two consumers — the re-split migration and the voice
    fingerprint — now read the same verdict, and a mutable one would let
    either of them rewrite what the other sees.

    Attributes:
        shape: What :func:`split_conversation_body` was able to
            attribute; see :class:`BodyShape`.
        human: The operator's own text, with blockquote markers and any
            model-written heading removed. ``""`` when none was found.
        assistant: The model's text. ``""`` when none was found.
    """

    shape: BodyShape
    human: str
    assistant: str


def _split_claude(body: str) -> tuple[str, str]:
    """Split a Claude body by blockquote membership.

    Claude renders the human turn blockquoted and the model's reply as
    plain prose, so for this platform — unlike ChatGPT — the ``>``
    prefix genuinely is the role signal. Blank unquoted lines are
    dropped from the assistant bucket: the ``"> "``-prefixed blank line
    that :data:`TURN_TEXT_SEPARATOR` produces inside a two-send human
    turn is quoted, but a body hand-edited to lose that prefix must not
    read as the model speaking.

    ``line[1:].lstrip()`` removes exactly one blockquote marker; see the
    module docstring on why a greedy strip loses the operator's own.

    Args:
        body: The stored fragment body.

    Returns:
        ``(human, assistant)``; either may be ``""``.
    """
    quoted = [line[1:].lstrip() for line in body.split("\n") if line.startswith(">")]
    plain = [
        line for line in body.split("\n") if not line.startswith(">") and line.strip()
    ]
    return "\n".join(quoted).strip(), "\n".join(plain).strip()


def _split_chatgpt(body: str) -> tuple[str, str]:
    """Split a ChatGPT body on its ``**User**:`` / ``**Assistant**:`` markers.

    The ``startswith(">")`` test comes **first**, and the ``"# "`` drop
    rule is applied only to a line that is still quoted. That ordering
    is what makes a heading the operator actually typed survive — it
    renders as ``"> # …"`` and so never reaches the drop rule — while
    the model-written conversation title, which the renderer emits
    unquoted, is discarded.

    With no ``**User**:`` marker there is nothing to partition on, and
    the whole (unquoted, de-headed) text is the human's; see this
    module's docstring for why no blockquote-shape branch is added here,
    and for why ``line[1:].lstrip()`` removes only one marker.

    Args:
        body: The stored fragment body.

    Returns:
        ``(human, assistant)``; either may be ``""``.
    """
    cleaned: list[str] = []
    for line in body.split("\n"):
        if line.startswith(">"):
            cleaned.append(line[1:].lstrip())
        elif not line.startswith("# "):
            cleaned.append(line)
    text = "\n".join(cleaned)
    if "**User**:" not in text:
        return text.strip(), ""
    user_part, _, assistant_part = text.partition("**Assistant**:")
    return user_part.replace("**User**:", "").strip(), assistant_part.strip()


def _classify(human: str, assistant: str) -> BodyShape:
    """Derive the body's shape from which halves came back non-empty.

    Args:
        human: The recovered human half.
        assistant: The recovered assistant half.

    Returns:
        :attr:`BodyShape.MERGED` when both halves are present,
        :attr:`BodyShape.SPLIT` when only the human half is, and
        :attr:`BodyShape.UNRECOVERABLE` otherwise — including the
        assistant-only case, where there is no human half to keep.
    """
    if human and assistant:
        return BodyShape.MERGED
    if human:
        return BodyShape.SPLIT
    return BodyShape.UNRECOVERABLE


def split_conversation_body(platform: str, body: str) -> ConversationTurns:
    """Recover the human and assistant halves of a stored chat body.

    The one classifier both the re-split migration and the voice
    fingerprint consult. See this module's docstring for the five body
    shapes it must read and why the two consumers share it.

    Args:
        platform: The fragment's ``source.platform``. Anything other
            than ``"claude"`` takes the ChatGPT arm; callers gate on
            :data:`CONVERSATION_PLATFORMS` before asking.
        body: The stored fragment body.

    Returns:
        A :class:`ConversationTurns` carrying the shape and both halves.
    """
    human, assistant = (
        _split_claude(body) if platform == "claude" else _split_chatgpt(body)
    )
    return ConversationTurns(_classify(human, assistant), human, assistant)
