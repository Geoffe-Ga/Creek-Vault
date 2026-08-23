"""A frontmatter ``id`` YAML types as something other than ``str`` (#1291).

``id: 12345`` is valid YAML and reads perfectly well to a human, but
``SafeLoader`` resolves it to an ``int``. Every reader of the id index requires
``isinstance(id, str)``, so such a file is **invisible to identity**: it is
never indexed, ``find_fragment`` never resolves it, and the next write for the
logical id ``"12345"`` mints a second file beside it. Nothing anywhere tells
the operator, because from the pipeline's point of view the file simply
declares no id.

#1291 offered two ways out. Option (a) — normalise on read, ``str(id)`` — was
**rejected**: it silently merges two identities the vault never said were the
same, and it would have arrived as a side effect of the #1543 byte-scan rather
than as a decision. ``creek.vault.writer._typed_scalar`` therefore reports an
unquoted scalar only when YAML itself would type it ``str``, and
``tests/test_vault_writer.py::TestIdIndexVerification::test_non_str_id_in_frontmatter_is_a_mismatch``
still pins the strict outcome.

Option (b) — report it — is this check. The hazard stops being silent without
the writer having to guess what the operator meant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from creek.lint import runner as runner_module
from creek.lint.checks import nonstring_id as nonstring_id_check

if TYPE_CHECKING:
    from pathlib import Path

CHECK_NAME: Final[str] = "nonstring-id"
"""The registry key, asserted rather than assumed in the wiring test below."""

_NON_STRING_IDS: Final[dict[str, str]] = {
    "int": "12345",
    "float": "1.5",
    "bool": "true",
    "date": "2024-05-01",
}
"""``id:`` values a human reads as an id and ``SafeLoader`` types as not-a-str."""


def _write_note(path: Path, header: str) -> Path:
    """Write a markdown note whose frontmatter block is *header* verbatim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header}---\nbody text\n", encoding="utf-8")
    return path


def _findings_text(vault: Path) -> str:
    """Run the check over *vault* and return its findings joined into one blob."""
    return "\n".join(nonstring_id_check.run(vault).findings)


