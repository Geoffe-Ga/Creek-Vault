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
    DEFAULT_SCAN_PATHS,
    IGNORE_DECORATORS,
    IMPLICITLY_INVOKED_NAMES,
    LEGACY_MIN_CONFIDENCE,
    REFERENCE_ONLY_PATHS,
    Finding,
    UnknownFindingTypeError,
    find_dead_code,
    main,
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

    ``reference_only`` is passed empty on purpose: the default names the
    repository's own ``tests/`` tree, and a test that leaves it in place
    would be asserting about a synthetic package *and* whatever the real
    suite happens to contain.

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
    assert DEFAULT_SCAN_PATHS == ("creek", "creek_mcp", "tests"), (
        "the gate must scan both shipped packages plus the test tree; "
        f"got {DEFAULT_SCAN_PATHS!r}"
    )
    assert REFERENCE_ONLY_PATHS == ("tests",), (
        f"tests/ is scanned for references only; got {REFERENCE_ONLY_PATHS!r}"
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

    ``REFERENCE_ONLY_PATHS`` is the relative name ``tests``. If containment
    were decided by looking for that name among a finding's path
    components, then a perfectly ordinary clone into ``~/tests/creek-tools``
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

    findings = find_dead_code([package], reference_only=REFERENCE_ONLY_PATHS)

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
    with_references = find_dead_code(
        [package, reference], reference_only=(str(reference),)
    )

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

    findings = find_dead_code([package, reference], reference_only=(str(reference),))

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

    monkeypatch.setattr(lint_vulture, "find_dead_code", _clean_scan)

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

    monkeypatch.setattr(lint_vulture, "find_dead_code", _stub_scan)

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
    findings = find_dead_code()

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
