"""Personal mission and values documents for Reconcile.

Reconcile keeps a person's own mission statement on their device as a title
and a Markdown body (its personal page). This module drafts one from the
owner's own vault: it picks their fragments, the ones where they say what
they value first, asks the ``generation`` model for a mission and a few
values in their voice, and renders the answer as Markdown Reconcile accepts
as it stands.

**What Reconcile needs from the text.** A title of 1 to 120 code points after
NFC, with no space around it, and a body that splits into Atoms at blank
lines. The body is laid out so each Atom is whole on its own: the mission
under its heading, a ``## Values`` heading, then one ``### <value>`` heading
and its statement per value. No field may contain a blank line, so each field
is collapsed to one paragraph before rendering. The same text therefore works
on the personal page and as a group's founding Document.

**Whose words.** Only the owner's own fragments are sources: ``source.author``
is ``self``, no ``author_slug`` (another author's folder), and not a
``reference``. A mission statement made from someone else's words would be
theirs, not the owner's.

**The tier rule.** Admission is a hard cutoff,
:func:`~creek.classify.privacy_filter.tier_within_override` over
:func:`~creek.classify.privacy_filter.fragment_tier`: a fragment above the
override is left out entirely, and a fragment whose frontmatter never wrote a
tier down ranks as intimate. Personal fragments are never replaced by
title-only summaries here, as :func:`filter_fragments_by_tier` does, because
a summary carries a title into the prompt and nothing a mission can use. So
every byte the prompt carries is admitted, and :func:`content_tier` over the
selected sources is the whole of the prompt's sensitivity. Callers key the
model router with it (the MCP adapter combines it with the caller's ceiling);
this module never picks a provider.

**What is identifying.** A mission statement links a person to what they
believe, which Reconcile treats as high-risk data. The prompt asks for no
names of people, groups, organizations or places, so the draft says what the
owner values without saying who or where they are. The owner reviews and
edits it before it goes anywhere.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from operator import itemgetter
from typing import TYPE_CHECKING, Final, TypeAlias

import frontmatter

from creek.classify.privacy_filter import (
    fragment_tier,
    max_source_tier,
    tier_within_override,
)
from creek.generate.drafts import (
    INFERENCE_SOURCE_FRONTMATTER_KEY,
    DraftLLMResponse,
    reserve_dated_path,
)
from creek.models import Authorship, Frequency, PraxisPotential, PrivacyTier
from creek.time import now_la
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride
    from creek.models import Fragment

__all__ = [
    "DEFAULT_MISSION_LIMIT",
    "DEFAULT_MISSION_TITLE",
    "MAX_EXCERPT_CHARS",
    "MAX_MISSION_LIMIT",
    "MAX_VALUES",
    "MISSION_MAX_OUTPUT_TOKENS",
    "MISSION_NOTE_TYPE",
    "MISSION_SUBDIR",
    "RECONCILE_TITLE_MAX",
    "VALUE_CUES",
    "MissionDocument",
    "MissionLLM",
    "MissionParseError",
    "MissionResult",
    "MissionSource",
    "MissionValue",
    "build_mission_prompt",
    "content_tier",
    "generate_mission",
    "parse_mission_response",
    "reconcile_title",
    "render_reconcile_markdown",
    "save_mission",
    "select_mission_sources",
]

MISSION_SUBDIR: Final[str] = "07-Voice/Mission"
"""Where mission notes are saved, beside ``07-Voice/Drafts``.

Inside ``07-Voice`` so a fragment purge scrubs a mission's
``source_fragments`` exactly as it scrubs a draft's.
"""

MISSION_NOTE_TYPE: Final[str] = "mission"
"""The ``type`` a saved mission note carries in its frontmatter."""

DEFAULT_MISSION_TITLE: Final[str] = "My mission and values"
"""The title used when the model gives none Reconcile would accept."""

RECONCILE_TITLE_MAX: Final[int] = 120
"""Reconcile's longest personal Document title, in code points after NFC."""

DEFAULT_MISSION_LIMIT: Final[int] = 12
"""How many source fragments a mission draws on by default."""

MAX_MISSION_LIMIT: Final[int] = 40
"""The most source fragments one prompt may carry, whatever a caller asks."""

MAX_EXCERPT_CHARS: Final[int] = 1_200
"""The longest excerpt of one fragment the prompt carries."""

MAX_VALUES: Final[int] = 7
"""The most values a mission keeps; a longer list is cut, in order."""

