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
from typing import Final

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


_FIXED_ZONE_NAMES: Final[frozenset[str]] = frozenset({"UTC", "LA_TZ"})
"""Bare names denoting a *fixed* zone constant when passed as ``tzinfo=``.

Deliberately excludes a variable: ``creek/ingest/base.py``'s
``_localize_naive_timestamp`` does ``dt.replace(tzinfo=tz)`` where ``tz``
comes from the ingestor's declared ``source_tz``. That is the layer which
*produces* a correctly anchored value from a known source zone, not one
repairing a Creek wall clock, and it is correct as written.
"""

_ANCHOR_EXEMPTIONS: Final[dict[str, str]] = {
    "creek/time.py": (
        "the single sanctioned anchor: ensure_aware attaches LA_TZ, and #1115 "
        "consolidated every Creek wall-clock repair onto it"
    ),
    "creek/cli.py": (
        "_parse_since_arg anchors an operator-typed --since argument, where "
        "UTC is a defensible default rather than a Creek wall clock"
    ),
    "creek/pipeline.py": (
        "_as_aware compares ledger / --since cursors; deferred with "
        "creek/cli.py and pinned by test_ingest_incremental.py::"
        "TestUnitIsChanged::test_since_naive_timestamp_is_utc_not_la"
    ),
    "creek/ingest/gdrive.py": (
        "Google Drive modifiedTime and google-auth token expiry are RFC 3339 "
        "UTC by a third party's contract; a trailing Z is rewritten to +00:00 "
        "before parsing, so the anchor only catches a malformed external value"
    ),
    "creek/link/embeddings.py": (
        "computed_at is read back from a parquet cache this module itself "
        "writes with datetime.now(tz=UTC); it is never a Creek wall clock"
    ),
    "creek/purge/engine.py": (
        "fromisoformat(value).replace(tzinfo=UTC).date() takes .date() "
        "immediately, so the attached zone is provably discarded before any "
        "comparison can observe it"
    ),
}
"""Modules allowed to attach a fixed zone inline, each with the reason why.

Subset semantics (``found - exempt``) rather than equality, so a listed
module that legitimately grows or loses an anchor does not fail the build for
an unrelated reason — which is what lets this rule survive alongside
uncommitted work in ``creek/cli.py``. The trade-off is that an exemption
whose anchor has since been deleted lingers silently, so treat this dict as
documentation to re-read when a listed module changes, not as a live census.
"""


