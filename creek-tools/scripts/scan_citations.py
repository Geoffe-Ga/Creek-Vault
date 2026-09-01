"""Resolve and verify the symbols a scan issue cites, at its scan SHA.

Issue #1651. The producer scans file GitHub issues naming a file, a line
range and --- in the title --- a function name. The name was free text
nothing ever checked, and the ``scan:coverage`` producer confabulated it:
re-resolving #1446 and #1447 against their own scan SHAs found four of ten
and four of five citing definitions that exist in **no** revision of the
file, paraphrased from surrounding code (``_scrub_references`` cited as
``_replace_references``, ``_generate_filename`` as ``_unique_filename``).

Coverage is the worst-hit scan for a structural reason:
``--cov-report=term-missing`` emits line numbers and no names at all, so
the model has nothing to copy from, while radon, vulture, mypy and mutmut
hand the other scans real names.

Two operations, deliberately separate:

* :func:`resolve_enclosing_symbol` answers "what is actually at these
  lines" --- the **innermost** enclosing definition, so a nested ``def``
  wins over the method holding it. It returns ``None`` for a module-level
  line rather than reaching for the nearest name above, because reaching
  is the failure mode this module exists to prevent.
* :func:`verify_symbol` answers "does this exact name exist here at all",
  which is the cheap gate a filing pipeline can run per citation.

  **Residual risk, stated plainly:** that is "exists somewhere in this
  file", not "this citation is correct". Matching is by bare name over
  the whole blob, and a dotted citation is reduced to its last segment,
  so a citation naming the wrong class while reusing a leaf name that
  exists elsewhere in the same file verifies as ``True``. It catches
  every phantom in the real ground-truth table because invented names
  exist nowhere in the file, which is the observed failure mode --- but
  a caller wanting "is it where the citation says" must pair this with
  :func:`resolve_enclosing_symbol`, as ``verify-scan-citations.sh`` does.

Both read the blob **at the recorded SHA**, never the working tree. A
citation is a claim about a specific revision, and checking it against
whatever happens to be checked out is how a stale claim passes.
"""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CitationError(RuntimeError):
    """A citation could not be checked, as distinct from being wrong.

    Raised when the blob itself is unreadable --- an unknown SHA, a path
    absent at that revision, or source that will not parse. It is a
    separate condition from "the symbol is not there" on purpose: a
    verifier that answered ``False`` for an unreachable blob would report
    every stale citation as a phantom and every unreadable one as a pass,
    depending on which way the caller happened to read it.
    """


def _blob_at(repo: Path, sha: str, path: str) -> str:
    """Return the text of *path* as of *sha*.

    Args:
        repo: Repository root.
        sha: Commit SHA the citation was recorded against.
        path: Repository-relative path.

    Returns:
        The file's contents at that revision.

    Raises:
        CitationError: If the blob cannot be read.
    """
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"cannot read {path!r} at {sha!r}: {result.stderr.strip()}. "
            "A citation that cannot be checked must not be reported as "
            "verified."
        )
        raise CitationError(msg)
    return result.stdout


def _parse(source: str, *, path: str, sha: str) -> ast.Module:
    """Parse *source*, turning a syntax error into a CitationError.

    Args:
        source: File contents.
        path: Path, for the error message.
        sha: SHA, for the error message.

    Returns:
        The parsed module.

    Raises:
        CitationError: If the source does not parse.
    """
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        msg = f"cannot parse {path!r} at {sha!r}: {exc}"
        raise CitationError(msg) from exc


_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _qualified_name(stack: list[_Definition]) -> str:
    """Join a nesting stack into a dotted qualified name.

    Args:
        stack: Enclosing definitions, outermost first.

    Returns:
        The dotted name, e.g. ``Engine.purge_daterange.body``.
    """
    return ".".join(node.name for node in stack)