MIN_REQUESTED_VALUES: Final[int] = 3
"""The fewest values the prompt asks for. Fewer are accepted, not refused."""

MISSION_MAX_OUTPUT_TOKENS: Final[int] = 2_048
"""Output budget for the one mission turn: a short JSON object with headroom."""

_MAX_VALUE_NAME_CHARS: Final[int] = 80
"""The longest value name kept, in code points; a heading, not a sentence."""

VALUE_CUES: Final[tuple[str, ...]] = (
    "i believe",
    "i value",
    "i care about",
    "matters to me",
    "what matters",
    "i stand for",
    "my purpose",
    "my mission",
    "i want to be",
    "i commit",
    "i promise",
    "i refuse to",
    "i won't",
    "principle",
    "integrity",
    "the kind of person",
    "i choose to",
    "values",
)
"""Lower-case phrases that mark a fragment where the owner states a value.

Each distinct phrase found adds one to a fragment's score. Matching is plain
substring matching on the lower-cased title and body: cheap, deterministic,
and easy to read against a fragment by hand.
"""

_F4_BONUS: Final[int] = 2
"""Score added for F4, Community Love: moral grounding and shared values."""

_ASPIRATION_BONUS: Final[int] = 1
"""Score added for an ``aspirational`` fragment: who the owner means to be."""

_PRAXIS_BONUS: Final[int] = 1
"""Score added for a fragment with praxis potential: a value acted on."""

_OWN_AUTHORSHIP: Final[str] = Authorship.SELF.value
_REFERENCE: Final[str] = "reference"
_ASPIRATIONAL: Final[str] = "aspirational"
_ELLIPSIS: Final[str] = "…"

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_HEADING_MARKS: Final[re.Pattern[str]] = re.compile(r"^#+\s*")
_NAME_EDGE_MARKS: Final[str] = "*_#:.`'\" "
_TITLE_QUOTES: Final[str] = "\"'\u201c\u201d\u2018\u2019"
_BLOCK_PUNCTUATION: Final[re.Pattern[str]] = re.compile(r"^(?:#|>|[-+*](?=\s))")
_ORDERED_MARKER: Final[re.Pattern[str]] = re.compile(r"^(\d+)([.)])(?=\s)")

MissionLLM: TypeAlias = Callable[[str], str | DraftLLMResponse]
"""A prompt-completion callable: plain text, or the draft factories' pair."""


class MissionParseError(ValueError):
    """The model's answer was not a mission with at least one value."""


@dataclass(frozen=True, slots=True)
class MissionSource:
    """One admitted fragment a mission draws on.

    Attributes:
        fragment_id: The fragment's id, recorded in the saved note.
        title: The fragment's title, shown to the model beside its excerpt.
        excerpt: The fragment's body, trimmed to :data:`MAX_EXCERPT_CHARS`.
        tier: The fragment's fail-closed tier, for routing only.
        score: How strongly it states a value; higher ranks first.
    """

    fragment_id: str
    title: str
    excerpt: str
    tier: PrivacyTier
    score: int


@dataclass(frozen=True, slots=True)
class MissionValue:
    """One value: a short name and the statement under it.

    Attributes:
        name: The value's name, used as its heading.
        statement: One paragraph saying what the value means to the owner.
    """

    name: str
    statement: str


@dataclass(frozen=True, slots=True)
class MissionDocument:
    """A parsed mission: title, mission paragraph and values.

    Attributes:
        title: A title Reconcile accepts (see :func:`reconcile_title`).
        mission: One paragraph.
        values: One to :data:`MAX_VALUES` values, in the model's order.
    """

    title: str
    mission: str
    values: tuple[MissionValue, ...]


@dataclass(frozen=True, slots=True)
class MissionResult:
    """A saved mission.

    Attributes:
        path: The saved note.
        document: The parsed document.
        markdown: The paste-ready body, exactly as saved.
        sources: The fragments the prompt carried.
    """

    path: Path
    document: MissionDocument
    markdown: str
    sources: tuple[MissionSource, ...]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _is_owners_own(fragment: Fragment) -> bool:
    """Return whether *fragment* holds the owner's own words."""
    source = fragment.source
    return (
        str(source.author) == _OWN_AUTHORSHIP
        and source.author_slug is None
        and str(fragment.representativeness) != _REFERENCE
    )


