"""Mechanical integrity gate for every tracked Markdown file (issue #1190).

``creek-tools/CLAUDE.md`` shipped with a code fence that is opened and never
closed, a table of contents listing seven sections that do not exist, and
three internal links pointing at absent anchors. None of it was caught by
anything: CI does not run ``pre-commit``, and the obvious hand-rolled check
(``grep -c '^```'``) returns ``6`` on that file -- an even number, so the
fences look balanced. An indent-tolerant count returns ``11``. The bug is
invisible to the naive checker precisely because the broken opener is
indented three spaces inside an ordered-list item.

This module therefore does two things. Sections A/C/D prove the checker is
*capable of failing* by driving it against synthetic documents whose verdict
is known; section B applies it to the repository. Both halves matter: a
repo-wide sweep that silently parses nothing would be a gate that cannot
fail.

Parsing lives in ``tests/markdown_integrity_support.py`` (a non-``test_*``
module, so ``python_files = ["test_*.py"]`` never collects it). Its contract,
which this module is written against:

``find_unclosed_fence(text: str) -> int | None``
    Return the 1-based line number of the fenced-code-block opener that is
    still open at end of input, or ``None`` when every fence is closed.
    CommonMark rules, and only these:

    * An *opener* is a line indented 0-3 spaces followed by a run of three
      or more backticks or three or more tildes, optionally followed by an
      info string. Four or more spaces of indentation makes an indented code
      block, not a fence.
    * A *closer* is a line indented 0-3 spaces consisting solely of a run of
      the **same** fence character, **at least as long** as the opener's
      run, followed only by whitespace. An info string disqualifies a line
      from closing anything.
    * Fences do not nest. Inside an open fence every other line is content,
      including a line that would otherwise open a fence -- which is why a
      ``markdown`` block containing a ``json`` block ends early at the
      inner block's bare closer, and why the outer block's intended closer
      then opens a fresh, unclosed fence.
    * The CommonMark rule that a backtick info string may not itself
      contain a backtick is out of scope; no file in this repository
      exercises it.

``heading_slugs(text, *, levels=None) -> list[str]``
    Return GitHub anchor slugs for the ATX headings of ``text``, in document
    order. ``levels`` is a ``Collection[int] | None``; when given, the result
    is filtered to headings of those levels *after* de-duplication, so
    suffixes stay consistent with the whole document.

    * ATX only (``^ {0,3}#{1,6} ``). Setext headings are deliberately not
      recognised: there are zero ``^=+$`` lines across the tracked Markdown,
      so honouring ``---`` as a setext underline would only manufacture a
      bogus anchor out of every horizontal rule in the repository.
    * Headings inside a fenced code block are skipped, using the same fence
      scanner as :func:`find_unclosed_fence`.
    * An optional closing run of ``#`` is stripped, then the text is
      slugged: lowercase, drop every character that is not alphanumeric,
      space, hyphen or underscore, then map spaces to hyphens.
    * Repeated slugs get ``-1``, ``-2``, ... in order of appearance; the
      first occurrence keeps the bare slug.

``same_file_anchor_links(text) -> list[tuple[int, str]]``
    Return ``(1-based line number, anchor without its "#")`` for every
    inline ``[label](#anchor)`` link that is not inside a fenced code block,
    in document order, left to right within a line. Only same-file targets
    qualify -- a target must start with ``#``, so ``path.md#frag`` and
    ``https://host/#frag`` are out of scope.

``tracked_markdown_files(repo_root: Path) -> list[Path]``
    Return the absolute path of every ``*.md`` file tracked by git, sorted.
    Must shell out as ``git -C <repo_root> ls-files -z -- '*.md'``: the CI
    ``quality`` job runs with ``working-directory: creek-tools``, so a bare
    ``git ls-files`` would quietly scan one subtree and the gate would miss
    most of the repository. ``-z`` because paths are otherwise quoted. It
    must **not** probe whether ``.git`` is a directory -- in a linked git
    worktree, which is where this branch is being developed, ``.git`` is a
    file, and that probe false-negatives. A missing ``git`` binary or a
    non-zero exit raises ``RuntimeError`` naming the cause; it must never
    return an empty list, and this module must never skip.

``load_fence_exceptions(path: Path) -> dict[str, str]``
    Parse ``tests/markdown-integrity-exceptions.txt``, whose format mirrors
    ``scripts/coverage-waivers.txt``: blank lines and lines whose stripped
    form starts with ``#`` are ignored; every other line is partitioned on
    its first space into a repo-relative POSIX path and a reason. Returns
    path -> reason, insertion-ordered. A non-comment line carrying no reason
    raises ``ValueError`` naming the offending path, as does a repeated
    path. The listed files are exempt from the fence check **only** -- the
    trailing-newline and anchor checks still apply to them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.markdown_integrity_support import (
    find_unclosed_fence,
    heading_slugs,
    load_fence_exceptions,
    same_file_anchor_links,
    tracked_markdown_files,
)
from tests.shell_command_support import CREEK_TOOLS_DIR, REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

EXCEPTIONS_FILE = CREEK_TOOLS_DIR / "tests" / "markdown-integrity-exceptions.txt"
CREEK_TOOLS_CLAUDE_MD = CREEK_TOOLS_DIR / "CLAUDE.md"

# The tracked-Markdown count was 251 when this gate was written. The floor is
# an anti-vacuity guard, not a census: it fails loudly if discovery ever
# collapses to a subtree (or to nothing) instead of passing on an empty sweep.
MINIMUM_TRACKED_MARKDOWN_FILES = 200

# The only files allowed in the fence-exception list. Every one of them is
# broken by the same CommonMark nesting trap and is tracked by issue #1193.
# Growing this set is a deliberate edit to a test, not a config tweak.
KNOWN_FENCE_EXCEPTIONS = frozenset(
    {
        ".claude/skills/architectural-decisions/references/examples.md",
        ".claude/skills/spec-decomposition/references/templates.md",
        "creek-tools/.claude/skills/architectural-decisions.md",
    }
)

_ISSUE_REFERENCE = re.compile(r"#\d+")
_H2_HEADING = re.compile(r"^## +\S")


def _doc(*lines: str) -> str:
    """Join ``lines`` into a Markdown document with a trailing newline.

    Written out line by line rather than as a triple-quoted block because
    leading whitespace is load-bearing in most of these cases and
    :func:`textwrap.dedent` would eat it.

    Args:
        *lines: The lines of the document, without line terminators.

    Returns:
        The document text, newline-separated and newline-terminated.
    """
    return "\n".join(lines) + "\n"


def _tracked_markdown() -> list[Path]:
    """Return every tracked Markdown file, refusing a vacuous result.

    Returns:
        Absolute paths of the tracked ``*.md`` files.

    Raises:
        AssertionError: If discovery found implausibly few files, which
            would let every repo-wide assertion below pass by scanning
            nothing.
    """
    files = tracked_markdown_files(REPO_ROOT)
    assert len(files) >= MINIMUM_TRACKED_MARKDOWN_FILES, (
        f"Found only {len(files)} tracked Markdown file(s) under {REPO_ROOT}; "
        f"expected at least {MINIMUM_TRACKED_MARKDOWN_FILES}. Discovery has "
        "collapsed to a subtree (a bare `git ls-files` run from creek-tools/ "
        "does exactly this) and the gate is scanning almost nothing."
    )
    return files


def _relative(path: Path) -> str:
    """Return ``path`` as a repo-relative POSIX string.

    Args:
        path: An absolute path inside the repository.

    Returns:
        The path relative to :data:`REPO_ROOT`, POSIX-separated, which is
        the form the exception file and every failure message use.
    """
    return path.relative_to(REPO_ROOT).as_posix()


def _table_of_contents_anchors(text: str) -> list[str]:
    """Return the anchors of the table-of-contents list at the top of ``text``.

    The table of contents is the run of same-file anchor links that precedes
    the document's first ``## `` heading. That region contains no code
    fences in the file this is applied to, so no fence handling beyond what
    :func:`same_file_anchor_links` already does is needed.

    Args:
        text: The full Markdown document.

    Returns:
        The anchors (without their leading ``#``) listed before the first
        level-2 heading, in document order.
    """
    lines = text.splitlines()
    first_section = len(lines)
    for index, line in enumerate(lines):
        if _H2_HEADING.match(line):
            first_section = index
            break
    return [
        anchor
        for line_number, anchor in same_file_anchor_links(text)
        if line_number <= first_section
    ]


# --------------------------------------------------------------------------
# A. Parser self-tests -- prove the checker can fail
# --------------------------------------------------------------------------

_FENCE_CASES = [
    pytest.param(
        _doc("1. Item", "", "   ```python", "   x = 1"),
        3,
        id="opener-indented-three-spaces-is-a-fence",
    ),
    pytest.param(
        _doc("1. Item", "", "   ```python", "   x = 1", "   ```"),
        None,
        id="indented-fence-closed-by-indented-closer",
    ),
    pytest.param(
        _doc("Prose.", "", "    ```python", "    x = 1"),
        None,
        id="opener-indented-four-spaces-is-an-indented-code-block",
    ),
    pytest.param(
        _doc("~~~", "code", "~~~"),
        None,
        id="tilde-fence-closed-by-tildes",
    ),
    pytest.param(
        _doc("~~~", "code", "```", "more"),
        1,
        id="tilde-fence-not-closed-by-backticks",
    ),
    pytest.param(
        _doc("```", "code", "~~~", "more"),
        1,
        id="backtick-fence-not-closed-by-tildes",
    ),
    pytest.param(
        _doc("````", "code", "```", "more"),
        1,
        id="four-backtick-opener-not-closed-by-three",
    ),
    pytest.param(
        _doc("````", "code", "````"),
        None,
        id="four-backtick-opener-closed-by-four",
    ),
    pytest.param(
        _doc("```", "code", "````"),
        None,
        id="three-backtick-opener-closed-by-a-longer-run",
    ),
    pytest.param(
        _doc("```", "code", "```python", "more"),
        1,
        id="closer-may-not-carry-an-info-string",
    ),
    pytest.param(
        _doc("```", "code", "```   "),
        None,
        id="closer-may-carry-trailing-whitespace",
    ),
    pytest.param(
        _doc(
            "```markdown",
            "# Example",
            "",
            "```json",
            '{"a": 1}',
            "```",
            "",
            "More prose.",
            "```",
        ),
        9,
        id="inner-fence-terminates-the-outer-block-early",
    ),
    pytest.param(
        _doc("# Title", "", "```bash", "ls", "```", "", "Done."),
        None,
        id="balanced-backtick-document",
    ),
    pytest.param(
        _doc("# Title", "", "~~~text", "hi", "~~~", "", "Done."),
        None,
        id="balanced-tilde-document",
    ),
    pytest.param(
        _doc("# Title", "", "No fences here."),
        None,
        id="document-with-no-fences",
    ),
    pytest.param("", None, id="empty-document"),
]


@pytest.mark.parametrize(("document", "expected"), _FENCE_CASES)
def test_find_unclosed_fence_matches_commonmark(
    document: str, expected: int | None
) -> None:
    """The fence scanner agrees with CommonMark on every known-verdict case.

    Two cases carry the whole issue. ``opener-indented-three-spaces-is-a-
    fence`` is the shape that broke ``creek-tools/CLAUDE.md``: the opener
    sits inside an ordered-list item at three spaces, so a checker anchored
    on ``^`` never sees it and reports a balanced file.
    ``inner-fence-terminates-the-outer-block-early`` is the shape that broke
    three skill files (issue #1193): fences do not nest, so the inner block's
    bare closer ends the outer block and the outer block's intended closer
    opens a fence that is never closed.

    Args:
        document: A synthetic Markdown document.
        expected: 1-based line number of the unclosed opener, or ``None``
            when the document is balanced.
    """
    assert find_unclosed_fence(document) == expected


_SLUG_CASES = [
    pytest.param(
        _doc("## 9. Tool Usage & Code Standards"),
        ["9-tool-usage--code-standards"],
        id="ampersand-is-dropped-leaving-a-double-hyphen",
    ),
    pytest.param(
        _doc("### 9.1 Tool Invocation Patterns"),
        ["91-tool-invocation-patterns"],
        id="dot-is-dropped-from-a-numbered-subsection",
    ),
    pytest.param(
        _doc("## Generic (`generic`)"),
        ["generic-generic"],
        id="parentheses-and-backticks-are-dropped-not-replaced",
    ),
    pytest.param(
        _doc("## What `--apply` does"),
        ["what---apply-does"],
        id="backticks-vanish-leaving-the-literal-double-hyphen",
    ),
    pytest.param(
        _doc("# Title", "", "## Section", "", "### Subsection"),
        ["title", "section", "subsection"],
        id="every-level-in-document-order",
    ),
    pytest.param(
        _doc("## Setup", "", "## Setup", "", "## Setup"),
        ["setup", "setup-1", "setup-2"],
        id="repeated-headings-get-numeric-suffixes",
    ),
    pytest.param(
        _doc("Some prose", "---", "", "## Real Heading"),
        ["real-heading"],
        id="triple-hyphen-is-a-horizontal-rule-not-a-setext-underline",
    ),
    pytest.param(
        _doc("```bash", "# not a heading", "```", "", "## Real Heading"),
        ["real-heading"],
        id="headings-inside-a-fence-are-ignored",
    ),
    pytest.param(
        _doc("## Foo ##"),
        ["foo"],
        id="closing-hash-run-is-stripped",
    ),
    pytest.param(
        _doc("## a_b-c d"),
        ["a_b-c-d"],
        id="underscore-and-hyphen-survive-spaces-become-hyphens",
    ),
    pytest.param(
        _doc("Prose.", "", "    # Indented"),
        [],
        id="four-space-indent-is-not-a-heading",
    ),
    pytest.param(
        _doc("#NoSpace"),
        [],
        id="hash-without-a-following-space-is-not-a-heading",
    ),
]


@pytest.mark.parametrize(("document", "expected"), _SLUG_CASES)
def test_heading_slugs_match_github_anchors(document: str, expected: list[str]) -> None:
    """Slugging reproduces GitHub's anchors for known headings.

    The first two cases are lifted verbatim from ``creek-tools/CLAUDE.md``'s
    own table of contents, so they pin the two characters that trip people
    up: ``&`` is deleted rather than replaced, leaving the two spaces around
    it to become a *double* hyphen, and ``.`` is deleted outright so
    ``9.1`` slugs to ``91``. The next two are live headings from
    ``docs/ingestion.md`` and ``docs/redaction.md`` that already have links
    pointing at them; both would break if punctuation were replaced with a
    hyphen instead of deleted.

    ``triple-hyphen-is-a-horizontal-rule-not-a-setext-underline`` is the
    negative case that keeps the anchor gate honest. There are zero ``^=+$``
    lines in the tracked Markdown, so setext support could only ever fire on
    ``---`` -- and every horizontal rule in the repository would then mint a
    heading anchor out of whatever prose happened to precede it.

    Args:
        document: A synthetic Markdown document.
        expected: The slugs the document should yield, in order.
    """
    assert heading_slugs(document) == expected


def test_heading_slugs_dedupe_across_levels_then_filter() -> None:
    """``levels`` filters after de-duplication, not before.

    De-duplication is a property of the whole document -- GitHub numbers the
    second ``Alpha`` ``alpha-1`` no matter what level it sits at. Filtering
    first would hand back ``alpha`` for a heading whose real anchor is
    ``alpha-1``, which is exactly the silent mis-resolution the anchor gate
    exists to catch.
    """
    document = _doc("# Title", "", "## Alpha", "", "### Alpha", "", "## Alpha")

    assert heading_slugs(document) == ["title", "alpha", "alpha-1", "alpha-2"]
    assert heading_slugs(document, levels={2}) == ["alpha", "alpha-2"]
    assert heading_slugs(document, levels={3}) == ["alpha-1"]
    assert heading_slugs(document, levels={1, 3}) == ["title", "alpha-1"]


_ANCHOR_CASES = [
    pytest.param(
        _doc("See [X](#x-y) and [Y](#z)."),
        [(1, "x-y"), (1, "z")],
        id="two-links-on-one-line-left-to-right",
    ),
    pytest.param(
        _doc("```", "[X](#x)", "```", "", "[Y](#y)"),
        [(5, "y")],
        id="links-inside-a-fence-are-ignored",
    ),
    pytest.param(
        _doc("```markdown", "- [A](#a)", "```"),
        [],
        id="sample-toc-inside-a-fence-is-ignored",
    ),
    pytest.param(
        _doc("[X](other.md#x)"),
        [],
        id="cross-file-anchor-is-out-of-scope",
    ),
    pytest.param(
        _doc("[X](https://example.com/page#x)"),
        [],
        id="external-anchor-is-out-of-scope",
    ),
    pytest.param(
        _doc("- [1. A Section](#1-a-section)"),
        [(1, "1-a-section")],
        id="table-of-contents-entry",
    ),
    pytest.param(
        _doc("no links here"),
        [],
        id="document-with-no-links",
    ),
]


@pytest.mark.parametrize(("document", "expected"), _ANCHOR_CASES)
def test_same_file_anchor_links_are_located(
    document: str, expected: list[tuple[int, str]]
) -> None:
    """Only live, same-file anchor links are reported, with their line numbers.

    Fenced content is excluded because this repository's skill files are
    largely *examples of Markdown*: several show a sample table of contents
    inside a ``markdown`` block. Resolving those against the enclosing
    document's headings would produce failures nobody can fix.

    Args:
        document: A synthetic Markdown document.
        expected: ``(line number, anchor)`` pairs, in document order.
    """
    assert same_file_anchor_links(document) == expected


# --------------------------------------------------------------------------
# B. Repo-wide assertions over tracked Markdown
# --------------------------------------------------------------------------


def test_tracked_markdown_discovery_covers_the_whole_repository() -> None:
    """Discovery reaches both the repo root and the ``creek-tools`` subtree.

    ``CLAUDE.md`` and ``creek-tools/CLAUDE.md`` are named explicitly because
    they sit on opposite sides of the CI ``working-directory: creek-tools``
    boundary. If the helper ever drops ``-C REPO_ROOT``, the root file
    disappears from the sweep and every gate below narrows silently.
    """
    files = _tracked_markdown()
    relative = {_relative(path) for path in files}

    assert "CLAUDE.md" in relative
    assert "creek-tools/CLAUDE.md" in relative
    assert files == sorted(files)


def test_tracked_markdown_discovery_fails_loudly_without_a_repository(
    tmp_path: Path,
) -> None:
    """Discovery raises instead of returning an empty list when git cannot run.

    A directory that does not exist is used rather than merely a non-repo
    directory, so the outcome does not depend on whether the temp root
    happens to sit inside some other checkout. The point is the failure
    *mode*: a gate whose discovery step can return ``[]`` -- or skip -- is a
    gate that cannot fail.
    """
    with pytest.raises(RuntimeError):
        tracked_markdown_files(tmp_path / "definitely-not-a-repository")


def test_every_tracked_markdown_file_has_balanced_code_fences() -> None:
    """No tracked Markdown file leaves a code fence open at end of file.

    Files named in ``tests/markdown-integrity-exceptions.txt`` are exempt
    from *this* check only; the trailing-newline and anchor checks below
    still cover them.
    """
    exceptions = load_fence_exceptions(EXCEPTIONS_FILE)
    broken: list[str] = []
    for path in _tracked_markdown():
        relative = _relative(path)
        if relative in exceptions:
            continue
        opener = find_unclosed_fence(path.read_text(encoding="utf-8"))
        if opener is not None:
            broken.append(f"{relative}:{opener}")

    assert not broken, (
        "Code fence opened and never closed (path:line of the opener): "
        + ", ".join(broken)
        + ". Close the fence; do not add it to "
        "tests/markdown-integrity-exceptions.txt."
    )


def test_every_tracked_markdown_file_ends_with_exactly_one_newline() -> None:
    """Every non-empty tracked Markdown file ends with a single ``\\n``.

    All 251 tracked files satisfy this today, so this is pure regression
    protection -- deliberately so. ``.github/workflows/ci.yml`` has no
    ``pre-commit run`` step, which makes the ``end-of-file-fixer`` hook a
    local convenience rather than a merge gate. Empty files are exempt
    because an empty file has no last line to terminate.
    """
    offenders: list[str] = []
    for path in _tracked_markdown():
        raw = path.read_bytes()
        if not raw:
            continue
        if not raw.endswith(b"\n"):
            offenders.append(f"{_relative(path)} (no trailing newline)")
        elif raw.endswith(b"\n\n"):
            offenders.append(f"{_relative(path)} (blank line at end of file)")

    assert not offenders, "Bad file ending: " + ", ".join(offenders)


def test_every_same_file_anchor_link_resolves_to_a_heading() -> None:
    """Every ``[...](#anchor)`` link points at a heading in its own file.

    Cross-file ``path.md#fragment`` links are out of scope: resolving them
    is a different check with different failure modes, and
    ``creek clean broken-links`` already owns the vault-side equivalent.
    """
    dangling: list[str] = []
    for path in _tracked_markdown():
        text = path.read_text(encoding="utf-8")
        slugs = set(heading_slugs(text))
        dangling.extend(
            f"{_relative(path)}:{line_number} -> #{anchor}"
            for line_number, anchor in same_file_anchor_links(text)
            if anchor not in slugs
        )

    assert not dangling, (
        "Anchor link with no matching heading in the same file: " + ", ".join(dangling)
    )


# --------------------------------------------------------------------------
# C. Exception-file semantics
# --------------------------------------------------------------------------


def test_fence_exceptions_file_exists_and_is_not_empty() -> None:
    """The exception file exists and parses to at least one entry.

    Without this, deleting the file (or emptying it) would turn the
    "entries must still be broken" and "reasons must cite an issue" tests
    below into no-ops that pass by iterating nothing.
    """
    assert EXCEPTIONS_FILE.is_file(), f"Missing {EXCEPTIONS_FILE}"
    assert load_fence_exceptions(EXCEPTIONS_FILE)


def test_no_fence_exception_names_an_untracked_file() -> None:
    """Every exception entry names a file git still tracks.

    Mirrors the orphan-waiver detector in ``scripts/coverage-per-file.sh``:
    a stale entry is dead weight that quietly exempts nothing, and it hides
    a path-format drift (``creek-tools/...`` vs ``...``) that would exempt
    nothing either.
    """
    exceptions = load_fence_exceptions(EXCEPTIONS_FILE)
    tracked = {_relative(path) for path in _tracked_markdown()}
    orphans = sorted(set(exceptions) - tracked)

    assert not orphans, (
        "tests/markdown-integrity-exceptions.txt lists path(s) that are not "
        "tracked Markdown files: " + ", ".join(orphans) + ". Remove them."
    )


def test_every_fence_exception_still_has_an_unclosed_fence() -> None:
    """No exception entry names a file that now passes the fence check.

    This is what stops the list becoming permanent cover. The moment issue
    #1193 repairs a file, its entry has to go -- and the failure message
    says so, rather than leaving a silent exemption behind.
    """
    exceptions = load_fence_exceptions(EXCEPTIONS_FILE)
    now_passing: list[str] = []
    for entry in exceptions:
        target = REPO_ROOT / entry
        if not target.is_file():
            continue  # the orphan test above owns this failure mode
        if find_unclosed_fence(target.read_text(encoding="utf-8")) is None:
            now_passing.append(entry)

    now_passing.sort()
    assert not now_passing, (
        "These file(s) now have balanced fences and no longer need an "
        "exception: " + ", ".join(now_passing) + ". Delete their lines from "
        "tests/markdown-integrity-exceptions.txt."
    )


def test_every_fence_exception_reason_cites_an_issue() -> None:
    """Every exception reason references an issue number.

    Same rule the coverage waivers carry: an exemption without a tracked
    owner is a permanent one.
    """
    exceptions = load_fence_exceptions(EXCEPTIONS_FILE)
    unsourced = sorted(
        entry
        for entry, reason in exceptions.items()
        if not _ISSUE_REFERENCE.search(reason)
    )

    assert not unsourced, (
        "Fence exception(s) with no issue reference in the reason: "
        + ", ".join(unsourced)
    )


def test_fence_exceptions_cannot_grow_without_a_test_change() -> None:
    """The exception list is a subset of the three files issue #1193 owns.

    Shrinking is free; growing requires editing
    :data:`KNOWN_FENCE_EXCEPTIONS`, which puts a new exemption in the diff
    a reviewer is already reading rather than in a text file nobody opens.
    """
    exceptions = load_fence_exceptions(EXCEPTIONS_FILE)
    unexpected = sorted(set(exceptions) - KNOWN_FENCE_EXCEPTIONS)

    assert not unexpected, (
        "Unexpected fence exception(s): "
        + ", ".join(unexpected)
        + ". Fix the fence instead; if an exemption really is warranted, "
        "add the path to KNOWN_FENCE_EXCEPTIONS in this file with an issue."
    )


def test_load_fence_exceptions_ignores_comments_and_blank_lines(
    tmp_path: Path,
) -> None:
    """Comments, indented comments and blank lines are not entries.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    path = tmp_path / "exceptions.txt"
    path.write_text(
        "# Fence-integrity exceptions.\n"
        "\n"
        "a/b.md nested-fence trap; issue #1193\n"
        "   # an indented comment\n"
        "c/d.md same trap; issue #1193\n",
        encoding="utf-8",
    )

    assert load_fence_exceptions(path) == {
        "a/b.md": "nested-fence trap; issue #1193",
        "c/d.md": "same trap; issue #1193",
    }


def test_load_fence_exceptions_rejects_an_entry_without_a_reason(
    tmp_path: Path,
) -> None:
    """A bare path with no reason is an error, not a reasonless exemption.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    path = tmp_path / "exceptions.txt"
    path.write_text("a/b.md\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"a/b\.md"):
        load_fence_exceptions(path)


def test_load_fence_exceptions_rejects_a_duplicate_path(tmp_path: Path) -> None:
    """A repeated path is an error rather than a last-one-wins overwrite.

    Two entries for one file means two reasons, and a dict would silently
    drop the first -- taking its issue reference with it.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    path = tmp_path / "exceptions.txt"
    path.write_text(
        "a/b.md first reason; issue #1193\na/b.md second reason; issue #1193\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"a/b\.md"):
        load_fence_exceptions(path)


# --------------------------------------------------------------------------
# D. creek-tools/CLAUDE.md table of contents
# --------------------------------------------------------------------------


def test_creek_tools_claude_md_table_of_contents_is_bidirectional() -> None:
    """Every section is in the TOC and every TOC entry names a real heading.

    The anchor gate above only catches one direction. ``## 0. Repo topology``
    exists in the body of ``creek-tools/CLAUDE.md`` and is absent from the
    table of contents -- a real defect that no dangling-anchor check can
    see, because there is no link to dangle.
    """
    text = CREEK_TOOLS_CLAUDE_MD.read_text(encoding="utf-8")
    listed = _table_of_contents_anchors(text)
    assert listed, "No table of contents found in creek-tools/CLAUDE.md"

    all_slugs = set(heading_slugs(text))
    sections = heading_slugs(text, levels={2})

    missing_from_toc = [slug for slug in sections if slug not in set(listed)]
    dangling = [anchor for anchor in listed if anchor not in all_slugs]

    assert not missing_from_toc and not dangling, (
        "creek-tools/CLAUDE.md table of contents is out of sync -- sections "
        f"with no TOC entry: {missing_from_toc}; TOC entries with no such "
        f"heading: {dangling}"
    )
