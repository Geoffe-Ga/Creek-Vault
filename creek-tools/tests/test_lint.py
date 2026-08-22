"""Tests for ``creek lint`` — unified vault hygiene (FEAT-008).

Pins the FEAT-008 acceptance criteria:

* ``creek lint`` CLI with ``--check`` and ``--since`` flags
* Five emergence reports reachable as ``--check`` values
* Three new deterministic checks: ``broken-links``, ``orphan-compiled``,
  ``skill-size``
* Non-negotiable rules ("never resolve, never auto-create, never delete")
  documented and verified by regression tests
* Lint output appends to the next ``creek state`` run
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.lint import (
    ALL_CHECKS,
    DETERMINISTIC_CHECKS,
    SEMANTIC_CHECKS,
    CheckResult,
    LintReport,
    LintRunner,
    parse_since,
)
from creek.lint.checks import (
    broken_links,
    orphan_compiled,
    skill_size_budget,
)
from creek.lint.checks import (
    compost as compost_check,
)
from creek.lint.checks import (
    paradox as paradox_check,
)
from creek.lint.checks import (
    synchronicity as synchronicity_check,
)
from creek.lint.checks import (
    tags as tags_check,
)
from creek.lint.checks import (
    unnamed as unnamed_check,
)

if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


# ---------------------------------------------------------------------------
# Vault fixture
# ---------------------------------------------------------------------------


def _seed_vault(vault: Path) -> None:
    """Create the canonical vault folder layout used by the lint tests."""
    for sub in (
        "00-Creek-Meta/Skills",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/State",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis",
        "10-Liminal/Paradoxes",
        "10-Liminal/Unnamed",
        "10-Liminal/Synchronicities",
        "10-Liminal/Compost",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str = "A note",
    body: str = "Body text",
    tags: list[str] | None = None,
    created: datetime | None = None,
    links: list[str] | None = None,
    primary_frequency: str = "F1",
) -> Path:
    """Write a minimal fragment markdown file with optional wiki-links."""
    when = created or datetime(2026, 5, 1, tzinfo=UTC)
    meta = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": primary_frequency, "secondary": []},
        "tags": tags or [],
    }
    link_text = "\n".join(f"[[{target}]]" for target in (links or []))
    full_body = body + ("\n" + link_text if link_text else "")
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=full_body, **meta)),
        encoding="utf-8",
    )
    return target


def _write_compiled_thread(
    vault: Path,
    *,
    thread_id: str,
    title: str = "Compiled thread",
) -> Path:
    """Write a compiled-layer Thread page (no inbound fragment links)."""
    meta = {
        "type": "thread",
        "id": thread_id,
        "title": title,
        "status": "active",
        "first_seen": "2026-01-01",
        "last_seen": "2026-05-01",
        "fragment_count": 1,
    }
    target = vault / "02-Threads" / "Active" / f"{thread_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **meta)),
        encoding="utf-8",
    )
    return target


def _write_skill(vault: Path, *, stem: str, lines: int) -> Path:
    """Write a schema skill markdown file with *lines* body lines."""
    body = "\n".join(f"word {i}" * 20 for i in range(lines))
    target = vault / "00-Creek-Meta" / "Skills" / f"{stem}.SKILL.md"
    target.write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")
    return target


def _write_paradox_pair_cross_level(vault: Path) -> None:
    """Write two fragments that share a thread but sit at different levels.

    The pair has the same dosage-rule trigger (medicine vs. toxic on a
    shared primary frequency) so the detector would emit a paradox if
    the cross-level guard were off. FEAT-025: with the default policy,
    the pair is skipped.
    """
    when = datetime(2026, 5, 1, tzinfo=UTC)
    common: dict[str, object] = {
        "type": "fragment",
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "threads": ["shared-thread"],
        "tags": [],
    }
    section = {
        **common,
        "id": "frag-section",
        "title": "Section-level take",
        "wavelength": {
            "phase": "rising",
            "mode": "express",
            "dosage": "medicine",
        },
        "level": "section",
    }
    sentence = {
        **common,
        "id": "frag-sentence",
        "title": "Sentence-level take",
        "wavelength": {
            "phase": "rising",
            "mode": "express",
            "dosage": "toxic",
        },
        "level": "sentence",
        "parent_id": "frag-section",
    }
    for record in (section, sentence):
        target = vault / "01-Fragments" / "Notes" / f"{record['id']}.md"
        target.write_text(
            frontmatter.dumps(frontmatter.Post(content="body", **record)),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestLintModuleContract:
    """Module docstring and registries are part of the FEAT-008 contract."""

    def test_check_registries_partition_all_checks(self) -> None:
        """ALL_CHECKS is the union of deterministic + semantic, no overlap."""
        assert set(ALL_CHECKS) == set(DETERMINISTIC_CHECKS) | set(SEMANTIC_CHECKS)
        assert set(DETERMINISTIC_CHECKS).isdisjoint(SEMANTIC_CHECKS)

    def test_all_eight_acceptance_check_names_reachable(self) -> None:
        """The eight check names called out by FEAT-008 are registered."""
        expected = {
            "paradox",
            "unnamed",
            "synchronicity",
            "compost",
            "tags",
            "broken-links",
            "orphan-compiled",
            "skill-size",
        }
        assert expected.issubset(set(ALL_CHECKS))

    def test_the_check_help_string_lists_every_check(self) -> None:
        """``creek lint --help`` names every registry entry, and no ghosts.

        The string is restated in ``cli.py`` rather than rendered from
        ``ALL_CHECKS``, because ``creek.lint`` is imported lazily inside the
        command bodies to keep CLI startup fast. Restating is what let it
        drift three checks behind the registry (#926 review); this is the
        check that makes the restatement safe.

        Asserted against the rendered help rather than the source string, so
        it pins what an operator actually reads.
        """
        from typer.testing import CliRunner

        from creek.cli import app

        rendered = CliRunner().invoke(app, ["lint", "--help"]).output
        # Typer wraps help text, so strip newlines before substring checks.
        flat = " ".join(rendered.split())

        missing = [name for name in ALL_CHECKS if name not in flat]
        assert missing == [], f"--check help omits: {missing}"

    def test_semantic_checks_match_pre_decided_choices(self) -> None:
        """Pre-decided: paradox, synchronicity, unnamed are semantic."""
        assert set(SEMANTIC_CHECKS) == {"paradox", "synchronicity", "unnamed"}

    def test_module_docstring_states_non_negotiables(self) -> None:
        """The non-negotiable rules are pinned in the module docstring."""
        import creek.lint as lint_pkg

        doc = (lint_pkg.__doc__ or "").lower()
        assert "never resolve" in doc, "must state: never resolve paradoxes"
        assert "never auto-create" in doc, "must state: never auto-create pages"
        assert "never delete" in doc, "must state: never delete orphan fragments"


# ---------------------------------------------------------------------------
# --since parser
# ---------------------------------------------------------------------------


class TestParseSince:
    """``--since`` accepts 7d, 1w, 1mo, 30d (per FEAT-008)."""

    @pytest.mark.parametrize(
        ("text", "expected_days"),
        [
            ("7d", 7),
            ("30d", 30),
            ("1w", 7),
            ("2w", 14),
            ("1mo", 30),
            ("3mo", 90),
        ],
    )
    def test_accepts_documented_durations(
        self,
        text: str,
        expected_days: int,
    ) -> None:
        """Each documented suffix maps to its day count."""
        ref = datetime(2026, 5, 10, tzinfo=UTC)
        cutoff = parse_since(text, now=ref)
        assert cutoff == ref - timedelta(days=expected_days)

    def test_rejects_garbage(self) -> None:
        """Unknown suffixes raise ValueError."""
        with pytest.raises(ValueError, match="duration"):
            parse_since("forever")


# ---------------------------------------------------------------------------
# Deterministic check: broken-links
# ---------------------------------------------------------------------------


class TestBrokenLinksCheck:
    """``broken-links`` wraps ``BrokenLinkScanner``."""

    def test_flags_broken_wikilink(self, tmp_path: Path) -> None:
        """A wiki-link to a missing target shows up as a finding."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-1", links=["does-not-exist"])
        result = broken_links.run(tmp_path)
        assert result.name == "broken-links"
        assert any("does-not-exist" in line for line in result.findings)
        assert "broken link" in result.summary

    def test_clean_vault_has_no_findings(self, tmp_path: Path) -> None:
        """A vault whose every wiki-link resolves produces zero findings."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-a")
        _write_fragment(tmp_path, frag_id="frag-b", links=["frag-a"])
        result = broken_links.run(tmp_path)
        assert result.findings == []


# ---------------------------------------------------------------------------
# Deterministic check: orphan-compiled
# ---------------------------------------------------------------------------


class TestOrphanCompiledCheck:
    """``orphan-compiled`` flags compiled pages with zero inbound links."""

    def test_flags_compiled_thread_with_no_inbound_links(
        self,
        tmp_path: Path,
    ) -> None:
        """A compiled thread that no fragment links to is an orphan."""
        _seed_vault(tmp_path)
        _write_compiled_thread(tmp_path, thread_id="lonely-thread")
        _write_fragment(tmp_path, frag_id="frag-a")  # does not link
        result = orphan_compiled.run(tmp_path)
        assert any("lonely-thread" in line for line in result.findings)

    def test_skips_compiled_page_with_inbound_link(self, tmp_path: Path) -> None:
        """A compiled thread that at least one fragment links to is OK."""
        _seed_vault(tmp_path)
        _write_compiled_thread(tmp_path, thread_id="linked-thread")
        _write_fragment(tmp_path, frag_id="frag-a", links=["linked-thread"])
        result = orphan_compiled.run(tmp_path)
        assert not any("linked-thread" in line for line in result.findings)

    def test_only_flags_compiled_pages_not_fragments(
        self,
        tmp_path: Path,
    ) -> None:
        """Orphan fragments are NOT flagged — only orphan compiled pages."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="lonely-fragment")
        result = orphan_compiled.run(tmp_path)
        assert not any("lonely-fragment" in line for line in result.findings)


