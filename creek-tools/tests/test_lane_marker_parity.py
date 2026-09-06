"""The unit lane and the coverage lane must run the same set of tests.

Issue #1670. ``scripts/check-all.sh`` runs ``test.sh --unit`` and then
``coverage.sh --json`` back to back, and every contributor reads the pair as
one verdict: "the unit lane passed" is taken to say something about the
coverage lane that follows it. That reading is only sound while both lanes
select the same tests.

They had drifted. ``test.sh``'s unit lane deselected four markers
(``integration``, ``e2e``, ``slow``, ``live``); ``coverage.sh`` deselected
two. So the coverage lane ran the live smokes -- tests that reach real
provider APIs and local services -- and the two lanes' results were not
comparable. Two independent lanes working unrelated issues both reported
"13 failures in the unit lane, 14 in the coverage lane" on 2026-08-25 and
both spent effort establishing that the extra one was not theirs. That is
the cost this module exists to prevent: a gate whose red is unrelated to
the code under test trains everyone to dismiss it.

**This module parses, it does not grep.** The scripts quote their own
marker expressions inside explanatory comments, so a substring scan would
happily assert against prose while the executing line said something else
(see ``test_ruff_gate_parity.py`` for the same hazard, and #1186's review
for why an assertion that cannot fail is not a guard). Every helper here
reads ``PYTEST_ARGS`` assignments through
``tests.shell_command_support.shell_tokens`` and walks ``test.sh``'s
``case`` structure, so the marker expression under assertion is the one
bash would actually pass to pytest.
"""

from __future__ import annotations

import re
import tomllib

from tests.shell_command_support import (
    CREEK_TOOLS_DIR,
    SCRIPTS_DIR,
    non_comment_lines,
    shell_tokens,
)

TEST_SCRIPT = SCRIPTS_DIR / "test.sh"
COVERAGE_SCRIPT = SCRIPTS_DIR / "coverage.sh"
PYPROJECT = CREEK_TOOLS_DIR / "pyproject.toml"

#: A ``case`` branch label, e.g. ``    unit)``. Bash permits several
#: patterns per branch; the lanes use one apiece, which is what this pins.
_BRANCH_LABEL = re.compile(r"^\s*([A-Za-z0-9_|-]+)\)\s*$")
_BRANCH_END = re.compile(r"^\s*;;\s*$")
_CASE_START = re.compile(r'^\s*case\s+"\$TEST_TYPE"\s+in\s*$')
_CASE_END = re.compile(r"^\s*esac\s*$")

#: ``PYTEST_ARGS+=(...)`` / ``PYTEST_ARGS=(...)`` on one line.
_PYTEST_ARGS_LINE = re.compile(r"^\s*PYTEST_ARGS\+?=\(")


def _pytest_args_tokens(line: str) -> list[str]:
    """Return the arguments a one-line ``PYTEST_ARGS`` assignment appends.

    Args:
        line: A raw script line assigning or appending to ``PYTEST_ARGS``.

    Returns:
        The tokens inside the parentheses, shell-split. An assignment that
        opens a multi-line array (nothing after the paren) yields ``[]``.
    """
    inner = line.strip()
    inner = inner[inner.index("(") + 1 :]
    inner = inner.rsplit(")", maxsplit=1)[0] if ")" in inner else inner
    return shell_tokens(inner)


def _marker_expression(tokens: list[str]) -> str | None:
    """Return the marker expression a token list passes via ``-m``.

    Args:
        tokens: Shell tokens of a single pytest argument list.

    Returns:
        The argument following ``-m``, or ``None`` when the list carries
        no ``-m`` at all.
    """
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _deselected_markers(expression: str) -> frozenset[str]:
    """Return the markers a purely-negative marker expression excludes.

    Args:
        expression: A pytest ``-m`` expression such as
            ``"not integration and not e2e"``.

    Returns:
        The excluded marker names.

    Raises:
        ValueError: If any conjunct is not a bare ``not <marker>`` term.
            The lanes are deliberately restricted to that shape; anything
            richer must be read by a human rather than silently accepted
            as "no markers excluded".
    """
    markers: set[str] = set()
    for term in expression.split(" and "):
        parts = term.strip().split()
        if len(parts) != 2 or parts[0] != "not":
            msg = f"not a bare negative term: {term!r} (in {expression!r})"
            raise ValueError(msg)
        markers.add(parts[1])
    return frozenset(markers)


