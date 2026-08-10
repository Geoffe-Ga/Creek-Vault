"""The voice-skill tree must honour the caller's tier ceiling (#971).

``creek.skills.refresh`` accepts a ``privacy_tier_ceiling``, audits it and
echoes it back, but at the time these tests were written it never converted it
and never handed it to :class:`creek.generate.skills.SkillTreeGenerator`. The
CLI twin, ``creek skills generate``, parses ``--include-tier`` into a
``PrivacyTierOverride`` purely so the flag can be *audited*, then constructs the
generator without it.

The consequence, across the four fragment-derived categories this module
exercises, is not a read-side leak: for those, ``skills_refresh_tool`` returns
a count and fixed skill names (``F1``, ``rising``, ``express-do``…), never
content. It is a ``personal`` fragment's **full body, its title and its id**
copied verbatim into ``<vault>/creek-skills/**.SKILL.md`` for a caller who
declared ``ceiling=open``.

The response is *not* content-free in general, and this module does not claim
otherwise: a ``threads/`` or ``eddies/`` skill takes its filename from a title
derived from its member fragments, so those ``skill_paths`` can carry
above-ceiling vocabulary. That surface is ungated — ``Thread`` and ``Eddy``
have no ``privacy_tier`` field and ``_collect_typed`` takes no override — and
deliberately out of scope here: no test below seeds ``02-Threads`` or
``03-Eddies``, and the gap is tracked by #1284.

The fragment leak is looser than every sibling generation tool: ``creek.mine``,
``creek.draft`` and ``creek.author`` all route their corpus through
``filter_fragments_by_tier``, where a personal fragment at ``open`` contributes
a title-only summary instead of prose.

Three properties of the existing generator shape every assertion here, and each
one is the difference between a real test and a vacuous one:

1. **Nothing asserts on the tool's return value as evidence of exclusion.** A
   test that did would have passed against the unfixed code and proved nothing.
   The evidence is always the bytes of the files the call wrote.
2. **``intimate`` already does not leak**, via the ``allow_intimate=False``
   hardcode in ``_is_snapshot_fragment``. An intimate-only canary would have
   been excluded by the pre-existing filter and every test would have gone green
   with the ceiling still unthreaded. The above-ceiling canaries that carry the
   weight are therefore ``personal`` and *untiered* — the latter because an
   explicit ``unclassified`` ranks with ``personal`` (#876), so it leaks too.
3. **The fix must be a hard cutoff, not a summariser.** Routing the corpus
   through ``filter_fragments_by_tier`` would replace a personal body with
   ``"[Personal-tier summary: <title>]"`` and then write *that* into
   ``## Exemplar Passages`` as a voice exemplar — leaking the title, in bold,
   beside the fragment id, and poisoning the voice corpus with a synthetic
   sentence in nobody's voice. That is the same call #968 made for
   ``creek.report``, and
   :func:`test_a_long_titled_personal_fragment_leaks_no_title_at_the_open_ceiling`
   is the test that makes the difference between the two fixes visible.

Both production surfaces are covered. A fix threaded only through MCP would
leave ``creek skills generate --include-tier`` as a flag that is parsed,
audited, and then ignored — which is precisely the state this module was
written against.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.cli import app
from creek.generate.skills import SkillTreeGenerator
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.skills import skills_refresh_tool

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Canaries
#
# Plain sentinels rather than realistic prose: a leak cannot then be excused as
# "that phrase could have come from anywhere".
# ---------------------------------------------------------------------------

_OPEN_CANARY = "CANARY-OPEN-971"
"""Below every ceiling under test — the mandatory positive control."""

_PERSONAL_CANARY = "CANARY-PERSONAL-971"
"""Above ``ceiling=open``. The body #971 reproduced in a generated SKILL file."""

_UNCLASSIFIED_CANARY = "CANARY-UNCLASSIFIED-971"
"""Carried by a fragment with no ``privacy_tier`` key at all.

It ranks with ``personal`` (#876) rather than with ``open``, so it is above
``ceiling=open`` too — a second, independent route to the same leak, and the
one an operator who has never run ``creek classify`` would actually hit.
"""

_INTIMATE_CANARY = "CANARY-INTIMATE-971"
"""Already excluded by the ``allow_intimate=False`` hardcode, and asserted anyway.

Its absence is not evidence that the ceiling is enforced — it was absent for
the whole life of the gap. It is here so a fix that *replaced* the consent gate
with a ceiling gate, rather than ANDing the two, is caught.
"""

_OPEN_TITLE = "Open title"
_PERSONAL_TITLE = "Personal title"
_UNCLASSIFIED_TITLE = "Unclassified title"
_INTIMATE_TITLE = "Intimate title"

_PERSONAL_SUMMARY_PREFIX = "[Personal-tier summary:"
"""The stub :func:`creek.classify.privacy_filter._summarize_personal` produces.

It must never appear anywhere under ``creek-skills/``. A skill file is a voice
exemplar corpus; a synthetic sentence in nobody's voice is worse there than an
omission, and the stub carries the title it was built from.
"""

_LONG_PERSONAL_TITLE = (
    "Why I finally told my therapist about the thing that happened in the "
    "summer of nineteen ninety eight and what it cost me to say it out loud"
)
"""A 28-word title, chosen so the summariser stub clears the exemplar floor.

See :func:`test_a_long_titled_personal_fragment_leaks_no_title_at_the_open_ceiling`
for why the exact length is load-bearing.
"""

_SKILLS_RELDIR = "creek-skills"

_EXEMPLAR_HEADING = "## Exemplar Passages"

_EXEMPLAR_PLACEHOLDER = "_No qualifying exemplars"

