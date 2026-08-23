"""Pipeline reporting of contested and unclaimed source files (issue #1304).

Two diagnostics, neither of which changes what the pipeline does:

* ``contested_sources`` — files more than one ingestor produced output
  for. It is the migration notice: a vault ingested before #1304 already
  holds the losing ingestor's fragment and nothing removes it, so the
  path has to be named or the stray is unfindable.
* ``unclaimed_sources`` — files that produced nothing at all. A
  pre-existing silent drop, now audible.

The arbiter's own contract is covered in ``tests/test_ingest_routing.py``
and its effect on the vault in
``tests/e2e/test_full_pipeline_mixed_sources.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig
from creek.ingest.base import IngestResult, ParsedFragment
from creek.pipeline import Pipeline, PipelineResult
from creek.scaffold import scaffold_vault


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A real scaffolded vault, as ``creek init`` would build it."""
    path = tmp_path / "vault"
    scaffold_vault(path)
    return path


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """An empty source directory for the test to populate."""
    path = tmp_path / "source"
    path.mkdir()
    return path


def _run(source: Path, vault: Path) -> PipelineResult:
    """Run the real pipeline over *source* into *vault*."""
    return Pipeline(config=CreekConfig()).run(source_path=source, vault_path=vault)


class TestUnclaimedSources:
    """Files no ingestor produced a fragment for are named, not swallowed."""

    def test_a_plain_json_is_reported(self, source: Path, vault: Path) -> None:
        """The pre-existing drop: a ``.json`` that is not a chat export.

        The chat sniffers reject it and ``GenericIngestor`` excludes the
        extension outright, so it has always yielded nothing.
        """
        (source / "notes.json").write_text('{"a": 1}\n', encoding="utf-8")

        result = _run(source, vault)

        assert [Path(p).name for p in result.unclaimed_sources] == ["notes.json"]
        assert result.fragments_created == 0

    def test_an_ingested_file_is_not_reported(self, source: Path, vault: Path) -> None:
        """Anything that produced a fragment stays off the list."""
        (source / "note.md").write_text("# Note\n\nBody.\n", encoding="utf-8")

        result = _run(source, vault)

        assert result.unclaimed_sources == []

    def test_a_losing_ingestors_file_is_not_reported_as_unclaimed(
        self, source: Path, vault: Path
    ) -> None:
        """Arbitration must not manufacture unclaimed files.

        The diff runs against the fragments produced *before*
        arbitration, so a file whose only surviving claimant lost is
        still 'seen'. Getting this wrong would report every contested
        file twice, in two contradictory lists.
        """
        (source / "log.txt").write_text("a line\n", encoding="utf-8")

        result = _run(source, vault)

        assert result.unclaimed_sources == []
        assert [Path(p).name for p in result.contested_sources] == ["log.txt"]

    def test_machinery_directories_are_excluded(
        self, source: Path, vault: Path
    ) -> None:
        """Build output and VCS internals are not authored content.

        The exclusion list is shared with ``CodeIngestor``'s discovery
        walk so the two cannot disagree about what was meant to be
        ingested.
        """
        (source / "node_modules" / "pkg").mkdir(parents=True)
        (source / "node_modules" / "pkg" / "meta.json").write_text("{}\n")
        (source / ".cache").mkdir()
        (source / ".cache" / "state.json").write_text("{}\n")
        (source / "wanted.json").write_text("{}\n")

        result = _run(source, vault)

        assert [Path(p).name for p in result.unclaimed_sources] == ["wanted.json"]

    def test_a_dotfile_at_the_source_root_is_still_reported(
        self, source: Path, vault: Path
    ) -> None:
        """Only dot-*directories* are machinery; a dotfile is a file.

        The exclusion tests the parent directories, not the filename, so
        a file the operator deliberately put in the source root is
        reported however it is named.
        """
        (source / ".secrets.json").write_text("{}\n", encoding="utf-8")

        result = _run(source, vault)

        assert [Path(p).name for p in result.unclaimed_sources] == [".secrets.json"]

    def test_a_single_file_source_reports_nothing(
        self, source: Path, vault: Path
    ) -> None:
        """A file, not a directory, as the source root must not crash."""
        note = source / "note.md"
        note.write_text("# Note\n\nBody.\n", encoding="utf-8")

        result = Pipeline(config=CreekConfig()).run(source_path=note, vault_path=vault)

        assert result.unclaimed_sources == []