class TestNonStringIdCheck:
    """The check reports exactly the files identity cannot see."""

    @pytest.mark.parametrize("label", sorted(_NON_STRING_IDS))
    def test_a_non_string_id_is_reported(self, tmp_path: Path, label: str) -> None:
        """Each YAML type that is not ``str`` surfaces as its own finding."""
        note = _write_note(
            tmp_path / "01-Fragments" / "Notes" / f"{label}.md",
            f"id: {_NON_STRING_IDS[label]}\ntype: fragment\n",
        )

        result = nonstring_id_check.run(tmp_path)

        assert result.name == CHECK_NAME
        assert len(result.findings) == 1
        assert str(note.relative_to(tmp_path)) in result.findings[0]

    def test_the_finding_names_the_yaml_type_and_not_the_value(
        self,
        tmp_path: Path,
    ) -> None:
        """The report says *what went wrong*, never the id's own text.

        An id is operator-authored content out of a private vault, and the
        actionable signal is entirely in the type: "this resolved to an ``int``
        — quote it". Echoing the value into a report that lands in the vault
        (and into a terminal, and into any log capturing it) buys nothing and
        widens what the lint surface discloses. ``unparseable`` makes the same
        call about exception messages.
        """
        _write_note(
            tmp_path / "01-Fragments" / "Notes" / "secretive.md",
            "id: 98765\ntype: fragment\n",
        )

        findings = _findings_text(tmp_path)

        assert "int" in findings
        assert "98765" not in findings

    def test_a_string_id_is_not_reported(self, tmp_path: Path) -> None:
        """Positive control: an ordinary quoted-or-plain string id is fine."""
        notes = tmp_path / "01-Fragments" / "Notes"
        _write_note(notes / "plain.md", "id: frag-000000000001\ntype: fragment\n")
        _write_note(notes / "quoted.md", 'id: "12345"\ntype: fragment\n')

        result = nonstring_id_check.run(tmp_path)

        assert result.findings == []
        assert "No " in result.summary

    def test_a_file_with_no_id_key_is_not_reported(self, tmp_path: Path) -> None:
        """ "Declares no id" is a different thing from "declares a bad one".

        A ``_author.md`` manifest or a hand-written index note has no ``id``
        and is *supposed* to have none. Reporting those would bury the four
        files that actually matter under every non-fragment in the vault.
        """
        _write_note(tmp_path / "01-Fragments" / "Notes" / "idless.md", "title: T\n")

        assert nonstring_id_check.run(tmp_path).findings == []

    def test_an_unreadable_file_is_left_to_the_unparseable_check(
        self,
        tmp_path: Path,
    ) -> None:
        """A header that will not parse is a *different* finding, already owned.

        ``unparseable`` reports it. Reporting it here as well would double-count
        one broken file across two checks, and this check cannot say anything
        about an id it never managed to read.
        """
        _write_note(
            tmp_path / "01-Fragments" / "Notes" / "broken.md",
            "id: 12345\n2024-05-01: reflection\n",
        )

        assert nonstring_id_check.run(tmp_path).findings == []

    def test_every_corpus_subtree_is_walked(self, tmp_path: Path) -> None:
        """Identity is vault-wide, so the scan is too.

        A non-string id under ``09-Reference`` or ``11-Other-Authors`` is
        exactly as invisible as one under ``01-Fragments``; narrowing to the
        fragments tree would hand the operator a clean report and a broken
        vault.
        """
        from creek.vault.reader import CORPUS_SUBDIRS

        for subdir in CORPUS_SUBDIRS:
            _write_note(
                tmp_path / subdir / "Notes" / "numeric.md",
                "id: 12345\ntype: fragment\n",
            )

        result = nonstring_id_check.run(tmp_path)

        assert len(result.findings) == len(CORPUS_SUBDIRS)
        assert len(CORPUS_SUBDIRS) > 1  # positive control: more than one subtree.

    def test_findings_are_sorted_so_two_runs_render_identically(
        self,
        tmp_path: Path,
    ) -> None:
        """The report is written into the vault, so an unstable order is a diff."""
        notes = tmp_path / "01-Fragments" / "Notes"
        for name in ("zebra", "alpha", "mango"):
            _write_note(notes / f"{name}.md", "id: 12345\ntype: fragment\n")

        findings = nonstring_id_check.run(tmp_path).findings

        assert len(findings) == 3
        assert findings == sorted(findings)

    def test_a_missing_vault_subtree_is_not_an_error(self, tmp_path: Path) -> None:
        """A vault that has no corpus yet lints clean rather than crashing."""
        assert nonstring_id_check.run(tmp_path).findings == []


class TestNonStringIdWiring:
    """The check is reachable the way every other deterministic check is."""

    def test_the_check_is_registered_and_runs_by_default(self) -> None:
        """Registered, defaulted, and the *same object* in both tables."""
        assert CHECK_NAME in runner_module.DETERMINISTIC_CHECKS
        assert runner_module._REGISTRY[CHECK_NAME] is nonstring_id_check.run

    def test_the_runner_reports_the_check(self, tmp_path: Path) -> None:
        """Driving through :class:`~creek.lint.runner.LintRunner` reaches it."""
        _write_note(
            tmp_path / "01-Fragments" / "Notes" / "numeric.md",
            "id: 12345\ntype: fragment\n",
        )

        report = runner_module.LintRunner(tmp_path).run([CHECK_NAME])

        assert [result.name for result in report.results] == [CHECK_NAME]
        assert report.results[0].findings

    def test_the_cli_help_lists_the_check(self) -> None:
        """The ``--check`` help string is restated by hand, so it must be updated.

        Asserted against the *rendered* help an operator actually reads,
        not against ``inspect.getsource(cli)``: a source-substring match
        passes on any mention of the name anywhere in the module — a
        comment, an unrelated branch, a docstring — and so would keep
        passing if the name were dropped from the help text it exists to
        pin. ``test_lint.py::test_the_check_help_string_lists_every_check``
        makes the same guarantee over the whole registry; this one is
        here so the wiring for *this* check is visible beside the check.
        """
        from typer.testing import CliRunner

        from creek.cli import app

        rendered = CliRunner().invoke(app, ["lint", "--help"]).output
        # Typer wraps help text, so strip newlines before substring checks.
        flat = " ".join(rendered.split())

        assert CHECK_NAME in flat, f"--check help omits {CHECK_NAME}: {flat}"
