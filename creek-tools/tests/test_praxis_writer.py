"""The fragment → Praxis mapping and the ``04-Praxis`` writer pass (#2122).

Before #2122, :meth:`creek.vault.writer.VaultWriter.write_praxis` had no
caller: the praxis pass stamped ``praxis_potential: explicit`` on fragments
and nothing ever turned one into a ``04-Praxis/`` page, so
``ReflectionResponse.related_praxis`` was empty for every vault. These tests
pin the pure mapping first, then the vault pass: what it writes, where, with
which tier, and that a second run (or an owner's edit) is never overwritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.classify.praxis_writer import (
    PRAXIS_TITLE_MAX_CHARS,
    derived_fragment_ids,
    praxis_from_fragment,
    praxis_title,
    praxis_type_for,
    write_praxis_pages,
)
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    Praxis,
    PraxisPotential,
    PraxisStatus,
    PraxisType,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.writer import VaultWriter
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path


_SIT_BODY = (
    "From now on, I'm committing to a 10-minute morning sit. I will start tomorrow."
)
"""Explicit practice language — two distinct moderate markers."""


def _fragment(
    frag_id: str = "frag-2122-sit",
    *,
    title: str = "Morning notes",
    potential: PraxisPotential = PraxisPotential.EXPLICIT,
    tier: PrivacyTier = PrivacyTier.PERSONAL,
) -> Fragment:
    """Build a fragment carrying a praxis verdict and a tier."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            author=Authorship.SELF,
        ),
        praxis_potential=potential,
        privacy_tier=tier,
    )


def _seed(vault: Path, fragment: Fragment, body: str = _SIT_BODY) -> Path:
    """Write *fragment* into a scaffolded vault's ``01-Fragments/Notes``."""
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    return write_fragment_file(vault=vault, fragment=fragment, body=body)


def _praxis_pages(vault: Path) -> list[Path]:
    """Return every markdown page under ``04-Praxis``, sorted."""
    root = vault / "04-Praxis"
    return sorted(root.rglob("*.md")) if root.is_dir() else []


# --------------------------------------------------------------------------- #
# praxis_type_for
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("I'm committing to a 10-minute morning sit.", PraxisType.HABIT),
        ("A daily walk after lunch.", PraxisType.HABIT),
        ("Each evening I will journal.", PraxisType.HABIT),
        ("This is a habit worth keeping.", PraxisType.HABIT),
        ("I commit to finishing the manuscript this year.", PraxisType.COMMITMENT),
        ("A vow to stop drinking.", PraxisType.COMMITMENT),
        ("I should read more slowly.", PraxisType.INSIGHT),
    ],
)
def test_praxis_type_follows_the_keyword_map(body: str, expected: PraxisType) -> None:
    """Habit language wins, then commitment language, else insight."""
    assert praxis_type_for(body) is expected


def test_keywords_match_whole_words_only() -> None:
    """``commitee``-style substrings and ``mornings`` lookalikes do not fire."""
    assert praxis_type_for("The committee met; dailyness is not a word.") is (
        PraxisType.INSIGHT
    )


# --------------------------------------------------------------------------- #
# praxis_title
# --------------------------------------------------------------------------- #


def test_title_is_the_first_sentence() -> None:
    """The first sentence, terminator included, nothing after it."""
    assert praxis_title(_fragment(), _SIT_BODY) == (
        "From now on, I'm committing to a 10-minute morning sit."
    )


def test_title_strips_markdown_line_markers() -> None:
    """A checkbox or heading prefix is not part of the practice's name."""
    body = "- [ ] book the boiler service before October\nmore"
    assert praxis_title(_fragment(), body) == ("book the boiler service before October")


def test_title_is_bounded_to_the_cap_on_a_word_boundary() -> None:
    """A long first sentence is cut at a word boundary within the cap."""
    body = "I will " + "practise slowly " * 20 + "today."
    title = praxis_title(_fragment(), body)
    assert len(title) <= PRAXIS_TITLE_MAX_CHARS
    assert title.startswith("I will practise slowly")
    assert not title.endswith(" ")
    assert body.startswith(title)


