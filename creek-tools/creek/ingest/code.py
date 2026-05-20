"""Code repository ingestor for the Creek ingest pipeline.

Extracts human-readable insights from code repositories: READMEs,
CLAUDE.md project context, Architecture Decision Records, significant
comments (3+ lines or marked with TODO/FIXME/NOTE/HACK), Python
docstrings, and commit messages. Skips generated and vendor directories.

Exports:
    CodeIngestor: Concrete ``Ingestor`` subclass for code repositories.
    _extract_docstrings: Extract Python docstrings via ``ast.parse()``.
    _extract_comments: Extract significant comments from source files.
    _is_skip_directory: Check whether a directory should be skipped.
"""

from __future__ import annotations

import ast
import logging
import re
import shutil
import subprocess  # nosec B404 — used with hardcoded args only (git log)
from datetime import datetime
from pathlib import Path
from typing import Any

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_encoding,
)
from creek.models import SourcePlatform

logger = logging.getLogger(__name__)

# ---- Constants ----

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "vendor",
        ".git",
        "__pycache__",
        ".tox",
        ".venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".eggs",
    }
)
"""Directory names to skip during discovery."""

_README_PATTERNS: tuple[str, ...] = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
)
"""Filename patterns recognized as README files."""

_SIGNIFICANT_COMMENT_MARKERS: tuple[str, ...] = (
    "TODO",
    "FIXME",
    "NOTE",
    "HACK",
)
"""Markers that make a single-line comment significant."""

_MIN_COMMENT_BLOCK_LINES = 3
"""Minimum consecutive comment lines to qualify as a significant block."""


# ---- Helper Functions ----


def _is_skip_directory(path: Path) -> bool:
    """Check whether a directory should be skipped during discovery.

    Args:
        path: The directory path to check.

    Returns:
        True if the directory name is in the skip list.
    """
    return path.name in _SKIP_DIRS


def _should_skip_path(path: Path) -> bool:
    """Check whether any ancestor of the path is a skip directory.

    Args:
        path: The file path to check.

    Returns:
        True if any ancestor directory is in the skip list.
    """
    return any(part in _SKIP_DIRS for part in path.parts)


def _extract_docstrings(source: str, file_path: str) -> list[dict[str, Any]]:
    """Extract Python docstrings from source code using ``ast.parse()``.

    Extracts module, class, function, and method docstrings. Returns
    an empty list if the source has syntax errors.

    Args:
        source: The Python source code text.
        file_path: Path to the source file (for metadata).

    Returns:
        A list of dicts with keys: kind, name, docstring, line, file.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("Syntax error parsing %s, skipping docstrings", file_path)
        return []

    results: list[dict[str, Any]] = []
    _extract_module_docstring(tree, file_path, results)
    _extract_node_docstrings(tree, file_path, results, prefix="")
    return results


def _extract_module_docstring(
    tree: ast.Module,
    file_path: str,
    results: list[dict[str, Any]],
) -> None:
    """Extract the module-level docstring if present.

    Args:
        tree: The parsed AST module.
        file_path: Path to the source file.
        results: List to append results to.
    """
    docstring = ast.get_docstring(tree)
    if docstring:
        results.append(
            {
                "kind": "module",
                "name": Path(file_path).stem,
                "docstring": docstring,
                "line": 1,
                "file": file_path,
            }
        )


def _extract_node_docstrings(
    tree: ast.AST,
    file_path: str,
    results: list[dict[str, Any]],
    prefix: str,
) -> None:
    """Recursively extract docstrings from classes, functions, and methods.

    Args:
        tree: The AST node to walk.
        file_path: Path to the source file.
        results: List to append results to.
        prefix: Dot-separated prefix for nested names (e.g. class name).
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            _process_class_node(node, file_path, results, prefix)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _process_function_node(node, file_path, results, prefix, "function")


