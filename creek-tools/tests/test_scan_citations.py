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

import io
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.scan_citations import (
    CitationError,
    MalformedFindingError,
    citations_from_body,
    extract_citations,
    main,
    resolve_enclosing_symbol,
    sha_from_body,
    verify_symbol,
)
from tests.shell_command_support import (
    agent_issue_filing_jobs,
    load_yaml,
    shell_tokens,
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
        """The workflow TEXT names the tool. That is all this proves.

        Kept, and deliberately narrowed in description rather than
        deleted: it still pins the defence-in-depth sentence in the
        ``prompt:`` block, and a rejected citation should never be filed
        in the first place.

        But read what it can see. The assertion is a raw substring scan
        over the whole file, and the only mention of the verifier in
        ``_claude-scan.yml`` before #1700 was **prose inside the
        ``prompt:`` block** — which satisfies this on its own. A sentence
        asking an agent to check itself and a `run:` step that checks it
        are indistinguishable here. The agent-independent guarantee is
        :class:`TestTheBackstopRunsOutsideTheAgent`; this is not it.
        """
        workflow = (
            _REPO_ROOT / ".github" / "workflows" / "_claude-scan.yml"
        ).read_text(encoding="utf-8")

        assert "verify-scan-citations.sh" in workflow, (
            "the scan workflow no longer names the citation verifier, so the "
            "agent is not even asked to reject a phantom symbol before filing"
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


_BACKSTOP = "creek-tools/scripts/recheck-filed-scan-citations.sh"
"""The agent-independent re-check, as a `run:` step would invoke it."""

_BACKSTOP_SCRIPT = _REPO_ROOT / _BACKSTOP
"""The same script as an absolute path, for the subprocess tests."""


def _runs_the_backstop(step: dict[str, Any]) -> bool:
    """Whether this step's ``run:`` body actually invokes the backstop.

    Comment-blind and tokenised via :func:`shell_tokens`, for the reason
    that helper exists: prose naming a command is never the command. The
    workflow's ``prompt:`` block names ``verify-scan-citations.sh`` in
    English, and a substring scan cannot tell that apart from a step that
    runs it.

    Args:
        step: One parsed GitHub Actions step mapping.

    Returns:
        ``True`` when a non-comment line of ``run:`` has the backstop as a
        shell token.
    """
    for raw in str(step.get("run", "")).splitlines():
        if raw.lstrip().startswith("#"):
            continue
        try:
            tokens = shell_tokens(raw)
        except ValueError:
            continue
        if _BACKSTOP in tokens:
            return True
    return False


def _assert_backstop_wiring(workflow_name: str, job_name: str) -> None:
    """Assert one job re-verifies filed citations outside the agent.

    Args:
        workflow_name: File name under ``.github/workflows``.
        job_name: Job id within that workflow.
    """
    document = load_yaml(_REPO_ROOT / ".github" / "workflows" / workflow_name)
    steps = document["jobs"][job_name]["steps"]
    assert steps, f"the {job_name} job has no steps; this test would be vacuous"

    agent = [
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("anthropics/claude-code-action")
    ]
    assert len(agent) == 1, (
        f"the {job_name} job no longer runs exactly one Claude action"
    )

    backstop = [index for index, step in enumerate(steps) if _runs_the_backstop(step)]
    assert backstop, (
        f"no step in the `{job_name}` job of {workflow_name} has a `run:` "
        "invoking recheck-filed-scan-citations.sh; the only mention of the "
        "citation verifier in this file is prose inside the `prompt:` block "
        "(line 239), which the agent may skip, error out of, or exhaust "
        "--max-turns 40 before reaching -- while the issues it already filed "
        "stay filed"
    )

    index = backstop[0]
    step = steps[index]
    assert index > agent[-1], "the backstop runs before the issues exist"
    assert "always()" in str(step.get("if", "")), (
        "the backstop is skipped exactly when it is needed -- the agent step "
        "failing is the scenario issue #1700 names"
    )
    assert step.get("continue-on-error") in (None, False)
    assert "${{" not in str(step["run"]), (
        "a workflow expression is substituted into the shell SOURCE"
    )
    assert "GH_TOKEN" in (step.get("env") or {}), (
        "the GH_TOKEN at line 201 is scoped to the claude-code-action step and "
        "does not reach a new run: step"
    )


_AGENT_ISSUE_FILING_DISPOSITIONS: dict[tuple[str, str], str] = {
    ("_claude-scan.yml", "scan"): "BACKSTOPPED",
    ("deslop.yml", "deslop"): "NO_CITATION_CONTRACT (#1708)",
    ("scan-groom.yml", "groom"): "CONSUMER_NOT_PRODUCER",
}
"""Every Claude job that can FILE an issue, and what guards its citations.

The defect #1700 names is a class, not one workflow: *the only enforcement
of a machine-checkable claim lives inside the prompt of the agent that
makes the claim*. The population is **derived** by
:func:`agent_issue_filing_jobs` -- runs ``anthropics/claude-code-action``
crossed with an effective ``issues: write`` -- so ``claude.yml`` and
``code-review.yml`` fall out because their token cannot create an issue,
not because anyone remembered to exclude them. A twelfth producer, or a
flip of an existing one to ``issues: write``, reddens the test below until
someone declares what guards it.

* ``BACKSTOPPED`` -- an agent-independent post-run ``run:`` step re-reads
  the issues it filed. Asserted structurally, not declared.
* ``NO_CITATION_CONTRACT`` -- files issues, has no symbol/SHA contract for
  a backstop to check yet. Scoped out of #1700 by its own Context and
  tracked as a real issue number rather than a promise in prose.
* ``CONSUMER_NOT_PRODUCER`` -- routes through the ``backlog-grooming``
  skill (``prompts/scans/groom.md``): it closes, dedupes and promotes, and
  creates nothing. It cites nothing, so there is nothing to backstop.
  ``test_backlog_ceiling_gate.py`` already pins it away from
  ``scan-issue-writer``, so a rewrite into a producer reddens rather than
  silently acquiring the gap.
"""


class TestTheBackstopRunsOutsideTheAgent:
    """Issue #1700. A prompt sentence is not a gate; a `run:` step is."""

    def test_the_scan_job_reverifies_filed_citations_in_a_run_step(self) -> None:
        """A prompt sentence is not a gate; a `run:` step is.

        The agent has Bash and ``--max-turns 40``. It can file issues and
        then error, or exhaust the budget, before it ever reaches the
        verification the prompt asks for -- and nothing downstream would
        know. Substring-scanning the workflow cannot see this: the prompt
        at line 239 already names the script.
        """
        _assert_backstop_wiring("_claude-scan.yml", "scan")

    def test_every_agent_issue_filing_job_declares_a_disposition(self) -> None:
        """A derived population, so the table cannot decay into fiction.

        Prose sweep tables in this repository go stale; a derived one
        cannot. The non-emptiness and the explicit membership check are
        not belt-and-braces: a helper that returned ``[]`` -- a renamed
        action, a YAML shape change -- would otherwise satisfy an
        equality against a table nobody noticed had emptied.
        """
        derived = agent_issue_filing_jobs()

        assert derived, (
            "no job was derived as able to file issues with a Claude agent; "
            "the population helper found nothing and this test is vacuous"
        )
        assert ("_claude-scan.yml", "scan") in derived, (
            "the job issue #1700 is about is not in its own population"
        )
        assert set(derived) == set(_AGENT_ISSUE_FILING_DISPOSITIONS), (
            "a Claude job that can file issues has appeared or moved; declare "
            "what guards its citations in _AGENT_ISSUE_FILING_DISPOSITIONS "
            f"(derived {sorted(derived)})"
        )

    def test_every_backstopped_job_runs_the_backstop_outside_the_agent(self) -> None:
        """`BACKSTOPPED` is asserted structurally, never taken on trust.

        Otherwise the table is just another prose claim, one edit away
        from saying a job is guarded when it is not.
        """
        backstopped = [
            job
            for job, disposition in _AGENT_ISSUE_FILING_DISPOSITIONS.items()
            if disposition == "BACKSTOPPED"
        ]

        assert backstopped, "no job claims to be backstopped; this test is vacuous"
        for workflow_name, job_name in backstopped:
            _assert_backstop_wiring(workflow_name, job_name)


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

    def test_a_phantom_error_names_what_the_cited_lines_really_hold(
        self,
    ) -> None:
        """The gate's own error must carry the remedy, not just the verdict.

        The resolver appends "the lines cited hold X" only when given
        ``--line``; the wrapper originally omitted it on the primary
        call, so the common case got the generic message and the richer
        one fired only on the separate location pass.
        """
        result = self._run(
            '{"file":"creek-tools/creek/audit/log.py","symbol":"verify_chain",'
            '"lines":"145-150"}\n'
        )

        combined = result.stdout + result.stderr
        assert result.returncode == 1
        assert "the lines cited hold '_last_line'" in combined

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


@dataclass
class _BackstopResult:
    """One run of the filed-issue backstop against a stub ``gh``.

    Attributes:
        returncode: The script's exit status.
        console: Combined stdout and stderr, for ``::error::`` and
            ``::warning::`` annotations.
        gh_argv: Every argv line the stub ``gh`` recorded. Empty means
            the stub was never reached, which makes any assertion about
            what was queried or commented vacuous -- so the tests check
            it either way, and say which way they expect.
    """

    returncode: int
    console: str
    gh_argv: str


def _write_backstop_stub_gh(
    bin_dir: Path, listing: str, list_exit: int, log: Path
) -> None:
    """Install a stub ``gh`` that answers `issue list` and logs every call.

    ``issue comment`` always exits 0 while ``issue list`` exits
    ``list_exit``: the failure under test is "the run cannot read back
    what it filed", and a stub that also failed the comment would blur
    that with "the correction could not be posted", which the script
    treats as a warning rather than a failure.

    An empty ``listing`` prints **nothing at all**, not a bare newline --
    the same distinction ``test_backlog_ceiling_gate.py::_write_stub_gh``
    records, for the same reason: a stray newline lets a fail-open bug be
    caught by the wrong assertion.

    Args:
        bin_dir: Directory prepended to ``PATH``.
        listing: JSON the stub prints for ``gh issue list``.
        list_exit: The status ``gh issue list`` exits with.
        log: File the stub appends its argv to.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    emit = f"printf '%s\\n' {shlex.quote(listing)}\n" if listing else ""
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then\n'
        f"{emit}"
        f"  exit {list_exit}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_backstop(
    tmp_path: Path,
    issues: list[dict[str, Any]] | str,
    *,
    issues_before: str = "",
    snapshot_ok: str | None = "true",
    scan_label: str = "scan:coverage",
    list_exit: int = 0,
    gh_override: bool = False,
) -> _BackstopResult:
    """Run the real backstop script against a stub ``gh``, as CI does.

    Args:
        tmp_path: Sandbox directory.
        issues: The ``gh issue list`` payload, as records or raw text.
        issues_before: ``ISSUES_BEFORE`` -- the pre-agent snapshot.
        snapshot_ok: ``ISSUES_SNAPSHOT_OK``; ``None`` leaves it unset.
        scan_label: ``SCAN_LABEL``.
        list_exit: Status the stub's ``issue list`` exits with.
        gh_override: Point ``GH`` at the stub by absolute path instead of
            relying on ``PATH`` resolution.

    Returns:
        The parsed result of the run.
    """
    assert _BACKSTOP_SCRIPT.is_file(), f"{_BACKSTOP} does not exist"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "gh_argv"
    log.touch()
    listing = issues if isinstance(issues, str) else json.dumps(issues)
    _write_backstop_stub_gh(bin_dir, listing, list_exit, log)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin:/usr/local/bin",
        # sys.executable, never a hardcoded .venv path: CI provisions with
        # `uv pip install --system` and creates no creek-tools/.venv.
        "PYTHON": sys.executable,
        "SCAN_LABEL": scan_label,
        "ISSUES_BEFORE": issues_before,
        "RUN_STARTED_AT": "2000-01-01T00:00:00Z",
        "SCAN_SHA": _SCAN_SHA,
        "HOME": str(tmp_path),
    }
    if snapshot_ok is not None:
        env["ISSUES_SNAPSHOT_OK"] = snapshot_ok
    if gh_override:
        env["GH"] = str(bin_dir / "gh")

    completed = subprocess.run(
        [str(_BACKSTOP_SCRIPT)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return _BackstopResult(
        returncode=completed.returncode,
        console=completed.stdout + completed.stderr,
        gh_argv=log.read_text(encoding="utf-8"),
    )


def _filed_issue(
    number: int, body: str, created: str = "2030-01-01T00:00:00Z"
) -> dict[str, Any]:
    """Build one ``gh issue list --json number,body,createdAt`` record.

    Args:
        number: Issue number.
        body: Issue body.
        created: Creation instant.

    Returns:
        The record.
    """
    return {"number": number, "body": body, "createdAt": created}


class TestTheFiledIssueBackstop:
    """Issue #1700. The gate that runs after the agent, on what it filed.

    Every case drives the real script as a subprocess from the repository
    root -- the CWD ``_claude-scan.yml``'s scan job actually has -- with a
    stub ``gh`` whose argv is recorded. A test that asserted on exit codes
    without checking that log would pass identically against a ``PATH``
    mistake that never invoked ``gh`` at all.
    """

    def test_a_run_that_filed_nothing_new_passes_without_commenting(
        self, tmp_path: Path
    ) -> None:
        """Every returned issue is in the snapshot, so none is this run's.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(
            tmp_path,
            [_filed_issue(1447, _fixture("issue-1447.md"))],
            issues_before="1447",
        )

        assert result.returncode == 0, result.console
        assert result.gh_argv.strip(), "the stub gh was never called"
        assert "issue comment" not in result.gh_argv
        assert "1 pre-existing" in result.console

    def test_an_empty_label_reports_differently_from_an_all_old_one(
        self, tmp_path: Path
    ) -> None:
        """ "Nothing filed" and "nothing new" must not read identically.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [])

        assert result.returncode == 0, result.console
        assert result.gh_argv.strip(), "the stub gh was never called"
        assert "0 pre-existing" in result.console

    def test_a_filed_issue_with_no_parseable_citation_fails(
        self, tmp_path: Path
    ) -> None:
        """A body nothing can check is not a body that checked out.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(
            tmp_path,
            [_filed_issue(9001, "## Context\n- Evidence: no citation bullet here.\n")],
        )

        assert result.returncode == 1, result.console
        assert "::error::" in result.console
        assert "not one carries a parseable" in result.console

    def test_a_phantom_in_a_filed_body_reddens_and_is_commented_on(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end case the issue exists for.

        ``_unique_filename`` is the real confabulation from #1447: it
        exists in no revision of ``writer.py``, having been paraphrased
        from ``_generate_filename``.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(
            tmp_path,
            [_filed_issue(9002, _SINGLE_SYMBOL_BODY.format(symbol="_unique_filename"))],
        )

        assert result.returncode == 1, result.console
        assert "#9002" in result.console
        assert "_unique_filename" in result.console
        assert "issue comment 9002 --body-file" in result.gh_argv

    def test_the_same_body_with_the_real_symbol_passes(self, tmp_path: Path) -> None:
        """The mirror case. Without it the gate could simply always fail.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(
            tmp_path,
            [
                _filed_issue(
                    9002, _SINGLE_SYMBOL_BODY.format(symbol="_generate_filename")
                )
            ],
        )

        assert result.returncode == 0, result.console
        assert "issue comment" not in result.gh_argv
        assert "re-verified 1 citation(s)" in result.console

    def test_a_failed_listing_is_a_hard_failure(self, tmp_path: Path) -> None:
        """A run that cannot read back what it filed has verified nothing.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [], list_exit=1)

        assert result.returncode == 1, result.console
        assert "::error::" in result.console
        assert "gh exited non-zero" in result.console
        assert result.gh_argv.strip(), "the stub gh was never called"

    def test_a_missing_snapshot_fails_rather_than_skips(self, tmp_path: Path) -> None:
        """The fail-open trap, refused explicitly.

        Conditioning the step's ``if:`` on the snapshot's conclusion --
        or degrading here to "check everything" -- would leave the job
        GREEN with zero citations verified. That is a gate reporting it
        did nothing, which is the failure class #1700 exists to close.
        The stub is deliberately NOT reached: refusing before spending a
        query is the point.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [], snapshot_ok=None)

        assert result.returncode == 1, result.console
        assert "::error::" in result.console
        assert "snapshot" in result.console
        assert not result.gh_argv.strip(), (
            "the backstop queried gh before establishing a baseline"
        )

    def test_a_snapshot_that_reports_failure_also_fails(self, tmp_path: Path) -> None:
        """``ok=false`` is not "unset"; both must redden.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [], snapshot_ok="false")

        assert result.returncode == 1, result.console
        assert "snapshot" in result.console

    def test_a_listing_at_the_limit_is_treated_as_truncated(
        self, tmp_path: Path
    ) -> None:
        """A full page may be a partial answer, and a partial answer lies.

        Truncation drops issues out of the snapshot's set difference, so
        an OLD issue looks newly filed -- and the script would comment a
        correction on somebody else's issue. Every one here is in the
        snapshot, so the run would otherwise be a clean no-op: only the
        truncation guard can redden it.

        Args:
            tmp_path: Sandbox directory.
        """
        numbers = list(range(1, 201))
        result = _run_backstop(
            tmp_path,
            [_filed_issue(n, "") for n in numbers],
            issues_before=",".join(str(n) for n in numbers),
        )

        assert result.returncode == 1, result.console
        assert "TRUNCATED" in result.console

    def test_an_invalid_scan_label_never_reaches_gh(self, tmp_path: Path) -> None:
        """The charset allowlist mirrors the workflow's scan_name guard.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [], scan_label="scan:x; rm -rf /")

        assert result.returncode == 1, result.console
        assert "invalid SCAN_LABEL" in result.console
        assert not result.gh_argv.strip(), "an unvalidated label reached gh"

    def test_an_unreachable_recorded_sha_falls_back_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """Checking a citation against a revision it never claimed is a lie.

        Under ``fetch-depth: 50`` a body's own SHA may be absent. The
        fallback is legitimate but must be announced, naming both
        revisions, or a reader cannot tell which one produced the verdict.

        Args:
            tmp_path: Sandbox directory.
        """
        body = _SINGLE_SYMBOL_BODY.format(symbol="_generate_filename").replace(
            "`c8c5131`", "`" + "0" * 40 + "`"
        )
        result = _run_backstop(tmp_path, [_filed_issue(9003, body)])

        assert result.returncode == 0, result.console
        assert "::warning::" in result.console
        assert "0000000000" in result.console
        assert _SCAN_SHA in result.console

    def test_the_gh_override_is_a_real_seam(self, tmp_path: Path) -> None:
        """``GH`` must select the binary, not merely be documented.

        A subprocess test under a restricted ``PATH`` is one export away
        from silently resolving the real ``gh``; the override makes the
        seam explicit.

        Args:
            tmp_path: Sandbox directory.
        """
        result = _run_backstop(tmp_path, [], gh_override=True)

        assert result.returncode == 0, result.console
        assert result.gh_argv.strip(), "the GH override did not select the stub"

    def test_a_missing_gh_fails_instead_of_reporting_a_clean_pass(
        self, tmp_path: Path
    ) -> None:
        """A backstop that cannot run must not look like a backstop that ran.

        Args:
            tmp_path: Sandbox directory.
        """
        completed = subprocess.run(
            [str(_BACKSTOP_SCRIPT)],
            cwd=_REPO_ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHON": sys.executable,
                "GH": "/nonexistent/gh",
                "SCAN_LABEL": "scan:coverage",
                "ISSUES_SNAPSHOT_OK": "true",
                "ISSUES_BEFORE": "",
                "SCAN_SHA": _SCAN_SHA,
                "HOME": str(tmp_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 1
        assert "not executable" in completed.stdout + completed.stderr

    def test_an_unusable_interpreter_fails_instead_of_passing_silently(
        self, tmp_path: Path
    ) -> None:
        """The same refusal ``verify-scan-citations.sh`` already makes.

        Args:
            tmp_path: Sandbox directory.
        """
        completed = subprocess.run(
            [str(_BACKSTOP_SCRIPT)],
            cwd=_REPO_ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHON": "/nonexistent/python",
                "SCAN_LABEL": "scan:coverage",
                "ISSUES_SNAPSHOT_OK": "true",
                "ISSUES_BEFORE": "",
                "SCAN_SHA": _SCAN_SHA,
                "HOME": str(tmp_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 1
        assert "unusable" in completed.stdout + completed.stderr


class TestFromIssuesMode:
    """`--from-issues` is the seam between `gh` and the existing verifier."""

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: str,
        *args: str,
    ) -> int:
        """Feed *payload* on stdin and run the CLI.

        Args:
            monkeypatch: Used to feed stdin.
            payload: Raw stdin text.
            args: Extra command-line arguments.

        Returns:
            The exit code.
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        return main(["--from-issues", *args])

    def test_records_are_typed_and_carry_the_verifier_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The JSON payload must be what `extract_citations` consumes.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stdout.
        """
        payload = json.dumps(
            [_filed_issue(7, _SINGLE_SYMBOL_BODY.format(symbol="_generate_filename"))]
        )

        assert self._run(monkeypatch, payload, "--default-sha", "deadbee") == 0

        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "RETURNED\t1"
        assert lines[1] == "ISSUES\t1"
        kind, number, sha, blob = lines[2].split("\t")
        assert (kind, number, sha) == ("CITATION", "7", "c8c5131")
        assert extract_citations(blob) == [
            ("creek-tools/creek/vault/writer.py", "_generate_filename", "1929-1936")
        ]

    def test_a_body_recording_no_sha_falls_back_to_the_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The run's own SHA is the fallback, never "skip the citation".

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stdout.
        """
        body = _SINGLE_SYMBOL_BODY.format(symbol="_generate_filename").replace(
            "- Scanned at commit: `c8c5131` — re-verify against HEAD before starting\n",
            "",
        )
        payload = json.dumps([_filed_issue(7, body)])

        assert self._run(monkeypatch, payload, "--default-sha", "deadbee") == 0
        assert "CITATION\t7\tdeadbee\t" in capsys.readouterr().out

    def test_excluded_numbers_are_dropped_from_the_selection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The snapshot is the selector, and it is a set difference.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stdout.
        """
        payload = json.dumps(
            [
                _filed_issue(7, _SINGLE_SYMBOL_BODY.format(symbol="f")),
                _filed_issue(8, ""),
            ]
        )

        assert self._run(monkeypatch, payload, "--exclude", "7, 8") == 0

        out = capsys.readouterr().out
        assert "RETURNED\t2" in out
        assert "ISSUES\t0" in out
        assert "CITATION" not in out

    def test_an_older_issue_is_skipped_with_a_notice(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The date filter is secondary, and it says when it fires.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures output.
        """
        payload = json.dumps(
            [
                _filed_issue(
                    7,
                    _SINGLE_SYMBOL_BODY.format(symbol="f"),
                    created="1999-01-01T00:00:00Z",
                )
            ]
        )

        assert self._run(monkeypatch, payload, "--created-after", "2030-01-01") == 0

        captured = capsys.readouterr()
        assert "ISSUES\t0" in captured.out
        assert "::notice::issue #7" in captured.err

    @pytest.mark.parametrize(
        ("payload", "args", "reason"),
        [
            pytest.param("not json", (), "unparseable", id="not-json"),
            pytest.param('{"number": 1}', (), "an object, not an array", id="object"),
            pytest.param("[1, 2]", (), "not issue records", id="array-of-ints"),
            pytest.param("[]", ("--exclude", "7,nine"), "bad baseline", id="exclude"),
        ],
    )
    def test_malformed_input_exits_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        payload: str,
        args: tuple[str, ...],
        reason: str,
    ) -> None:
        """Every one of these would otherwise verify zero citations quietly.

        A malformed ``--exclude`` is the subtlest: an empty baseline makes
        every pre-existing issue look newly filed, so the run comments
        corrections on other people's issues.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stderr.
            payload: Raw stdin text.
            args: Extra command-line arguments.
            reason: Why it is malformed, for the failure message.
        """
        assert self._run(monkeypatch, payload, *args) == 2, reason
        assert "UNCHECKABLE" in capsys.readouterr().err