def resolve_enclosing_symbol(
    *,
    repo: Path,
    sha: str,
    path: str,
    line: int,
) -> str | None:
    """Return the innermost definition enclosing *line*, at *sha*.

    The innermost one, not the nearest one above: a nested ``def`` inside
    a method is a different symbol from the method, and a
    nearest-preceding-``def`` scan cannot tell them apart.

    ``lineno`` on a decorated definition points at the ``def`` itself
    rather than the decorator, so a decorated function resolves from its
    body without special-casing.

    Args:
        repo: Repository root.
        sha: Commit SHA the citation was recorded against.
        path: Repository-relative path.
        line: 1-indexed line number.

    Returns:
        The dotted qualified name, or ``None`` when *line* sits at module
        level --- never a guess at the nearest name above it.

    Raises:
        CitationError: If the blob cannot be read or parsed.
    """
    tree = _parse(_blob_at(repo, sha, path), path=path, sha=sha)
    best: list[_Definition] = []

    def walk(node: ast.AST, stack: list[_Definition]) -> None:
        """Descend, recording the deepest stack that still contains *line*."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                end = child.end_lineno or child.lineno
                if child.lineno <= line <= end:
                    nested = [*stack, child]
                    nonlocal best
                    if len(nested) > len(best):
                        best = nested
                    walk(child, nested)
                continue
            walk(child, stack)

    walk(tree, [])
    return _qualified_name(best) if best else None


def verify_symbol(*, repo: Path, sha: str, path: str, symbol: str) -> bool:
    """Return whether a definition named *symbol* exists in *path* at *sha*.

    Matching is by bare name anywhere in the blob, including nested
    definitions: a citation naming ``body`` is not wrong merely because
    the symbol is nested, only when no such definition exists. A dotted
    citation is matched on its last segment, so
    ``Engine.purge_daterange`` verifies against ``purge_daterange``.

    Args:
        repo: Repository root.
        sha: Commit SHA the citation was recorded against.
        path: Repository-relative path.
        symbol: The exact name the issue claims is there.

    Returns:
        ``True`` when a matching definition exists at that revision.

    Raises:
        CitationError: If the blob cannot be read or parsed.
    """
    tree = _parse(_blob_at(repo, sha, path), path=path, sha=sha)
    wanted = symbol.rsplit(".", 1)[-1]
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == wanted
        for node in ast.walk(tree)
    )


class MalformedFindingError(ValueError):
    """A findings line is not a usable JSON object.

    Distinct from a wrong citation. A payload that will not parse, or that
    parses to something other than an object, means a broken producer --
    and degrading it into "no symbol declared" would hide it behind the
    benign case, which is the "gate reports nothing wrong when it did
    nothing" failure this module exists to refuse.
    """


def extract_citations(line: str) -> list[tuple[str, str, str]]:
    """Extract ``(file, symbol, lines)`` triples from one findings line.

    Args:
        line: One newline-delimited JSON finding.

    Returns:
        One triple per declared symbol; empty when the finding declares
        none, which is legal for a whole-module or config finding.

    Raises:
        MalformedFindingError: If *line* is not a JSON object.
    """
    try:
        finding = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = str(exc)
        raise MalformedFindingError(msg) from exc
    if not isinstance(finding, dict):
        msg = f"finding is {type(finding).__name__}, expected an object"
        raise MalformedFindingError(msg)
    symbols = finding.get("symbol") or finding.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    path = str(finding.get("file", ""))
    lines = str(finding.get("lines", ""))
    return [(path, str(name), lines) for name in symbols]


# --- Reading citations back out of a FILED issue body (issue #1700) --------
#
# `extract_citations` above reads what a producer is ABOUT to file. These
# read what it actually DID file, so the backstop in `_claude-scan.yml`
# never has to ask the agent whether it verified itself.

_SOURCE_SUFFIXES = (
    ".py",
    ".pyi",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".txt",
)
"""Extensions a backticked span must carry to be read as a file path."""

_EXTENSION_SEGMENTS = frozenset(suffix.lstrip(".") for suffix in _SOURCE_SUFFIXES)
"""The same extensions as bare dotted segments, for the bare-file-name rule."""

_RANGE_RE = re.compile(r"^:(\d+(?:-\d+)?)$")
"""A standalone line span, e.g. ``:133-134`` -- never an identifier."""

_TRAILING_RANGE_RE = re.compile(r":(\d+(?:-\d+)?)$")
"""The ``:120-164`` suffix a path span may carry."""

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
"""A bare or dotted Python identifier."""

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
"""A recorded scan SHA. The floor is 7, not 40: #869 records ``82f9b89``."""

