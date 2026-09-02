"""Drift gate for the *gate facts* asserted by ``creek-tools/CLAUDE.md``.

Issue #1194 found the file stating gate facts that nobody was checking: mypy
attributed to ``scripts/lint.sh`` (which contains zero references to it), a
``/docs/workflows/`` single source that has never existed on any ref, a
``✅ ALWAYS`` example that exits 2, a ``tests/`` subtree half of which is
imaginary, a marker list naming a marker ``pyproject.toml`` does not register,
and two live ``check-all.sh`` gates attributed to a script whose own header
says it is ``OPTIONAL — not currently invoked from check-all.sh or CI``.

Every assertion here compares the doc against an **artifact** -- ``git
ls-files``, ``pyproject.toml``, ``scripts/check-all.sh``, ``scripts/test.sh``,
``.claude/agents/shared/house-rules.md`` -- never against prose. Doc text is
read *structurally*: Markdown continuation lines are folded into logical units
before matching (:func:`_logical_units`), and the §5.2 tree is parsed by
box-drawing depth (:func:`_tree_entries`), so re-wrapping a paragraph cannot
turn this gate red and cannot hide a semantic change behind a line break.

Boundaries, deliberate:

* Structure -- balanced fences, the single trailing newline, same-file anchor
  resolution and table-of-contents bidirectionality -- is already gated by
  ``tests/test_markdown_integrity.py``; none of it is duplicated here.
* The **script** direction (``test.sh`` ↔ ``pyproject.toml`` markers,
  ``ci.yml`` ↔ the gate list) is already gated by
  ``tests/test_lane_marker_parity.py`` and ``tests/test_ci_quality_gate.py``.
  This module asserts only the **doc** direction.
* A generic cross-file Markdown link checker (``path.md`` and
  ``path.md#fragment``) is **not** built here. That is open issue #1195, whose
  body cites this issue's ``/docs/workflows/`` row as its motivating
  precedent.

``pyproject.toml`` sets ``python_files = ["test_*.py"]``, so the private
helpers below are never collected as tests.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from tests.shell_command_support import (
    CREEK_TOOLS_DIR,
    REPO_ROOT,
    SCRIPTS_DIR,
    command_lines,
    non_comment_lines,
    shell_tokens,
)

CREEK_TOOLS_CLAUDE_MD = CREEK_TOOLS_DIR / "CLAUDE.md"
ROOT_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
HOUSE_RULES = REPO_ROOT / ".claude" / "agents" / "shared" / "house-rules.md"
PYPROJECT = CREEK_TOOLS_DIR / "pyproject.toml"
CHECK_ALL = SCRIPTS_DIR / "check-all.sh"
TEST_SH = SCRIPTS_DIR / "test.sh"

# Anti-vacuity floors. A parser that silently returns an empty -- or badly
# truncated -- collection turns every assertion downstream of it into a pass
# over nothing, the "gate that looks green but tests nothing" failure this
# suite exists to prevent. Each floor is the measured count at the commit this
# module was written against.
_MIN_TREE_ENTRIES = 30
_MIN_TRACKED_SCRIPTS = 20
_MIN_GATE_SCRIPTS = 13

_INLINE_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_UNIT_START = re.compile(r"^\s*(?:[-*+] |\d+\. |#{1,6} |\|)")
_TREE_BRANCH = re.compile(r"[├└]── ")
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MARKER_EXPRESSION = re.compile(r"\bnot \w+ and\b")
_PYTEST_M_ARG = re.compile(r'-m\s+"([^"]+)"')

# check-all.sh is the runner, not one of the gates it runs, so §6.1 may name
# it. Every other *.sh in that section is an "enforced by" attribution and
# must be a script check-all.sh actually invokes.
_GATE_RUNNER = "check-all.sh"


def _read(path: Path) -> str:
    """Return ``path`` decoded as UTF-8.

    The encoding is explicit because both CLAUDE.md files carry ✅, ❌, the
    box-drawing characters │ ├ └ ─ of the §5.2 tree, ≥ and —; a
    locale-dependent default decode would raise on a runner set to ASCII.

    Args:
        path: File to read.

    Returns:
        The file's full text.
    """
    return path.read_text(encoding="utf-8")


def _logical_units(text: str) -> list[tuple[int, str]]:
    """Split Markdown into reflow-insensitive logical units.

    A "unit" is one table row, one list item (with every continuation line
    folded in), one heading, one paragraph, or -- inside a fenced block -- one
    physical line, since indentation there is content. Whitespace inside a
    folded unit collapses to single spaces.

    Folding is what makes this gate read *structure* rather than source
    layout: re-wrapping a bullet across different line breaks yields the same
    unit, so a pure whitespace reflow cannot redden any assertion below, while
    changing a word inside it still can.

    Args:
        text: Full Markdown source.

    Returns:
        ``(line_number, unit_text)`` pairs, 1-based, in file order.
    """
    units: list[tuple[int, str]] = []
    pending: list[str] = []
    state = {"fenced": False, "start": 0}

    def flush() -> None:
        if pending:
            units.append((state["start"], " ".join(" ".join(pending).split())))
            pending.clear()

    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            flush()
            state["fenced"] = not state["fenced"]
            units.append((number, line))
        elif state["fenced"]:
            flush()
            units.append((number, line))
        elif not line.strip():
            flush()
        else:
            if _UNIT_START.match(line) or not pending:
                flush()
                state["start"] = number
            pending.append(line.strip())
    flush()
    return units


def _headings(text: str) -> list[tuple[int, int, str]]:
    """Return every ATX heading outside a fenced block.

    Args:
        text: Full Markdown source.

    Returns:
        ``(zero_based_line_index, level, title)`` triples.
    """
    found: list[tuple[int, int, str]] = []
    fenced = False
    for index, line in enumerate(text.splitlines()):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        match = _ATX_HEADING.match(line)
        if match and not fenced:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def _section_at(text: str, *, heading: str) -> tuple[int, str]:
    """Return the 1-based start line and body of a section.

    The slice runs between *parsed heading indices*, never :meth:`str.find`:
    a substring anchor can match an earlier occurrence of the same words -- in
    a table of contents, say -- and silently return a slice that shadows the
    real section.

    Args:
        text: Full Markdown source.
        heading: Prefix of the target heading's title, e.g. ``"5.2"``.

    Returns:
        ``(first_line_number, section_text)``, heading included, running to
        the next heading of the same or shallower level (or end of file).

    Raises:
        AssertionError: If no heading starts with ``heading``.
    """
    lines = text.splitlines()
    headings = _headings(text)
    for position, (index, level, title) in enumerate(headings):
        if not title.startswith(heading):
            continue
        end = len(lines)
        for later_index, later_level, _ in headings[position + 1 :]:
            if later_level <= level:
                end = later_index
                break
        return index + 1, "\n".join(lines[index:end])
    raise AssertionError(
        f"no heading starting with {heading!r} in the document; the section "
        "was renamed or removed, so this gate is asserting over nothing"
    )


def _section(text: str, *, heading: str) -> str:
    """Return the body of the section whose title starts with ``heading``.

    Args:
        text: Full Markdown source.
        heading: Prefix of the target heading's title.

    Returns:
        The section text; see :func:`_section_at`.
    """
    return _section_at(text, heading=heading)[1]


def _code_spans(unit: str) -> list[str]:
    """Return the inline-code spans of a logical unit, in order.

    Args:
        unit: One logical unit from :func:`_logical_units`.

    Returns:
        The text inside each pair of backticks.
    """
    return _INLINE_CODE.findall(unit)


def _tree_entries() -> list[tuple[str, str]]:
    """Parse the §5.2 component tree into ``(parent_path, name)`` pairs.

    Depth comes from the box-drawing prefix (four columns per level), the name
    runs from ``├── ``/``└── `` to the trailing comment, and a trailing ``/``
    is stripped so a directory and a file compare the same way.

    Returns:
        Every node of the tree; the root's children carry ``""`` as parent.

    Raises:
        AssertionError: If fewer than :data:`_MIN_TREE_ENTRIES` nodes parse,
            which would make every assertion over this tree vacuous.
    """
    section = _section(_read(CREEK_TOOLS_CLAUDE_MD), heading="5.2")
    entries: list[tuple[str, str]] = []
    stack: list[str] = []
    for line in section.splitlines():
        match = _TREE_BRANCH.search(line)
        if not match:
            continue
        depth = match.start() // 4
        name = re.split(r"\s{2,}|\s+#", line[match.end() :].strip())[0].rstrip("/")
        del stack[depth:]
        stack.append(name)
        entries.append(("/".join(stack[:-1]), name))
    if len(entries) < _MIN_TREE_ENTRIES:
        raise AssertionError(
            f"§5.2 tree parsed to only {len(entries)} entries (floor "
            f"{_MIN_TREE_ENTRIES}); the fence or its box-drawing prefixes "
            "changed shape, so this parser is now reading nothing"
        )
    return entries


def _tracked_scripts() -> set[str]:
    """Return the basenames of every **tracked** file in ``creek-tools/scripts/``.

    Deliberately ``git ls-files`` rather than :meth:`Path.iterdir`.
    ``scripts/`` holds two importable modules and sits on ``--cov=scripts``,
    so any pytest run not routed through ``scripts/test.sh`` (which mints a
    per-run ``PYTHONPYCACHEPREFIX``) leaves ``scripts/__pycache__/`` behind,
    and macOS leaves ``.DS_Store``. Either would fail set equality on a
    developer's machine while a fresh-clone CI stayed green.

    ``-C`` is explicit: CI's ``quality`` job runs with
    ``working-directory: creek-tools``, and a bare ``git ls-files`` there
    would enumerate a different subtree.

    Returns:
        Every tracked basename under ``creek-tools/scripts/``.

    Raises:
        RuntimeError: If git cannot be run or exits non-zero.
        AssertionError: If fewer than :data:`_MIN_TRACKED_SCRIPTS` are listed.
    """
    command = [
        "git",
        "-C",
        str(REPO_ROOT),
        "ls-files",
        "-z",
        "--",
        "creek-tools/scripts/",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:  # pragma: no cover - git is present in every lane
        raise RuntimeError(f"Could not run `git ls-files`: {error}") from error
    if completed.returncode != 0:  # pragma: no cover - non-zero needs a broken repo
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"`git ls-files` exited {completed.returncode}: "
            f"{detail or 'no diagnostic on stderr'}"
        )

    names = {
        PurePosixPath(entry).name
        for entry in completed.stdout.decode("utf-8").split("\0")
        if entry
    }
    if len(names) < _MIN_TRACKED_SCRIPTS:
        raise AssertionError(
            f"git ls-files listed only {len(names)} files under "
            f"creek-tools/scripts/ (floor {_MIN_TRACKED_SCRIPTS}); the "
            "listing failed, so the set equality below would be vacuous"
        )
    return names


def _gate_scripts() -> list[str]:
    """Return the scripts ``check-all.sh`` actually runs, in order.

    Each gate is the second argument of a ``run_check`` invocation. Comment
    lines are dropped by :func:`command_lines`, so prose quoting a gate cannot
    add one.

    Returns:
        The script basenames, e.g. ``["lint.sh", "format.sh", ...]``.

    Raises:
        AssertionError: If fewer than :data:`_MIN_GATE_SCRIPTS` are found.
    """
    gates = [
        shell_tokens(line)[2] for line in command_lines(CHECK_ALL, r"^run_check\s")
    ]
    if len(gates) < _MIN_GATE_SCRIPTS:
        raise AssertionError(
            f"only {len(gates)} run_check gates parsed from check-all.sh "
            f"(floor {_MIN_GATE_SCRIPTS}); the parser is reading nothing"
        )
    return gates


def _names(basename: str, text: str) -> bool:
    """Report whether ``text`` names ``basename`` as a whole path component.

    The left-hand lookbehind is load-bearing: without it ``lint.sh`` is
    "found" inside every mention of ``pylint.sh``, and an assertion that one
    gate is documented passes on a different gate's name.

    Args:
        basename: A file basename such as ``pylint.sh``.
        text: Text to search.

    Returns:
        ``True`` if ``basename`` appears as its own component.
    """
    return re.search(rf"(?<![\w.-]){re.escape(basename)}(?![\w-])", text) is not None


def _resolve(token: str) -> Path:
    """Resolve a repo-relative path token against ``creek-tools/``.

    ``str.lstrip("./")`` is forbidden here, and is why this function exists: it
    strips every leading character in the *set* ``{".", "/"}``, so
    ``../.claude/agents/shared/house-rules.md`` becomes
    ``claude/agents/shared/house-rules.md`` and a link that resolves perfectly
    well gets reported as dead.

    A single leading ``/`` reads as "from the subproject root", which is what
    §1.6 tells every agent repo-relative paths mean.

    Args:
        token: A path as written in the document.

    Returns:
        The absolute path the token denotes.
    """
    pure = PurePosixPath(token.lstrip("/") if token.startswith("/") else token)
    resolved = CREEK_TOOLS_DIR
    for part in pure.parts:
        if part == "..":
            resolved = resolved.parent
        elif part != ".":
            resolved = resolved / part
    return resolved


def _path_tokens(unit: str) -> list[str]:
    """Return the path-shaped inline-code tokens of a logical unit.

    A token counts as path-shaped when it contains ``/``. That excludes bare
    tool invocations (``mypy .``, ``pytest``) and every flag, which is why the
    ❌ column's ``ruff format .`` needs no special case.

    Args:
        unit: One logical unit.

    Returns:
        Each path-shaped token found inside the unit's inline-code spans.
    """
    tokens: list[str] = []
    for span in _code_spans(unit):
        tokens.extend(
            word.rstrip(".,;:")
            for word in span.split()
            if "/" in word and not word.startswith("-")
        )
    return tokens


def _table_rows(section: str) -> list[tuple[str, str]]:
    """Return the ``(NEVER, ALWAYS)`` cell pairs of the §1.1 table.

    Args:
        section: The §1.1 section text.

    Returns:
        One pair per data row; header and separator rows are dropped.
    """
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) == 3 and not set(cells[0]) <= set("- :"):
            rows.append((cells[1], cells[2]))
    return rows[1:]


def _tick_units(section: str) -> list[tuple[int, str]]:
    """Return the ``✅`` bullets of a section, folded and numbered.

    The ``❌``/NEVER material is excluded **by construction**: those cells and
    bullets name paths this repository has deliberately never had --
    ``bandit -r src/`` (the root ``CLAUDE.md`` says "flat layout, not src/")
    and ``cd tests/unit && pytest test_vault.py``. Asserting they resolve
    would either redden text that must not change or pressure a later editor
    into "fixing" an intentionally wrong example, destroying the ❌ column's
    whole function.

    Args:
        section: Section text.

    Returns:
        ``(line_number, unit_text)`` for each ✅ bullet.
    """
    return [(number, unit) for number, unit in _logical_units(section) if "✅" in unit]


def _command_only_lines(script: Path) -> list[str]:
    """Return ``script``'s lines with comments **and heredoc bodies** removed.

    Dropping heredocs is what keeps :func:`_invokes` non-vacuous: every gate
    script in this repo prints a usage block naming its own tool, and a guard
    that a tool's mere *name* satisfies is a guard that cannot fail.

    Args:
        script: Shell script to read.

    Returns:
        The lines that are candidate commands.
    """
    kept: list[str] = []
    terminator: str | None = None
    for line in non_comment_lines(script):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        opener = _HEREDOC.search(line)
        if opener:
            terminator = opener.group(1)
            continue
        kept.append(line)
    return kept


def _delegates_to(module: str, *, tool: str) -> bool:
    """Report whether ``scripts/<module>.py`` imports ``tool``.

    One delegation hop, no more: ``lint-vulture.sh`` runs
    ``python -m scripts.lint_vulture``, and the real ``vulture`` entry point
    is that module's ``from vulture import Vulture``. An ``import`` statement
    is structure, not prose, so a docstring naming the tool cannot satisfy it.

    Args:
        module: Dotted module name from a ``python -m`` invocation.
        tool: The executable the §1.1 row names.

    Returns:
        ``True`` when the delegate genuinely imports the tool.
    """
    delegate = SCRIPTS_DIR / f"{module.split('.')[-1]}.py"
    if not delegate.exists():
        return False
    return (
        re.search(
            rf"^\s*(?:from|import)\s+{re.escape(tool)}\b",
            _read(delegate),
            re.MULTILINE,
        )
        is not None
    )


def _invokes(script: Path, *, tool: str) -> bool:
    """Report whether ``script`` really executes ``tool``.

    The tool must appear as a *token* of a command -- not anywhere inside a
    message string, and not in a usage heredoc -- or be reached through a
    single ``python -m`` delegation. Shell grouping punctuation is flattened
    first so ``OUTPUT=$(tryceratops creek/)`` still reads as an invocation.

    Args:
        script: The shell script named in a §1.1 ✅ cell.
        tool: The executable named in the matching ❌ cell.

    Returns:
        ``True`` when the tool is reachable from a command the script runs.
    """
    if not script.exists():
        return False
    for line in _command_only_lines(script):
        tokens = shell_tokens(re.sub(r"[$`(){};|&<>]", " ", line))
        if tool in tokens:
            return True
        for index, token in enumerate(tokens[:-1]):
            if token == "-m" and _delegates_to(tokens[index + 1], tool=tool):
                return True
    return False


def _forbidden_bypasses() -> set[str]:
    """Return the bypass tokens the ratified anti-bypass record forbids.

    Parsed from the blockquote under ``## Anti-bypass`` in
    ``.claude/agents/shared/house-rules.md`` rather than hard-coded, so
    tightening that record tightens this gate with it.

    Returns:
        The inline-code tokens of the blockquote's prohibition sentence.

    Raises:
        AssertionError: If the blockquote names nothing.
    """
    section = _section(_read(HOUSE_RULES), heading="Anti-bypass")
    quote = " ".join(
        line.lstrip("> ").strip()
        for line in section.splitlines()
        if line.startswith(">")
    )
    tokens = set(_code_spans(quote.split("Fix the root cause", maxsplit=1)[0]))
    if not tokens:
        raise AssertionError(
            "no bypass tokens parsed from house-rules.md's anti-bypass "
            "blockquote; this gate would sanction everything"
        )
    return tokens


def _sanctions_a_bypass(span: str, *, forbidden: set[str]) -> str | None:
    """Return why ``span`` sanctions a forbidden bypass, or ``None``.

    ``# noqa`` and ``# type: ignore`` are the two carve-outs house-rules.md
    itself grants, but only in their ratified forms: a *rule-qualified*
    ``# noqa: RULE`` tied to a real issue. A bare ``# noqa`` suppresses every
    rule on the line, so granting it is a net loosening of the record.

    Args:
        span: One inline-code span or fenced code line.
        forbidden: Tokens from :func:`_forbidden_bypasses`.

    Returns:
        A human-readable reason, or ``None`` when the span is conformant.
    """
    if "# noqa" in span:
        if not re.search(r"#\s*noqa:\s*\S", span):
            return "grants a bare `# noqa`, which suppresses every rule"
        return None if "Issue #" in span else "grants `# noqa: RULE` with no issue"
    if "# type: ignore" in span:
        return None if "Issue #" in span else "grants `# type: ignore` with no issue"
    for token in sorted(forbidden):
        if token in span:
            return f"sanctions {token!r}, which house-rules.md forbids outright"
    return None


def _declared_markers() -> set[str]:
    """Return the marker names ``pyproject.toml`` registers.

    Returns:
        The names before each marker entry's first colon.

    Raises:
        AssertionError: If the table is empty, which would make the marker
            comparisons vacuous.
    """
    entries = tomllib.loads(_read(PYPROJECT))["tool"]["pytest"]["ini_options"][
        "markers"
    ]
    declared = {entry.split(":", 1)[0].strip() for entry in entries}
    if not declared:
        raise AssertionError("pyproject.toml registers no markers to compare against")
    return declared


def test_component_tree_lists_every_tracked_script() -> None:
    """§5.2's ``scripts/`` block must enumerate exactly the tracked files.

    The block is the restatement that drifted: it listed fifteen entries while
    ``scripts/`` grew to twenty-five, and four of the ten it omitted are live
    ``check-all.sh`` gates. Binding the enumeration to ``git ls-files`` is
    what stops the next omission from being invisible.
    """
    listed = {name for parent, name in _tree_entries() if parent == "scripts"}
    tracked = _tracked_scripts()
    assert listed == tracked, (
        "creek-tools/CLAUDE.md §5.2 disagrees with creek-tools/scripts/ -- "
        f"tracked but missing from the tree: {sorted(tracked - listed)}; "
        f"listed but not tracked: {sorted(listed - tracked)}"
    )


def _dead_paths(tokens: list[str]) -> list[str]:
    """Return the tokens that do not resolve to anything on disk.

    Args:
        tokens: Path-shaped tokens from :func:`_path_tokens`.

    Returns:
        Those that no file or directory answers to.
    """
    return [token for token in tokens if not _resolve(token).exists()]


def _dead_paths_in_tick_units(text: str, *, label: str) -> list[str]:
    """Return dead paths from a section's ✅ bullets, with absolute line numbers.

    Args:
        text: Full Markdown source of ``creek-tools/CLAUDE.md``.
        label: Section heading prefix, e.g. ``"1.6"``.

    Returns:
        One ``"<path> (§<label> L<n>)"`` string per unresolved path.
    """
    start, section = _section_at(text, heading=label)
    return [
        f"{token} (§{label} L{start + number - 1})"
        for number, unit in _tick_units(section)
        for token in _dead_paths(_path_tokens(unit))
    ]


def _tree_full_paths() -> list[str]:
    """Return every §5.2 tree node as a path relative to ``creek-tools/``.

    Returns:
        The joined ``parent/name`` path of each node.
    """
    return [f"{parent}/{name}" if parent else name for parent, name in _tree_entries()]


def test_every_path_named_in_claude_md_resolves() -> None:
    """Every path in a ✅/descriptive position of §1.1, §1.2, §1.6, §5.2 exists.

    Six of the eight errors #1194 catalogued were dead paths. ``❌``/NEVER
    material is excluded by construction -- see :func:`_tick_units`.
    """
    text = _read(CREEK_TOOLS_CLAUDE_MD)
    dead = [
        f"{token} (§1.1)"
        for _, always in _table_rows(_section(text, heading="1.1"))
        for token in _dead_paths(_path_tokens(always))
    ]
    for label in ("1.2", "1.6"):
        dead += _dead_paths_in_tick_units(text, label=label)
    dead += [
        f"{full} (§5.2 tree)"
        for full in _tree_full_paths()
        if not _resolve(full).exists()
    ]

    assert not dead, f"paths named in creek-tools/CLAUDE.md that do not exist: {dead}"


def test_section_5_2_retains_the_directories_that_do_exist() -> None:
    """``tests/e2e/`` and ``tests/fixtures/`` stay in the tree.

    Issue #1194's §5.2 row claims ``tests/unit/``, ``tests/integration/``,
    ``tests/e2e/`` and ``tests/fixtures/conftest.py`` all fail to exist. Two of
    the four **do** exist -- at this commit and at the issue's own provenance
    commit ``b99f375`` -- so deleting them on the issue's say-so would be a
    regression. This test is the standing rebuttal.
    """
    entries = set(_tree_entries())
    for name in ("e2e", "fixtures"):
        assert (CREEK_TOOLS_DIR / "tests" / name).is_dir(), (
            f"tests/{name}/ vanished from the repository; if it was genuinely "
            "retired, retire this assertion in the same change"
        )
        assert ("tests", name) in entries, (
            f"§5.2 no longer lists tests/{name}/, but the directory exists -- "
            "issue #1194's claim that it does not was false when written"
        )


def test_marker_list_matches_pyproject() -> None:
    """§6.1's marker list equals the markers ``pyproject.toml`` registers.

    The doc named ``unit`` -- not a registered marker at all, so
    ``strict_markers`` would make it a collection error -- and omitted ``live``
    and ``slow``. The issue's own Reality column got this wrong too: it names
    three registered markers, omitting ``live``.
    """
    declared = _declared_markers()
    units = _logical_units(_section(_read(CREEK_TOOLS_CLAUDE_MD), heading="6.1"))
    heads = [unit.split("—")[0] for _, unit in units if "**Test markers**" in unit]
    assert heads, "§6.1 no longer carries a `- **Test markers**:` bullet"
    documented = set(_code_spans(heads[0]))

    assert documented == declared, (
        f"§6.1 names markers {sorted(documented)}; "
        f"[tool.pytest.ini_options].markers declares {sorted(declared)} -- "
        f"documented but unregistered: {sorted(documented - declared)}; "
        f"registered but undocumented: {sorted(declared - documented)}"
    )


def test_every_marker_expression_in_section_6_1_is_one_test_sh_runs() -> None:
    """Every marker expression §6.1 quotes must be one ``test.sh`` really runs.

    The doc claimed the default lane is ``not integration and not e2e``.
    ``test.sh`` also excludes ``slow`` and ``live``, and its own comment
    explains why ``live`` is there: those tests skip themselves when a key is
    absent, "so omitting it here would look fine in CI and quietly bill a real
    API on a developer's machine". A doc that drops it invites exactly that.

    Matching is **exact**, never substring: the stale expression is a prefix of
    the real one, so a substring test would be green on the very defect it
    exists to catch.
    """
    documented = {
        " ".join(span.split())
        for _, unit in _logical_units(
            _section(_read(CREEK_TOOLS_CLAUDE_MD), heading="6.1")
        )
        for span in _code_spans(unit)
        if _MARKER_EXPRESSION.search(span)
    }
    assert documented, (
        "§6.1 quotes no marker expression at all; the default-lane statement "
        "was removed rather than corrected, so this gate reads nothing"
    )

    actual = {
        match.group(1)
        for line in non_comment_lines(TEST_SH)
        for match in _PYTEST_M_ARG.finditer(line)
    }
    assert actual, 'no `-m "..."` argument parsed from scripts/test.sh'

    orphans = sorted(documented - actual)
    assert not orphans, (
        f"§6.1 quotes marker expressions scripts/test.sh never runs: "
        f"{orphans}; the expressions it does run are {sorted(actual)}"
    )


def test_every_check_all_gate_is_named_in_both_claude_md_files() -> None:
    """Both CLAUDE.md files name every script ``check-all.sh`` runs.

    ``pylint.sh`` and ``lint-interrogate.sh`` were gates no agent could
    discover from either file, while ``lint-extended.sh`` -- which runs in
    neither CI nor ``check-all.sh`` -- was named three times.
    """
    gates = _gate_scripts()
    missing = {
        label: [gate for gate in gates if not _names(gate, _read(path))]
        for label, path in (
            ("creek-tools/CLAUDE.md", CREEK_TOOLS_CLAUDE_MD),
            ("CLAUDE.md", ROOT_CLAUDE_MD),
        )
    }
    assert not any(missing.values()), (
        f"check-all.sh gates never named: {missing} -- check-all.sh:109-121 "
        f"runs all of {gates}"
    )


def test_lint_extended_is_never_presented_as_a_gate() -> None:
    """Any line naming ``lint-extended.sh`` must also call it optional.

    Its own header line 4 reads "Status: OPTIONAL — not currently invoked from
    check-all.sh or CI." The root ``CLAUDE.md`` already says so and is the
    positive control here: the rule is not a blanket ban on the string.
    """
    offenders = [
        f"{label}:{number}"
        for label, path in (
            ("creek-tools/CLAUDE.md", CREEK_TOOLS_CLAUDE_MD),
            ("CLAUDE.md", ROOT_CLAUDE_MD),
        )
        for number, unit in _logical_units(_read(path))
        if _names("lint-extended.sh", unit) and "optional" not in unit.lower()
    ]
    assert not offenders, (
        "lines naming lint-extended.sh without saying it is optional: "
        f"{offenders} (its own header line 4: 'Status: OPTIONAL — not "
        "currently invoked from check-all.sh or CI.')"
    )


def test_every_shell_script_named_in_section_6_1_is_a_check_all_gate() -> None:
    """§6.1 may attribute a gate only to a script ``check-all.sh`` runs.

    "Does the named script invoke the tool?" does **not** catch this defect:
    ``lint-extended.sh`` genuinely invokes both interrogate and pylint, so
    such a rule stays green on the two rows #1194 filed. Membership in the
    ``check-all.sh`` gate list is the property that actually binds.
    """
    gates = set(_gate_scripts()) | {_GATE_RUNNER}
    start, section = _section_at(_read(CREEK_TOOLS_CLAUDE_MD), heading="6.1")
    offenders = sorted(
        {
            f"{name} (§6.1 L{start + number - 1})"
            for number, unit in _logical_units(section)
            for span in _code_spans(unit)
            for word in span.split()
            for name in [PurePosixPath(word).name]
            if name.endswith(".sh") and name not in gates
        }
    )
    assert not offenders, (
        "§6.1 attributes a quality gate to a script check-all.sh does not "
        f"run: {offenders}; the gates are {sorted(gates)}"
    )


def test_section_1_1_scripts_run_the_tools_they_replace() -> None:
    """Each §1.1 row's ✅ script must really invoke the ❌ row's tool.

    ``scripts/lint.sh`` was credited with "(includes mypy)" while containing
    zero references to it -- the gate is ``scripts/typecheck.sh``. Matching is
    comment-blind and heredoc-blind: a usage block that merely *names* its
    tool must not satisfy the assertion that the script *runs* it.
    """
    broken: list[str] = []
    for never, always in _table_rows(
        _section(_read(CREEK_TOOLS_CLAUDE_MD), heading="1.1")
    ):
        tools = _code_spans(never)
        scripts = [PurePosixPath(span).name for span in _code_spans(always)]
        if not tools or not scripts or not scripts[0].endswith(".sh"):
            continue
        tool = tools[0].split()[0]
        if not _invokes(SCRIPTS_DIR / scripts[0], tool=tool):
            broken.append(f"`{tool}` -> {scripts[0]}, which never invokes it")
    assert not broken, f"§1.1 rows whose ✅ script does not run the ❌ tool: {broken}"


def test_no_example_invokes_a_project_script_with_a_positional_argument() -> None:
    """§1.6's ✅ examples must be invocations that actually succeed.

    The example held up as correct was
    ``./scripts/test.sh tests/unit/test_vault.py``. ``test.sh``'s option loop
    ends ``*) echo "Error: Unknown option: $1" >&2; exit 2``, so a positional
    path never reaches pytest: the ✅ example exits 2.
    """
    assert command_lines(TEST_SH, r"Unknown option"), (
        "scripts/test.sh no longer rejects unknown options; this gate's "
        "premise is gone and must be re-derived rather than left green"
    )

    offenders: list[str] = []
    start, section = _section_at(_read(CREEK_TOOLS_CLAUDE_MD), heading="1.6")
    for number, unit in _tick_units(section):
        for span in _code_spans(unit):
            words = span.split()
            if words and words[0].startswith("./scripts/"):
                offenders += [
                    f"`{span}` (§1.6 L{start + number - 1})"
                    for word in words[1:]
                    if not word.startswith("-")
                ]
    assert not offenders, (
        "§1.6 ✅ examples passing a positional argument to a project script: "
        f"{offenders}; test.sh's option loop ends `*) ... exit 2`"
    )


def test_claude_md_sanctions_no_bypass_house_rules_forbids() -> None:
    """§1.3 may not sanction a bypass the ratified record forbids.

    ``@pytest.mark.skip(reason="Issue #N")`` was a ✅ Required Approach while
    house-rules.md forbids ``@pytest.mark.skip`` outright, and the escape
    hatch was written ``# noqa`` where the ratified form is ``# noqa: RULE``.
    §6.2's ``✅ ALLOWED`` examples already use the ratified forms and are the
    positive control: they must stay green untouched.
    """
    forbidden = _forbidden_bypasses()
    text = _read(CREEK_TOOLS_CLAUDE_MD)
    offenders: list[str] = []

    start_1_3, section_1_3 = _section_at(text, heading="1.3")
    for number, unit in _tick_units(section_1_3):
        offenders += [
            f"§1.3 L{start_1_3 + number - 1}: {reason}"
            for span in _code_spans(unit)
            if (reason := _sanctions_a_bypass(span, forbidden=forbidden))
        ]
    start_6_2, section_6_2 = _section_at(text, heading="6.2")
    for number, unit in _logical_units(section_6_2):
        reason = (
            _sanctions_a_bypass(unit, forbidden=forbidden)
            if "Issue #" in unit
            else None
        )
        if reason:
            offenders.append(f"§6.2 L{start_6_2 + number - 1}: {reason}")

    assert not offenders, (
        "creek-tools/CLAUDE.md sanctions bypasses house-rules.md forbids: "
        f"{offenders}; the only ratified escape hatches are "
        "`# noqa: RULE  # Issue #N: <reason>` and `# type: ignore  # Issue #N`"
    )


def test_claude_md_does_not_cite_its_own_drift_issue() -> None:
    """The shipped file must not point at the issue that repaired it.

    §7 said its drift "is tracked in issue #1194". Once #1194 closes that is a
    dangling pointer telling every agent the file is knowingly wrong.
    """
    citing = [
        f"L{number}"
        for number, unit in _logical_units(_read(CREEK_TOOLS_CLAUDE_MD))
        if re.search(r"#1194\b", unit)
    ]
    assert not citing, (
        f"creek-tools/CLAUDE.md still cites issue #1194 at {citing}; the drift "
        "is now gated by tests/test_claude_md_gate_facts_drift.py"
    )


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("lint-extended.sh", id="root-optional-control"),
        pytest.param("markers", id="root-marker-control"),
    ],
)
def test_positive_controls_in_the_root_claude_md_are_untouched(subject: str) -> None:
    """The root file already gets both rules right, and must not be edited.

    ``CLAUDE.md``'s Key Commands line already reads "Optional:" beside
    ``lint-extended.sh``, and its creek-tools section already names all four
    registered markers and points at ``pyproject.toml``. They are the evidence
    that the two rules above are not blanket string bans, so they are asserted
    green here **before** this PR touches anything.
    """
    units = [
        unit for _, unit in _logical_units(_read(ROOT_CLAUDE_MD)) if subject in unit
    ]
    assert units, f"the root CLAUDE.md no longer mentions {subject!r}"

    if subject == "lint-extended.sh":
        assert all("optional" in unit.lower() for unit in units), (
            "the root CLAUDE.md stopped calling lint-extended.sh optional; it "
            "is the positive control for that rule"
        )
    else:
        declared = _declared_markers()
        documented = set(_code_spans(" ".join(units)))
        assert declared <= documented, (
            f"the root CLAUDE.md's marker list no longer names all of "
            f"{sorted(declared)}; missing: {sorted(declared - documented)}"
        )
