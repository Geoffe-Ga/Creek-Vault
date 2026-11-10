"""Personal mission and values documents for Reconcile.

``creek.generate.mission`` picks the owner's own value-bearing fragments under
a privacy ceiling, asks the model for a mission and a short list of values in
the owner's voice, and renders the answer as Markdown that Reconcile accepts
as a personal Document: a title of 1 to 120 code points, and a body whose
Atoms split at blank lines.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate.drafts import DraftLLMResponse
from creek.generate.mission import (
    DEFAULT_MISSION_TITLE,
    MAX_EXCERPT_CHARS,
    MAX_VALUES,
    MISSION_NOTE_TYPE,
    MISSION_SUBDIR,
    RECONCILE_TITLE_MAX,
    MissionDocument,
    MissionParseError,
    MissionSource,
    MissionValue,
    build_mission_prompt,
    content_tier,
    generate_mission,
    parse_mission_response,
    reconcile_title,
    render_reconcile_markdown,
    save_mission,
    select_mission_sources,
)
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

_CUE_BODY = (
    "I believe a group should keep the promises it makes to its quietest member."
)
_PLAIN_BODY = "Bought groceries and walked the dog along the river."
_GENERATED = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def _write_fragment(
    vault: Path,
    frag_id: str,
    *,
    body: str = _CUE_BODY,
    tier: str | None = "open",
    primary: str = "F1",
    created: datetime | None = None,
    author: str = "self",
    author_slug: str | None = None,
    representativeness: str = "self",
    praxis: str = "none",
    title: str | None = None,
) -> None:
    """Write one fragment under ``01-Fragments/Notes``."""
    when = (created or datetime(2026, 5, 1, tzinfo=UTC)).isoformat()
    source: dict[str, Any] = {"platform": "journal", "author": author}
    if author_slug is not None:
        source["author_slug"] = author_slug
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title or f"Title {frag_id}",
        "created": when,
        "ingested": when,
        "source": source,
        "frequency": {"primary": primary, "secondary": []},
        "representativeness": representativeness,
        "praxis_potential": praxis,
    }
    if tier is not None:
        metadata["privacy_tier"] = tier
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


def _ids(sources: list[MissionSource]) -> list[str]:
    """Return the fragment ids of *sources*, in order."""
    return [source.fragment_id for source in sources]


def _document(**overrides: Any) -> MissionDocument:
    """Return a small, valid mission document."""
    fields: dict[str, Any] = {
        "title": "What I stand for",
        "mission": "I keep my word and I make room for the quiet ones.",
        "values": (
            MissionValue(name="Honesty", statement="I say what I mean."),
            MissionValue(name="Care", statement="I check on people first."),
        ),
    }
    fields.update(overrides)
    return MissionDocument(**fields)


def _response(**overrides: Any) -> str:
    """Return a JSON model answer, with *overrides* replacing its fields."""
    payload: dict[str, Any] = {
        "title": "What I stand for",
        "mission": "I keep my word.",
        "values": [
            {"name": "Honesty", "statement": "I say what I mean."},
            {"name": "Care", "statement": "I check on people first."},
            {"name": "Patience", "statement": "I let things take time."},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_admits_an_open_fragment_that_states_a_value(tmp_path: Path) -> None:
    """An open, self-authored fragment with a value cue is a source."""
    _write_fragment(tmp_path, "frag-open")

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-open"]
    assert sources[0].tier is PrivacyTier.OPEN
    assert sources[0].excerpt == _CUE_BODY
    assert sources[0].title == "Title frag-open"


def test_selection_ranks_a_fragment_with_no_value_signal_last(tmp_path: Path) -> None:
    """Groceries are not a value: they rank after every fragment that states one."""
    _write_fragment(
        tmp_path,
        "frag-plain",
        body=_PLAIN_BODY,
        created=datetime(2026, 9, 1, tzinfo=UTC),
    )
    _write_fragment(tmp_path, "frag-cue")

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-cue", "frag-plain"]
    assert sources[1].score == 0


def test_selection_falls_back_to_recent_writing_when_nothing_states_a_value(
    tmp_path: Path,
) -> None:
    """A vault that never names a value outright still has the owner's voice."""
    _write_fragment(tmp_path, "frag-plain", body=_PLAIN_BODY)

    assert _ids(select_mission_sources(tmp_path, override=None)) == ["frag-plain"]