_NOT_SYMBOLS = frozenset(keyword.kwlist) | {"None", "True", "False"}
"""Identifier-shaped spans that are never a citation."""


def _context_block(body: str) -> str:
    """Return the ``## Context`` section of an issue body.

    Args:
        body: The full issue body.

    Returns:
        Every line between the ``## Context`` heading and the next ``## ``
        heading; empty when the body has no Context section.
    """
    collected: list[str] = []
    inside = False
    for line in body.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line.strip() == "## Context"
            continue
        if inside:
            collected.append(line)
    return "\n".join(collected)


def _bullet_block(context: str, label: str) -> str:
    """Return the ``- <label>:`` bullet plus its continuation lines.

    A scan issue wraps a long citation bullet across several indented
    lines (#1449 spans four), so a single-line read would drop most of
    the citations. Collection stops at the next top-level ``- ``, the
    next heading, or a blank line.

    Args:
        context: The ``## Context`` section.
        label: The bullet label, e.g. ``File(s)`` or ``Symbol(s)``.

    Returns:
        The bullet text, or the empty string when absent.
    """
    prefix = f"- {label}:"
    collected: list[str] = []
    for line in context.splitlines():
        if not collected:
            if line.startswith(prefix):
                collected.append(line)
            continue
        if line.startswith(("- ", "#")) or not line.strip():
            break
        collected.append(line)
    return "\n".join(collected)


def _backtick_spans(text: str) -> list[str]:
    """Return the contents of every backticked span, in order.

    Only backticked text is ever read as a path, range or symbol. That is
    the anti-false-positive rule: #1449's bullet names ``enclave payload
    options`` in bare prose beside real backticked names, and inventing a
    citation from prose is the very failure this module exists to refuse.

    Args:
        text: Any markdown fragment.

    Returns:
        The span contents, stripped, in source order.
    """
    return [span.strip() for span in re.findall(r"`([^`]*)`", text)]


def _split_path_range(span: str) -> tuple[str, str]:
    """Split a trailing ``:<lines>`` off a path span.

    Args:
        span: A backticked span, e.g. ``a/b.py:120-164``.

    Returns:
        ``(path, lines)``; ``lines`` is empty when the span carries none.
    """
    match = _TRAILING_RANGE_RE.search(span)
    if match:
        return span[: match.start()], match.group(1)
    return span, ""