_EXEMPLAR_CATEGORIES = ("frequencies", "phases", "modes", "registers")
"""The skill categories whose renderers emit an exemplar section.

``threads``, ``eddies`` and ``meta`` render no exemplars at all — see
``_render_thread_body`` / ``_render_eddy_body`` / ``_render_voice_core_body``,
none of which call ``_maybe_render_exemplar_section``. Listing the four that do
keeps :func:`test_a_personal_only_vault_still_writes_well_formed_files` from
asserting a heading that never existed.
"""

_SKILLS_LOGGER_NAME = "creek.generate.skills"

_WITHHELD_HINT = "--include-tier personal"
"""The remedy the operator-feedback log line and the CLI hint must both name.

An operator whose whole vault is ``unclassified`` gets an exemplar-free skill
tree at the default ceiling. Silence there is indistinguishable from an empty
vault, so the count and the remedy are stated out loud in both surfaces.
"""

_HINT_NO_EXEMPLARS = "contribute no exemplars"
"""The other half of the CLI hint's required wording.

Pinned as a phrase rather than as "the word exemplar appears" because the
success line already reads ``(34 exemplar-bearing files)`` — a laxer assertion
would pass without any hint being printed at all.
"""


# ---------------------------------------------------------------------------
# Vault-building helpers
#
# Kept local rather than in conftest.py: every choice below is shaped around
# one specific admission condition of the skill generator, and moving them to
# shared scope would invite a later edit to simplify one of those conditions
# away.
# ---------------------------------------------------------------------------


def _exemplar_body(sentinel: str) -> str:
    """Return a body that really survives ``_extract_passage``.

    ``_EXEMPLAR_WORDS_MIN`` is 30 and ``_extract_passage`` accumulates *whole
    sentences* until it clears that floor, so a body has to be both long enough
    and shaped as a run of short sentences. A single 34-word sentence would
    work too, but a run is what a real journal entry looks like and it exercises
    the accumulation loop rather than its first iteration.

    The sentinel opens the first sentence so it is inside the extracted passage
    rather than beyond whatever boundary the extractor happens to stop at — a
    canary that lands after the cut is a canary that can never fail.

    Args:
        sentinel: The canary string this body carries.

    Returns:
        A five-sentence body whose first four sentences total 34 words.
    """
    return (
        f"{sentinel} opens this note and names it. "
        "I sat down at the table and wrote until the light changed. "
        "The house was quiet then. "
        "Nobody asked me what I meant by any of it. "
        "I kept the page and read it back the next morning."
    )


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str,
    body: str,
    privacy_tier: str | None,
) -> Path:
    """Write one classified fragment under ``01-Fragments/Notes``.

    Every field the skill generator groups on is populated, so the fragment
    reaches all four exemplar-bearing categories at once: ``frequency.primary``
    for ``frequencies/F3``, ``wavelength.phase`` for ``phases/rising``,
    ``wavelength.mode`` + ``orientation`` for ``modes/express-do``, and
    ``voice.voice_register`` for ``registers/confessional``. ``source.author``
    is ``self`` because ``_is_snapshot_fragment`` refuses anything else outright
    — a non-self fragment would be dropped before the ceiling gate ever ran, and
    every exclusion assertion here would hold for the wrong reason.

    Args:
        vault: Vault root.
        frag_id: Fragment id, and the file stem.
        title: Fragment title — what a summariser-based fix would leak.
        body: Markdown body, carrying this fragment's canary.
        privacy_tier: The declared tier, or ``None`` to omit the key entirely.
            ``None`` is not the same as ``"unclassified"``: the model defaults a
            missing key to ``unclassified``, and only the raw front matter can
            still tell the two apart.

    Returns:
        The path the fragment was written to.
    """
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": "2026-05-01T00:00:00+00:00",
        "ingested": "2026-05-01T00:00:00+00:00",
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F3", "secondary": []},
        "wavelength": {"phase": "rising", "mode": "express", "orientation": "do"},
        "voice": {"voice_register": "confessional", "confidence": "settled"},
        "eddies": [],
    }
    if privacy_tier is not None:
        metadata["privacy_tier"] = privacy_tier
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _seed_four_tiers(vault: Path) -> None:
    """Seed one fragment per tier position, each carrying its own canary.

    Four rows rather than two because the tiers fail for three different
    reasons: ``open`` must always be admitted, ``personal`` and *untiered* are
    the two above-ceiling routes #971 reproduced, and ``intimate`` is the one
    that was already excluded by an unrelated hardcode.

    Args:
        vault: Vault root, mutated in place.
    """
    _write_fragment(
        vault,
        frag_id="frag-971-open",
        title=_OPEN_TITLE,
        body=_exemplar_body(_OPEN_CANARY),
        privacy_tier="open",
    )
    _write_fragment(
        vault,
        frag_id="frag-971-personal",
        title=_PERSONAL_TITLE,
        body=_exemplar_body(_PERSONAL_CANARY),
        privacy_tier="personal",
    )
    _write_fragment(
        vault,
        frag_id="frag-971-unclassified",
        title=_UNCLASSIFIED_TITLE,
        body=_exemplar_body(_UNCLASSIFIED_CANARY),
        privacy_tier=None,
    )
    _write_fragment(
        vault,
        frag_id="frag-971-intimate",
        title=_INTIMATE_TITLE,
        body=_exemplar_body(_INTIMATE_CANARY),
        privacy_tier="intimate",
    )


def _skills_blob(vault: Path) -> str:
    """Return every generated skill file's text, concatenated.

    A whole-tree sweep rather than a named file: the same fragment reaches four
    different category files, and a leak into ``registers/confessional.SKILL.md``
    is exactly as real as one into ``frequencies/F3.SKILL.md``. Asserting on one
    named file would cover the shape of the tree that exists today.

    Args:
        vault: Vault root; the tree is read from ``<vault>/creek-skills``.

    Returns:
        One string covering every ``*.md`` under the skill tree, or the empty
        string when nothing was written — which the anti-vacuity assertions in
        each test are what catch.
    """
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((vault / _SKILLS_RELDIR).rglob("*.md"))
    )


