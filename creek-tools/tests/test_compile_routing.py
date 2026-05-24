"""Tests for ``creek.generate.compile_routing`` (FEAT-004)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.compile.provenance import ProvenanceEntry
from creek.generate.compile_routing import (
    COMPILE_GAPS_RELPATH,
    CompiledPageIndex,
    empty_index,
    load_compiled_pages,
    log_compile_gap,
)
from creek.models import CompiledPage
from tests.factories.compiled import build_compiled_page, write_compiled_page

if TYPE_CHECKING:
    from pathlib import Path


def _write_compiled_page(
    target: Path,
    *,
    target_kind: str,
    target_id: str,
    title: str,
    fragment_ids: tuple[str, ...] = (),
    body: str = "# Body\n",
) -> None:
    """Persist a compiled-layer page at *target* via the shared factory."""
    page = build_compiled_page(
        target_kind=target_kind,
        target_id=target_id,
        title=title,
        body=body,
        fragment_ids=fragment_ids,
    )
    write_compiled_page(page, target)


class TestLoadCompiledPages:
    """``load_compiled_pages`` indexes by target_kind + target_id."""

    def test_loads_threads_eddies_and_frequencies(self, tmp_path: Path) -> None:
        """One page per kind round-trips via the index."""
        _write_compiled_page(
            tmp_path / "02-Threads" / "Active" / "thread-grief.md",
            target_kind="thread",
            target_id="thread-grief",
            title="Grief",
            fragment_ids=("frag-a",),
        )
        _write_compiled_page(
            tmp_path / "03-Eddies" / "eddy-rituals.md",
            target_kind="eddy",
            target_id="eddy-rituals",
            title="Rituals",
            fragment_ids=("frag-b",),
        )
        _write_compiled_page(
            tmp_path / "06-Frequencies" / "F3.md",
            target_kind="frequency_index",
            target_id="F3",
            title="F3 Self-Love / Power",
            fragment_ids=("frag-c",),
        )

        index = load_compiled_pages(tmp_path)

        assert index.thread("thread-grief") is not None
        assert index.eddy("eddy-rituals") is not None
        assert index.frequency_index("F3") is not None

    def test_missing_directories_yield_empty_index(self, tmp_path: Path) -> None:
        """Vaults without compiled subdirs return empty maps, not errors."""
        index = load_compiled_pages(tmp_path)
        assert index.threads == {}
        assert index.eddies == {}
        assert index.frequency_indexes == {}

    def test_skips_non_compiled_pages_in_compiled_dirs(self, tmp_path: Path) -> None:
        """Files without ``type: compiled_page`` are ignored."""
        target = tmp_path / "02-Threads" / "stray.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(content="not a compiled page", title="Stray")
        target.write_text(frontmatter.dumps(post), encoding="utf-8")

        index = load_compiled_pages(tmp_path)

        assert index.threads == {}

    def test_skips_malformed_yaml(self, tmp_path: Path) -> None:
        """A page with broken YAML frontmatter is skipped silently."""
        target = tmp_path / "02-Threads" / "bad.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unbalanced ``[`` inside the YAML block triggers ``yaml.YAMLError``
        # when ``frontmatter.load`` attempts to parse it.
        target.write_text("---\nkey: [\n---\nbody\n", encoding="utf-8")

        index = load_compiled_pages(tmp_path)

        assert index.threads == {}

    def test_fragment_ids_for_dedupes_provenance(self, tmp_path: Path) -> None:
        """Repeated fragment IDs across claims surface once, in order."""
        page = CompiledPage(
            target_kind="thread",
            target_id="t",
            title="t",
            body="x",
            provenance=[
                ProvenanceEntry(
                    claim_id="c1",
                    claim_excerpt="e1",
                    fragment_ids=["frag-a", "frag-b"],
                    compiled_at=datetime(2026, 4, 1, tzinfo=UTC),
                    compile_method="llm",
                ),
                ProvenanceEntry(
                    claim_id="c2",
                    claim_excerpt="e2",
                    fragment_ids=["frag-b", "frag-c"],
                    compiled_at=datetime(2026, 4, 1, tzinfo=UTC),
                    compile_method="llm",
                ),
            ],
        )
        index = empty_index()
        assert index.fragment_ids_for(page) == ("frag-a", "frag-b", "frag-c")


class TestBypassedIndex:
    """``empty_index(bypassed=True)`` reports every lookup as a miss."""

    def test_bypassed_index_returns_none_for_known_id(self) -> None:
        """Even when the dict has a page, bypass mode reports a miss."""
        page = CompiledPage(
            target_kind="thread",
            target_id="thread-grief",
            title="Grief",
            body="x",
        )
        index = CompiledPageIndex(threads={"thread-grief": page}, bypassed=True)
        assert index.thread("thread-grief") is None
        assert index.eddy("anything") is None
        assert index.frequency_index("F1") is None

    def test_non_bypassed_returns_page(self) -> None:
        """The same map without bypass returns the page."""
        page = CompiledPage(
            target_kind="thread",
            target_id="thread-grief",
            title="Grief",
            body="x",
        )
        index = CompiledPageIndex(threads={"thread-grief": page})
        assert index.thread("thread-grief") is page


class TestLogCompileGap:
    """``log_compile_gap`` appends to ``compile-gaps.jsonl``."""

    def test_appends_jsonl_entry(self, tmp_path: Path) -> None:
        """The log records target/reason/surfaced-by fields per call."""
        log_compile_gap(
            tmp_path,
            target_kind="thread",
            target_id="thread-x",
            surfaced_by="mine.thread_terminus",
            reason="missing",
        )
        log_compile_gap(
            tmp_path,
            target_kind="eddy",
            target_id="eddy-y",
            surfaced_by="draft",
            reason="missing",
        )

        log_file = tmp_path / COMPILE_GAPS_RELPATH
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["target_kind"] == "thread"
        assert first["target_id"] == "thread-x"
        assert first["surfaced_by"] == "mine.thread_terminus"
        assert first["reason"] == "missing"
        assert "timestamp" in first

    def test_creates_processing_log_dir(self, tmp_path: Path) -> None:
        """Parent dirs are created on demand."""
        assert not (tmp_path / "00-Creek-Meta").exists()
        log_compile_gap(
            tmp_path,
            target_kind="frequency_index",
            target_id="F5",
            surfaced_by="mine",
            reason="insufficient",
        )
        assert (tmp_path / COMPILE_GAPS_RELPATH).exists()