def _classify_span(span: str, current_file: str) -> tuple[str, str]:
    """Classify one backticked span against the file in force.

    Symbol recognition is **gated on the current file being Python**. A
    ``.yml`` blob reaching the ast resolver raises
    :class:`CitationError`, which ``verify-scan-citations.sh`` counts as
    a phantom -- so an ungated parser would redden a run for a citation
    that was correct.

    A span shaped like a **bare file name** -- ``README.md``,
    ``conftest.py`` -- is identifier-shaped and carries no directory, so
    it satisfies neither the path rule nor any useful reading. It returns
    ``PATH`` with an **empty** value, which clears the file in force.
    Both alternatives manufacture a citation the issue never made:
    reading it as a symbol sends ``verify_symbol`` hunting for a
    definition named ``md`` (it strips to the last dotted segment), which
    exists nowhere, so a correct issue is reported as citing a phantom
    and gets an automated correction comment; and leaving the previous
    path in force would attribute every later name in the same bullet to
    a file the issue may not have meant. The cost is a real dotted
    citation whose last segment collides with an extension --
    ``Response.json`` -- going unchecked. That is the right side to err
    on: a missed check is silent, an invented one is a red run against
    correct work.

    Args:
        span: The span contents.
        current_file: The path in force for this bullet, if any.

    Returns:
        ``(kind, value)`` where kind is ``PATH``, ``RANGE``, ``SYMBOL``
        or ``IGNORE``. A ``PATH`` value is the span verbatim, still
        carrying any trailing range -- or empty, for an unresolvable
        file reference.
    """
    if not span:
        return ("IGNORE", "")
    span_range = _RANGE_RE.match(span)
    if span_range:
        return ("RANGE", span_range.group(1))
    path, _ = _split_path_range(span)
    if "." in path and path.rsplit(".", 1)[-1] in _EXTENSION_SEGMENTS:
        return ("PATH", span if "/" in path else "")
    if not current_file.endswith(".py"):
        return ("IGNORE", "")
    if _IDENTIFIER_RE.match(span) and span not in _NOT_SYMBOLS:
        return ("SYMBOL", span)
    return ("IGNORE", "")


@dataclass
class _BlockState:
    """The path and default line range carried across a bullet's clauses.

    Attributes:
        file: The repository-relative path in force.
        lines: The range the path itself declared, used when a clause
            names a symbol but no range of its own.
    """

    file: str = ""
    lines: str = ""


def _citations_from_block(block: str, state: _BlockState) -> list[tuple[str, str, str]]:
    """Extract ``(file, symbol, lines)`` triples from one citation bullet.

    The bullet is split on commas into clauses; ``state`` carries the
    path across them. A clause's range attaches to that clause's symbols
    **regardless of order** -- #993 writes ``:266-277`` before
    ``iter_entries`` and #1447 writes the range after the name -- falling
    back to the range the path itself declared.

    Args:
        block: The bullet text, continuation lines included.
        state: Mutated in place as the bullet names paths.

    Returns:
        One triple per symbol named, in source order.
    """
    citations: list[tuple[str, str, str]] = []
    for clause in block.split(","):
        symbols: list[str] = []
        clause_lines = ""
        for span in _backtick_spans(clause):
            kind, value = _classify_span(span, state.file)
            if kind == "PATH":
                state.file, state.lines = _split_path_range(value)
            elif kind == "RANGE":
                clause_lines = value
            elif kind == "SYMBOL":
                symbols.append(value)
        lines = clause_lines or state.lines
        citations.extend((state.file, name, lines) for name in symbols)
    return citations


def _distinct_python_paths(block: str) -> int:
    """Count the distinct ``.py`` paths a bullet names.

    Args:
        block: A citation bullet.

    Returns:
        How many different Python files it cites.
    """
    paths = set()
    for span in _backtick_spans(block):
        candidate, _ = _split_path_range(span)
        if "/" in candidate and candidate.endswith(".py"):
            paths.add(candidate)
    return len(paths)


def citations_from_body(body: str) -> list[tuple[str, str, str]]:
    """Extract every verifiable citation from a filed scan issue body.

    Two shapes, both live. The template's forward-looking form puts the
    path on a ``- File(s):`` bullet and the name on a ``- Symbol(s):``
    bullet (``prompts/templates/scan-issue-body.md``, landed in 8160ed0);
    every issue filed before that inlines the names in the ``File(s)``
    bullet itself.

    **When the ``File(s)`` bullet names more than one Python file, the
    ``Symbol(s)`` identifiers are dropped rather than attached to the
    first.** Attaching them would manufacture a citation the issue never
    made, and a manufactured citation is the exact thing this pipeline
    refuses. The cost is a missed check; the alternative is a false
    phantom that reddens a correct run.

    Args:
        body: The full issue body.

    Returns:
        ``(file, symbol, lines)`` triples; empty when the body cites no
        symbol, which is legal for a whole-module finding.
    """
    context = _context_block(body)
    files_block = _bullet_block(context, "File(s)")
    state = _BlockState()
    citations = _citations_from_block(files_block, state)
    symbol_block = _bullet_block(context, "Symbol(s)")
    if symbol_block and _distinct_python_paths(files_block) <= 1:
        inherited = _BlockState(file=state.file, lines=state.lines)
        citations.extend(_citations_from_block(symbol_block, inherited))
    return citations


