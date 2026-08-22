"""Generated index pages are not orphans; lint reports are not link sources.

Regression tests for issue #1344, which recorded two independent defects that
compound into one unusable report.

*The 13/13.* On a temp vault carrying nothing but
``IndexGenerator(vault).generate_all()``, a ``04-Praxis/praxis-1.md`` page, and
an ``08-Decisions/brief-1.md`` containing ``- [[praxis-1]]``,
``orphan-compiled`` reported ``13 orphan compiled page(s)`` — every one of the
thirteen a false positive: ``02-Threads/Thread-Index.md``,
``03-Eddies/Eddy-Map.md``, ``04-Praxis/praxis-1.md``, and the ten
``06-Frequencies/F*/F*-Index.md`` pages. Twelve of them are Dataview index
notes Creek itself writes and that nothing is ever expected to link; the
thirteenth is linked from ``08-Decisions``, a directory the check never
surveyed because its inbound-link sources were hard-coded to ``01-Fragments``
and ``10-Liminal``. A check that is 100% wrong is worse than no check at all,
because its output still has to be read.

*The 1 → 2 → 3.* ``broken-links`` surveys only ``01-Fragments`` while
reporting a whole-vault verdict, so the obvious remedy is to survey the whole
vault. Done naively, the check then eats its own output: ``creek lint`` writes
``00-Creek-Meta/Processing-Log/lint-<date>.md``, which renders every finding
as ``- `src` → `[[target]]` ``. On a vault holding exactly ONE genuine broken
link, three successive ``creek lint`` runs reported 1, then 2, then 3 broken
links. ``00-Creek-Meta/State/`` is the same hazard, and
``00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md`` carries the deployed
spec's own syntax example (``[[note-name]]``, line 746) — the single finding a
whole-vault scan produces on a fresh 32-file ``creek init`` vault. Those three
path prefixes are carved out and nothing else is, because
``00-Creek-Meta/Tag-Garden.md`` emits real ``[[fragment-id]]`` links and has to
remain a surveyed source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.indexes import GENERATED_INDEX_TYPES, IndexGenerator
from creek.lint import LintRunner
from creek.lint.checks import broken_links, orphan_compiled

if TYPE_CHECKING:
    from pathlib import Path


def _write(vault: Path, relpath: str, text: str) -> Path:
    """Write *text* to *relpath* under *vault*, creating parent folders.

    Args:
        vault: Vault root.
        relpath: Vault-relative path of the file to write.
        text: Full file contents.

    Returns:
        The path written.
    """
    path = vault / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _page(vault: Path, relpath: str, page_type: str, body: str = "Body.") -> Path:
    """Write a page whose frontmatter declares ``type: <page_type>``.

    Args:
        vault: Vault root.
        relpath: Vault-relative path of the page.
        page_type: Value of the frontmatter ``type`` key.
        body: Markdown body beneath the header.

    Returns:
        The path written.
    """
    return _write(vault, relpath, f"---\ntype: {page_type}\n---\n\n{body}\n")


# ---------------------------------------------------------------------------
# orphan-compiled: the twelve generated index pages
# ---------------------------------------------------------------------------


def test_generated_index_pages_are_not_orphans(tmp_path: Path) -> None:
    """Nothing links a Dataview index note, and nothing ever will.

    ``generate_all`` writes twelve pages into the compiled directories. Each
    one is a query, not a destination; flagging them as orphans is telling the
    operator to review pages Creek itself created moments earlier.
    """
    IndexGenerator(tmp_path).generate_all()

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []
    assert "0 orphan compiled page(s)" in result.summary


def test_the_thirteen_false_positives_are_all_gone(tmp_path: Path) -> None:
    """The measured repro from #1344, reported as ``13 orphan compiled page(s)``.

    This is the disjointness proof: twelve findings need the generated-index
    exclusion and the thirteenth (``04-Praxis/praxis-1.md``, linked from
    ``08-Decisions``) needs the widened inbound-source survey. Either remedy
    alone leaves this vault dirty.
    """
    IndexGenerator(tmp_path).generate_all()
    _page(tmp_path, "04-Praxis/praxis-1.md", "praxis", body="A practice.")
    _page(tmp_path, "08-Decisions/brief-1.md", "decision", body="- [[praxis-1]]")

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []
    assert "0 orphan compiled page(s)" in result.summary


def test_a_genuinely_orphaned_thread_is_still_reported_after_generate_all(
    tmp_path: Path,
) -> None:
    """The check must stay loud about a real orphan among the index pages.

    Excluding the generated pages by decorating the whole compiled layer as
    "linked" would silence the check — the same end state as the bug, reached
    from the other side. The unlinked thread is the ONLY finding here.
    """
    IndexGenerator(tmp_path).generate_all()
    _page(tmp_path, "02-Threads/Dormant/lonely-thread.md", "thread")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "02-Threads/Dormant/lonely-thread.md" in result.findings[0]
    assert "1 orphan compiled page(s)" in result.summary


def test_generated_index_types_matches_what_the_generators_write(
    tmp_path: Path,
) -> None:
    """Drift guard: the constant set must equal the types actually written.

    ``generate_all`` returns fourteen paths whose frontmatter ``type`` values
    are ten ``frequency-index`` plus ``thread-index``, ``eddy-map``,
    ``temporal-index`` and ``source-index``. If a generator ever renames or
    adds a type without updating :data:`GENERATED_INDEX_TYPES`, the exclusion
    silently stops applying and the false positives come back.
    """
    generated = IndexGenerator(tmp_path).generate_all()

    assert len(generated) == 14
    collected: set[str] = set()
    for path in generated:
        declared = frontmatter.load(str(path)).get("type")
        assert isinstance(declared, str)
        collected.add(declared)

    assert collected == GENERATED_INDEX_TYPES


# ---------------------------------------------------------------------------
# orphan-compiled: inbound links from every content directory
# ---------------------------------------------------------------------------


def test_praxis_linked_only_from_08_decisions_is_not_orphaned(
    tmp_path: Path,
) -> None:
    """A decision brief citing a praxis is an inbound link.

    No index pages here at all — this isolates the source-survey remedy from
    the generated-index remedy.
    """
    _page(tmp_path, "04-Praxis/praxis-1.md", "praxis", body="A practice.")
    _page(tmp_path, "08-Decisions/brief-1.md", "decision", body="- [[praxis-1]]")

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []


def test_praxis_linked_only_from_05_wavelength_is_not_orphaned(
    tmp_path: Path,
) -> None:
    """A wavelength observation citing a praxis is an inbound link."""
    _page(tmp_path, "04-Praxis/praxis-2.md", "praxis", body="A practice.")
    _page(
        tmp_path,
        "05-Wavelength/Observations/obs-1.md",
        "wavelength-observation",
        body="- [[praxis-2]]",
    )

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []


def test_praxis_linked_only_from_07_voice_is_not_orphaned(tmp_path: Path) -> None:
    """A voice draft citing a praxis is an inbound link."""
    _page(tmp_path, "04-Praxis/praxis-3.md", "praxis", body="A practice.")
    _page(
        tmp_path,
        "07-Voice/Drafts/draft-1.md",
        "voice-draft",
        body="- [[praxis-3]]",
    )

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []


def test_thread_linked_only_from_tag_garden_is_not_orphaned(tmp_path: Path) -> None:
    """``00-Creek-Meta/Tag-Garden.md`` emits real links and is a real source.

    Carving out the whole of ``00-Creek-Meta`` to escape the report/state
    feedback loop would silently discard the tag garden's links, which
    ``creek/generate/tags.py`` writes as ``[[fragment-id]]``.
    """
    _page(tmp_path, "02-Threads/Active/thread-1.md", "thread")
    _write(
        tmp_path,
        "00-Creek-Meta/Tag-Garden.md",
        "# Tag Garden\n\n## sourdough\n\n- [[thread-1]]\n",
    )

    result = orphan_compiled.run(tmp_path)

    assert result.findings == []


# ---------------------------------------------------------------------------
# orphan-compiled: the exclusion is by declared type, and nothing wider
# ---------------------------------------------------------------------------


def test_compiled_page_with_no_type_key_is_still_reported(tmp_path: Path) -> None:
    """A header without ``type`` earns no exclusion.

    The generated ``Thread-Index.md`` alongside it must still drop out, so the
    single finding proves the exclusion is narrow rather than absent.
    """
    IndexGenerator(tmp_path).generate_thread_index()
    _write(tmp_path, "03-Eddies/no-type.md", "---\ntitle: No Type\n---\n\nBody.\n")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "03-Eddies/no-type.md" in result.findings[0]


def test_compiled_page_with_unknown_type_is_still_reported(tmp_path: Path) -> None:
    """A type Creek's generators never write is not a generated index."""
    IndexGenerator(tmp_path).generate_thread_index()
    _page(tmp_path, "03-Eddies/eddy-atlas.md", "eddy-atlas")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "03-Eddies/eddy-atlas.md" in result.findings[0]


