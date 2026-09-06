"""Characterisation of the cleaning pipeline's **live** defaults (#1519).

Written green **before** the ``cleaning`` config block was collapsed onto the
filter-side models, and required to stay green and unedited afterwards. #1519
resolves seven drifted defaults by declaring that *the live value wins* — the
value the running code actually uses, not the dormant one written down in
``creek/config.py``. That rule is only safe if the live values are pinned
first, so this file is the fence around them.

Every assertion here is on **observable behaviour** — a keep/skip verdict, an
action enum, a reported count — never on an attribute name or a signature,
both of which a later refactor may legitimately move. Two consequences:

* A row that moves silently under an attribute assertion cannot move silently
  here. ``QualityScorer``'s thresholds are pinned by comparing a
  default-constructed scorer's verdict against an explicitly-configured one,
  so the test says "the default is not 0.7" rather than restating 0.6.
* The scanner rows reach ``HygieneReporter``'s own duplicate literals
  (``creek/clean/hygiene.py``) and ``creek clean orphans``'s
  ``typer.Option(30)`` in ``creek/cli.py``. Both are outside #1519's edit
  scope; pinning them behaviourally is how the residual duplication is held
  still without touching those files.

The Discord minimum-length row is deliberately redundant with
``tests/test_discord_filter.py``: it is the single row whose resolution could
change what ``creek ingest`` keeps, so it is asserted a second time in the
file that must not change.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter
from typer.testing import CliRunner

from creek.clean.dedup import Deduplicator
from creek.clean.filters.discord import DiscordFilter
from creek.clean.filters.google_drive import GoogleDriveFilter, StagedFile
from creek.clean.filters.markdown import MarkdownFilter
from creek.clean.hygiene import HygieneReporter, OrphanScanner, StaleReviewScanner
from creek.clean.quality import QualityScorer
from creek.clean.validator import FragmentValidator
from creek.cli import app
from creek.models import Fragment, FragmentSource, SourcePlatform, synthetic_fragment_id

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW: datetime = datetime(2026, 4, 5, 12, 0, 0, tzinfo=UTC)

_SCORES_JUST_ABOVE_SIX_TENTHS: str = "epsilon delta theta yz"
"""Content whose aggregate quality score lands in ``[0.6, 0.7)``.

That band is what separates the live ``accept_threshold`` (0.6) from the
config block's dormant 0.7, so it is the only band where the two disagree.
"""

_SCORES_JUST_BELOW_THREE_TENTHS: str = "beta beta yz delta beta beta"
"""Content whose aggregate quality score lands in ``[0.2, 0.3)``.

Separates the live ``review_threshold`` (0.3) from a lower one.
"""


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault skeleton.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the vault root.
    """
    vault = tmp_path / "vault"
    for relative in ("00-Creek-Meta", "01-Fragments/Conversations", "02-Threads"):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _write_unlinked_fragment(vault: Path, name: str, *, age_days: int) -> None:
    """Write a fragment that links to nothing and is linked from nothing.

    Args:
        vault: Vault root path.
        name: Filename stem.
        age_days: How many days ago the fragment was created.
    """
    created = datetime.now(tz=UTC) - timedelta(days=age_days)
    post = frontmatter.Post(
        content="Body text with no wiki-links at all.",
        id=f"frag-{name}",
        title=name,
        type="fragment",
        source={"platform": "claude", "original_file": f"{name}.json"},
        created=created.isoformat(),
    )
    target = vault / "01-Fragments" / "Conversations" / f"{name}.md"
    target.write_text(frontmatter.dumps(post), encoding="utf-8")


def _write_review_queue(vault: Path, *, age_days: int) -> None:
    """Write a review-queue file whose filename encodes its age.

    Args:
        vault: Vault root path.
        age_days: How many days ago the queue was written.
    """
    stamped = datetime.now(tz=UTC) - timedelta(days=age_days)
    name = f"review-queue-{stamped.strftime('%Y-%m-%d_%H%M%S')}.md"
    (vault / name).write_text("# Review Queue\n", encoding="utf-8")


