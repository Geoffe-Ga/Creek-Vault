"""A source-scanning guard against a silently-skipping ``e2e`` lane (#1345).

``tests/e2e/`` is a hermetic, *blocking* lane: the marker table in
``pyproject.toml`` declares it, and the CI job ``integration-e2e`` runs it on
every pull request. A test in a blocking lane that quietly skips is a merge
gate that is not gating — green, and pinning nothing.

Issue #1345 fixed exactly one such case. ``test_full_pipeline_purge.py``
wrapped its imports in ``try: ... except ImportError: pytest.skip(...)``,
guarding names that have been unconditionally importable for a long time. The
whole module could therefore have dropped out of the lane at any moment — on a
rename, a moved symbol, a broken optional extra — and the build would still
have been green. This module is the *tree-wide* net, so that the next author of
a new e2e module cannot reintroduce the pattern in a neighbouring file.

Five syntactic forms are banned, each by its own finder and its own test, so a
failure names the rule that broke:

1. ``pytest.skip(...)`` — an imperative skip at runtime.
2. ``pytest.importorskip(...)`` — a skip disguised as an import.
3. ``pytest.mark.skip`` / ``pytest.mark.skipif`` — as a decorator, as an entry
   in a module-level ``pytestmark`` list, or called with a reason.
4. ``pytest.mark.xfail`` — a failure declared expected instead of fixed.
5. ``pytest.xfail(...)`` — the runtime twin of rule 4. It never goes through
   ``.mark.``, so rule 4's attribute scan cannot see it; banning only the
   declarative spelling would wave the imperative one through.

Rules 1 and 5 exclude anything reached through a ``.mark.`` chain, which is
what keeps ``@pytest.mark.skip(reason=...)`` — simultaneously a call named
``skip`` and a marker attribute — owned by exactly one rule.

**What the scan matches, and therefore what it misses.** Rules are keyed on the
*spelling at the call site*: the callee's own name for the call rules, and an
attribute read through ``.mark.`` for the marker rules. That covers the forms
anyone writes by hand, including a marker nested inside ``pytest.param(...,
marks=...)`` or handed to ``request.applymarker(...)``, since :func:`ast.walk`
finds the attribute node wherever it sits. It does not cover a renamed alias
(``from pytest import skip as sk``) or a dynamically-assembled reference
(``getattr(pytest, "skip")(...)``), because neither leaves the banned name in
the position the rule reads. Those are deliberate-evasion spellings rather than
the accident this guard exists to catch, and matching them would mean tracking
imports and bindings across a module — a materially different tool. The claim
here is coverage of the honest mistake, not of the determined author.

**There is no allowlist, and the escape hatch is a different lane.** Anything
that genuinely needs credentials or a live service belongs on the ``live``
marker, which CI never runs, rather than skipping from inside a lane that
blocks the merge. As of #1345 there is no ``pytest.mark.live`` anywhere under
``tests/e2e/``, so nothing needs exempting.

**A sixth rule was considered and deliberately rejected**: banning
``assert x is not None``, the shape of the unfalsifiable assertion #1345
removed from the purge module. Optional-ness is not decidable from syntax, and
the rule is red on correct code — ``tests/e2e/test_full_pipeline_linking.py``
line 306 (``assert match is not None, ...``, narrowing an ``re.Match | None``
before reading the output it matched) and
``tests/e2e/test_voice_authenticity_epic_e2e.py`` line 231
(``assert deslop is not None``). Dropping the one assertion that really was
vacuous — against a non-Optional ``purge_source`` return — stays a local edit
to the purge module. A guard that fails the build on correct code teaches
people to disable guards.

The scan is deliberately syntactic, and here that matters more than usual.
Every banned name appears in this module's own prose; ``skip`` also appears in
the coverage flag ``--skip-covered``; and ``tests/e2e/`` itself contains
``test_pipeline_skips_ingestion_without_consent``, a perfectly good test whose
*name* says "skips". A regex guard fires on all three. An AST walk reads
*code*: docstrings and comments parse to string constants, and a function name
containing "skip" is not a call to ``skip``. Precedent for source-scanning
tests in this repo: ``test_timestamp_derivation_guard.py`` (#1329),
``test_dependency_pins.py``, ``test_ruff_cache_poisoning.py``.

This module is itself unmarked, so it runs in the *unit* lane. A guard over the
e2e lane that only ran inside the e2e lane would be one bad conftest away from
never running at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

E2E_LANE = "e2e"
"""Directory under ``tests/`` holding the blocking end-to-end lane."""

SKIP_MARK_ATTRS = frozenset({"skip", "skipif"})
"""``pytest.mark.<attr>`` names that take a test out of the lane."""

XFAIL_MARK_ATTRS = frozenset({"xfail"})
"""``pytest.mark.<attr>`` names that excuse a failure instead of fixing it."""


def _e2e_root() -> Path:
    """Return the ``tests/e2e/`` directory scanned by every rule.

    Returns:
        The directory holding the blocking end-to-end lane.
    """
    return Path(__file__).resolve().parent / E2E_LANE


def _python_files() -> list[Path]:
    """Return every ``.py`` file under the e2e lane.

    ``conftest.py`` and ``__init__.py`` are deliberately in scope: a skip in
    either takes the whole lane down, which is the worst version of the bug.
    The ``.suffix`` filter means ``__pycache__`` contributes nothing.

    Returns:
        A sorted list of module paths. Non-empty by construction; the
        emptiness check lives in its own test so a broken glob cannot make
        every other rule pass vacuously.
    """
    return sorted(path for path in _e2e_root().rglob("*.py") if path.suffix == ".py")


def _parse(path: Path) -> ast.Module:
    """Parse *path* into an AST module.

    Args:
        path: The Python file to parse.

    Returns:
        The parsed module.
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str:
    """Return the name of *node*'s callee for matching.

    Args:
        node: The call node.

    Returns:
        The attribute name for ``a.b()``, the bare name for ``b()``, or the
        empty string for anything more exotic (a call on a subscript, say).
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_mark_chain(node: ast.expr) -> bool:
    """Report whether *node* reads an attribute through ``.mark.``.

    This is the single dividing line between the call rules and the marker
    rules, so no site is reported twice. ``pytest.skip`` parses to
    ``Attribute(attr='skip', value=Name('pytest'))`` while
    ``pytest.mark.skip`` parses to
    ``Attribute(attr='skip', value=Attribute(attr='mark'))``; only the latter
    belongs to the marker rules, including when it is called as
    ``@pytest.mark.skip(reason=...)``.

    Args:
        node: Any expression node.

    Returns:
        ``True`` for ``<anything>.mark.<attr>``, ``False`` otherwise.
    """
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def _relative(path: Path) -> str:
    """Render *path* relative to the lane root for readable assertions.

    Args:
        path: An absolute module path.

    Returns:
        The path relative to ``tests/e2e/``, as a POSIX string.
    """
    return path.relative_to(_e2e_root()).as_posix()


def _mark_attribute_sites(attrs: frozenset[str]) -> list[str]:
    """Find every ``<x>.mark.<attr>`` read whose ``attr`` is in *attrs*.

    One entry per site regardless of spelling: the bare decorator
    ``@pytest.mark.skip``, the called ``@pytest.mark.skipif(...)`` and the
    ``pytestmark = [pytest.mark.skip]`` list entry each parse to exactly one
    matching :class:`ast.Attribute` node.

    Args:
        attrs: Marker names to report.

    Returns:
        ``"<file>:<line>"`` for each matching attribute read.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in attrs and _is_mark_chain(node):
                offenders.append(f"{_relative(path)}:{node.lineno}")
    return offenders


