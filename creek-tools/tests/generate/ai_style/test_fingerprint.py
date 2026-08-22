"""Tests for the idiolect / voice-fingerprint profiler (FEAT-040.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.classify.privacy_filter import raw_privacy_tier
from creek.config import AIStyleConfig, VoiceAudienceWeightingConfig
from creek.generate.ai_style.fingerprint import (
    _audience_factor,
    _eligible_texts,
    build_fingerprint,
    extract_user_turns,
    load_fingerprint,
    save_fingerprint,
)
from creek.ingest.base import ParsedFragment
from creek.ingest.claude import ClaudeIngestor
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = AIStyleConfig()


def _write_fragment(
    vault: Path,
    name: str,
    *,
    author: str = "self",
    platform: str = "markdown",
    tier: str | None = "open",
    body: str = "A short honest sentence about the river.",
) -> None:
    """Write a minimal fragment .md with the given source frontmatter.

    Args:
        vault: Vault root; the file lands under ``01-Fragments``.
        name: File name to write.
        author: ``source.author`` value.
        platform: ``source.platform`` value.
        tier: ``privacy_tier`` value, or ``None`` to omit the key entirely —
            the untiered legacy/hand-edited shape #1529 is about. ``None``
            means *absent*, which is not the same file as an explicit
            ``privacy_tier: unclassified``.
        body: Fragment body.
    """
    path = vault / "01-Fragments" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tier_line = "" if tier is None else f"privacy_tier: {tier}\n"
    front = (
        "---\n"
        "source:\n"
        f"  author: {author}\n"
        f"  platform: {platform}\n"
        f"{tier_line}"
        "---\n"
        f"{body}\n"
    )
    path.write_text(front, encoding="utf-8")


_CHATGPT_BODY = (
    "# Conversation (turn 1)\n\n"
    "> **User**: I reckon it was fine and that was that\n"
    ">\n"
    "> **Assistant**: a vibrant tapestry that delve into the intricate "
    "and pivotal underscore of it all\n"
)


class TestExtractUserTurns:
    """The user-turn splitter underpinning the authorship filter."""

    def test_extracts_only_user(self) -> None:
        """Assistant text is dropped; user text is kept."""
        user = extract_user_turns(_CHATGPT_BODY)
        assert user is not None
        assert "I reckon it was fine" in user
        assert "tapestry" not in user
        assert "Assistant" not in user

    def test_no_user_marker_returns_none(self) -> None:
        """A body with no User marker cannot be split → excluded."""
        assert extract_user_turns("# Title\n\njust some prose, no markers") is None


class TestAuthorshipFilter:
    """The decisive correctness property: never fingerprint AI text."""

    def test_assistant_turn_excluded_from_fingerprint(self, tmp_path: Path) -> None:
        """A chatgpt fragment contributes only its user-turn.

        The assistant half is dense with AI vocabulary; the user half has
        none. If the filter works, ``ai_vocab_density`` is 0.0.
        """
        _write_fragment(tmp_path, "c.md", platform="chatgpt", body=_CHATGPT_BODY)
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 1
        assert fp.rate_for("ai_vocab_density") == 0.0

    def test_non_self_author_excluded(self, tmp_path: Path) -> None:
        """AI- and other-authored fragments never enter the fingerprint."""
        _write_fragment(tmp_path, "self.md", author="self")
        _write_fragment(tmp_path, "ai.md", author="ai", body="a vibrant tapestry")
        _write_fragment(tmp_path, "other.md", author="other", body="delve delve")
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 1

    def test_intimate_excluded_by_default(self, tmp_path: Path) -> None:
        """Intimate-tier fragments are excluded unless explicitly included."""
        _write_fragment(tmp_path, "i.md", tier="intimate")
        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        included = build_fingerprint(tmp_path, _CONFIG, include_intimate=True)
        assert included.fragment_count == 1


class TestBuild:
    """Rate computation and corpus handling."""

    def test_rates_match_hand_computed(self, tmp_path: Path) -> None:
        """A known body yields the hand-computed em-dash rate."""
        body = "— — " + " ".join(["word"] * 998)
        _write_fragment(tmp_path, "m.md", body=body)
        fp = build_fingerprint(tmp_path, _CONFIG)
        rate = fp.rate_for("em_dash_density")
        assert rate is not None
        assert abs(rate - 2.0) < 0.05

    def test_empty_vault_yields_empty_fingerprint(self, tmp_path: Path) -> None:
        """No fragments → fragment_count 0, no features."""
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 0
        assert fp.features == {}

    def test_platform_weighting(self, tmp_path: Path) -> None:
        """A higher-weighted platform pulls the mean toward its rate.

        A journal (weight 1.0) full of em-dashes plus a chatgpt user-turn
        (weight 0.5) with none should yield a weighted mean above the plain
        average — i.e. the journal dominates.
        """
        _write_fragment(
            tmp_path,
            "j.md",
            platform="journal",
            body="— — " + " ".join(["w"] * 98),
        )
        _write_fragment(
            tmp_path,
            "c.md",
            platform="chatgpt",
            body="# c\n\n> **User**: no dashes here at all\n",
        )
        fp = build_fingerprint(tmp_path, _CONFIG)
        # journal em-dash rate ≈ 20/1k; chatgpt ≈ 0. Weighted (1.0,0.5):
        # (1.0*20 + 0.5*0)/1.5 ≈ 13.3 > plain mean 10.
        rate = fp.rate_for("em_dash_density")
        assert rate is not None
        assert rate > 11.0


class TestPersistence:
    """Round-trip to the vault JSON artifact."""

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        """A saved fingerprint loads back with identical data."""
        _write_fragment(tmp_path, "m.md", body="— a quick note about the day")
        built = build_fingerprint(tmp_path, _CONFIG)
        path = save_fingerprint(built, tmp_path, _CONFIG)
        assert path.exists()
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == built.fragment_count
        assert loaded.rate_for("em_dash_density") == built.rate_for("em_dash_density")

    def test_load_absent_returns_empty(self, tmp_path: Path) -> None:
        """Loading when no file exists yields an empty fingerprint."""
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == 0
        assert loaded.features == {}

    def test_load_corrupted_json_returns_empty(self, tmp_path: Path) -> None:
        """A corrupted fingerprint file is ignored, not raised on."""
        path = tmp_path / _CONFIG.fingerprint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == 0
        assert loaded.features == {}

    def test_load_version_mismatch_returns_empty(self, tmp_path: Path) -> None:
        """A stale-schema fingerprint is ignored so a rebuild is triggered."""
        import json

        path = tmp_path / _CONFIG.fingerprint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 999, "fragment_count": 3, "features": {}}),
            encoding="utf-8",
        )
        assert load_fingerprint(tmp_path, _CONFIG).fragment_count == 0


def test_build_skips_malformed_frontmatter(tmp_path: Path) -> None:
    """A fragment with broken YAML frontmatter is skipped, not fatal.

    The good fragment declares ``privacy_tier: open``. It carried no tier key
    at all until #1529, which made it eligible only because the admission gate
    failed open on an absent key; the surviving-fragment count is this test's
    positive control, and a control that depends on the very bug under fix
    would turn the test into a tripwire for the wrong property. What is being
    measured here is that broken YAML is skipped rather than fatal, so the
    good file states its tier and the assertion stays about parsing.
    """
    good = tmp_path / "01-Fragments" / "good.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        "---\nsource:\n  author: self\n  platform: markdown\n"
        "privacy_tier: open\n---\nclean note\n",
        encoding="utf-8",
    )
    bad = tmp_path / "01-Fragments" / "bad.md"
    bad.write_text("---\nsource: {unbalanced: [\n---\nbody\n", encoding="utf-8")
    fp = build_fingerprint(tmp_path, _CONFIG)
    assert fp.fragment_count == 1


def test_fingerprint_defensively_copies_features() -> None:
    """Mutating the source dict after construction does not alter the fp."""
    from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint

    src = {"em_dash_density": FeatureStat(rate=1.0, support=3)}
    fp = VoiceFingerprint(features=src, fragment_count=3)
    src["em_dash_density"] = FeatureStat(rate=999.0, support=3)
    assert fp.rate_for("em_dash_density") == 1.0


# ---------------------------------------------------------------------------
# Shape-dispatched conversation splitting (#1426)
#
# ``extract_user_turns`` recognises exactly one body shape: the *merged*
# pre-split rendering, ``> **User**: … / > **Assistant**: …``. Since per-turn
# attribution landed (#1333/#553) a Claude human turn is written as a plain
# blockquote with no ``**User**:`` marker at all, so the splitter returns
# ``None`` and ``_user_text`` excludes it — the operator's own chat prose never
# reaches the fingerprint. The fix dispatches on body *shape*
# (``creek.ingest.turns.split_conversation_body``) instead of on one marker.
#
# The property these tests pin is **platform invariance**: identical prose must
# measure identically whether it arrived as a journal entry, a Claude
# blockquote, or a titled ChatGPT export. Every assertion below is on the
# fingerprint's own corpus/rates, not on the splitter's return value, because
# the corpus is what actually poisons or starves the voice proxy.
# ---------------------------------------------------------------------------

_HUMAN_PROSE = (
    "I keep circling the same knot about how my notes should sit next to "
    "each other, and every time I try to force a folder tree on it the "
    "thing goes stiff and I stop writing in it at all for a week."
)
"""Tell-free operator prose: >20 words, no AI vocabulary, no em-dash.