def test_aspiration_alone_does_not_lift_a_fragment_above_one_with_a_value(
    tmp_path: Path,
) -> None:
    """Aspiration adds weight to a value, but is not a value on its own."""
    _write_fragment(
        tmp_path,
        "frag-aspire-plain",
        body=_PLAIN_BODY,
        representativeness="aspirational",
        created=datetime(2026, 9, 1, tzinfo=UTC),
    )
    _write_fragment(tmp_path, "frag-cue")

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-cue", "frag-aspire-plain"]
    assert sources[1].score == 0


def test_selection_admits_a_community_love_fragment_without_a_cue(
    tmp_path: Path,
) -> None:
    """F4 (moral grounding, shared values) qualifies on frequency alone."""
    _write_fragment(tmp_path, "frag-f4", body=_PLAIN_BODY, primary="F4")

    assert _ids(select_mission_sources(tmp_path, override=None)) == ["frag-f4"]


def test_selection_leaves_personal_out_entirely_at_the_default_ceiling(
    tmp_path: Path,
) -> None:
    """A hard cutoff: no title-only summary of a personal fragment slips in."""
    _write_fragment(tmp_path, "frag-personal", tier="personal")

    assert select_mission_sources(tmp_path, override=None) == []
    assert select_mission_sources(tmp_path, override=PrivacyTierOverride.OPEN) == []


def test_selection_admits_personal_under_a_personal_override(tmp_path: Path) -> None:
    """``--include-tier personal`` admits a personal fragment's full body."""
    _write_fragment(tmp_path, "frag-personal", tier="personal")

    sources = select_mission_sources(tmp_path, override=PrivacyTierOverride.PERSONAL)

    assert _ids(sources) == ["frag-personal"]
    assert sources[0].excerpt == _CUE_BODY
    assert sources[0].tier is PrivacyTier.PERSONAL


def test_selection_keeps_intimate_out_below_an_intimate_override(
    tmp_path: Path,
) -> None:
    """Intimate content needs an intimate or all override."""
    _write_fragment(tmp_path, "frag-intimate", tier="intimate")

    assert select_mission_sources(tmp_path, override=PrivacyTierOverride.PERSONAL) == []
    admitted = select_mission_sources(tmp_path, override=PrivacyTierOverride.ALL)
    assert _ids(admitted) == ["frag-intimate"]
    assert admitted[0].tier is PrivacyTier.INTIMATE


def test_selection_treats_a_missing_tier_as_intimate(tmp_path: Path) -> None:
    """A fragment that never wrote its tier down fails closed to intimate."""
    _write_fragment(tmp_path, "frag-untiered", tier=None)

    assert select_mission_sources(tmp_path, override=PrivacyTierOverride.PERSONAL) == []
    admitted = select_mission_sources(tmp_path, override=PrivacyTierOverride.INTIMATE)
    assert admitted[0].tier is PrivacyTier.INTIMATE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author", "ai"),
        ("author", "other"),
        ("author", "collaborative"),
        ("representativeness", "reference"),
    ],
)
def test_selection_uses_only_the_owners_own_words(
    tmp_path: Path, field: str, value: str
) -> None:
    """A mission statement is the owner's: borrowed or AI words are left out."""
    _write_fragment(tmp_path, "frag-borrowed", **{field: value})

    assert select_mission_sources(tmp_path, override=PrivacyTierOverride.ALL) == []


def test_selection_leaves_out_another_authors_fragment(tmp_path: Path) -> None:
    """A fragment carrying an ``author_slug`` belongs to someone else."""
    _write_fragment(tmp_path, "frag-other", author_slug="a-friend")

    assert select_mission_sources(tmp_path, override=PrivacyTierOverride.ALL) == []


def test_selection_admits_an_aspirational_fragment(tmp_path: Path) -> None:
    """Aspirational writing is exactly what a mission statement draws on."""
    _write_fragment(tmp_path, "frag-aspire", representativeness="aspirational")

    assert _ids(select_mission_sources(tmp_path, override=None)) == ["frag-aspire"]


