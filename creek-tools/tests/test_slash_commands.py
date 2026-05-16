"""Test the `/creek` slash command skill files (FEAT-016).

Each `.claude/commands/<name>.md` file is a Claude Code slash command:
optional YAML frontmatter followed by a Markdown body that becomes the
prompt when the user types ``/creek <name>``. The tests here pin the
file set, the frontmatter shape, and that every command's body
references a real MCP tool name from the `creek-tools-mcp` surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMMANDS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "commands"

_EXPECTED_COMMANDS = {
    "creek",  # the root command (no-arg fallback to creek state)
    "state",
    "lint",
    "mine",
    "draft",
    "save",
    "explain",
    "phase",
    "wavelength",
    "skills",
    "ingest",
}


def test_commands_directory_exists() -> None:
    """The .claude/commands directory ships with creek-tools."""
    assert _COMMANDS_DIR.is_dir(), f"{_COMMANDS_DIR} should exist"


def test_all_required_commands_have_files() -> None:
    """Each of the 11 documented commands has a markdown file."""
    on_disk = {p.stem for p in _COMMANDS_DIR.glob("*.md")}
    missing = _EXPECTED_COMMANDS - on_disk
    assert not missing, f"missing command files: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(_EXPECTED_COMMANDS))
def test_command_frontmatter_parses(name: str) -> None:
    """Every command file has parseable YAML frontmatter with a description."""
    body = (_COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(body)
    assert frontmatter is not None, f"{name}.md missing frontmatter"
    assert "description" in frontmatter, f"{name}.md missing description"
    assert frontmatter["description"].strip(), f"{name}.md description is empty"


@pytest.mark.parametrize(
    ("name", "expected_tool"),
    [
        ("creek", "creek.state"),
        ("state", "creek.state"),
        ("lint", "creek.lint"),
        ("mine", "creek.mine"),
        ("draft", "creek.draft"),
        ("save", "creek.save"),
        ("phase", "creek.state"),
        ("wavelength", "creek.state"),
        ("skills", "creek.skills"),
        ("ingest", "creek.ingest"),
    ],
)
def test_command_body_references_mcp_tool(name: str, expected_tool: str) -> None:
    """Each command's body names the MCP tool it routes through."""
    body = (_COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert expected_tool in body, (
        f"{name}.md must mention the {expected_tool!r} MCP tool"
    )


def test_explain_command_body_describes_its_purpose() -> None:
    """``/creek explain`` is the help command — narrative, not a tool call."""
    body = (_COMMANDS_DIR / "explain.md").read_text(encoding="utf-8")
    assert "help" in body.lower() or "explain" in body.lower()


def test_root_creek_command_documents_default_action() -> None:
    """Bare ``/creek`` (no args) renders the state report — documented in body."""
    body = (_COMMANDS_DIR / "creek.md").read_text(encoding="utf-8")
    assert "state" in body.lower()


def _extract_frontmatter(text: str) -> dict[str, object] | None:
    """Return the parsed YAML frontmatter, or ``None`` if absent."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    raw = text[4:end]
    loaded = yaml.safe_load(raw)
    return loaded if isinstance(loaded, dict) else None
