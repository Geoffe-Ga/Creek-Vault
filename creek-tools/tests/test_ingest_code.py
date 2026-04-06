"""Tests for creek.ingest.code — Code repository ingestor.

Covers discovery of repositories and standalone files, parsing of READMEs,
CLAUDE.md files, ADRs, significant comments, Python docstrings, and commit
messages. Also tests skip-directory logic, convert_to_markdown formatting,
frontmatter generation, full pipeline integration, and registry registration.
"""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import IngestResult, ParsedFragment, RawDocument
from creek.ingest.code import (
    CodeIngestor,
    _extract_comments,
    _extract_docstrings,
    _is_skip_directory,
)
from creek.models import SourcePlatform

LA_TZ = ZoneInfo("America/Los_Angeles")


# ---- Fixtures ----


@pytest.fixture()
def code_ingestor() -> CodeIngestor:
    """Create a CodeIngestor instance for testing."""
    return CodeIngestor()


@pytest.fixture()
def mock_repo(tmp_path: Path) -> Path:
    """Create a mock git repository with various code artifacts.

    Returns:
        Path to the temporary repo directory.
    """
    repo = tmp_path / "my-project"
    repo.mkdir()
    (repo / ".git").mkdir()

    # README.md
    readme = repo / "README.md"
    readme.write_text(
        "# My Project\n\nA sample project for testing.\n\n"
        "## Installation\n\n```bash\npip install my-project\n```\n",
        encoding="utf-8",
    )

    # CLAUDE.md
    claude_md = repo / "CLAUDE.md"
    claude_md.write_text(
        "# Project Context\n\nThis project uses Python 3.11.\n",
        encoding="utf-8",
    )

    # ADR file
    adr_dir = repo / "docs" / "architecture" / "ADR"
    adr_dir.mkdir(parents=True)
    adr = adr_dir / "001-use-pydantic.md"
    adr.write_text(
        "# ADR-001: Use Pydantic for Data Models\n\n"
        "## Status\nAccepted\n\n"
        "## Decision\nWe will use Pydantic v2.\n",
        encoding="utf-8",
    )

    # Python file with docstrings and significant comments
    src_dir = repo / "src"
    src_dir.mkdir()
    py_file = src_dir / "main.py"
    py_file.write_text(
        textwrap.dedent('''\
        """Main module for the application.

        This module provides the entry point for the CLI.
        """


        # TODO: refactor this function to use async
        def run() -> None:
            """Run the main application loop.

            This function initializes all subsystems
            and starts the event loop.
            """
            pass


        # NOTE: This is a critical section.
        # Do not modify without consulting the team.
        # Performance depends on this ordering.
        def process(data: list[str]) -> list[str]:
            """Process input data through the pipeline."""
            return data
        '''),
        encoding="utf-8",
    )

    # Python file with no docstrings (should yield nothing)
    bare_py = src_dir / "bare.py"
    bare_py.write_text("x = 1\ny = 2\n", encoding="utf-8")

    # README.rst
    readme_rst = repo / "README.rst"
    readme_rst.write_text(
        "My Project\n==========\n\nA reStructuredText readme.\n",
        encoding="utf-8",
    )

    # Skip directories: should be ignored
    node_modules = repo / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("// bundled", encoding="utf-8")

    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site.py").write_text("# venv", encoding="utf-8")

    pycache = repo / "src" / "__pycache__"
    pycache.mkdir()
    (pycache / "main.cpython-311.pyc").write_bytes(b"\x00\x00")

    return repo