Over 20 words so ``one_line_fragment_density`` reads 0.0 (the extractor's
``_ONE_LINE_MAX_WORDS`` ceiling is 20), no AI-vocab hit so
``ai_vocab_density`` reads 0.0, and no em-dash so a stray one introduced by
a splitter would show up. It therefore measures the same on ``markdown`` as
it must on ``claude``/``chatgpt`` — which is the whole invariant.
"""

_AI_PROSE = (
    "It is important to note that a vibrant tapestry of intricate systems "
    "underscores the pivotal realm you navigate as you delve into it."
)
"""Assistant-side prose saturated with AI vocabulary.

Any leak of this text into the corpus is visible twice over: as a non-zero
``ai_vocab_density`` and as the literal substring ``"tapestry"``.
"""


def _corpus(vault: Path) -> list[str]:
    """Return the fingerprint's eligible corpus texts for *vault*."""
    texts = _eligible_texts(vault, _CONFIG, include_intimate=False)
    return [text for _weight, text in texts]


class TestSplitTurnCorpus:
    """The conversation corpus, measured through ``build_fingerprint``."""

    @pytest.mark.parametrize(
        ("platform", "body"),
        [
            ("claude", "> " + _HUMAN_PROSE),
            (
                "chatgpt",
                "# Navigating the Intricate Tapestry of Knowledge Systems\n\n> "
                + _HUMAN_PROSE
                + "\n",
            ),
            ("chatgpt", "> " + _HUMAN_PROSE),
        ],
        ids=["claude-split", "chatgpt-split-fresh", "chatgpt-split-migrated"],
    )
    def test_split_human_turn_matches_the_platform_invariant_baseline(
        self,
        tmp_path: Path,
        platform: str,
        body: str,
    ) -> None:
        """The same prose must measure the same whatever body shape wrapped it.

        Platform invariance is the property, not "chat is included": a
        splitter that kept the ``>`` prefixes, or that let the ChatGPT
        ``# Title`` heading through, would still produce ``fragment_count 1``
        while quietly shifting every rate away from the journal baseline. So
        the baseline is *computed here* from the identical prose written as a
        plain ``markdown`` fragment in a second vault root, and the chat
        rendering must land on it.

        Note the heading in the fresh-ChatGPT case is itself AI slop
        ("Navigating the Intricate Tapestry"): the model titled the
        conversation, the operator did not, so admitting the heading would
        add words the operator never wrote *and* three AI-vocab hits.

        Args:
            tmp_path: pytest temporary directory; holds two vault roots.
            platform: The conversation platform under test.
            body: The stored fragment body for that platform.
        """
        baseline_vault = tmp_path / "baseline"
        chat_vault = tmp_path / "chat"
        _write_fragment(
            baseline_vault,
            "b.md",
            platform="markdown",
            body=_HUMAN_PROSE,
        )
        _write_fragment(chat_vault, "c.md", platform=platform, body=body)

        baseline = build_fingerprint(baseline_vault, _CONFIG)
        fp = build_fingerprint(chat_vault, _CONFIG)

        assert baseline.fragment_count == 1
        assert _corpus(baseline_vault) == [_HUMAN_PROSE]
        assert fp.fragment_count == 1
        # Exact string: proves the ``>`` markers and the ``# `` heading are
        # gone and that nothing else was trimmed with them.
        assert _corpus(chat_vault) == [_HUMAN_PROSE]
        assert fp.rate_for("ai_vocab_density") == 0.0
        assert fp.rate_for("one_line_fragment_density") == 0.0
        baseline_rate = baseline.rate_for("sentence_length_mean")
        assert baseline_rate is not None
        assert fp.rate_for("sentence_length_mean") == pytest.approx(baseline_rate)

    def test_merged_claude_body_never_admits_the_ai_prose(self, tmp_path: Path) -> None:
        """A merged Claude body contributes its human half and nothing else.

        This is the case that rules out the issue's own literal remedy —
        "fall back to the whole body when ``source.author`` is ``self`` and no
        ``**Assistant**:`` marker is present". A merged *pre-split* Claude body
        never carried an ``Assistant`` marker: the human turn was blockquoted
        and the model's reply followed as plain prose. So the guard would fire
        on exactly the body it was meant to protect, and this body measures
        **173.9 AI-vocab hits per 1000 words** under it. Shape dispatch reads
        the same body as MERGED and keeps only the blockquoted half.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        body = "> How should I organize knowledge?\n\n" + _AI_PROSE
        _write_fragment(tmp_path, "m.md", platform="claude", body=body)

        fp = build_fingerprint(tmp_path, _CONFIG)

        assert fp.fragment_count == 1
        assert _corpus(tmp_path) == ["How should I organize knowledge?"]
        assert fp.rate_for("ai_vocab_density") == 0.0
        assert all("tapestry" not in text for text in _corpus(tmp_path))

    def test_merged_claude_with_empty_human_turn_is_excluded(
        self, tmp_path: Path
    ) -> None:
        """An assistant-only merged body stays out of the corpus entirely.

        This shape is reachable, not hypothetical: the merged rendering at
        ``git show fc00d49~1:creek-tools/creek/ingest/claude.py`` blockquotes
        whatever ``_extract_text`` (``creek/ingest/claude.py:50-71``) returned
        for the human send, and that is ``""`` for an image-only or tool-only
        send — the parts list holds no ``{"type": "text"}`` entry. The stored
        body is then a bare ``"> "`` line followed by the model's full reply.

        A whole-body fallback admits that entire AI reply at **217.4 hits per
        1000 words**. This is the ``UNRECOVERABLE`` assistant-only case, and
        excluding it is the safety property: when the human half cannot be
        identified, contribute nothing rather than guess.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(
            tmp_path,
            "e.md",
            platform="claude",
            body="> \n\n" + _AI_PROSE,
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        assert _corpus(tmp_path) == []

    def test_chatgpt_body_with_only_an_assistant_marker_is_excluded(
        self, tmp_path: Path
    ) -> None:
        """The ChatGPT sibling of the assistant-only merged body stays out.

        Found in review of PR #1486. A ChatGPT body that kept its
        ``**Assistant**:`` marker but lost its ``**User**:`` one — a truncated
        export, or a hand edit that took the top off the file — has no
        identifiable human half, exactly like the Claude ``"> \\n\\n{AI
        reply}"`` shape above. Dispatching on the *absence* of ``**User**:``
        alone read the whole thing, marker and model reply together, as the
        operator's own words.

        This is a regression the shape table did not model, not a
        pre-existing hole: the retired ``extract_user_turns`` never sets
        ``saw_user`` for this body and returns ``None``, so the fingerprint
        excluded it before #1426. The rule that closes it is stated over the
        recovered halves rather than over this body shape — a human half
        still carrying an ``**Assistant**:`` marker was never separated from
        the model in the first place, whichever arm produced it.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(
            tmp_path,
            "trunc.md",
            platform="chatgpt",
            body="# Conversation (turn 1)\n\n> **Assistant**: " + _AI_PROSE + "\n",
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        assert _corpus(tmp_path) == []

    def test_claude_platform_body_with_chatgpt_markers_is_excluded(
        self, tmp_path: Path
    ) -> None:
        """The same hole on the Claude arm, from the same review's audit.

        For ``platform: claude`` the blockquote *is* the role signal — the
        merged Claude rendering left the model's reply unquoted — so a body
        carrying ChatGPT's ``**User**:``/``**Assistant**:`` markers inside the
        quote hands every line, the model's included, to the human half.

        This one narrows against HEAD rather than restoring it:
        ``extract_user_turns`` is platform-blind and walked the markers, so it
        returned ``"my question"`` and dropped the reply. Honouring markers on
        the Claude arm is not available as a fix — it would make
        ``resplit_merged_ai_chat`` call this body ``MERGED`` and
        ``md_file.unlink()`` a fragment the shipped
        ``_parse_claude_merged`` has always returned ``None`` for. Between
        losing one hand-edited fragment's question and admitting a model reply
        to the false-positive authority, the corpus keeps neither.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(
            tmp_path,
            "mislabelled.md",
            platform="claude",
            body="> **User**: my question\n>\n> **Assistant**: " + _AI_PROSE,
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        assert _corpus(tmp_path) == []

    def test_merged_chatgpt_yields_only_the_user_half(self, tmp_path: Path) -> None:
        """The historical ``**User**:``/``**Assistant**:`` body is unchanged.

        Byte-for-byte the same corpus the marker-based splitter produced.
        Shape dispatch adds shapes; it must not renegotiate the one that
        already worked, or every legacy chat fragment in an operator's vault
        re-measures.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(tmp_path, "c.md", platform="chatgpt", body=_CHATGPT_BODY)

        fp = build_fingerprint(tmp_path, _CONFIG)

        assert fp.fragment_count == 1
        assert _corpus(tmp_path) == ["I reckon it was fine and that was that"]
        assert fp.rate_for("ai_vocab_density") == 0.0
        assert all("tapestry" not in text for text in _corpus(tmp_path))

    def test_two_send_human_turn_survives(self, tmp_path: Path) -> None:
        """A #1429 two-send human turn reaches the corpus with both sends.

        ``creek.ingest.turns.TURN_TEXT_SEPARATOR`` joins the messages of one
        same-role run with a blank line, and
        ``ClaudeIngestor.convert_to_markdown`` prefixes every line — including
        that blank one — with ``"> "``. So a split human body legitimately
        contains a bare ``"> "`` line, and a shape test has to read it as
        blockquoted rather than as prose escaping the quote. The body is built
        by the real ingestor here precisely so this test cannot drift from
        what #1429 actually writes.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        text = "first send\n\nsecond send"
        body = ClaudeIngestor().convert_to_markdown(
            ParsedFragment(
                content=text,
                metadata={"turn_text": text, "author_role": "self"},
                source_path="/exports/claude.json",
                timestamp=datetime(2024, 11, 15, 10, 0, tzinfo=UTC),
            ),
        )
        assert "\n> \n" in body, "the separator must render as a quoted blank line"
        _write_fragment(tmp_path, "t.md", platform="claude", body=body)

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == [text]

    def test_user_marker_in_operator_prose_is_not_truncated(
        self, tmp_path: Path
    ) -> None:
        """A ``user:`` inside the operator's own sentence is content, not a cue.

        At HEAD this body is *not* excluded — ``extract_user_turns`` returns
        ``'alice\\nwhich finally explained the 403.'``, a non-``None`` value —
        so the first sentence is silently deleted from the corpus and no
        ``None``-keyed fallback could ever have rescued it. The loss is
        invisible: ``fragment_count`` is 1 either way. Only shape dispatch,
        which never treats a line's contents as a speaker cue, fixes it.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        body = (
            "> I was debugging an auth bug today and the log line read\n"
            "> user: alice\n"
            "> which finally explained the 403."
        )
        _write_fragment(tmp_path, "u.md", platform="claude", body=body)

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == [
            "I was debugging an auth bug today and the log line read\n"
            "user: alice\n"
            "which finally explained the 403."
        ]

    def test_bare_assistant_marker_in_operator_prose_is_included(
        self, tmp_path: Path
    ) -> None:
        """``Assistant:`` written by the operator is their prose, not a boundary.

        ``_ASSISTANT_MARKER`` is the loose regex ``^\\**assistant\\**\\s*:``,
        which matches a bare ``Assistant:`` anywhere a line starts with it.
        The ChatGPT arm partitions on the *literal* ``**Assistant**:`` string
        instead, and the Claude arm has no speaker markers at all, so a
        sentence that merely begins with the word survives intact.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        body = "> Assistant: please file the ticket\n> is what I told the intern."
        _write_fragment(tmp_path, "a.md", platform="claude", body=body)

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == [
            "Assistant: please file the ticket\nis what I told the intern."
        ]

    def test_nested_quote_keeps_the_operators_own_marker(self, tmp_path: Path) -> None:
        """An operator quoting somebody else keeps their own ``>``.

        The rendering adds exactly one marker, so the parse removes exactly
        one: ``ClaudeIngestor.convert_to_markdown`` writes ``f"> {line}"``, and
        ``line[1:].lstrip()`` is its inverse. What this pins is the
        round-trip, on the realistic body the ingestor actually produces.

        A caveat worth recording, because the obvious reading of this test is
        wrong. It does **not** discriminate ``line[1:].lstrip()`` from
        ``_strip_quote``'s ``line.lstrip(">").strip()``. ``lstrip(">")`` halts
        at the space, so both yield ``"> he said hi"`` here. The two forms
        diverge only on a line beginning ``">>"`` with no space --- and since
        every writer of a stored body interpolates ``f"> {line}"``, neither
        the ingestors nor ``refresh._build_split_fragment`` can emit one. The
        one-marker form is kept because it is ``refresh``'s shipped
        semantics and byte-parity with the migration is a tested
        requirement, not because the greedy form is reachable here.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(
            tmp_path,
            "n.md",
            platform="claude",
            body="> > he said hi\n> and I laughed",
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == ["> he said hi\nand I laughed"]

    def test_nested_quote_keeps_the_operators_own_marker_on_chatgpt(
        self, tmp_path: Path
    ) -> None:
        """The ChatGPT arm unquotes the same way, under an AI heading.

        The Claude and ChatGPT arms carry *separate* copies of the unquote
        step, so covering only one leaves the other free to drift. ChatGPT is
        the higher-volume half of the corpus and the only arm that also
        strips a heading, so this pins both operations acting on one body:
        the AI-generated title goes, the operator's own ``>`` stays.

        Like its Claude twin this does not discriminate the one-marker strip
        from ``_strip_quote`` --- see that test's docstring for why no
        reachable body can.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(
            tmp_path,
            "nc.md",
            platform="chatgpt",
            body="# Some AI Title\n\n> > he said hi\n> and I laughed",
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == ["> he said hi\nand I laughed"]

    @pytest.mark.parametrize("platform", ["claude", "chatgpt"])
    def test_ai_turn_contributes_nothing(self, tmp_path: Path, platform: str) -> None:
        """An ``author: ai`` fragment stays out however human its body looks.

        The authorship filter runs before any shape dispatch, so widening the
        set of recognised shapes must not widen the set of admitted authors.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
            platform: The conversation platform under test.
        """
        _write_fragment(
            tmp_path,
            "ai.md",
            author="ai",
            platform=platform,
            body="> " + _HUMAN_PROSE,
        )

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        assert _corpus(tmp_path) == []

    @pytest.mark.parametrize(
        "platform",
        ["journal", "markdown", "substack", "essay", "email", "discord", "messages"],
    )
    def test_non_conversation_platform_body_is_unchanged(
        self, tmp_path: Path, platform: str
    ) -> None:
        """Off the conversation path the whole body is the corpus, verbatim.

        A journal entry may legitimately quote somebody (``> quoted line``) or
        carry a heading the operator typed. No stripping happens for these
        platforms, so both survive byte-for-byte — the shape dispatch is
        gated on ``CONVERSATION_PLATFORMS`` and nothing else.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
            platform: A non-conversation source platform.
        """
        body = (
            "# Heading\n"
            "\n"
            "> quoted line\n"
            "\n"
            "and then my own prose, neither quoted nor headed."
        )
        _write_fragment(tmp_path, "p.md", platform=platform, body=body)

        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1
        assert _corpus(tmp_path) == [body]

    @pytest.mark.parametrize(
        ("tier", "expected_weight"),
        [("open", 0.225), ("unclassified", 0.1125)],
        ids=["open-tier", "unclassified-tier"],
    )
    def test_split_human_turn_survives_the_weighted_entry_path(
        self, tmp_path: Path, tier: str, expected_weight: float
    ) -> None:
        """Chat survives the weighted path's membership gate, at a measured weight.

        ``build_fingerprint`` has two entry paths and they are not equivalent.
        With ``audience_weighting`` supplied, ``_eligible_texts`` applies
        ``if weight > 0.0`` as a **membership gate**: a fragment weighted to
        zero is dropped from the corpus outright rather than merely de-ranked.
        Chat is multiplied down hard on that path — the ``claude`` authorship
        weight 0.5 times ``audience`` 1.0 x ``privacy_tier`` x
        ``representativeness`` 1.0 x ``platform_authority`` 0.3 — so the
        surviving weight is asserted rather than assumed. Both tiers stay
        strictly positive, which is what keeps the gate open.

        The unweighted path is exercised in the same test so a fix that only
        works for one caller cannot pass.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
            tier: The fragment's ``privacy_tier``.
            expected_weight: The product the weighted path must produce.
        """
        from creek.config import VoiceAudienceWeightingConfig

        weighting = VoiceAudienceWeightingConfig()
        assert weighting.enabled is True
        _write_fragment(
            tmp_path,
            "w.md",
            platform="claude",
            tier=tier,
            body="> " + _HUMAN_PROSE,
        )

        weighted = _eligible_texts(
            tmp_path,
            _CONFIG,
            include_intimate=False,
            audience_weighting=weighting,
        )

        assert [text for _weight, text in weighted] == [_HUMAN_PROSE]
        assert weighted[0][0] == pytest.approx(expected_weight)
        assert weighted[0][0] > 0.0
        weighted_fp = build_fingerprint(
            tmp_path,
            _CONFIG,
            audience_weighting=weighting,
        )
        assert weighted_fp.fragment_count == 1
        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1


_KEYLESS_BODY = "The untiered note nobody ever classified, kept in a drawer."
"""Body of the fragment whose frontmatter carries no ``privacy_tier`` key."""

_DECLARED_BODY = "The note that says out loud which tier it belongs to."
"""Body of the fragment that declares its tier explicitly."""

# The tier survey #1529 measures over: the four enum members, the *absent*
# key, and the two malformed spellings `raw_privacy_tier` also fails closed
# on. ``None`` means "omit the key"; ``'""'`` writes an explicit empty string.
_TIER_CASES: dict[str, str | None] = {
    "absent": None,
    "open": "open",
    "personal": "personal",
    "intimate": "intimate",
    "unclassified": "unclassified",
    "unrecognised": "bogus",
    "empty": '""',
}

# Admission measured at HEAD (4767db3), before the fix: the surviving weight of
# a lone self-authored `markdown` fragment, keyed by (weighted, include_intimate)
# then by tier case. ``0.0`` means the fragment was refused — `_eligible_texts`
# ends `if weight > 0.0`, so a zero authority is a membership gate here.
_HEAD_WEIGHTS: dict[tuple[bool, bool], dict[str, float]] = {
    (True, False): {
        "absent": 0.75,
        "open": 1.5,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 0.75,
        "unrecognised": 1.0,
        "empty": 1.0,
    },
    (True, True): {
        "absent": 0.75,
        "open": 1.5,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 0.75,
        "unrecognised": 1.0,
        "empty": 1.0,
    },
    (False, False): {
        "absent": 1.0,
        "open": 1.0,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 1.0,
        "unrecognised": 1.0,
        "empty": 1.0,
    },
    (False, True): {
        "absent": 1.0,
        "open": 1.0,
        "personal": 1.0,
        "intimate": 1.0,
        "unclassified": 1.0,
        "unrecognised": 1.0,
        "empty": 1.0,
    },
}

# The same survey after the fix: `absent`, `unrecognised` and `empty` are
# resolved by `raw_privacy_tier` to INTIMATE, so each becomes byte-for-byte
# the `intimate` cell of its own row. Every declared, recognised tier is
# untouched.
_FIXED_WEIGHTS: dict[tuple[bool, bool], dict[str, float]] = {
    (True, False): {
        "absent": 0.0,
        "open": 1.5,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 0.75,
        "unrecognised": 0.0,
        "empty": 0.0,
    },
    (True, True): {
        "absent": 0.0,
        "open": 1.5,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 0.75,
        "unrecognised": 0.0,
        "empty": 0.0,
    },
    (False, False): {
        "absent": 0.0,
        "open": 1.0,
        "personal": 1.0,
        "intimate": 0.0,
        "unclassified": 1.0,
        "unrecognised": 0.0,
        "empty": 0.0,
    },
    (False, True): {
        "absent": 1.0,
        "open": 1.0,
        "personal": 1.0,
        "intimate": 1.0,
        "unclassified": 1.0,
        "unrecognised": 1.0,
        "empty": 1.0,
    },
}


def _lone_fragment_weight(
    vault: Path,
    case: str,
    *,
    weighted: bool,
    include_intimate: bool,
) -> float:
    """Return the surviving weight of a lone fragment written for *case*.

    Args:
        vault: A private vault root (one fragment, so the reading is unambiguous).
        case: A key of :data:`_TIER_CASES`.
        weighted: Supply the default :class:`VoiceAudienceWeightingConfig` when
            ``True``; pass ``None`` (the ``creek voice-check`` fallback path)
            when ``False``.
        include_intimate: Forwarded to :func:`_eligible_texts`.

    Returns:
        The fragment's weight, or ``0.0`` when it was refused admission.
    """
    _write_fragment(vault, "f.md", tier=_TIER_CASES[case], body=_DECLARED_BODY)
    pairs = _eligible_texts(
        vault,
        _CONFIG,
        include_intimate=include_intimate,
        audience_weighting=VoiceAudienceWeightingConfig() if weighted else None,
    )
    assert len(pairs) <= 1
    return pairs[0][0] if pairs else 0.0


class TestAbsentPrivacyTierFailsClosed:
    """An untiered fragment must not reach the voice corpus (#1529).

    ``_eligible_texts`` walks raw frontmatter and never builds a
    :class:`~creek.models.Fragment`, so it needs the raw-frontmatter tier
    reader, not a hand-rolled comparison. Until #1529 it asked
    ``post.metadata.get("privacy_tier") == "intimate"``, which answers
    ``False`` for a file with no key at all and admitted it — a *raw* read
    that failed **open**, the exact inverse of
    :func:`creek.classify.privacy_filter.raw_privacy_tier`, the repo's one
    fail-closed raw reader.
    """

    def test_untiered_fragment_is_refused_while_a_tiered_one_survives(
        self, tmp_path: Path
    ) -> None:
        """The keyless fragment is dropped; the declared-open one is kept.

        The surviving fragment is the positive control: a walk that saw zero
        fragments would satisfy every "not admitted" assertion vacuously, so
        the corpus is asserted to be exactly ``[_DECLARED_BODY]`` rather than
        merely free of ``_KEYLESS_BODY``.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(tmp_path, "keyless.md", tier=None, body=_KEYLESS_BODY)
        _write_fragment(tmp_path, "open.md", tier="open", body=_DECLARED_BODY)

        pairs = _eligible_texts(
            tmp_path,
            _CONFIG,
            include_intimate=False,
            audience_weighting=VoiceAudienceWeightingConfig(),
        )

        assert pairs, "positive control: the corpus must not be empty"
        assert [text for _weight, text in pairs] == [_DECLARED_BODY]
        assert all(_KEYLESS_BODY not in text for _weight, text in pairs)
        assert (
            build_fingerprint(
                tmp_path,
                _CONFIG,
                audience_weighting=VoiceAudienceWeightingConfig(),
            ).fragment_count
            == 1
        )

    def test_untiered_fragment_is_refused_on_the_unweighted_path_too(
        self, tmp_path: Path
    ) -> None:
        """The admission gate alone must refuse it when no weighting is supplied.

        ``build_fingerprint``'s ``audience_weighting=None`` default is reachable
        in production — ``creek/cli.py``'s ``_resolve_voice_fingerprint`` takes
        it when a vault has no persisted fingerprint — and on that path the
        authority multiplier is skipped entirely. So the gate, not the ``0.0``
        intimate authority, has to be what refuses here.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
        """
        _write_fragment(tmp_path, "keyless.md", tier=None, body=_KEYLESS_BODY)
        _write_fragment(tmp_path, "open.md", tier="open", body=_DECLARED_BODY)

        pairs = _eligible_texts(tmp_path, _CONFIG, include_intimate=False)

        assert pairs, "positive control: the corpus must not be empty"
        assert [text for _weight, text in pairs] == [_DECLARED_BODY]
        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 1

    def test_an_absent_key_is_treated_exactly_as_a_declared_intimate(self) -> None:
        """The authority multiplier resolves the absent key the way the gate does.

        This is the property that keeps one tier opinion in the module: the
        weighting must not disagree with the admission gate about the same
        file. Asserted as an equality against the ``intimate`` reading rather
        than against a literal, so it holds for any configured authority map.
        """
        weighting = VoiceAudienceWeightingConfig()

        absent = _audience_factor({}, "markdown", weighting)
        declared_intimate = _audience_factor(
            {"privacy_tier": "intimate"}, "markdown", weighting
        )

        assert absent == declared_intimate
        assert absent == 0.0

    def test_an_explicit_unclassified_keeps_its_own_authority(self) -> None:
        """Only the *absent* key moves; ``unclassified`` still reads ``0.75``.

        A pipeline-written note that says ``unclassified`` out loud carries
        more assurance than one with no key at all, and ranks with
        ``personal`` (#876/#961). Collapsing the two would be a behaviour
        change nobody asked for.
        """
        weighting = VoiceAudienceWeightingConfig()

        unclassified = _audience_factor(
            {"privacy_tier": "unclassified"}, "markdown", weighting
        )

        assert unclassified == 0.75
        assert unclassified != _audience_factor({}, "markdown", weighting)

    @pytest.mark.parametrize(
        ("case", "value"),
        [("unrecognised", "bogus"), ("empty", ""), ("null", None)],
    )
    def test_a_malformed_tier_value_also_fails_closed(
        self, case: str, value: str | None
    ) -> None:
        """A tier the enum cannot parse must weigh as ``intimate``, not as ``1.0``.

        At HEAD these fell through both guards — ``!= "intimate"`` admitted
        them and ``privacy_tier_authority.get(..., 1.0)`` weighted them
        *above* ``personal``. Routing through ``raw_privacy_tier`` narrows
        them along with the absent key, because they carry exactly as little
        assurance.

        Args:
            case: Human-readable label for the malformed spelling.
            value: The raw ``privacy_tier`` value under test.
        """
        weighting = VoiceAudienceWeightingConfig()

        factor = _audience_factor({"privacy_tier": value}, "markdown", weighting)

        assert factor == 0.0, case

    def test_the_legacy_public_alias_resolves_to_its_open_authority(self) -> None:
        """``privacy_tier: public`` now weighs as ``open``, not as an unknown.

        INC-003 keeps ``"public"`` readable as the deprecated spelling of
        ``open``, and ``raw_privacy_tier`` honours it. Until #1529 this module
        compared the raw string against its own authority table, where
        ``"public"`` is simply absent, so a legacy note fell to the ``1.0``
        unknown-value fall-back instead of ``open``'s ``1.5``. Routing through
        the shared reader corrects that too.

        It is the one cell in this survey whose weight *rises*, so it is
        pinned deliberately rather than discovered later: it is not a privacy
        loosening — ``open`` is the least sensitive tier, and the note was
        admitted at either weight — it is the module stopping disagreeing with
        the canonical reader about what the file says.
        """
        weighting = VoiceAudienceWeightingConfig()

        with pytest.warns(DeprecationWarning):
            public = _audience_factor({"privacy_tier": "public"}, "markdown", weighting)

        assert public == _audience_factor(
            {"privacy_tier": "open"}, "markdown", weighting
        )
        assert public == 1.5

    def test_the_module_agrees_with_the_one_fail_closed_raw_reader(self) -> None:
        """No fifth tier opinion: the authority read tracks ``raw_privacy_tier``.

        Pins the weighting's refusal (a ``0.0`` factor under the default
        authority map, which is a membership gate here) to the shared reader
        for every spelling in the survey, so a future edit cannot reintroduce
        a private comparison that drifts from ``creek.classify.privacy_filter``.
        The admission gate's half of the same agreement is covered by the two
        refusal tests above and by the narrowing survey below.
        """
        weighting = VoiceAudienceWeightingConfig()
        refused: list[str] = []
        for case, value in _TIER_CASES.items():
            raw: dict[str, object] = {}
            if value is not None:
                raw["privacy_tier"] = "" if value == '""' else value
            expected_intimate = raw_privacy_tier(raw) is PrivacyTier.INTIMATE
            assert (_audience_factor(raw, "markdown", weighting) == 0.0) is (
                expected_intimate
            ), case
            if expected_intimate:
                refused.append(case)

        assert sorted(refused) == ["absent", "empty", "intimate", "unrecognised"]

    @pytest.mark.parametrize("weighted", [True, False], ids=["weighted", "unweighted"])
    @pytest.mark.parametrize(
        "include_intimate", [True, False], ids=["include", "exclude"]
    )
    def test_admission_never_widens_against_the_head_baseline(
        self, tmp_path: Path, weighted: bool, include_intimate: bool
    ) -> None:
        """The whole 2x2x7 survey is a strict narrowing of HEAD's behaviour.

        ``privacy_tier`` is a one-way ratchet, so this fix may only ever
        *refuse* more. Each cell's HEAD weight was measured at 4767db3 and
        frozen in :data:`_HEAD_WEIGHTS`; the post-fix expectation is
        :data:`_FIXED_WEIGHTS`. Both are asserted — the exact value so the
        fix is pinned, and ``<=`` so no future edit can widen a cell — plus a
        positive control (some cell still admits) and a negative control (some
        cell narrowed), so neither an all-empty nor an all-unchanged survey
        can pass.

        Args:
            tmp_path: pytest temporary directory used as the vault root.
            weighted: Whether an audience-weighting config is supplied.
            include_intimate: Whether intimate-tier fragments are requested.
        """
        head = _HEAD_WEIGHTS[weighted, include_intimate]
        fixed = _FIXED_WEIGHTS[weighted, include_intimate]
        assert set(head) == set(fixed) == set(_TIER_CASES)

        measured = {
            case: _lone_fragment_weight(
                tmp_path / case,
                case,
                weighted=weighted,
                include_intimate=include_intimate,
            )
            for case in _TIER_CASES
        }

        assert measured == pytest.approx(fixed)
        for case, weight in measured.items():
            assert weight <= head[case], f"{case} admits more than HEAD did"
        assert any(weight > 0.0 for weight in measured.values()), (
            "positive control: the survey must still admit something"
        )
        if (weighted, include_intimate) != (False, True):
            assert any(measured[case] < head[case] for case in measured), (
                "negative control: this row must have narrowed"
            )
