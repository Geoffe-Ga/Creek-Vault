"""Tests for creek.vault.links — the shared wiki-link resolution index (#887).

Two lint checks each carried their own answer to "does this wiki-link
resolve", and both answered it with filename stems alone:
``BrokenLinkScanner`` (``creek/clean/hygiene.py``) and the orphan-compiled
check. Since #730 the eddy/thread linkers write date-prefixed pages
(``2023-03-30-Messages.md``) and put the human-readable name in ``aliases:``,
so stem-only matching declares almost every real link broken — 65,879 of
66,380 on the demo vault, 99.2%.

This module pins the single definition both checks now share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.vault.links import build_link_index, iter_link_sources, read_header_meta

if TYPE_CHECKING:
    from pathlib import Path


def _page(
    vault: Path,
    relpath: str,
    *,
    title: str | None = None,
    aliases: list[str] | str | None = None,
    body: str = "Body text.",
) -> Path:
    """Write a markdown page with an optional frontmatter header."""
    path = vault / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if title is not None or aliases is not None:
        lines.append("---")
        if title is not None:
            lines.append(f"title: {title}")
        if isinstance(aliases, str):
            lines.append(f"aliases: {aliases}")
        elif aliases is not None:
            lines.append("aliases:")
            lines.extend(f"  - {alias}" for alias in aliases)
        lines.append("---")
    lines.extend(("", body, ""))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """An empty vault root."""
    (tmp_path / "02-Threads").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_resolves_a_page_by_its_filename_stem(vault: Path) -> None:
    """The pre-existing stem behaviour is preserved."""
    page = _page(vault, "02-Threads/Sourdough.md")
    index = build_link_index(vault)

    assert index.resolve("Sourdough") == page
    assert "Sourdough" in index


def test_resolves_a_page_by_a_frontmatter_alias(vault: Path) -> None:
    """The #730 convention: date-prefixed filename, human name in ``aliases``.

    This is the exact shape behind the 60,993 ``[[Messages]]`` links the
    stem-only checker called broken.
    """
    page = _page(vault, "02-Threads/2020-09-26-Messages.md", aliases=["Messages"])
    index = build_link_index(vault)

    assert index.resolve("Messages") == page


def test_resolves_a_page_by_its_frontmatter_title(vault: Path) -> None:
    """``title`` is a linkable name too, per the issue's acceptance criteria."""
    page = _page(vault, "02-Threads/2023-03-30-x.md", title="Long Walks")
    index = build_link_index(vault)

    assert index.resolve("Long Walks") == page


def test_resolves_every_entry_in_a_multi_alias_list(vault: Path) -> None:
    """A page with several aliases is reachable by each of them."""
    page = _page(
        vault,
        "02-Threads/2021-01-01-many.md",
        aliases=["First Name", "Second Name"],
    )
    index = build_link_index(vault)

    assert index.resolve("First Name") == page
    assert index.resolve("Second Name") == page


def test_accepts_a_scalar_alias(vault: Path) -> None:
    """Obsidian permits ``aliases: Name`` as well as a list."""
    page = _page(vault, "02-Threads/2021-02-02-solo.md", aliases="Solo Name")
    index = build_link_index(vault)

    assert index.resolve("Solo Name") == page


def test_unknown_target_resolves_to_none(vault: Path) -> None:
    """A genuinely dangling link must still be reported as unresolvable.

    The issue is explicit that the true dangling set (243 links, date-coded
    names like ``2025-09-20-w3``) has to survive the fix.
    """
    _page(vault, "02-Threads/Sourdough.md")
    index = build_link_index(vault)

    assert index.resolve("2025-09-20-w3") is None
    assert "2025-09-20-w3" not in index


def test_case_insensitive_fallback_matches_obsidian(vault: Path) -> None:
    """Obsidian resolves links case-insensitively; the index follows."""
    page = _page(vault, "02-Threads/2020-09-26-Messages.md", aliases=["Messages"])
    index = build_link_index(vault)

    assert index.resolve("messages") == page
    assert index.resolve("MESSAGES") == page