class TestExtractCitations:
    """Findings parsing is a unit, not an inline shell heredoc.

    Moved out of the wrapper so the malformed cases are directly testable
    --- and so the shell stays free of an embedded program.
    """

    def test_a_single_symbol_is_extracted_with_its_lines(self) -> None:
        """The common shape."""
        assert extract_citations('{"file":"a.py","symbol":"f","lines":"1-2"}') == [
            ("a.py", "f", "1-2")
        ]

    def test_a_symbols_list_yields_one_triple_each(self) -> None:
        """`symbols` is the plural spelling the schema also allows."""
        assert extract_citations('{"file":"a.py","symbols":["f","g"]}') == [
            ("a.py", "f", ""),
            ("a.py", "g", ""),
        ]

    def test_a_finding_with_no_symbol_is_empty_not_an_error(self) -> None:
        """A whole-module or config finding legitimately names none."""
        assert extract_citations('{"file":"a.py"}') == []

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json at all", id="unparseable"),
            pytest.param("[1,2,3]", id="list"),
            pytest.param("null", id="null"),
            pytest.param('"a string"', id="string"),
            pytest.param("42", id="int"),
        ],
    )
    def test_anything_that_is_not_an_object_is_malformed(
        self,
        payload: str,
    ) -> None:
        """Parses-but-wrong-shape must not degrade into "declared no symbol".

        A bare list or ``null`` previously reached ``.get`` and raised
        ``AttributeError``, so the citations went unchecked with no
        sentinel — the same silent-pass failure this module refuses
        everywhere else.

        Args:
            payload: A findings line that is not a JSON object.
        """
        with pytest.raises(MalformedFindingError):
            extract_citations(payload)


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scan_issue_bodies"
"""Filed issue bodies, fetched byte-for-byte with ``gh issue view``.

Not retyped. The em-dashes, the ``(`:207-224`)`` parentheses and the
two-space continuation indents ARE the contract the parser must meet: a
hand-tidied copy would quietly test a shape no scan has ever filed.
"""