def _stub_ingestor_class(ingest_result: IngestResult) -> type:
    """Build a real ingestor *class* whose ``ingest`` returns *ingest_result*.

    A class, not a ``MagicMock()`` standing in for one. ``INGESTOR_REGISTRY``
    has always been declared ``dict[str, type[Ingestor]]``, and #1517's
    ``build_ingestor`` now asks ``issubclass(entry, ImageIngestor)`` of every
    entry so it can hand the OCR block to the one ingestor that reads pixels.
    A mock *instance* cannot answer ``issubclass``.

    Args:
        ingest_result: The canned result every instance returns.

    Returns:
        A zero-argument-constructible ingestor class.
    """

    class _StubIngestor:
        """Minimal ingestor returning one canned result."""

        def ingest(self, source_path: Path) -> IngestResult:
            """Return the canned result, ignoring *source_path*."""
            del source_path
            return ingest_result

    return _StubIngestor


class TestArbitrationNeverSuppressesAnError:
    """Every ingestor's errors reach the operator, winner or loser.

    Arbitration discards *fragments*. Discarding an ingestor's error
    report along with them would make failure invisible in exactly the
    cases that matter most: an ingestor that produced nothing because it
    crashed, and an ingestor that lost the file it failed on.
    """

    def _registry(
        self, fragments: list[ParsedFragment], errors: list[str]
    ) -> dict[str, type]:
        """Build a single-entry registry returning *fragments* and *errors*."""
        result = IngestResult(fragments=fragments, errors=errors)
        return {"mock": _stub_ingestor_class(result)}

    def test_an_ingestor_that_produced_nothing_still_reports_its_errors(
        self, source: Path, vault: Path
    ) -> None:
        """Zero fragments plus an error is the OCR-unavailable case.

        ``ImageIngestor`` does exactly this when the ``tesseract`` binary
        is missing. Tying error reporting to fragment output would hide
        it completely.
        """
        registry = self._registry([], ["parse error for shot.png: no tesseract"])

        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = _run(source, vault)

        assert result.errors == ["[mock] parse error for shot.png: no tesseract"]

    def test_a_losing_ingestors_errors_still_reach_the_result(
        self, source: Path, vault: Path
    ) -> None:
        """Losing the arbitration does not retract a failure report."""
        (source / "log.txt").write_text("a line\n", encoding="utf-8")

        result = _run(source, vault)
        # ``generic`` loses ``log.txt`` to ``document``; both ran, and
        # neither is filtered out of the error channel by having lost.
        assert result.errors == []
        assert [Path(p).name for p in result.contested_sources] == ["log.txt"]

        registry = self._registry([], ["generic: unreadable encoding"])
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            mocked = _run(source, vault)
        assert mocked.errors == ["[mock] generic: unreadable encoding"]


class TestProcessCommandOutput:
    """What an operator actually sees on the terminal."""

    def _process(self, source: Path, vault: Path) -> str:
        """Invoke ``creek process`` and return its rendered output."""
        result = CliRunner().invoke(
            app,
            [
                "process",
                "--source",
                str(source),
                "--vault",
                str(vault),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_contested_sources_are_reported_with_the_cleanup_command(
        self, source: Path, vault: Path
    ) -> None:
        """The migration notice names the read-only command that finds strays.

        Nothing is deleted on the operator's behalf, so the notice has to
        carry enough to act on: the count, the paths, and the dry-run
        purge invocation that lists what is actually in the vault.
        """
        (source / "log.txt").write_text("a line\n", encoding="utf-8")

        output = self._process(source, vault)

        assert "Contested sources: 1" in output
        assert "creek purge source --source-path" in output
        assert "--dry-run" in output

    def test_unclaimed_sources_are_reported(self, source: Path, vault: Path) -> None:
        """A file that produced nothing is named in the summary."""
        (source / "notes.json").write_text("{}\n", encoding="utf-8")

        output = self._process(source, vault)

        assert "Unclaimed sources: 1" in output
        assert "notes.json" in output

    def test_a_clean_run_says_neither(self, source: Path, vault: Path) -> None:
        """No contest and no drop means the summary keeps its old shape."""
        (source / "note.md").write_text("# Note\n\nBody.\n", encoding="utf-8")

        output = self._process(source, vault)

        assert "Contested sources" not in output
        assert "Unclaimed sources" not in output
        assert "Fragments created: 1" in output