def _value_score(fragment: Fragment, body: str) -> int:
    """Return how strongly *fragment* states a value; ``0`` means not at all.

    Only a value cue or an F4 classification makes the score positive.
    Aspiration and praxis potential add weight to a fragment that already
    states a value, but never lift one that does not: an aspirational
    shopping list is still a shopping list.
    """
    text = f"{fragment.title}\n{body}".lower()
    score = sum(1 for cue in VALUE_CUES if cue in text)
    if str(fragment.frequency.primary) == Frequency.F4.value:
        score += _F4_BONUS
    if score == 0:
        return 0
    if str(fragment.representativeness) == _ASPIRATIONAL:
        score += _ASPIRATION_BONUS
    if str(fragment.praxis_potential) != PraxisPotential.NONE.value:
        score += _PRAXIS_BONUS
    return score


def _source_tier(fragment: Fragment, raw: dict[str, object]) -> PrivacyTier:
    """Return *fragment*'s fail-closed tier as a :class:`PrivacyTier`.

    :func:`~creek.classify.privacy_filter.fragment_tier` owns the rule (a
    missing ``privacy_tier`` key ranks as intimate). It hands back the model's
    field, which ``use_enum_values`` leaves a plain string, so it is coerced
    here; the loader validated it against the enum, so the coercion cannot
    fail.
    """
    return PrivacyTier(fragment_tier(fragment, raw))


def _excerpt(body: str) -> str:
    """Return *body* trimmed to :data:`MAX_EXCERPT_CHARS` at a word boundary."""
    text = body.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    cut = text[:MAX_EXCERPT_CHARS]
    head, space, _tail = cut.rpartition(" ")
    return f"{(head if space else cut).rstrip()}{_ELLIPSIS}"


def select_mission_sources(
    vault_path: Path,
    *,
    override: PrivacyTierOverride | None,
    limit: int = DEFAULT_MISSION_LIMIT,
) -> list[MissionSource]:
    """Return the owner's value-bearing fragments admitted under *override*.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        override: The admission ceiling. ``None`` is ``open``, the most
            restrictive. Above-ceiling fragments are left out entirely.
        limit: How many sources to keep, at least 1.

    Returns:
        Up to *limit* sources, strongest first, then newest, then by id, so
        the same vault always gives the same order. A fragment that states no
        value (score ``0``) still qualifies, after every one that does: on a
        vault that rarely names its values outright, the owner's recent
        writing is what the model has to hear their voice in, and the prompt
        tells it to leave out what the notes only mention in passing. A
        fragment ``creek classify`` has not tiered yet ranks with personal, so
        it still needs a personal override to be admitted.

    Raises:
        ValueError: When *limit* is below 1.
    """
    if limit < 1:
        msg = f"limit must be at least 1, got {limit}"
        raise ValueError(msg)
    ranked: list[tuple[tuple[int, float, str], MissionSource]] = []
    for _path, fragment, body, raw in iter_vault_fragments(vault_path / "01-Fragments"):
        tier = _source_tier(fragment, raw)
        if not tier_within_override(tier, override) or not _is_owners_own(fragment):
            continue
        score = _value_score(fragment, body)
        source = MissionSource(
            fragment_id=fragment.id,
            title=fragment.title.strip(),
            excerpt=_excerpt(body),
            tier=tier,
            score=score,
        )
        order = (-score, -fragment.created.timestamp(), fragment.id)
        ranked.append((order, source))
    ranked.sort(key=itemgetter(0))
    return [source for _order, source in ranked[:limit]]


def content_tier(sources: Iterable[MissionSource]) -> PrivacyTier:
    """Return the most sensitive tier among *sources*, failing closed when empty.

    Args:
        sources: The sources one prompt carries.

    Returns:
        The tier to key the model router with. It is a routing signal only;
        an MCP caller must never be told it.
    """
    return max_source_tier(source.tier for source in sources)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_INSTRUCTIONS: Final[str] = (
    "You are helping the person whose notes these are write their personal "
    "mission statement and values, to keep on their own personal page. Write "
    "in the first person, in their voice: reuse their own words and phrasing "
    "where you can, plainly, with no slogans or corporate language. Use only "
    "what these notes support. Do not invent commitments, causes, beliefs or "
    "history the notes do not show, and leave out anything they only mention "
    "in passing. Do not name any person, group, organization, or place, and "
    "do not quote a note at length: the statement says what they value, not "
    "who or where they are. Write a mission of one paragraph, two to four "
    f"sentences, and between {MIN_REQUESTED_VALUES} and {MAX_VALUES} values, "
    "each a short name of one to three words and a statement of one to three "
    "sentences saying what it means for how they act. Return only a JSON "
    'object, with no Markdown fence and nothing before or after it: {"title": '
    '"a short title for the document", "mission": "the mission paragraph", '
    '"values": [{"name": "the value", "statement": "what it means to them"}]}'
)


