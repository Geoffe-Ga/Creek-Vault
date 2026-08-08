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

from creek.vault.links import build_link_index

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
