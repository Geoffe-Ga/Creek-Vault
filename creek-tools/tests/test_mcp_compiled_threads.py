"""``related_threads`` — the narrative currents an entry touches (#2127).

A thread page under ``02-Threads/{Active,Dormant,Resolved}/`` is compiled from
the fragments carrying a ``threads:`` wiki-link to it, exactly as an eddy page
is compiled from those carrying an ``eddies:`` one. So it is selected and
admitted by the same rule :mod:`creek_mcp.compiled_pages` applies to eddies:

    **Provenance authorizes; seeds only select.**

A thread is returned only when a seed fragment is one of its members (never
padded with unrelated pages), only when every member is within the caller's
ceiling, and only when the members on disk account for its
``fragment_count``. Its status comes from the subfolder it lives in, and its
body never crosses the wire: only ``{id, title, status}`` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.compiled_pages import (
    MAX_RELATED_THREADS,
    RelatedCompiled,
    related_compiled,
)
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

_OPEN: Final[str] = "frag-2127open0001"
_OTHER: Final[str] = "frag-2127open0002"
_UNRELATED: Final[str] = "frag-2127unrel001"
_THREAD_TITLE: Final[str] = "Leaving the firm"
_THREAD_ID: Final[str] = "thread-2127000001"
_BODY_MARKER: Final[str] = "zz-thread-body-2127-zz"
_NONE: Final[RelatedCompiled] = RelatedCompiled([], [], [])


def _vault(tmp_path: Path) -> Path:
    """Create an empty vault root."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _fragment(
    vault: Path,
    frag_id: str,
    *,
    tier: str | None = "open",
    threads: tuple[str, ...] = (),
) -> None:
    """Write a loadable fragment, optionally linked to *threads*.

    Args:
        vault: Vault root.
        frag_id: The fragment ``id``.
        tier: Its ``privacy_tier``; ``None`` omits the key (fails closed).
        threads: Thread titles it links to, written as ``[[title]]``.
    """
    directory = vault / "01-Fragments" / "Notes"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: fragment",
        f"id: {frag_id}",
        "title: A fragment",
        "source:",
        "  platform: journal",
    ]
    if tier is not None:
        lines.append(f"privacy_tier: {tier}")
    if threads:
        lines.append("threads:")
        lines.extend(f'  - "[[{title}]]"' for title in threads)
    lines.extend(["---", "", "Body text."])
    (directory / f"{frag_id}.md").write_text("\n".join(lines) + "\n")


def _thread(
    vault: Path,
    *,
    title: str = _THREAD_TITLE,
    thread_id: str | None = _THREAD_ID,
    folder: str = "Active",
    fragment_count: int | str = 1,
    page_type: str = "thread",
    filename: str | None = None,
) -> None:
    """Write a thread page under ``02-Threads/<folder>/``."""
    directory = vault / "02-Threads" / folder if folder else vault / "02-Threads"
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"type: {page_type}"]
    if thread_id is not None:
        lines.append(f"id: {thread_id}")
    lines.extend(
        [
            f"title: {title}",
            "status: active",
            f"fragment_count: {fragment_count}",
            "---",
            "",
            f"{_BODY_MARKER} the thread's own summary.",
        ]
    )
    (directory / (filename or f"{title}.md")).write_text("\n".join(lines) + "\n")


def _titles(rows: list[dict[str, Any]]) -> list[str]:
    """Return the ``title`` of each returned row."""
    return [str(row["title"]) for row in rows]


def test_a_seeded_thread_is_published_with_id_title_and_status(
    tmp_path: Path,
) -> None:
    """The control: an all-``open`` thread reaches even an ``open`` caller."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault)

    related = related_compiled([_OPEN], vault, TierCeiling.OPEN)

    assert related.threads == [
        {"id": _THREAD_ID, "title": _THREAD_TITLE, "status": "active"}
    ]


@pytest.mark.parametrize(
    ("folder", "status"),
    [("Active", "active"), ("Dormant", "dormant"), ("Resolved", "resolved")],
)
def test_status_comes_from_the_subfolder(
    tmp_path: Path, folder: str, status: str
) -> None:
    """The subfolder, not the front matter, names the thread's status."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault, folder=folder)

    related = related_compiled([_OPEN], vault, TierCeiling.OPEN)

    assert [row["status"] for row in related.threads] == [status]