def _imperative_call_sites(name: str) -> list[str]:
    """Find every imperative call to *name* in the lane.

    Matches any call whose callee is named *name*, covering both the
    ``pytest.<name>(...)`` spelling and the bare ``<name>(...)`` that
    ``from pytest import <name>`` permits. Sites reached through the
    ``.mark.`` chain are excluded and left to :func:`_mark_attribute_sites`,
    so no line is ever reported by two rules.

    Args:
        name: The callee name to report, e.g. ``"skip"``.

    Returns:
        ``"<file>:<line>"`` for each offending call site.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or _call_name(node) != name:
                continue
            if _is_mark_chain(node.func):
                continue
            offenders.append(f"{_relative(path)}:{node.lineno}")
    return offenders


def _imperative_skip_sites() -> list[str]:
    """Find every imperative ``skip(...)`` call in the lane.

    Returns:
        ``"<file>:<line>"`` for each offending call site.
    """
    return _imperative_call_sites("skip")


def _imperative_xfail_sites() -> list[str]:
    """Find every imperative ``xfail(...)`` call in the lane.

    ``pytest.xfail()`` aborts the running test and reports it as expectedly
    failing, so it is the runtime twin of the ``xfail`` marker and needs its
    own rule: the marker scan reads attribute chains through ``.mark.`` and
    cannot see this spelling.

    Returns:
        ``"<file>:<line>"`` for each offending call site.
    """
    return _imperative_call_sites("xfail")


def _importorskip_sites() -> list[str]:
    """Find every ``importorskip(...)`` call in the lane.

    Returns:
        ``"<file>:<line>"`` for each offending call site.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and _call_name(node) == "importorskip":
                offenders.append(f"{_relative(path)}:{node.lineno}")
    return offenders