def test_selection_ranks_by_signal_then_recency_then_id(tmp_path: Path) -> None:
    """More value cues rank first; ties go to the newer, then the lower id."""
    two_cues = "I believe in showing up. Integrity matters to me."
    _write_fragment(tmp_path, "frag-b-old", created=datetime(2026, 1, 1, tzinfo=UTC))
    _write_fragment(tmp_path, "frag-a-old", created=datetime(2026, 1, 1, tzinfo=UTC))
    _write_fragment(tmp_path, "frag-new", created=datetime(2026, 6, 1, tzinfo=UTC))
    _write_fragment(tmp_path, "frag-strong", body=two_cues)

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-strong", "frag-new", "frag-a-old", "frag-b-old"]
    assert sources[0].score > sources[1].score


def test_selection_scores_f4_and_aspiration_above_a_bare_cue(tmp_path: Path) -> None:
    """Each signal adds to the score, so the strongest source leads."""
    _write_fragment(tmp_path, "frag-bare")
    _write_fragment(tmp_path, "frag-f4", primary="F4")
    _write_fragment(
        tmp_path, "frag-both", primary="F4", representativeness="aspirational"
    )

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-both", "frag-f4", "frag-bare"]
    assert [source.score for source in sources] == [4, 3, 1]


def test_selection_adds_weight_for_a_value_acted_on(tmp_path: Path) -> None:
    """A value acted on (praxis potential) outranks one only stated."""
    _write_fragment(tmp_path, "frag-said", created=datetime(2026, 9, 1, tzinfo=UTC))
    _write_fragment(tmp_path, "frag-done", praxis="explicit")

    sources = select_mission_sources(tmp_path, override=None)

    assert _ids(sources) == ["frag-done", "frag-said"]
    assert [source.score for source in sources] == [2, 1]


def test_selection_honours_the_limit(tmp_path: Path) -> None:
    """Only the top *limit* sources are kept."""
    for index in range(5):
        _write_fragment(tmp_path, f"frag-{index}")

    assert len(select_mission_sources(tmp_path, override=None, limit=3)) == 3


@pytest.mark.parametrize("limit", [0, -1])
def test_selection_refuses_a_limit_below_one(tmp_path: Path, limit: int) -> None:
    """A mission drawn from nothing is not a request this module accepts."""
    with pytest.raises(ValueError, match="limit must be at least 1"):
        select_mission_sources(tmp_path, override=None, limit=limit)


def test_selection_trims_a_long_body_to_the_excerpt_budget(tmp_path: Path) -> None:
    """A long fragment is cut at a word boundary and marked with an ellipsis."""
    long_body = "I believe " + "steady work " * 400
    _write_fragment(tmp_path, "frag-long", body=long_body)

    excerpt = select_mission_sources(tmp_path, override=None)[0].excerpt

    assert len(excerpt) <= MAX_EXCERPT_CHARS + 1
    assert excerpt.endswith("…")
    assert excerpt.startswith("I believe steady work")
    assert not excerpt[:-1].endswith(" ")


def test_selection_of_an_empty_vault_is_empty(tmp_path: Path) -> None:
    """A freshly initialised vault has nothing to draw on, and that is not an error."""
    assert select_mission_sources(tmp_path, override=None) == []


# ---------------------------------------------------------------------------
# Routing tier
# ---------------------------------------------------------------------------


def test_content_tier_is_the_most_sensitive_source() -> None:
    """The routing tier is the worst tier the prompt carries."""
    sources = [
        MissionSource("a", "A", "x", PrivacyTier.OPEN, 1),
        MissionSource("b", "B", "y", PrivacyTier.PERSONAL, 1),
    ]

    assert content_tier(sources) is PrivacyTier.PERSONAL
    assert content_tier(sources[:1]) is PrivacyTier.OPEN


