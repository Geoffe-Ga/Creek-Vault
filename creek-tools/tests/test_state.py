"""Tests for ``creek state`` audit-report generator (FEAT-006, PR 1).

Covers the two sections implemented in this slice (vault summary and
pre-LLM yield), the seven-section ordering contract, and the
empty-state placeholder that every section must render. The remaining
five sections, the ``write()`` / ``latest.md`` plumbing, and the
``creek state`` CLI command are added in PR 2.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.generate.state import (
    EMPTY_PLACEHOLDER,
    SECTION_ORDER,
    StateReportGenerator,
    _frequency_label,
)


def _seed_dirs(vault: Path) -> None:
    """Create the canonical Creek vault folder layout under *vault*."""
    for sub in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str = "Note",
    created: datetime | None = None,
    frequency: str = "F1",
) -> Path:
    """Write a Creek fragment markdown file under ``01-Fragments/Notes``."""
    when = created or datetime(2026, 5, 1, tzinfo=UTC)
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": frequency, "secondary": []},
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="body", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_eddy(vault: Path, *, eddy_id: str, title: str) -> Path:
    """Write a minimal Eddy markdown file under ``03-Eddies``."""
    metadata = {
        "type": "eddy",
        "id": eddy_id,
        "title": title,
        "formed": date(2026, 1, 1).isoformat(),
        "fragment_count": 1,
        "threads": [],
    }
    target = vault / "03-Eddies" / f"{title}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_thread(vault: Path, *, thread_id: str, title: str) -> Path:
    """Write a minimal Thread markdown file under ``02-Threads/Active``."""
    last_seen = date(2026, 5, 1)
    metadata = {
        "type": "thread",
        "id": thread_id,
        "title": title,
        "status": "active",
        "first_seen": (last_seen - timedelta(days=14)).isoformat(),
        "last_seen": last_seen.isoformat(),
        "fragment_count": 1,
    }
    target = vault / "02-Threads" / "Active" / f"{title}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_yield_jsonl(vault: Path, lines: list[dict[str, object]]) -> Path:
    """Write the pre-LLM yield JSONL log with the given lines."""
    log = vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return log


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """Return a vault with the canonical folder layout and no content."""
    _seed_dirs(tmp_path)
    return tmp_path


@pytest.fixture
def populated_vault(tmp_path: Path) -> Path:
    """Return a vault with a small mix of fragments, eddies, threads, yield."""
    _seed_dirs(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=UTC)
    _write_fragment(
        tmp_path,
        frag_id="frag-001",
        title="A",
        created=base,
        frequency="F1",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-002",
        title="B",
        created=base + timedelta(days=2),
        frequency="F2",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-003",
        title="C",
        created=base + timedelta(days=4),
        frequency="F1",
    )
    _write_eddy(tmp_path, eddy_id="eddy-1", title="Innovation")
    _write_thread(tmp_path, thread_id="thread-1", title="Recursion")
    _write_yield_jsonl(
        tmp_path,
        [
            {
                "run_id": "run-1",
                "deterministic_classified": 4,
                "local_model_processed": 3,
                "residue": 1,
                "no_llm": False,
                "timestamp": "2026-05-01T00:00:00+00:00",
            },
            {
                "run_id": "run-2",
                "deterministic_classified": 7,
                "local_model_processed": 5,
                "residue": 2,
                "no_llm": True,
                "timestamp": "2026-05-08T12:00:00+00:00",
            },
        ],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Section: vault summary
# ---------------------------------------------------------------------------


def test_section_vault_summary_includes_counts(populated_vault: Path) -> None:
    """The vault summary lists fragment, eddy, and thread counts."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_vault_summary()

    assert section.startswith("## Vault summary")
    assert "Fragments: 3" in section
    assert "Eddies: 1" in section
    assert "Threads: 1" in section


def test_section_vault_summary_includes_frequency_distribution(
    populated_vault: Path,
) -> None:
    """The summary surfaces the per-frequency distribution under a header."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_vault_summary()

    assert "**Frequency distribution**" in section
    # Two F1 fragments, one F2 fragment.
    assert "F1" in section and "F2" in section
    assert ": 2" in section
    assert ": 1" in section


def test_section_vault_summary_empty(empty_vault: Path) -> None:
    """An empty vault renders zero counts (not the placeholder)."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_vault_summary()

    assert section.startswith("## Vault summary")
    assert "Fragments: 0" in section
    assert "Eddies: 0" in section
    assert "Threads: 0" in section
    assert EMPTY_PLACEHOLDER not in section


# ---------------------------------------------------------------------------
# Section: pre-LLM yield
# ---------------------------------------------------------------------------


