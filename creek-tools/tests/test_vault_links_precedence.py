"""Name-resolution precedence in :func:`creek.vault.links.build_link_index` (#1224).

``build_link_index`` registers a page's filename stem and every name its
frontmatter declares in a *single* pass over ``sorted(rglob("*.md"))``::

    for md_file in sorted(vault_path.rglob("*.md")):
        for name in (md_file.stem, *_header_names(md_file)):
            by_name.setdefault(name, md_file)

Because stem and declared names are registered together, provenance is
invisible to the tie-break and the winner is whichever page happens to sort
first. A courtesy name a *different* page volunteered therefore outranks a
page's own identity, which is what an operator sees in the Obsidian sidebar
and what Obsidian itself resolves first.

The rule these tests pin is a strict four-level ladder — the first level with
a hit wins, and within a level the first page in sorted path order wins:

    1. exact-case FILENAME STEM
    2. exact-case DECLARED NAME (frontmatter ``title`` or an ``aliases`` entry)
    3. case-folded FILENAME STEM
    4. case-folded DECLARED NAME

Consequence worth stating plainly, and pinned by
:meth:`TestNamePrecedence.test_an_exact_case_alias_outranks_a_folded_stem`:
an exact-case foreign alias still beats a folded own stem. Obsidian's
exact-before-folded rule is preserved; the ladder only reorders *within* a
case-match quality.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import pytest

from creek.vault import links as links_module
from creek.vault.links import build_link_index

if TYPE_CHECKING:
    from pathlib import Path


def _page(
    vault: Path,
    relpath: str,
    *,
    title: str | None = None,
    aliases: tuple[str, ...] = (),
) -> Path:
    """Write a markdown page at *relpath* declaring *title* / *aliases*.

    Args:
        vault: Vault root.
        relpath: Vault-relative path of the page, e.g. ``"03-Eddies/M.md"``.
        title: Optional frontmatter ``title``.
        aliases: Optional frontmatter ``aliases`` entries.

    Returns:
        The path written.
    """
    target = vault / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    header = ["---"]
    if title is not None:
        header.append(f"title: {title}")
    if aliases:
        header.append("aliases:")
        header.extend(f"  - {alias}" for alias in aliases)
    header.extend(("---", "", "Body text.", ""))
    target.write_text("\n".join(header), encoding="utf-8")
    return target


class TestNamePrecedence:
    """The four-level ladder, one test per rung and per edge case."""

    def test_own_stem_beats_a_foreign_alias(self, tmp_path: Path) -> None:
        """#1224 core: a page named ``Messages.md`` wins over a foreign alias.

        ``01-Fragments/frag-a.md`` sorts before ``03-Eddies/Messages.md`` and
        volunteers ``Messages`` as an alias, so today's single-pass
        ``setdefault`` hands the name to the fragment. The eddy page *is*
        ``Messages``; the fragment merely mentions it.
        """
        _page(tmp_path, "01-Fragments/frag-a.md", aliases=("Messages",))
        eddy = _page(tmp_path, "03-Eddies/Messages.md")

        assert build_link_index(tmp_path).resolve("Messages") == eddy

    def test_own_stem_beats_a_foreign_title(self, tmp_path: Path) -> None:
        """``title`` is a declared name too, and loses to a stem the same way."""
        _page(tmp_path, "01-Fragments/frag-a.md", title="Messages")
        eddy = _page(tmp_path, "03-Eddies/Messages.md")

        assert build_link_index(tmp_path).resolve("Messages") == eddy

    def test_folded_lookup_prefers_the_stem_over_a_foreign_alias(
        self,
        tmp_path: Path,
    ) -> None:
        """The folded rung flips too — the fixture at tests/test_vault_links.py:132.

        ``02-Threads/2020-09-26-Messages.md`` sorts first (digits before
        letters) and aliases ``Messages``; ``02-Threads/messages.md`` holds the
        stem. ``resolve("messages")`` already returns the stem page today by
        luck of the exact-case rung, so the existing suite does **not** catch
        this. ``resolve("MESSAGES")`` — folded on both sides — is where the
        single-pass index hands the answer to the alias page.
        """
        _page(
            tmp_path,
            "02-Threads/2020-09-26-Messages.md",
            aliases=("Messages",),
        )
        lower = _page(tmp_path, "02-Threads/messages.md")

        assert build_link_index(tmp_path).resolve("MESSAGES") == lower

    def test_an_exact_case_alias_outranks_a_folded_stem(
        self,
        tmp_path: Path,
    ) -> None:
        """Level 2 beats level 3: the one case a foreign alias still shadows a stem.

        Deliberate, and the reason the ladder is four rungs rather than two:
        Obsidian resolves exact-case first regardless of provenance, and #1224
        must not quietly change that.
        """
        alias_page = _page(
            tmp_path,
            "02-Threads/2020-09-26-Messages.md",
            aliases=("Messages",),
        )
        _page(tmp_path, "02-Threads/messages.md")

        assert build_link_index(tmp_path).resolve("Messages") == alias_page

    def test_two_pages_sharing_a_stem_keep_sorted_path_order(
        self,
        tmp_path: Path,
    ) -> None:
        """Within a rung the first page in sorted path order still wins."""
        first = _page(tmp_path, "02-Threads/Messages.md")
        _page(tmp_path, "03-Eddies/Messages.md")

        assert build_link_index(tmp_path).resolve("Messages") == first

    def test_two_pages_declaring_one_alias_keep_sorted_path_order(
        self,
        tmp_path: Path,
    ) -> None:
        """Alias-vs-alias is unchanged by #1224: sorted path order decides."""
        first = _page(tmp_path, "01-Fragments/a.md", aliases=("Shared",))
        _page(tmp_path, "01-Fragments/b.md", aliases=("Shared",))

        assert build_link_index(tmp_path).resolve("Shared") == first

    def test_a_page_aliasing_its_own_stem_is_unaffected(
        self,
        tmp_path: Path,
    ) -> None:
        """Self-alias is a no-op: both rungs name the same page."""
        page = _page(tmp_path, "03-Eddies/Messages.md", aliases=("Messages",))

        assert build_link_index(tmp_path).resolve("Messages") == page

    @pytest.mark.parametrize("attempt", range(5))
    def test_resolution_is_stable_across_rebuilds(
        self,
        tmp_path: Path,
        attempt: int,
    ) -> None:
        """Two passes over one pre-sorted list stay filesystem-order independent.

        The fixture is rebuilt in a different creation order on each attempt;
        the answer must not move. ``rglob`` yields in directory order on some
        filesystems, so a fix that sorts once and iterates twice is safe while
        one that calls ``rglob`` twice is not.
        """
        root = tmp_path / f"vault-{attempt}"
        order = [
            ("01-Fragments/frag-a.md", ("Messages",)),
            ("03-Eddies/Messages.md", ()),
            ("02-Threads/other.md", ("Messages",)),
        ]
        for relpath, aliases in (
            order[attempt % len(order) :] + order[: attempt % len(order)]
        ):
            _page(root, relpath, aliases=aliases)

        assert build_link_index(root).resolve("Messages") == root / "03-Eddies" / (
            "Messages.md"
        )


class TestIndexBuildCost:
    """#1224 must not double the per-file header I/O that #1223 exists to halve."""

    def test_each_page_header_is_read_exactly_once_per_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One ``read_header_meta`` call per file, across the whole build.

        A two-pass implementation that re-derives ``_header_names`` on the
        second pass silently doubles the walk cost. The counter is installed on
        ``creek.vault.links.read_header_meta``: ``_header_names`` resolves that
        name through the module global, so this patch does take effect (unlike
        patching an importer's re-bound copy).
        """
        pages = [
            _page(tmp_path, "01-Fragments/frag-a.md", aliases=("Messages",)),
            _page(tmp_path, "02-Threads/t.md", title="A Thread"),
            _page(tmp_path, "03-Eddies/Messages.md"),
        ]
        calls: Counter[Path] = Counter()
        original = links_module.read_header_meta

        def _spy(path: Path) -> dict[str, object]:
            calls[path] += 1
            return original(path)

        monkeypatch.setattr(links_module, "read_header_meta", _spy)

        build_link_index(tmp_path)

        assert dict(calls) == dict.fromkeys(pages, 1)