_FIXTURE_SHA_NAMES = (
    "issue-1449.md",
    "issue-1447.md",
    "issue-993.md",
    "issue-1177.md",
    "issue-869.md",
)
"""The five real bodies, which between them record four distinct revisions."""


def _fixture(name: str) -> str:
    """Read one committed issue-body fixture.

    Args:
        name: File name under ``tests/fixtures/scan_issue_bodies``.

    Returns:
        The body text.
    """
    path = _FIXTURES / name
    assert path.is_file(), f"{path} is missing; the parser tests would be vacuous"
    return path.read_text(encoding="utf-8")


_SINGLE_SYMBOL_BODY = """\
## Context
- File(s): `creek-tools/creek/vault/writer.py:1929-1936`
- Symbol(s): `{symbol}` — verified present at the scan SHA
- Scanned at commit: `c8c5131` — re-verify against HEAD before starting
- Evidence: purpose-built fixture for the end-to-end backstop test.

## Output Format
A single PR.
"""
"""A body citing exactly ONE symbol, for the phantom/real mirror pair.

Purpose-built, and that is deliberate. No real filed body has exactly one
citation: #1447 carries four phantoms, #1449 one of nine, #993 three of
four -- so "swap the phantom for the real name and assert exit 0" is
unachievable against any of them. Real bodies are the parser's fixtures,
where the assertion is the extracted citation LIST; this one is the
verifier's, where the assertion is an exit code.
"""