def sha_from_body(body: str) -> str | None:
    """Return the scan SHA an issue body records for its citations.

    Args:
        body: The full issue body.

    Returns:
        The first hex span on the ``- Scanned at commit:`` line, or
        ``None`` when the body records none -- including a body that
        still carries the template's ``<SHA>`` placeholder.
    """
    for line in body.splitlines():
        if not line.lstrip().startswith("- Scanned at commit:"):
            continue
        for span in _backtick_spans(line):
            if _SHA_RE.match(span):
                return span
    return None


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Verify a scan issue's symbol citation against its scan SHA.",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--sha")
    parser.add_argument("--path")
    parser.add_argument("--symbol", help="Verify this exact name exists.")
    parser.add_argument(
        "--line",
        type=int,
        help="Resolve the innermost definition enclosing this line instead.",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Read newline-delimited JSON findings on stdin and print "
        "tab-separated file/symbol/lines triples.",
    )
    parser.add_argument(
        "--from-issues",
        action="store_true",
        help="Read a `gh issue list --json number,body,createdAt` array on "
        "stdin and print the citations the filed bodies carry.",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="Issue numbers that existed before the run, comma- or "
        "space-separated. Everything else is treated as newly filed.",
    )
    parser.add_argument(
        "--created-after",
        default="",
        help="ISO-8601 instant; issues created before it are skipped. A "
        "secondary filter only -- --exclude is the selector.",
    )
    parser.add_argument(
        "--default-sha",
        default="",
        help="Scan SHA to use for an issue whose body records none.",
    )
    return parser


def _number_set(raw: str) -> set[int]:
    """Parse a comma- or space-separated list of issue numbers.

    Args:
        raw: The ``--exclude`` value; may be empty.

    Returns:
        The numbers named.

    Raises:
        ValueError: If any token is not an integer. A malformed baseline
            must be an error, never a silently empty set -- an empty set
            makes every pre-existing issue look newly filed.
    """
    return {int(token) for token in re.split(r"[,\s]+", raw) if token}


def _is_newly_filed(issue: dict[str, Any], excluded: set[int], after: str) -> bool:
    """Whether this issue was filed by the run under examination.

    Args:
        issue: One ``gh issue list`` record.
        excluded: Issue numbers present in the pre-agent snapshot.
        after: ISO-8601 instant; empty disables the secondary filter.

    Returns:
        ``True`` when the issue is outside the snapshot and not older
        than the run.
    """
    number = issue.get("number")
    if number in excluded:
        return False
    created = str(issue.get("createdAt") or "")
    if after and created and created < after:
        print(
            f"::notice::issue #{number} was created at {created}, before this "
            f"run started at {after}; skipping",
            file=sys.stderr,
        )
        return False
    return True


def _print_issue_citations(issue: dict[str, Any], default_sha: str) -> int:
    """Print one ``CITATION`` record per symbol the issue body cites.

    Each record's JSON payload is the exact ``{"file","symbol","lines"}``
    shape :func:`extract_citations` consumes, so the backstop pipes it
    straight into ``verify-scan-citations.sh`` rather than reimplementing
    symbol resolution.

    Args:
        issue: One ``gh issue list`` record.
        default_sha: Fallback when the body records no scan SHA.

    Returns:
        How many citation records were printed.
    """
    number = issue.get("number")
    body = str(issue.get("body") or "")
    sha = sha_from_body(body) or default_sha
    citations = citations_from_body(body)
    for path, symbol, lines in citations:
        payload = json.dumps(
            {"file": path, "symbol": symbol, "lines": lines}, sort_keys=True
        )
        print(f"CITATION\t{number}\t{sha}\t{payload}")
    return len(citations)