def test_compiled_page_with_no_frontmatter_at_all_is_still_reported(
    tmp_path: Path,
) -> None:
    """A bare markdown page has no declared type, so it stays a candidate."""
    IndexGenerator(tmp_path).generate_thread_index()
    _write(tmp_path, "02-Threads/plain.md", "Just a body, no header.\n")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "02-Threads/plain.md" in result.findings[0]


def test_compiled_page_with_malformed_yaml_header_is_still_reported(
    tmp_path: Path,
) -> None:
    """Unparseable YAML yields no type — and no exception out of the check.

    Failing open here would hand any page an exclusion by writing a broken
    header; failing loudly would cost the whole run over one bad file.
    """
    IndexGenerator(tmp_path).generate_thread_index()
    _write(tmp_path, "03-Eddies/malformed.md", "---\ntype: [unclosed\n---\n\nBody.\n")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "03-Eddies/malformed.md" in result.findings[0]


def test_a_page_cannot_credit_itself(tmp_path: Path) -> None:
    """A page's wiki-link to its own name is not an inbound link.

    Widening the inbound-source survey to the compiled layer means a page's
    own body is now read. Self-credit would let every self-referencing page
    exempt itself from the check.
    """
    IndexGenerator(tmp_path).generate_thread_index()
    _page(tmp_path, "02-Threads/Self.md", "thread", body="See [[Self]] for more.")

    result = orphan_compiled.run(tmp_path)

    assert len(result.findings) == 1
    assert "02-Threads/Self.md" in result.findings[0]