class TestCitationsFromAFiledIssueBody:
    """Issue #1700. The backstop reads what was filed, not what was promised.

    Two shapes are live at once. The template grew a ``- Symbol(s):``
    bullet in 8160ed0 (HEAD~1 when #1700 was written) and no scan has run
    since, so **every** issue in the backlog still carries the older
    inline ``- File(s):`` shape. A parser tested only against the template
    would parse nothing that actually exists.
    """

    def test_the_forward_looking_template_shape_yields_one_citation(self) -> None:
        """`File(s)` supplies path and lines; `Symbol(s)` supplies the name."""
        assert citations_from_body(_fixture("template-forward.md")) == [
            ("path/to/file.py", "enclosing_function_name", "120-164")
        ]

    def test_1449_yields_every_backticked_symbol_in_a_four_line_bullet(self) -> None:
        """Nine symbols across a wrapped bullet, not just the first.

        A parser that stopped at the first clause would pass a test
        written to match it and check one citation in nine.
        """
        assert citations_from_body(_fixture("issue-1449.md")) == [
            ("creek-tools/creek/classify/llm/providers.py", "_require_env", "133-134"),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_extract_anthropic_usage",
                "612",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_fetch_attestation_quote",
                "850",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_extract_openai_text",
                "1316",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_map_openai_stop_reason",
                "1333",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_extract_openai_usage",
                "1352",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_extract_gemini_text",
                "1563",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_map_gemini_stop_reason",
                "1589",
            ),
            (
                "creek-tools/creek/classify/llm/providers.py",
                "_extract_gemini_usage",
                "1610",
            ),
        ]

    def test_1449_invents_no_citation_for_its_unbackticked_prose_clause(self) -> None:
        """``enclave payload options (`:981`, `:983`)`` must yield nothing.

        The clause has line numbers and no backticked name. Manufacturing
        a symbol for it -- by reaching for the nearest name, as the
        original confabulation did -- would put a phantom into the very
        pipeline that exists to refuse them.
        """
        citations = citations_from_body(_fixture("issue-1449.md"))

        assert not [lines for _, _, lines in citations if lines in {"981", "983"}]
        assert "enclave" not in {symbol for _, symbol, _ in citations}

    def test_993_reads_both_token_orders_and_the_inherited_path(self) -> None:
        """The range precedes the name here, and only clause one names a path."""
        assert citations_from_body(_fixture("issue-993.md")) == [
            ("creek-tools/creek/audit/log.py", "_read_last_line", "149-157"),
            ("creek-tools/creek/audit/log.py", "iter_entries", "266-277"),
            ("creek-tools/creek/audit/log.py", "verify_chain", "289-296"),
            ("creek-tools/creek/audit/log.py", "_verify_line", "313-320"),
        ]

    def test_1177_cites_only_line_ranges_and_that_is_not_an_error(self) -> None:
        """``:120-121`` is a range, never an identifier.

        The trap: a span that looks token-shaped but is a line span. Read
        as a symbol it becomes a guaranteed phantom, reddening a run for
        a citation that was fine.
        """
        assert citations_from_body(_fixture("issue-1177.md")) == []

    def test_1447_yields_all_eight_symbols_with_their_ranges(self) -> None:
        """The range follows the name here, and one clause is range-only."""
        assert citations_from_body(_fixture("issue-1447.md")) == [
            ("creek-tools/creek/vault/writer.py", "_read_legacy_provenance", "207-224"),
            (
                "creek-tools/creek/vault/writer.py",
                "_migrate_legacy_provenance",
                "295-308",
            ),
            ("creek-tools/creek/vault/writer.py", "_render_decision_body", "362"),
            ("creek-tools/creek/vault/writer.py", "_atomic_write", "414-415"),
            ("creek-tools/creek/vault/writer.py", "find_fragment_file", "941"),
            ("creek-tools/creek/vault/writer.py", "_rebuild_index", "1747"),
            ("creek-tools/creek/vault/writer.py", "_unique_filename", "1929-1936"),
            ("creek-tools/creek/vault/writer.py", "_log_provenance", "1980-1981"),
        ]

    def test_869_cites_a_whole_module_and_names_no_symbol(self) -> None:
        """A whole-module finding legitimately cites none -- and is not an error."""
        assert citations_from_body(_fixture("issue-869.md")) == []

    @pytest.mark.parametrize(
        ("path", "symbol"),
        [
            pytest.param(".github/workflows/_claude-scan.yml", "scan", id="yaml-job"),
            pytest.param("creek-tools/scripts/backlog-gate.sh", "fail", id="shell-fn"),
            pytest.param("creek-tools/pyproject.toml", "tool", id="toml-table"),
        ],
    )
    def test_a_non_python_path_yields_no_symbol_citation(
        self,
        path: str,
        symbol: str,
    ) -> None:
        """A non-Python citation must never reach the ast resolver.

        Every name here is a perfectly good identifier -- a job id, a
        shell function, a TOML table -- which is the whole point: the
        gate has to be the FILE's extension, not the name's shape.

        ``ast.parse`` on a YAML, shell or TOML blob raises, the resolver
        turns that into ``CitationError`` and exits 2, and
        ``verify-scan-citations.sh`` counts any non-zero as a phantom --
        so an ungated parser reddens a run for a **correct** citation.

        Args:
            path: A non-Python file the issue cites.
            symbol: An identifier-shaped name inside it.
        """
        body = (
            "## Context\n"
            f"- File(s): `{path}:195-201`\n"
            f"- Symbol(s): `{symbol}` — verified present at the scan SHA\n"
            "- Scanned at commit: `c8c5131`\n"
        )

        assert citations_from_body(body) == []

    def test_the_same_body_with_a_python_path_does_yield_the_citation(self) -> None:
        """The mirror of the case above, so the gate is not just "never".

        Without this, a parser that returned nothing at all would satisfy
        the non-Python cases perfectly.
        """
        body = (
            "## Context\n"
            "- File(s): `creek-tools/creek/audit/log.py:195-201`\n"
            "- Symbol(s): `scan` — verified present at the scan SHA\n"
            "- Scanned at commit: `c8c5131`\n"
        )

        assert citations_from_body(body) == [
            ("creek-tools/creek/audit/log.py", "scan", "195-201")
        ]

    def test_a_bare_file_name_is_never_read_as_a_symbol(self) -> None:
        """``README.md`` beside a `.py` path must not become a citation.

        Found in pre-push review of this change, and it is the parser's
        own worst failure mode rather than a cosmetic one: the span is
        identifier-shaped, so it reached the SYMBOL branch; then
        ``verify_symbol`` strips to the last dotted segment and hunts for
        a definition named ``md``, which exists in no file. The result is
        a **correct** issue reported as citing a phantom, a red scan run,
        and an automated correction comment posted on it.
        """
        body = (
            "## Context\n"
            "- File(s): `creek-tools/creek/vault/writer.py:100-120`, `README.md`\n"
            "- Scanned at commit: `c8c5131`\n"
        )

        assert citations_from_body(body) == []

    def test_a_bare_file_name_clears_the_path_rather_than_guessing(self) -> None:
        """After an unresolvable file reference, later names are dropped.

        The alternative -- keeping the previous path in force -- would
        attribute ``some_helper`` to ``writer.py`` when the issue plainly
        meant it to belong to whatever ``helpers.py`` is. Guessing which
        file a name lives in is how a phantom gets manufactured.
        """
        body = (
            "## Context\n"
            "- File(s): `creek-tools/creek/vault/writer.py:100-120` — `_atomic_write`,"
            " `helpers.py` — `some_helper`\n"
            "- Scanned at commit: `c8c5131`\n"
        )

        assert citations_from_body(body) == [
            ("creek-tools/creek/vault/writer.py", "_atomic_write", "100-120")
        ]

    def test_symbols_are_dropped_when_the_files_bullet_names_two_modules(self) -> None:
        """A guess about WHICH file a name lives in manufactures a phantom.

        Pinned by its own test because the rule reads like an omission:
        the next reader "fixes" it by attaching the names to the first
        path, and the parser starts inventing citations.
        """
        body = (
            "## Context\n"
            "- File(s): `creek-tools/creek/audit/log.py:10-20`, "
            "`creek-tools/creek/vault/writer.py:30-40`\n"
            "- Symbol(s): `verify`, `_generate_filename`\n"
            "- Scanned at commit: `c8c5131`\n"
        )

        assert citations_from_body(body) == []

    @pytest.mark.parametrize(
        ("fixture", "prefix", "length"),
        [
            ("issue-1449.md", "c8c5131b", 40),
            ("issue-1447.md", "c8c5131b", 40),
            ("issue-993.md", "317ea3d9", 40),
            ("issue-1177.md", "a5809aa5", 40),
            ("issue-869.md", "82f9b89", 7),
        ],
    )
    def test_the_recorded_scan_sha_is_read_from_the_body(
        self,
        fixture: str,
        prefix: str,
        length: int,
    ) -> None:
        """Each body's citations are checked at ITS OWN revision, not HEAD.

        Asserted as prefix plus exact length rather than one 40-character
        literal per row. The length is the load-bearing half: a parser
        that truncated, padded or normalised the recorded value would
        still satisfy the prefix, and a truncated SHA sent to
        ``git cat-file`` resolves to a *different* revision or to nothing.
        #869 records a **7-character** SHA, so a 40-character floor would
        silently fall back to the run's SHA and check the citation against
        the wrong revision entirely.

        (Writing the full SHAs out here would also trip ``detect-secrets``
        as high-entropy hex, and the honest fix for a false positive is
        not to write the string, not to annotate around the scanner.)

        Args:
            fixture: Fixture file name.
            prefix: The opening characters of the SHA that body records.
            length: The exact length of the recorded SHA.
        """
        recorded = sha_from_body(_fixture(fixture))

        assert recorded is not None, "the body records a SHA and the parser lost it"
        assert len(recorded) == length
        assert recorded.startswith(prefix)

    def test_bodies_from_the_same_scan_run_agree_and_others_differ(self) -> None:
        """Distinct revisions must not collapse onto one another.

        #1447 and #1449 were filed by the same run and record the same
        SHA; #993 and #1177 were filed by different runs. A parser
        returning a constant -- or the first SHA it ever saw -- passes
        every per-fixture assertion above and fails here.
        """
        shas = {name: sha_from_body(_fixture(name)) for name in _FIXTURE_SHA_NAMES}

        assert shas["issue-1447.md"] == shas["issue-1449.md"]
        assert len(set(shas.values())) == 4, f"expected four distinct SHAs: {shas}"

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            pytest.param("## Context\n- File(s): `a/b.py`\n", "absent", id="no-line"),
            pytest.param(
                _fixture.__doc__ or "", "not a body", id="prose-without-the-line"
            ),
        ],
    )
    def test_a_body_recording_no_sha_yields_none(self, body: str, reason: str) -> None:
        """No SHA is None, so the caller falls back deliberately, not silently.

        Args:
            body: An issue body with no ``Scanned at commit:`` line.
            reason: Why it has none, for the failure message.
        """
        assert sha_from_body(body) is None, reason

    def test_the_template_placeholder_is_not_mistaken_for_a_sha(self) -> None:
        """``<SHA>`` left unfilled must not parse as a revision."""
        assert sha_from_body(_fixture("template-forward.md")) is None


