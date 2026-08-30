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

Both read the blob **at the recorded SHA**, never the working tree. A
citation is a claim about a specific revision, and checking it against
whatever happens to be checked out is how a stale claim passes.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Verify a scan issue's symbol citation against its scan SHA.",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--sha", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--symbol", help="Verify this exact name exists.")
    parser.add_argument(
        "--line",
        type=int,
        help="Resolve the innermost definition enclosing this line instead.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Verify one citation, or resolve one line, and report.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when the citation holds, ``1`` when it does not, ``2`` when
        it could not be checked at all.
    """
    args = _build_parser().parse_args(argv)
    try:
        if args.symbol:
            if verify_symbol(
                repo=args.repo, sha=args.sha, path=args.path, symbol=args.symbol
            ):
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
        resolved = resolve_enclosing_symbol(
            repo=args.repo, sha=args.sha, path=args.path, line=args.line or 1
        )
        print(resolved if resolved else "<module level>")
    except CitationError as exc:
        print(f"UNCHECKABLE {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