def _skip_mark_sites() -> list[str]:
    """Find every ``pytest.mark.skip`` / ``pytest.mark.skipif`` in the lane.

    Returns:
        ``"<file>:<line>"`` for each offending marker reference.
    """
    return _mark_attribute_sites(SKIP_MARK_ATTRS)


def _xfail_mark_sites() -> list[str]:
    """Find every ``pytest.mark.xfail`` in the lane.

    Returns:
        ``"<file>:<line>"`` for each offending marker reference.
    """
    return _mark_attribute_sites(XFAIL_MARK_ATTRS)


def test_the_scan_actually_reaches_the_e2e_lane() -> None:
    """The file glob finds modules, so the rules below cannot pass vacuously.

    A guard that scans nothing is green forever, and this one globs a single
    directory whose name is a string constant — one typo and every rule below
    reports ``[]`` truthfully and uselessly. The count is a floor rather than
    the exact 14 files present at #1345 so that adding an e2e module is not a
    spurious failure, and two known members are named: a regular test module
    and ``conftest.py``, whose in-scope-ness the rules depend on.
    """
    files = _python_files()
    assert len(files) >= 10
    names = {_relative(path) for path in files}
    assert "test_full_pipeline_purge.py" in names
    assert "conftest.py" in names


def test_no_e2e_module_skips_imperatively() -> None:
    """Nothing in the blocking lane calls ``pytest.skip`` at runtime.

    This is the #1345 bug's exact shape: an ``except ImportError`` arm that
    skipped the module rather than failing it. An import that cannot be
    satisfied in a hermetic lane is a broken build, not an absent test.
    """
    assert _imperative_skip_sites() == []


def test_no_e2e_module_uses_importorskip() -> None:
    """Nothing in the blocking lane makes its own presence conditional.

    ``pytest.importorskip`` is the same opt-out as an ``except ImportError``
    skip with less ceremony. The e2e lane's dependencies are installed by
    ``uv sync --all-extras``; if one is missing the lane must go red.
    """
    assert _importorskip_sites() == []


def test_no_e2e_module_is_marked_skip_or_skipif() -> None:
    """No declarative skip marker removes an e2e test from the lane.

    Covers the decorator, the called ``skipif(...)`` form and the
    module-level ``pytestmark`` list, which is the form that silences an
    entire file at once and the easiest to miss in review.
    """
    assert _skip_mark_sites() == []


def test_no_e2e_module_is_marked_xfail() -> None:
    """No e2e test declares its own failure expected.

    ``xfail`` in a merge-blocking lane converts a regression into a green
    check. If a behaviour is known-broken, the issue tracker records it and
    the test is written to pass once it is fixed.
    """
    assert _xfail_mark_sites() == []


def test_no_e2e_module_calls_xfail_imperatively() -> None:
    """Nothing in the blocking lane bails out via ``pytest.xfail()``.

    The runtime twin of the marker, and invisible to the marker scan because
    it never goes through ``.mark.``. Without this rule the guard would ban
    the declarative spelling of "expected to fail" while waving the
    imperative one through.
    """
    assert _imperative_xfail_sites() == []