# ---------------------------------------------------------------------------
# Deterministic check: skill-size
# ---------------------------------------------------------------------------


class TestSkillSizeCheck:
    """``skill-size`` enforces token budgets on schema-skill files."""

    def test_flags_skill_over_budget(self, tmp_path: Path) -> None:
        """A SKILL.md whose word count exceeds the budget is flagged."""
        _seed_vault(tmp_path)
        _write_skill(tmp_path, stem="bloated", lines=300)
        result = skill_size_budget.run(tmp_path)
        assert any("bloated" in line for line in result.findings)

    def test_passes_skill_under_budget(self, tmp_path: Path) -> None:
        """A small SKILL.md is not flagged."""
        _seed_vault(tmp_path)
        _write_skill(tmp_path, stem="lean", lines=10)
        result = skill_size_budget.run(tmp_path)
        assert not any("lean" in line for line in result.findings)

    def test_flags_oversized_agents_md(self, tmp_path: Path) -> None:
        """An ``AGENTS.md`` over budget shows up alongside SKILL files."""
        _seed_vault(tmp_path)
        big_body = " ".join(["word"] * 4000)
        (tmp_path / "AGENTS.md").write_text(big_body, encoding="utf-8")
        result = skill_size_budget.run(tmp_path)
        assert any("AGENTS.md" in line for line in result.findings)

    def test_handles_missing_skills_folder(self, tmp_path: Path) -> None:
        """No skills folder → check returns zero findings, no crash."""
        result = skill_size_budget.run(tmp_path)
        assert result.findings == []