def test_a_thread_outside_a_status_folder_is_never_named(tmp_path: Path) -> None:
    """A page at ``02-Threads/`` root has no status, so it is not published."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault, folder="")

    assert related_compiled([_OPEN], vault, TierCeiling.ALL).threads == []


def test_an_entry_touching_nothing_returns_empty_on_every_axis(
    tmp_path: Path,
) -> None:
    """Relevance-gated: an unrelated seed is never padded with other pages."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _fragment(vault, _UNRELATED)
    _thread(vault)

    assert related_compiled([_UNRELATED], vault, TierCeiling.ALL) == _NONE
    assert related_compiled([], vault, TierCeiling.ALL) == _NONE


@pytest.mark.parametrize(
    ("ceiling", "member_tier"),
    [
        ("open", "personal"),
        ("open", "intimate"),
        ("personal", "intimate"),
        ("open", None),
        ("personal", None),
    ],
    ids=[
        "personal@open",
        "intimate@open",
        "intimate@personal",
        "nokey@open",
        "nokey@personal",
    ],
)
def test_a_thread_with_an_above_ceiling_member_is_never_named(
    tmp_path: Path, ceiling: str, member_tier: str | None
) -> None:
    """One above-ceiling member withholds the thread, title and all."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _fragment(vault, _OTHER, tier=member_tier, threads=(_THREAD_TITLE,))
    _thread(vault, fragment_count=2)

    related = related_compiled([_OPEN], vault, TierCeiling(ceiling))

    assert related.threads == []


def test_an_intimate_thread_is_named_only_at_an_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """The gate is a rank comparison, not a blanket refusal of threads."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, tier="intimate", threads=(_THREAD_TITLE,))
    _thread(vault)

    assert related_compiled([_OPEN], vault, TierCeiling.PERSONAL).threads == []
    assert _titles(related_compiled([_OPEN], vault, TierCeiling.ALL).threads) == [
        _THREAD_TITLE
    ]


def test_a_member_missing_from_the_walk_makes_the_thread_opaque(
    tmp_path: Path,
) -> None:
    """``fragment_count`` above the enumerable members withholds the page."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault, fragment_count=2)

    assert related_compiled([_OPEN], vault, TierCeiling.ALL).threads == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_type": "eddy"},
        {"thread_id": None},
        {"thread_id": "''"},
        {"fragment_count": "many"},
        {"fragment_count": -1},
        {"fragment_count": "true"},
        {"title": "''"},
    ],
    ids=[
        "wrong-type",
        "no-id",
        "blank-id",
        "count-text",
        "count-neg",
        "count-bool",
        "blank-title",
    ],
)
def test_a_malformed_thread_page_is_never_named(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """A page whose header cannot be trusted is withheld, not guessed at."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault, filename="thread.md", **overrides)

    assert related_compiled([_OPEN], vault, TierCeiling.ALL).threads == []


def test_a_title_two_thread_pages_claim_is_dropped(tmp_path: Path) -> None:
    """An ambiguous title cannot be matched to the members that authorize it."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault, folder="Active", filename="a.md")
    _thread(vault, folder="Dormant", filename="b.md")

    assert related_compiled([_OPEN], vault, TierCeiling.ALL).threads == []


def test_threads_are_capped_and_ranked_by_seed_overlap_then_title(
    tmp_path: Path,
) -> None:
    """At most :data:`MAX_RELATED_THREADS`, most-overlapping first."""
    vault = _vault(tmp_path)
    titles = ["Thread C", "Thread B", "Thread A"]
    _fragment(vault, _OPEN, threads=tuple(titles))
    _fragment(vault, _OTHER, threads=("Thread C",))
    for index, title in enumerate(titles):
        count = 2 if title == "Thread C" else 1
        _thread(vault, title=title, thread_id=f"thread-{index}", fragment_count=count)

    related = related_compiled([_OPEN, _OTHER], vault, TierCeiling.ALL)

    assert MAX_RELATED_THREADS == 2
    assert _titles(related.threads) == ["Thread C", "Thread A"]


def test_no_thread_body_or_count_crosses_into_the_result(tmp_path: Path) -> None:
    """Only ``{id, title, status}``: never the page's own prose."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN, threads=(_THREAD_TITLE,))
    _thread(vault)

    related = related_compiled([_OPEN], vault, TierCeiling.ALL)

    assert set(related.threads[0]) == {"id", "title", "status"}
    assert _BODY_MARKER not in repr(related)