@pytest.mark.parametrize(
    ("snippet", "finder", "expected_line"),
    [
        pytest.param(
            'import pytest\n\n\ndef test_thing():\n    pytest.skip("x")\n',
            _imperative_skip_sites,
            5,
            id="imperative-skip",
        ),
        pytest.param(
            'import pytest\n\nmod = pytest.importorskip("creek.purge")\n',
            _importorskip_sites,
            3,
            id="importorskip",
        ),
        pytest.param(
            'import pytest\n\n\n@pytest.mark.skipif(True, reason="x")\n'
            "def test_thing():\n    pass\n",
            _skip_mark_sites,
            4,
            id="skipif-decorator",
        ),
        pytest.param(
            "import pytest\n\npytestmark = [pytest.mark.skip]\n",
            _skip_mark_sites,
            3,
            id="skip-pytestmark-list",
        ),
        pytest.param(
            "import pytest\n\n\n@pytest.mark.xfail\ndef test_thing():\n    pass\n",
            _xfail_mark_sites,
            4,
            id="xfail-decorator",
        ),
        pytest.param(
            'import pytest\n\n\ndef test_thing():\n    pytest.xfail("x")\n',
            _imperative_xfail_sites,
            5,
            id="imperative-xfail",
        ),
    ],
)
def test_each_rule_bites_when_the_pattern_is_reintroduced(
    snippet: str,
    finder: object,
    expected_line: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each rule really detects its pattern, rather than being green by luck.

    The lane is clean today, so every rule above asserts ``== []`` — a result
    a scanner that had decayed into a no-op would also produce. Rather than
    mutating the real lane, each rule is pointed at a one-file scratch
    directory containing exactly the form it forbids, and must name the file
    and the line.

    Args:
        snippet: Source text reintroducing one forbidden pattern.
        finder: The rule's finder function.
        expected_line: Line within *snippet* the finder must report.
        monkeypatch: Used to redirect the scan at the scratch directory.
        tmp_path: Pytest temp directory holding the scratch module.
    """
    (tmp_path / "offender.py").write_text(snippet, encoding="utf-8")

    monkeypatch.setattr(
        "tests.test_e2e_lane_never_skips._e2e_root",
        lambda: tmp_path,
    )

    assert callable(finder)
    assert finder() == [f"offender.py:{expected_line}"]


@pytest.mark.parametrize(
    ("decorator", "mark_finder", "call_finder"),
    [
        pytest.param(
            '@pytest.mark.skip(reason="x")',
            _skip_mark_sites,
            _imperative_skip_sites,
            id="skip",
        ),
        pytest.param(
            '@pytest.mark.xfail(reason="x")',
            _xfail_mark_sites,
            _imperative_xfail_sites,
            id="xfail",
        ),
    ],
)
def test_the_called_marker_forms_are_reported_by_one_rule_only(
    decorator: str,
    mark_finder: object,
    call_finder: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A called marker belongs to the marker rule alone, never to both.

    ``@pytest.mark.skip(reason=...)`` is simultaneously a call whose callee is
    named ``skip`` and an attribute read through ``.mark.``; ``xfail`` has the
    same double shape. Without the :func:`_is_mark_chain` split each would be
    reported by two rules, and a reader chasing the imperative failure would
    go looking for a runtime skip that is not there. This pins ownership.

    Args:
        decorator: The called-marker decorator to seed.
        mark_finder: The rule that must own the site.
        call_finder: The rule that must stay silent about it.
        monkeypatch: Used to redirect the scan at the scratch directory.
        tmp_path: Pytest temp directory holding the scratch module.
    """
    snippet = f"import pytest\n\n\n{decorator}\ndef test_thing():\n    pass\n"
    (tmp_path / "offender.py").write_text(snippet, encoding="utf-8")

    monkeypatch.setattr(
        "tests.test_e2e_lane_never_skips._e2e_root",
        lambda: tmp_path,
    )

    assert callable(mark_finder)
    assert callable(call_finder)
    assert mark_finder() == ["offender.py:4"]
    assert call_finder() == []