def _fixed_zone_anchor_sites() -> list[str]:
    """Find every ``X.replace(tzinfo=<fixed zone>)`` call in the packages.

    This is the rule an equivalent ``git grep 'replace(tzinfo='`` cannot
    stand in for. That grep returns ten hits and misses
    ``creek/clean/hygiene.py``'s review-queue anchor entirely, because the
    call was wrapped across three lines — and that site was the
    operator-visible defect #1115 was filed about. Matching the parsed call
    rather than a line of text is what makes the sweep honest.

    Returns:
        ``"<file>:<line>"`` for each call attaching a fixed zone constant,
        sorted.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or _call_name(node) != "replace":
                continue
            for keyword in node.keywords:
                if keyword.arg != "tzinfo":
                    continue
                value = keyword.value
                names_a_fixed_zone = (
                    isinstance(value, ast.Name) and value.id in _FIXED_ZONE_NAMES
                ) or (isinstance(value, ast.Attribute) and value.attr in {"utc", "UTC"})
                if names_a_fixed_zone:
                    offenders.append(f"{_relative(path)}:{node.lineno}")
    return sorted(offenders)


def _ensure_aware_definitions() -> list[str]:
    """Find every function *defining* an ``ensure_aware`` in the packages.

    Matched on the exact name after stripping leading underscores, so a
    private clone counts — and deliberately no wider than that. A
    name-shaped rule that also matched ``_as_aware`` would redden
    ``creek/pipeline.py``'s deliberately-kept helper, and this module's own
    docstring records that a guard failing the build on correct code teaches
    people to disable guards.

    Returns:
        ``"<file>"`` for each module defining such a function, sorted.
    """
    definitions: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name.lstrip("_") == "ensure_aware"
            ):
                definitions.append(_relative(path))
    return sorted(definitions)


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


def test_no_unexempted_module_attaches_a_fixed_zone_inline() -> None:
    """A Creek wall clock is anchored by one helper, not by hand (#1115).

    ``creek.time.ensure_aware`` attaches America/Los_Angeles, because the
    ontology (§8.3) makes an offsetless Creek timestamp an LA wall clock
    that lost its offset in transit. Two modules had each hand-rolled the
    same repair against **UTC** instead, so the same value aged 7-8 h
    differently depending on which code path read it — and a review queue
    written at LA midnight came back as UTC midnight.

    Every survivor is listed in :data:`_ANCHOR_EXEMPTIONS` with the reason
    it is genuinely not a Creek wall clock. The reason is interpolated into
    the failure message so it is load-bearing rather than decorative: a new
    entry has to state its evidence to be added at all.

    Scope, stated precisely rather than optimistically: this closes one
    *spelling* of the class — ``X.replace(tzinfo=<constant>)``. A literal
    ``datetime(2024, 1, 1, tzinfo=UTC)`` or an ``.astimezone(...)`` is
    untouched by it, and the first is legitimate and common.
    """
    unexempted = [
        site
        for site in _fixed_zone_anchor_sites()
        if site.rsplit(":", 1)[0] not in _ANCHOR_EXEMPTIONS
    ]
    assert unexempted == [], (
        "these modules attach a fixed timezone inline instead of routing "
        f"through creek.time.ensure_aware: {unexempted}. Exempt modules and "
        "their evidence: "
        + "; ".join(f"{path} — {why}" for path, why in _ANCHOR_EXEMPTIONS.items())
    )


def test_ensure_aware_is_defined_exactly_once() -> None:
    """One anchor helper exists, and it lives in ``creek/time.py`` (#1115).

    This is issue #1115's headline acceptance criterion made executable
    rather than aspirational. ``creek/clean/validator.py`` carried a second
    ``_ensure_aware`` that anchored to UTC while this one anchors to LA;
    the two agreed on aware inputs and disagreed by the LA offset on every
    naive one, which is the hardest kind of divergence to notice.

    A second implementation is banned outright rather than merely
    discouraged: a docstring is not sufficient protection, as this module's
    own opening paragraph records, and *this* function is the one every
    future author will copy when they need "the same thing but UTC".
    """
    assert _ensure_aware_definitions() == ["creek/time.py"], (
        "ensure_aware must have exactly one implementation, in creek/time.py; "
        f"found: {_ensure_aware_definitions()}"
    )


@pytest.mark.parametrize(
    ("snippet", "finder", "expected"),
    [
        pytest.param(
            "import datetime\nx = datetime.datetime.fromtimestamp(1.0)\n",
            _naive_fromtimestamp_sites,
            ["creek/offender.py:2"],
            id="naive-fromtimestamp",
        ),
        pytest.param(
            "def f(stat):\n    return stat.st_birthtime\n",
            _birthtime_sites,
            ["creek/offender.py:2"],
            id="birthtime-attribute",
        ),
        pytest.param(
            'def f(stat):\n    return getattr(stat, "st_birthtime", stat.st_mtime)\n',
            _birthtime_sites,
            ["creek/offender.py:2"],
            id="birthtime-getattr",
        ),
        pytest.param(
            "from datetime import UTC\ndef f(dt):\n    return dt.replace(tzinfo=UTC)\n",
            _fixed_zone_anchor_sites,
            ["creek/offender.py:3"],
            id="inline-utc-anchor",
        ),
        pytest.param(
            "from creek.time import LA_TZ\n"
            "def f(dt):\n"
            "    return dt.replace(tzinfo=LA_TZ)\n",
            _fixed_zone_anchor_sites,
            ["creek/offender.py:3"],
            id="inline-la-anchor",
        ),
        pytest.param(
            "def f(dt):\n    return dt.replace(\n        tzinfo=UTC,\n    )\n",
            _fixed_zone_anchor_sites,
            ["creek/offender.py:2"],
            id="line-wrapped-utc-anchor",
        ),
        pytest.param(
            "def f(dt):\n    return dt.replace(tzinfo=source_tz)\n",
            _fixed_zone_anchor_sites,
            [],
            id="variable-zone-is-not-an-offender",
        ),
        pytest.param(
            "def _ensure_aware(dt):\n    return dt\n",
            _ensure_aware_definitions,
            ["creek/offender.py"],
            id="private-ensure-aware-clone",
        ),
        pytest.param(
            "def _as_aware(dt):\n    return dt\n",
            _ensure_aware_definitions,
            [],
            id="as-aware-is-deliberately-not-an-offender",
        ),
    ],
)
def test_each_rule_bites_when_the_pattern_is_reintroduced(
    snippet: str,
    finder: object,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each rule really detects its pattern, rather than being green by luck.

    A guard is only worth its runtime if it fails when the thing it guards
    against comes back. Rather than mutating the real tree, each rule is
    pointed at a one-file scratch package containing exactly the pattern it
    forbids, in the precise syntactic form the bug shipped as.

    Three cases carry extra weight beyond "the rule fires":

    * ``line-wrapped-utc-anchor`` is the shape ``creek/clean/hygiene.py``
      actually shipped, and the shape a ``git grep 'replace(tzinfo='``
      sweep cannot see at all.
    * ``variable-zone-is-not-an-offender`` and
      ``as-aware-is-deliberately-not-an-offender`` assert the *negative*
      half of each rule's boundary — ``creek/ingest/base.py``'s explicit
      ``source_tz`` and ``creek/pipeline.py``'s kept ``_as_aware``
      respectively. Without them a rule could be quietly widened into
      reddening correct code, which this module's opening docstring
      records as the failure that teaches people to disable guards.

    Args:
        snippet: Source text reintroducing (or deliberately not
            reintroducing) one pattern.
        finder: The rule's finder function.
        expected: Exactly what the finder must report for *snippet*.
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
    assert finder() == expected