def _test_script_branches() -> dict[str, list[str]]:
    """Return ``test.sh``'s ``case "$TEST_TYPE"`` branches, by label.

    Walking the ``case`` structure -- rather than matching whichever line
    happens to mention a marker -- is what lets the ``all`` assertions
    below distinguish "this branch deliberately adds no ``-m``" from "no
    such branch exists any more".

    Returns:
        A mapping of branch label to that branch's non-comment lines.
    """
    branches: dict[str, list[str]] = {}
    label: str | None = None
    in_case = False
    for line in non_comment_lines(TEST_SCRIPT):
        if not in_case:
            in_case = bool(_CASE_START.match(line))
            continue
        if _CASE_END.match(line):
            break
        if label is None:
            match = _BRANCH_LABEL.match(line)
            if match:
                label = match.group(1)
                branches[label] = []
            continue
        if _BRANCH_END.match(line):
            label = None
            continue
        branches[label].append(line)
    return branches


def _branch_marker_expression(label: str) -> str | None:
    """Return the ``-m`` expression a ``test.sh`` lane branch builds.

    Args:
        label: The ``case`` branch label, e.g. ``"unit"``.

    Returns:
        The branch's marker expression, or ``None`` when it sets none.

    Raises:
        AssertionError: If no such branch exists, or if the branch sets
            more than one marker expression.
    """
    branches = _test_script_branches()
    assert label in branches, (
        f"scripts/test.sh has no `{label})` branch in its "
        f'`case "$TEST_TYPE"`; found {sorted(branches)}. The lane this '
        "module pins was renamed or removed."
    )
    expressions = [
        expression
        for line in branches[label]
        if _PYTEST_ARGS_LINE.match(line)
        if (expression := _marker_expression(_pytest_args_tokens(line))) is not None
    ]
    assert len(expressions) <= 1, (
        f"scripts/test.sh's `{label})` branch sets {len(expressions)} marker "
        f"expressions ({expressions!r}); this module assumes one per lane"
    )
    return expressions[0] if expressions else None


def _coverage_marker_expression() -> str:
    """Return the marker expression ``coverage.sh`` passes to pytest.

    Returns:
        The coverage lane's ``-m`` expression.

    Raises:
        AssertionError: If the script sets no marker expression, or more
            than one.
    """
    expressions = [
        expression
        for line in non_comment_lines(COVERAGE_SCRIPT)
        if _PYTEST_ARGS_LINE.match(line)
        if (expression := _marker_expression(_pytest_args_tokens(line))) is not None
    ]
    assert len(expressions) == 1, (
        "scripts/coverage.sh sets "
        f"{len(expressions)} pytest marker expressions ({expressions!r}); "
        "expected exactly one. A coverage lane with none runs every "
        "marker, including the live smokes."
    )
    return expressions[0]


