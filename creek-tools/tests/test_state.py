"""Tests for ``creek state`` audit-report generator (FEAT-006).

This file pins the FEAT-006 acceptance criteria for the ``creek state``
audit-report generator. The generator is a *view* over the compiled
vault layer — it never re-runs classification, linking, or compile.

Sections covered:

1. Vault summary (counts + frequency distribution)
2. Pre-LLM yield (latest line of ``run-summary.jsonl``)
3. Active eddies (top-N by ``fragment_count``)
4. Active threads (top-N by ``last_seen`` recency)
5. Surprising connections (synchronicities)
6. Hyperedges (praxis spanning multiple eddies)
7. Drift warnings (broken wiki-links + stale fragments)

Plus the document-level integration tests: section ordering, ``write()``
+ ``latest.md`` plumbing, idempotency, and the ``creek state`` CLI.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.cli import app
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.generate.state import (
    EMPTY_PLACEHOLDER,
    SECTION_ORDER,
    StateReportGenerator,
    _admit_praxis,
    _admitted_liminal_notes,
    _frequency_label,
    _load_latest_yield,
    _load_synchronicities,
    _load_typed_models,
    _read_fragment_files,
    _refresh_latest,
    _seed_as_prompt,
)
from creek.generate.state_budget import (
    SIZE_BUDGET_TOKENS,
    BudgetResult,
    check_budget,
    estimate_tokens,
)
from creek.generate.state_budget import main as state_budget_main
from creek.models import (
    Eddy,
    Frequency,
    Praxis,
    PraxisStatus,
    PraxisType,
    PrivacyTier,
)
from tests.conftest import CORRUPT_NOTE_SHAPES

if TYPE_CHECKING:
    from collections.abc import Callable


runner = CliRunner()


def _seed_dirs(vault: Path) -> None:
    """Create the canonical Creek vault folder layout under *vault*."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis/Daily",
        "10-Liminal/Synchronicities",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str = "Note",
    created: datetime | None = None,
    frequency: str = "F1",
    eddies: list[str] | None = None,
    body: str = "body",
    phase: str = "unclassified",
    mode: str = "unclassified",
    dosage: str = "unclassified",
    praxis_potential: str = "none",
    source_platform: str = "journal",
) -> Path:
    """Write a Creek fragment markdown file under ``01-Fragments/Notes``."""
    when = created or datetime(2026, 5, 1, tzinfo=UTC)
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": source_platform, "author": "self"},
        "frequency": {"primary": frequency, "secondary": []},
        "wavelength": {"phase": phase, "mode": mode, "dosage": dosage},
        "eddies": list(eddies or []),
        "praxis_potential": praxis_potential,
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _write_eddy(
    vault: Path,
    *,
    eddy_id: str,
    title: str,
    fragment_count: int = 1,
) -> Path:
    """Write a minimal Eddy markdown file under ``03-Eddies``."""
    metadata = {
        "type": "eddy",
        "id": eddy_id,
        "title": title,
        "formed": date(2026, 1, 1).isoformat(),
        "fragment_count": fragment_count,
        "threads": [],
    }
    # Use ``eddy_id`` as the filename to avoid breaking when titles contain
    # spaces, slashes, or other path-unfriendly characters.
    target = vault / "03-Eddies" / f"{eddy_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_thread(
    vault: Path,
    *,
    thread_id: str,
    title: str,
    last_seen: date | None = None,
    fragment_count: int = 1,
) -> Path:
    """Write a minimal Thread markdown file under ``02-Threads/Active``."""
    seen = last_seen or date(2026, 5, 1)
    metadata = {
        "type": "thread",
        "id": thread_id,
        "title": title,
        "status": "active",
        "first_seen": (seen - timedelta(days=14)).isoformat(),
        "last_seen": seen.isoformat(),
        "fragment_count": fragment_count,
    }
    # Use ``thread_id`` as the filename for the same reason as ``_write_eddy``:
    # titles can contain spaces or slashes that the filesystem rejects.
    target = vault / "02-Threads" / "Active" / f"{thread_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_praxis(
    vault: Path,
    *,
    praxis_id: str,
    title: str,
    derived_from: list[str],
) -> Path:
    """Write a Praxis markdown file under ``04-Praxis/Daily``."""
    metadata = {
        "type": "praxis",
        "id": praxis_id,
        "title": title,
        "praxis_type": "habit",
        "status": "proposed",
        "derived_from": list(derived_from),
    }
    target = vault / "04-Praxis" / "Daily" / f"{praxis_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_synchronicity(
    vault: Path,
    *,
    sync_id: str,
    frag_a: str,
    frag_b: str,
    similarity: float = 0.95,
    time_gap_days: int = 60,
) -> Path:
    """Write a Synchronicity markdown file under ``10-Liminal/Synchronicities``."""
    metadata = {
        "type": "synchronicity",
        "id": sync_id,
        "fragment_a_id": frag_a,
        "fragment_b_id": frag_b,
        "similarity": similarity,
        "time_gap_days": time_gap_days,
        "source_a": "journal",
        "source_b": "essay",
    }
    target = vault / "10-Liminal" / "Synchronicities" / f"{sync_id}.md"
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
    """Return a vault stocked with fragments, eddies, threads, praxis, syncs."""
    _seed_dirs(tmp_path)
    base = datetime(2026, 5, 1, tzinfo=UTC)
    _write_fragment(
        tmp_path,
        frag_id="frag-001",
        title="A",
        created=base,
        frequency="F1",
        eddies=["[[Receptivity Eddy]]"],
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-002",
        title="B",
        created=base + timedelta(days=2),
        frequency="F2",
        eddies=["[[Receptivity Eddy]]", "[[Innovation Eddy]]"],
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-003",
        title="C",
        created=base + timedelta(days=4),
        frequency="F1",
        eddies=["[[Innovation Eddy]]"],
    )
    # frag-004 (FEAT-025) makes Receptivity outweigh Innovation in leaf counts —
    # otherwise leaf-aware counting would tie and the sort would flip to
    # alphabetical order.
    _write_fragment(
        tmp_path,
        frag_id="frag-004",
        title="D",
        created=base + timedelta(days=6),
        frequency="F1",
        eddies=["[[Receptivity Eddy]]"],
    )
    _write_eddy(tmp_path, eddy_id="eddy-1", title="Receptivity Eddy", fragment_count=3)
    _write_eddy(tmp_path, eddy_id="eddy-2", title="Innovation Eddy", fragment_count=2)
    _write_thread(
        tmp_path,
        thread_id="thread-old",
        title="Old Thread",
        last_seen=date(2026, 1, 1),
        fragment_count=4,
    )
    _write_thread(
        tmp_path,
        thread_id="thread-new",
        title="New Thread",
        last_seen=date(2026, 5, 7),
        fragment_count=2,
    )
    _write_praxis(
        tmp_path,
        praxis_id="praxis-bridge",
        title="Bridge Practice",
        derived_from=["frag-001", "frag-002", "frag-003"],
    )
    _write_praxis(
        tmp_path,
        praxis_id="praxis-solo",
        title="Single-Eddy Practice",
        derived_from=["frag-001"],
    )
    _write_synchronicity(
        tmp_path,
        sync_id="sync-abc",
        frag_a="frag-001",
        frag_b="frag-003",
        similarity=0.92,
        time_gap_days=45,
    )
    _write_yield_jsonl(
        tmp_path,
        [
            {
                "run_id": "run-1",
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
    assert "Fragments: 4" in section
    assert "Eddies: 2" in section
    assert "Threads: 2" in section


def test_section_vault_summary_empty(empty_vault: Path) -> None:
    """An empty vault renders zero counts on every primitive (not the placeholder)."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_vault_summary()

    assert "Fragments: 0" in section
    assert "Eddies: 0" in section
    assert "Threads: 0" in section
    assert EMPTY_PLACEHOLDER not in section


def test_section_vault_summary_includes_frequency_distribution(
    populated_vault: Path,
) -> None:
    """The summary surfaces the per-frequency distribution under a header."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_vault_summary()

    assert "**Frequency distribution**" in section
    # Three F1 fragments and one F2 fragment in the populated fixture.
    assert "F1 (Agency/Survival): 3" in section
    assert "F2 (Receptivity/Kinship): 1" in section


def test_frequency_distribution_sorts_numerically_not_lexicographically(
    empty_vault: Path,
) -> None:
    """F10 sorts after F2 (and after F9), not between F1 and F2."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    _write_fragment(empty_vault, frag_id="frag-a", created=base, frequency="F1")
    _write_fragment(empty_vault, frag_id="frag-b", created=base, frequency="F2")
    _write_fragment(empty_vault, frag_id="frag-c", created=base, frequency="F10")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_vault_summary()

    f1_idx = section.index("F1 (")
    f2_idx = section.index("F2 (")
    f10_idx = section.index("F10 (")
    assert f1_idx < f2_idx < f10_idx


# ---------------------------------------------------------------------------
# Section: pre-LLM yield
# ---------------------------------------------------------------------------


def test_section_pre_llm_yield_reads_latest_run(populated_vault: Path) -> None:
    """The pre-LLM yield section reflects the most recent run-summary line.

    The populated fixture's only yield entry has ``no_llm: True``, so this
    test pins both the count rendering and the truthy ``--no-llm`` token.
    """
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_pre_llm_yield()

    assert section.startswith("## Pre-LLM yield")
    assert "Deterministic: 7" in section
    assert "Local-model: 5" in section
    assert "Residue: 2" in section
    assert "`--no-llm`: yes" in section


def test_section_pre_llm_yield_renders_no_llm_false(empty_vault: Path) -> None:
    """When the latest run is *not* ``--no-llm``, the renderer prints ``no``."""
    _write_yield_jsonl(
        empty_vault,
        [
            {
                "run_id": "run-pass3",
                "deterministic_classified": 4,
                "local_model_processed": 3,
                "residue": 1,
                "no_llm": False,
                "timestamp": "2026-05-01T00:00:00+00:00",
            },
        ],
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert "`--no-llm`: no" in section


def test_section_pre_llm_yield_empty(empty_vault: Path) -> None:
    """A missing yield log renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Section: active eddies (FEAT-006 PR 2)
# ---------------------------------------------------------------------------


def test_section_active_eddies_sorts_by_fragment_count(populated_vault: Path) -> None:
    """Eddies render in descending order by ``fragment_count``."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_active_eddies()

    assert section.startswith("## Active eddies")
    receptivity_idx = section.index("Receptivity Eddy")
    innovation_idx = section.index("Innovation Eddy")
    assert receptivity_idx < innovation_idx
    assert "3 fragment(s)" in section
    assert "2 fragment(s)" in section


def test_section_active_eddies_caps_at_ten(empty_vault: Path) -> None:
    """At most ten eddies appear in the section."""
    for index in range(15):
        _write_eddy(
            empty_vault,
            eddy_id=f"eddy-{index:02d}",
            title=f"Eddy {index:02d}",
            fragment_count=index + 1,
        )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    assert section.count("Eddy ") == 10
    # Top eddy (count 15) is present, smallest excluded.
    assert "Eddy 14" in section
    assert "Eddy 00" not in section


def test_section_active_eddies_empty(empty_vault: Path) -> None:
    """No eddies renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    assert section.startswith("## Active eddies")
    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Section: active threads
# ---------------------------------------------------------------------------


def test_section_active_threads_sorted_by_recency(populated_vault: Path) -> None:
    """Threads render most-recent first by ``last_seen``."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_active_threads()

    assert section.startswith("## Active threads")
    new_idx = section.index("New Thread")
    old_idx = section.index("Old Thread")
    assert new_idx < old_idx


def test_section_active_threads_caps_at_ten(empty_vault: Path) -> None:
    """At most ten threads appear in the section."""
    for index in range(15):
        _write_thread(
            empty_vault,
            thread_id=f"thread-{index:02d}",
            title=f"Thread {index:02d}",
            last_seen=date(2026, 1, 1) + timedelta(days=index),
        )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_threads()

    assert section.count("Thread ") == 10
    assert "Thread 14" in section
    assert "Thread 00" not in section


def test_section_active_threads_empty(empty_vault: Path) -> None:
    """No threads renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_threads()

    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Section: synchronicities
# ---------------------------------------------------------------------------


def test_section_synchronicities_lists_pairs(populated_vault: Path) -> None:
    """Synchronicities surface fragment IDs, similarity, and time gap."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_synchronicities()

    assert section.startswith("## Surprising connections")
    assert "frag-001" in section
    assert "frag-003" in section
    assert "0.92" in section
    assert "45" in section


def test_section_synchronicities_empty(empty_vault: Path) -> None:
    """No synchronicities renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_synchronicities()

    assert EMPTY_PLACEHOLDER in section


def test_section_synchronicities_skips_invalid(empty_vault: Path) -> None:
    """Synchronicity files with the wrong shape are silently skipped."""
    bad = empty_vault / "10-Liminal" / "Synchronicities" / "bad.md"
    bad.write_text(
        "---\ntype: synchronicity\nid: sync-bad\n---\nbody\n",
        encoding="utf-8",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_synchronicities()

    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Section: hyperedges (praxis spanning 2+ eddies)
# ---------------------------------------------------------------------------


def test_section_hyperedges_lists_multi_eddy_praxis(populated_vault: Path) -> None:
    """Praxis whose ``derived_from`` fragments span 2+ eddies is listed."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_hyperedges()

    assert section.startswith("## Hyperedges")
    assert "Bridge Practice" in section
    # Single-eddy praxis is filtered out — it is not a hyperedge.
    assert "Single-Eddy Practice" not in section
    assert "Receptivity Eddy" in section
    assert "Innovation Eddy" in section


def test_section_hyperedges_empty(empty_vault: Path) -> None:
    """No multi-eddy praxis renders the documented placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_hyperedges()

    assert EMPTY_PLACEHOLDER in section


# ---------------------------------------------------------------------------
# Section: drift warnings
# ---------------------------------------------------------------------------


def test_section_drift_warnings_reports_broken_links(empty_vault: Path) -> None:
    """Broken wiki-links inside fragments surface in the drift warnings."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    _write_fragment(
        empty_vault,
        frag_id="frag-link",
        title="Linker",
        created=base,
        body="See [[Nonexistent Target]] for context.",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_drift_warnings()

    assert section.startswith("## Drift warnings")
    assert "Nonexistent Target" in section


def test_section_drift_warnings_reports_stale_fragments(empty_vault: Path) -> None:
    """Old, link-isolated fragments appear under stale fragments."""
    long_ago = datetime.now(tz=UTC) - timedelta(days=400)
    _write_fragment(
        empty_vault,
        frag_id="frag-stale",
        title="Forgotten",
        created=long_ago,
        body="No links anywhere.",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_drift_warnings()

    assert "frag-stale" in section


def test_section_drift_warnings_empty(empty_vault: Path) -> None:
    """No broken links or stale fragments renders the placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_drift_warnings()

    assert EMPTY_PLACEHOLDER in section


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
# write() and latest.md
# ---------------------------------------------------------------------------


def test_write_creates_iso_week_file(populated_vault: Path) -> None:
    """``write`` emits ``<vault>/00-Creek-Meta/State/<iso-year>-W<week>.md``."""
    today = date(2026, 5, 9)  # ISO week 19 of 2026
    written = StateReportGenerator(
        vault_path=populated_vault,
        today=today,
    ).write()

    iso_year, iso_week, _ = today.isocalendar()
    expected = (
        populated_vault / "00-Creek-Meta" / "State" / f"{iso_year}-W{iso_week:02d}.md"
    )
    assert written == expected
    assert expected.exists()
    text = expected.read_text(encoding="utf-8")
    for header in SECTION_ORDER:
        assert header in text


def test_write_is_idempotent_within_same_week(populated_vault: Path) -> None:
    """Re-running in the same ISO week overwrites the existing file."""
    today = date(2026, 5, 9)
    generator = StateReportGenerator(vault_path=populated_vault, today=today)

    first = generator.write()
    generator.write()
    state_dir = populated_vault / "00-Creek-Meta" / "State"
    week_files = sorted(p.name for p in state_dir.glob("*.md") if p.name != "latest.md")
    assert week_files == [first.name]


def test_write_updates_latest_pointer(populated_vault: Path) -> None:
    """``latest.md`` is created (symlink or copy) and points at the new file."""
    today = date(2026, 5, 9)
    written = StateReportGenerator(
        vault_path=populated_vault,
        today=today,
    ).write()

    latest = populated_vault / "00-Creek-Meta" / "State" / "latest.md"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == written.read_text(encoding="utf-8")


def test_write_refreshes_latest_after_new_week(populated_vault: Path) -> None:
    """A second run in a new week refreshes ``latest.md`` to the newer file."""
    StateReportGenerator(
        vault_path=populated_vault,
        today=date(2026, 5, 2),
    ).write()
    second = StateReportGenerator(
        vault_path=populated_vault,
        today=date(2026, 5, 9),
    ).write()

    latest = populated_vault / "00-Creek-Meta" / "State" / "latest.md"
    assert latest.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_latest_md_falls_back_to_copy(
    populated_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``Path.symlink_to`` raises ``OSError`` the writer copies instead."""
    from pathlib import Path as _Path

    def _refuse_symlink(self: _Path, target: str | _Path) -> None:
        msg = "symlink refused (test)"
        raise OSError(msg)

    monkeypatch.setattr(_Path, "symlink_to", _refuse_symlink)

    written = StateReportGenerator(
        vault_path=populated_vault,
        today=date(2026, 5, 9),
    ).write()

    latest = populated_vault / "00-Creek-Meta" / "State" / "latest.md"
    assert latest.exists()
    assert not latest.is_symlink()
    assert latest.read_text(encoding="utf-8") == written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_state_writes_report(populated_vault: Path) -> None:
    """``creek state --vault <vault>`` produces the report and exits 0."""
    result = runner.invoke(app, ["state", "--vault", str(populated_vault)])

    assert result.exit_code == 0, result.output
    state_dir = populated_vault / "00-Creek-Meta" / "State"
    week_files = list(state_dir.glob("*.md"))
    assert any(p.name == "latest.md" for p in week_files)
    assert any(p.name != "latest.md" for p in week_files)


def test_cli_state_does_not_run_pipeline_passes(
    populated_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek state`` is a view: it never invokes classify/link/compile."""
    import creek.pipeline as pipeline_module

    def _explode(*_args: object, **_kwargs: object) -> None:
        msg = "creek state must not run the pipeline"
        raise AssertionError(msg)

    monkeypatch.setattr(pipeline_module.Pipeline, "run", _explode)

    result = runner.invoke(app, ["state", "--vault", str(populated_vault)])
    assert result.exit_code == 0, result.output


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


def test_section_pre_llm_yield_skips_corrupt_last_line(empty_vault: Path) -> None:
    """A bare non-JSON last line falls back to the empty-state placeholder.

    Distinct from ``test_pre_llm_yield_rejects_json_array_root``: that
    test pins the *valid-JSON-wrong-shape* branch; this one pins the
    truly corrupt input that hits ``json.JSONDecodeError`` directly.
    """
    log = empty_vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    log.write_text("not json at all\n", encoding="utf-8")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert EMPTY_PLACEHOLDER in section


def test_frequency_label_renders_known_aptitude_name() -> None:
    """Known frequencies render with their canonical APTITUDE name."""
    assert _frequency_label("F1") == "F1 (Agency/Survival)"


def test_frequency_label_handles_unknown_value() -> None:
    """An unrecognised frequency string round-trips unchanged."""
    assert _frequency_label("F-99") == "F-99"


def test_pre_llm_yield_rejects_json_array_root(empty_vault: Path) -> None:
    """A JSONL line whose root is a JSON array falls back to placeholder."""
    log = empty_vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    log.write_text("[1, 2, 3]\n", encoding="utf-8")

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_pre_llm_yield()

    assert EMPTY_PLACEHOLDER in section


def test_render_against_missing_vault_root(tmp_path: Path) -> None:
    """A vault path that does not exist on disk renders an empty report.

    Pins the contract for every loader's ``not exists`` short-circuit:
    the report degrades to zero counts and empty-state placeholders
    rather than raising.
    """
    missing = tmp_path / "no-such-vault"

    rendered = StateReportGenerator(vault_path=missing).render()

    assert "Fragments: 0" in rendered
    assert "Eddies: 0" in rendered
    assert "Threads: 0" in rendered
    for header in SECTION_ORDER:
        assert header in rendered


# ---------------------------------------------------------------------------
# FEAT-007: wavelength snapshot, suggested questions, liminal watch
# ---------------------------------------------------------------------------


def _write_liminal_unnamed(vault: Path, name: str, *, created: datetime) -> Path:
    """Write a minimal Unnamed fragment under ``10-Liminal/Unnamed``."""
    (vault / "10-Liminal" / "Unnamed").mkdir(parents=True, exist_ok=True)
    target = vault / "10-Liminal" / "Unnamed" / f"{name}.md"
    metadata = {
        "type": "fragment",
        "id": name,
        "title": name.replace("-", " ").title(),
        "created": created.isoformat(),
        "ingested": created.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "unclassified", "secondary": []},
    }
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="unnamed body", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_liminal_paradox(vault: Path, name: str, *, created: datetime) -> Path:
    """Write a minimal Paradox note under ``10-Liminal/Paradoxes``."""
    (vault / "10-Liminal" / "Paradoxes").mkdir(parents=True, exist_ok=True)
    target = vault / "10-Liminal" / "Paradoxes" / f"{name}.md"
    metadata = {
        "type": "paradox",
        "id": name,
        "title": name.replace("-", " ").title(),
        "created": created.isoformat(),
    }
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="held tension", **metadata)),
        encoding="utf-8",
    )
    return target


# ---- Wavelength snapshot ----


def test_section_wavelength_snapshot_has_header_and_first(empty_vault: Path) -> None:
    """The wavelength snapshot is the very first section header in render output.

    Pre-decided choice (FEAT-007): the snapshot interprets every other
    section, so it must precede ``## Vault summary``.
    """
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_wavelength_snapshot()
    assert section.startswith("## Wavelength snapshot")


def test_section_wavelength_snapshot_reports_dominant_phase_and_mode(
    empty_vault: Path,
) -> None:
    """A populated vault surfaces the dominant phase, mode, and dosage shares."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for index in range(3):
        _write_fragment(
            empty_vault,
            frag_id=f"frag-r{index}",
            created=base + timedelta(days=index),
            phase="rising",
            mode="express",
            dosage="medicine",
            frequency="F1",
        )
    _write_fragment(
        empty_vault,
        frag_id="frag-other",
        created=base + timedelta(days=3),
        phase="peaking",
        mode="inhabit",
        dosage="toxic",
        frequency="F2",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
        today=date(2026, 5, 7),
    ).section_wavelength_snapshot()

    assert "rising" in section.lower()
    assert "express" in section.lower()
    # Medicine share rendered as a percentage.
    assert "Medicine" in section
    assert "Toxic" in section


def test_section_wavelength_snapshot_empty_when_no_fragments(
    empty_vault: Path,
) -> None:
    """An empty vault renders the empty-state placeholder under the header."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_wavelength_snapshot()
    assert section.startswith("## Wavelength snapshot")
    assert EMPTY_PLACEHOLDER in section


# ---- Suggested questions (phase-aware) ----


def _seed_rising_fixture(vault: Path) -> None:
    """Populate a vault biased toward Rising-phase thread-terminus candidates."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Active thread with high fragment_count → terminus candidate.
    _write_thread(
        vault,
        thread_id="thread-rising",
        title="Rising essay topic",
        last_seen=date(2026, 5, 8),
        fragment_count=20,
    )
    # Companion fragments classified as Rising with explicit praxis potential.
    for index in range(2):
        _write_fragment(
            vault,
            frag_id=f"frag-rise-{index}",
            title=f"Rise {index}",
            created=base + timedelta(days=index),
            phase="rising",
            praxis_potential="explicit",
            frequency="F1",
        )


def _seed_bottoming_fixture(vault: Path) -> None:
    """Populate a vault biased toward Bottoming-Out compost/synchronicity prompts.

    ``privacy_tier`` is explicit because ``section_suggested_questions``
    routes through :func:`creek.generate.mining.phase_filtered_seeds`,
    which tier-filters its corpus. Since #876 an untiered fragment ranks
    as PERSONAL and arrives as a title-only summary, stripping the
    ``composting``/``synchronicity`` body keywords this fixture exists to
    surface. Tier policy is covered in the privacy-filter suite.
    """
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Liminal "Unnamed" fragment plus matching eddy → liminal-cross-eddy seed.
    (vault / "10-Liminal" / "Unnamed").mkdir(parents=True, exist_ok=True)
    unnamed_meta = {
        "type": "fragment",
        "id": "frag-unnamed",
        "title": "Compost stirring",
        "created": base.isoformat(),
        "ingested": base.isoformat(),
        "privacy_tier": "open",
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F4", "secondary": []},
    }
    unnamed = vault / "10-Liminal" / "Unnamed" / "frag-unnamed.md"
    unnamed.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content=(
                    "winter darkness composting receptivity quiet stillness "
                    "synchronicity grief rest"
                ),
                **unnamed_meta,
            ),
        ),
        encoding="utf-8",
    )
    _write_eddy(vault, eddy_id="eddy-compost", title="Compost", fragment_count=4)
    eddy = vault / "03-Eddies" / "eddy-compost.md"
    eddy_meta = {
        "type": "eddy",
        "id": "eddy-compost",
        "title": "Compost",
        "formed": date(2026, 1, 1).isoformat(),
        "fragment_count": 4,
        "threads": [],
        "description": (
            "winter darkness composting receptivity quiet stillness synchronicity "
            "grief rest"
        ),
    }
    eddy.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **eddy_meta)),
        encoding="utf-8",
    )


def test_section_suggested_questions_bottoming_surfaces_compost(
    empty_vault: Path,
) -> None:
    """Bottoming Out fixture surfaces liminal/compost prompts (not drafting prompts)."""
    _seed_bottoming_fixture(empty_vault)

    section = StateReportGenerator(
        vault_path=empty_vault,
        today=date(2026, 5, 7),
        current_phase="bottoming_out",
    ).section_suggested_questions()

    assert section.startswith("## Suggested questions")
    # The compost eddy or its name should appear as a prompt.
    assert "compost" in section.lower()
    # Drafting prompts (thread-terminus titles) should not appear in a
    # Bottoming Out report — the fixture has no thread-terminus seeds at
    # all, but the assertion pins the phase filter regardless.
    assert "Rising essay topic" not in section


def test_section_suggested_questions_rising_surfaces_drafting(
    empty_vault: Path,
) -> None:
    """Rising fixture surfaces drafting/mining prompts (not compost prompts)."""
    _seed_rising_fixture(empty_vault)

    section = StateReportGenerator(
        vault_path=empty_vault,
        today=date(2026, 5, 7),
        current_phase="rising",
    ).section_suggested_questions()

    assert section.startswith("## Suggested questions")
    assert "Rising essay topic" in section


def test_section_suggested_questions_empty(empty_vault: Path) -> None:
    """A vault with no seeds renders the empty-state placeholder."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_suggested_questions()
    assert section.startswith("## Suggested questions")
    assert EMPTY_PLACEHOLDER in section


def test_section_suggested_questions_caps_at_five(empty_vault: Path) -> None:
    """At most five prompts are listed (FEAT-007 pre-decided 4-5).

    Thread-terminus does not filter on fragment phase — a thread becomes
    a terminus candidate based on its own ``fragment_count`` and
    ``ACTIVE`` status. That's why this fixture writes 10 threads
    without any classified fragments and still expects 5 prompts.
    """
    # Generate plenty of thread-terminus candidates.
    for index in range(10):
        _write_thread(
            empty_vault,
            thread_id=f"thread-{index}",
            title=f"Active thread {index}",
            last_seen=date(2026, 5, 8),
            fragment_count=15 + index,
        )

    section = StateReportGenerator(
        vault_path=empty_vault,
        current_phase="rising",
    ).section_suggested_questions()

    bullet_count = sum(1 for line in section.splitlines() if line.startswith("- "))
    # Ten thread-terminus candidates feed five slots → all five fill;
    # tightened from ``1 <= bullet_count <= 5`` so a regression that
    # drops to zero seeds would now fail.
    assert bullet_count == 5


# ---- Liminal watch ----


def test_section_liminal_watch_surfaces_unnamed_and_paradoxes(
    empty_vault: Path,
) -> None:
    """Liminal watch surfaces fresh content from Unnamed and Paradoxes."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    _write_liminal_unnamed(empty_vault, "stirring-of-doubt", created=base)
    _write_liminal_paradox(empty_vault, "freedom-and-form", created=base)

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_liminal_watch()

    assert section.startswith("## Liminal Watch")
    # ``path.stem`` keeps the original hyphenated lowercase filename, so
    # a plain substring check is sufficient — no case-folding needed.
    assert "stirring-of-doubt" in section
    assert "freedom-and-form" in section


def test_section_liminal_watch_empty(empty_vault: Path) -> None:
    """Liminal watch renders the empty placeholder when nothing has surfaced."""
    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_liminal_watch()
    assert section.startswith("## Liminal Watch")
    assert EMPTY_PLACEHOLDER in section


def test_section_liminal_watch_excludes_synchronicities(
    empty_vault: Path,
) -> None:
    """Synchronicities live in their own section and must not double up here."""
    _write_synchronicity(
        empty_vault,
        sync_id="sync-lw",
        frag_a="frag-001",
        frag_b="frag-002",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_liminal_watch()

    assert "sync-lw" not in section


# ---- Section ordering & wavelength-first contract ----


def test_render_wavelength_snapshot_is_first(populated_vault: Path) -> None:
    """``## Wavelength snapshot`` precedes ``## Vault summary`` in render output."""
    rendered = StateReportGenerator(vault_path=populated_vault).render()
    wavelength_idx = rendered.index("## Wavelength snapshot")
    summary_idx = rendered.index("## Vault summary")
    assert wavelength_idx < summary_idx


def test_render_emits_feat007_section_order(populated_vault: Path) -> None:
    """The FEAT-007 documented section order is preserved end-to-end.

    Includes FEAT-008's ``## Lint summary`` as the final appendix so the
    assertion pins the entire eleven-section contract, not just the
    FEAT-007 subset.
    """
    rendered = StateReportGenerator(vault_path=populated_vault).render()
    ordered_headers = [
        "## Wavelength snapshot",
        "## Vault summary",
        "## Pre-LLM yield",
        "## Liminal Watch",
        "## Active eddies",
        "## Active threads",
        "## Surprising connections",
        "## Hyperedges",
        "## Drift warnings",
        "## Suggested questions",
        "## Lint summary",
    ]
    indices = [rendered.index(header) for header in ordered_headers]
    assert indices == sorted(indices)


# ---- Size-budget gate ----


def test_estimate_tokens_returns_positive_for_text() -> None:
    """``estimate_tokens`` is positive for non-empty text and zero for empty."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0


def test_check_budget_passes_for_small_file(tmp_path: Path) -> None:
    """A short ``latest.md`` passes the budget without complaint."""
    target = tmp_path / "latest.md"
    target.write_text("# Creek state\n\n## Vault summary\n\nshort.\n", encoding="utf-8")

    result = check_budget(target)

    assert isinstance(result, BudgetResult)
    assert result.ok is True
    assert result.tokens < SIZE_BUDGET_TOKENS


def test_check_budget_fails_when_file_exceeds_budget(tmp_path: Path) -> None:
    """A file over the size budget fails and names the section that grew."""
    big_payload = "lorem ipsum dolor sit amet " * 80_000  # well over 200KB
    target = tmp_path / "latest.md"
    target.write_text(
        "# Creek state\n\n"
        "## Wavelength snapshot\n\nshort.\n\n"
        "## Vault summary\n\nshort.\n\n"
        "## Hyperedges\n\n"
        f"{big_payload}\n",
        encoding="utf-8",
    )

    result = check_budget(target)

    assert result.ok is False
    assert result.tokens > SIZE_BUDGET_TOKENS
    # The failure message must surface the section that consumed the budget.
    assert "## Hyperedges" in result.largest_sections[0][0]


def test_check_budget_missing_file_passes(tmp_path: Path) -> None:
    """A missing ``latest.md`` is treated as a no-op (CI safety net)."""
    result = check_budget(tmp_path / "no-such.md")
    assert result.ok is True
    assert result.tokens == 0


def test_check_budget_message_is_not_a_cap_to_raise(tmp_path: Path) -> None:
    """The failure message frames the budget as a fragmentation signal."""
    big_payload = "x " * 200_000
    target = tmp_path / "latest.md"
    target.write_text(
        "## Vault summary\n\n" + big_payload,
        encoding="utf-8",
    )

    result = check_budget(target)

    assert result.ok is False
    # The exact wording matters: this gate exists to surface fragmentation,
    # not to be raised. The CLI summary should make that clear.
    assert "consolidate" in result.message.lower()


# ---- CLI: budget command surface ----


def test_cli_state_budget_passes_after_state_run(populated_vault: Path) -> None:
    """``creek state-budget`` exits 0 when ``latest.md`` is under budget."""
    StateReportGenerator(
        vault_path=populated_vault,
        today=date(2026, 5, 9),
    ).write()

    result = runner.invoke(app, ["state-budget", "--vault", str(populated_vault)])

    assert result.exit_code == 0, result.output


def test_state_budget_main_with_explicit_argv_under_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` returns 0 and prints the ok message to stdout (not stderr)."""
    target = tmp_path / "latest.md"
    target.write_text("# Creek state\n\n## Vault summary\n\nshort.\n", encoding="utf-8")

    assert state_budget_main([str(target)]) == 0
    captured = capsys.readouterr()
    assert "ok" in captured.out.lower()
    assert captured.err == ""


def test_state_budget_main_with_explicit_argv_over_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` returns 1 and writes the failure message to stderr.

    Pins the stderr-vs-stdout dispatch contract: a failing budget is
    surfaced on stderr so shells that pipe stdout into a CI summary or
    Slack post don't lose the actionable consolidation hint.
    """
    target = tmp_path / "latest.md"
    target.write_text(
        "## Vault summary\n\n" + ("lorem ipsum " * 80_000),
        encoding="utf-8",
    )

    assert state_budget_main([str(target)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # The over-budget message names the section that grew + the
    # consolidate-don't-raise hint; both live on stderr.
    assert "exceeds budget" in captured.err.lower()
    assert "consolidate" in captured.err.lower()


def test_state_budget_main_missing_argv_returns_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main([])`` prints a usage hint to stderr and returns 2."""
    assert state_budget_main([]) == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_budget_result_largest_sections_is_immutable() -> None:
    """``BudgetResult.largest_sections`` is a tuple — ``append`` must fail."""
    result = BudgetResult(ok=True, tokens=0, largest_sections=(("## A", 1),))
    with pytest.raises(AttributeError):
        result.largest_sections.append(("## B", 2))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FEAT-025: hierarchy-aware state
# ---------------------------------------------------------------------------


def _write_hier_fragment(
    vault: Path,
    *,
    frag_id: str,
    level: str,
    parent_id: str | None = None,
    child_ids: list[str] | None = None,
    eddies: list[str] | None = None,
    threads: list[str] | None = None,
    phase: str = "rising",
    mode: str = "express",
    dosage: str = "medicine",
) -> Path:
    """Write a fragment with explicit hierarchy fields for FEAT-025 tests."""
    when = datetime(2026, 5, 1, tzinfo=UTC)
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Title {frag_id}",
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "wavelength": {"phase": phase, "mode": mode, "dosage": dosage},
        "eddies": list(eddies or []),
        "threads": list(threads or []),
        "level": level,
        "parent_id": parent_id,
        "child_ids": list(child_ids or []),
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="body", **metadata)),
        encoding="utf-8",
    )
    return target


def test_active_eddies_counts_leaves_not_parents(empty_vault: Path) -> None:
    """A parent fragment with two children counts as 2 leaves, not 3."""
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-parent",
        level="document",
        child_ids=["frag-leaf-1", "frag-leaf-2"],
        eddies=["[[Hierarchical Eddy]]"],
    )
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-leaf-1",
        level="paragraph",
        parent_id="frag-parent",
        eddies=["[[Hierarchical Eddy]]"],
    )
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-leaf-2",
        level="paragraph",
        parent_id="frag-parent",
        eddies=["[[Hierarchical Eddy]]"],
    )
    _write_eddy(
        empty_vault,
        eddy_id="eddy-h",
        title="Hierarchical Eddy",
        fragment_count=99,
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    # 2 leaves link to the eddy. The stored fragment_count (99) is irrelevant
    # — state recounts at leaf granularity.
    assert "Hierarchical Eddy — 2 fragment(s)" in section


def test_active_eddies_falls_back_to_stored_when_no_fragments(
    empty_vault: Path,
) -> None:
    """When no fragments are loaded, stored fragment_count is the fallback."""
    _write_eddy(empty_vault, eddy_id="eddy-x", title="Eddy X", fragment_count=7)

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    assert "Eddy X — 7 fragment(s)" in section


def test_active_eddies_shows_zero_when_leaves_present_but_unlinked(
    empty_vault: Path,
) -> None:
    """An eddy with no leaves linking to it renders ``0``, not the stored count.

    Regression: an earlier draft used ``leaf_counts.get(title) or stored`` which
    conflated "zero leaves linked" with "no fragments loaded" and leaked a
    stale stored count into the report.
    """
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-other",
        level="document",
        eddies=["[[Some Other Eddy]]"],
    )
    _write_eddy(
        empty_vault,
        eddy_id="eddy-stale",
        title="Stale Eddy",
        fragment_count=42,
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    assert "Stale Eddy — 0 fragment(s)" in section
    assert "42" not in section


def test_active_eddies_annotates_level(populated_vault: Path) -> None:
    """Each list-style section opens with the level it counted from."""
    section = StateReportGenerator(
        vault_path=populated_vault,
    ).section_active_eddies()
    assert "_Counted at: leaves._" in section


def test_active_threads_counts_leaves_not_parents(empty_vault: Path) -> None:
    """Threads also recount by leaves when the hierarchy contains them."""
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-parent",
        level="document",
        child_ids=["frag-leaf"],
        threads=["[[Thread X]]"],
    )
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-leaf",
        level="paragraph",
        parent_id="frag-parent",
        threads=["[[Thread X]]"],
    )
    _write_thread(
        empty_vault,
        thread_id="thread-x",
        title="Thread X",
        last_seen=date(2026, 5, 1),
        fragment_count=99,
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_threads()

    # 1 leaf links to Thread X (the parent is excluded).
    assert "Thread X" in section
    assert "(1 fragment(s))" in section
    assert "_Counted at: leaves._" in section


def test_synchronicities_drop_non_leaf_endpoints(empty_vault: Path) -> None:
    """A sync between a parent and any other fragment is hidden in leaves view."""
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-parent",
        level="document",
        child_ids=["frag-leaf"],
    )
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-leaf",
        level="paragraph",
        parent_id="frag-parent",
    )
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-other",
        level="document",
    )
    # Sync between the parent and an unrelated leaf — endpoint is non-leaf.
    _write_synchronicity(
        empty_vault,
        sync_id="sync-bad",
        frag_a="frag-parent",
        frag_b="frag-other",
    )
    # Sync between two leaves — should survive.
    _write_synchronicity(
        empty_vault,
        sync_id="sync-good",
        frag_a="frag-leaf",
        frag_b="frag-other",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_synchronicities()

    assert "frag-leaf" in section
    assert "frag-other" in section
    # The non-leaf sync is filtered out.
    assert "frag-parent" not in section
    assert "_Counted at: leaves._" in section


def test_wavelength_snapshot_reads_at_documents(empty_vault: Path) -> None:
    """Wavelength snapshot uses document/session-level fragments only."""
    # Three sentence-level fragments tagged ``peaking``; should be ignored.
    for idx in range(3):
        _write_hier_fragment(
            empty_vault,
            frag_id=f"frag-sentence-{idx}",
            level="sentence",
            phase="peaking",
        )
    # One document-level fragment tagged ``rising``; this should dominate.
    _write_hier_fragment(
        empty_vault,
        frag_id="frag-doc",
        level="document",
        phase="rising",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
        today=date(2026, 5, 1),
    ).section_wavelength_snapshot()

    assert "_Counted at: documents._" in section
    # Only one fragment passes the documents filter.
    assert "Fragments observed: 1" in section
    # The dominant phase reflects the single document, not the sentences.
    assert "**rising**" in section


def test_flat_vault_regression(empty_vault: Path) -> None:
    """A vault with only flat ``document``-level fragments behaves as before."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    _write_fragment(
        empty_vault,
        frag_id="frag-flat-1",
        title="Flat one",
        created=base,
        frequency="F1",
        eddies=["[[Flat Eddy]]"],
    )
    _write_fragment(
        empty_vault,
        frag_id="frag-flat-2",
        title="Flat two",
        created=base + timedelta(days=1),
        frequency="F2",
        eddies=["[[Flat Eddy]]"],
    )
    _write_eddy(empty_vault, eddy_id="eddy-flat", title="Flat Eddy", fragment_count=2)

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_active_eddies()

    # Counts and ordering are unchanged: all fragments are leaves.
    assert "Flat Eddy — 2 fragment(s)" in section


def test_cli_state_budget_fails_when_over_budget(populated_vault: Path) -> None:
    """``creek state-budget`` exits non-zero when ``latest.md`` exceeds budget."""
    state_dir = populated_vault / "00-Creek-Meta" / "State"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "latest.md"
    big_payload = "lorem ipsum " * 100_000
    target.write_text(
        f"# Creek state\n\n## Vault summary\n\n{big_payload}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["state-budget", "--vault", str(populated_vault)])

    assert result.exit_code != 0
    assert "budget" in result.output.lower()


# ---------------------------------------------------------------------------
# #1344: the drift section stays fragment-scoped as the link survey widens
# ---------------------------------------------------------------------------


def test_section_drift_warnings_omits_non_fragment_sources(
    empty_vault: Path,
) -> None:
    """A broken link on a thread page must not reach the drift section.

    ``BrokenLinkScanner`` now surveys the whole vault minus Creek's own
    report folders (#1344), but ``## Drift warnings`` renders only sources in
    ``admitted_paths`` — which holds admitted *fragments*. #969 gates this
    section by source precisely so an above-ceiling path never lands in a
    committable artefact; widening the scanner must not widen the section.
    """
    base = datetime(2026, 4, 1, tzinfo=UTC)
    _write_fragment(
        empty_vault,
        frag_id="frag-link",
        title="Linker",
        created=base,
        body="See [[Missing Fragment Target]] for context.",
    )
    thread_meta = {
        "type": "thread",
        "id": "thread-link",
        "title": "Thread Link",
        "status": "active",
        "first_seen": date(2026, 4, 1).isoformat(),
        "last_seen": date(2026, 4, 15).isoformat(),
        "fragment_count": 1,
    }
    thread = empty_vault / "02-Threads" / "Active" / "thread-link.md"
    thread.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="See [[Nonexistent Thread Target]] for context.",
                **thread_meta,
            ),
        ),
        encoding="utf-8",
    )

    section = StateReportGenerator(
        vault_path=empty_vault,
    ).section_drift_warnings()

    assert section.startswith("## Drift warnings")
    assert "Missing Fragment Target" in section
    assert "Nonexistent Thread Target" not in section
    assert "thread-link" not in section


# ---------------------------------------------------------------------------
# Issue #1451: the state report's unwitnessed skip and fallback arms
# ---------------------------------------------------------------------------


class TestLoadTypedModelsStepsOverBadNotes:
    """``_load_typed_models`` skips bad notes and keeps the good ones.

    Every arm here is characterization — the loader already behaves
    correctly — so these tests go green the moment they are written and
    cannot be proved by watching them fail. They are therefore written to
    be killed by mutation instead: each asserts the **exact surviving
    ids** by list equality, with a positive control proving the loader
    returns the good notes when no poison is present. A loader that
    returned ``[]`` unconditionally passes every assertion weaker than
    that.
    """

    @staticmethod
    def _seed_good(vault: Path) -> list[str]:
        """Write two readable eddies and return their ids."""
        for eddy_id in ("eddy-good-a", "eddy-good-b"):
            _write_eddy(vault, eddy_id=eddy_id, title=f"Title {eddy_id}")
        return ["eddy-good-a", "eddy-good-b"]

    @staticmethod
    def _ids(vault: Path) -> list[str]:
        """Return the eddy ids ``_load_typed_models`` recovers, in order."""
        loaded = _load_typed_models(vault / "03-Eddies", type_tag="eddy", cls=Eddy)
        return [model.id for model in loaded]

    def test_positive_control_returns_both_eddies(self, empty_vault: Path) -> None:
        """With no poison present the loader returns both good records."""
        expected = self._seed_good(empty_vault)

        assert self._ids(empty_vault) == expected

    def test_absent_root_returns_empty(self, tmp_path: Path) -> None:
        """A vault with no eddy folder yields nothing rather than raising."""
        assert not (tmp_path / "03-Eddies").exists()

        assert self._ids(tmp_path) == []

    @pytest.mark.parametrize("shape", CORRUPT_NOTE_SHAPES)
    def test_skips_each_corrupt_shape(
        self,
        empty_vault: Path,
        shape: str,
        corrupt_note: Callable[[Path, str], Path],
    ) -> None:
        """An unreadable note is stepped over; both good eddies survive.

        The poison sorts between the two good notes, so a walk that
        aborted on it returns one id and is caught by the equality.
        """
        expected = self._seed_good(empty_vault)
        corrupt_note(empty_vault / "03-Eddies" / "eddy-good-aa.md", shape)

        assert self._ids(empty_vault) == expected

    def test_skips_foreign_type_note(self, empty_vault: Path) -> None:
        """A well-formed note of another ``type`` is skipped by the gate.

        The impostor is a *valid* thread record, so the ``type`` gate is
        the only thing that can reject it — a malformed note here would
        be caught one arm earlier and leave the gate untested.
        """
        expected = self._seed_good(empty_vault)
        (empty_vault / "03-Eddies" / "eddy-good-aa.md").write_text(
            "---\n"
            "type: thread\n"
            "id: thread-impostor\n"
            "title: Not an eddy\n"
            "status: active\n"
            "first_seen: '2026-01-01'\n"
            "last_seen: '2026-02-01'\n"
            "---\n"
            "impostor body\n",
            encoding="utf-8",
        )

        assert self._ids(empty_vault) == expected

    def test_skips_schema_invalid_note(self, empty_vault: Path) -> None:
        """A note that parses but fails ``Eddy.model_validate`` is skipped.

        Surgically invalid: the record carries ``type: eddy`` and every
        required field, with only ``formed`` broken, so it reaches
        ``model_validate`` and fails there rather than at some earlier
        arm.
        """
        expected = self._seed_good(empty_vault)
        (empty_vault / "03-Eddies" / "eddy-good-aa.md").write_text(
            "---\n"
            "type: eddy\n"
            "id: eddy-broken\n"
            "title: Unparseable formed date\n"
            # The one broken field; everything else is a valid Eddy.
            "formed: not-a-date\n"
            "fragment_count: 1\n"
            "threads: []\n"
            "---\n"
            "broken body\n",
            encoding="utf-8",
        )

        assert self._ids(empty_vault) == expected


class TestReadFragmentFilesSkipsSchemaInvalid:
    """``_read_fragment_files`` drops a fragment whose schema will not validate.

    The drop is fail-closed on purpose (a fragment nobody can load is a
    fragment nobody can vouch for), so the assertion is on the surviving
    ids rather than on the absence of a crash.
    """

    def test_positive_control_then_schema_invalid_is_dropped(
        self,
        empty_vault: Path,
        pydantic_invalid_note: Callable[[Path], Path],
    ) -> None:
        """The good fragment survives; the schema-invalid one does not."""
        _write_fragment(
            empty_vault,
            frag_id="frag-good",
            title="Readable",
            created=datetime(2026, 5, 1, tzinfo=UTC),
        )
        root = empty_vault / "01-Fragments"
        before = [f.id for _p, f, _raw in _read_fragment_files(root)]
        assert before == ["frag-good"]

        pydantic_invalid_note(root / "Notes" / "broken.md")

        after = [f.id for _p, f, _raw in _read_fragment_files(root)]
        assert after == ["frag-good"]


class TestAdmitPraxisRequiresEvidence:
    """``_admit_praxis`` excludes a praxis that names no source fragments.

    The empty-``derived_from`` case is not a corrupt-note arm at all: it
    is a privacy rule. A praxis with no evidence has no source tier that
    could vouch for its title, so admitting it would leak a title past a
    ceiling that never cleared it. That is why it gets its own test and
    its own assertion on *which* praxis survived.
    """

    def test_praxis_without_derived_from_is_excluded(self) -> None:
        """Only the evidence-carrying praxis is admitted, by id."""
        grounded = Praxis(
            id="prx-grounded",
            title="Grounded",
            praxis_type=PraxisType.HABIT,
            status=PraxisStatus.PROPOSED,
            derived_from=["frag-001"],
        )
        ungrounded = Praxis(
            id="prx-ungrounded",
            title="Ungrounded",
            praxis_type=PraxisType.HABIT,
            status=PraxisStatus.PROPOSED,
            derived_from=[],
        )

        admitted, tiers = _admit_praxis(
            [grounded, ungrounded],
            {"frag-001": PrivacyTier.OPEN},
        )

        assert [item.id for item in admitted] == ["prx-grounded"]
        assert tiers == [PrivacyTier.OPEN]


class TestAdmittedLiminalNotesStepsOverBadNotes:
    """``_admitted_liminal_notes`` skips an unreadable note in the watch folder.

    The liminal watch is the operator's hand-edited territory, so a
    single malformed note there is the likeliest way to lose the whole
    section. The assertion names the surviving stems.
    """

    @pytest.mark.parametrize("shape", CORRUPT_NOTE_SHAPES)
    def test_skips_each_corrupt_shape(
        self,
        empty_vault: Path,
        shape: str,
        corrupt_note: Callable[[Path, str], Path],
    ) -> None:
        """Both readable notes survive an unreadable sibling."""
        folder = empty_vault / "10-Liminal" / "Unnamed"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("note-a", "note-b"):
            _write_liminal_unnamed(
                empty_vault,
                name,
                created=datetime(2026, 5, 1, tzinfo=UTC),
            )
        corrupt_note(folder / "note-aa.md", shape)

        admitted = _admitted_liminal_notes(folder, PrivacyTierOverride.ALL)

        assert sorted(stem for stem, _tier in admitted) == ["note-a", "note-b"]

    def test_positive_control_returns_the_readable_notes(
        self,
        empty_vault: Path,
    ) -> None:
        """With no poison present both readable notes are admitted.

        Without this control the skip test above is vacuous: a loader
        returning ``[]`` for everything would pass it.
        """
        folder = empty_vault / "10-Liminal" / "Unnamed"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("note-a", "note-b"):
            _write_liminal_unnamed(
                empty_vault,
                name,
                created=datetime(2026, 5, 1, tzinfo=UTC),
            )

        admitted = _admitted_liminal_notes(folder, PrivacyTierOverride.ALL)

        assert sorted(stem for stem, _tier in admitted) == ["note-a", "note-b"]


class TestLoadSynchronicitiesTypeGate:
    """``_load_synchronicities`` ignores non-synchronicity notes in its folder.

    Issue #1451 asked for the ``post is None`` arm of this loader. **That
    arm is gone**: the loader now reads through
    :func:`creek.vault.links.read_header_meta` rather than ``_safe_post``
    (#1416), so an unreadable note yields an empty mapping and the crash
    the issue described is structurally impossible rather than handled.
    The genuinely uncovered arm the migration left behind is the ``type``
    gate below, which is what this pins.
    """

    def test_foreign_type_note_is_ignored(self, empty_vault: Path) -> None:
        """A non-synchronicity note in the folder does not become a row.

        The impostor is deliberately **synchronicity-shaped**: it carries
        a valid ``id``, both fragment ids, a float ``similarity`` and an
        int ``time_gap_days``, so every isinstance guard downstream would
        happily admit it. The ``type`` gate is therefore the only thing
        that can reject it. An impostor missing those fields — a bare
        paradox note, say — gets dropped by the isinstance guards instead
        and leaves the gate untested while the coverage line still turns
        green.
        """
        _write_synchronicity(
            empty_vault,
            sync_id="sync-good",
            frag_a="frag-001",
            frag_b="frag-002",
        )
        folder = empty_vault / "10-Liminal" / "Synchronicities"
        (folder / "not-a-sync.md").write_text(
            "---\n"
            # Everything a synchronicity row needs, under the wrong type.
            "type: resonance\n"
            "id: res-001\n"
            "fragment_a_id: frag-003\n"
            "fragment_b_id: frag-004\n"
            "similarity: 0.91\n"
            "time_gap_days: 12\n"
            "source_a: journal\n"
            "source_b: essay\n"
            "---\n"
            "impostor body\n",
            encoding="utf-8",
        )

        rows = _load_synchronicities(folder)

        assert [row.sync_id for row in rows] == ["sync-good"]


class TestLoadLatestYieldFailsSoft:
    """``_load_latest_yield`` returns ``None`` for every unusable log.

    Each arm returns the same ``None``, so the positive control is what
    stops the battery being satisfied by a reader that returned ``None``
    unconditionally.
    """

    def test_positive_control_returns_the_last_line(self, empty_vault: Path) -> None:
        """The newest JSONL row is the one returned."""
        log = _write_yield_jsonl(
            empty_vault,
            [{"run": "older", "n": 1}, {"run": "newest", "n": 2}],
        )

        assert _load_latest_yield(log) == {"run": "newest", "n": 2}

    def test_unreadable_log_returns_none(
        self,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``OSError`` on read degrades to "no yield", not a crash.

        The failure is injected by monkeypatching ``read_text`` rather
        than by ``chmod``: a permission bit is root- and
        filesystem-dependent and silently does nothing when the suite
        runs as root in a container.
        """
        log = _write_yield_jsonl(empty_vault, [{"run": "newest"}])
        real_read_text = Path.read_text

        def _boom(self: Path, *args: Any, **kwargs: Any) -> str:
            """Raise for the yield log only, delegating every other read."""
            if self == log:
                msg = "simulated IO failure"
                raise OSError(msg)
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom)

        assert _load_latest_yield(log) is None

    @pytest.mark.parametrize("raw", ["", "   \n", "\n\n \t \n"])
    def test_whitespace_only_log_returns_none(
        self,
        empty_vault: Path,
        raw: str,
    ) -> None:
        """A log holding only blank lines yields no summary rather than raising.

        Without the ``if not lines`` guard this indexes ``lines[-1]`` on
        an empty list and raises ``IndexError``, which is a crash rather
        than a missing section.
        """
        log = empty_vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
        log.write_text(raw, encoding="utf-8")

        assert _load_latest_yield(log) is None


class TestFrequencyLabelAcceptsEnumMembers:
    """``_frequency_label`` accepts a real :class:`Frequency` member.

    Scope stated honestly: the ``isinstance(freq_value, Frequency)``
    short-circuit this drives is **defensive, not load-bearing today**.
    Removing it falls through to ``Frequency(freq_value)``, and calling
    an enum with one of its own members returns that member — so the
    mutant is semantically identical and no behavioural test can kill it.
    The branch earns its place against a future in which
    :class:`Frequency` loses its :class:`enum.StrEnum` base, which is
    what the module comment says. What these tests pin is the contract
    (a member and its ``.value`` must label identically) and the
    previously unexecuted branch; the redundancy is reported as a finding
    rather than dressed up as a kill.
    """

    def test_enum_member_and_its_string_agree(self) -> None:
        """A member and its ``.value`` produce the identical label."""
        assert _frequency_label(Frequency.F1) == _frequency_label(Frequency.F1.value)

    def test_enum_member_renders_code_and_name(self) -> None:
        """The label carries both the code and the human-readable name."""
        label = _frequency_label(Frequency.F1)

        assert label.startswith("F1 (")
        assert label.endswith(")")


class TestSeedAsPrompt:
    """``_seed_as_prompt`` renders a seed with and without a brief description."""

    @staticmethod
    def _seed(brief: str) -> IdeaSeed:
        """Return an IdeaSeed carrying *brief* as its brief description."""
        return IdeaSeed(
            strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
            title="Naming what orbits",
            source_fragments=("frag-001",),
            threads=(),
            eddies=(),
            frequency_affinity=(Frequency.F1,),
            brief_description=brief,
            score=1.0,
        )

    def test_brief_description_is_appended(self) -> None:
        """A seed with a brief renders ``title — brief``."""
        assert _seed_as_prompt(self._seed("why now")) == "Naming what orbits — why now"

    def test_missing_brief_description_renders_title_alone(self) -> None:
        """A seed without a brief renders the bare title, with no dangling dash.

        The dash is the tell: the naive single-branch implementation
        emits ``"Naming what orbits — "`` and would pass any assertion
        that only checked the title was present.
        """
        rendered = _seed_as_prompt(self._seed(""))

        assert rendered == "Naming what orbits"
        assert "—" not in rendered


class TestRefreshLatestSurvivesUnlinkFailure:
    """``_refresh_latest`` warns but still republishes when unlink fails.

    The log level is the product here: the module comments say WARNING is
    deliberate because an unlink failure can mask a permissions problem
    and leave a dangling symlink. So the test asserts the level *and*
    that the operator still got a usable ``latest.md`` — the degradation,
    not just the message.
    """

    def test_unlink_oserror_warns_and_still_publishes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing unlink is reported at WARNING and does not abort."""
        state_dir = tmp_path / "State"
        state_dir.mkdir()
        target = state_dir / "2026-W18.md"
        target.write_text("# Week 18\n", encoding="utf-8")
        latest = state_dir / "latest.md"
        latest.write_text("stale\n", encoding="utf-8")
        real_unlink = Path.unlink

        def _boom(self: Path, *args: Any, **kwargs: Any) -> None:
            """Fail for ``latest.md`` only, delegating every other unlink."""
            if self == latest:
                msg = "simulated unlink failure"
                raise OSError(msg)
            real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom)

        with caplog.at_level(logging.WARNING, logger="creek.generate.state"):
            result = _refresh_latest(state_dir, target)

        assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == [
            "Could not unlink existing latest.md",
        ]
        assert result == latest
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "# Week 18\n"
