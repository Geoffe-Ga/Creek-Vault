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

import frontmatter
import yaml

from creek.lint._result import CheckResult

_COMPOST_DIR: tuple[str, ...] = ("10-Liminal", "Compost")


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Walk ``10-Liminal/Compost/`` and report each recorded compost note."""
    del since
    compost_dir = vault_path.joinpath(*_COMPOST_DIR)
    findings: list[str] = []
    if compost_dir.is_dir():
        for note in sorted(compost_dir.glob("*.md")):
            if note.stem.startswith("_"):
                continue  # skip the rollup report itself
            try:
                post = frontmatter.load(str(note))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            if post.get("type") != "compost":
                continue
            title = str(post.get("title") or note.stem)
            findings.append(f"- `{title}` (`{note.relative_to(vault_path)}`)")
    summary = f"{len(findings)} recorded compost note(s)"
    return CheckResult(name="compost", summary=summary, findings=findings)
