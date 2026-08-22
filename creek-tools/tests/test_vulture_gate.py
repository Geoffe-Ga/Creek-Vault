"""The dead-code gate must be able to fail on dead code (issue #1395).

Before this module existed, the repository's only vulture invocations were
``vulture creek/ --min-confidence 80`` in ``.pre-commit-config.yaml`` and
``scripts/lint-extended.sh``. Vulture scores an unused function, method,
class, property or attribute at **60%**, so a floor of 80 excluded the
entire dead-symbol tier: a brand-new zero-caller function in ``creek/``
produced *zero* findings. Neither invocation ran in CI or in
``check-all.sh`` either, so the gate was doubly incapable of failing.

``scripts.lint_vulture`` replaces the single blunt threshold with
a per-type floor table plus categorical carve-outs for symbols the
interpreter or a framework invokes by protocol rather than by name. The
two tests at the top of this module are the heart of it: one proves the
new policy reports a zero-caller function, its twin proves the old
threshold reported nothing on the very same code.

Two vulture implementation details shape the synthetic fixtures below:

* ``vulture.core._is_test_file`` matches ``*/test*.py`` and ``*/tests/*``
  case-insensitively against the *resolved* path, and ``tmp_path``
  directories are named after the test function -- so every synthetic
  module here is, as far as vulture is concerned, a test file. That only
  changes behaviour for symbols named ``test_*`` and classes containing
  ``Test``, which the fixtures therefore avoid.
* ``used_names`` is global across a scan, so a name loaded anywhere marks
  every definition of that name used. The fixtures keep their supporting
  symbols genuinely referenced so each one has exactly the findings its
  test claims.

The residual-zero regression test at the bottom is deliberately NOT
marked ``slow``: ``scripts/test.sh --unit`` selects
``not integration and not e2e and not slow and not live``, so marking it
would drop it straight out of the only lane that blocks a merge.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
from vulture.core import ERROR_CODES

from scripts import lint_vulture
from scripts.lint_vulture import (
    CONFIDENCE_FLOORS,
    CRAWDAD,
    CREEK_TOOLS,
    IGNORE_DECORATORS,
    IMPLICITLY_BOUND_PARAMETERS,
    IMPLICITLY_INVOKED_NAMES,
    LEGACY_MIN_CONFIDENCE,
    SCOPES,
    Finding,
    RelativeReferenceRootError,
    Scope,
    UnknownFindingTypeError,
    find_dead_code,
    main,
    scan_scope,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# ---------------------------------------------------------------------------
# Synthetic sources
# ---------------------------------------------------------------------------

# One zero-caller function next to one that is genuinely called. The
# module-level ``RESULT`` is an unused *variable* (60%), which the policy's
# 90% variable floor drops -- so the expected finding set is exactly one.
_ZERO_CALLER_SOURCE = """
def reachable_helper(value: int) -> int:
    \"\"\"Called below, so vulture sees it used.\"\"\"
    return value + 1


def orphaned_helper(value: int) -> int:
    \"\"\"Dead: nothing in the package calls this.\"\"\"
    return value - 1


RESULT = reachable_helper(1)
"""

# Nothing here is dead: ``add`` is called, its parameters are used, and
# ``SUM`` is a 60% variable that the policy floors out.
_CLEAN_SOURCE = """
def add(left: int, right: int) -> int:
    \"\"\"Sum two numbers.\"\"\"
    return left + right


SUM = add(1, 2)
"""

# ``json`` is an unused import (90%), ``unused_parameter`` an unused
# parameter -- which vulture types as a *variable* at 100% -- and
# ``LEFTOVER_CONSTANT`` an unused variable at 60%.
_FLOORS_SOURCE = """
import json


LEFTOVER_CONSTANT = "left behind, but only 60% confidence"


def scale(factor: int, unused_parameter: int) -> int:
    \"\"\"Double a factor, ignoring the second argument.\"\"\"
    return factor * 2


TOTAL = scale(1, 2)
"""

_TYPER_COMMAND_SOURCE = """
import typer

app = typer.Typer()


@app.command()
def ship_it(count: int) -> int:
    \"\"\"Invoked by Typer's registry, never by name.\"\"\"
    return count + 1


def orphaned_sibling(count: int) -> int:
    \"\"\"Dead: no decorator, no caller.\"\"\"
    return count - 1
"""

_FIELD_VALIDATOR_SOURCE = """
from pydantic import BaseModel, field_validator


class Widget(BaseModel):
    \"\"\"A model whose validator pydantic calls by protocol.\"\"\"

    size: int

    @field_validator("size")
    @classmethod
    def check_size(cls, value: int) -> int:
        \"\"\"Invoked by pydantic during validation, never by name.\"\"\"
        if value < 0:
            raise ValueError(f"{cls.__name__} needs a non-negative size")
        return value

    def orphaned_method(self) -> None:
        \"\"\"Dead: an ordinary method with no caller.\"\"\"


SCHEMA = Widget
"""

_DUNDER_CALL_SOURCE = """
class Callback:
    \"\"\"A callable object.\"\"\"

    def __call__(self, value: int) -> int:
        \"\"\"Invoked by the () operator, never by name.\"\"\"
        return value

    def orphaned_method(self) -> None:
        \"\"\"Dead: an ordinary method with no caller.\"\"\"


HANDLER = Callback()
"""

# ``@mcp.tool`` matches the wildcard ``@*.tool`` but none of the exact
# patterns, so this fixture is the only thing that exercises a wildcard.
_WILDCARD_TOOL_SOURCE = """
class Registry:
    \"\"\"Stand-in for an MCP server's tool registry.\"\"\"

    def tool(self, fn: object) -> object:
        \"\"\"Register and return the callback unchanged.\"\"\"
        return fn


mcp = Registry()


@mcp.tool
def registered_tool(value: int) -> int:
    \"\"\"Invoked through the registry, never by name.\"\"\"
    return value


def orphaned_sibling(value: int) -> int:
    \"\"\"Dead: no decorator, no caller.\"\"\"
    return value - 1
"""

# An overloaded function nobody calls. All three definitions share one
# name, so without the carve-out vulture reports the same dead symbol
# three times -- once per ``def``.
_OVERLOAD_SOURCE = """
from typing import overload


@overload
def widen(value: int) -> int: ...
@overload
def widen(value: str) -> str: ...
def widen(value: object) -> object:
    \"\"\"Never called anywhere.\"\"\"
    return value