def test_exact_case_wins_over_a_differently_cased_page(vault: Path) -> None:
    """A case-sensitive hit is preferred over the folded fallback."""
    lower = _page(vault, "02-Threads/messages.md")
    _page(vault, "02-Threads/2020-09-26-Messages.md", aliases=["Messages"])
    index = build_link_index(vault)

    assert index.resolve("messages") == lower


def test_a_page_without_frontmatter_is_still_indexed_by_stem(vault: Path) -> None:
    """Plain markdown notes coexist with fragments and must stay resolvable."""
    path = vault / "02-Threads" / "Plain.md"
    path.write_text("Just a body, no header.\n", encoding="utf-8")
    index = build_link_index(vault)

    assert index.resolve("Plain") == path


def test_malformed_frontmatter_does_not_break_the_index(vault: Path) -> None:
    """One unparseable header must not cost the whole vault its index.

    Lint runs across every file in a 35k-file vault; aborting on the first
    bad YAML block would make the check useless exactly when it matters.
    """
    bad = vault / "02-Threads" / "Broken.md"
    bad.write_text("---\ntitle: [unclosed\n---\n\nBody.\n", encoding="utf-8")
    good = _page(vault, "02-Threads/Good.md", aliases=["Fine"])

    index = build_link_index(vault)

    assert index.resolve("Fine") == good
    assert index.resolve("Broken") == bad


def test_alias_like_text_in_the_body_is_not_indexed(vault: Path) -> None:
    """Only the YAML header counts — the body is never parsed as frontmatter.

    This is the observable consequence of the memory bound the issue
    requires ("stream frontmatter, don't load bodies"): a body that happens
    to contain ``aliases:`` must not contribute a name. A reader that
    slurped whole files and searched them would fail here.
    """
    _page(
        vault,
        "02-Threads/Real.md",
        aliases=["Genuine"],
        body="aliases:\n  - Ghost\ntitle: Phantom\n",
    )
    index = build_link_index(vault)

    assert index.resolve("Genuine") is not None
    assert index.resolve("Ghost") is None
    assert index.resolve("Phantom") is None


def test_index_spans_the_whole_vault_not_one_folder(vault: Path) -> None:
    """Links cross folders, so the index must too."""
    thread = _page(vault, "02-Threads/2020-01-01-t.md", aliases=["Thread Name"])
    eddy = _page(vault, "03-Eddies/2020-01-01-e.md", aliases=["Eddy Name"])
    index = build_link_index(vault)

    assert index.resolve("Thread Name") == thread
    assert index.resolve("Eddy Name") == eddy


def test_empty_vault_yields_an_empty_index(vault: Path) -> None:
    """A vault with no pages resolves nothing and does not raise."""
    index = build_link_index(vault)

    assert index.resolve("anything") is None
    assert "anything" not in index


# ---------------------------------------------------------------------------
# iter_link_sources — whose outbound links Creek surveys (#1344)
# ---------------------------------------------------------------------------
#
# ``broken-links`` surveyed ``01-Fragments`` alone while reporting a
# whole-vault verdict, and ``orphan-compiled`` counted inbound links from
# ``01-Fragments`` and ``10-Liminal`` alone. Widening either to the literal
# whole vault makes Creek read its own output back as content: three
# successive ``creek lint`` runs over a vault with ONE genuine broken link
# reported 1, then 2, then 3, because ``lint-<date>.md`` renders every finding
# as ``- `src` → `[[target]]` ``. This is the one definition of the surveyed
# set, and of the three prefixes it withholds.


def _relative(vault: Path, paths: list[Path]) -> list[str]:
    """Render *paths* as POSIX, vault-relative strings, order preserved."""
    return [path.relative_to(vault).as_posix() for path in paths]


def test_iter_link_sources_includes_every_content_directory(vault: Path) -> None:
    """Every folder a human or a generator writes links into is surveyed.

    ``00-Creek-Meta/Tag-Garden.md`` is in the list deliberately: it emits real
    ``[[fragment-id]]`` links, so carving out the whole of ``00-Creek-Meta``
    to escape the report feedback loop would be a weakening.
    """
    expected = [
        "00-Creek-Meta/Tag-Garden.md",
        "01-Fragments/Notes/f.md",
        "02-Threads/Active/t.md",
        "04-Praxis/Daily/p.md",
        "05-Wavelength/Observations/w.md",
        "07-Voice/Drafts/v.md",
        "08-Decisions/Active/d.md",
        "10-Liminal/Compost/l.md",
    ]
    for relpath in expected:
        _page(vault, relpath)

    assert _relative(vault, iter_link_sources(vault)) == expected