def test_content_tier_of_nothing_fails_closed() -> None:
    """No evidence about the prompt is treated as the worst case."""
    assert content_tier([]) is PrivacyTier.INTIMATE


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_prompt_carries_each_source_inside_its_own_delimiter() -> None:
    """The model can tell the owner's words from the task."""
    sources = [
        MissionSource("frag-1", "On promises", "I keep my word.", PrivacyTier.OPEN, 1),
        MissionSource("frag-2", "On care", "I check in first.", PrivacyTier.OPEN, 1),
    ]

    prompt = build_mission_prompt(sources)

    assert (
        '<fragment id="frag-1" title="On promises">\nI keep my word.\n</fragment>'
        in (prompt)
    )
    assert '<fragment id="frag-2" title="On care">\nI check in first.\n</fragment>' in (
        prompt
    )
    assert prompt.index("Return only a JSON object") < prompt.index("<fragment")


def test_prompt_asks_for_first_person_grounded_unidentifying_json() -> None:
    """The instructions carry the rules the output depends on."""
    prompt = build_mission_prompt(
        [MissionSource("f", "T", "I keep my word.", PrivacyTier.OPEN, 1)]
    )

    for phrase in (
        "first person",
        "Use only what these notes support",
        "Do not name any person, group, organization, or place",
        f"between 3 and {MAX_VALUES} values",
        '"title"',
        '"mission"',
        '"values"',
    ):
        assert phrase in prompt


def test_prompt_includes_the_voice_preamble_and_voice_core_when_given() -> None:
    """Voice targets and the owner's voice core steer the wording."""
    sources = [MissionSource("f", "T", "I keep my word.", PrivacyTier.OPEN, 1)]

    prompt = build_mission_prompt(
        sources, style_preamble="## Voice targets\n- plain", voice_core="Short lines."
    )

    assert "## Voice targets\n- plain" in prompt
    assert "<voice_core>\nShort lines.\n</voice_core>" in prompt


def test_prompt_leaves_out_empty_voice_sections() -> None:
    """No voice material means no empty voice section."""
    prompt = build_mission_prompt(
        [MissionSource("f", "T", "I keep my word.", PrivacyTier.OPEN, 1)]
    )

    assert "<voice_core>" not in prompt
    assert "## Voice targets" not in prompt


def test_prompt_escapes_quotes_in_a_title_attribute() -> None:
    """A title cannot close its own delimiter early."""
    sources = [MissionSource("f", 'Say "no"', "I keep my word.", PrivacyTier.OPEN, 1)]

    assert 'title="Say &quot;no&quot;"' in build_mission_prompt(sources)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_reads_a_plain_json_answer() -> None:
    """The happy path keeps every field."""
    document = parse_mission_response(_response())

    assert document.title == "What I stand for"
    assert document.mission == "I keep my word."
    assert [value.name for value in document.values] == ["Honesty", "Care", "Patience"]
    assert document.values[0].statement == "I say what I mean."


def test_parse_reads_a_fenced_answer_with_prose_around_it() -> None:
    """Models wrap JSON in fences and chatter; the object is still found."""
    wrapped = f"Here you go:\n```json\n{_response()}\n```\nHope it helps."

    assert parse_mission_response(wrapped).mission == "I keep my word."


def test_parse_keeps_at_most_the_value_cap() -> None:
    """A model that lists too many values is cut to the cap, in order."""
    values = [{"name": f"V{n}", "statement": f"S{n}."} for n in range(MAX_VALUES + 3)]

    document = parse_mission_response(_response(values=values))

    assert [value.name for value in document.values] == [
        f"V{n}" for n in range(MAX_VALUES)
    ]


def test_parse_skips_blank_value_entries() -> None:
    """An empty name or statement is dropped rather than rendered as a gap."""
    values = [
        {"name": "  ", "statement": "Nameless."},
        {"name": "Silent", "statement": ""},
        {"name": "Kept", "statement": "I stay."},
    ]

    document = parse_mission_response(_response(values=values))

    assert [value.name for value in document.values] == ["Kept"]


@pytest.mark.parametrize(
    "text",
    [
        "no json here at all",
        "{not valid json}",
        "[1, 2, 3]",
        json.dumps({"title": "T", "values": []}),
        json.dumps({"title": "T", "mission": "  ", "values": []}),
        json.dumps({"title": "T", "mission": "M", "values": "lots"}),
        json.dumps({"title": "T", "mission": "M", "values": []}),
        json.dumps({"title": "T", "mission": 7, "values": []}),
        json.dumps({"title": "T", "mission": "M", "values": [{"name": "A"}]}),
        json.dumps({"title": "T", "mission": "M", "values": ["Honesty"]}),
    ],
)
def test_parse_refuses_an_answer_it_cannot_read(text: str) -> None:
    """Anything that is not a mission with at least one value is refused."""
    with pytest.raises(MissionParseError):
        parse_mission_response(text)