"""

# ``clamp`` is called, so the only finding is the statement after the
# return -- vulture's one 100%-confidence category.
_UNREACHABLE_SOURCE = """
def clamp(value: int) -> int:
    \"\"\"Return the value; the line after the return can never run.\"\"\"
    return value
    value += 1


RESULT = clamp(1)
"""

_ENUM_MISSING_SOURCE = """
from enum import Enum


class Colour(Enum):
    \"\"\"An enum with a lookup hook.\"\"\"

    RED = "red"

    @classmethod
    def _missing_(cls, value: object) -> "Colour | None":
        \"\"\"Invoked by Enum lookup, never by name.\"\"\"
        for member in cls:
            if member.value == value:
                return member
        return None

    def orphaned_method(self) -> None:
        \"\"\"Dead: an ordinary method with no caller.\"\"\"


DEFAULT = Colour.RED
"""

# (source, symbol the carve-out must protect, dead sibling that must still
# be reported, the type the gate should give that sibling).
_CARVE_OUT_CASES = (
    pytest.param(
        _TYPER_COMMAND_SOURCE,
        "ship_it",
        "orphaned_sibling",
        "function",
        id="app.command-decorator",
    ),
    pytest.param(
        _FIELD_VALIDATOR_SOURCE,
        "check_size",
        "orphaned_method",
        "method",
        id="field_validator-decorator",
    ),
    pytest.param(
        _DUNDER_CALL_SOURCE,
        "__call__",
        "orphaned_method",
        "method",
        id="dunder-special-method",
    ),
    pytest.param(
        _ENUM_MISSING_SOURCE,
        "_missing_",
        "orphaned_method",
        "method",
        id="enum-_missing_-hook",
    ),
    pytest.param(
        _WILDCARD_TOOL_SOURCE,
        "registered_tool",
        "orphaned_sibling",
        "function",
        id="wildcard-tool-decorator",
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepare(source: str) -> str:
    """Normalise a triple-quoted fixture source into file content.

    Args:
        source: A fixture source constant.

    Returns:
        The source with common indentation and leading blank lines removed,
        so line numbers in the written file match :func:`_line_of`.
    """
    return dedent(source).lstrip("\n")


def _line_of(source: str, needle: str) -> int:
    """Return the 1-based line number of the first line containing ``needle``.

    Args:
        source: A fixture source constant (un-normalised).
        needle: Substring to look for.

    Returns:
        The line number the substring appears on.

    Raises:
        AssertionError: If the substring is absent, which would make the
            assertion that uses it meaningless.
    """
    for number, line in enumerate(_prepare(source).splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in the fixture source")


def _write_module(directory: Path, name: str, source: str) -> Path:
    """Write one synthetic module into ``directory``.

    Args:
        directory: Directory to create and write into.
        name: Module name, without the ``.py`` suffix.
        source: Fixture source constant.

    Returns:
        The path of the written module.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(_prepare(source), encoding="utf-8")
    return path


def _write_package(root: Path, sources: Mapping[str, str]) -> Path:
    """Write a synthetic importable package under ``root``.

    Args:
        root: The ``tmp_path`` of the calling test.
        sources: Module name (no suffix) -> fixture source constant.

    Returns:
        The package directory, ready to hand to :func:`find_dead_code`.
    """
    package = root / "synthetic_pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in sources.items():
        _write_module(package, name, source)
    return package


def _scan(*paths: Path) -> list[Finding]:
    """Run the gate over synthetic paths with reference-only filtering off.

    ``reference_only`` is passed empty on purpose: a scope's own roots
    name the repository's real ``tests/`` tree, and a test that left them
    in place would be asserting about a synthetic package *and* whatever
    the real suite happens to contain.

    Args:
        *paths: Directories or files to scan.

    Returns:
        The findings, sorted by ``(path, lineno)``.
    """
    return find_dead_code(list(paths), reference_only=())


def _names(findings: Sequence[Finding]) -> list[str]:
    """Return the symbol names of ``findings``, in report order.

    Args:
        findings: Findings from :func:`find_dead_code`.

    Returns:
        The ``name`` of each finding.
    """
    return [finding.name for finding in findings]


def _one(findings: Sequence[Finding], name: str) -> Finding:
    """Return the single finding called ``name``.

    Args:
        findings: Findings from :func:`find_dead_code`.
        name: The symbol name expected exactly once.

    Returns:
        The matching finding.
    """
    matching = [finding for finding in findings if finding.name == name]
    assert len(matching) == 1, (
        f"expected exactly one finding named {name!r}, got {_names(findings)!r}"
    )
    return matching[0]


# ---------------------------------------------------------------------------
# The defect: a gate that could not fail
# ---------------------------------------------------------------------------


def test_the_gate_reports_a_zero_caller_function(tmp_path: Path) -> None:
    """A brand-new function nobody calls must be reported (#1395).

    This is the whole issue in one assertion. Adding a zero-caller
    function to ``creek/`` and running the repository's only vulture
    invocation produced no findings at all, which made every "dead code is
    gated" claim in the docs false. The gate is asserted on ``Finding``
    objects -- name, type, line and confidence -- rather than on scraped
    stdout, so a change to the report format cannot quietly turn this into
    a substring test that passes on an empty report.
    """
    package = _write_package(tmp_path, {"module": _ZERO_CALLER_SOURCE})
    module = package / "module.py"

    findings = _scan(package)

    assert _names(findings) == ["orphaned_helper"], (
        "the dead-code gate did not report a zero-caller function, which is "
        f"exactly the defect of #1395; findings were {findings!r}"
    )
    finding = findings[0]
    assert finding.typ == "function", (
        f"expected vulture to type the orphan as a function, got {finding.typ!r}"
    )
    assert finding.confidence == 60, (
        "vulture scores an unused function at 60%; a different score here "
        "means the fixture no longer exercises the tier the old "
        f"--min-confidence 80 excluded (got {finding.confidence})"
    )
    assert finding.lineno == _line_of(_ZERO_CALLER_SOURCE, "def orphaned_helper"), (
        f"the finding points at line {finding.lineno}, not at the definition"
    )
    assert finding.path.is_absolute(), (
        f"Finding.path must be absolute so it is unambiguous, got {finding.path!r}"
    )
    assert finding.path.resolve() == module.resolve(), (
        f"the finding names {finding.path!r}, not the module that holds the orphan"
    )