def _squash(text: str) -> str:
    """Return *text* with ANSI escapes removed and whitespace collapsed.

    Rich re-wraps console output at the terminal width, so a phrase asserted
    verbatim can be split across a newline. Collapsing runs of whitespace to a
    single space makes a multi-word assertion independent of where the wrap
    landed.

    Args:
        text: Captured CLI output.

    Returns:
        The single-spaced, escape-free text.
    """
    stripped = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return " ".join(stripped.split())


def _skills_log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the messages logged by the skill generator itself.

    Filtered by logger name so an unrelated INFO record from the vault reader
    or the audit log cannot satisfy — or defeat — an assertion about the
    generator's operator feedback.

    Args:
        caplog: pytest's log-capture fixture.

    Returns:
        The rendered messages, in emission order.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _SKILLS_LOGGER_NAME
    ]


# ---------------------------------------------------------------------------
# T1 — the leak itself, through the MCP surface
# ---------------------------------------------------------------------------


def test_refresh_leaks_no_personal_or_unclassified_body_at_the_open_ceiling(
    tmp_path: Path,
) -> None:
    """``creek.skills.refresh`` at ``ceiling=open`` writes only ``open`` prose.

    The reproduction for #971, asserted where the leak actually lives. The
    response carries ``skill_paths`` and a count — never content — so a
    response-level assertion is satisfied by the unfixed code and proves
    nothing. Every canary assertion below is against the bytes of the tree the
    call wrote.

    The ``open`` canary is asserted *present* first, and that is not decoration:
    without it, a generator that filtered everything out — or crashed into
    writing empty exemplar sections — would satisfy the three exclusion
    assertions perfectly while being an outage rather than a gate.

    Titles are swept as well as bodies because a skill file renders
    ``> **<title>** (`<id>`)`` above every quoted passage, so an exemplar
    admitted at all leaks three things, not one.

    ``_PERSONAL_SUMMARY_PREFIX`` is the fourth exclusion and it points at the
    *other* fix: routing this corpus through ``filter_fragments_by_tier`` would
    drop the body and keep the title, in bold, inside a synthetic exemplar. A
    skill tree must omit.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    response = skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="probe",
    )
    blob = _skills_blob(tmp_path)

    assert response["skill_count"] > 0, (
        "creek.skills.refresh wrote no skill files at all, so every assertion "
        f"below is vacuous: {response}"
    )
    assert blob, (
        "the skill tree under <vault>/creek-skills is empty, so the exclusion "
        "assertions below hold over nothing."
    )
    assert _OPEN_CANARY in blob, (
        f"the admitted open-tier fragment's body {_OPEN_CANARY!r} never "
        "reached the skill tree at ceiling=open. A gate that drops everything "
        "is an outage, not a fix, and it would make every exclusion below "
        "meaningless."
    )
    assert _PERSONAL_CANARY not in blob, (
        "creek.skills.refresh copied a personal fragment's full body into the "
        f"skill tree at privacy_tier_ceiling=open: {_PERSONAL_CANARY!r}. The "
        "response envelope was clean — it always is — so the only place this "
        f"shows up is the file.\n\n{blob}"
    )
    assert _UNCLASSIFIED_CANARY not in blob, (
        "a fragment with no privacy_tier key contributed its full body at "
        f"privacy_tier_ceiling=open: {_UNCLASSIFIED_CANARY!r}. Untiered content "
        "ranks with personal (#876), and before `creek classify` runs it is the "
        f"whole vault.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob, (
        f"an intimate fragment's body reached the skill tree: "
        f"{_INTIMATE_CANARY!r}. Note this was already excluded by "
        "_is_snapshot_fragment's allow_intimate=False before #971, so seeing it "
        "here means the ceiling gate REPLACED the consent gate instead of "
        f"being ANDed with it.\n\n{blob}"
    )
    assert _PERSONAL_TITLE not in blob, (
        "a personal fragment's title reached the skill tree at ceiling=open. "
        "Every exemplar renders `> **<title>** (`<id>`)`, so an admitted "
        f"fragment leaks its title and its id as well as its prose.\n\n{blob}"
    )
    assert _UNCLASSIFIED_TITLE not in blob, (
        "an untiered fragment's title reached the skill tree at ceiling=open."
        f"\n\n{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob, (
        "the skill tree carries a filter_fragments_by_tier summary stub. That "
        "is the summarising filter, not the hard cutoff: it writes "
        "'[Personal-tier summary: <title>]' into ## Exemplar Passages as a "
        "voice exemplar, leaking the title it was built from and teaching the "
        f"model a sentence nobody ever wrote.\n\n{blob}"
    )


# ---------------------------------------------------------------------------
# T2 — recoverability: the ceiling must widen as well as narrow
# ---------------------------------------------------------------------------


def test_personal_bodies_return_in_full_at_the_personal_ceiling(
    tmp_path: Path,
) -> None:
    """``ceiling=personal`` restores the personal body, unsummarised.

    The permissive direction has to be pinned as hard as the restrictive one.
    Every assertion in the test above is satisfied by a generator that writes an
    exemplar-free tree at every ceiling, so without this test "the ceiling is
    enforced" and "the skill tree is broken" are indistinguishable.

    ``in full`` is the operative phrase and it is why the body canary is what is
    asserted rather than the title: a summariser-based fix would put the title
    back at this ceiling too, and only the prose tells the two apart.

    ``intimate`` is asserted *absent* in the same call. The two gates are
    independent and ANDed — ``ceiling=personal`` does not admit intimate under
    either of them — so this row also pins that widening the ceiling one step
    does not quietly widen it two.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="probe",
    )
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob
    assert _PERSONAL_CANARY in blob, (
        "creek.skills.refresh dropped a personal fragment at "
        "privacy_tier_ceiling=personal. The ceiling is the caller's declared "
        "reach; filtering below it is a regression, not caution.\n\n"
        f"{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob, (
        "the personal fragment came back as a summary stub rather than as its "
        "own prose. At ceiling=personal there is nothing left to summarise "
        f"for.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob, (
        "an intimate fragment reached the skill tree at "
        f"privacy_tier_ceiling=personal.\n\n{blob}"
    )