# ---------------------------------------------------------------------------
# broken-links: the survey covers every content directory
# ---------------------------------------------------------------------------


def test_broken_link_in_08_decisions_is_reported(tmp_path: Path) -> None:
    """A dangling link in a decision brief is a whole-vault finding."""
    _page(
        tmp_path,
        "08-Decisions/brief-2.md",
        "decision",
        body="See [[ghost-decision]].",
    )

    result = broken_links.run(tmp_path)

    assert len(result.findings) == 1
    assert "08-Decisions/brief-2.md" in result.findings[0]
    assert "[[ghost-decision]]" in result.findings[0]


def test_broken_link_in_05_wavelength_is_reported(tmp_path: Path) -> None:
    """A dangling link in a wavelength observation is a whole-vault finding."""
    _page(
        tmp_path,
        "05-Wavelength/Observations/obs-2.md",
        "wavelength-observation",
        body="See [[ghost-phase]].",
    )

    result = broken_links.run(tmp_path)

    assert len(result.findings) == 1
    assert "05-Wavelength/Observations/obs-2.md" in result.findings[0]
    assert "[[ghost-phase]]" in result.findings[0]


def test_broken_link_in_07_voice_is_reported(tmp_path: Path) -> None:
    """A dangling link in a voice draft is a whole-vault finding."""
    _page(
        tmp_path,
        "07-Voice/Drafts/draft-2.md",
        "voice-draft",
        body="See [[ghost-lexeme]].",
    )

    result = broken_links.run(tmp_path)

    assert len(result.findings) == 1
    assert "07-Voice/Drafts/draft-2.md" in result.findings[0]
    assert "[[ghost-lexeme]]" in result.findings[0]


