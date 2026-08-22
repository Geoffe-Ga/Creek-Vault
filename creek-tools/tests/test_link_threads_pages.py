"""Bulk thread-page materialisation via ``creek link --method threads`` (#718).

The threads linker detects narrative threads over the existing
:class:`~creek.link.threads.ThreadDetector` and writes one page per thread via
:meth:`~creek.vault.writer.VaultWriter.write_thread`, so 02-Threads is populated
in bulk instead of one ``creek compile`` per thread. These tests pin the
end-to-end command path, the fragment-link↔page relationship, and idempotency.

Embeddings are patched to an empty mapping so detection runs through the
deterministic frequency-overlap path (the shared conftest mock returns *random*
vectors, which would make the detector's cosine gate non-deterministic). The
detector's embedding-similarity semantics are covered separately in
``test_threads.py``; here the unit under test is page materialisation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig
from creek.link import link_engine
from creek.link.link_engine import run_link
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    SourcePlatform,
)
from creek.time import now_la
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _frequency_only_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Detect threads via frequency overlap alone (deterministic, no embeddings).

    Returns an empty embeddings map so :class:`ThreadDetector` takes its
    "no embedding -> rely on frequency overlap" branch rather than the cosine
    gate, which the conftest random-vector mock would make flaky.
    """
    monkeypatch.setattr(
        link_engine,
        "_load_or_compute_embeddings",
        lambda **_kwargs: link_engine.EmbeddingPass(
            vectors={},
            computed=0,
            reused=0,
            # #1337: no vectors were computed, so none were persisted either.
            persisted=0,
        ),
    )
    yield


def _thread_corpus(vault: Path) -> None:
    """Write three F5 fragments dated within the window so they form a thread."""
    # VaultWriter requires the meta + fragments scaffold to exist before it will
    # materialise pages; write_fragment_file creates 01-Fragments, so add the
    # other required dir. 02-Threads/{status}/ is created by the writer.
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    base = now_la() - timedelta(days=1)
    for i in range(3):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-thread0000{i:03d}",
                title=f"Thread member {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                authored_at=base - timedelta(days=i),
                frequency=FrequencyClassification(primary=Frequency.F5),
            ),
            body=f"Shared achievism theme, entry {i}.",
        )


def _thread_pages(vault: Path) -> list[Path]:
    """Return the thread markdown pages written under 02-Threads/."""
    return [
        p for p in (vault / "02-Threads").rglob("*.md") if not p.name.startswith(".")
    ]


def test_link_threads_writes_one_page_per_detected_thread(tmp_path: Path) -> None:
    """``run_link(method="threads")`` materialises a page for each thread."""
    vault = tmp_path / "vault"
    _thread_corpus(vault)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="threads",
        rebuild=False,
    )

    assert summary.method == "threads"
    assert summary.fragment_count == 3
    assert summary.threads_detected == 1
    assert summary.threads_written == summary.threads_detected
    pages = _thread_pages(vault)
    assert len(pages) == summary.threads_written
    # Pages route under a status subfolder (Active/Dormant/Resolved).
    assert all(p.parent.parent.name == "02-Threads" for p in pages)
    assert all(p.parent.name in {"Active", "Dormant", "Resolved"} for p in pages)


def test_fragment_thread_links_target_written_pages(tmp_path: Path) -> None:
    """Each fragment ``threads:`` wiki-link names a thread that now has a page.

    Resolution is asserted against each page's frontmatter ``title`` (which is
    exactly the wiki-link target), not the on-disk filename: pages are written
    with a ``{date}-{title}`` stem by the shared VaultWriter — the same
    behaviour the eddies path already has — so the bare ``[[title]]`` link does
    not byte-match the filename. Truly date-prefix-resolving Obsidian links is a
    documented follow-up, out of scope for "reuse the existing writer".
    """
    vault = tmp_path / "vault"
    _thread_corpus(vault)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="threads",
        rebuild=False,
    )
    assert summary.member_fragments_updated >= 1

    page_titles = {
        str(frontmatter.load(p).metadata.get("title", "")) for p in _thread_pages(vault)
    }
    # Every page also aliases its bare title, so a fragment's ``[[<title>]]``
    # link resolves to the ``{date}-<title>.md`` filename in stock Obsidian.
    page_aliases: set[str] = set()
    for page in _thread_pages(vault):
        page_aliases.update(
            str(a) for a in frontmatter.load(page).metadata.get("aliases", [])
        )

    linked_titles: set[str] = set()
    for frag_file in (vault / "01-Fragments").rglob("*.md"):
        post = frontmatter.load(frag_file)
        for link in post.metadata.get("threads", []):
            linked_titles.add(str(link).strip("[]"))

    assert linked_titles, "expected fragments to carry threads: wiki-links"
    assert linked_titles <= page_titles, (
        f"thread links {linked_titles - page_titles} have no page with that title"
    )
    assert linked_titles <= page_aliases, (
        f"thread links {linked_titles - page_aliases} do not resolve to a page alias"
    )


def test_link_threads_is_idempotent(tmp_path: Path) -> None:
    """Re-running the threads linker overwrites pages with identical bytes."""
    vault = tmp_path / "vault"
    _thread_corpus(vault)

    run_link(vault_path=vault, config=CreekConfig(), method="threads", rebuild=False)
    pages = sorted(_thread_pages(vault))
    snapshot = {p: p.read_bytes() for p in pages}
    assert snapshot, "expected thread pages to be written"

    second = run_link(
        vault_path=vault, config=CreekConfig(), method="threads", rebuild=False
    )
    after = sorted(_thread_pages(vault))
    assert after == pages, "re-run must not create or drop pages"
    assert second.threads_written == len(pages)
    for path, original in snapshot.items():
        assert path.read_bytes() == original, f"{path} changed on re-run"


def test_link_threads_no_detection_is_a_clean_noop(tmp_path: Path) -> None:
    """When no thread forms, the linker writes nothing and reports zeros.

    Three ``UNCLASSIFIED`` fragments share no frequency, so the detector's
    topic-consistency check never unions them — exercising the ``if not
    threads`` early-return branch in ``_run_threads``.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-loner00000{i:03d}",
                title=f"Loner {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                authored_at=now_la() - timedelta(days=i),
                # No frequency -> UNCLASSIFIED -> no topic overlap -> no thread.
            ),
            body=f"Unrelated note {i}.",
        )

    summary = run_link(
        vault_path=vault, config=CreekConfig(), method="threads", rebuild=False
    )

    assert summary.method == "threads"
    assert summary.fragment_count == 3
    assert summary.threads_detected == 0
    assert summary.threads_written == 0
    assert summary.member_fragments_updated == 0
    assert summary.link_count == 0
    assert _thread_pages(vault) == []


def test_link_threads_cli_reports_pages(tmp_path: Path) -> None:
    """``creek link --method threads`` exits 0 and reports written pages."""
    vault = tmp_path / "vault"
    _thread_corpus(vault)

    result = runner.invoke(app, ["link", "--method", "threads", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "Threads linker" in result.output
    assert "02-Threads" in result.output
    assert "page(s) written" in result.output