class TestExtractModeCLI:
    """`--extract` is the entry point the shell wrapper actually calls."""

    def test_extract_prints_tab_separated_triples(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Blank lines are skipped; each symbol gets its own row.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stdout.
        """
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                '{"file":"a.py","symbol":"f","lines":"1-2"}\n'
                "\n"
                '{"file":"b.py","symbols":["g","h"]}\n'
            ),
        )

        assert main(["--extract"]) == 0
        assert capsys.readouterr().out.splitlines() == [
            "a.py\tf\t1-2",
            "b.py\tg\t",
            "b.py\th\t",
        ]

    def test_extract_reports_a_malformed_line_and_exits_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One bad line taints the run but does not hide the good ones.

        Args:
            monkeypatch: Used to feed stdin.
            capsys: Captures stdout.
        """
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO('[1,2,3]\n{"file":"a.py","symbol":"f"}\n'),
        )

        assert main(["--extract"]) == 2
        out = capsys.readouterr().out
        assert "MALFORMED" in out
        assert "a.py\tf\t" in out

    def test_extract_needs_neither_sha_nor_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """They are required for the other modes, not this one.

        Args:
            monkeypatch: Used to feed stdin.
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))

        assert main(["--extract"]) == 0

    def test_the_other_modes_still_require_sha_and_path(self) -> None:
        """Making them optional must not make them skippable."""
        with pytest.raises(SystemExit):
            main(["--symbol", "f"])

    def test_unparseable_source_is_uncheckable_not_a_phantom(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A blob that will not parse exits 2, never 1.

        Reporting broken source as a phantom would send someone hunting
        for a symbol that may well be there.

        Args:
            tmp_path: Pytest-provided temporary directory.
            capsys: Captures stderr.
        """
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
            )

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "broken.py").write_text("def (((\n", encoding="utf-8")
        git("add", "broken.py")
        git("commit", "-qm", "broken")

        code = main(
            [
                "--repo",
                str(repo),
                "--sha",
                "HEAD",
                "--path",
                "broken.py",
                "--symbol",
                "anything",
            ]
        )

        assert code == 2
        assert "UNCHECKABLE" in capsys.readouterr().err
