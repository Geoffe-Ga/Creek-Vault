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
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.state import (
    EMPTY_PLACEHOLDER,
    SECTION_ORDER,
    StateReportGenerator,
    _frequency_label,
)

if TYPE_CHECKING:
    from pathlib import Path


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
        "eddies": list(eddies or []),
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
    _write_eddy(tmp_path, eddy_id="eddy-1", title="Receptivity Eddy", fragment_count=8)
    _write_eddy(tmp_path, eddy_id="eddy-2", title="Innovation Eddy", fragment_count=5)
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
    assert "Fragments: 3" in section
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
    # Two F1 fragments and one F2 fragment in the populated fixture.
    assert "F1 (Agency/Survival): 2" in section
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
    assert "8" in section
    assert "5" in section


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