def test_an_unbroken_run_longer_than_the_cap_is_hard_cut() -> None:
    """With no space to break on, the cap still holds."""
    title = praxis_title(_fragment(), "x" * (PRAXIS_TITLE_MAX_CHARS * 2))
    assert title == "x" * PRAXIS_TITLE_MAX_CHARS


def test_an_empty_body_falls_back_to_the_fragment_title() -> None:
    """No prose to name it from means the fragment's own title names it."""
    assert praxis_title(_fragment(title="Morning notes"), "  \n- [ ]  \n") == (
        "Morning notes"
    )


# --------------------------------------------------------------------------- #
# praxis_from_fragment
# --------------------------------------------------------------------------- #


def test_mapping_carries_provenance_type_title_and_proposed_status() -> None:
    """The issue's mapping, field by field."""
    praxis = praxis_from_fragment(_fragment("frag-a"), _SIT_BODY)

    assert praxis.derived_from == ["frag-a"]
    assert praxis.praxis_type == PraxisType.HABIT.value
    assert praxis.status == PraxisStatus.PROPOSED.value
    assert praxis.title.startswith("From now on")
    assert praxis.id == "praxis-frag-a"


# --------------------------------------------------------------------------- #
# write_praxis_pages
# --------------------------------------------------------------------------- #


def test_an_explicit_fragment_becomes_a_daily_praxis_page(tmp_path: Path) -> None:
    """The bug's fix: an explicit fragment produces a page under ``Daily/``."""
    _seed(tmp_path, _fragment("frag-a"))

    summary = write_praxis_pages(tmp_path)

    assert summary.written == 1
    assert summary.errors == ()
    pages = _praxis_pages(tmp_path)
    assert [page.parent.name for page in pages] == ["Daily"]
    meta = frontmatter.load(str(pages[0])).metadata
    assert meta["type"] == "praxis"
    assert meta["praxis_type"] == "habit"
    assert meta["derived_from"] == ["frag-a"]
    assert meta["status"] == "proposed"


@pytest.mark.parametrize("potential", [PraxisPotential.NONE, PraxisPotential.LATENT])
def test_only_an_explicit_verdict_writes_a_page(
    tmp_path: Path, potential: PraxisPotential
) -> None:
    """``latent`` is a hunch the author has not named — no page for it."""
    _seed(tmp_path, _fragment(potential=potential))

    assert write_praxis_pages(tmp_path).written == 0
    assert _praxis_pages(tmp_path) == []


def test_a_rerun_never_writes_a_second_page(tmp_path: Path) -> None:
    """Idempotent: dedupe is by ``derived_from``."""
    _seed(tmp_path, _fragment("frag-a"))

    write_praxis_pages(tmp_path)
    second = write_praxis_pages(tmp_path)

    assert second.written == 0
    assert len(_praxis_pages(tmp_path)) == 1


def test_an_owner_edited_page_is_left_byte_for_byte(tmp_path: Path) -> None:
    """A page the owner promoted (or moved) is never rewritten or duplicated."""
    _seed(tmp_path, _fragment("frag-a"))
    write_praxis_pages(tmp_path)
    (page,) = _praxis_pages(tmp_path)
    post = frontmatter.load(str(page))
    post["status"] = PraxisStatus.ACTIVE.value
    post["title"] = "My sit"
    moved = tmp_path / "04-Praxis" / "Seasonal" / "mine.md"
    moved.parent.mkdir(parents=True)
    moved.write_text(frontmatter.dumps(post), encoding="utf-8")
    page.unlink()
    before = moved.read_bytes()

    assert write_praxis_pages(tmp_path).written == 0
    assert _praxis_pages(tmp_path) == [moved]
    assert moved.read_bytes() == before


