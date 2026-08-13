"""Tests for the idiolect / voice-fingerprint profiler (FEAT-040.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import (
    _eligible_texts,
    build_fingerprint,
    extract_user_turns,
    load_fingerprint,
    save_fingerprint,
)
from creek.ingest.base import ParsedFragment
from creek.ingest.claude import ClaudeIngestor

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = AIStyleConfig()


def _write_fragment(
    vault: Path,
    name: str,
    *,
    author: str = "self",
    platform: str = "markdown",
    tier: str = "open",
    body: str = "A short honest sentence about the river.",
) -> None:
    """Write a minimal fragment .md with the given source frontmatter."""
    path = vault / "01-Fragments" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    front = (
        "---\n"
        "source:\n"
        f"  author: {author}\n"
        f"  platform: {platform}\n"
        f"privacy_tier: {tier}\n"
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
    """A fragment with broken YAML frontmatter is skipped, not fatal."""
    good = tmp_path / "01-Fragments" / "good.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        "---\nsource:\n  author: self\n  platform: markdown\n---\nclean note\n",
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