def test_the_legacy_min_confidence_reported_nothing_on_the_same_package(
    tmp_path: Path,
) -> None:
    """The old 80% threshold saw nothing in the code above (#1395).

    This is the regression proper: the same synthetic package, filtered the
    way ``vulture --min-confidence 80`` filtered -- vulture's own rule is
    literally ``item.confidence >= min_confidence`` -- yields an empty
    report, while the per-type floors yield exactly one finding. Without
    this twin, the test above would only show that *some* configuration
    finds dead code; together they show that the configuration this repo
    actually shipped could not.
    """
    package = _write_package(tmp_path, {"module": _ZERO_CALLER_SOURCE})

    findings = _scan(package)
    survivors = [
        finding for finding in findings if finding.confidence >= LEGACY_MIN_CONFIDENCE
    ]

    assert LEGACY_MIN_CONFIDENCE == 80, (
        "LEGACY_MIN_CONFIDENCE records the broken threshold this issue "
        f"removed; it must stay 80, got {LEGACY_MIN_CONFIDENCE}"
    )
    assert survivors == [], (
        "a --min-confidence 80 filter is supposed to be blind to the "
        f"dead-symbol tier; these survived, so the fixture is wrong: {survivors!r}"
    )
    assert len(findings) == 1, (
        "the new policy must still report the orphan the legacy threshold "
        f"missed; got {findings!r}"
    )
    assert CONFIDENCE_FLOORS["function"] < LEGACY_MIN_CONFIDENCE, (
        "the function floor must sit below the legacy threshold, or the new "
        "policy is no stronger than the one it replaced: "
        f"{CONFIDENCE_FLOORS['function']} vs {LEGACY_MIN_CONFIDENCE}"
    )


# ---------------------------------------------------------------------------
# The Finding value object
# ---------------------------------------------------------------------------


def test_finding_renders_as_a_vulture_style_report_line() -> None:
    """``str(Finding)`` must name the file, line, type, symbol and score.

    ``main`` prints findings for a human who then has to go delete
    something, so every element needed to find and judge the symbol has to
    be in the line. The exact string is pinned because a report that drops
    the confidence, or the line number, sends that human hunting.
    """
    finding = Finding(
        path=Path("/repo/creek/link/eddies.py"),
        lineno=12,
        typ="function",
        name="_legacy_curve",
        confidence=60,
    )

    assert str(finding) == (
        "/repo/creek/link/eddies.py:12: unused function '_legacy_curve' (60%)"
    ), f"unexpected report line: {str(finding)!r}"


def test_finding_is_an_immutable_value_object() -> None:
    """A ``Finding`` must not be mutable after construction.

    Findings are compared, sorted and reported; a caller that could edit a
    finding's confidence in place could silently launder a real finding
    past a floor. Frozen also makes them hashable, which is what lets
    tests compare finding sets directly.
    """
    finding = Finding(
        path=Path("/repo/creek/link/eddies.py"),
        lineno=12,
        typ="function",
        name="_legacy_curve",
        confidence=60,
    )

    with pytest.raises(FrozenInstanceError):
        finding.confidence = 100


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


def test_the_policy_constants_are_pinned() -> None:
    """The scan surface, floors and carve-outs are the contract (#1395).

    Every one of these values is a way for the gate to quietly stop
    gating: drop a scan path and a package goes unchecked; raise the
    function floor back above 60 and the whole defect returns; add a name
    to the implicit-invocation set and a real orphan gets buried. Pinning
    them means weakening the gate has to be a deliberate, reviewable edit
    to this list.
    """
    assert CREEK_TOOLS.scan == ("creek", "creek_mcp", "tests"), (
        "the gate must scan both shipped packages plus the test tree; "
        f"got {CREEK_TOOLS.scan!r}"
    )
    assert CREEK_TOOLS.reference_only == ("tests",), (
        f"tests/ is scanned for references only; got {CREEK_TOOLS.reference_only!r}"
    )
    assert CRAWDAD.scan == ("crawdad", "tests"), (
        "the sibling subproject is gated too (#1472); the scan surface must "
        f"be its package plus its test tree. Got {CRAWDAD.scan!r}"
    )
    assert CRAWDAD.reference_only == ("tests",), (
        f"crawdad/tests/ is scanned for references only; got {CRAWDAD.reference_only!r}"
    )
    assert set(SCOPES) == {"creek-tools", "crawdad"}, (
        "SCOPES is what `--scope` selects and what the two wrapper scripts "
        f"name; dropping one un-gates a whole subproject. Got {sorted(SCOPES)!r}"
    )
    assert frozenset({"cls", "self"}) == IMPLICITLY_BOUND_PARAMETERS, (
        "this set is for parameters the interpreter binds whether the body "
        "reads them or not -- it is not a burial ground for dead locals. Got "
        f"{sorted(IMPLICITLY_BOUND_PARAMETERS)!r}"
    )
    assert dict(CONFIDENCE_FLOORS) == {
        "function": 60,
        "method": 60,
        "class": 60,
        "property": 60,
        "attribute": 60,
        "variable": 90,
        "import": 90,
        "unreachable_code": 90,
    }, (
        "the dead-symbol tier scores 60% and must be gated at 60; the noisy "
        f"tier is gated at 90. Got {dict(CONFIDENCE_FLOORS)!r}"
    )
    assert set(CONFIDENCE_FLOORS) == set(ERROR_CODES), (
        "every item type vulture can emit needs a floor, or the gate will "
        "raise UnknownFindingTypeError on a real scan; vulture emits "
        f"{sorted(ERROR_CODES)!r}, the policy floors {sorted(CONFIDENCE_FLOORS)!r}"
    )
    assert frozenset({"_missing_"}) == IMPLICITLY_INVOKED_NAMES, (
        "this set is for symbols the interpreter invokes by protocol and "
        "that are not dunders -- it is not a burial ground for individual "
        f"dead symbols. Got {sorted(IMPLICITLY_INVOKED_NAMES)!r}"
    )
    for pattern in (
        "@app.command",
        "@*.command",
        "@*.callback",
        "@server.tool",
        "@*.tool",
        "@field_validator",
        "@model_validator",
        "@*.field_validator",
        "@*.model_validator",
        "@overload",
        "@typing.overload",
    ):
        assert pattern in IGNORE_DECORATORS, (
            f"{pattern!r} registers a symbol with a framework that then calls "
            "it by protocol; dropping the carve-out floods the gate with "
            f"false positives. Got {IGNORE_DECORATORS!r}"
        )