def test_personal_bodies_return_in_full_at_the_all_ceiling(tmp_path: Path) -> None:
    """``ceiling=all`` is the broadest ceiling and still admits nothing intimate.

    Two claims in one call, and the second is the interesting one.
    ``to_privacy_override(ALL)`` maps to ``PrivacyTierOverride.ALL``, which the
    rank cutoff admits *every* tier under — so the only thing still keeping
    intimate exemplars out of an MCP-generated skill tree is the ``allow_intimate``
    consent gate, which this tool must never pass. Pinning the absence here is
    what makes "the MCP surface exposes no route to intimate exemplars"
    checkable rather than a property of the code nobody wrote down.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.ALL,
        consumer="probe",
    )
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob
    assert _PERSONAL_CANARY in blob, (
        "creek.skills.refresh dropped a personal fragment at "
        f"privacy_tier_ceiling=all, the caller's explicit override.\n\n{blob}"
    )
    assert _UNCLASSIFIED_CANARY in blob, (
        "creek.skills.refresh dropped an untiered fragment at "
        f"privacy_tier_ceiling=all.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob, (
        "an intimate fragment reached the skill tree at "
        "privacy_tier_ceiling=all. The rank cutoff admits every tier under "
        "ALL, so the consent gate is the only thing standing here — and "
        "skills_refresh_tool must never pass allow_intimate=True.\n\n"
        f"{blob}"
    )


# ---------------------------------------------------------------------------
# T3 — the two gates are independent, and ANDed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allow_intimate", "override", "admitted"),
    [
        pytest.param(False, PrivacyTierOverride.OPEN, False, id="neither"),
        pytest.param(True, PrivacyTierOverride.OPEN, False, id="consent-only"),
        pytest.param(False, PrivacyTierOverride.INTIMATE, False, id="ceiling-only"),
        pytest.param(True, PrivacyTierOverride.INTIMATE, True, id="both"),
    ],
)
def test_intimate_needs_both_consent_and_ceiling(
    tmp_path: Path,
    allow_intimate: bool,
    override: PrivacyTierOverride,
    admitted: bool,
) -> None:
    """Intimate content appears iff consent **and** ceiling both allow it.

    The full truth table, because three of its four rows are green under three
    different broken implementations. A fix that *replaced* the
    ``allow_intimate`` consent gate with the new ceiling gate passes
    ``neither`` and ``both`` and fails ``ceiling-only``. A fix that kept the
    consent gate and ignored the ceiling passes ``neither`` and ``ceiling-only``
    and fails ``consent-only``. Only asserting all four says the two gates are
    ANDed rather than merged.

    Driven through the generator directly rather than through a tool wrapper:
    neither production surface exposes both switches at once — the MCP tool
    never passes ``allow_intimate`` and the CLI must never derive it from
    ``--include-tier`` — so this is the only place the combination is reachable.

    The ``open`` canary is asserted present on every row, so no row can pass by
    the generator writing nothing.

    Args:
        tmp_path: pytest's per-test temporary directory.
        allow_intimate: The consent opt-in passed to the generator.
        override: The admission ceiling passed to the generator.
        admitted: Whether the intimate canary must appear.
    """
    _seed_four_tiers(tmp_path)
    SkillTreeGenerator(
        allow_intimate=allow_intimate,
        override=override,
    ).generate_all_skills(tmp_path, tmp_path / _SKILLS_RELDIR)
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob, (
        "the open-tier fragment did not reach the skill tree with "
        f"allow_intimate={allow_intimate} / override={override.value!r}, so "
        "the assertion below is vacuous."
    )
    assert (_INTIMATE_CANARY in blob) is admitted, (
        f"with allow_intimate={allow_intimate} and override={override.value!r} "
        f"the intimate canary should be {'present' if admitted else 'absent'} "
        "and is not. Admission is `_is_snapshot_fragment(...) AND "
        "tier_within_override(...)`: the consent opt-in and the ceiling are two "
        f"independent questions and both must say yes.\n\n{blob}"
    )


# ---------------------------------------------------------------------------
# T4 — why the fix is a hard cutoff and not a summariser
# ---------------------------------------------------------------------------


def test_a_long_titled_personal_fragment_leaks_no_title_at_the_open_ceiling(
    tmp_path: Path,
) -> None:
    """A summariser-based fix fails this test; the hard cutoff passes it.

    This is the test that distinguishes the two candidate fixes, and the title
    length is doing the work.

    Route the corpus through ``filter_fragments_by_tier`` and a personal
    fragment is not dropped — its body is replaced by
    ``_summarize_personal``'s ``"[Personal-tier summary: <title>]"``. Split on
    whitespace that stub is 2 tokens plus the title's 28, i.e. **exactly 30** —
    and ``_extract_passage`` accepts at ``word_count >= _EXEMPLAR_WORDS_MIN``,
    where ``_EXEMPLAR_WORDS_MIN`` is 30. The stub contains no ``.``/``!``/``?``,
    so the sentence splitter yields it whole, it clears the floor on the first
    iteration, and ``_build_exemplar`` returns it. ``_render_exemplar_section``
    then writes the stub as a quoted passage *and* renders
    ``> **<title>** (`<id>`)`` above it in bold.

    The result is the title leaked twice, the id leaked once, and the voice
    corpus taught a sentence nobody wrote — from a "fix". A shorter title would
    have fallen under the floor and hidden the whole failure mode behind an
    accident of word count, which is why this fragment's title is 28 words and
    not five.

    The body is a real 34-word passage, so under the *current* unfixed code the
    title is written for the ordinary reason too: the test is red today because
    of the gap, and would stay red under the wrong fix.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _write_fragment(
        tmp_path,
        frag_id="frag-971-long-title",
        title=_LONG_PERSONAL_TITLE,
        body=_exemplar_body(_PERSONAL_CANARY),
        privacy_tier="personal",
    )
    response = skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="probe",
    )
    blob = _skills_blob(tmp_path)

    assert response["skill_count"] > 0
    assert blob, "no skill tree was written, so this sweep has nothing to sweep."
    assert _LONG_PERSONAL_TITLE not in blob, (
        "a personal fragment's title reached the skill tree at ceiling=open. "
        "If the body canary is absent but this title is present, the fix is "
        "filter_fragments_by_tier: the summary stub is 30 words, exactly "
        "_EXEMPLAR_WORDS_MIN, so it survives _extract_passage and is rendered "
        f"as an exemplar with the title in bold above it.\n\n{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob, (
        "the summariser's stub was written into the skill tree. A skill file is "
        "a voice-exemplar corpus; a synthetic title-only sentence there is "
        f"worse than an omission.\n\n{blob}"
    )
    assert _PERSONAL_CANARY not in blob, (
        f"the personal body reached the skill tree at ceiling=open.\n\n{blob}"
    )


