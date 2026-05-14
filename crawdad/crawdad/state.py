"""Session-state loader: parse ``<vault>/00-Creek-Meta/State/latest.md``.

FEAT-013 §Pre-decided choices §40: "Session-state load is read-only and
cheap: just a file read + parse; no MCP call needed for the initial
load."

The parser is *forgiving* — every section is optional. The four sections
the bot actually needs (wavelength snapshot, eddies, threads, suggested
questions) land on a frozen Pydantic model. Everything else stays in
``raw_markdown`` for downstream prompt assembly.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_STATE_RELPATH = Path("00-Creek-Meta") / "State" / "latest.md"

_HEADER_WAVELENGTH = "Wavelength snapshot"
_HEADER_EDDIES = "Active eddies"
_HEADER_THREADS = "Active threads"
_HEADER_SUGGESTED = "Suggested questions"

_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")


class StateUnavailableError(RuntimeError):
    """Raised when ``latest.md`` is missing or unreadable.

    The bot catches this and replies with the documented guidance ("no
    audit report yet — run `creek state`") rather than crashing.
    """


class SessionState(BaseModel):
    """Structured view of ``State/latest.md`` for in-memory session use."""

    model_config = ConfigDict(frozen=True)

    raw_markdown: str
    wavelength_snapshot: str | None
    eddies: tuple[str, ...]
    threads: tuple[str, ...]
    suggested_questions: tuple[str, ...]


def load_session_state(vault_path: Path) -> SessionState:
    """Read and parse ``<vault>/00-Creek-Meta/State/latest.md``.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        A frozen :class:`SessionState` with the four FEAT-013 sections.

    Raises:
        StateUnavailableError: when the file does not exist.
    """
    path = vault_path / _STATE_RELPATH
    if not path.is_file():
        msg = (
            f"latest.md not found at {path}; run `creek state` to generate "
            "the audit report."
        )
        raise StateUnavailableError(msg)

    raw = path.read_text(encoding="utf-8")
    sections = _split_sections(raw)
    return SessionState(
        raw_markdown=raw,
        wavelength_snapshot=sections.get(_HEADER_WAVELENGTH),
        eddies=_extract_bullets(sections.get(_HEADER_EDDIES)),
        threads=_extract_bullets(sections.get(_HEADER_THREADS)),
        suggested_questions=_extract_bullets(sections.get(_HEADER_SUGGESTED)),
    )


def _split_sections(markdown: str) -> dict[str, str]:
    """Return ``{section_title: body}`` for every ``## …`` heading."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in markdown.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip("\n")
            current = match.group("title").strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip("\n")
    return sections


def _extract_bullets(body: str | None) -> tuple[str, ...]:
    """Return the leading ``- `` items from *body*, in document order."""
    if not body:
        return ()
    items: list[str] = []
    for line in body.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            items.append(match.group(1).strip())
    return tuple(items)
