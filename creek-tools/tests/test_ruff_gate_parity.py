"""Local and CI ruff must run the same rules, with no cache in between.

Issue #1119. ``test_ruff_cache_poisoning.py`` demonstrates the failure
behaviourally, one script invocation at a time. This module pins the static
contract, which catches what a behavioural suite structurally cannot: a
*partial* revert that drops ``--no-cache`` from, say, only the ``--fix``
path of ``format.sh``, or from the two pre-commit hooks, leaving the
behavioural tests green while Gate 2 quietly goes back to answering from a
cache CI does not have.

"Gate-relevant" here means rule configuration, target scope, mutation and
cache-freedom -- not output formatting:

* Cosmetic, and free to differ between local and CI: ``--output-format=*``,
  ``--exit-non-zero-on-fix``, ``--quiet`` / ``-q``, ``--no-cache``.
* Gate-relevant, and therefore either absent from a gate line or identical
  on both sides: ``--select``, ``--extend-select``, ``--ignore``,
  ``--extend-ignore``, ``--per-file-ignores``, ``--config``, ``--isolated``,
  ``--exclude``, ``--extend-exclude``, ``--preview`` / ``--no-preview``,
  ``--fix``, ``--cache-dir``.

``.github/workflows/ci.yml`` runs ruff **inline** rather than through
``scripts/lint.sh`` / ``scripts/format.sh`` -- one ``ruff check`` step and
one ``ruff format --check`` step. The rule-configuration and no-autofix
tests below therefore intentionally pin that "exactly one inline CI ruff
line per mode" shape. The follow-up that switches CI over to calling the
scripts must update this module as part of that change: these assertions
record what CI does today, they are not an argument that it should stay
inline.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING

from tests.shell_command_support import (
    PRE_COMMIT_CONFIG,
    SCRIPTS_DIR,
    all_workflow_steps,
    ci_steps,
    command_lines,
    load_yaml,
    non_comment_lines,
    shell_tokens,
    step_run_lines,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_GATE_SCRIPTS = ("lint.sh", "format.sh")

_RUFF_LINE = re.compile(r"^ruff\s")
_IF_LINE = re.compile(r"^if .*; then$")
# The mode switch of each gate script: ``lint.sh`` branches on ``$FIX``,
# ``format.sh`` on ``$CHECK``.
_MODE_IF = re.compile(r"^if \$(FIX|CHECK); then$")

# Shell condition -> (branch entered when true, branch of the ``else`` arm).
_MODE_BRANCHES = {"FIX": ("fix", "check"), "CHECK": ("check", "fix")}

# Tokens after which the rest of the line is no longer part of the ruff
# argv: ``format.sh`` spells every call as ``ruff … || { echo …; exit 1; }``.
_SHELL_OPERATORS = frozenset({"||", "&&", "|", ";", "&"})

# Flags whose value is a separate following token, so that value must not
# be mistaken for a positional target.
_VALUE_FLAGS = frozenset(
    {
        "--output-format",
        "--select",
        "--extend-select",
        "--ignore",
        "--extend-ignore",
        "--per-file-ignores",
        "--config",
        "--exclude",
        "--extend-exclude",
        "--cache-dir",
        "--target-version",
        "--line-length",
    }
)

# Differences that do not change which code is accepted.
_COSMETIC_FLAGS = frozenset(
    {"--output-format", "--exit-non-zero-on-fix", "--quiet", "-q", "--no-cache"}
)
# Flags that change the rule set or the file set, i.e. that stop the gate
# taking its configuration from creek-tools/pyproject.toml.
_RULE_CONFIG_FLAGS = frozenset(
    {
        "--select",
        "--extend-select",
        "--ignore",
        "--extend-ignore",
        "--per-file-ignores",
        "--config",
        "--isolated",
        "--exclude",
        "--extend-exclude",
        "--cache-dir",
    }
)
_PREVIEW_FLAGS = frozenset({"--preview", "--no-preview"})
_MUTATION_FLAGS = frozenset({"--fix"})


class _ModeBranchTracker:
    """Tracks which mode branch of a gate script the current line sits in.

    ``lint.sh`` branches on ``if $FIX; then … else … fi`` and ``format.sh``
    on ``if $CHECK; then … else … fi``. Both nest ``if $VERBOSE`` blocks
    inside those arms, so the tracker counts ``if``/``fi`` depth and reacts
    only to the ``else``/``fi`` that close the mode branch itself.
    """

    def __init__(self) -> None:
        """Start outside any mode branch."""
        self.depth = 0
        self.open_depth = -1
        self.branch: str | None = None
        self.branches: tuple[str, str] | None = None

    def feed(self, line: str) -> str | None:
        """Consume one stripped script line and report the branch it is in.

        Args:
            line: A stripped, non-comment line of the script.

        Returns:
            ``"check"``, ``"fix"``, or ``None`` outside the mode branch.
        """
        if _IF_LINE.match(line):
            return self._open(line)
        if line == "else":
            return self._toggle()
        if line == "fi":
            return self._close()
        return self.branch

    def _open(self, line: str) -> str | None:
        """Enter an ``if`` block, opening the mode branch if this is it."""
        self.depth += 1
        match = _MODE_IF.match(line)
        if match is not None and self.branches is None:
            self.branches = _MODE_BRANCHES[match.group(1)]
            self.branch = self.branches[0]
            self.open_depth = self.depth
        return self.branch

    def _toggle(self) -> str | None:
        """Swap to the other arm, if this ``else`` closes the mode branch."""
        if self.branches is not None and self.depth == self.open_depth:
            self.branch = self.branches[1]
        return self.branch

    def _close(self) -> str | None:
        """Leave an ``if`` block, closing the mode branch if this ends it."""
        if self.depth == self.open_depth:
            self.branch = None
            self.branches = None
            self.open_depth = -1
        self.depth -= 1
        return self.branch


def _mode_branch_ruff_lines(script: Path) -> dict[str, list[str]]:
    """Group ``script``'s ruff command lines by the mode branch they sit in.

    Args:
        script: A gate script that branches on ``$FIX`` or ``$CHECK``.

    Returns:
        ``{"check": [...], "fix": [...]}`` -- the ruff lines a ``--check``
        run executes, and the ones a mutating run executes.
    """
    grouped: dict[str, list[str]] = {"check": [], "fix": []}
    tracker = _ModeBranchTracker()
    for raw_line in non_comment_lines(script):
        line = raw_line.strip()
        branch = tracker.feed(line)
        if branch is not None and _RUFF_LINE.match(line):
            grouped[branch].append(line)
    return grouped


def _ruff_argv(command: str) -> list[str]:
    """Return the ruff argument vector at the head of ``command``.

    Everything from the first shell operator or redirection onwards is
    dropped, so the ``|| { echo …; exit 1; }`` tail that ``format.sh``
    appends to each call is not mistaken for ruff arguments.

    Args:
        command: A single shell command line beginning with ``ruff``.

    Returns:
        The tokens ruff itself receives.
    """
    argv: list[str] = []
    for token in shell_tokens(command):
        if token in _SHELL_OPERATORS or token.startswith(">"):
            break
        argv.append(token)
    return argv


def _flag_names(argv: list[str]) -> set[str]:
    """Return the flag names in ``argv``, with any ``=value`` suffix dropped.

    Args:
        argv: A ruff argument vector from :func:`_ruff_argv`.

    Returns:
        Flag names such as ``{"--output-format", "--exit-non-zero-on-fix"}``.
    """
    return {token.partition("=")[0] for token in argv if token.startswith("-")}


def _has_flag(command: str, flag: str) -> bool:
    """Return whether ``command``'s ruff argv carries ``flag``.

    Args:
        command: A single shell command line beginning with ``ruff``.
        flag: The exact flag name to look for.

    Returns:
        ``True`` if the flag is present as its own token.
    """
    return flag in _flag_names(_ruff_argv(command))


def _targets(argv: list[str]) -> list[str]:
    """Return the positional targets of a ruff argv.

    The program name and the subcommand are dropped, as is the value of any
    flag spelled ``--flag value`` rather than ``--flag=value``.

    Args:
        argv: A ruff argument vector of the form ``["ruff", "<sub>", …]``.

    Returns:
        The positional path arguments, in order.
    """
    targets: list[str] = []
    skip = False
    for token in argv[2:]:
        if skip:
            skip = False
        elif token.startswith("-"):
            skip = token in _VALUE_FLAGS
        else:
            targets.append(token)
    return targets


def _local_ruff_lines() -> list[str]:
    """Return every non-comment ruff command line in the two gate scripts.

    Returns:
        The stripped command lines, ``lint.sh`` first.
    """
    lines: list[str] = []
    for script in _GATE_SCRIPTS:
        lines.extend(command_lines(SCRIPTS_DIR / script, r"^\s*ruff\s"))
    return lines


def _ci_ruff_lines() -> list[str]:
    """Return every ruff command line in the root CI workflow's run blocks.

    Returns:
        The stripped command lines, in workflow order.
    """
    return step_run_lines(ci_steps(), r"^ruff\s")


def _is_checking_ruff_check(line: str) -> bool:
    """Return whether ``line`` is the gate's plain, whole-tree ``ruff check``.

    The gate scripts also spell a mutating ``ruff check … --fix`` and an
    import-sorting ``ruff check --select I …``; neither is the line CI's
    inline ``ruff check`` corresponds to.

    Args:
        line: A stripped shell command line from a gate script.

    Returns:
        ``True`` for the non-mutating, unnarrowed ``ruff check`` line.
    """
    return (
        line.startswith("ruff check ")
        and not _has_flag(line, "--fix")
        and not _has_flag(line, "--select")
    )


def _single_gate_line(
    lines: list[str], predicate: Callable[[str], bool], description: str
) -> str:
    """Return the one line of ``lines`` matching ``predicate``.

    The gate-parity assertions compare one local line against one CI line,
    so "exactly one match" is part of the contract, not a convenience: two
    matches would mean the comparison silently ignores a second gate line,
    and zero would make it vacuous.

    Args:
        lines: Candidate command lines.
        predicate: Test applied to each line.
        description: Human-readable name of the line being looked for,
            used in the failure message.

    Returns:
        The single matching line.
    """
    matching = [line for line in lines if predicate(line)]
    assert len(matching) == 1, f"expected exactly one {description}, got {matching!r}"
    return matching[0]


def _assert_same_rule_configuration(local: str, ci: str, label: str) -> None:
    """Assert a local gate line and its CI twin accept exactly the same code.

    Both sides must take their rule set from ``creek-tools/pyproject.toml``:
    neither may narrow it on the command line, both must target the whole
    tree, they must agree on preview rules, and the only flags they may
    disagree on are cosmetic ones.

    Args:
        local: The gate line from ``scripts/``.
        ci: The corresponding inline line from ``ci.yml``.
        label: Human-readable name of the gate, used in failure messages.
    """
    local_argv = _ruff_argv(local)
    ci_argv = _ruff_argv(ci)
    for source, line, argv in (
        ("scripts/", local, local_argv),
        ("ci.yml", ci, ci_argv),
    ):
        narrowing = sorted(_flag_names(argv) & _RULE_CONFIG_FLAGS)
        assert not narrowing, (
            f"{source} {label} narrows the rule set with {narrowing!r} instead "
            f"of taking it from pyproject.toml: {line!r}"
        )
        assert _targets(argv) == ["."], (
            f"{source} {label} no longer checks the whole tree: {line!r}"
        )

    local_flags = _flag_names(local_argv)
    ci_flags = _flag_names(ci_argv)
    assert (local_flags & _PREVIEW_FLAGS) == (ci_flags & _PREVIEW_FLAGS), (
        f"{label} disagrees on preview rules between local and CI: {local!r} vs {ci!r}"
    )
    difference = sorted((local_flags ^ ci_flags) - _COSMETIC_FLAGS)
    assert not difference, (
        f"{label} differs between local and CI by non-cosmetic flag(s) "
        f"{difference!r}: {local!r} vs {ci!r}"
    )


def test_every_local_ruff_invocation_is_cache_free() -> None:
    """Every ruff call in the gate scripts must pass ``--no-cache`` (#1119).

    Ruff's per-file cache key is mtime-only, so a content change that leaves
    the mtime alone leaves a cached verdict standing. CI has no cache and
    re-reads the file, which is how ``check-all.sh`` can exit 0 on a tree CI
    rejects. Every invocation is covered, not just the checking ones: a
    ``--fix`` run that answers from a poisoned cache silently declines to
    fix the file it was asked to fix.

    The flag must sit in ruff's own argv, not merely somewhere on the line:
    a mention in the ``|| { echo …; exit 1; }`` tail, or in a trailing
    comment, does not make the invocation cache-free.
    """
    lines = _local_ruff_lines()
    assert len(lines) >= 6, (
        "expected at least the six ruff invocations of scripts/lint.sh (2) "
        f"and scripts/format.sh (4); found {lines!r}"
    )
    for line in lines:
        assert _has_flag(line, "--no-cache"), (
            "this ruff invocation may answer from .ruff_cache, which CI never "
            f"has, so Gate 2 can pass a tree CI rejects (#1119): {line!r}"
        )


def test_a_commented_flag_cannot_satisfy_the_cache_free_gate() -> None:
    """Prose naming ``--no-cache`` must not count as passing ``--no-cache``.

    The gate above asks whether each ruff invocation carries the flag. If
    tokenisation kept inline comments, an edit that dropped the real flag
    but left a comment mentioning it would keep that gate green -- a check
    that cannot fail, which is the exact defect class of issue #1119 (a
    local gate that answers a question CI never asked). So the tokeniser
    must fail closed. Quoting still wins: a ``#`` inside quotes is text,
    not the start of a comment.
    """
    commented = "ruff check . --fix  # mentions --no-cache"
    assert shell_tokens(commented) == ["ruff", "check", ".", "--fix"], (
        "an inline comment survived tokenisation, so a comment naming a flag "
        f"could satisfy a flag-presence gate: {shell_tokens(commented)!r}"
    )
    assert not _has_flag(commented, "--no-cache"), (
        "the cache-freedom gate accepted an invocation that only mentions "
        f"--no-cache in a comment: {commented!r}"
    )

    genuine = "ruff check . --no-cache"
    assert shell_tokens(genuine) == ["ruff", "check", ".", "--no-cache"], (
        f"a genuine --no-cache invocation no longer tokenises: {genuine!r}"
    )
    assert _has_flag(genuine, "--no-cache"), (
        f"the cache-freedom gate rejected a genuinely cache-free line: {genuine!r}"
    )

    quoted = 'echo "a # not a comment" --no-cache'
    assert shell_tokens(quoted) == ["echo", "a # not a comment", "--no-cache"], (
        "a # inside quotes was mistaken for a comment, so tokenisation is "
        f"no longer quote-aware: {shell_tokens(quoted)!r}"
    )


def test_pre_commit_ruff_hooks_are_cache_free() -> None:
    """Both ruff pre-commit hooks must pass ``--no-cache`` too (#1119).

    The hooks run the same two tools over the same tree, from a cache the
    developer's machine keeps between commits; leaving them cached would
    reopen the same gap one layer earlier than ``check-all.sh``.
    """
    config = load_yaml(PRE_COMMIT_CONFIG)
    repos = [
        repo
        for repo in config["repos"]
        if "astral-sh/ruff-pre-commit" in str(repo.get("repo", ""))
    ]
    assert len(repos) == 1, (
        "expected exactly one astral-sh/ruff-pre-commit repo entry in "
        f"{PRE_COMMIT_CONFIG.name}, found {len(repos)}"
    )
    hooks = {str(hook.get("id")): hook for hook in repos[0].get("hooks", [])}
    for hook_id in ("ruff", "ruff-format"):
        assert hook_id in hooks, (
            f"the {hook_id!r} hook is gone from the ruff-pre-commit entry; "
            f"found {sorted(hooks)!r}"
        )
        args = [str(arg) for arg in hooks[hook_id].get("args", [])]
        assert "--no-cache" in args, (
            f"pre-commit hook {hook_id!r} may answer from .ruff_cache "
            f"(#1119); args are {args!r}"
        )


def test_ci_does_not_restore_a_ruff_cache() -> None:
    """CI must stay cold: no restored ``.ruff_cache``, no ``--cache-dir``.

    The whole value of the CI ruff run is that it re-reads every file.
    Caching ``.ruff_cache`` across runs, or pointing ruff at a shared cache
    directory, would import the exact staleness ``--no-cache`` removes
    locally -- and would do it on the side that is meant to be the backstop.
    """
    steps = all_workflow_steps()
    cache_steps = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/cache")
    ]
    assert cache_steps, (
        "no actions/cache step was found in any workflow, so the workflow "
        "parse is not seeing steps and the assertions below are vacuous"
    )
    for step in cache_steps:
        cached_path = str((step.get("with") or {}).get("path", ""))
        assert ".ruff_cache" not in cached_path, (
            "a CI cache step restores ruff's cache, which is exactly the "
            f"staleness this gate exists to prevent: {step.get('name')!r} "
            f"caches {cached_path!r}"
        )

    ruff_lines = step_run_lines(steps, r"^ruff\s")
    assert ruff_lines, "no ruff command line was found in any workflow run block"
    for line in ruff_lines:
        assert not _has_flag(line, "--cache-dir"), (
            f"a CI ruff invocation points at a cache directory: {line!r}"
        )


def test_ci_and_local_ruff_share_the_same_rule_configuration() -> None:
    """The local gate and CI must accept exactly the same code.

    Both sides must read their rules from ``creek-tools/pyproject.toml``.
    If either narrows the rule set on the command line, or checks a smaller
    target, ``--no-cache`` alone would not close the gap this issue is
    about: the local gate would still be answering a different question
    than CI asks.
    """
    lint_local = _single_gate_line(
        _local_ruff_lines(),
        _is_checking_ruff_check,
        "checking `ruff check` line in the gate scripts",
    )
    lint_ci = _single_gate_line(
        _ci_ruff_lines(),
        lambda line: line.startswith("ruff check "),
        "inline `ruff check` line in ci.yml",
    )
    _assert_same_rule_configuration(lint_local, lint_ci, "ruff check")

    format_local = _single_gate_line(
        _mode_branch_ruff_lines(SCRIPTS_DIR / "format.sh")["check"],
        lambda line: line.startswith("ruff format "),
        "`ruff format` line in format.sh's --check branch",
    )
    format_ci = _single_gate_line(
        _ci_ruff_lines(),
        lambda line: line.startswith("ruff format "),
        "inline `ruff format` line in ci.yml",
    )
    _assert_same_rule_configuration(format_local, format_ci, "ruff format --check")


def test_gate_ruff_invocations_do_not_autofix() -> None:
    """No checking ruff invocation may rewrite the tree to make itself pass.

    ``ruff check . --fix`` edits the offending file and exits 0, so an
    autofixing gate is a gate that cannot fail. Mutation belongs on the
    scripts' ``--fix`` path and in pre-commit, never on the path
    ``check-all.sh`` and CI take.
    """
    lint_check = _mode_branch_ruff_lines(SCRIPTS_DIR / "lint.sh")["check"]
    assert len(lint_check) == 1, (
        f"expected exactly one ruff line in lint.sh's check branch, got {lint_check!r}"
    )
    format_check = _mode_branch_ruff_lines(SCRIPTS_DIR / "format.sh")["check"]
    assert len(format_check) == 2, (
        "expected the import-sorting and formatter lines in format.sh's "
        f"--check branch, got {format_check!r}"
    )
    ci_lines = _ci_ruff_lines()
    assert len(ci_lines) == 2, (
        f"expected exactly the two inline ruff lines in ci.yml, got {ci_lines!r}"
    )

    for line in [*lint_check, *format_check, *ci_lines]:
        mutating = sorted(_flag_names(_ruff_argv(line)) & _MUTATION_FLAGS)
        assert not mutating, (
            f"a gate ruff invocation autofixes with {mutating!r}, so it can "
            f"never fail: {line!r}"
        )


# --------------------------------------------------------------------------
# Issue #1189: lint.sh's failure path.
#
# ``set -euo pipefail`` (lint.sh:5) kills the script at the bare ``ruff
# check`` line, so the ``EXIT_CODE=$?`` capture and the ``if [ $EXIT_CODE
# -eq 0 ]`` report that followed it were unreachable: the gate exited
# non-zero, correctly, but never said why. The tests below pin the three
# properties any fix must hold simultaneously -- the announcement, ruff's
# own exit code, and the ruff lines staying visible to the parser above.
# --------------------------------------------------------------------------

_LINT_FIXTURE_CONFIG = '[lint]\nselect = ["F"]\n'
# F401: imported and never used. Rejected by ruff with exit 1.
_LINT_FIXTURE_VIOLATION = "import os\n"
# Not parseable as TOML at all, which ruff reports as exit 2 rather than 1.
_LINT_FIXTURE_BROKEN_CONFIG = "this is not = valid toml [[[\n"

_LINT_FAILURE_MESSAGE = "✗ Linting checks failed"


def _lint_fixture_tree(tmp_path: Path, *, config: str, source: str) -> Path:
    """Build a throwaway project tree whose ``scripts/`` holds the real lint.sh.

    ``scripts/lint.sh`` is exposed as a *symlink*, so the file that executes
    is byte-identical to the one this repository ships; a copy could drift.
    ``lint.sh`` derives ``PROJECT_ROOT`` from the parent of its own
    directory, so the symlink makes ``tmp_path`` the project root and ruff
    runs over the fixture rather than over this repository.

    Args:
        tmp_path: An empty directory to use as the project root.
        config: Contents of the fixture's ``ruff.toml``, which anchors
            ruff's config discovery inside the tree so no repository
            setting can change what the fixture means.
        source: Contents of the single Python module in the tree.

    Returns:
        The fixture project root.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "lint.sh").symlink_to(SCRIPTS_DIR / "lint.sh")
    (tmp_path / "ruff.toml").write_text(config, encoding="utf-8")
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    return tmp_path