# ---------------------------------------------------------------------------
# T5 — excluding everything must still leave a usable tree
# ---------------------------------------------------------------------------


def test_a_personal_only_vault_still_writes_well_formed_files(
    tmp_path: Path,
) -> None:
    """A vault the ceiling empties still produces the full, parseable tree.

    The failure mode this rules out is not a leak — it is the fix overshooting
    into an outage. An operator whose entire vault is ``personal`` (or, far more
    commonly, untiered) gets *no* exemplars at the default ceiling, and the
    correct behaviour is the existing "no qualifying exemplars" placeholder path
    that an empty vault already takes: 34 well-formed skill files, each with
    frontmatter a downstream loader can read.

    ``exactly one`` exemplar heading is asserted rather than "at least one",
    because the plausible bad fix here is a renderer that emits the placeholder
    section *in addition to* a filtered one. Only the four exemplar-bearing
    categories are checked: ``threads``, ``eddies`` and ``meta`` render no
    exemplar section at all, so requiring a heading there would be asserting
    against a shape the generator has never had.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _write_fragment(
        tmp_path,
        frag_id="frag-971-personal-a",
        title=_PERSONAL_TITLE,
        body=_exemplar_body(_PERSONAL_CANARY),
        privacy_tier="personal",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-971-personal-b",
        title="Second personal title",
        body=_exemplar_body(_PERSONAL_CANARY),
        privacy_tier="personal",
    )
    response = skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="probe",
    )

    files = sorted((tmp_path / _SKILLS_RELDIR).rglob("*.md"))
    assert response["skill_count"] == len(files)
    assert len(files) == 34, (
        "a vault whose fragments are all above the ceiling should still yield "
        "the full 10 + 6 + 9 + 7 + 2 tree; the ceiling governs exemplars, not "
        f"whether the tree exists: {[p.name for p in files]}"
    )
    for path in files:
        post = frontmatter.load(str(path))
        assert post.metadata["type"] == "skill", (
            f"{path.name} did not parse as a skill file: {post.metadata}"
        )
        assert _PERSONAL_CANARY not in post.content, (
            f"{path.name} carries the personal canary at ceiling=open."
        )
        heading_count = post.content.count(_EXEMPLAR_HEADING)
        if path.parent.name in _EXEMPLAR_CATEGORIES:
            assert heading_count == 1, (
                f"{path.name} carries {heading_count} '{_EXEMPLAR_HEADING}' "
                "headings; an exemplar-bearing skill file has exactly one."
            )
            assert _EXEMPLAR_PLACEHOLDER in post.content, (
                f"{path.name} has an exemplar section with no placeholder in "
                "it. When the ceiling admits nothing, the existing "
                "'no qualifying exemplars' text is what tells the operator the "
                f"section is empty on purpose.\n\n{post.content}"
            )
        else:
            assert heading_count == 0, (
                f"{path.name} is a {path.parent.name} skill and grew an "
                "exemplar section it never had."
            )


# ---------------------------------------------------------------------------
# T6 — which tier reader the gate uses is a deliberate choice
# ---------------------------------------------------------------------------


def test_a_keyless_fragment_is_admitted_at_the_personal_ceiling(
    tmp_path: Path,
) -> None:
    """A fragment with no ``privacy_tier`` key ranks with ``personal``, not intimate.

    This documents a deliberate divergence from ``creek.report``'s gate, and it
    is the assertion that tells the two implementations apart.
    ``creek.report`` reads raw frontmatter through ``raw_privacy_tier``, where a
    *missing* key fails all the way closed to ``INTIMATE`` — because that
    generator walks note types (eddies, threads, praxis) that have no
    ``privacy_tier`` field in their models at all and cannot be asked for one.

    The skill generator has no such problem: it validates every file into a
    :class:`~creek.models.Fragment` before doing anything with it, so it reads
    the tier off the model with ``tier_of``, a missing key becomes the model's
    ``UNCLASSIFIED`` default, and ``UNCLASSIFIED`` ranks with ``PERSONAL``
    (#876). An untiered fragment is therefore refused at ``open`` and admitted
    at ``personal``.

    An implementation that reached for ``raw_privacy_tier`` here — the "obvious"
    consistency move — would refuse this fragment at ``personal`` and only admit
    it at ``intimate``, which would quietly make ``--include-tier personal``
    useless for the one corpus that needs it most: a freshly ingested vault
    where ``creek classify`` has not yet run and *every* fragment is untiered.
    Both implementations satisfy the ``open``-ceiling assertions in T1. Only
    this row tells them apart.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    skills_refresh_tool(
        vault_path=tmp_path,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="probe",
    )
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob
    assert _UNCLASSIFIED_CANARY in blob, (
        "a fragment with no privacy_tier key was refused at "
        "privacy_tier_ceiling=personal. It validates to UNCLASSIFIED, which "
        "ranks with PERSONAL (#876), so `personal` is the ceiling that admits "
        "it. Refusing it here means the gate reads raw frontmatter rather than "
        "the validated model — which leaves --include-tier personal useless on "
        f"an unclassified vault.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob


# ---------------------------------------------------------------------------
# T7 — the operator has to be told what was withheld
# ---------------------------------------------------------------------------


def test_the_ceiling_gate_tells_the_operator_what_it_withheld(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence and an empty vault must not look the same to the operator.

    With the ceiling enforced, the common case for a real vault is a skill tree
    full of "no qualifying exemplars" placeholders — because a not-yet-classified
    corpus is entirely ``unclassified`` and ``unclassified`` ranks with
    ``personal``. Without a word from the generator, that is indistinguishable
    from a vault with no fragments in it, and the operator's next move is to
    file a bug rather than to pass ``--include-tier personal``.

    So the count, the ceiling and the remedy are all pinned. The count is
    matched against the integers in the message rather than as a bare substring:
    an assertion that ``"4" in message`` would be satisfied by the ``4`` inside
    some unrelated number, and requiring the exact token rules that out while
    staying agnostic about the sentence it sits in.

    The negative half is what stops the line becoming noise: a vault where the
    ceiling withheld nothing must produce no such message at all. Only
    ``personal`` and untiered fragments are seeded, never ``intimate``, so
    "rejected by the ceiling gate" is unambiguous — an intimate fragment is
    refused by the consent gate first and would make the expected count depend
    on which gate the implementation happens to evaluate first.

    Args:
        tmp_path: pytest's per-test temporary directory.
        caplog: pytest's log-capture fixture.
    """
    withheld_vault = tmp_path / "withheld"
    for index in range(3):
        _write_fragment(
            withheld_vault,
            frag_id=f"frag-971-log-personal-{index}",
            title=f"Withheld personal {index}",
            body=_exemplar_body(_PERSONAL_CANARY),
            privacy_tier="personal",
        )
    _write_fragment(
        withheld_vault,
        frag_id="frag-971-log-unclassified",
        title=_UNCLASSIFIED_TITLE,
        body=_exemplar_body(_UNCLASSIFIED_CANARY),
        privacy_tier=None,
    )
    _write_fragment(
        withheld_vault,
        frag_id="frag-971-log-open",
        title=_OPEN_TITLE,
        body=_exemplar_body(_OPEN_CANARY),
        privacy_tier="open",
    )

    with caplog.at_level(logging.INFO, logger=_SKILLS_LOGGER_NAME):
        skills_refresh_tool(
            vault_path=withheld_vault,
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer="probe",
        )
        reported = [m for m in _skills_log_messages(caplog) if _WITHHELD_HINT in m]

    assert len(reported) == 1, (
        "the ceiling gate withheld four fragments and said so "
        f"{len(reported)} times. Exactly one summary per generation run: zero "
        "leaves the operator staring at an empty skill tree with no idea why, "
        "and one per fragment turns a 35k-fragment vault's log into noise."
        f"\n\n{_skills_log_messages(caplog)}"
    )
    message = reported[0]
    assert "4" in re.findall(r"\d+", message), (
        "the withheld-count message does not name the number of fragments the "
        f"ceiling dropped (four): {message!r}"
    )
    assert TierCeiling.OPEN.value in message, (
        "the withheld-count message does not name the ceiling it was applied "
        f"under, so a reader cannot tell what to widen: {message!r}"
    )

    caplog.clear()
    clean_vault = tmp_path / "clean"
    _write_fragment(
        clean_vault,
        frag_id="frag-971-clean-open",
        title=_OPEN_TITLE,
        body=_exemplar_body(_OPEN_CANARY),
        privacy_tier="open",
    )
    with caplog.at_level(logging.INFO, logger=_SKILLS_LOGGER_NAME):
        skills_refresh_tool(
            vault_path=clean_vault,
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer="probe",
        )
        quiet = [m for m in _skills_log_messages(caplog) if _WITHHELD_HINT in m]

    assert quiet == [], (
        "the generator reported withheld fragments for a vault where the "
        "ceiling withheld none. A warning that fires unconditionally is a "
        f"warning nobody reads: {quiet}"
    )
    assert _OPEN_CANARY in _skills_blob(clean_vault), (
        "the open-only vault produced no exemplar, so the silence asserted "
        "above could simply mean the corpus walk never ran."
    )


# ---------------------------------------------------------------------------
# T8 — the CLI is the second production surface
# ---------------------------------------------------------------------------


def test_cli_leaks_no_personal_body_at_the_default_ceiling(tmp_path: Path) -> None:
    """``creek skills generate`` honours the default ceiling too.

    ``skills_refresh_tool`` is not the only production caller. The CLI reaches
    the same :class:`~creek.generate.skills.SkillTreeGenerator`, already parses
    ``--include-tier`` into a ``PrivacyTierOverride``, and — at the time this
    was written — used that value for exactly one thing: writing an audit entry.
    A fix threaded only through MCP would leave a flag that is parsed, audited,
    and then ignored, on the surface the vault's own operator actually uses.

    The default (no flag at all) is what is tested, because that is what an
    operator gets by accident. ``_parse_include_tier`` returns ``None`` for an
    absent flag and ``None`` must normalise to ``OPEN``, not to "unfiltered".

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    result = runner.invoke(
        app,
        ["skills", "generate", "--generate", "--vault", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob, (
        "creek skills generate wrote no admitted exemplar, so the exclusions "
        f"below are vacuous.\n\n{result.output}"
    )
    assert _PERSONAL_CANARY not in blob, (
        "creek skills generate copied a personal fragment's full body into "
        "<vault>/creek-skills with no --include-tier flag. An absent flag "
        f"parses to None, and None must mean the open ceiling.\n\n{blob}"
    )
    assert _UNCLASSIFIED_CANARY not in blob, (
        "creek skills generate copied an untiered fragment's full body into "
        f"<vault>/creek-skills at the default ceiling.\n\n{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob


def test_cli_include_tier_personal_recovers_the_personal_body(
    tmp_path: Path,
) -> None:
    """``--include-tier personal`` is the documented way back to that content.

    The companion to the test above, and the reason the default can be strict
    at all: the operator who wants personal exemplars in their voice model has
    a one-flag path to them, and the flag is already audited to
    ``00-Creek-Meta/audit/privacy.jsonl``. Without this test, "the CLI enforces
    the ceiling" and "the CLI can no longer see personal fragments" are the same
    result.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    result = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--vault",
            str(tmp_path),
            "--include-tier",
            "personal",
        ],
    )
    assert result.exit_code == 0, result.output
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob
    assert _PERSONAL_CANARY in blob, (
        "creek skills generate --include-tier personal did not restore the "
        "personal fragment's body. The flag is the operator's explicit "
        f"decision and it is recorded in the privacy audit log.\n\n{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob, (
        "the personal fragment came back summarised rather than whole at "
        f"--include-tier personal.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob


def test_cli_include_tier_intimate_still_emits_no_intimate_exemplars(
    tmp_path: Path,
) -> None:
    """``--include-tier intimate`` must NOT switch on intimate exemplars.

    This is the loosening that must not happen, and it would be an easy one to
    make by accident: the flag says ``intimate``, the generator has an
    ``allow_intimate`` switch, and wiring one to the other looks like finishing
    the job.

    It is not. ``creek/templates/skills/privacy-tier.SKILL.md`` rule 1 is a
    shipped promise that intimate content never enters the voice-skill tree, and
    ``skills_refresh_tool``'s own docstring records the exclusion as a
    deliberate hardcode. Threading the ceiling into the *rank cutoff* narrows
    what the tree may contain; deriving ``allow_intimate`` from the same flag
    would widen it, in the same change, past a boundary nobody asked to move.

    The personal canary is asserted present as the positive control:
    ``--include-tier intimate`` really does widen the rank cutoff (intimate
    outranks personal), so a bare "the intimate canary is absent" could
    otherwise pass on a run where the flag did nothing at all.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    result = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--vault",
            str(tmp_path),
            "--include-tier",
            "intimate",
        ],
    )
    assert result.exit_code == 0, result.output
    blob = _skills_blob(tmp_path)

    assert _OPEN_CANARY in blob
    assert _PERSONAL_CANARY in blob, (
        "--include-tier intimate did not even widen the ceiling to personal, "
        f"so the intimate assertion below proves nothing.\n\n{blob}"
    )
    assert _INTIMATE_CANARY not in blob, (
        "creek skills generate --include-tier intimate emitted an intimate "
        "exemplar. The flag governs the rank cutoff, never the allow_intimate "
        "consent gate: privacy-tier.SKILL.md rule 1 promises intimate content "
        f"never enters the voice-skill tree.\n\n{blob}"
    )
    assert _INTIMATE_TITLE not in blob, (
        "an intimate fragment's title reached the skill tree at "
        f"--include-tier intimate.\n\n{blob}"
    )


def test_cli_hints_at_the_default_ceiling(tmp_path: Path) -> None:
    """The default ceiling explains itself once, and only when it applies.

    The CLI half of the operator-feedback argument in
    :func:`test_the_ceiling_gate_tells_the_operator_what_it_withheld`. An
    operator who runs ``creek skills generate`` on an unclassified vault gets 34
    files of placeholders and, without a hint, no way to tell a working command
    from a broken one.

    The required wording is pinned as two phrases rather than one so the
    assertion cannot be satisfied by accident: the success line already reads
    ``(34 exemplar-bearing files)``, so "the word exemplar appears" is free. The
    hint must read along the lines of::

        Personal and unclassified fragments contribute no exemplars at the
        default ceiling; pass --include-tier personal to include them.

    It is static — printed whenever the ceiling is the default, not only when
    something was withheld — because the alternative is a per-run vault survey
    the CLI does not otherwise need, and because "you would have been told" is
    only reassuring if the message is unconditional.

    The negative half is asserted at ``--include-tier personal``, where the hint
    would be actively wrong: that operator has already done the thing it asks
    for.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    default_run = runner.invoke(
        app,
        ["skills", "generate", "--generate", "--vault", str(tmp_path)],
    )
    assert default_run.exit_code == 0, default_run.output
    default_output = _squash(default_run.output)

    assert _WITHHELD_HINT in default_output, (
        "creek skills generate printed no remedy at the default ceiling. The "
        f"operator needs to be told about {_WITHHELD_HINT!r}, because an "
        "exemplar-free tree is otherwise indistinguishable from a broken "
        f"one.\n\n{default_run.output}"
    )
    assert _HINT_NO_EXEMPLARS in default_output, (
        "creek skills generate names the flag but never says what the default "
        f"ceiling does: the hint must contain {_HINT_NO_EXEMPLARS!r}.\n\n"
        f"{default_run.output}"
    )

    widened_run = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--vault",
            str(tmp_path),
            "--include-tier",
            "personal",
        ],
    )
    assert widened_run.exit_code == 0, widened_run.output
    widened_output = _squash(widened_run.output)

    assert _WITHHELD_HINT not in widened_output, (
        "creek skills generate --include-tier personal still advises passing "
        "--include-tier personal. A hint that fires when it no longer applies "
        f"is how operators learn to skim past it.\n\n{widened_run.output}"
    )
    assert _HINT_NO_EXEMPLARS not in widened_output, (
        "creek skills generate --include-tier personal still claims personal "
        f"fragments contribute no exemplars.\n\n{widened_run.output}"
    )


def _generate_signature_only(vault: Path, output: Path, ceiling: str | None) -> str:
    """Run ``skills generate --signature-only`` and return its squashed output.

    Args:
        vault: Vault root to read from.
        output: Destination for the skill tree, so two ceilings can be
            generated side by side and compared.
        ceiling: Value for ``--include-tier``, or ``None`` to leave the flag
            off and take the default ceiling.

    Returns:
        The CLI's console output, ANSI-stripped and whitespace-collapsed.
    """
    argv = [
        "skills",
        "generate",
        "--generate",
        "--signature-only",
        "--vault",
        str(vault),
        "--output",
        str(output),
    ]
    if ceiling is not None:
        argv += ["--include-tier", ceiling]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    return _squash(result.output)


_GENERATED_DATE_RE = re.compile(r"^generated_date:.*$", re.MULTILINE)
"""The one frontmatter field that differs between two runs of the same input.

Dropped before comparing two trees so ``_tree_contents`` measures what the
ceiling did, not how many microseconds apart the two invocations landed.
"""


def _tree_contents(root: Path) -> dict[str, str]:
    """Return every generated file under *root*, keyed by tree-relative path.

    The wall-clock ``generated_date`` stamp is blanked so two trees generated
    from identical inputs compare equal.

    Args:
        root: Root of a generated skill tree.

    Returns:
        Relative path to file text, for every ``.md`` file beneath *root*.
    """
    return {
        str(path.relative_to(root)): _GENERATED_DATE_RE.sub(
            "generated_date: <stamp>",
            path.read_text(encoding="utf-8"),
        )
        for path in sorted(root.rglob("*.md"))
    }


def test_cli_does_not_offer_the_ceiling_remedy_in_signature_only_mode(
    tmp_path: Path,
) -> None:
    """``--signature-only`` must not be told that widening the ceiling helps.

    PR #1286 review. The hint asserted by
    :func:`test_cli_hints_at_the_default_ceiling` fires on ``not
    override_elevates(override)`` alone, so ``creek skills generate
    --signature-only`` at the default ceiling also printed "pass
    --include-tier personal to include them". That remedy does not work in
    that mode: :meth:`SkillTreeGenerator._maybe_pick_exemplars` returns an
    empty list and ``_maybe_render_exemplar_section`` omits the section
    outright whenever ``signature_only`` is set — before either one consults a
    tier. No exemplar appears at *any* ceiling, so the operator who follows the
    advice re-runs the command and sees the identical tree.

    Nothing leaks; the cost is a false remedy, which is the specific thing the
    house rule against unactionable operator messages forbids.

    The fix suppresses the hint rather than qualifying it, and the first
    assertion below is why that is safe: in signature-only mode the ceiling is
    *inert*, changing not one byte of the output tree. There is no true remedy
    to reword the message into, and the confusion the hint exists to prevent —
    an exemplar-free tree reading as a broken command — cannot arise for an
    operator who explicitly asked for zero exemplars and is told so by the
    ``(N signature-only files)`` success line.

    That first assertion is also the tripwire: should the ceiling ever gain a
    real effect on signature-only output, it goes red and says to restore a
    hint here rather than leaving the operator uninformed.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _seed_four_tiers(tmp_path)
    default_dir = tmp_path / "out-default"
    widened_dir = tmp_path / "out-personal"

    default_output = _generate_signature_only(tmp_path, default_dir, None)
    _generate_signature_only(tmp_path, widened_dir, "personal")

    assert _tree_contents(default_dir) == _tree_contents(widened_dir), (
        "--include-tier changed the signature-only tree. The hint is "
        "suppressed in this mode on the premise that the ceiling is inert "
        "here; that premise no longer holds, so restore a hint describing "
        "whatever the ceiling now does."
    )
    assert _WITHHELD_HINT not in default_output, (
        "creek skills generate --signature-only advises passing "
        f"{_WITHHELD_HINT!r}, but signature-only output carries no exemplars "
        "at any ceiling — the operator who follows the advice gets a "
        f"byte-identical tree.\n\n{default_output}"
    )
    assert _HINT_NO_EXEMPLARS not in default_output, (
        "creek skills generate --signature-only blames the default ceiling "
        f"for the absence of exemplars ({_HINT_NO_EXEMPLARS!r}). The mode, "
        f"not the ceiling, is why there are none.\n\n{default_output}"
    )

    exemplar_bearing = runner.invoke(
        app,
        ["skills", "generate", "--generate", "--vault", str(tmp_path)],
    )
    assert exemplar_bearing.exit_code == 0, exemplar_bearing.output
    assert _WITHHELD_HINT in _squash(exemplar_bearing.output), (
        "Suppressing the signature-only hint also silenced the "
        "exemplar-bearing one, where the remedy is true and load-bearing."
    )