def _from_issues_mode(args: argparse.Namespace) -> int:
    """Turn a ``gh issue list`` payload into citation records.

    Prints ``RETURNED<TAB>n`` (how many issues the listing held, so the
    caller can detect a truncated page), ``ISSUES<TAB>n`` (how many of
    them this run filed), then one ``CITATION`` line per symbol.

    Args:
        args: Parsed arguments carrying exclude, created_after and
            default_sha.

    Returns:
        ``0`` on success, ``2`` when stdin or ``--exclude`` is malformed.
    """
    try:
        issues = json.loads(sys.stdin.read())
        excluded = _number_set(str(args.exclude))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"UNCHECKABLE --from-issues input: {exc}", file=sys.stderr)
        return 2
    if not isinstance(issues, list) or not all(
        isinstance(item, dict) for item in issues
    ):
        print(
            "UNCHECKABLE --from-issues input: expected a JSON array of issue "
            "objects from `gh issue list --json number,body,createdAt`",
            file=sys.stderr,
        )
        return 2
    print(f"RETURNED\t{len(issues)}")
    selected = [
        issue
        for issue in issues
        if _is_newly_filed(issue, excluded, str(args.created_after))
    ]
    print(f"ISSUES\t{len(selected)}")
    for issue in selected:
        _print_issue_citations(issue, str(args.default_sha))
    return 0


def _extract_mode() -> int:
    """Print ``file\tsymbol\tlines`` for every citation on stdin.

    Returns:
        ``0`` when every line parsed, ``2`` when any did not.
    """
    status = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            triples = extract_citations(line)
        except MalformedFindingError as exc:
            print(f"MALFORMED\t{exc}\t")
            status = 2
            continue
        for path, symbol, lines in triples:
            print(f"{path}\t{symbol}\t{lines}")
    return status


def _verify_mode(args: argparse.Namespace) -> int:
    """Verify one symbol citation and report the verdict.

    Args:
        args: Parsed arguments carrying repo, sha, path, symbol and line.

    Returns:
        ``0`` when the symbol exists at that revision, ``1`` when it does not.
    """
    if verify_symbol(repo=args.repo, sha=args.sha, path=args.path, symbol=args.symbol):
        print(f"OK {args.symbol} exists in {args.path} at {args.sha[:12]}")
        return 0
    actual = (
        resolve_enclosing_symbol(
            repo=args.repo, sha=args.sha, path=args.path, line=args.line
        )
        if args.line
        else None
    )
    suffix = f"; the lines cited hold {actual!r}" if actual else ""
    print(
        f"PHANTOM {args.symbol!r} has no definition in {args.path} "
        f"at {args.sha[:12]}{suffix}",
        file=sys.stderr,
    )
    return 1


def _resolve_mode(args: argparse.Namespace) -> int:
    """Print the innermost definition enclosing the requested line.

    Args:
        args: Parsed arguments carrying repo, sha, path and line.

    Returns:
        ``0`` always; an unresolvable blob raises :class:`CitationError`.
    """
    resolved = resolve_enclosing_symbol(
        repo=args.repo, sha=args.sha, path=args.path, line=args.line or 1
    )
    print(resolved if resolved else "<module level>")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch to extract, verify or resolve, and report.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when the citation holds, ``1`` when it does not, ``2`` when
        it could not be checked at all.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.extract:
        return _extract_mode()
    if args.from_issues:
        return _from_issues_mode(args)
    if not args.sha or not args.path:
        parser.error(
            "--sha and --path are required unless --extract or --from-issues is given"
        )
    try:
        return _verify_mode(args) if args.symbol else _resolve_mode(args)
    except CitationError as exc:
        print(f"UNCHECKABLE {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