# ---------------------------------------------------------------------------
# Semantic-check wrappers — they delegate without resolving anything
# ---------------------------------------------------------------------------


class TestSemanticWrappers:
    """Semantic check wrappers must not resolve, create, or delete."""

    def test_paradox_wrapper_returns_empty_on_empty_vault(
        self,
        tmp_path: Path,
    ) -> None:
        """Paradox wrapper on an empty vault produces no findings."""
        _seed_vault(tmp_path)
        result = paradox_check.run(tmp_path)
        assert result.name == "paradox"
        assert result.findings == []

    def test_paradox_wrapper_does_not_create_to_fix_queue(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression: lint never opens a "to-fix" queue (ADOPT-002 pin)."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-a")
        paradox_check.run(tmp_path)
        # No "to-fix" path should ever be created by lint
        assert not (tmp_path / "10-Liminal" / "Paradoxes" / "to-fix").exists()
        assert not (tmp_path / "to-fix").exists()

    def test_paradox_skips_unparseable_files(self, tmp_path: Path) -> None:
        """Garbage fragments are skipped rather than crashing."""
        _seed_vault(tmp_path)
        (tmp_path / "01-Fragments" / "Notes" / "broken.md").write_text(
            "not a fragment at all",
            encoding="utf-8",
        )
        result = paradox_check.run(tmp_path)
        assert result.findings == []

    def test_paradox_skips_cross_level_pairs_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        """FEAT-025: cross-level paradox candidates are filtered by default."""
        _seed_vault(tmp_path)
        _write_paradox_pair_cross_level(tmp_path)
        result = paradox_check.run(tmp_path)
        # The pair is at different levels, so no paradox is recorded.
        assert result.findings == []

    def test_paradox_includes_cross_level_pairs_when_configured(
        self,
        tmp_path: Path,
    ) -> None:
        """FEAT-025: ``lint.paradox_cross_level: true`` opts the pair back in."""
        _seed_vault(tmp_path)
        _write_paradox_pair_cross_level(tmp_path)
        config_path = tmp_path / "00-Creek-Meta" / "creek_config.yaml"
        config_path.write_text(
            "lint:\n  paradox_cross_level: true\n",
            encoding="utf-8",
        )
        result = paradox_check.run(tmp_path)
        assert len(result.findings) == 1

    def test_unnamed_reports_zero_for_empty_vault(self, tmp_path: Path) -> None:
        """A vault with no unclassified fragments reports zero, not an error."""
        # Note: not calling _seed_vault — nothing to surface.
        result = unnamed_check.run(tmp_path)
        assert result.findings == []
        assert "0 fragment(s)" in result.summary

    def test_unnamed_surfaces_unclassified_fragment_in_fragments_root(
        self,
        tmp_path: Path,
    ) -> None:
        """An ``unclassified`` fragment under ``01-Fragments/`` is surfaced."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="51b", primary_frequency="unclassified")
        result = unnamed_check.run(tmp_path)
        assert any("51b" in line for line in result.findings)
        assert "1 fragment(s)" in result.summary

    def test_unnamed_ignores_classified_fragments(
        self,
        tmp_path: Path,
    ) -> None:
        """A fragment with a real frequency is not surfaced as unnamed."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="named", primary_frequency="F1")
        result = unnamed_check.run(tmp_path)
        assert not any("named" in line for line in result.findings)
        assert "0 fragment(s)" in result.summary

    def test_unnamed_counts_fragments_in_unnamed_folder(
        self,
        tmp_path: Path,
    ) -> None:
        """Files physically under ``10-Liminal/Unnamed`` are still surfaced."""
        _seed_vault(tmp_path)
        (tmp_path / "10-Liminal" / "Unnamed" / "lonely.md").write_text(
            "---\nid: lonely\n---\nbody",
            encoding="utf-8",
        )
        (tmp_path / "10-Liminal" / "Unnamed" / "Digests").mkdir(exist_ok=True)
        (tmp_path / "10-Liminal" / "Unnamed" / "Digests" / "week.md").write_text(
            "digest",
            encoding="utf-8",
        )
        result = unnamed_check.run(tmp_path)
        assert any("lonely" in line for line in result.findings)
        assert not any("Digests" in line for line in result.findings)

    def test_unnamed_deduplicates_unclassified_fragment_in_unnamed_folder(
        self,
        tmp_path: Path,
    ) -> None:
        """An unclassified fragment living in Unnamed is reported once."""
        _seed_vault(tmp_path)
        unnamed_dir = tmp_path / "10-Liminal" / "Unnamed"
        meta = {
            "type": "fragment",
            "id": "drifting",
            "title": "A note",
            "created": "2026-05-01T00:00:00+00:00",
            "ingested": "2026-05-01T00:00:00+00:00",
            "source": {"platform": "journal", "author": "self"},
            "frequency": {"primary": "unclassified", "secondary": []},
            "tags": [],
        }
        (unnamed_dir / "drifting.md").write_text(
            frontmatter.dumps(frontmatter.Post(content="body", **meta)),
            encoding="utf-8",
        )
        result = unnamed_check.run(tmp_path)
        assert sum("drifting" in line for line in result.findings) == 1
        assert "1 fragment(s)" in result.summary

    def test_synchronicity_reads_recorded_notes(self, tmp_path: Path) -> None:
        """Synchronicity wrapper surfaces notes already on disk."""
        _seed_vault(tmp_path)
        note = tmp_path / "10-Liminal" / "Synchronicities" / "sync-1.md"
        note.write_text(
            frontmatter.dumps(
                frontmatter.Post(content="", type="synchronicity", id="sync-1"),
            ),
            encoding="utf-8",
        )
        result = synchronicity_check.run(tmp_path)
        assert any("sync-1" in line for line in result.findings)

    def test_synchronicity_ignores_non_synchronicity_files(
        self,
        tmp_path: Path,
    ) -> None:
        """Files without ``type: synchronicity`` are not counted."""
        _seed_vault(tmp_path)
        note = tmp_path / "10-Liminal" / "Synchronicities" / "stray.md"
        note.write_text(
            frontmatter.dumps(frontmatter.Post(content="", type="other")),
            encoding="utf-8",
        )
        result = synchronicity_check.run(tmp_path)
        assert result.findings == []

    def test_compost_reads_recorded_notes(self, tmp_path: Path) -> None:
        """Compost wrapper surfaces real compost notes."""
        _seed_vault(tmp_path)
        note = tmp_path / "10-Liminal" / "Compost" / "old-project.md"
        note.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    content="",
                    type="compost",
                    title="Old project",
                ),
            ),
            encoding="utf-8",
        )
        (tmp_path / "10-Liminal" / "Compost" / "_Compost-Report.md").write_text(
            "rollup",
            encoding="utf-8",
        )
        result = compost_check.run(tmp_path)
        assert any("Old project" in line for line in result.findings)

    def test_compost_ignores_non_compost_files(self, tmp_path: Path) -> None:
        """Files without ``type: compost`` are not counted."""
        _seed_vault(tmp_path)
        note = tmp_path / "10-Liminal" / "Compost" / "stray.md"
        note.write_text(
            frontmatter.dumps(frontmatter.Post(content="", type="other")),
            encoding="utf-8",
        )
        result = compost_check.run(tmp_path)
        assert result.findings == []

    def test_tags_wrapper_finds_orphan_tag(self, tmp_path: Path) -> None:
        """Tags wrapper surfaces single-use tags as orphans."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-1", tags=["only-once"])
        result = tags_check.run(tmp_path)
        assert any("only-once" in line for line in result.findings)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestLintRunner:
    """``LintRunner`` dispatches and renders consolidated markdown."""

    def test_default_runs_only_deterministic_checks(self, tmp_path: Path) -> None:
        """No flags → all deterministic checks, no semantic checks."""
        _seed_vault(tmp_path)
        report = LintRunner(vault_path=tmp_path).run()
        names = {r.name for r in report.results}
        assert names == set(DETERMINISTIC_CHECKS)

    def test_since_flag_pulls_in_semantic_checks(self, tmp_path: Path) -> None:
        """``--since`` flips semantic checks on."""
        _seed_vault(tmp_path)
        report = LintRunner(
            vault_path=tmp_path,
            since=datetime(2026, 5, 1, tzinfo=UTC),
        ).run()
        names = {r.name for r in report.results}
        assert set(SEMANTIC_CHECKS).issubset(names)

    def test_explicit_check_overrides_default(self, tmp_path: Path) -> None:
        """An explicit ``--check paradox`` runs only the paradox check."""
        _seed_vault(tmp_path)
        report = LintRunner(vault_path=tmp_path).run(checks=["paradox"])
        names = {r.name for r in report.results}
        assert names == {"paradox"}

    def test_multiple_explicit_checks(self, tmp_path: Path) -> None:
        """Multiple ``--check`` flags run those checks (in registry order)."""
        _seed_vault(tmp_path)
        report = LintRunner(vault_path=tmp_path).run(
            checks=["broken-links", "tags"],
        )
        names = {r.name for r in report.results}
        assert names == {"broken-links", "tags"}

    def test_unknown_check_raises(self, tmp_path: Path) -> None:
        """An unrecognised ``--check`` is a hard error."""
        _seed_vault(tmp_path)
        with pytest.raises(ValueError, match="unknown check"):
            LintRunner(vault_path=tmp_path).run(checks=["not-a-check"])

    def test_write_produces_processing_log_file(self, tmp_path: Path) -> None:
        """``LintRunner.write`` lands a per-run markdown file under the log dir."""
        _seed_vault(tmp_path)
        runner_ = LintRunner(vault_path=tmp_path, today=date(2026, 5, 10))
        report = runner_.run()
        written = runner_.write(report)
        assert written.parent == tmp_path / "00-Creek-Meta" / "Processing-Log"
        assert written.name == "lint-2026-05-10.md"
        assert "# Creek lint report" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regression: non-negotiable behaviours
# ---------------------------------------------------------------------------


class TestNonNegotiables:
    """The "never resolve / never create / never delete" rules."""

    def test_lint_never_creates_compiled_pages(self, tmp_path: Path) -> None:
        """Even when orphan-compiled fires, lint never writes compiled pages."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-1")
        threads_before = list((tmp_path / "02-Threads" / "Active").glob("*.md"))
        eddies_before = list((tmp_path / "03-Eddies").glob("*.md"))

        LintRunner(vault_path=tmp_path).run()

        threads_after = list((tmp_path / "02-Threads" / "Active").glob("*.md"))
        eddies_after = list((tmp_path / "03-Eddies").glob("*.md"))
        assert threads_after == threads_before
        assert eddies_after == eddies_before

    def test_lint_never_deletes_orphan_fragments(self, tmp_path: Path) -> None:
        """An orphan fragment is preserved on disk after lint runs."""
        _seed_vault(tmp_path)
        frag_path = _write_fragment(tmp_path, frag_id="orphan-frag")
        LintRunner(vault_path=tmp_path).run()
        assert frag_path.exists()

    def test_paradox_routes_to_liminal_not_to_fix(self, tmp_path: Path) -> None:
        """Regression (ADOPT-002 pin): paradox findings reference Liminal."""
        _seed_vault(tmp_path)
        result = paradox_check.run(tmp_path)
        assert result.summary
        # The summary must mention the routing destination, not "to-fix".
        assert "10-Liminal/Paradoxes" in result.summary
        assert "to-fix" not in result.summary.lower()


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


