"""Symbol citations in filed scan issues must be verifiable, not confabulated.

Issue #1651. The ``scan:coverage`` producer emitted function names that
exist in no revision of the file it cites. Re-resolving every citation in
#1446 and #1447 against their own scan SHA found four of ten and four of
five wrong, and the wrong names differ from the real ones by **paraphrase**
--- ``_scrub_references`` cited as ``_replace_references``,
``_intimate_pointer`` as ``_intimate_body_pointer``, ``_generate_filename``
as ``_unique_filename``.

That rules out the obvious explanation. Scanning upward from a raw line
number in a stale checkout would have produced the *right* names: ast and
nearest-preceding-``def`` agree on six of the ten. The names are
confabulated from surrounding code.

The root cause is structural. Every ``prompts/scans/*.md`` finding schema
is ``{slug, title, severity, file, lines, evidence, test_plan}`` --- there
is no ``symbol`` field, so a name reaches the issue title as free text that
nothing ever checks. Coverage is the worst-hit scan because
``--cov-report=term-missing`` prints line numbers and **no names at all**,
while complexity, dead-code, types and mutation get real names out of
radon, vulture, mypy and mutmut.

So the fix is not a better prompt. It is to make a phantom symbol
*unrepresentable* in a filed issue: declare the symbol as a datum, resolve
it against the scan SHA's blob, and refuse the citation when no definition
of that exact name exists there.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.scan_citations import (
    CitationError,
    main,
    resolve_enclosing_symbol,
    verify_symbol,
)

_REVISION_A = '''\
"""Fixture module, revision A."""


class Engine:
    """Holds the nested definition ast must find and a scan must not."""

    def purge_daterange(self, start: str, end: str) -> None:
        """The enclosing METHOD, which is not the innermost definition."""

        def body(chunk: str) -> str:
            """The innermost definition at the line below."""
            return chunk.strip()

        body(start + end)


@staticmethod
def decorated_helper() -> int:
    """A decorated def, so the decorator line is not the def line."""
    return 1


def renamed_between_revisions() -> str:
    """Present in revision A, gone in revision B."""
    return "a"
'''

_REVISION_B = _REVISION_A.replace(
    "def renamed_between_revisions() -> str:",
    "def renamed_in_revision_b() -> str:",
).replace('"""Fixture module, revision A."""', '"""Fixture module, revision B."""')


@pytest.fixture()
def two_revision_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a git repo whose fixture module has two revisions.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        ``(repo_path, sha_a, sha_b)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    target = repo / "engine.py"

    target.write_text(_REVISION_A, encoding="utf-8")
    git("add", "engine.py")
    git("commit", "-qm", "revision A")
    sha_a = git("rev-parse", "HEAD")

    target.write_text(_REVISION_B, encoding="utf-8")
    git("add", "engine.py")
    git("commit", "-qm", "revision B")
    sha_b = git("rev-parse", "HEAD")

    return repo, sha_a, sha_b


class TestResolveEnclosingSymbol:
    """Resolution returns the INNERMOST enclosing definition."""

    def test_returns_the_innermost_nested_definition(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """A nested ``def`` wins over the method containing it.

        This is the case that separates an ast lookup from a
        nearest-preceding-``def`` scan: both find *a* name, but only ast
        finds the right one.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, sha_a, _ = two_revision_repo
        line = _REVISION_A.splitlines().index("            return chunk.strip()") + 1

        assert (
            resolve_enclosing_symbol(repo=repo, sha=sha_a, path="engine.py", line=line)
            == "Engine.purge_daterange.body"
        )

    def test_a_module_level_line_is_not_found_rather_than_guessed(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """A line outside any definition returns None, never a nearby name.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, sha_a, _ = two_revision_repo

        assert (
            resolve_enclosing_symbol(repo=repo, sha=sha_a, path="engine.py", line=1)
            is None
        )

    def test_a_decorated_def_resolves_from_its_body(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """The decorator line must not shift the resolved range.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, sha_a, _ = two_revision_repo
        line = _REVISION_A.splitlines().index("    return 1") + 1

        assert (
            resolve_enclosing_symbol(repo=repo, sha=sha_a, path="engine.py", line=line)
            == "decorated_helper"
        )


class TestVerifySymbol:
    """Verification is against the blob at the recorded SHA."""

    def test_a_symbol_present_at_that_sha_is_accepted(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """The happy path.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, sha_a, _ = two_revision_repo

        assert verify_symbol(
            repo=repo, sha=sha_a, path="engine.py", symbol="renamed_between_revisions"
        )

    def test_the_same_symbol_is_rejected_at_the_other_revision(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """Pointing the verifier at revision B makes the assertion go red.

        This is the non-vacuity guard the acceptance criteria name: a
        verifier that always returned True would pass the test above.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, _, sha_b = two_revision_repo

        assert not verify_symbol(
            repo=repo, sha=sha_b, path="engine.py", symbol="renamed_between_revisions"
        )

    def test_a_nested_symbol_is_found_by_its_bare_name(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """Verification is by name anywhere in the blob, not by path.

        A citation naming ``body`` is not wrong merely because the symbol
        is nested; it is wrong only when no such definition exists.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, sha_a, _ = two_revision_repo

        assert verify_symbol(repo=repo, sha=sha_a, path="engine.py", symbol="body")

    def test_an_unknown_sha_raises_rather_than_silently_passing(
        self,
        two_revision_repo: tuple[Path, str, str],
    ) -> None:
        """A verifier that cannot read the blob must not report success.

        Failing open here would make the whole gate decorative: an
        unreachable SHA is exactly the condition a stale citation creates.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
        """
        repo, _, _ = two_revision_repo

        with pytest.raises(CitationError):
            verify_symbol(
                repo=repo,
                sha="0" * 40,
                path="engine.py",
                symbol="renamed_between_revisions",
            )


_SCAN_SHA = "c8c5131"
"""The scan SHA #1446, #1447, #993, #1449 and #1173 were all filed against."""

_REPO_ROOT = Path(__file__).resolve().parents[2]
"""Repository root; the citations below are repo-relative, not package-relative."""

_GROUND_TRUTH: tuple[tuple[str, str, bool], ...] = (
    ("creek-tools/creek/vault/writer.py", "_unique_filename", False),
    ("creek-tools/creek/vault/writer.py", "_generate_filename", True),
    ("creek-tools/creek/audit/log.py", "iter_entries", False),
    ("creek-tools/creek/audit/log.py", "verify_chain", False),
    ("creek-tools/creek/audit/log.py", "_read_last_line", False),
    ("creek-tools/creek/audit/log.py", "read", True),
    ("creek-tools/creek/audit/log.py", "verify", True),
    ("creek-tools/creek/audit/log.py", "_last_line", True),
    ("creek-tools/creek/clean/semantic_dedup.py", "SemanticDedup", False),
    ("creek-tools/creek/clean/semantic_dedup.py", "SemanticDeduplicator", True),
)
"""Real citations from the filed issues, with the verdict each must get.

Taken from #1446/#1447/#993/#1449/#1173 and hand-resolved against
``c8c5131``. This is the table that matters: the synthetic fixture above
proves the resolver's *mechanics*, but only real confabulated names prove
it catches the thing that actually happened. Note the shape of the
errors --- ``_unique_filename`` for ``_generate_filename``, ``verify_chain``
for ``verify`` --- these are paraphrases, not stale names.
"""


@pytest.mark.parametrize(("path", "symbol", "expected"), _GROUND_TRUTH)
def test_the_real_filed_citations_are_judged_correctly(
    path: str,
    symbol: str,
    *,
    expected: bool,
) -> None:
    """Every phantom in the filed issues is rejected; every real name passes.

    Args:
        path: Repo-relative file the issue cited.
        symbol: The symbol name the issue cited.
        expected: Whether that name really exists at the scan SHA.
    """
    assert (
        verify_symbol(repo=_REPO_ROOT, sha=_SCAN_SHA, path=path, symbol=symbol)
        is expected
    )


class TestTheGuardIsActuallyWired:
    """The verification step must be mandated, not merely available.

    Issue #1651's acceptance criteria are explicit that a prompt sentence
    alone does not satisfy the fix: a phantom must be *unrepresentable* in
    a filed issue, not discouraged. These assertions fail if the wiring is
    deleted, so removing the guard reddens CI instead of silently
    reverting the pipeline to confabulating names.
    """

    def test_the_verifier_script_exists_and_is_executable(self) -> None:
        """A mandated script that is not executable is a silent no-op."""
        script = _REPO_ROOT / "creek-tools" / "scripts" / "verify-scan-citations.sh"

        assert script.is_file(), f"{script} is missing"
        assert script.stat().st_mode & 0o111, f"{script} is not executable"

    def test_the_scan_workflow_invokes_the_verifier(self) -> None:
        """The reusable scan core must name the script, not just the skill."""
        workflow = (
            _REPO_ROOT / ".github" / "workflows" / "_claude-scan.yml"
        ).read_text(encoding="utf-8")

        assert "verify-scan-citations.sh" in workflow, (
            "the scan workflow no longer invokes the citation verifier, so "
            "nothing stops a phantom symbol reaching a filed issue"
        )

    def test_the_skill_mandates_the_verification_step(self) -> None:
        """The skill is what the workflow actually tells the model to follow."""
        skill = (
            _REPO_ROOT / ".claude" / "skills" / "scan-issue-writer" / "SKILL.md"
        ).read_text(encoding="utf-8")

        assert "verify-scan-citations.sh" in skill
        assert "MANDATORY" in skill

    def test_every_scan_prompt_declares_the_symbol_field(self) -> None:
        """A scan whose schema omits `symbol` files unverifiable names.

        ``groom.md`` is excluded deliberately: it triages existing issues
        rather than filing findings against source lines.
        """
        prompts = sorted((_REPO_ROOT / "prompts" / "scans").glob("*.md"))
        assert prompts, "no scan prompts found; this test would be vacuous"

        missing = [
            path.name
            for path in prompts
            if path.name != "groom.md"
            and "symbol" not in path.read_text(encoding="utf-8")
        ]

        assert not missing, f"scan prompts with no symbol field: {missing}"

    def test_every_json_example_declares_a_literal_symbol_key(self) -> None:
        """Prose is not enough where the prompt ships a copyable example.

        Found in review: seven prompts illustrate the schema with a fenced
        ``json`` block, and mentioning ``symbol`` only in the paragraph
        above left every concrete template without the key. Models pattern
        -match against examples at least as strongly as against prose, so
        the gap sat precisely where a literal template matters most.
        """
        prompts = sorted((_REPO_ROOT / "prompts" / "scans").glob("*.md"))
        offenders = []
        for path in prompts:
            text = path.read_text(encoding="utf-8")
            if "```json" not in text or path.name == "groom.md":
                continue
            if '"symbol"' not in text:
                offenders.append(path.name)

        assert not offenders, (
            f"these prompts ship a JSON example with no symbol key: {offenders}"
        )

    def test_the_issue_template_scopes_citations_to_the_scan_sha(self) -> None:
        """A reader must be told the cites are revision-specific."""
        template = (
            _REPO_ROOT / "prompts" / "templates" / "scan-issue-body.md"
        ).read_text(encoding="utf-8")

        assert "valid ONLY at that scan SHA" in template
        assert "Symbol(s)" in template


class TestCommandLineInterface:
    """The CLI is what CI actually runs, so its exit codes are the contract."""

    def test_a_verified_symbol_exits_zero(
        self,
        two_revision_repo: tuple[Path, str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A real name reports OK and exits 0.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
            capsys: Captures stdout.
        """
        repo, sha_a, _ = two_revision_repo

        code = main(
            [
                "--repo",
                str(repo),
                "--sha",
                sha_a,
                "--path",
                "engine.py",
                "--symbol",
                "renamed_between_revisions",
            ]
        )

        assert code == 0
        assert "OK" in capsys.readouterr().out

    def test_a_phantom_exits_one_and_names_the_real_symbol(
        self,
        two_revision_repo: tuple[Path, str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 1 is what blocks the create; the message must be actionable.

        Passing ``--line`` lets the CLI say what is really there, which is
        the whole remedy: re-cite the real name.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
            capsys: Captures stderr.
        """
        repo, _, sha_b = two_revision_repo
        line = _REVISION_B.splitlines().index("            return chunk.strip()") + 1

        code = main(
            [
                "--repo",
                str(repo),
                "--sha",
                sha_b,
                "--path",
                "engine.py",
                "--symbol",
                "renamed_between_revisions",
                "--line",
                str(line),
            ]
        )

        err = capsys.readouterr().err
        assert code == 1
        assert "PHANTOM" in err
        assert "Engine.purge_daterange.body" in err

    def test_an_unreadable_blob_exits_two_not_zero(
        self,
        two_revision_repo: tuple[Path, str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Uncheckable is its own exit code, never a silent pass.

        Failing open here would make the gate decorative: an unreachable
        SHA is exactly what a stale citation produces.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
            capsys: Captures stderr.
        """
        repo, _, _ = two_revision_repo

        code = main(
            [
                "--repo",
                str(repo),
                "--sha",
                "0" * 40,
                "--path",
                "engine.py",
                "--symbol",
                "anything",
            ]
        )

        assert code == 2
        assert "UNCHECKABLE" in capsys.readouterr().err

    def test_resolve_mode_prints_the_enclosing_symbol(
        self,
        two_revision_repo: tuple[Path, str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without --symbol the CLI answers "what is at this line".

        Args:
            two_revision_repo: Repo plus both revision SHAs.
            capsys: Captures stdout.
        """
        repo, sha_a, _ = two_revision_repo
        line = _REVISION_A.splitlines().index("            return chunk.strip()") + 1

        code = main(
            [
                "--repo",
                str(repo),
                "--sha",
                sha_a,
                "--path",
                "engine.py",
                "--line",
                str(line),
            ]
        )

        assert code == 0
        assert capsys.readouterr().out.strip() == "Engine.purge_daterange.body"

    def test_resolve_mode_reports_module_level_explicitly(
        self,
        two_revision_repo: tuple[Path, str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ "<module level>" is a real answer, not an empty line.

        Args:
            two_revision_repo: Repo plus both revision SHAs.
            capsys: Captures stdout.
        """
        repo, sha_a, _ = two_revision_repo

        code = main(
            ["--repo", str(repo), "--sha", sha_a, "--path", "engine.py", "--line", "1"]
        )

        assert code == 0
        assert capsys.readouterr().out.strip() == "<module level>"


class TestTheShellWrapperFromItsRealWorkingDirectory:
    """The wrapper must work from the CWD it is documented to run in.

    Found in review of the first fix. The wrapper originally invoked the
    resolver as ``python -m scripts.scan_citations``, which works from
    ``creek-tools/`` and **fails from the repository root** --- where
    ``scripts`` resolves to the repo-root ``./scripts/`` (Ralph tooling),
    and where ``creek-tools/pyproject.toml``'s package-find never puts
    ``creek-tools/scripts`` on ``sys.path``.

    Both the skill and the workflow prompt invoke it as
    ``creek-tools/scripts/verify-scan-citations.sh``, i.e. from the repo
    root, and ``_claude-scan.yml``'s scan job sets no
    ``working-directory``. So the gate was broken in its own documented
    usage --- and worse than absent: every citation, *including real
    ones*, came back a failure.

    Every test above missed it, because they all call ``main()`` through a
    Python import while pytest runs with CWD ``creek-tools``. Only running
    the shell wrapper as a subprocess, from the repo root, exercises what
    CI actually does.
    """

    def _run(self, findings: str) -> subprocess.CompletedProcess[str]:
        """Run the wrapper from the repo root, as CI does.

        Args:
            findings: Newline-delimited JSON findings for stdin.

        Returns:
            The completed process.
        """
        return subprocess.run(
            ["creek-tools/scripts/verify-scan-citations.sh"],
            cwd=_REPO_ROOT,
            input=findings,
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "SCAN_SHA": _SCAN_SHA,
                # sys.executable, never a hardcoded .venv path: CI provisions
                # with `uv pip install --system` and creates no
                # creek-tools/.venv (ci.yml's tests job), so pinning that path
                # would make every citation fail with "No such file or
                # directory" -- green locally, red in CI, and failing in
                # exactly the way this class exists to catch.
                "PYTHON": sys.executable,
            },
        )

    def test_a_real_symbol_passes_from_the_repo_root(self) -> None:
        """The documented invocation must verify a real name, not error."""
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py","symbol":"verify"}\n'
        )

        assert result.returncode == 0, (
            f"the wrapper failed on a REAL symbol from the repo root, so the "
            f"gate rejects every citation:\n{result.stdout}\n{result.stderr}"
        )
        assert "No module named" not in result.stdout + result.stderr
        assert "1 symbol citation(s) verified" in result.stdout

    def test_a_phantom_is_rejected_from_the_repo_root(self) -> None:
        """And it must still catch the thing it exists to catch."""
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py","symbol":"verify_chain"}\n'
        )

        assert result.returncode == 1
        assert "PHANTOM" in result.stdout + result.stderr

    def test_a_findings_list_reports_every_phantom(self) -> None:
        """A `symbols` list is checked element by element."""
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py",'
            '"symbols":["read","verify","iter_entries"]}\n'
        )

        assert result.returncode == 1
        assert "iter_entries" in result.stdout + result.stderr

    def test_an_unusable_interpreter_fails_instead_of_passing_silently(
        self,
    ) -> None:
        """A gate that cannot run must not report a clean pass.

        ``$PYTHON`` parses the findings JSON as well as running the
        resolver, so an unusable one parsed nothing, checked nothing and
        exited **0** --- indistinguishable from "every citation verified".
        Surfaced while proving the CI-path fix mattered.
        """
        result = subprocess.run(
            ["creek-tools/scripts/verify-scan-citations.sh"],
            cwd=_REPO_ROOT,
            input='{"file":"creek-tools/creek/audit/log.py","symbol":"verify"}\n',
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "SCAN_SHA": _SCAN_SHA,
                "PYTHON": "/nonexistent/python",
            },
        )

        assert result.returncode == 1, (
            "an unusable interpreter reported success; the gate is decorative"
        )
        assert "unusable" in result.stdout + result.stderr

    def test_findings_that_declare_no_symbol_are_announced(self) -> None:
        """Reading findings but checking nothing must not look like a pass."""
        result = subprocess.run(
            ["creek-tools/scripts/verify-scan-citations.sh"],
            cwd=_REPO_ROOT,
            input='{"file":"creek-tools/creek/audit/log.py"}\n',
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "SCAN_SHA": _SCAN_SHA,
                "PYTHON": sys.executable,
            },
        )

        assert result.returncode == 0
        assert "checked 0 symbols" in result.stdout + result.stderr

    def test_malformed_findings_json_is_a_hard_failure(self) -> None:
        """A payload that will not parse is a broken producer, not "no symbol".

        Degrading it to the same "checked 0" signal would hide it behind
        the benign case.
        """
        result = self._run("not json at all\n")

        assert result.returncode == 1
        assert "malformed findings JSON" in result.stdout + result.stderr

    def test_a_symbol_far_from_its_cited_lines_is_warned_about(self) -> None:
        """A real name in the wrong place still misdirects a reader.

        Warned rather than failed: a finding may legitimately cite a
        helper *called* from those lines rather than defined in them.
        """
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py","symbol":"verify",'
            '"lines":"145-150"}\n'
        )

        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "::warning::" in combined
        assert "_last_line" in combined

    def test_a_citation_pointing_where_it_claims_is_silent(self) -> None:
        """The location check must not fire on a correct citation."""
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py","symbol":"_last_line",'
            '"lines":"141-150"}\n'
        )

        assert result.returncode == 0
        assert "::warning::" not in result.stdout + result.stderr