def test_confidence_floors_keep_the_noisy_tier_out_and_let_the_rest_in(
    tmp_path: Path,
) -> None:
    """Per-type floors, exercised on real vulture output (#1395).

    A single threshold cannot express this policy: unused *variables* at
    60% are mostly noise (loop targets, tuple unpacking, module constants
    read by name elsewhere), while an unused *parameter* -- which vulture
    also types as a variable, but scores at 100% -- and an unused import
    at 90% are real. The floors have to discriminate by type, and this
    test is the only place that proves they do against genuine findings
    rather than hand-built ones.
    """
    package = _write_package(tmp_path, {"module": _FLOORS_SOURCE})

    findings = _scan(package)

    assert [(f.name, f.typ, f.confidence) for f in findings] == [
        ("json", "import", 90),
        ("unused_parameter", "variable", 100),
    ], f"unexpected finding set for the floors fixture: {findings!r}"
    assert "LEFTOVER_CONSTANT" not in _names(findings), (
        "a 60%-confidence unused variable is below the 90% variable floor "
        "and must not be reported, or the gate becomes unusable noise"
    )
    parameter_finding = _one(findings, "unused_parameter")
    assert parameter_finding.lineno == _line_of(_FLOORS_SOURCE, "def scale"), (
        "the unused-parameter finding must point at the signature that declares it"
    )


def test_unreachable_code_is_reported_at_its_own_floor(tmp_path: Path) -> None:
    """The 100%-confidence category must actually reach the report (#1395).

    ``unreachable_code`` is the one finding vulture proves from control
    flow rather than infers, and it sits in the >=90 band with
    ``variable`` and ``import``. It is the only floor with no production
    instance in this repo, so without a synthetic fixture the entry in
    CONFIDENCE_FLOORS would be pinned as a constant and never exercised
    -- a floor nobody has watched work.
    """
    package = _write_package(tmp_path, {"module": _UNREACHABLE_SOURCE})

    findings = _scan(package)

    assert [(f.typ, f.confidence) for f in findings] == [("unreachable_code", 100)], (
        "the statement after the return is provably dead and must be "
        f"reported; findings were {findings!r}"
    )
    assert findings[0].confidence >= CONFIDENCE_FLOORS["unreachable_code"], (
        "the finding must clear its own floor, or this test is asserting "
        "something the policy did not decide"
    )


def test_overload_stubs_collapse_to_one_finding(tmp_path: Path) -> None:
    """A dead overloaded function is reported once, not once per stub.

    ``@overload`` stubs are erased at runtime and share their name with
    the implementation, so a dead overloaded function yields one ``def``
    worth of real news and two decoys. Without the carve-out this
    fixture reports three findings for one symbol; with it, exactly one,
    pointing at the implementation a developer would actually delete.

    This is the carve-out's whole job, and it is why the assertion is on
    the finding's *line number* rather than just its name -- all three
    definitions are called ``widen``, so a name-only assertion would pass
    on the noisy behaviour too.
    """
    package = _write_package(tmp_path, {"module": _OVERLOAD_SOURCE})

    findings = _scan(package)

    implementation_line = _line_of(_OVERLOAD_SOURCE, "def widen(value: object)")
    assert [(f.name, f.lineno) for f in findings] == [("widen", implementation_line)], (
        "a dead overloaded function must be reported exactly once, at its "
        f"implementation (line {implementation_line}); got {findings!r}"
    )


@pytest.mark.parametrize(
    ("source", "carved_name", "control_name", "control_typ"), _CARVE_OUT_CASES
)
def test_implicitly_invoked_symbols_are_carved_out_and_dead_siblings_are_not(
    tmp_path: Path,
    source: str,
    carved_name: str,
    control_name: str,
    control_typ: str,
) -> None:
    """Categorical carve-outs must spare protocol symbols, and nothing else.

    A Typer command, a pydantic validator, ``__call__`` and an enum's
    ``_missing_`` hook are all invoked by a framework or by the
    interpreter, never by name, so a name-based dead-code scan sees them
    as orphans. The carve-outs are categorical -- a decorator pattern, the
    dunder shape, one named protocol hook -- rather than a per-symbol
    allowlist, because an allowlist is where real dead code goes to hide.

    Each case carries a **negative control**: an undecorated, non-dunder
    dead sibling in the same module. Without it, a carve-out that
    swallowed the entire module -- or a gate that reported nothing at all
    -- would pass this test.
    """
    package = _write_package(tmp_path, {"module": source})

    findings = _scan(package)

    assert carved_name not in _names(findings), (
        f"{carved_name!r} is invoked by protocol, not by name, and must not "
        f"be reported as dead; findings were {findings!r}"
    )
    assert _names(findings) == [control_name], (
        f"the negative control {control_name!r} is genuinely dead and must "
        "still be reported -- otherwise the carve-out above is vacuous; "
        f"findings were {findings!r}"
    )
    assert findings[0].typ == control_typ, (
        f"expected the control to be typed {control_typ!r}, got {findings[0].typ!r}"
    )


def test_findings_are_sorted_by_path_then_line(tmp_path: Path) -> None:
    """The report order is stable, so diffs of it are readable.

    A gate whose output order depends on dict or set iteration produces a
    different report for the same tree on every run, which makes "is this
    the same failure as last time?" unanswerable.
    """
    package = _write_package(
        tmp_path,
        {
            "alpha": """
            def orphan_alpha_one() -> None:
                \"\"\"Dead.\"\"\"


            def orphan_alpha_two() -> None:
                \"\"\"Dead.\"\"\"
            """,
            "beta": """
            def orphan_beta_one() -> None:
                \"\"\"Dead.\"\"\"
            """,
        },
    )

    findings = _scan(package)

    assert _names(findings) == [
        "orphan_alpha_one",
        "orphan_alpha_two",
        "orphan_beta_one",
    ], f"findings are not sorted by (path, lineno): {findings!r}"
    assert [f.path.name for f in findings] == ["alpha.py", "alpha.py", "beta.py"], (
        f"findings are not grouped by file: {findings!r}"
    )


# ---------------------------------------------------------------------------
# Reference-only paths
# ---------------------------------------------------------------------------


