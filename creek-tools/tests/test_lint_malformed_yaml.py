"""Regression tests: one malformed-YAML note must not take down ``creek lint``.

``yaml.YAMLError`` is **not** a :class:`ValueError` subclass, so an
``except (OSError, ValueError)`` guard around ``frontmatter.load`` never
catches a note whose frontmatter fails to parse. The ``continue`` that
each guard was written to reach is therefore dead, and the parser error
escapes to the operator as a raw traceback.

These tests pin the contract already honoured by
``creek/lint/checks/draft_grounding.py`` and ``creek/vault/reader.py``:
an unparseable note is skipped, every other note is still reported, and
the command exits cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.lint.checks import (
    compost as compost_check,
)
from creek.lint.checks import (
    paradox as paradox_check,
)
from creek.lint.checks import (
    synchronicity as synchronicity_check,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_MALFORMED = "---\ntags: [a, b\ntitle: unclosed flow sequence\n---\n\nBody.\n"
"""Frontmatter with an unterminated flow sequence — raises ``yaml.ParserError``."""


def _seed_vault(vault: Path) -> None:
    """Create the canonical vault folders the lint checks walk."""
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


def _write_malformed(vault: Path, relative: str) -> Path:
    """Write a note whose YAML frontmatter cannot be parsed."""
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MALFORMED, encoding="utf-8")
    return target


def _write_compost_note(vault: Path, *, stem: str, title: str) -> Path:
    """Write a well-formed compost note under ``10-Liminal/Compost/``."""
    target = vault / "10-Liminal" / "Compost" / f"{stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'---\ntype: compost\ntitle: "{title}"\n---\n\nDormant.\n',
        encoding="utf-8",
    )
    return target


def _write_sync_note(vault: Path, *, stem: str, sync_id: str) -> Path:
    """Write a well-formed synchronicity note under ``10-Liminal/``."""
    target = vault / "10-Liminal" / "Synchronicities" / f"{stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'---\ntype: synchronicity\nid: "{sync_id}"\n---\n\nEcho.\n',
        encoding="utf-8",
    )
    return target


class TestCompostCheckSkipsMalformedYaml:
    """``compost`` must skip an unparseable note, not crash on it."""

    def test_malformed_note_is_skipped(self, tmp_path: Path) -> None:
        """A note with unclosed frontmatter does not raise out of the check."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "10-Liminal/Compost/broken.md")

        result = compost_check.run(tmp_path)

        assert result.name == "compost"
        assert result.findings == []

    def test_healthy_notes_still_reported(self, tmp_path: Path) -> None:
        """The good notes either side of the bad one are still counted."""
        _seed_vault(tmp_path)
        _write_compost_note(tmp_path, stem="a-note", title="Alpha")
        _write_malformed(tmp_path, "10-Liminal/Compost/broken.md")
        _write_compost_note(tmp_path, stem="z-note", title="Zeta")

        result = compost_check.run(tmp_path)

        assert len(result.findings) == 2
        assert result.summary == "2 recorded compost note(s)"
        assert any("Alpha" in line for line in result.findings)
        assert any("Zeta" in line for line in result.findings)


class TestSynchronicityCheckSkipsMalformedYaml:
    """``synchronicity`` must skip an unparseable note, not crash on it."""

    def test_malformed_note_is_skipped(self, tmp_path: Path) -> None:
        """A note with unclosed frontmatter does not raise out of the check."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "10-Liminal/Synchronicities/broken.md")

        result = synchronicity_check.run(tmp_path)

        assert result.name == "synchronicity"
        assert result.findings == []

    def test_healthy_notes_still_reported(self, tmp_path: Path) -> None:
        """The good notes either side of the bad one are still counted."""
        _seed_vault(tmp_path)
        _write_sync_note(tmp_path, stem="a-sync", sync_id="sync-alpha")
        _write_malformed(tmp_path, "10-Liminal/Synchronicities/broken.md")
        _write_sync_note(tmp_path, stem="z-sync", sync_id="sync-zeta")

        result = synchronicity_check.run(tmp_path)

        assert len(result.findings) == 2
        assert result.summary == "2 recorded synchronicity note(s)"
        assert any("sync-alpha" in line for line in result.findings)
        assert any("sync-zeta" in line for line in result.findings)


class TestParadoxCheckSkipsMalformedYaml:
    """``paradox`` must skip an unparseable fragment, not crash on it."""

    def test_malformed_fragment_is_skipped(self, tmp_path: Path) -> None:
        """A fragment with unclosed frontmatter does not raise out of the check."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "01-Fragments/Notes/broken.md")

        result = paradox_check.run(tmp_path)

        assert result.name == "paradox"
        assert result.findings == []

    def test_malformed_liminal_note_is_skipped(self, tmp_path: Path) -> None:
        """The ``10-Liminal`` half of the fragment sweep is guarded too."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "10-Liminal/Unnamed/broken.md")

        result = paradox_check.run(tmp_path)

        assert result.name == "paradox"
        assert result.findings == []
        assert result.summary.startswith("0 paradox(es) detected")


class TestLintCliSurvivesMalformedYaml:
    """The real Typer entry point must exit cleanly on a malformed note."""

    def test_default_lint_exits_zero(self, tmp_path: Path) -> None:
        """``creek lint`` with no flags survives a malformed compost note."""
        _seed_vault(tmp_path)
        _write_compost_note(tmp_path, stem="a-note", title="Alpha")
        _write_malformed(tmp_path, "10-Liminal/Compost/broken.md")

        result = runner.invoke(app, ["lint", "--vault", str(tmp_path)])

        assert result.exit_code == 0, result.output
        report = next(
            (tmp_path / "00-Creek-Meta" / "Processing-Log").glob("lint-*.md"),
        ).read_text(encoding="utf-8")
        assert "1 recorded compost note(s)" in report

    def test_check_compost_exits_zero(self, tmp_path: Path) -> None:
        """``--check compost`` survives a malformed compost note."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "10-Liminal/Compost/broken.md")

        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--check", "compost"],
        )

        assert result.exit_code == 0, result.output

    def test_check_synchronicity_exits_zero(self, tmp_path: Path) -> None:
        """``--check synchronicity`` survives a malformed synchronicity note."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "10-Liminal/Synchronicities/broken.md")

        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--check", "synchronicity"],
        )

        assert result.exit_code == 0, result.output

    def test_check_paradox_exits_zero(self, tmp_path: Path) -> None:
        """``--check paradox`` survives a malformed fragment."""
        _seed_vault(tmp_path)
        _write_malformed(tmp_path, "01-Fragments/Notes/broken.md")

        result = runner.invoke(
            app,
            ["lint", "--vault", str(tmp_path), "--check", "paradox"],
        )

        assert result.exit_code == 0, result.output