def test_section_pre_llm_yield_reads_latest_run(populated_vault: Path) -> None:
    """The pre-LLM yield section reflects the most recent run-summary line."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_pre_llm_yield()

    assert section.startswith("## Pre-LLM yield")
    # Latest entry is run-2: deterministic 7, local-model 5, residue 2, no_llm true.
    assert "Deterministic: 7" in section
    assert "Local-model: 5" in section
    assert "Residue: 2" in section
    assert "run-2" in section
    assert "yes" in section


def test_section_pre_llm_yield_empty(empty_vault: Path) -> None:
    """A missing yield log renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert section.startswith("## Pre-LLM yield")
    assert EMPTY_PLACEHOLDER in section


def test_section_pre_llm_yield_skips_corrupt_last_line(empty_vault: Path) -> None:
    """An unparseable last JSONL line falls back to the empty placeholder."""
    log = empty_vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    log.write_text("not json at all\n", encoding="utf-8")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Deferred-section placeholders (PR 2 will fill these in)
# ---------------------------------------------------------------------------


def test_deferred_sections_render_empty_placeholder(empty_vault: Path) -> None:
    """Sections 3-7 emit the empty-state note in PR 1.

    PR 2 replaces these placeholders with real content; this test pins
    the empty-state contract so the section headers never silently
    disappear during the transition.
    """
    generator = StateReportGenerator(vault_path=empty_vault)
    deferred = (
        generator.section_active_eddies(),
        generator.section_active_threads(),
        generator.section_synchronicities(),
        generator.section_hyperedges(),
        generator.section_drift_warnings(),
    )
    for index, body in enumerate(deferred, start=2):
        assert body.startswith(SECTION_ORDER[index])
        assert EMPTY_PLACEHOLDER in body


# ---------------------------------------------------------------------------
# Render: section ordering
# ---------------------------------------------------------------------------


def test_render_includes_every_section_header(empty_vault: Path) -> None:
    """All seven section headers are present even when sections are empty."""
    rendered = StateReportGenerator(vault_path=empty_vault).render()
    for header in SECTION_ORDER:
        assert header in rendered


def test_render_emits_sections_in_documented_order(populated_vault: Path) -> None:
    """The seven sections appear in the order pinned by FEAT-006."""
    rendered = StateReportGenerator(vault_path=populated_vault).render()
    indices = [rendered.index(header) for header in SECTION_ORDER]
    assert indices == sorted(indices)


def test_render_starts_with_document_header(populated_vault: Path) -> None:
    """The rendered document starts with a ``# Creek state`` title."""
    rendered = StateReportGenerator(vault_path=populated_vault).render()
    assert rendered.startswith("# Creek state")


# ---------------------------------------------------------------------------
# Loader edge cases
# ---------------------------------------------------------------------------


def test_loader_skips_unparsable_yaml(empty_vault: Path) -> None:
    """A markdown file with broken YAML never aborts the audit view."""
    bad = empty_vault / "01-Fragments" / "Notes" / "broken.md"
    bad.write_text("---\nthis: : not yaml\n---\nbody\n", encoding="utf-8")
    _write_fragment(empty_vault, frag_id="frag-ok")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_vault_summary()

    assert "Fragments: 1" in section


def test_loader_skips_non_fragment_markdown(empty_vault: Path) -> None:
    """Plain notes coexist with fragments and are skipped silently."""
    note = empty_vault / "01-Fragments" / "Notes" / "note.md"
    note.write_text("---\ntitle: Plain note\n---\nbody\n", encoding="utf-8")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_vault_summary()

    assert "Fragments: 0" in section


def test_frequency_label_renders_known_aptitude_name() -> None:
    """Known frequencies render with their canonical APTITUDE name."""
    assert _frequency_label("F1") == "F1 (Agency/Survival)"


def test_frequency_label_handles_unknown_value() -> None:
    """An unrecognised frequency string round-trips unchanged."""
    assert _frequency_label("F-99") == "F-99"


def test_pre_llm_yield_rejects_json_array_root(empty_vault: Path) -> None:
    """A JSONL line whose root is a JSON array (not a dict) is rejected.

    ``_load_latest_yield`` only accepts dict-shaped payloads — anything
    else falls back to the empty-state placeholder, matching the type
    contract callers rely on.
    """
    log = empty_vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    log.write_text("[1, 2, 3]\n", encoding="utf-8")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert EMPTY_PLACEHOLDER in section


def test_render_against_missing_vault_root(tmp_path: Path) -> None:
    """A vault path that does not exist on disk renders an empty report.

    Each loader checks ``root.exists()`` before walking — the report
    must degrade to zero counts and empty-state placeholders rather
    than raising. Pins the contract explicitly rather than relying on
    every loader's individual ``not exists`` short-circuit.
    """
    missing = tmp_path / "no-such-vault"

    rendered = StateReportGenerator(vault_path=missing).render()

    assert "Fragments: 0" in rendered
    assert "Eddies: 0" in rendered
    assert "Threads: 0" in rendered
    for header in SECTION_ORDER:
        assert header in rendered