def test_parse_falls_back_to_the_default_title() -> None:
    """A missing or non-string title gets the default rather than a refusal."""
    assert parse_mission_response(_response(title=None)).title == DEFAULT_MISSION_TITLE
    assert parse_mission_response(_response(title="")).title == DEFAULT_MISSION_TITLE


def test_parse_collapses_line_breaks_inside_a_field() -> None:
    """A field is one paragraph, so a blank line cannot split an Atom."""
    document = parse_mission_response(_response(mission="I keep\n\nmy   word."))

    assert document.mission == "I keep my word."


def test_parse_strips_markup_from_a_value_name() -> None:
    """``**Honesty:**`` becomes ``Honesty`` so the heading reads cleanly."""
    values = [{"name": "**Honesty:**", "statement": "I say what I mean."}]

    assert parse_mission_response(_response(values=values)).values[0].name == "Honesty"


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------


def test_title_is_trimmed_and_unmarked() -> None:
    """Heading marks, quotes and surrounding space leave the title."""
    assert reconcile_title('  # "What I stand for"  ') == "What I stand for"


def test_title_fits_reconciles_limit_at_a_word_boundary() -> None:
    """Reconcile takes 1 to 120 code points, trimmed."""
    long_title = " ".join(["steadfast"] * 30)

    title = reconcile_title(long_title)

    assert len(title) <= RECONCILE_TITLE_MAX
    assert title == title.strip()
    assert title.endswith("steadfast")


def test_title_cuts_a_single_long_word_hard() -> None:
    """With no space to break at, the title is cut at the limit."""
    title = reconcile_title("x" * 300)

    assert title == "x" * RECONCILE_TITLE_MAX


def test_title_is_normalized_to_nfc() -> None:
    """A decomposed accent becomes one code point, as Reconcile counts it."""
    assert reconcile_title("Café values") == "Café values"


def test_blank_title_becomes_the_default() -> None:
    """A title Reconcile would refuse is replaced, never sent empty."""
    assert reconcile_title("  #  ") == DEFAULT_MISSION_TITLE


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_lays_out_mission_then_one_atom_per_value() -> None:
    """The exact Markdown Reconcile receives."""
    assert render_reconcile_markdown(_document()) == (
        "## Mission\n"
        "I keep my word and I make room for the quiet ones.\n"
        "\n"
        "## Values\n"
        "\n"
        "### Honesty\n"
        "I say what I mean.\n"
        "\n"
        "### Care\n"
        "I check on people first.\n"
    )


def test_render_splits_into_the_atoms_reconcile_expects() -> None:
    """Splitting at blank lines yields the mission, the heading, then each value."""
    markdown = render_reconcile_markdown(_document())

    atoms = [atom for atom in markdown.split("\n\n") if atom.strip()]

    assert len(atoms) == 2 + len(_document().values)
    assert all(line == line.rstrip(" \t") for line in markdown.splitlines())
    assert markdown.endswith("\n")
    assert not markdown.endswith("\n\n")


def test_render_escapes_a_statement_that_would_start_a_block() -> None:
    """A statement opening with ``#`` or a list marker stays a paragraph."""
    document = _document(
        values=(
            MissionValue(name="Focus", statement="# one thing at a time"),
            MissionValue(name="Order", statement="- first things first"),
            MissionValue(name="Count", statement="1. then the next"),
            MissionValue(name="Quote", statement="> as I was told"),
            MissionValue(name="Star", statement="* one more"),
            MissionValue(name="Stress", statement="*Always* kind"),
        ),
    )

    markdown = render_reconcile_markdown(document)

    assert "### Focus\n\\# one thing at a time\n" in markdown
    assert "### Order\n\\- first things first\n" in markdown
    # CommonMark escapes only punctuation, so an ordered marker escapes its dot.
    assert "### Count\n1\\. then the next\n" in markdown
    assert "### Quote\n\\> as I was told\n" in markdown
    assert "### Star\n\\* one more\n" in markdown
    # Emphasis is not a block marker and is left alone.
    assert "### Stress\n*Always* kind\n" in markdown


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def _sources() -> list[MissionSource]:
    """Return two sources of different tiers."""
    return [
        MissionSource("frag-1", "One", "I keep my word.", PrivacyTier.OPEN, 1),
        MissionSource("frag-2", "Two", "I check in.", PrivacyTier.PERSONAL, 1),
    ]