def test_broken_link_in_a_compiled_thread_page_is_reported(tmp_path: Path) -> None:
    """A dangling link on a thread page is a whole-vault finding."""
    _page(
        tmp_path,
        "02-Threads/Active/thread-2.md",
        "thread",
        body="See [[ghost-thread]].",
    )

    result = broken_links.run(tmp_path)

    assert len(result.findings) == 1
    assert "02-Threads/Active/thread-2.md" in result.findings[0]
    assert "[[ghost-thread]]" in result.findings[0]


# ---------------------------------------------------------------------------
# broken-links: Creek's own artefacts are not vault content
# ---------------------------------------------------------------------------


def test_lint_report_does_not_feed_its_own_next_scan(tmp_path: Path) -> None:
    """Three successive lint runs must report the same one broken link.

    Measured on the naive whole-vault scan: 1, then 2, then 3, because
    ``lint-<date>.md`` renders each finding as ``- `src` → `[[target]]` `` and
    the next run reads it back as vault content. The report is asserted to
    exist on disk between passes, so the test cannot pass by never writing it.
    """
    _page(
        tmp_path,
        "01-Fragments/Notes/frag-1.md",
        "fragment",
        body="See [[ghost]] for context.",
    )
    expected = "1 broken link(s) across 1 file(s)"
    finding = "- `01-Fragments/Notes/frag-1.md` → `[[ghost]]`"

    summaries: list[str] = []
    report_path: Path | None = None
    for _ in range(3):
        if report_path is not None:
            assert report_path.exists()
        lint = LintRunner(tmp_path)
        report = lint.run(["broken-links"])
        summaries.append(report.results[0].summary)
        report_path = lint.write(report)
        assert report_path.exists()
        assert finding in report_path.read_text(encoding="utf-8")

    assert summaries == [expected, expected, expected]


def test_state_artifact_does_not_feed_the_broken_link_scan(tmp_path: Path) -> None:
    """``00-Creek-Meta/State/latest.md`` echoes findings; it is not a source."""
    _write(
        tmp_path,
        "00-Creek-Meta/State/latest.md",
        "## Drift warnings\n\n- Broken links in `x`: [[ghost]]\n",
    )

    result = broken_links.run(tmp_path)

    assert result.findings == []
    assert "0 broken link(s)" in result.summary


def test_deployed_ontology_syntax_example_is_not_a_broken_link(
    tmp_path: Path,
) -> None:
    """The deployed spec documents wiki-link syntax; it does not link a page.

    Line 746 of ``creek_ontology_agent_prompt.md`` is the only finding a
    whole-vault scan produces on a fresh ``creek init`` vault, which is why
    ``00-Creek-Meta/Ontology/`` is carved out.
    """
    _write(
        tmp_path,
        "00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md",
        "Generate wiki-links (`[[note-name]]`) between related fragments and"
        " add them to the appropriate frontmatter arrays.\n",
    )

    result = broken_links.run(tmp_path)

    assert result.findings == []


@pytest.mark.integration
def test_fresh_init_vault_lints_clean(tmp_path: Path) -> None:
    """The tripwire bounding the deny-list: a scaffolded vault lints clean.

    Every path component excluded from the survey costs coverage, so the
    carve-out is bounded by this: canonical material plus the generated index
    notes must produce zero findings from both checks. A wider deny-list would
    still pass; a narrower one would fail here, which is where the argument
    for each excluded prefix has to be made.
    """
    vault = tmp_path / "vault"

    result = CliRunner().invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0

    IndexGenerator(vault).generate_all()

    assert broken_links.run(vault).findings == []
    assert orphan_compiled.run(vault).findings == []