def _make_msg(content: str) -> dict[str, Any]:
    """Build a raw Discord message dict.

    Args:
        content: The message text.

    Returns:
        A message dict in Discord export shape.
    """
    return {
        "id": "msg-001",
        "author": {"id": "user-alice", "name": "Alice", "isBot": False},
        "content": content,
        "timestamp": "2024-11-10T14:00:00Z",
    }


def _make_staged(authors: list[str]) -> StagedFile:
    """Build a staged Drive file owned by ``alice`` with given contributors.

    Args:
        authors: Contributor addresses, repeats allowed.

    Returns:
        A :class:`StagedFile` ready for ``filter_batch``.
    """
    return StagedFile(
        path=Path("/staged/quarterly-notes.docx"),
        filename="quarterly-notes.docx",
        content="A document with enough body text to avoid the empty check.",
        modified=_NOW,
        authors=authors,
        owner="alice@example.com",
        size_bytes=1024,
    )


def _make_fragment(title: str) -> Fragment:
    """Build a fragment whose only interesting property is its title length.

    Args:
        title: The fragment title, which is what the length check reads.

    Returns:
        A valid :class:`Fragment`.
    """
    return Fragment(
        id=synthetic_fragment_id(),
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor="alice",
        ),
        created=datetime(2024, 6, 15, tzinfo=UTC),
        ingested=datetime(2024, 6, 15, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# DiscordFilter — the one row that could change live ingest
# ---------------------------------------------------------------------------


class TestDiscordMinimumLength:
    """``DiscordFilter()`` keeps 3-character messages, skips 2-character ones.

    The live ``min_length`` is 3; ``cleaning.discord.min_message_length`` said
    10. Adopting 10 would start dropping every 3-to-9 character Discord
    message — a data-retention change, not a tidy-up. Deliberately redundant
    with ``tests/test_discord_filter.py`` so the boundary survives an edit to
    that suite.
    """

    def test_a_three_character_message_is_kept(self) -> None:
        """Three characters is exactly the live minimum, so it survives."""
        assert DiscordFilter().check(_make_msg("hey")).keep is True

    def test_a_two_character_message_is_skipped(self) -> None:
        """Two characters is below the live minimum."""
        result = DiscordFilter().check(_make_msg("hi"))
        assert result.keep is False
        assert result.reason == "below_min_length"

    def test_a_nine_character_message_is_kept(self) -> None:
        """Nine characters survives at 3 and would die at 10.

        This is the assertion that fails if ``min_length`` ever adopts the
        config block's dormant 10.
        """
        assert DiscordFilter().check(_make_msg("九 chars!!")).keep is True


# ---------------------------------------------------------------------------
# QualityScorer — accept/review/skip thresholds and the word-count floor
# ---------------------------------------------------------------------------


class TestQualityScorerThresholds:
    """``QualityScorer()`` accepts at 0.6 and reviews down to 0.3.

    Asserted by disagreement rather than by restating the numbers: a
    default-constructed scorer and an explicitly-configured one are given the
    same content, and the verdicts must differ. That fails if the default
    moves to the config block's dormant 0.7.
    """

    def test_the_default_accept_threshold_is_not_seven_tenths(self) -> None:
        """Content scoring in ``[0.6, 0.7)`` is accepted by default."""
        content = _SCORES_JUST_ABOVE_SIX_TENTHS
        assert QualityScorer().score(content).action == "accept"
        assert QualityScorer(accept_threshold=0.7).score(content).action == "review"

    def test_the_default_review_threshold_is_not_two_tenths(self) -> None:
        """Content scoring in ``[0.2, 0.3)`` is skipped by default."""
        content = _SCORES_JUST_BELOW_THREE_TENTHS
        assert QualityScorer().score(content).action == "skip"
        assert QualityScorer(review_threshold=0.2).score(content).action == "review"

    def test_the_default_word_floor_penalises_nine_words(self) -> None:
        """Nine words is short for the live floor of 10, not the config's 5."""
        nine_words = "The quick brown fox jumps over the lazy dog"
        reasons = QualityScorer().score(nine_words).reasons
        assert any("Few words" in reason for reason in reasons)

    def test_the_default_word_floor_clears_eleven_words(self) -> None:
        """Comfortably above the floor, so no word-count penalty is raised."""
        eleven_words = "The quick brown fox jumps over the lazy sleeping dog today"
        reasons = QualityScorer().score(eleven_words).reasons
        assert not any("Few words" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# MarkdownFilter — the stub-body floor
# ---------------------------------------------------------------------------


class TestMarkdownStubFloor:
    """``MarkdownFilter()`` calls a 9-character body a stub and passes 10.

    The live floor is 10; ``cleaning.markdown.min_body_length`` said 50.
    """

    def test_a_nine_character_body_is_a_stub(self) -> None:
        """One character below the live floor is skipped."""
        assert MarkdownFilter().filter("---\ntitle: T\n---\n012345678").keep is False

    def test_a_ten_character_body_is_kept(self) -> None:
        """Exactly at the live floor is kept — and would be a stub at 50."""
        assert MarkdownFilter().filter("---\ntitle: T\n---\n0123456789").keep is True


# ---------------------------------------------------------------------------
# GoogleDriveFilter — the multi-author flag
# ---------------------------------------------------------------------------


class TestGoogleDriveMultiAuthorFlag:
    """``GoogleDriveFilter(now=...)`` flags above a 0.5 non-owner ratio.

    The live threshold is 0.5; ``cleaning.google_drive.max_collaboration_ratio``
    said 0.9 under a different name. The comparison is strict (``ratio >
    threshold``), so 0.6 flags and 0.4 does not — and *neither* would flag at
    0.9, which is what makes the first assertion the drift detector.
    """

    def test_a_three_fifths_non_owner_ratio_is_flagged(self) -> None:
        """0.6 clears the live 0.5 threshold but not a 0.9 one."""
        doc = _make_staged(
            [
                "alice@example.com",
                "alice@example.com",
                "bob@example.com",
                "carol@example.com",
                "dave@example.com",
            ],
        )
        results = GoogleDriveFilter(now=_NOW).filter_batch([doc])
        assert any("author" in reason.lower() for reason in results[0].reasons)

    def test_a_two_fifths_non_owner_ratio_is_not_flagged(self) -> None:
        """0.4 sits below the live threshold, so nothing is raised."""
        doc = _make_staged(
            [
                "alice@example.com",
                "alice@example.com",
                "alice@example.com",
                "bob@example.com",
                "carol@example.com",
            ],
        )
        results = GoogleDriveFilter(now=_NOW).filter_batch([doc])
        assert not any("author" in reason.lower() for reason in results[0].reasons)


# ---------------------------------------------------------------------------
# FragmentValidator — the content-length floor
# ---------------------------------------------------------------------------


class TestFragmentValidatorContentFloor:
    """``FragmentValidator()`` rejects 19 characters and passes 20.

    Pins ``min_content_length = 20`` behaviourally. Both sides of #1519's
    ``validation.min_characters`` row already agree on 20, so this row is a
    pure rename — and this test is what proves the rename moved no value.
    """

    def test_nineteen_characters_is_too_short(self) -> None:
        """One below the floor raises ``too_short``."""
        result = FragmentValidator().validate(_make_fragment("x" * 19))
        assert any(v.code == "too_short" for v in result.violations)

    def test_twenty_characters_clears_the_floor(self) -> None:
        """Exactly at the floor raises nothing."""
        result = FragmentValidator().validate(_make_fragment("x" * 20))
        assert not any(v.code == "too_short" for v in result.violations)


# ---------------------------------------------------------------------------
# Deduplicator — the evidence that the config block has no live counterpart
# ---------------------------------------------------------------------------


def test_the_hash_deduplicator_takes_no_configuration() -> None:
    """``Deduplicator`` holds no strategy and no threshold of any kind.

    ``cleaning.deduplication.strategy`` and ``.similarity_threshold`` are
    therefore describing nothing that runs, which is why #1519 leaves both
    leaf paths alone and renames only the colliding class name. If this
    constructor ever grows a parameter, that decision needs revisiting.
    """
    assert set(inspect.signature(Deduplicator.__init__).parameters) == {"self"}


# ---------------------------------------------------------------------------
# Hygiene scanners — the two live values behind one dormant knob
# ---------------------------------------------------------------------------


class TestHygieneScannerAges:
    """``cleaning.hygiene.staleness_days`` (90) describes two live values.

    ``OrphanScanner`` runs at 30 days and ``StaleReviewScanner`` at 14, so one
    knob cannot honestly name either. #1519 splits it; these tests are what
    pin the two values it splits into.
    """

    def test_orphan_scanner_ignores_29_days_and_reports_31(
        self,
        tmp_path: Path,
    ) -> None:
        """The default orphan age is 30 days, not the config's 90.

        Args:
            tmp_path: Pytest temporary directory.
        """
        vault = _make_vault(tmp_path)
        _write_unlinked_fragment(vault, "young", age_days=29)
        _write_unlinked_fragment(vault, "old", age_days=31)

        result = OrphanScanner().scan(vault)

        assert [p for p in result.orphan_paths if "old" in p]
        assert not [p for p in result.orphan_paths if "young" in p]

    def test_stale_review_scanner_ignores_13_days_and_reports_15(
        self,
        tmp_path: Path,
    ) -> None:
        """The default stale-review age is 14 days, not the config's 90.

        Args:
            tmp_path: Pytest temporary directory.
        """
        fresh_vault = _make_vault(tmp_path / "fresh")
        _write_review_queue(fresh_vault, age_days=13)
        stale_vault = _make_vault(tmp_path / "stale")
        _write_review_queue(stale_vault, age_days=15)

        assert StaleReviewScanner().scan(fresh_vault).stale_paths == []
        assert len(StaleReviewScanner().scan(stale_vault).stale_paths) == 1

    def test_hygiene_reporter_defaults_match_the_two_scanners(
        self,
        tmp_path: Path,
    ) -> None:
        """``HygieneReporter()`` carries its own copies of 30 and 14.

        ``creek/clean/hygiene.py``'s ``HygieneReporter.__init__`` repeats both
        literals, and ``creek/cli.py`` constructs it with no arguments. #1519
        does not edit either file, so this is the assertion that holds those
        third and fourth copies still.

        Args:
            tmp_path: Pytest temporary directory.
        """
        vault = _make_vault(tmp_path)
        _write_unlinked_fragment(vault, "young", age_days=29)
        _write_unlinked_fragment(vault, "old", age_days=31)
        _write_review_queue(vault, age_days=15)

        report = HygieneReporter().generate(vault)

        assert report.orphan_count == 1
        assert report.stale_review_count == 1


def test_creek_clean_orphans_defaults_to_thirty_days(tmp_path: Path) -> None:
    """``creek clean orphans`` with no ``--age-days`` uses 30.

    Reaches ``creek/cli.py``'s ``typer.Option(30)`` — the fifth copy of the
    orphan age, in a file #1519 must not edit — through the CLI rather than
    by reading the literal.

    Args:
        tmp_path: Pytest temporary directory.
    """
    vault = _make_vault(tmp_path)
    _write_unlinked_fragment(vault, "young", age_days=29)
    _write_unlinked_fragment(vault, "old", age_days=31)

    result = runner.invoke(app, ["clean", "orphans", "--vault", str(vault)])

    assert result.exit_code == 0
    assert "Orphans found: 1" in result.stdout
