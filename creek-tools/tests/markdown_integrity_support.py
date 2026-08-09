"""Markdown structure parsing for the integrity gate (issue #1190).

``tests/test_markdown_integrity.py`` asserts that every tracked Markdown
file closes the code fences it opens, ends with exactly one newline, and
only links to anchors that exist. The parsing those assertions need is
here, hand-rolled against the subset of CommonMark the repository
actually exercises.

Hand-rolled rather than delegated. ``markdown-it-py`` is on the tree only
as a transitive of ``rich``, and it auto-closes an unclosed fence at end
of input -- so it cannot answer the one question this gate exists to ask.
The naive alternative (``grep -c '^```'``) is worse: it counts ``6`` on
``creek-tools/CLAUDE.md``, an even number, because the broken opener is
indented three spaces inside an ordered-list item and never touches
column zero. An indent-tolerant count returns ``11``.

Three rules carry the whole gate, and each has a live counter-example in
this repository:

* A fence opener may be indented **0-3 spaces**; a fourth space makes the
  line an indented code block instead. This is the
  ``creek-tools/CLAUDE.md`` defect.
* A closer must use the **same** fence character, run **at least as
  long** as the opener's, and carry **no info string**.
* Fences do not **nest**. Inside an open fence every line is content, so
  a ``markdown`` block demonstrating a ``json`` block ends early at the
  inner block's bare closer, and the outer block's intended closer then
  opens a fresh fence that is never closed. Three skill files were broken
  exactly this way; all three were deleted by the context prune, so
  ``tests/markdown-integrity-exceptions.txt`` is now empty and the gate
  runs unexempted.

``pyproject.toml`` sets ``python_files = ["test_*.py"]``, so this module
is never collected as a test module -- it is a plain support module in
the same spirit as ``tests/shell_command_support.py``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator
    from pathlib import Path

# A fence line: up to three spaces of indent, then a run of three or more
# backticks or tildes, then (for an opener) an info string. Written
# verbose so the 0-3 indent allowance -- the crux of issue #1190 -- is
# documented where it is enforced rather than buried in a literal.
_FENCE_LINE = re.compile(
    r"""
    ^\ {0,3}                # 0-3 spaces; a 4th makes an indented code block
    (?P<run>`{3,}|~{3,})    # three or more of a single fence character
    (?P<info>.*)$           # info string; disqualifies the line as a closer
    """,
    re.VERBOSE,
)

# An ATX heading: 0-3 spaces of indent, one to six hashes, then at least
# one space. Setext headings are deliberately unsupported -- see
# :func:`heading_slugs`.
_ATX_HEADING = re.compile(
    r"""
    ^\ {0,3}
    (?P<hashes>\#{1,6})     # `#` needs escaping: re.VERBOSE reads it as a comment
    [\ \t]+
    (?P<text>.*)$
    """,
    re.VERBOSE,
)

# An optional closing run of hashes, which GitHub strips before slugging.
# The run must be preceded by whitespace (or be the whole line), so a
# trailing hash inside a word -- ``## C#`` -- survives.
_CLOSING_HASHES = re.compile(r"(?:^|\s)#+[ \t]*$")

# Everything GitHub's slugger deletes outright: anything that is not a
# word character (alphanumeric or underscore), a hyphen or a space.
# Deleted, never replaced -- ``## Generic (`generic`)`` slugs to
# ``generic-generic``, not ``generic---generic--``.
_SLUG_DROP = re.compile(r"[^\w\- ]")

# An inline link whose target is a same-file anchor. The target must
# start with ``#``, which is what puts ``other.md#frag`` and
# ``https://host/page#frag`` out of scope.
_ANCHOR_LINK = re.compile(r"\[[^\]]*\]\(#(?P<anchor>[^)\s]+)\)")


@dataclass(frozen=True)
class _Fence:
    """A parsed fence line.

    Attributes:
        char: The fence character, either a backtick or a tilde.
        length: How many fence characters the run contains.
        info: The info string that followed the run, stripped. Non-empty
            only on an opener: an info string disqualifies a line from
            closing anything.
    """

    char: str
    length: int
    info: str


def _parse_fence(line: str) -> _Fence | None:
    """Parse ``line`` as a code fence.

    Args:
        line: A single line of Markdown, without its terminator.

    Returns:
        The parsed fence, or ``None`` when the line is not a fence --
        including when it is indented four or more spaces, which makes it
        an indented code block.
    """
    match = _FENCE_LINE.match(line)
    if match is None:
        return None
    run = match["run"]
    return _Fence(char=run[0], length=len(run), info=match["info"].strip())


def _closes(opener: _Fence, candidate: _Fence) -> bool:
    """Report whether ``candidate`` closes the block ``opener`` started.

    Args:
        opener: The fence that opened the currently open block.
        candidate: A fence found while that block is open.

    Returns:
        ``True`` when ``candidate`` uses the same fence character, runs at
        least as long as ``opener``, and carries no info string.
    """
    return (
        candidate.char == opener.char
        and candidate.length >= opener.length
        and not candidate.info
    )


def _scan_fences(text: str) -> tuple[list[bool], int | None]:
    """Walk ``text`` once, tracking fenced-code state.

    The single scan behind all three of :func:`find_unclosed_fence`,
    :func:`heading_slugs` and :func:`same_file_anchor_links`, so those
    three can never disagree about what counts as code.

    Fences do not nest: while a block is open, a fence line either closes
    it or is content, and can never open a second block.

    Args:
        text: A Markdown document.

    Returns:
        A ``(flags, opener)`` pair. ``flags`` has one entry per line of
        ``text``, ``True`` when the line belongs to a fenced code block
        (its opener and closer included). ``opener`` is the 1-based line
        number of the fence still open at end of input, or ``None``.
    """
    flags: list[bool] = []
    open_fence: _Fence | None = None
    opener_line: int | None = None

    for number, line in enumerate(text.splitlines(), start=1):
        fence = _parse_fence(line)
        if open_fence is None:
            if fence is not None:
                open_fence, opener_line = fence, number
            flags.append(fence is not None)
            continue
        flags.append(True)
        if fence is not None and _closes(open_fence, fence):
            open_fence, opener_line = None, None

    return flags, opener_line


def _lines_outside_code(text: str) -> Iterator[tuple[int, str]]:
    """Yield the lines of ``text`` that are not inside a fenced block.

    Args:
        text: A Markdown document.

    Yields:
        ``(1-based line number, line)`` for each line outside fenced code,
        in document order.
    """
    flags, _ = _scan_fences(text)
    numbered = enumerate(zip(text.splitlines(), flags, strict=True), start=1)
    for number, (line, in_code) in numbered:
        if not in_code:
            yield number, line


def find_unclosed_fence(text: str) -> int | None:
    """Locate the code fence that ``text`` opens and never closes.

    Args:
        text: A Markdown document.

    Returns:
        The 1-based line number of the opener still open at end of input,
        or ``None`` when every fence in the document is closed.
    """
    return _scan_fences(text)[1]


def _slug(heading_text: str) -> str:
    """Convert heading text into a GitHub anchor slug.

    Lowercase, delete every character that is not alphanumeric, a space, a
    hyphen or an underscore, then map spaces to hyphens. Deletion (rather
    than replacement) is what makes ``9. Tool Usage & Code Standards``
    slug to ``9-tool-usage--code-standards``: the ampersand vanishes and
    the two spaces that surrounded it each become a hyphen.

    Args:
        heading_text: The heading's text, with any closing run of hashes
            already stripped.

    Returns:
        The anchor slug, before de-duplication.
    """
    return _SLUG_DROP.sub("", heading_text.lower()).replace(" ", "-")


def _atx_headings(text: str) -> list[tuple[int, str]]:
    """Return the level and bare slug of every ATX heading in ``text``.

    Args:
        text: A Markdown document.

    Returns:
        ``(level, slug)`` pairs in document order, for headings outside
        fenced code. The slugs are not yet de-duplicated.
    """
    headings: list[tuple[int, str]] = []
    for _, line in _lines_outside_code(text):
        match = _ATX_HEADING.match(line)
        if match is None:
            continue
        title = _CLOSING_HASHES.sub("", match["text"]).strip()
        headings.append((len(match["hashes"]), _slug(title)))
    return headings


def _deduplicate(slugs: Iterable[str]) -> list[str]:
    """Suffix repeated slugs the way GitHub does.

    Args:
        slugs: Bare slugs in document order.

    Returns:
        The same slugs with ``-1``, ``-2``, ... appended to the second and
        later occurrences of each value; the first keeps the bare slug.
    """
    seen: dict[str, int] = {}
    unique: list[str] = []
    for slug in slugs:
        occurrence = seen.get(slug, 0)
        seen[slug] = occurrence + 1
        unique.append(slug if occurrence == 0 else f"{slug}-{occurrence}")
    return unique


def heading_slugs(text: str, *, levels: Collection[int] | None = None) -> list[str]:
    """Return the GitHub anchor slugs of ``text``'s ATX headings.

    Only ATX headings (``^ {0,3}#{1,6} ``) are recognised. Setext support
    is deliberately absent: there are no ``^=+$`` lines in the tracked
    Markdown, so it could only ever fire on ``---`` -- turning every
    horizontal rule in the repository into a phantom anchor minted from
    whatever prose preceded it. Headings inside fenced code are skipped
    for the same reason, using the same scanner as
    :func:`find_unclosed_fence`.

    Args:
        text: A Markdown document.
        levels: When given, keep only headings of these levels. The filter
            is applied *after* de-duplication, so a heading's slug is the
            one GitHub would mint for it in the whole document rather than
            in the filtered subset.

    Returns:
        The slugs, in document order.
    """
    headings = _atx_headings(text)
    unique = _deduplicate(slug for _, slug in headings)
    return [
        slug
        for (level, _), slug in zip(headings, unique, strict=True)
        if levels is None or level in levels
    ]


def same_file_anchor_links(text: str) -> list[tuple[int, str]]:
    """Return every same-file anchor link in ``text``.

    Links inside fenced code are excluded. Several skill files in this
    repository are *examples of Markdown* and show a sample table of
    contents inside a ``markdown`` block; resolving those against the
    enclosing document's headings would manufacture failures nobody can
    fix.

    Args:
        text: A Markdown document.

    Returns:
        ``(1-based line number, anchor without its leading "#")`` for each
        ``[label](#anchor)`` link, in document order and left to right
        within a line.
    """
    return [
        (number, match["anchor"])
        for number, line in _lines_outside_code(text)
        for match in _ANCHOR_LINK.finditer(line)
    ]


def tracked_markdown_files(repo_root: Path) -> list[Path]:
    """Return every ``*.md`` file git tracks under ``repo_root``.

    Always shells out with an explicit ``-C repo_root``. The CI ``quality``
    job runs with ``working-directory: creek-tools``, so a bare ``git
    ls-files`` would quietly enumerate one subtree and the gate would miss
    most of the repository. ``-z`` because git otherwise quotes paths
    containing unusual bytes. Whether ``.git`` is a directory is never
    probed: in a linked worktree it is a *file*, and that probe would
    false-negative into an empty sweep.

    Args:
        repo_root: Directory to run git in, and the base the returned
            paths are joined onto.

    Returns:
        The tracked Markdown paths, joined onto ``repo_root`` and sorted.

    Raises:
        RuntimeError: If the ``git`` binary cannot be run, if git exits
            non-zero, or if the sweep comes back empty. Every failure mode
            is loud: a discovery step that can return ``[]`` turns every
            assertion downstream into a pass over nothing.
    """
    command = ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.md"]
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        raise RuntimeError(f"Could not run `git ls-files`: {error}") from error

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"`git ls-files` exited {completed.returncode} in {repo_root}: "
            f"{detail or 'no diagnostic on stderr'}"
        )

    entries = completed.stdout.decode("utf-8").split("\0")
    files = sorted(repo_root / entry for entry in entries if entry)
    if not files:
        raise RuntimeError(f"git tracks no Markdown files under {repo_root}")
    return files


def load_fence_exceptions(path: Path) -> dict[str, str]:
    """Parse the fence-exception list at ``path``.

    The format mirrors ``scripts/coverage-waivers.txt``: blank lines and
    lines whose stripped form starts with ``#`` are ignored, and every
    other line is partitioned on its first space into a repo-relative
    POSIX path and a reason. Listed files are exempt from the fence check
    **only**; the trailing-newline and anchor checks still apply.

    Args:
        path: The exception file to read.

    Returns:
        Path to reason, in file order.

    Raises:
        ValueError: If an entry carries no reason -- an exemption nobody
            has to justify is a permanent one -- or if a path is listed
            twice, which a plain dict would silently resolve last-one-wins
            and take the first reason's issue reference with it.
    """
    exceptions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry, _, reason = stripped.partition(" ")
        if not reason.strip():
            raise ValueError(
                f"Fence exception {entry!r} in {path} has no reason; every "
                "entry needs one, citing the issue that will remove it."
            )
        if entry in exceptions:
            raise ValueError(f"Fence exception {entry!r} is listed twice in {path}")
        exceptions[entry] = reason.strip()
    return exceptions