def _run_lint_sh(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the fixture tree's ``scripts/lint.sh --check``.

    Args:
        root: A project root built by :func:`_lint_fixture_tree`.

    Returns:
        The completed process, with stdout and stderr captured separately
        so the failure announcement can be located on the stream the
        script is supposed to write it to.
    """
    env = dict(os.environ)
    env.pop("RUFF_OUTPUT_FORMAT", None)
    # Keeps the fixture from touching the developer's real .ruff_cache.
    env["RUFF_CACHE_DIR"] = str(root / ".ruff_cache")
    return subprocess.run(
        ["bash", str(root / "scripts" / "lint.sh"), "--check"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_lint_gate_announces_the_failure_it_exits_on(tmp_path: Path) -> None:
    """A rejected tree must produce the failure message, not a silent exit.

    ``check-all.sh`` runs eleven gates in sequence; one of them exiting
    non-zero with no line naming itself leaves the operator reading raw
    ruff output to work out which gate spoke. The message existed in the
    script and was unreachable, which is indistinguishable from absent.
    """
    root = _lint_fixture_tree(
        tmp_path,
        config=_LINT_FIXTURE_CONFIG,
        source=_LINT_FIXTURE_VIOLATION,
    )

    result = _run_lint_sh(root)

    assert result.returncode != 0, (
        "the fixture premise never held: scripts/lint.sh cleared a tree "
        f"carrying an unused import.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert _LINT_FAILURE_MESSAGE in result.stderr, (
        "scripts/lint.sh rejected the tree without announcing that it was "
        "the linting gate that failed; the message is unreachable under "
        f"`set -e` (#1189).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_lint_gate_preserves_ruffs_error_exit_code(tmp_path: Path) -> None:
    """``2`` (error running checks) must not collapse into ``1`` (violations).

    ``lint.sh --help`` documents three exit codes, and the difference
    between them is operationally real: ``1`` means fix your code, ``2``
    means the linter never ran. Any failure path that hard-codes ``exit 1``
    -- as the dead branch did, and as an ``if ruff check …; then … else
    exit 1; fi`` restructure would -- silently breaks that contract.
    """
    root = _lint_fixture_tree(
        tmp_path,
        config=_LINT_FIXTURE_BROKEN_CONFIG,
        source=_LINT_FIXTURE_VIOLATION,
    )

    result = _run_lint_sh(root)

    ruff_error_exit = 2
    assert result.returncode == ruff_error_exit, (
        "scripts/lint.sh must report ruff's own exit 2 ('Error running "
        "checks', per its --help) rather than flattening every failure to "
        f"1.\nexit={result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_lint_sh_ruff_calls_stay_visible_to_the_parity_parser() -> None:
    """Both ruff invocations must stay bare commands inside the mode branch.

    The parser above finds ruff lines with ``^ruff\\s`` against stripped,
    non-comment lines, and finds the mode branch with ``^if \\$FIX; then$``.
    Moving either invocation into an ``if`` *condition* -- the shape the
    obvious rewrite of the dead branch reaches for -- makes both lines
    invisible to it, and the cache-freedom and no-autofix gates then stop
    seeing ``lint.sh`` at all while still passing. That is a gate going
    blind, so it is locked here explicitly rather than left implied.
    """
    assert _mode_branch_ruff_lines(SCRIPTS_DIR / "lint.sh") == {
        "check": ["ruff check . --no-cache"],
        "fix": ["ruff check . --fix --no-cache"],
    }, (
        "the parity parser can no longer see lint.sh's ruff invocations, so "
        "the --no-cache and no-autofix gates above now assert over an empty "
        f"set: {_mode_branch_ruff_lines(SCRIPTS_DIR / 'lint.sh')!r}"
    )