def _process_class_node(
    node: ast.ClassDef,
    file_path: str,
    results: list[dict[str, Any]],
    prefix: str,
) -> None:
    """Extract docstring from a class node and recurse into its methods.

    Args:
        node: The class AST node.
        file_path: Path to the source file.
        results: List to append results to.
        prefix: Dot-separated prefix for the class name.
    """
    class_name = f"{prefix}{node.name}" if not prefix else f"{prefix}.{node.name}"
    docstring = ast.get_docstring(node)
    if docstring:
        results.append(
            {
                "kind": "class",
                "name": class_name,
                "docstring": docstring,
                "line": node.lineno,
                "file": file_path,
            }
        )
    # Extract method docstrings
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _process_function_node(child, file_path, results, class_name, "method")


def _process_function_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str,
    results: list[dict[str, Any]],
    prefix: str,
    kind: str,
) -> None:
    """Extract docstring from a function or method node.

    Args:
        node: The function AST node.
        file_path: Path to the source file.
        results: List to append results to.
        prefix: Dot-separated prefix (class name for methods).
        kind: Either 'function' or 'method'.
    """
    func_name = f"{prefix}.{node.name}" if prefix else node.name
    docstring = ast.get_docstring(node)
    if docstring:
        results.append(
            {
                "kind": kind,
                "name": func_name,
                "docstring": docstring,
                "line": node.lineno,
                "file": file_path,
            }
        )


def _extract_comments(source: str, file_path: str) -> list[dict[str, Any]]:
    """Extract significant comments from source code.

    Significant comments are either:
    - Single lines containing TODO, FIXME, NOTE, or HACK markers
    - Blocks of 3+ consecutive comment-only lines

    Inline comments (where code precedes the ``#``) are ignored.

    Args:
        source: The source code text.
        file_path: Path to the source file (for metadata).

    Returns:
        A list of dicts with keys: text, line, file, marker (optional).
    """
    lines = source.splitlines()
    results: list[dict[str, Any]] = []
    block: list[tuple[int, str]] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if _is_comment_line(stripped):
            comment_text = stripped.lstrip("# ")
            block.append((i, comment_text))
        else:
            _flush_comment_block(block, file_path, results)
            block = []

    # Flush any trailing block
    _flush_comment_block(block, file_path, results)
    return results


def _is_comment_line(stripped: str) -> bool:
    """Check if a line is a standalone comment (not inline).

    Args:
        stripped: The stripped line text.

    Returns:
        True if the line starts with ``#``.
    """
    return stripped.startswith("#")


def _flush_comment_block(
    block: list[tuple[int, str]],
    file_path: str,
    results: list[dict[str, Any]],
) -> None:
    """Process a collected comment block and emit it if significant.

    A block is significant if it contains a marker keyword or has
    3+ consecutive lines.

    Args:
        block: List of (line_number, comment_text) tuples.
        file_path: Path to the source file.
        results: List to append results to.
    """
    if not block:
        return

    full_text = "\n".join(text for _, text in block)
    start_line = block[0][0]
    marker = _find_marker(full_text)

    if marker or len(block) >= _MIN_COMMENT_BLOCK_LINES:
        entry: dict[str, Any] = {
            "text": full_text,
            "line": start_line,
            "file": file_path,
        }
        if marker:
            entry["marker"] = marker
        results.append(entry)


def _find_marker(text: str) -> str | None:
    """Find a significant comment marker in text.

    Args:
        text: The comment text to search.

    Returns:
        The marker string if found, or None.
    """
    for marker in _SIGNIFICANT_COMMENT_MARKERS:
        if marker in text.upper():
            return marker
    return None