def _reference_only_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Build a package plus a reference-only directory that exercises it.

    Args:
        tmp_path: The calling test's temporary directory.

    Returns:
        ``(package_dir, reference_dir)``.
    """
    package = _write_package(
        tmp_path,
        {
            "module": """
            def shared_helper(value: int) -> int:
                \"\"\"Only ever called from the reference-only directory.\"\"\"
                return value + 1


            def orphaned_helper(value: int) -> int:
                \"\"\"Dead: called from nowhere at all.\"\"\"
                return value - 1
            """
        },
    )
    reference = tmp_path / "tests"
    _write_module(
        reference,
        "test_shared_helper",
        """
        from synthetic_pkg.module import shared_helper


        def test_shared_helper_adds_one() -> None:
            \"\"\"Exercise the helper.\"\"\"
            assert shared_helper(1) == 2


        def orphaned_fixture_builder() -> int:
            \"\"\"Dead, but dead inside the reference-only tree.\"\"\"
            return 41
        """,
    )
    return package, reference


def test_a_checkout_inside_a_directory_named_tests_does_not_blind_the_gate(
    tmp_path: Path,
) -> None:
    """A ``tests`` component in the *checkout path* must not suppress findings.

    ``Scope.reference_only`` is the relative name ``tests``, joined to the
    scope's own root. If containment were decided by looking for that name
    among a finding's path components, then a clone into ``~/tests/creek-tools``
    would put ``tests`` in the path of every file in the repository, mark
    every finding reference-only, and the gate would report a cheerful zero
    forever -- silently, and only on some machines.

    That is the same failure as the ``--min-confidence 80`` this module
    replaced: a gate that cannot fail. So containment is resolved against a
    real directory, and this test pins it by building the package *under* a
    directory called ``tests`` and asserting the orphan is still reported.
    """
    checkout = tmp_path / "tests" / "checkout"
    package = _write_package(checkout, {"module": _ZERO_CALLER_SOURCE})

    findings = find_dead_code(
        [package], reference_only=CREEK_TOOLS.reference_only_paths
    )

    assert _names(findings) == ["orphaned_helper"], (
        "the gate went blind because the checkout happens to live under a "
        "directory named 'tests'; reference-only roots must be resolved to "
        f"real directories, not matched as path components. Got {findings!r}"
    )


def test_a_symbol_used_only_from_a_reference_only_path_is_not_dead(
    tmp_path: Path,
) -> None:
    """Reference-only paths are scanned so their references count (#1395).

    Plenty of ``creek/`` helpers exist to be exercised by the suite and are
    called from nowhere else. If ``tests/`` were left out of the scan, every
    one of them would be reported and the gate would be unusable on day one
    -- which is the pressure that produces an allowlist. The control here
    is the same scan without the reference directory: the helper *is*
    reported then, which proves the directory is genuinely being read for
    references rather than the finding being dropped for some other reason.
    """
    package, reference = _reference_only_layout(tmp_path)

    without_references = _scan(package)
    with_references = find_dead_code([package, reference], reference_only=(reference,))

    assert "shared_helper" in _names(without_references), (
        "control failed: with the reference-only tree left out of the scan, "
        "the helper should look dead. If it does not, this test proves "
        f"nothing. Findings were {without_references!r}"
    )
    assert "shared_helper" not in _names(with_references), (
        "a helper called from the reference-only tree is not dead code; "
        f"findings were {with_references!r}"
    )
    assert _names(with_references) == ["orphaned_helper"], (
        "the genuinely dead sibling must survive the reference-only pass, "
        f"or the whole package is being discarded; got {with_references!r}"
    )


def test_a_dead_symbol_inside_a_reference_only_path_is_not_reported(
    tmp_path: Path,
) -> None:
    """Findings located inside a reference-only path are discarded (#1395).

    ``tests/`` is scanned for its references, not for its own hygiene:
    fixtures, parametrisation helpers and assertion builders are reached
    through pytest's collection machinery, so reporting them would mean a
    gate that fails on correct code. The dead sibling in the *package*
    still has to be reported, which is what separates "discard findings
    from tests/" from "discard everything once tests/ is in the scan".
    """
    package, reference = _reference_only_layout(tmp_path)

    findings = find_dead_code([package, reference], reference_only=(reference,))

    assert "orphaned_fixture_builder" not in _names(findings), (
        "a dead symbol inside the reference-only tree must be discarded; "
        f"findings were {findings!r}"
    )
    inside = [f for f in findings if reference.resolve() in f.path.resolve().parents]
    assert inside == [], (
        f"no finding may be located inside a reference-only path; got {inside!r}"
    )
    assert _names(findings) == ["orphaned_helper"], (
        "the dead symbol in the scanned package must still be reported; "
        f"got {findings!r}"
    )


# ---------------------------------------------------------------------------
# Failing loudly on an unknown item type
# ---------------------------------------------------------------------------


def test_an_unknown_finding_type_raises_instead_of_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vulture upgrade that adds a category must break the gate loudly.

    The floors are keyed by vulture's item type. If a future vulture emits
    a category this table has never heard of, the tempting implementation
    -- ``CONFIDENCE_FLOORS.get(typ, 100)`` or a silent ``continue`` --
    would drop that whole category on the floor and the gate would go on
    reporting a cheerful zero. Raising means somebody has to decide on a
    floor for the new tier.

    The mapping is monkeypatched rather than a stub item pushed through an
    internal seam, which also pins that the floors are consulted per
    finding at call time.
    """
    package = _write_package(tmp_path, {"module": _ZERO_CALLER_SOURCE})
    without_functions = {
        typ: floor for typ, floor in CONFIDENCE_FLOORS.items() if typ != "function"
    }
    monkeypatch.setattr(lint_vulture, "CONFIDENCE_FLOORS", without_functions)

    with pytest.raises(UnknownFindingTypeError) as raised:
        _scan(package)

    assert "function" in str(raised.value), (
        "the error must name the unrecognised item type so the fix is "
        f"obvious; message was {str(raised.value)!r}"
    )
    assert issubclass(UnknownFindingTypeError, RuntimeError), (
        "UnknownFindingTypeError signals a misconfigured gate, not bad user "
        "input, and must stay a RuntimeError subclass"
    )


def test_healthy_code_produces_no_findings_at_all(tmp_path: Path) -> None:
    """An empty list means clean, and healthy code must produce one.

    The counterweight to every test above: a gate that reports a called
    function, a used parameter or an ordinary module constant is a gate
    developers will route around within a week, and routing around it is
    how the ``--min-confidence 80`` of #1395 got there in the first place.
    """
    package = _write_package(tmp_path, {"module": _CLEAN_SOURCE})

    findings = _scan(package)

    assert findings == [], (
        f"nothing in this module is dead, yet the gate reported {findings!r}"
    )


# ---------------------------------------------------------------------------
# The command-line entry point
# ---------------------------------------------------------------------------


def test_main_exits_zero_and_reports_nothing_when_the_scan_is_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean scan exits 0 and prints no findings.

    ``scripts/lint-vulture.sh`` propagates this exit code into
    ``check-all.sh`` and CI, so a non-zero exit on a clean tree would make
    the gate unpassable, and a finding printed on a clean tree would train
    reviewers to ignore its output.

    The scan itself is stubbed rather than pointed at a synthetic package:
    the wrapper deliberately gives ``main`` no positional paths, so this
    test pins only what ``main`` itself owns -- the exit code and the
    report -- and stays correct whatever argv shape it grows.
    """

    def _clean_scan(*_args: object, **_kwargs: object) -> list[Finding]:
        """Stand in for the scan, finding nothing."""
        return []

    monkeypatch.setattr(lint_vulture, "scan_scope", _clean_scan)

    exit_code = main([])

    assert exit_code == 0, f"a clean tree must exit 0, got {exit_code}"
    assert "unused" not in capsys.readouterr().out, (
        "nothing may be reported for a tree with no dead code"
    )


def test_main_exits_three_and_prints_every_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Findings exit 3, and every one of them is printed.

    Exit 3 distinguishes "the gate ran and found dead code" from a crash
    (1) or a usage error (2), which is what lets the wrapper report the
    difference. Printing *every* finding matters just as much: a gate that
    fails while showing only the first finding turns one cleanup into N
    round trips through CI. The call count is asserted too, so a ``main``
    that hard-codes an exit code without running the scan cannot pass.
    """
    canned = [
        Finding(
            path=Path("/repo/creek/link/eddies.py"),
            lineno=3,
            typ="function",
            name="_legacy_curve",
            confidence=60,
        ),
        Finding(
            path=Path("/repo/creek_mcp/tools/link.py"),
            lineno=9,
            typ="import",
            name="json",
            confidence=90,
        ),
    ]
    calls: list[object] = []

    def _stub_scan(*args: object, **kwargs: object) -> list[Finding]:
        """Record the call and return the canned findings."""
        calls.append((args, kwargs))
        return list(canned)

    monkeypatch.setattr(lint_vulture, "scan_scope", _stub_scan)

    exit_code = main([])

    output = capsys.readouterr().out
    assert len(calls) == 1, (
        f"main must run the scan exactly once, it ran it {len(calls)} time(s)"
    )
    assert exit_code == 3, (
        "dead code must exit 3 so the wrapper can tell it from a crash, "
        f"got {exit_code}"
    )
    for finding in canned:
        assert str(finding) in output, (
            f"{finding} was found but never printed; output was {output!r}"
        )


# ---------------------------------------------------------------------------
# The residual-zero regression gate
# ---------------------------------------------------------------------------


def test_the_repository_has_no_residual_dead_code() -> None:
    """The real tree, under the real policy, must be clean (#1395).

    This is the test that keeps the repository clean going forward. It is
    deliberately unmarked: ``scripts/test.sh --unit`` deselects ``slow``,
    so a ``@pytest.mark.slow`` here would move it out of the only lane that
    blocks a merge -- the same class of mistake as the ``--min-confidence
    80`` this issue removes.
    """
    findings = scan_scope(CREEK_TOOLS)

    listing = "\n".join(f"  {finding}" for finding in findings)
    assert findings == [], (
        f"{len(findings)} dead symbol(s) are reachable from no caller:\n"
        f"{listing}\n"
        "Delete them. Do NOT add an allowlist entry, do NOT raise a floor in "
        "scripts/lint_vulture.py, and do NOT add a name to "
        "IMPLICITLY_INVOKED_NAMES unless the interpreter or a framework "
        "really does invoke it by protocol -- in which case add the "
        "category (a decorator pattern or a protocol name), never the "
        "individual symbol."
    )


def test_the_crawdad_tree_has_no_residual_dead_code() -> None:
    """The sibling subproject is gated by the same policy (#1472).

    Before this landed, ``crawdad/`` was outside the dead-code gate
    entirely: #1395 built the policy here and wired it into this project's
    ``check-all.sh`` and CI, and crawdad's ``check-all.sh`` ran seven gates,
    none of them vulture. A zero-caller function added under ``crawdad/``
    was reported by nothing.

    Asserting it from *this* suite as well as from crawdad's own
    ``check-all.sh`` is deliberate. The policy module lives here, so a
    carve-out edited here can break the other subproject; without this
    test that breakage would only surface in a different CI job, on a
    different matrix, after the change had already been reviewed.
    """
    assert CRAWDAD.root.is_dir(), (
        f"{CRAWDAD.root} is not a directory, so this gate is scanning "
        "nothing and reporting clean -- the exact shape of failure the "
        "whole issue is about. The sibling checkout must be present."
    )
    findings = scan_scope(CRAWDAD)

    listing = "\n".join(f"  {finding}" for finding in findings)
    assert findings == [], (
        f"{len(findings)} dead symbol(s) in crawdad/ are reachable from no "
        f"caller:\n{listing}\n"
        "Delete them. The policy is shared with creek-tools, so do NOT "
        "answer a crawdad finding by weakening a floor or adding a carve-out "
        "here unless the carve-out is categorical and true of both trees."
    )


# ---------------------------------------------------------------------------
# One policy, two subprojects (#1472)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [SCOPES[name] for name in sorted(SCOPES)])
def test_every_scope_resolves_to_directories_that_exist(scope: Scope) -> None:
    """A scope naming a directory that is not there scans nothing, silently.

    ``Vulture.scavenge`` prints a message for a missing path and carries on,
    so a scope with a typo'd or renamed entry produces a clean run rather
    than an error. That is the "gate reports it did nothing" failure, and
    the only defence is to assert the surface is real.

    Args:
        scope: One declared scope.
    """
    assert scope.root.is_dir(), f"{scope.name}: root {scope.root} is not a directory"
    for path in scope.scan_paths:
        assert path.is_dir(), (
            f"{scope.name}: scan path {path} does not exist, so that whole "
            "subtree is silently ungated"
        )
    for path in scope.reference_only_paths:
        assert path.is_dir(), (
            f"{scope.name}: reference-only path {path} does not exist. A "
            "reference-only root that resolves to nothing filters nothing, "
            "and every finding inside the real tree gets reported instead."
        )


def test_the_two_scopes_resolve_under_different_roots() -> None:
    """Neither scope may resolve inside the other's tree.

    This is the fail-open #1472 measured. When reference-only roots were
    relative names anchored at one module-level project root, asking for
    ``crawdad/tests`` resolved to ``creek-tools/crawdad/tests`` -- a path
    that does not exist -- so nothing was filtered and findings inside
    crawdad's own test tree were reported as production dead code.
    """
    assert not CRAWDAD.root.is_relative_to(CREEK_TOOLS.root), (
        f"{CRAWDAD.root} resolved inside {CREEK_TOOLS.root}. A scope anchored "
        "at the wrong project root matches nothing and fails open."
    )
    for path in (*CRAWDAD.scan_paths, *CRAWDAD.reference_only_paths):
        assert path.is_relative_to(CRAWDAD.root), (
            f"{path} is not under the crawdad scope's own root; scope paths "
            "must be joined to the scope's root, never to a module default."
        )


def test_a_relative_reference_only_root_is_refused(tmp_path: Path) -> None:
    """A relative reference-only root must raise, not be guessed at (#1472).

    Guessing an anchor fails *open*: the guess resolves somewhere that does
    not exist, containment matches nothing, and every finding the root was
    meant to drop gets reported. Measured on crawdad at the time: 12
    findings under the relative form against 11 under the absolute one, the
    twelfth being an ``unreachable_code`` finding inside
    ``crawdad/tests/test_cli.py`` that leaked through. Refusing is the only
    reading of a relative root that cannot go quietly wrong.

    Args:
        tmp_path: Scratch directory for the synthetic package.
    """
    package = _write_package(tmp_path / "pkg", {"module": _ZERO_CALLER_SOURCE})

    with pytest.raises(RelativeReferenceRootError, match="relative"):
        find_dead_code([package], reference_only=(Path("tests"),))


def test_a_scope_reference_only_root_suppresses_a_finding_inside_it(
    tmp_path: Path,
) -> None:
    """The absolute form does filter -- the positive half of the pair above.

    Without this, :func:`test_a_relative_reference_only_root_is_refused`
    would be satisfied by a module that refuses relative roots and then
    ignores absolute ones too.

    Args:
        tmp_path: Scratch directory for the synthetic trees.
    """
    scope = Scope(
        name="synthetic",
        root=tmp_path,
        scan=("pkg", "tests"),
        reference_only=("tests",),
    )
    _write_package(tmp_path / "pkg", {"module": _CLEAN_SOURCE})
    _write_package(tmp_path / "tests", {"test_orphan": _ZERO_CALLER_SOURCE})

    assert _names(find_dead_code(scope.scan_paths)) == ["orphaned_helper"], (
        "the planted orphan is not being reported at all, so the suppression "
        "asserted below would prove nothing"
    )
    assert scan_scope(scope) == [], (
        "a finding inside a reference-only root must be dropped; the root is "
        "scanned for the references it makes, not for the code it holds"
    )


def test_the_crawdad_wrapper_runs_the_shared_policy_and_is_wired_into_its_gate() -> (
    None
):
    """crawdad's gate must execute this module, and its check-all must run it.

    Two ways #1472 could regress into a green run, both closed here: the
    wrapper could grow into a *copy* of the policy (four call sites that
    will eventually disagree about a threshold -- the drift #1395's
    single-wrapper design exists to prevent), and the wrapper could exist
    but never be invoked, which is precisely the state the pre-#1395
    vulture invocations were in.
    """
    wrapper = CRAWDAD.root / "scripts" / "lint-vulture.sh"
    check_all = CRAWDAD.root / "scripts" / "check-all.sh"
    assert wrapper.is_file(), f"{wrapper} is missing; crawdad has no dead-code gate"

    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "scripts.lint_vulture" in wrapper_text, (
        f"{wrapper} no longer runs the shared policy module. A second copy of "
        "the policy is how the two subprojects start disagreeing about a "
        "floor without anyone deciding to."
    )
    assert f"--scope {CRAWDAD.name}" in wrapper_text, (
        f"{wrapper} must select the crawdad scope explicitly; without it the "
        "module defaults to creek-tools and crawdad's gate scans the wrong "
        "tree while reporting success."
    )
    assert "lint-vulture.sh" in check_all.read_text(encoding="utf-8"), (
        f"{check_all} does not run the dead-code gate. A gate nothing invokes "
        "is the state this issue found crawdad in."
    )


def test_crawdad_declares_the_vulture_dependency_its_gate_needs() -> None:
    """crawdad's CI installs from its own lock, so vulture must be in it.

    The shared policy module is executed by *crawdad's* interpreter in
    crawdad's CI job, which provisions itself from ``crawdad/uv.lock`` and
    nothing else. An undeclared ``vulture`` would make the gate crash there
    while passing on a developer machine that happens to have creek-tools'
    virtualenv on ``PATH``.
    """
    for artefact, needle in (
        (CRAWDAD.root / "pyproject.toml", "vulture"),
        (CRAWDAD.root / "uv.lock", 'name = "vulture"'),
    ):
        assert needle in artefact.read_text(encoding="utf-8"), (
            f"{artefact} does not mention {needle!r}. crawdad's CI installs "
            "from the lock, so an undeclared gate dependency fails the job "
            "rather than the policy."
        )


# ---------------------------------------------------------------------------
# Carve-outs that only became visible once crawdad was in scope (#1472)
# ---------------------------------------------------------------------------

# ``cls`` is never read, so vulture types it as an unused *variable* at
# 100% -- above the 90 floor, and out of reach of ``ignore_decorators``,
# which suppresses findings on the decorated function and not on its
# parameters. ``orphaned_method`` is the control: a genuinely dead sibling
# in the same class must still be reported.
_UNREAD_CLS_SOURCE = """
from pydantic import BaseModel, field_validator


class Gadget(BaseModel):
    \"\"\"A model whose validator never reads its bound class.\"\"\"

    size: int

    @field_validator("size")
    @classmethod
    def clamp_size(cls, value: int) -> int:
        \"\"\"Invoked by pydantic; the bound class is never read.\"\"\"
        return max(value, 0)

    def orphaned_method(self) -> None:
        \"\"\"Dead: an ordinary method with no caller.\"\"\"


SCHEMA = Gadget
"""

# ``Sequence`` is used only inside a *string* annotation, which vulture
# never evaluates, so it reports the import as unused at 90%. ``json`` is
# the control: an ordinary unused import outside the guard, which must
# still be reported.
_STRING_ANNOTATION_SOURCE = """
from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


def widen(values: object) -> object:
    \"\"\"Return the values under a quoted annotation vulture cannot read.\"\"\"
    return cast("Sequence[int]", values)


WIDENED = widen([1])
"""


def test_an_unread_cls_parameter_is_carved_out_and_a_dead_sibling_is_not(
    tmp_path: Path,
) -> None:
    """``cls`` is bound by the interpreter; not reading it is not dead code.

    Every one of crawdad's eight pydantic validators reported this way the
    day the gate was pointed at it, and creek-tools was clean only by
    accident: vulture resolves used names globally by bare identifier, so
    one unrelated ``@classmethod`` in ``creek/`` that happens to mention
    ``cls`` kept every ``cls`` parameter in the scan alive.

    The dead sibling in the same class is the non-vacuousness control: a
    carve-out that silenced the whole file would pass without it.

    Args:
        tmp_path: Scratch directory for the synthetic package.
    """
    package = _write_package(tmp_path / "pkg", {"models": _UNREAD_CLS_SOURCE})

    assert _names(_scan(package)) == ["orphaned_method"], (
        "expected the unread `cls` parameter to be carved out and the dead "
        "sibling method to survive; got "
        f"{[str(finding) for finding in _scan(package)]!r}"
    )


def test_a_type_checking_import_used_in_a_string_annotation_is_carved_out(
    tmp_path: Path,
) -> None:
    """Deleting it would break mypy, so the two gates must not contradict.

    Vulture parses the AST and never evaluates a string annotation, so an
    import reachable only from ``cast("Sequence[int]", ...)`` or a quoted
    annotation looks unreferenced. Nothing is ceded by dropping the
    category: ruff's ``F401`` is selected in both subprojects and does
    understand those use sites.

    The ordinary unused import outside the guard is the control.

    Args:
        tmp_path: Scratch directory for the synthetic package.
    """
    package = _write_package(tmp_path / "pkg", {"widen": _STRING_ANNOTATION_SOURCE})

    assert _names(_scan(package)) == ["json"], (
        "expected the TYPE_CHECKING import to be carved out and the ordinary "
        "unused import to survive; got "
        f"{[str(finding) for finding in _scan(package)]!r}"
    )


def test_an_unreadable_source_file_still_reports_its_unused_import(
    tmp_path: Path,
) -> None:
    """The TYPE_CHECKING carve-out must fail *safe*, not open.

    The carve-out re-reads the file as UTF-8 to find the guard's line
    range. Vulture itself honours a PEP-263 coding cookie, so it happily
    scans a latin-1 module the re-read cannot decode. If that failure were
    read as "no guard found, therefore carve everything out", a single
    undecodable file would silently drop every import finding it contains.
    It is read the other way: nothing is carved out, and the finding
    stands.

    Args:
        tmp_path: Scratch directory for the synthetic package.
    """
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"")
    # A latin-1 coding cookie plus a byte that is not valid UTF-8.
    (package / "legacy.py").write_bytes(
        b'# -*- coding: latin-1 -*-\nimport json\nCAF\xc9 = "caf\xe9"\nUSED = CAF\xc9\n'
    )

    assert _names(_scan(package)) == ["json"], (
        "an import finding in a file the carve-out cannot re-read must "
        "survive; got "
        f"{[str(finding) for finding in _scan(package)]!r}"
    )


# ---------------------------------------------------------------------------
# The scope selector
# ---------------------------------------------------------------------------


def _record_scanned_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Scope]:
    """Replace the scan with a recorder, so a test can read the scope back.

    Args:
        monkeypatch: Used to stand in for the scan.

    Returns:
        The list the recorder appends each scanned scope to.
    """
    scanned: list[Scope] = []

    def _record(scope: Scope) -> list[Finding]:
        """Record the requested scope and report nothing."""
        scanned.append(scope)
        return []

    monkeypatch.setattr(lint_vulture, "scan_scope", _record)
    return scanned


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param([], CREEK_TOOLS, id="bare-invocation"),
        pytest.param(["--scope", "creek-tools"], CREEK_TOOLS, id="explicit"),
        pytest.param(["--scope", "crawdad"], CRAWDAD, id="sibling"),
    ],
)
def test_the_command_line_selects_the_scope_it_names(
    argv: list[str],
    expected: Scope,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each wrapper's invocation must reach the scope that wrapper means.

    The bare form is pinned because it is what ``scripts/lint-vulture.sh``
    actually passes: leaving the default untested is how a default silently
    starts pointing at the wrong tree while every explicit test stays green.

    Args:
        argv: The command line.
        expected: The scope it must select.
        monkeypatch: Used to stand in for the scan.
        capsys: Captures the report.
    """
    scanned = _record_scanned_scope(monkeypatch)

    exit_code = main(argv)

    assert exit_code == 0, f"a clean scan must exit 0, got {exit_code}"
    assert scanned == [expected], (
        f"{argv!r} scanned {[scope.name for scope in scanned]!r}, expected "
        f"[{expected.name!r}]"
    )
    assert expected.name in capsys.readouterr().out, (
        "the report must name the scope it scanned, or two wrappers produce "
        "indistinguishable output"
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["creek"], id="positional-path"),
        pytest.param(["--scope"], id="flag-without-value"),
        pytest.param(["--scope", "nonesuch"], id="unknown-scope"),
        pytest.param(["--scope", "crawdad", "creek"], id="scope-plus-path"),
    ],
)
def test_a_command_line_that_could_narrow_the_scan_is_refused(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a whole named scope may be requested.

    A gate call site that could pass a path could narrow itself into a
    green run -- the property ``scripts/lint-vulture.sh`` states in its
    header and had no enforcement for. Exit 2 is distinct from 3 so a
    wrapper can tell a bad invocation from real findings, and the scan must
    not run at all.

    Args:
        argv: A command line the module must refuse.
        monkeypatch: Used to prove the scan never ran.
        capsys: Captures the usage message.
    """
    scanned = _record_scanned_scope(monkeypatch)

    exit_code = main(argv)

    assert exit_code == 2, f"a usage error must exit 2, got {exit_code}"
    assert scanned == [], (
        f"{argv!r} was refused but the scan ran anyway on "
        f"{[scope.name for scope in scanned]!r}"
    )
    assert "usage:" in capsys.readouterr().err