def _fragment_block(source: MissionSource) -> str:
    """Return one source inside its own delimiter."""
    fragment_id = html.escape(source.fragment_id, quote=True)
    title = html.escape(source.title, quote=True)
    return (
        f'<fragment id="{fragment_id}" title="{title}">\n{source.excerpt}\n</fragment>'
    )


def build_mission_prompt(
    sources: Sequence[MissionSource],
    *,
    style_preamble: str = "",
    voice_core: str = "",
) -> str:
    """Compose the mission prompt: instructions, voice material, then the notes.

    Args:
        sources: The admitted fragments, strongest first.
        style_preamble: The ``## Voice targets`` preamble built from the
            owner's voice fingerprint, or ``""``.
        voice_core: The owner's voice-core text, or ``""``.

    Returns:
        The prompt.
    """
    parts = [_INSTRUCTIONS]
    if style_preamble.strip():
        parts.append(style_preamble.strip())
    if voice_core.strip():
        parts.append(f"<voice_core>\n{voice_core.strip()}\n</voice_core>")
    parts.append("Their notes:")
    parts.extend(_fragment_block(source) for source in sources)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _one_line(text: str) -> str:
    """Return *text* as one NFC paragraph: every whitespace run is one space."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def reconcile_title(raw: str) -> str:
    """Return *raw* as a title Reconcile accepts, or the default.

    Heading marks and surrounding quotes are removed, the text is NFC and one
    line, and a title past :data:`RECONCILE_TITLE_MAX` code points is cut at
    the last space that fits (or hard, when there is none).

    Args:
        raw: The model's title.

    Returns:
        A trimmed title of 1 to :data:`RECONCILE_TITLE_MAX` code points.
    """
    title = _HEADING_MARKS.sub("", _one_line(raw)).strip(_TITLE_QUOTES).strip()
    if len(title) > RECONCILE_TITLE_MAX:
        cut = title[: RECONCILE_TITLE_MAX + 1]
        head, space, _tail = cut.rpartition(" ")
        title = head if space else title[:RECONCILE_TITLE_MAX]
        title = title.rstrip()
    return title or DEFAULT_MISSION_TITLE


def _json_object(text: str) -> dict[str, object]:
    """Return the one JSON object in *text*, ignoring fences and prose around it.

    The slice runs from the first ``{`` to the last ``}``, so whatever parses
    from it is an object: valid JSON that opens with a brace is nothing else.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        msg = "the mission answer holds no JSON object"
        raise MissionParseError(msg)
    try:
        data: dict[str, object] = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        msg = "the mission answer is not valid JSON"
        raise MissionParseError(msg) from error
    return data