def _declared_markers() -> frozenset[str]:
    """Return the marker names declared in ``pyproject.toml``.

    Returns:
        Every name in ``[tool.pytest.ini_options].markers``, taken from
        the text before each entry's ``:`` description.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = config["tool"]["pytest"]["ini_options"]["markers"]
    return frozenset(entry.split(":", maxsplit=1)[0].strip() for entry in declared)


def test_unit_and_coverage_lanes_deselect_the_same_markers() -> None:
    """The two lanes ``check-all.sh`` runs must select the same tests.

    This is the assertion the issue turns on. Both lanes run in one
    ``check-all.sh`` invocation, seconds apart, and their results are read
    together; a marker excluded from one and not the other makes that
    reading wrong without making it look wrong.
    """
    unit_expression = _branch_marker_expression("unit")
    assert unit_expression is not None, (
        "scripts/test.sh's `unit)` branch passes no -m expression, so the "
        "unit lane now runs integration, e2e, slow and live tests too"
    )
    unit = _deselected_markers(unit_expression)
    coverage = _deselected_markers(_coverage_marker_expression())
    assert unit == coverage, (
        "scripts/test.sh --unit and scripts/coverage.sh deselect different "
        f"markers, so the two lanes of check-all.sh run different tests. "
        f"Only the unit lane excludes {sorted(unit - coverage)}; only the "
        f"coverage lane excludes {sorted(coverage - unit)}."
    )


def test_the_coverage_lane_deselects_live_and_slow() -> None:
    """Naming the two markers pins them against a symmetric regression.

    The parity test above compares the lanes to each other, so an edit
    that dropped ``live`` from *both* scripts would keep it green while
    reintroducing exactly the failure #1670 describes. This one names the
    markers, so that edit is red here.
    """
    coverage = _deselected_markers(_coverage_marker_expression())
    for marker in ("live", "slow"):
        assert marker in coverage, (
            f"scripts/coverage.sh no longer deselects `{marker}`, so the "
            f"coverage lane runs it. `live` tests reach real provider APIs "
            f"and local services; excluded markers are {sorted(coverage)}."
        )


def test_the_all_lane_still_reaches_every_marker() -> None:
    """``--all`` deliberately runs the live smokes; #1670 did not change it.

    ``scripts/test.sh``'s ``all)`` branch sets no ``-m`` on purpose, so a
    marker added in future is covered there for free. Recording that
    decision here is an acceptance criterion of #1670: narrowing the
    coverage lane must not silently narrow this lane too.
    """
    assert _branch_marker_expression("all") is None, (
        "scripts/test.sh's `all)` branch now passes an -m expression. "
        "`all` means every marker, live smokes and slow benchmarks "
        "included; a union of the other lanes silently drops any marker "
        "added later."
    )


def test_every_deselected_marker_is_declared() -> None:
    """A lane cannot deselect a marker ``pyproject.toml`` never declares.

    ``--strict-markers`` is on, so an undeclared marker is an error at
    collection rather than a silent no-op -- but only for tests that
    *apply* it. A ``-m "not typo"`` expression is accepted silently and
    excludes nothing, which is the false-green shape of #1186/#1187.
    """
    declared = _declared_markers()
    unit_expression = _branch_marker_expression("unit")
    assert unit_expression is not None
    used = _deselected_markers(unit_expression) | _deselected_markers(
        _coverage_marker_expression()
    )
    undeclared = used - declared
    assert not undeclared, (
        f"the gate lanes deselect {sorted(undeclared)}, which "
        "[tool.pytest.ini_options].markers does not declare. pytest "
        "accepts an unknown name in a -m expression silently, so that "
        "term excludes nothing and the lane runs the tests anyway."
    )


def test_a_commented_marker_line_cannot_satisfy_the_parity_gate() -> None:
    """Prose that quotes a marker expression must not count as one.

    Both scripts explain their marker choice in comments that quote the
    expression verbatim, so comment-blindness is what separates this
    module from a substring scan that would pass against the prose while
    the executing line said something else.
    """
    commented = '# PYTEST_ARGS+=(-m "not integration and not e2e")'
    assert _PYTEST_ARGS_LINE.match(commented) is None, (
        "a commented-out PYTEST_ARGS line matched as an executing one"
    )
    genuine = 'PYTEST_ARGS+=(-m "not integration and not e2e")'
    assert _PYTEST_ARGS_LINE.match(genuine) is not None
    assert _marker_expression(_pytest_args_tokens(genuine)) == (
        "not integration and not e2e"
    )
    assert non_comment_lines(COVERAGE_SCRIPT).count(commented) == 0


def test_a_non_negative_marker_term_is_rejected_not_ignored() -> None:
    """An expression this module cannot read must fail, never parse empty.

    Returning an empty set for an unrecognised expression would make
    every assertion above vacuously true -- two lanes "agreeing" because
    neither was understood.
    """
    for expression in ("integration", "not integration and e2e", ""):
        try:
            _deselected_markers(expression)
        except ValueError:
            continue
        msg = f"{expression!r} parsed as a set of negative terms; it is not"
        raise AssertionError(msg)