class TestLintReport:
    """``LintReport.render`` produces deterministic markdown."""

    def test_render_includes_each_section_header(self, tmp_path: Path) -> None:
        """Each result's section appears in the rendered report."""
        report = LintReport(
            results=[
                CheckResult(
                    name="broken-links",
                    summary="0 broken links",
                    findings=[],
                ),
                CheckResult(
                    name="orphan-compiled",
                    summary="1 orphan compiled page",
                    findings=["- `02-Threads/Active/lonely.md`"],
                ),
            ],
            since=None,
            today=date(2026, 5, 10),
        )
        text = report.render()
        assert "# Creek lint report — 2026-05-10" in text
        assert "## broken-links" in text
        assert "## orphan-compiled" in text
        assert "1 orphan compiled page" in text

    def test_render_notes_since_window(self) -> None:
        """When ``since`` is set, the report mentions the window."""
        report = LintReport(
            results=[],
            since="7d",
            today=date(2026, 5, 10),
        )
        text = report.render()
        assert "7d" in text

    def test_empty_section_uses_placeholder(self) -> None:
        """A check with zero findings still renders its section."""
        report = LintReport(
            results=[
                CheckResult(name="broken-links", summary="OK", findings=[]),
            ],
            since=None,
            today=date(2026, 5, 10),
        )
        text = report.render()
        assert "## broken-links" in text
        assert "OK" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestLintCLI:
    """``creek lint`` Typer command."""

    def test_lint_no_args_writes_deterministic_report(
        self,
        tmp_path: Path,
    ) -> None:
        """``creek lint --vault X`` writes a per-run lint report."""
        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-a")
        result = runner.invoke(app, ["lint", "--vault", str(tmp_path)])
        assert result.exit_code == 0, result.output
        log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
        written = list(log_dir.glob("lint-*.md"))
        assert written, "lint did not write a per-run report"

    def test_lint_check_flag_runs_single_check(self, tmp_path: Path) -> None:
        """``--check paradox`` runs only the paradox check."""
        _seed_vault(tmp_path)
        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--check", "paradox"],
        )
        assert result.exit_code == 0, result.output
        text = next(
            (tmp_path / "00-Creek-Meta" / "Processing-Log").glob("lint-*.md"),
        ).read_text(encoding="utf-8")
        assert "## paradox" in text
        assert "## broken-links" not in text

    def test_lint_since_flag_accepted(self, tmp_path: Path) -> None:
        """``--since 7d`` runs without error and produces a report."""
        _seed_vault(tmp_path)
        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--since", "7d"],
        )
        assert result.exit_code == 0, result.output

    def test_lint_rejects_unknown_check(self, tmp_path: Path) -> None:
        """Unknown ``--check`` exits non-zero."""
        _seed_vault(tmp_path)
        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--check", "no-such-check"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# `creek state` appends the latest lint report
# ---------------------------------------------------------------------------


class TestStateAppendsLint:
    """``creek state`` appends the most recent lint report (FEAT-008 AC)."""

    def test_state_includes_lint_section_when_report_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """A prior lint run shows up in the state report."""
        from creek.generate.state import StateReportGenerator

        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-a")
        LintRunner(vault_path=tmp_path, today=date(2026, 5, 10)).write(
            LintRunner(vault_path=tmp_path, today=date(2026, 5, 10)).run(),
        )

        text = StateReportGenerator(
            vault_path=tmp_path,
            today=date(2026, 5, 10),
        ).render()
        assert "## Lint summary" in text

    def test_state_omits_lint_section_when_no_report(self, tmp_path: Path) -> None:
        """No lint report → the state report still renders without crashing."""
        from creek.generate.state import StateReportGenerator

        _seed_vault(tmp_path)
        _write_fragment(tmp_path, frag_id="frag-a")
        text = StateReportGenerator(
            vault_path=tmp_path,
            today=date(2026, 5, 10),
        ).render()
        # The section header should still appear (consistent rendering),
        # but with the empty-state placeholder.
        assert "## Lint summary" in text