def _required_text(data: dict[str, object], key: str) -> str:
    """Return the non-blank string at *key*, or refuse."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"the mission answer has no {key!r} text"
        raise MissionParseError(msg)
    return _one_line(value)


def _value_from(entry: object) -> MissionValue | None:
    """Return the value in *entry*, or ``None`` when it is not a whole one."""
    if not isinstance(entry, dict):
        return None
    name, statement = entry.get("name"), entry.get("statement")
    if not isinstance(name, str) or not isinstance(statement, str):
        return None
    clean_name = _one_line(name).strip(_NAME_EDGE_MARKS)[:_MAX_VALUE_NAME_CHARS]
    clean_statement = _one_line(statement)
    if not clean_name or not clean_statement:
        return None
    return MissionValue(name=clean_name.strip(), statement=clean_statement)


def _values(data: dict[str, object]) -> tuple[MissionValue, ...]:
    """Return the whole values in *data*, capped at :data:`MAX_VALUES`."""
    raw = data.get("values")
    if not isinstance(raw, list):
        msg = "the mission answer has no list of values"
        raise MissionParseError(msg)
    values = [value for value in map(_value_from, raw) if value is not None]
    if not values:
        msg = "the mission answer has no whole value"
        raise MissionParseError(msg)
    return tuple(values[:MAX_VALUES])


def parse_mission_response(text: str) -> MissionDocument:
    """Parse the model's JSON answer into a :class:`MissionDocument`.

    Args:
        text: The model's completion.

    Returns:
        The document, with every field collapsed to one paragraph.

    Raises:
        MissionParseError: When the answer holds no JSON object, has no
            mission text, or has no value with both a name and a statement.
    """
    data = _json_object(text)
    mission = _required_text(data, "mission")
    values = _values(data)
    raw_title = data.get("title")
    title = reconcile_title(raw_title if isinstance(raw_title, str) else "")
    return MissionDocument(title=title, mission=mission, values=values)


# ---------------------------------------------------------------------------
# Rendering and saving
# ---------------------------------------------------------------------------


def _paragraph(text: str) -> str:
    """Return *text* escaped so it cannot open a heading, quote or list."""
    text = _ORDERED_MARKER.sub(r"\1\\\2", text, count=1)
    return _BLOCK_PUNCTUATION.sub(lambda match: f"\\{match.group(0)}", text, count=1)


def render_reconcile_markdown(document: MissionDocument) -> str:
    """Render *document* as the Markdown body Reconcile receives.

    Each Atom is one block with no blank line inside it: the mission under
    ``## Mission``, the ``## Values`` heading, then each value under its own
    ``### <name>``. The text has no trailing spaces and ends in one line feed,
    as Reconcile's Atom normalizer leaves it.

    Args:
        document: The parsed mission.

    Returns:
        The body text.
    """
    atoms = [f"## Mission\n{_paragraph(document.mission)}", "## Values"]
    atoms.extend(
        f"### {value.name}\n{_paragraph(value.statement)}" for value in document.values
    )
    return "\n\n".join(atoms) + "\n"


def save_mission(
    vault_path: Path,
    document: MissionDocument,
    *,
    sources: Sequence[MissionSource],
    generated: datetime,
    inference_source: str | None = None,
) -> Path:
    """Write *document* to ``07-Voice/Mission/YYYY-MM-DD-{slug}.md``.

    The note's body is exactly the paste-ready Markdown, so the owner can copy
    it from Obsidian. Its frontmatter records the sources, and the most
    sensitive source tier as ``privacy_tier``, so any later reader of the note
    can gate it the way it would gate the fragments it came from.

    Args:
        vault_path: Vault root.
        document: The parsed mission.
        sources: The fragments the prompt carried.
        generated: When the mission was written; its date names the file.
        inference_source: The route label the MCP adapter recorded, or
            ``None`` to stamp nothing, as the CLI does. Never a key.

    Returns:
        The written note's path; an existing note is never overwritten.
    """
    target = reserve_dated_path(vault_path / MISSION_SUBDIR, document.title, generated)
    post = frontmatter.Post(
        content=render_reconcile_markdown(document),
        type=MISSION_NOTE_TYPE,
        title=document.title,
        source_fragments=[source.fragment_id for source in sources],
        privacy_tier=content_tier(sources).value,
        generated_date=generated.isoformat(),
    )
    if inference_source is not None:
        post[INFERENCE_SOURCE_FRONTMATTER_KEY] = inference_source
    target.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return target


def _completion_text(raw: str | DraftLLMResponse) -> str:
    """Return the text of a completion, whichever shape the factory returns."""
    return raw.text if isinstance(raw, DraftLLMResponse) else raw


def generate_mission(
    vault_path: Path,
    *,
    llm: MissionLLM,
    sources: Sequence[MissionSource],
    style_preamble: str = "",
    voice_core: str = "",
    generated: datetime | None = None,
    inference_source: str | None = None,
) -> MissionResult:
    """Prompt once, parse, render and save one mission.

    Args:
        vault_path: Vault root.
        llm: The routed completion callable, already keyed with
            :func:`content_tier` of *sources* (and, on MCP, the ceiling).
        sources: The admitted fragments, from :func:`select_mission_sources`.
        style_preamble: The ``## Voice targets`` preamble, or ``""``.
        voice_core: The owner's voice-core text, or ``""``.
        generated: When the mission is written; defaults to now in LA time.
        inference_source: The route label to stamp, or ``None``.

    Returns:
        The saved mission.

    Raises:
        ValueError: When *sources* is empty: a mission drawn from no notes
            would be invented.
        MissionParseError: When the answer cannot be read. Nothing is saved.
    """
    if not sources:
        msg = "a mission needs at least one source fragment"
        raise ValueError(msg)
    prompt = build_mission_prompt(
        sources, style_preamble=style_preamble, voice_core=voice_core
    )
    document = parse_mission_response(_completion_text(llm(prompt)))
    path = save_mission(
        vault_path,
        document,
        sources=sources,
        generated=generated if generated is not None else now_la(),
        inference_source=inference_source,
    )
    return MissionResult(
        path=path,
        document=document,
        markdown=render_reconcile_markdown(document),
        sources=tuple(sources),
    )
