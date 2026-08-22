"""Deterministic check: surface recorded compost notes.

The full :class:`creek.generate.compost.CompostTracker` scans threads
and fragments for dormancy. Inside lint we walk the already-written
notes under ``10-Liminal/Compost/`` and report their count — the
expensive scan still happens via ``creek report --type compost`` (or a
future :mod:`creek.generate.compost` re-entry point).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult
from creek.vault.links import read_header_meta

_COMPOST_DIR: tuple[str, ...] = ("10-Liminal", "Compost")


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Walk ``10-Liminal/Compost/`` and report each recorded compost note.

    Only the header is read, via :func:`~creek.vault.links.read_header_meta`:
    this check reports a note's ``type`` and ``title`` and never looks at a
    body. That also makes it immune to the ``**metadata`` splat in
    ``frontmatter.load``, which a note carrying a non-string frontmatter key
    turned into a run-ending ``TypeError`` (#1475). An unreadable header yields
    an empty mapping, which fails the ``type`` test and skips the note exactly
    as the old guard did.

    Header-only reading carries the same three deliberate consequences #1416
    accepted and documents in full at
    :func:`creek.generate.synchronicity._existing_synchronicity_pairs`: the
    ``---`` fence must open line 1, the 200-line / 64 KB header caps apply, and
    a note carrying a stray non-string key is tolerated rather than rejected.
    """
    del since
    compost_dir = vault_path.joinpath(*_COMPOST_DIR)
    findings: list[str] = []
    if compost_dir.is_dir():
        for note in sorted(compost_dir.glob("*.md")):
            if note.stem.startswith("_"):
                continue  # skip the rollup report itself
            meta = read_header_meta(note)
            if meta.get("type") != "compost":
                continue
            title = str(meta.get("title") or note.stem)
            findings.append(f"- `{title}` (`{note.relative_to(vault_path)}`)")
    summary = f"{len(findings)} recorded compost note(s)"
    return CheckResult(name="compost", summary=summary, findings=findings)