def test_iter_link_sources_excludes_processing_log(vault: Path) -> None:
    """``creek lint`` writes its report here; re-reading it doubles findings."""
    _page(vault, "01-Fragments/Notes/f.md")
    _page(vault, "00-Creek-Meta/Processing-Log/lint-2026-08-09.md")
    _page(vault, "00-Creek-Meta/Processing-Log/2026/archived-lint.md")

    assert _relative(vault, iter_link_sources(vault)) == ["01-Fragments/Notes/f.md"]


def test_iter_link_sources_excludes_state(vault: Path) -> None:
    """``creek state`` echoes findings into ``latest.md``; same feedback loop."""
    _page(vault, "01-Fragments/Notes/f.md")
    _page(vault, "00-Creek-Meta/State/latest.md")

    assert _relative(vault, iter_link_sources(vault)) == ["01-Fragments/Notes/f.md"]


def test_iter_link_sources_excludes_ontology(vault: Path) -> None:
    """The deployed spec documents wiki-link syntax rather than linking pages.

    Its ``[[note-name]]`` example is the only finding a whole-vault scan
    produces on a fresh 32-file ``creek init`` vault.
    """
    _page(vault, "01-Fragments/Notes/f.md")
    _page(vault, "00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md")

    assert _relative(vault, iter_link_sources(vault)) == ["01-Fragments/Notes/f.md"]


def test_iter_link_sources_is_sorted(vault: Path) -> None:
    """Deterministic order: two runs on one vault must report identically."""
    for relpath in (
        "03-Eddies/mmm.md",
        "02-Threads/zzz.md",
        "01-Fragments/aaa.md",
    ):
        _page(vault, relpath)

    assert _relative(vault, iter_link_sources(vault)) == [
        "01-Fragments/aaa.md",
        "02-Threads/zzz.md",
        "03-Eddies/mmm.md",
    ]


def test_iter_link_sources_on_a_missing_vault_returns_empty(tmp_path: Path) -> None:
    """A path that does not exist yields no sources and does not raise."""
    assert iter_link_sources(tmp_path / "no-such-vault") == []


def test_iter_link_sources_keeps_similarly_named_siblings(vault: Path) -> None:
    """The carve-out matches path components, not string prefixes.

    ``str(path).startswith("00-Creek-Meta/State")`` would swallow
    ``State-Machine-Notes.md`` and ``Processing-Logs-Archive.md`` — operator
    notes that live beside the excluded folders and carry real links.
    """
    _page(vault, "00-Creek-Meta/State-Machine-Notes.md")
    _page(vault, "00-Creek-Meta/Processing-Logs-Archive.md")
    _page(vault, "00-Creek-Meta/State/latest.md")
    _page(vault, "00-Creek-Meta/Processing-Log/lint-2026-08-09.md")

    assert _relative(vault, iter_link_sources(vault)) == [
        "00-Creek-Meta/Processing-Logs-Archive.md",
        "00-Creek-Meta/State-Machine-Notes.md",
    ]


# ---------------------------------------------------------------------------
# read_header_meta — the public, header-only frontmatter read (#1344)
# ---------------------------------------------------------------------------


def test_read_header_meta_returns_the_frontmatter_mapping(vault: Path) -> None:
    """The parsed header comes back whole, not just the names it declares."""
    page = _page(vault, "02-Threads/Meta.md", title="Long Walks", aliases=["Walks"])

    assert read_header_meta(page) == {"title": "Long Walks", "aliases": ["Walks"]}


def test_read_header_meta_returns_empty_without_frontmatter(vault: Path) -> None:
    """A bare markdown page declares nothing; it does not raise."""
    page = vault / "02-Threads" / "Plainer.md"
    page.write_text("Just a body, no header.\n", encoding="utf-8")

    assert read_header_meta(page) == {}