@pytest.fixture()
def standalone_files(tmp_path: Path) -> Path:
    """Create standalone code files (no .git directory).

    Returns:
        Path to the temporary directory.
    """
    py_file = tmp_path / "script.py"
    py_file.write_text(
        '"""A standalone script."""\n\n'
        'def hello() -> None:\n    """Say hello."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    return tmp_path


# ---- Skip Directory Tests ----


class TestIsSkipDirectory:
    """Tests for _is_skip_directory helper function."""

    def test_skips_node_modules(self) -> None:
        """Should skip node_modules directory."""
        assert _is_skip_directory(Path("/project/node_modules"))

    def test_skips_vendor(self) -> None:
        """Should skip vendor directory."""
        assert _is_skip_directory(Path("/project/vendor"))

    def test_skips_dot_git(self) -> None:
        """Should skip .git directory."""
        assert _is_skip_directory(Path("/project/.git"))

    def test_skips_pycache(self) -> None:
        """Should skip __pycache__ directory."""
        assert _is_skip_directory(Path("/project/__pycache__"))

    def test_skips_dot_tox(self) -> None:
        """Should skip .tox directory."""
        assert _is_skip_directory(Path("/project/.tox"))

    def test_skips_dot_venv(self) -> None:
        """Should skip .venv directory."""
        assert _is_skip_directory(Path("/project/.venv"))

    def test_allows_src(self) -> None:
        """Should not skip regular source directories."""
        assert not _is_skip_directory(Path("/project/src"))

    def test_allows_docs(self) -> None:
        """Should not skip docs directories."""
        assert not _is_skip_directory(Path("/project/docs"))


# ---- Docstring Extraction Tests ----


class TestExtractDocstrings:
    """Tests for _extract_docstrings helper function."""

    def test_extracts_module_docstring(self) -> None:
        """Should extract module-level docstrings."""
        source = '"""Module docstring."""\n\nx = 1\n'
        results = _extract_docstrings(source, "test.py")
        module_docs = [r for r in results if r["kind"] == "module"]
        assert len(module_docs) == 1
        assert module_docs[0]["docstring"] == "Module docstring."

    def test_extracts_function_docstring(self) -> None:
        """Should extract function docstrings."""
        source = 'def foo():\n    """Foo does things."""\n    pass\n'
        results = _extract_docstrings(source, "test.py")
        func_docs = [r for r in results if r["kind"] == "function"]
        assert len(func_docs) == 1
        assert func_docs[0]["name"] == "foo"
        assert func_docs[0]["docstring"] == "Foo does things."

    def test_extracts_class_docstring(self) -> None:
        """Should extract class docstrings."""
        source = 'class Bar:\n    """Bar is a class."""\n    pass\n'
        results = _extract_docstrings(source, "test.py")
        class_docs = [r for r in results if r["kind"] == "class"]
        assert len(class_docs) == 1
        assert class_docs[0]["name"] == "Bar"

    def test_skips_undocumented_functions(self) -> None:
        """Should not extract entries for functions without docstrings."""
        source = "def foo():\n    pass\n"
        results = _extract_docstrings(source, "test.py")
        func_docs = [r for r in results if r["kind"] == "function"]
        assert len(func_docs) == 0

    def test_handles_syntax_error(self) -> None:
        """Should return empty list for unparseable Python files."""
        source = "def foo(:\n    pass\n"
        results = _extract_docstrings(source, "test.py")
        assert results == []

    def test_extracts_method_docstring(self) -> None:
        """Should extract method docstrings from classes."""
        source = (
            "class Foo:\n"
            '    """Foo class."""\n\n'
            "    def bar(self):\n"
            '        """Bar method."""\n'
            "        pass\n"
        )
        results = _extract_docstrings(source, "test.py")
        method_docs = [r for r in results if r["kind"] == "method"]
        assert len(method_docs) == 1
        assert method_docs[0]["name"] == "Foo.bar"


# ---- Comment Extraction Tests ----


class TestExtractComments:
    """Tests for _extract_comments helper function."""

    def test_extracts_todo_comment(self) -> None:
        """Should extract single-line TODO comments."""
        source = "# TODO: fix this later\nx = 1\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 1
        assert "TODO" in results[0]["text"]

    def test_extracts_fixme_comment(self) -> None:
        """Should extract FIXME comments."""
        source = "# FIXME: broken logic here\nx = 1\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 1

    def test_extracts_note_comment(self) -> None:
        """Should extract NOTE comments."""
        source = "# NOTE: important detail\nx = 1\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 1

    def test_extracts_hack_comment(self) -> None:
        """Should extract HACK comments."""
        source = "# HACK: workaround for bug\nx = 1\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 1

    def test_extracts_multiline_comment_block(self) -> None:
        """Should extract comment blocks of 3+ consecutive lines."""
        source = (
            "# This is a long comment\n"
            "# that spans multiple lines\n"
            "# and provides important context\n"
            "x = 1\n"
        )
        results = _extract_comments(source, "test.py")
        assert len(results) == 1
        assert "long comment" in results[0]["text"]

    def test_ignores_short_comments(self) -> None:
        """Should ignore comment blocks shorter than 3 lines without markers."""
        source = "# short comment\nx = 1\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 0

    def test_ignores_inline_comments(self) -> None:
        """Should ignore inline comments (code before #)."""
        source = "x = 1  # inline comment\n"
        results = _extract_comments(source, "test.py")
        assert len(results) == 0


# ---- Discovery Tests ----


class TestCodeIngestorDiscover:
    """Tests for CodeIngestor.discover()."""

    def test_discovers_repo(self, code_ingestor: CodeIngestor, mock_repo: Path) -> None:
        """Should discover files in a git repository."""
        docs = code_ingestor.discover(mock_repo)
        assert len(docs) > 0

    def test_discovers_readme_md(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should discover README.md files."""
        docs = code_ingestor.discover(mock_repo)
        readme_docs = [d for d in docs if d.path.name == "README.md"]
        assert len(readme_docs) == 1

    def test_discovers_readme_rst(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should discover README.rst files."""
        docs = code_ingestor.discover(mock_repo)
        rst_docs = [d for d in docs if d.path.name == "README.rst"]
        assert len(rst_docs) == 1

    def test_discovers_claude_md(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should discover CLAUDE.md files."""
        docs = code_ingestor.discover(mock_repo)
        claude_docs = [d for d in docs if d.path.name == "CLAUDE.md"]
        assert len(claude_docs) == 1

    def test_discovers_adr_files(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should discover Architecture Decision Record files."""
        docs = code_ingestor.discover(mock_repo)
        adr_docs = [d for d in docs if "ADR" in str(d.path)]
        assert len(adr_docs) == 1

    def test_discovers_python_files(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should discover Python source files."""
        docs = code_ingestor.discover(mock_repo)
        py_docs = [d for d in docs if str(d.path).endswith(".py")]
        # main.py and bare.py (not in __pycache__ or .venv)
        assert len(py_docs) == 2

    def test_skips_node_modules(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should skip files in node_modules."""
        docs = code_ingestor.discover(mock_repo)
        # Check paths relative to repo root to avoid test dir name pollution
        nm_docs = [d for d in docs if (mock_repo / "node_modules") in d.path.parents]
        assert len(nm_docs) == 0

    def test_skips_venv(self, code_ingestor: CodeIngestor, mock_repo: Path) -> None:
        """Should skip files in .venv."""
        docs = code_ingestor.discover(mock_repo)
        venv_docs = [d for d in docs if (mock_repo / ".venv") in d.path.parents]
        assert len(venv_docs) == 0

    def test_skips_pycache(self, code_ingestor: CodeIngestor, mock_repo: Path) -> None:
        """Should skip files in __pycache__."""
        docs = code_ingestor.discover(mock_repo)
        cache_docs = [
            d for d in docs if (mock_repo / "src" / "__pycache__") in d.path.parents
        ]
        assert len(cache_docs) == 0

    def test_returns_raw_documents(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should return RawDocument instances."""
        docs = code_ingestor.discover(mock_repo)
        assert all(isinstance(d, RawDocument) for d in docs)

    def test_nonexistent_path(self, code_ingestor: CodeIngestor) -> None:
        """Should return empty list for non-existent path."""
        docs = code_ingestor.discover(Path("/nonexistent/path"))
        assert docs == []

    def test_empty_directory(self, code_ingestor: CodeIngestor, tmp_path: Path) -> None:
        """Should return empty list for empty directory."""
        docs = code_ingestor.discover(tmp_path)
        assert docs == []


# ---- Parse Tests ----


class TestCodeIngestorParse:
    """Tests for CodeIngestor.parse()."""

    def test_parse_readme(self, code_ingestor: CodeIngestor, mock_repo: Path) -> None:
        """Should parse README files into a single fragment."""
        docs = code_ingestor.discover(mock_repo)
        readme_doc = next(d for d in docs if d.path.name == "README.md")
        fragments = code_ingestor.parse(readme_doc)
        assert len(fragments) >= 1
        readme_frags = [
            f for f in fragments if f.metadata.get("artifact_type") == "readme"
        ]
        assert len(readme_frags) == 1
        assert "My Project" in readme_frags[0].content

    def test_parse_python_docstrings(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract Python docstrings from source files."""
        docs = code_ingestor.discover(mock_repo)
        py_doc = next(d for d in docs if d.path.name == "main.py")
        fragments = code_ingestor.parse(py_doc)
        docstring_frags = [
            f for f in fragments if f.metadata.get("artifact_type") == "docstring"
        ]
        # module + run + process docstrings
        assert len(docstring_frags) >= 3

    def test_parse_significant_comments(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract significant comments from Python files."""
        docs = code_ingestor.discover(mock_repo)
        py_doc = next(d for d in docs if d.path.name == "main.py")
        fragments = code_ingestor.parse(py_doc)
        comment_frags = [
            f for f in fragments if f.metadata.get("artifact_type") == "comment"
        ]
        # TODO comment + 3-line NOTE block
        assert len(comment_frags) >= 2

    def test_parse_adr(self, code_ingestor: CodeIngestor, mock_repo: Path) -> None:
        """Should parse ADR files into fragments."""
        docs = code_ingestor.discover(mock_repo)
        adr_doc = next(d for d in docs if "ADR" in str(d.path))
        fragments = code_ingestor.parse(adr_doc)
        adr_frags = [f for f in fragments if f.metadata.get("artifact_type") == "adr"]
        assert len(adr_frags) == 1
        assert "Pydantic" in adr_frags[0].content

    def test_parse_sets_source_path(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should set source_path on all parsed fragments."""
        docs = code_ingestor.discover(mock_repo)
        readme_doc = next(d for d in docs if d.path.name == "README.md")
        fragments = code_ingestor.parse(readme_doc)
        assert all(f.source_path == str(readme_doc.path) for f in fragments)

    def test_parse_sets_timestamp(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should set a timestamp on all parsed fragments."""
        docs = code_ingestor.discover(mock_repo)
        fragments = code_ingestor.parse(docs[0])
        assert all(isinstance(f.timestamp, datetime) for f in fragments)

    def test_parse_bare_python_has_no_docstrings(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should produce no docstring fragments for files without docstrings."""
        docs = code_ingestor.discover(mock_repo)
        bare_doc = next(d for d in docs if d.path.name == "bare.py")
        fragments = code_ingestor.parse(bare_doc)
        docstring_frags = [
            f for f in fragments if f.metadata.get("artifact_type") == "docstring"
        ]
        assert len(docstring_frags) == 0

    def test_parse_returns_parsed_fragments(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should return ParsedFragment instances."""
        docs = code_ingestor.discover(mock_repo)
        fragments = code_ingestor.parse(docs[0])
        assert all(isinstance(f, ParsedFragment) for f in fragments)


# ---- convert_to_markdown Tests ----


class TestCodeIngestorConvertToMarkdown:
    """Tests for CodeIngestor.convert_to_markdown()."""

    def test_readme_preserved_as_is(self, code_ingestor: CodeIngestor) -> None:
        """Should preserve README content as-is (already markdown)."""
        fragment = ParsedFragment(
            content="# My Project\n\nContent here.\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert result == "# My Project\n\nContent here.\n"

    def test_docstring_formatted_with_context(
        self, code_ingestor: CodeIngestor
    ) -> None:
        """Should format docstrings with source context."""
        fragment = ParsedFragment(
            content="Run the main loop.",
            metadata={
                "artifact_type": "docstring",
                "kind": "function",
                "name": "run",
                "line": 10,
            },
            source_path="/fake/main.py",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert "function" in result.lower() or "run" in result
        assert "Run the main loop." in result

    def test_comment_formatted_with_context(self, code_ingestor: CodeIngestor) -> None:
        """Should format comments with source context."""
        fragment = ParsedFragment(
            content="TODO: fix this later",
            metadata={
                "artifact_type": "comment",
                "line": 5,
            },
            source_path="/fake/main.py",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert "TODO: fix this later" in result

    def test_adr_preserved(self, code_ingestor: CodeIngestor) -> None:
        """Should preserve ADR content as markdown."""
        fragment = ParsedFragment(
            content="# ADR-001: Use Pydantic\n\n## Decision\nUse it.\n",
            metadata={"artifact_type": "adr"},
            source_path="/fake/docs/ADR/001.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert "ADR-001" in result


# ---- generate_frontmatter Tests ----


class TestCodeIngestorGenerateFrontmatter:
    """Tests for CodeIngestor.generate_frontmatter()."""

    def test_platform_is_code(self, code_ingestor: CodeIngestor) -> None:
        """Should set source.platform to 'code'."""
        fragment = ParsedFragment(
            content="# README\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == SourcePlatform.CODE

    def test_includes_original_file(self, code_ingestor: CodeIngestor) -> None:
        """Should include source.original_file."""
        fragment = ParsedFragment(
            content="# README\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["source"]["original_file"] == "/fake/README.md"

    def test_includes_type_fragment(self, code_ingestor: CodeIngestor) -> None:
        """Should set type to 'fragment'."""
        fragment = ParsedFragment(
            content="# README\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["type"] == "fragment"

    def test_includes_created_timestamp(self, code_ingestor: CodeIngestor) -> None:
        """Should include a 'created' timestamp."""
        fragment = ParsedFragment(
            content="# README\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert "created" in fm

    def test_includes_artifact_type(self, code_ingestor: CodeIngestor) -> None:
        """Should include artifact_type in frontmatter."""
        fragment = ParsedFragment(
            content="Module doc.",
            metadata={"artifact_type": "docstring", "kind": "module", "name": "main"},
            source_path="/fake/main.py",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["artifact_type"] == "docstring"

    def test_title_from_readme_heading(self, code_ingestor: CodeIngestor) -> None:
        """Should derive title from first heading in README."""
        fragment = ParsedFragment(
            content="# My Project\n\nContent.\n",
            metadata={"artifact_type": "readme"},
            source_path="/fake/README.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["title"] == "My Project"

    def test_title_from_docstring_name(self, code_ingestor: CodeIngestor) -> None:
        """Should derive title from docstring name and kind."""
        fragment = ParsedFragment(
            content="Run the loop.",
            metadata={"artifact_type": "docstring", "kind": "function", "name": "run"},
            source_path="/fake/main.py",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert "run" in fm["title"].lower()


# ---- Full Pipeline Integration Tests ----


class TestCodeIngestorFullPipeline:
    """Tests for the full CodeIngestor.ingest() pipeline."""

    def test_ingest_returns_result(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Full ingest pipeline should return an IngestResult."""
        result = code_ingestor.ingest(mock_repo)
        assert isinstance(result, IngestResult)

    def test_ingest_finds_fragments(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should produce fragments from the mock repository."""
        result = code_ingestor.ingest(mock_repo)
        assert len(result.fragments) > 0

    def test_ingest_has_provenance(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should generate provenance entries."""
        result = code_ingestor.ingest(mock_repo)
        assert len(result.provenance) > 0

    def test_ingest_provenance_names_ingestor(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Provenance should identify CodeIngestor as the processing class."""
        result = code_ingestor.ingest(mock_repo)
        assert all(p.ingestor_name == "CodeIngestor" for p in result.provenance)

    def test_ingest_no_errors(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Full pipeline should complete without errors for valid repos."""
        result = code_ingestor.ingest(mock_repo)
        assert result.errors == []

    def test_ingest_fragments_have_frontmatter(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """All fragments should have frontmatter generated."""
        result = code_ingestor.ingest(mock_repo)
        for frag in result.fragments:
            assert "frontmatter" in frag.metadata

    def test_ingest_fragments_have_markdown(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """All fragments should have markdown content."""
        result = code_ingestor.ingest(mock_repo)
        for frag in result.fragments:
            assert "markdown" in frag.metadata

    def test_ingest_readme_extracted(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract README files from repos."""
        result = code_ingestor.ingest(mock_repo)
        readme_frags = [
            f for f in result.fragments if f.metadata.get("artifact_type") == "readme"
        ]
        assert len(readme_frags) >= 1

    def test_ingest_docstrings_extracted(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract Python docstrings from repos."""
        result = code_ingestor.ingest(mock_repo)
        docstring_frags = [
            f
            for f in result.fragments
            if f.metadata.get("artifact_type") == "docstring"
        ]
        assert len(docstring_frags) >= 1

    def test_ingest_comments_extracted(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract significant comments from repos."""
        result = code_ingestor.ingest(mock_repo)
        comment_frags = [
            f for f in result.fragments if f.metadata.get("artifact_type") == "comment"
        ]
        assert len(comment_frags) >= 1

    def test_ingest_skips_generated_code(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should not extract artifacts from generated/vendor directories."""
        result = code_ingestor.ingest(mock_repo)
        repo_str = str(mock_repo)
        for frag in result.fragments:
            # Get path relative to repo to avoid test dir name interference
            rel = frag.source_path.replace(repo_str, "")
            assert "/node_modules/" not in rel
            assert "/.venv/" not in rel
            assert "/__pycache__/" not in rel


# ---- Commit Message Tests ----


class TestCodeIngestorCommitMessages:
    """Tests for commit message extraction."""

    def test_parse_commit_messages(
        self, code_ingestor: CodeIngestor, mock_repo: Path
    ) -> None:
        """Should extract commit messages when git log is available."""
        # Create a mock git log file to simulate commit messages
        git_dir = mock_repo / ".git"
        assert git_dir.exists()

        # Mock subprocess to return commit messages
        mock_output = (
            "abc1234 feat: initial commit\n"
            "def5678 fix: resolve parsing bug\n"
            "ghi9012 docs: update README\n"
        )
        with patch(
            "creek.ingest.code._get_commit_messages",
            return_value=mock_output,
        ):
            docs = code_ingestor.discover(mock_repo)
            # Find the commit-messages virtual document
            commit_docs = [
                d for d in docs if d.metadata.get("artifact_type") == "commits"
            ]
            assert len(commit_docs) == 1
            fragments = code_ingestor.parse(commit_docs[0])
            assert len(fragments) == 1
            assert "initial commit" in fragments[0].content


# ---- Edge Case Tests ----


class TestCodeIngestorEdgeCases:
    """Tests for edge cases and error paths in CodeIngestor."""

    def test_should_skip_path_with_skip_dir(self) -> None:
        """Should detect skip directories in path ancestors."""
        from creek.ingest.code import _should_skip_path

        assert _should_skip_path(Path("/project/node_modules/pkg/index.js"))
        assert not _should_skip_path(Path("/project/src/main.py"))

    def test_git_not_on_path(self, mock_repo: Path) -> None:
        """Should return empty string when git is not available."""
        from creek.ingest.code import _get_commit_messages

        with patch("creek.ingest.code.shutil.which", return_value=None):
            result = _get_commit_messages(mock_repo)
            assert result == ""

    def test_git_subprocess_error(self, mock_repo: Path) -> None:
        """Should return empty on subprocess error."""
        from creek.ingest.code import _get_commit_messages

        with (
            patch("creek.ingest.code.shutil.which", return_value="/usr/bin/git"),
            patch(
                "creek.ingest.code.subprocess.run",
                side_effect=subprocess.SubprocessError("fail"),
            ),
        ):
            result = _get_commit_messages(mock_repo)
            assert result == ""

    def test_unreadable_file_skipped(
        self, code_ingestor: CodeIngestor, tmp_path: Path
    ) -> None:
        """Should skip files that raise OSError on read."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        bad_file = repo / "README.md"
        bad_file.write_text("# Hello", encoding="utf-8")

        original_read = Path.read_bytes

        def mock_read(self_path: Path) -> bytes:
            """Raise OSError for the bad file, pass through others."""
            if self_path.name == "README.md":
                msg = "Permission denied"
                raise OSError(msg)
            return original_read(self_path)

        with patch.object(Path, "read_bytes", mock_read):
            docs = code_ingestor.discover(repo)
        readme_docs = [d for d in docs if d.path.name == "README.md"]
        assert len(readme_docs) == 0

    def test_derive_title_commit_log(self, code_ingestor: CodeIngestor) -> None:
        """Should derive title for commit log fragments."""
        fragment = ParsedFragment(
            content="abc123 initial commit",
            metadata={"artifact_type": "commits"},
            source_path="/fake/my-repo",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert "Commit log" in fm["title"]

    def test_derive_title_comment(self, code_ingestor: CodeIngestor) -> None:
        """Should derive title for comment fragments."""
        fragment = ParsedFragment(
            content="TODO: fix this important thing",
            metadata={"artifact_type": "comment", "line": 5},
            source_path="/fake/main.py",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert "Comment" in fm["title"]

    def test_derive_title_unknown_artifact(self, code_ingestor: CodeIngestor) -> None:
        """Should fall back to filename for unknown artifact types."""
        fragment = ParsedFragment(
            content="some content",
            metadata={"artifact_type": "unknown"},
            source_path="/fake/myfile.txt",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = code_ingestor.generate_frontmatter(fragment)
        assert fm["title"] == "myfile"

    def test_convert_commits_to_markdown(self, code_ingestor: CodeIngestor) -> None:
        """Should format commit messages as markdown list."""
        fragment = ParsedFragment(
            content="abc123 first\ndef456 second",
            metadata={"artifact_type": "commits"},
            source_path="/fake/repo",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert "- abc123 first" in result
        assert "- def456 second" in result

    def test_convert_unknown_artifact(self, code_ingestor: CodeIngestor) -> None:
        """Should return content as-is for unknown artifact types."""
        fragment = ParsedFragment(
            content="raw content here",
            metadata={"artifact_type": "unknown"},
            source_path="/fake/file.txt",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = code_ingestor.convert_to_markdown(fragment)
        assert result == "raw content here"

    def test_parse_empty_commits(
        self, code_ingestor: CodeIngestor, tmp_path: Path
    ) -> None:
        """Should return empty list for empty commit log."""
        raw = RawDocument(
            path=tmp_path,
            content=b"   ",
            metadata={"artifact_type": "commits"},
            detected_encoding="utf-8",
        )
        fragments = code_ingestor.parse(raw)
        assert len(fragments) == 0

    def test_parse_unknown_artifact_type(
        self, code_ingestor: CodeIngestor, tmp_path: Path
    ) -> None:
        """Should return empty for unknown artifact types."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("hello", encoding="utf-8")
        raw = RawDocument(
            path=test_file,
            content=b"hello",
            metadata={"artifact_type": "other"},
            detected_encoding="utf-8",
        )
        fragments = code_ingestor.parse(raw)
        assert len(fragments) == 0

    def test_is_adr_by_filename(self) -> None:
        """Should detect ADR by filename prefix."""
        from creek.ingest.code import _is_adr_file

        assert _is_adr_file(Path("/docs/adr-001-decision.md"))
        assert not _is_adr_file(Path("/docs/readme.md"))

    def test_is_claude_md(self) -> None:
        """Should detect CLAUDE.md files."""
        from creek.ingest.code import _is_claude_md

        assert _is_claude_md(Path("/project/CLAUDE.md"))
        assert not _is_claude_md(Path("/project/README.md"))

    def test_single_file_discover(
        self, code_ingestor: CodeIngestor, tmp_path: Path
    ) -> None:
        """Should handle single file path as source."""
        py_file = tmp_path / "test.py"
        py_file.write_text('"""Module."""\n', encoding="utf-8")
        docs = code_ingestor.discover(py_file)
        assert len(docs) == 1
        assert docs[0].metadata["artifact_type"] == "python"


# ---- Registry Tests ----


class TestCodeIngestorRegistry:
    """Tests for CodeIngestor registration in INGESTOR_REGISTRY."""

    def test_registered_in_registry(self) -> None:
        """CodeIngestor should be registered in INGESTOR_REGISTRY."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "code" in INGESTOR_REGISTRY

    def test_registry_maps_to_class(self) -> None:
        """Registry entry should map to the CodeIngestor class."""
        from creek.ingest import INGESTOR_REGISTRY

        assert INGESTOR_REGISTRY["code"] is CodeIngestor
