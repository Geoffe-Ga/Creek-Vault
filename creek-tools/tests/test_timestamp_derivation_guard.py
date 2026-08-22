"""A source-scanning guard against reintroducing host-dependent timestamps (#1329).

:func:`creek.ingest.base.generate_fragment_id` hashes a fragment's timestamp,
so any host-dependent input to that timestamp becomes part of the fragment's
identity. Two such inputs shipped for a long time and both *looked like*
simplifications, which is exactly why a docstring is not sufficient protection:

* ``datetime.fromtimestamp(mtime)`` with no ``tz=`` renders the epoch in the
  host's local zone, so one file minted a different id in every timezone.
* ``getattr(stat, "st_birthtime", stat.st_mtime)`` reads a field present on
  macOS/BSD and absent on Linux, so one file minted a different id per
  operating system.

The behavioural regression tests in ``test_ingest_id_host_independence.py``
pin the two ingestors that had the bug. This module is the *tree-wide* net: it
walks the AST of every module under ``creek/`` and ``creek_mcp/`` so a new
ingestor cannot quietly reintroduce either pattern somewhere the behavioural
tests do not look. Precedent for source-scanning tests in this repo:
``test_dependency_pins.py``, ``test_config_contract.py``,
``test_ruff_cache_poisoning.py``.

**A third rule was considered and deliberately rejected**: "no ``.st_mtime``
attribute read under ``creek/ingest/`` outside ``file_modified_time``". It is
red on ``creek/ingest/gdrive.py`` (``GoogleDriveDownloader._is_up_to_date``),
which compares a local file's mtime against Drive's to decide whether a
re-download is needed. That is a staleness comparison, not a fragment-timestamp
derivation, and it is correct as written. A guard that fails the build on
correct code teaches people to disable guards.

The scan is deliberately syntactic. It reads *code*, not prose — docstrings and
comments discussing the forbidden patterns (this module is full of them, and so
are the fixed call sites) are invisible to it, because they parse to string
constants and comments rather than to calls and attribute reads.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOTS = ("creek", "creek_mcp")
"""Package directories scanned by every rule in this module."""


def _repo_source_root() -> Path:
    """Return the ``creek-tools/`` directory holding the scanned packages.

    Returns:
        The directory containing ``creek/`` and ``creek_mcp/``.
    """
    return Path(__file__).resolve().parent.parent


def _python_files() -> list[Path]:
    """Return every ``.py`` file under the scanned package roots.

    Returns:
        A sorted list of module paths. Non-empty by construction; the
        emptiness check lives in its own test so a broken glob cannot make
        every other rule pass vacuously.
    """
    root = _repo_source_root()
    files: list[Path] = []
    for package in PACKAGE_ROOTS:
        files.extend((root / package).rglob("*.py"))
    return sorted(files)


def _parse(path: Path) -> ast.Module:
    """Parse *path* into an AST module.

    Args:
        path: The Python file to parse.

    Returns:
        The parsed module.
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str:
    """Return the dotted-ish name of *node*'s callee for matching.

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


def _relative(path: Path) -> str:
    """Render *path* relative to the source root for readable assertions.

    Args:
        path: An absolute module path.

    Returns:
        The path relative to ``creek-tools/``, as a POSIX string.
    """
    return path.relative_to(_repo_source_root()).as_posix()


def _naive_fromtimestamp_sites() -> list[str]:
    """Find every ``fromtimestamp(...)`` call that omits a ``tz`` keyword.

    Returns:
        ``"<file>:<line>"`` for each offending call site.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) != "fromtimestamp":
                continue
            if any(keyword.arg == "tz" for keyword in node.keywords):
                continue
            offenders.append(f"{_relative(path)}:{node.lineno}")
    return offenders


def _birthtime_sites() -> list[str]:
    """Find every reference to ``st_birthtime`` in executable code.

    Catches both spellings that matter: a direct ``stat.st_birthtime``
    attribute read, and the ``getattr(stat, "st_birthtime", ...)`` form the
    bug actually shipped as — which hides the field name inside a string
    constant where an attribute-only scan would miss it.

    Returns:
        ``"<file>:<line>"`` for each offending reference.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if (isinstance(node, ast.Attribute) and node.attr == "st_birthtime") or (
                isinstance(node, ast.Call)
                and _call_name(node) == "getattr"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == "st_birthtime"
                    for arg in node.args
                )
            ):
                offenders.append(f"{_relative(path)}:{node.lineno}")
    return offenders


def test_the_scan_actually_reaches_the_source_tree() -> None:
    """The file glob finds modules, so the rules below cannot pass vacuously.

    A guard that scans nothing is green forever. This pins the scan's own
    precondition, and asserts the two packages the rules claim to cover are
    both really in the set.
    """
    files = _python_files()
    assert len(files) > 100
    scanned = {_relative(path).split("/")[0] for path in files}
    assert scanned == set(PACKAGE_ROOTS)
    assert "creek/ingest/base.py" in {_relative(path) for path in files}


def test_every_fromtimestamp_call_specifies_a_timezone() -> None:
    """No naive ``datetime.fromtimestamp`` survives anywhere in the packages.

    Naive conversion renders the epoch in the *host's* local zone. When the
    result reaches ``generate_fragment_id`` the host's ``TZ`` env var ends up
    inside the fragment's identity — the #1329 bug. Passing ``tz=`` makes the
    conversion a pure function of the epoch float instead.
    """
    assert _naive_fromtimestamp_sites() == []


def test_no_code_reads_st_birthtime() -> None:
    """File *creation* time is never consulted, because Linux does not have it.

    ``st_birthtime`` is populated on macOS/BSD and missing on Linux, so a
    derivation that reads it makes a fragment's id a function of the
    ingesting machine's operating system. ``st_mtime`` is the only stat field
    every supported platform agrees on.
    """
    assert _birthtime_sites() == []


@pytest.mark.parametrize(
    ("snippet", "finder", "expected_line"),
    [
        pytest.param(
            "import datetime\nx = datetime.datetime.fromtimestamp(1.0)\n",
            _naive_fromtimestamp_sites,
            2,
            id="naive-fromtimestamp",
        ),
        pytest.param(
            "def f(stat):\n    return stat.st_birthtime\n",
            _birthtime_sites,
            2,
            id="birthtime-attribute",
        ),
        pytest.param(
            'def f(stat):\n    return getattr(stat, "st_birthtime", stat.st_mtime)\n',
            _birthtime_sites,
            2,
            id="birthtime-getattr",
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

    A guard is only worth its runtime if it fails when the thing it guards
    against comes back. Rather than mutating the real tree, each rule is
    pointed at a one-file scratch package containing exactly the pattern it
    forbids, in the precise syntactic form the bug shipped as.

    Args:
        snippet: Source text reintroducing one forbidden pattern.
        finder: The rule's finder function.
        expected_line: Line within *snippet* the finder must report.
        monkeypatch: Used to redirect the scan at the scratch package.
        tmp_path: Pytest temp directory holding the scratch package.
    """
    package = tmp_path / "creek"
    package.mkdir()
    offender = package / "offender.py"
    offender.write_text(snippet, encoding="utf-8")

    monkeypatch.setattr(
        "tests.test_timestamp_derivation_guard._repo_source_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "tests.test_timestamp_derivation_guard.PACKAGE_ROOTS",
        ("creek",),
    )

    assert callable(finder)
    assert finder() == [f"creek/offender.py:{expected_line}"]