def test_read_header_meta_returns_empty_on_malformed_yaml(vault: Path) -> None:
    """One unparseable header must not cost a whole-vault walk its run."""
    page = vault / "02-Threads" / "Malformed.md"
    page.write_text("---\ntype: [unclosed\n---\n\nBody.\n", encoding="utf-8")

    assert read_header_meta(page) == {}


def test_read_header_meta_stops_at_the_closing_fence(vault: Path) -> None:
    """The body is never read, so a second fence in it cannot forge a type.

    This is the observable consequence of the header-only bound: a 35k-file
    vault must not pay to load every body just to learn a page's type.
    """
    page = vault / "02-Threads" / "Fenced.md"
    page.write_text(
        "---\ntype: thread\n---\n\nBody text.\n\n---\ntype: forged\n---\n",
        encoding="utf-8",
    )

    assert read_header_meta(page) == {"type": "thread"}


# ---------------------------------------------------------------------------
# read_header_meta — "malformed YAML" means every way YAML can be malformed
# ---------------------------------------------------------------------------
#
# ``yaml.safe_load`` does not funnel every failure through ``YAMLError``. Its
# constructors call ``int()`` and ``datetime.date()`` on the scanned text and
# let those raise ``ValueError`` straight through. Catching ``YAMLError``
# alone therefore kept the promise for a broken *fence* and broke it for a
# broken *value* — and because ``build_link_index`` reads the header of every
# file in the vault, one such file aborted the whole of ``creek lint`` and
# ``creek state``. Measured before the fix: ``ValueError: month must be in
# 1..12`` propagating out of ``build_link_index``.


def test_read_header_meta_returns_empty_on_an_oversized_integer(vault: Path) -> None:
    """A 5000-digit scalar exceeds CPython's int-parsing limit, and is not YAML's.

    Well inside the 64 KiB header cap, so no size guard catches it first:
    ``int()`` raises ``ValueError`` from inside PyYAML's own constructor.
    """
    page = vault / "02-Threads" / "Bigint.md"
    page.write_text(f"---\nn: {'1' * 5000}\n---\n\nBody.\n", encoding="utf-8")

    assert read_header_meta(page) == {}


def test_read_header_meta_returns_empty_on_an_impossible_date(vault: Path) -> None:
    """``created: 2020-13-45`` resolves as a timestamp, then fails to construct.

    ``created`` is written by the ingest parsers from export metadata nobody
    in this system authored, so an impossible date is a reachable input rather
    than a contrived one.
    """
    page = vault / "02-Threads" / "Baddate.md"
    page.write_text("---\ncreated: 2020-13-45\n---\n\nBody.\n", encoding="utf-8")

    assert read_header_meta(page) == {}


def test_build_link_index_survives_a_header_that_breaks_the_constructor(
    vault: Path,
) -> None:
    """One poisoned header must not cost the whole vault its index.

    This is the consequence that matters: the index is built once per lint or
    state run over every ``*.md`` in the vault, so a single unlucky file took
    down both commands entirely rather than costing itself its aliases.
    """
    (vault / "02-Threads" / "Poison.md").write_text(
        "---\ncreated: 2020-13-45\n---\n\nBody.\n",
        encoding="utf-8",
    )
    _page(vault, "02-Threads/Healthy.md", title="Long Walks")

    index = build_link_index(vault)

    assert index.resolve("Long Walks") == vault / "02-Threads" / "Healthy.md"
    assert index.resolve("Poison") == vault / "02-Threads" / "Poison.md"


def test_unsurveyed_prefixes_track_the_writers_own_constants() -> None:
    """The deny list must name the directories the writers actually write to.

    ``_UNSURVEYED_PREFIXES`` restates paths that ``creek lint`` and
    ``creek state`` define for themselves. If either writer's constant moves
    and this one does not, the guard silently stops matching, the report
    becomes a link source again, and the feedback loop reopens with the whole
    suite still green — the failure mode this test exists to make loud.
    """
    from creek.generate.state import _STATE_SUBPATH
    from creek.lint.runner import _PROCESSING_LOG
    from creek.vault.links import _UNSURVEYED_PREFIXES

    assert _PROCESSING_LOG in _UNSURVEYED_PREFIXES
    assert _STATE_SUBPATH in _UNSURVEYED_PREFIXES