def _get_commit_messages(repo_path: Path) -> str:
    """Get recent commit messages from a git repository.

    Runs ``git log --oneline --no-merges -50`` in the repository directory.

    Args:
        repo_path: Path to the repository root.

    Returns:
        The raw output string from git log, or empty string on error.
    """
    git_path = shutil.which("git")
    if git_path is None:
        logger.warning("git not found on PATH for %s", repo_path)
        return ""

    try:
        result = subprocess.run(  # nosec B603 — hardcoded args, no user input
            [git_path, "log", "--oneline", "--no-merges", "-50"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        logger.warning("Could not retrieve git log for %s", repo_path)
        return ""
    else:
        return result.stdout.strip()


def _get_file_timestamp(path: Path) -> datetime:
    """Get a file's modification timestamp as a timezone-aware datetime.

    Args:
        path: The file path to inspect.

    Returns:
        A timezone-aware datetime from the file's modification time.
    """
    from creek.ingest.base import LA_TZ

    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=LA_TZ)


def _is_adr_file(path: Path) -> bool:
    """Check if a file is an Architecture Decision Record.

    Matches files in directories containing 'ADR' (case-insensitive)
    or files named with the ``adr-`` prefix.

    Args:
        path: The file path to check.

    Returns:
        True if the file appears to be an ADR.
    """
    path_lower = str(path).lower()
    return "/adr/" in path_lower or path.name.lower().startswith("adr-")


def _is_readme_file(path: Path) -> bool:
    """Check if a file is a README.

    Args:
        path: The file path to check.

    Returns:
        True if the filename matches a known README pattern.
    """
    return path.name in _README_PATTERNS


def _is_claude_md(path: Path) -> bool:
    """Check if a file is a CLAUDE.md project context file.

    Args:
        path: The file path to check.

    Returns:
        True if the filename is CLAUDE.md.
    """
    return path.name == "CLAUDE.md"


def _derive_title(fragment: ParsedFragment) -> str:
    """Derive a title from a parsed fragment based on its artifact type.

    Args:
        fragment: The parsed fragment.

    Returns:
        A descriptive title string.
    """
    artifact_type = fragment.metadata.get("artifact_type", "")

    if artifact_type in ("readme", "adr", "claude_md"):
        heading_match = re.match(r"^#\s+(.+)$", fragment.content, re.MULTILINE)
        if heading_match:
            return heading_match.group(1).strip()
        return Path(fragment.source_path).name

    if artifact_type == "docstring":
        kind = fragment.metadata.get("kind", "")
        name = fragment.metadata.get("name", "")
        return f"{kind}: {name}" if name else Path(fragment.source_path).stem

    if artifact_type == "comment":
        text = fragment.content[:60].strip()
        return f"Comment: {text}"

    if artifact_type == "commits":
        return f"Commit log: {Path(fragment.source_path).name}"

    return Path(fragment.source_path).stem


# ---- CodeIngestor ----


class CodeIngestor(Ingestor):
    """Ingestor for code repositories and standalone source files.

    Extracts human-readable insights from code repositories: READMEs,
    CLAUDE.md project context, Architecture Decision Records, significant
    comments, Python docstrings, and commit messages. Skips generated
    and vendor directories (node_modules, .venv, __pycache__, etc.).
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all relevant code files at the given source path.

        Locates repositories (directories with ``.git/``) and standalone
        source files. Discovers READMEs, CLAUDE.md, ADRs, and Python
        files while skipping generated/vendor directories.

        Args:
            source_path: A file or directory path to search.

        Returns:
            A list of ``RawDocument`` objects for each discovered file.
        """
        if not source_path.exists():
            return []

        if source_path.is_file():
            return self._read_single_file(source_path)

        docs: list[RawDocument] = []
        self._discover_directory(source_path, docs)
        return docs

    def _read_single_file(self, file_path: Path) -> list[RawDocument]:
        """Read a single file into a RawDocument.

        Args:
            file_path: Path to the source file.

        Returns:
            A single-element list containing the RawDocument.
        """
        raw_bytes = file_path.read_bytes()
        _text, encoding = normalize_encoding(raw_bytes)
        artifact_type = self._classify_file(file_path)
        return [
            RawDocument(
                path=file_path,
                content=raw_bytes,
                metadata={"source_type": "code", "artifact_type": artifact_type},
                detected_encoding=encoding,
            )
        ]

    def _discover_directory(self, dir_path: Path, docs: list[RawDocument]) -> None:
        """Recursively discover relevant code files in a directory.

        Args:
            dir_path: Directory to search.
            docs: List to append discovered documents to.
        """
        # Discover regular files
        for item in sorted(dir_path.iterdir()):
            if item.is_dir():
                if not _is_skip_directory(item):
                    self._discover_directory(item, docs)
            elif item.is_file() and self._is_relevant_file(item):
                self._add_file_document(item, docs)

        # Discover commit messages for git repos
        if (dir_path / ".git").is_dir():
            self._add_commit_document(dir_path, docs)

    def _is_relevant_file(self, path: Path) -> bool:
        """Check if a file is relevant for code ingestion.

        Relevant files are READMEs, CLAUDE.md, ADRs, and Python files.

        Args:
            path: The file path to check.

        Returns:
            True if the file should be ingested.
        """
        if _is_readme_file(path):
            return True
        if _is_claude_md(path):
            return True
        if _is_adr_file(path) and path.suffix in (".md", ".rst", ".txt"):
            return True
        return path.suffix == ".py"

    def _classify_file(self, path: Path) -> str:
        """Classify a file by its artifact type.

        Args:
            path: The file path to classify.

        Returns:
            A string artifact type identifier.
        """
        if _is_readme_file(path):
            return "readme"
        if _is_claude_md(path):
            return "claude_md"
        if _is_adr_file(path):
            return "adr"
        if path.suffix == ".py":
            return "python"
        return "other"

    def _add_file_document(self, file_path: Path, docs: list[RawDocument]) -> None:
        """Read a file and add it to the document list.

        Args:
            file_path: Path to the file.
            docs: List to append the document to.
        """
        try:
            raw_bytes = file_path.read_bytes()
        except (OSError, PermissionError):
            logger.warning("Could not read file: %s", file_path)
            return

        _text, encoding = normalize_encoding(raw_bytes)
        artifact_type = self._classify_file(file_path)
        docs.append(
            RawDocument(
                path=file_path,
                content=raw_bytes,
                metadata={"source_type": "code", "artifact_type": artifact_type},
                detected_encoding=encoding,
            )
        )

    def _add_commit_document(self, repo_path: Path, docs: list[RawDocument]) -> None:
        """Create a virtual document for commit messages.

        Args:
            repo_path: Path to the git repository root.
            docs: List to append the document to.
        """
        log_output = _get_commit_messages(repo_path)
        if log_output:
            docs.append(
                RawDocument(
                    path=repo_path,
                    content=log_output.encode("utf-8"),
                    metadata={"source_type": "code", "artifact_type": "commits"},
                    detected_encoding="utf-8",
                )
            )

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw code document into content fragments.

        Dispatches to artifact-specific parsers based on the document's
        metadata ``artifact_type``.

        Args:
            raw: The raw document to parse.

        Returns:
            A list of ``ParsedFragment`` objects extracted from the document.
        """
        artifact_type = raw.metadata.get("artifact_type", "other")
        text, _encoding = normalize_encoding(raw.content)
        timestamp = _get_file_timestamp(raw.path)

        if artifact_type in ("readme", "claude_md", "adr"):
            return self._parse_markdown_artifact(raw, text, timestamp, artifact_type)

        if artifact_type == "python":
            return self._parse_python_file(raw, text, timestamp)

        if artifact_type == "commits":
            return self._parse_commits(raw, text, timestamp)

        return []

    def _parse_markdown_artifact(
        self,
        raw: RawDocument,
        text: str,
        timestamp: datetime,
        artifact_type: str,
    ) -> list[ParsedFragment]:
        """Parse a markdown-based artifact (README, CLAUDE.md, ADR).

        Args:
            raw: The raw document.
            text: Decoded text content.
            timestamp: File timestamp.
            artifact_type: The artifact type identifier.

        Returns:
            A single-element list containing the parsed fragment.
        """
        return [
            ParsedFragment(
                content=text,
                metadata={"artifact_type": artifact_type},
                source_path=str(raw.path),
                timestamp=timestamp,
            )
        ]

    def _parse_python_file(
        self,
        raw: RawDocument,
        text: str,
        timestamp: datetime,
    ) -> list[ParsedFragment]:
        """Parse a Python file for docstrings and significant comments.

        Args:
            raw: The raw document.
            text: Decoded Python source text.
            timestamp: File timestamp.

        Returns:
            A list of fragments for each docstring and significant comment.
        """
        fragments: list[ParsedFragment] = []
        source_path = str(raw.path)

        # Extract docstrings
        for doc_info in _extract_docstrings(text, source_path):
            fragments.append(
                ParsedFragment(
                    content=doc_info["docstring"],
                    metadata={
                        "artifact_type": "docstring",
                        "kind": doc_info["kind"],
                        "name": doc_info["name"],
                        "line": doc_info["line"],
                    },
                    source_path=source_path,
                    timestamp=timestamp,
                )
            )

        # Extract significant comments
        for comment_info in _extract_comments(text, source_path):
            metadata: dict[str, Any] = {
                "artifact_type": "comment",
                "line": comment_info["line"],
            }
            if "marker" in comment_info:
                metadata["marker"] = comment_info["marker"]
            fragments.append(
                ParsedFragment(
                    content=comment_info["text"],
                    metadata=metadata,
                    source_path=source_path,
                    timestamp=timestamp,
                )
            )

        return fragments

    def _parse_commits(
        self,
        raw: RawDocument,
        text: str,
        timestamp: datetime,
    ) -> list[ParsedFragment]:
        """Parse commit messages into a single fragment.

        Args:
            raw: The raw document.
            text: The git log output text.
            timestamp: Repository timestamp.

        Returns:
            A single-element list containing the commit log fragment.
        """
        if not text.strip():
            return []

        return [
            ParsedFragment(
                content=text,
                metadata={"artifact_type": "commits"},
                source_path=str(raw.path),
                timestamp=timestamp,
            )
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to clean Markdown.

        READMEs, ADRs, and CLAUDE.md files are preserved as-is.
        Docstrings and comments are formatted with source context.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string.
        """
        artifact_type = fragment.metadata.get("artifact_type", "")

        if artifact_type in ("readme", "adr", "claude_md"):
            return fragment.content

        if artifact_type == "docstring":
            return self._format_docstring(fragment)

        if artifact_type == "comment":
            return self._format_comment(fragment)

        if artifact_type == "commits":
            return self._format_commits(fragment)

        return fragment.content

    def _format_docstring(self, fragment: ParsedFragment) -> str:
        """Format a docstring fragment as markdown with context.

        Args:
            fragment: The docstring fragment.

        Returns:
            Formatted markdown string.
        """
        kind = fragment.metadata.get("kind", "")
        name = fragment.metadata.get("name", "")
        line = fragment.metadata.get("line", "")
        source = Path(fragment.source_path).name

        header = f"**{kind.title()}**: `{name}` ({source}:{line})"
        return f"{header}\n\n{fragment.content}"

    def _format_comment(self, fragment: ParsedFragment) -> str:
        """Format a comment fragment as markdown with context.

        Args:
            fragment: The comment fragment.

        Returns:
            Formatted markdown string.
        """
        line = fragment.metadata.get("line", "")
        source = Path(fragment.source_path).name
        marker = fragment.metadata.get("marker", "")

        header_parts = [f"**Comment** ({source}:{line})"]
        if marker:
            header_parts.append(f"[{marker}]")

        header = " ".join(header_parts)
        return f"{header}\n\n> {fragment.content}"

    def _format_commits(self, fragment: ParsedFragment) -> str:
        """Format commit messages as markdown.

        Args:
            fragment: The commit log fragment.

        Returns:
            Formatted markdown string.
        """
        source = Path(fragment.source_path).name
        header = f"**Commit Log**: {source}"
        lines = fragment.content.strip().splitlines()
        formatted = "\n".join(f"- {line}" for line in lines)
        return f"{header}\n\n{formatted}"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a parsed fragment.

        Sets the platform to ``code`` and includes the source file path
        and artifact type.

        Args:
            fragment: The parsed fragment with metadata.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        title = _derive_title(fragment)
        artifact_type = fragment.metadata.get("artifact_type", "")

        return {
            "type": "fragment",
            "title": title,
            "source": {
                "platform": SourcePlatform.CODE,
                "original_file": fragment.source_path,
            },
            "created": fragment.timestamp.isoformat(),
            "artifact_type": artifact_type,
        }