@pytest.mark.parametrize(
    "tier", [PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE]
)
def test_the_page_inherits_its_source_tier(tmp_path: Path, tier: PrivacyTier) -> None:
    """A praxis page is never tiered below the fragment it was compiled from."""
    _seed(tmp_path, _fragment(tier=tier))

    write_praxis_pages(tmp_path)

    (page,) = _praxis_pages(tmp_path)
    assert frontmatter.load(str(page)).metadata["privacy_tier"] == tier.value


def test_an_untiered_source_fails_closed_to_intimate(tmp_path: Path) -> None:
    """A source with no ``privacy_tier`` key is treated as intimate."""
    path = _seed(tmp_path, _fragment())
    post = frontmatter.load(str(path))
    del post["privacy_tier"]
    path.write_text(frontmatter.dumps(post), encoding="utf-8")

    write_praxis_pages(tmp_path)

    (page,) = _praxis_pages(tmp_path)
    assert frontmatter.load(str(page)).metadata["privacy_tier"] == "intimate"


def test_a_missing_fragments_folder_writes_nothing(tmp_path: Path) -> None:
    """A vault with no ``01-Fragments`` is a no-op, not an error."""
    summary = write_praxis_pages(tmp_path)

    assert summary.written == 0
    assert summary.errors == ()


def test_an_unscaffolded_vault_skips_the_stage_without_raising(
    tmp_path: Path,
) -> None:
    """No ``00-Creek-Meta`` means no writer: nothing written, nothing raised."""
    write_fragment_file(vault=tmp_path, fragment=_fragment(), body=_SIT_BODY)

    summary = write_praxis_pages(tmp_path)

    assert summary.written == 0
    assert summary.errors == ()
    assert _praxis_pages(tmp_path) == []


def test_an_unloadable_explicit_note_is_skipped(tmp_path: Path) -> None:
    """A header that says explicit on a non-fragment note writes nothing."""
    (tmp_path / "00-Creek-Meta").mkdir()
    notes = tmp_path / "01-Fragments" / "Notes"
    notes.mkdir(parents=True)
    (notes / "stray.md").write_text(
        "---\npraxis_potential: explicit\n---\nI will. From now on.\n",
        encoding="utf-8",
    )

    assert write_praxis_pages(tmp_path).written == 0


def test_a_failed_write_is_reported_without_a_path_and_the_pass_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing page costs that page only, and names an id, not a path."""
    _seed(tmp_path, _fragment("frag-a"))
    _seed(tmp_path, _fragment("frag-b"))
    real = VaultWriter.write_praxis
    calls: list[str] = []

    def _flaky(self: VaultWriter, praxis: Praxis, **kwargs: Any) -> Path:
        calls.append(praxis.derived_from[0])
        if praxis.derived_from == ["frag-a"]:
            msg = f"disk full at {tmp_path}"
            raise OSError(msg)
        return real(self, praxis, **kwargs)

    monkeypatch.setattr(VaultWriter, "write_praxis", _flaky)

    summary = write_praxis_pages(tmp_path)

    assert calls == ["frag-a", "frag-b"]
    assert summary.written == 1
    assert len(summary.errors) == 1
    assert "frag-a" in summary.errors[0]
    assert str(tmp_path) not in summary.errors[0]


def test_derived_ids_ignore_non_praxis_and_malformed_pages(tmp_path: Path) -> None:
    """Only a ``type: praxis`` page's string ``derived_from`` entries count."""
    root = tmp_path / "04-Praxis" / "Daily"
    root.mkdir(parents=True)
    (root / "a.md").write_text(
        "---\ntype: praxis\ntitle: A\nderived_from: [frag-a, 7]\n---\n",
        encoding="utf-8",
    )
    (root / "b.md").write_text(
        "---\ntype: eddy\ntitle: B\nderived_from: [frag-b]\n---\n",
        encoding="utf-8",
    )
    (root / "c.md").write_text(
        "---\ntype: praxis\ntitle: C\nderived_from: frag-c\n---\n",
        encoding="utf-8",
    )

    assert derived_fragment_ids(tmp_path) == {"frag-a"}
