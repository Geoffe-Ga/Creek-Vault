"""Alias-aware resolution in the ``broken-links`` and ``orphan-compiled`` checks.

Regression tests for #887. Both checks resolved wiki-links against filename
stems alone, which stopped being correct at #730 when the eddy/thread linkers
began writing date-prefixed pages (``2020-09-26-Messages.md``) and putting the
human-readable name in ``aliases:``. Fragments link the alias form
(``[[Messages]]``), so on the demo vault:

* ``broken-links`` flagged 66,380 links, 65,879 of which (99.2%) resolved
  through some page's ``aliases`` or ``title``; only 243 were truly dangling;
* ``orphan-compiled`` flagged ``02-Threads/Dormant/2020-09-26-Messages.md``,
  a page with roughly 30,000 inbound ``[[Messages]]`` links.

A check that is wrong 99.2% of the time is worse than absent, because its
output still has to be read. These tests pin both directions: aliased links
resolve, and genuinely dangling ones are still reported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.lint.checks import broken_links, orphan_compiled

if TYPE_CHECKING:
    from pathlib import Path


def _write(vault: Path, relpath: str, text: str) -> Path:
    """Write *text* to *relpath* under *vault*, creating parents."""
    path = vault / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fragment(vault: Path, name: str, body: str) -> Path:
    """Write a fragment note carrying *body* in its content."""
    return _write(
        vault,
        f"01-Fragments/{name}.md",
        f"---\ntype: fragment\nid: {name}\ntitle: {name}\n---\n\n{body}\n",
    )


def _aliased_thread(vault: Path, filename: str, alias: str) -> Path:
    """Write a date-prefixed thread page whose human name lives in ``aliases``."""
    return _write(
        vault,
        f"02-Threads/{filename}.md",
        f"---\ntype: thread\naliases:\n  - {alias}\n---\n\nThread body.\n",
    )


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A vault with the fragment and compiled folders the checks walk."""
    for rel in ("01-Fragments", "02-Threads", "03-Eddies", "10-Liminal"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---- broken-links ----


def test_alias_link_is_not_reported_broken(vault: Path) -> None:
    """``[[Messages]]`` resolving via ``aliases`` is not a broken link."""
    _aliased_thread(vault, "2020-09-26-Messages", "Messages")
    _fragment(vault, "frag-1", "See [[Messages]] for the thread.")

    result = broken_links.run(vault)

    assert result.findings == []
    assert "0 broken link(s)" in result.summary


def test_title_link_is_not_reported_broken(vault: Path) -> None:
    """A link matching a page's frontmatter ``title`` also resolves."""
    _write(
        vault,
        "02-Threads/2023-03-30-x.md",
        "---\ntype: thread\ntitle: Long Walks\n---\n\nBody.\n",
    )
    _fragment(vault, "frag-2", "See [[Long Walks]].")

    result = broken_links.run(vault)

    assert result.findings == []


def test_genuinely_dangling_link_is_still_reported(vault: Path) -> None:
    """The true dangling set must survive the fix.

    The issue is explicit that 243 real stale targets — date-coded names
    like ``2025-09-20-w3`` — should keep being reported. A fix that made
    the check quiet by resolving everything would be the opposite failure.
    """
    _aliased_thread(vault, "2020-09-26-Messages", "Messages")
    _fragment(vault, "frag-3", "Live [[Messages]] and stale [[2025-09-20-w3]].")

    result = broken_links.run(vault)

    assert len(result.findings) == 1
    assert "2025-09-20-w3" in result.findings[0]
    assert "Messages" not in result.findings[0]


def test_stem_links_keep_resolving(vault: Path) -> None:
    """The pre-existing stem behaviour is not regressed by adding aliases."""
    _write(vault, "02-Threads/Sourdough.md", "---\ntype: thread\n---\n\nBody.\n")
    _fragment(vault, "frag-4", "See [[Sourdough]].")

    result = broken_links.run(vault)

    assert result.findings == []


# ---- orphan-compiled ----


def test_page_with_inbound_alias_links_is_not_orphaned(vault: Path) -> None:
    """The ``2020-09-26-Messages.md`` case: ~30k inbound links, called orphan."""
    _aliased_thread(vault, "2020-09-26-Messages", "Messages")
    _fragment(vault, "frag-5", "See [[Messages]].")

    result = orphan_compiled.run(vault)

    assert result.findings == []
    assert "0 orphan compiled page(s)" in result.summary


def test_page_with_inbound_title_links_is_not_orphaned(vault: Path) -> None:
    """Inbound links matching a page's ``title`` count as inbound too."""
    _write(
        vault,
        "02-Threads/2023-03-30-y.md",
        "---\ntype: thread\ntitle: Deep Work\n---\n\nBody.\n",
    )
    _fragment(vault, "frag-6", "See [[Deep Work]].")

    result = orphan_compiled.run(vault)

    assert result.findings == []


def test_page_with_no_inbound_links_is_still_orphaned(vault: Path) -> None:
    """A genuinely unreferenced compiled page is still surfaced."""
    _aliased_thread(vault, "2020-09-26-Lonely", "Lonely")
    _fragment(vault, "frag-7", "Links nowhere in particular.")

    result = orphan_compiled.run(vault)

    assert len(result.findings) == 1
    assert "2020-09-26-Lonely" in result.findings[0]


def test_orphan_check_does_not_credit_a_link_to_a_different_page(
    vault: Path,
) -> None:
    """Resolution must be per-page, not "some alias somewhere matched".

    Counting any resolvable inbound target as covering every compiled page
    would silence the check entirely — the same end state as the bug, just
    reached from the other side.
    """
    _aliased_thread(vault, "2020-09-26-Messages", "Messages")
    _aliased_thread(vault, "2021-01-01-Orphan", "Orphan Thread")
    _fragment(vault, "frag-8", "Only [[Messages]] is linked.")

    result = orphan_compiled.run(vault)

    assert len(result.findings) == 1
    assert "2021-01-01-Orphan" in result.findings[0]