def test_save_writes_a_dated_note_whose_body_is_the_paste_ready_markdown(
    tmp_path: Path,
) -> None:
    """The saved note records its provenance and tier, and its body is the doc."""
    document = _document()

    path = save_mission(tmp_path, document, sources=_sources(), generated=_GENERATED)

    assert path == tmp_path / MISSION_SUBDIR / "2026-11-10-what-i-stand-for.md"
    post = frontmatter.load(path)
    assert post["type"] == MISSION_NOTE_TYPE
    assert post["title"] == "What I stand for"
    assert post["source_fragments"] == ["frag-1", "frag-2"]
    assert post["privacy_tier"] == "personal"
    assert post["generated_date"] == _GENERATED.isoformat()
    assert "inference_source" not in post.metadata
    assert post.content + "\n" == render_reconcile_markdown(document)


def test_save_never_overwrites_a_same_day_note(tmp_path: Path) -> None:
    """A second mission on the same day gets its own file."""
    first = save_mission(
        tmp_path, _document(), sources=_sources(), generated=_GENERATED
    )
    second = save_mission(
        tmp_path, _document(), sources=_sources(), generated=_GENERATED
    )

    assert first != second
    assert second.name == "2026-11-10-what-i-stand-for-2.md"


def test_save_stamps_the_route_label_when_given(tmp_path: Path) -> None:
    """The MCP adapter records which route wrote the body; never a key."""
    path = save_mission(
        tmp_path,
        _document(),
        sources=_sources(),
        generated=_GENERATED,
        inference_source="local",
    )

    assert frontmatter.load(path)["inference_source"] == "local"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_generate_prompts_once_parses_renders_and_saves(tmp_path: Path) -> None:
    """One model turn becomes a saved, paste-ready document."""
    prompts: list[str] = []

    def _llm(prompt: str) -> str:
        prompts.append(prompt)
        return _response()

    result = generate_mission(
        tmp_path,
        llm=_llm,
        sources=_sources(),
        style_preamble="## Voice targets",
        voice_core="Short lines.",
        generated=_GENERATED,
    )

    assert len(prompts) == 1
    assert "frag-1" in prompts[0]
    assert "## Voice targets" in prompts[0]
    assert result.document.title == "What I stand for"
    assert result.markdown == render_reconcile_markdown(result.document)
    assert result.path.is_file()
    assert result.sources == tuple(_sources())


def test_generate_accepts_a_draft_style_response(tmp_path: Path) -> None:
    """The draft factories answer with a text-and-stop-reason pair."""
    result = generate_mission(
        tmp_path,
        llm=lambda _prompt: DraftLLMResponse(text=_response(), stop_reason="end_turn"),
        sources=_sources(),
        generated=_GENERATED,
    )

    assert result.document.mission == "I keep my word."


def test_generate_saves_nothing_when_the_answer_cannot_be_read(
    tmp_path: Path,
) -> None:
    """A refused answer leaves no half-written note behind."""
    with pytest.raises(MissionParseError):
        generate_mission(
            tmp_path,
            llm=lambda _prompt: "I'd rather not.",
            sources=_sources(),
            generated=_GENERATED,
        )

    assert not (tmp_path / MISSION_SUBDIR).exists()


def test_generate_refuses_to_run_without_sources(tmp_path: Path) -> None:
    """A mission drawn from no notes would be invented, so it is refused."""

    def _never(_prompt: str) -> str:
        raise AssertionError("the model must not be called without sources")

    with pytest.raises(ValueError, match="at least one source"):
        generate_mission(tmp_path, llm=_never, sources=[], generated=_GENERATED)
